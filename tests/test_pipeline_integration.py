"""真实 7z 集成测试：用 7z 现场构造嵌套/加密压缩包，验证端到端流水线。"""
import subprocess
from pathlib import Path

import pytest

from core.config import Config, detect_sevenzip
from core.models import TaskStatus
from core.pipeline import Pipeline
from core.sevenzip import SevenZip
from core.store import TaskStore
from core.vault import PasswordVault


def _sevenzip_or_skip() -> Path:
    try:
        return detect_sevenzip()
    except FileNotFoundError:
        pytest.skip("未找到 7z.exe，跳过集成测试")


def _run7z(exe: Path, args: list[str], cwd: Path) -> None:
    proc = subprocess.run([str(exe), *args], cwd=str(cwd), capture_output=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def _make_env(tmp_path: Path):
    exe = _sevenzip_or_skip()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    db = tmp_path / "app.db"
    cfg = Config.create(sevenzip=str(exe), workdir=tmp_path / "wd")
    vault = PasswordVault(db)
    store = TaskStore(db)
    pipe = Pipeline(cfg, vault, store)
    return exe, inputs, cfg, vault, pipe


def test_nested_encrypted_zip(tmp_path):
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "note.txt").write_text("hello 开镜", encoding="utf-8")
    _run7z(exe, ["a", "inner.zip", "note.txt"], stage)
    _run7z(exe, ["a", "-ppw1234", str(inputs / "outer.zip"), "inner.zip"], stage)
    # 注意: Windows 文件名不能含半角冒号, 用全角"："(真实下载文件同样如此)
    outer = inputs / "outer[密码：pw1234].zip"
    (inputs / "outer.zip").rename(outer)

    report = pipe.run([inputs])

    assert report.failed == 0
    assert report.needs_password == 0
    assert report.done == 2
    notes = list(cfg.workdir.rglob("note.txt"))
    assert notes and notes[0].read_text(encoding="utf-8") == "hello 开镜"
    assert "pw1234" in vault.candidates_for("")
    assert list(cfg.workdir.rglob("inner.zip")) == []
    assert outer.exists()


def test_needs_password(tmp_path):
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    (inputs / "a.txt").write_text("x", encoding="utf-8")
    _run7z(exe, ["a", "-psecret!", "hush.zip", "a.txt"], inputs)

    report = pipe.run([inputs])

    assert report.done == 0
    assert report.needs_password == 1
    assert report.failed == 0
    assert report.needs_password_tasks[0].status == TaskStatus.NEEDS_PASSWORD


def test_plain_zip(tmp_path):
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    (inputs / "a.txt").write_text("x", encoding="utf-8")
    _run7z(exe, ["a", "p.zip", "a.txt"], inputs)

    report = pipe.run([inputs])

    assert report.done == 1
    assert report.failed == 0
    assert report.needs_password == 0


def test_on_event_stream(tmp_path):
    """on_event 回调应产出 task/status 事件，供 UI 桥接。"""
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    (inputs / "a.txt").write_text("x", encoding="utf-8")
    _run7z(exe, ["a", "e.zip", "a.txt"], inputs)

    events: list[dict] = []
    pipe.run([inputs], on_event=events.append)

    kinds = [e["kind"] for e in events]
    assert "task" in kinds and "status" in kinds
    task_events = [e for e in events if e["kind"] == "task"]
    assert task_events[0]["parent_id"] is None and task_events[0]["depth"] == 0
    statuses = [e["status"] for e in events if e["kind"] == "status"]
    assert statuses[0] == "probing"
    assert statuses[-1] == "done"


def test_ask_password_callback(tmp_path):
    """候选密码全失败时现场询问：先给错密码，再给对的，应成功并入库。"""
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    (inputs / "a.txt").write_text("x", encoding="utf-8")
    _run7z(exe, ["a", "-psecret9", "hush.zip", "a.txt"], inputs)

    asked: list[dict] = []
    replies = iter(["wrong1", "secret9"])

    def ask(info: dict) -> str:
        asked.append(info)
        return next(replies)

    report = pipe.run([inputs], ask_password=ask)

    assert report.done == 1 and report.needs_password == 0 and report.failed == 0
    assert len(asked) == 2 and "hush.zip" in asked[0]["path"]
    assert "secret9" in vault.candidates_for("")


def test_ask_password_give_up(tmp_path):
    """用户取消询问（返回 None）→ 任务进入 needs_password。"""
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    (inputs / "a.txt").write_text("x", encoding="utf-8")
    _run7z(exe, ["a", "-psecret9", "hush.zip", "a.txt"], inputs)

    report = pipe.run([inputs], ask_password=lambda info: None)

    assert report.needs_password == 1 and report.done == 0


def test_should_cancel(tmp_path):
    """取消探测返回 True → 不处理任何任务，并给出取消警告。"""
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    (inputs / "a.txt").write_text("x", encoding="utf-8")
    _run7z(exe, ["a", "c.zip", "a.txt"], inputs)

    report = pipe.run([inputs], should_cancel=lambda: True)

    assert report.done == 0 and report.failed == 0
    assert any("取消" in w for w in report.warnings)


def test_progress_events(tmp_path):
    """真实解压过程中应产出 progress 事件（百分比 0-100）。"""
    import os

    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    # 8MB 随机数据（不可压缩）→ 7z 多帧进度输出
    (inputs / "big.bin").write_bytes(os.urandom(8 * 1024 * 1024))
    _run7z(exe, ["a", "big.zip", "big.bin"], inputs)

    events: list[dict] = []
    report = pipe.run([inputs], on_event=events.append)

    assert report.done == 1
    percents = [e["percent"] for e in events if e["kind"] == "progress"]
    assert percents, "解压过程中没有收到任何 progress 事件"
    assert all(0 <= p <= 100 for p in percents)


def test_sevenzip_cancelled_raises(tmp_path):
    """should_cancel 置位 → 立即杀掉 7z 并抛 SevenZipCancelled。"""
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    (inputs / "a.txt").write_text("x", encoding="utf-8")
    _run7z(exe, ["a", "c2.zip", "a.txt"], inputs)

    from core.sevenzip import SevenZipCancelled

    sz = SevenZip(cfg.sevenzip_path, cfg.list_timeout, cfg.extract_timeout)
    with pytest.raises(SevenZipCancelled):
        sz.extract(inputs / "c2.zip", tmp_path / "out", "",
                   should_cancel=lambda: True)


def test_disguised_inner_zip_expanded(tmp_path):
    """内层压缩包伪装成 .dat：扩展名不命中，但读文件头 PK 后应被展开。"""
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "note.txt").write_text("inner payload", encoding="utf-8")
    _run7z(exe, ["a", "inner.zip", "note.txt"], stage)
    (stage / "inner.zip").rename(stage / "image.dat")  # 伪装扩展名
    _run7z(exe, ["a", str(inputs / "outer.zip"), "image.dat"], stage)

    report = pipe.run([inputs])

    assert report.done == 2, "伪装成 .dat 的内层 zip 未被识别展开"
    notes = list(cfg.workdir.rglob("note.txt"))
    assert notes and notes[0].read_text(encoding="utf-8") == "inner payload"


def test_compound_docx_inside_zip_not_expanded(tmp_path):
    """内层是 .docx（本质 zip）：即使 PK 头命中也不应展开。"""
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "doc.txt").write_text("document", encoding="utf-8")
    _run7z(exe, ["a", "fake.docx", "doc.txt"], stage)  # 7z 产出的就是 PK 头 zip
    _run7z(exe, ["a", str(inputs / "outer2.zip"), "fake.docx"], stage)

    report = pipe.run([inputs])

    assert report.done == 1, ".docx 复合文档不应被当作压缩包展开"
    assert list(cfg.workdir.rglob("fake.docx")), "docx 应保持原样"


def test_source_password_found_when_vault_saturated(tmp_path):
    """P0-2 回归（端到端）：库里堆满不匹配密码时，仍应能用来源域名解出。

    真实场景：用户密码库积累了几十条历史密码，新下载的包密码恰好是站点域名。
    旧实现下库候选占满 max_password_attempts，来源派生永远试不到。
    """
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    for i in range(20):
        vault.add_manual(f"stale{i:02d}")  # 占满默认 20 个候选配额

    (inputs / "a.txt").write_text("x", encoding="utf-8")
    _run7z(exe, ["a", "-pwnacg.com", "pack.zip", "a.txt"], inputs)
    # 先建包再改名：7z 的通配符会把方括号文件名当模式解析
    (inputs / "pack.zip").rename(inputs / "pack[wnacg.com].zip")

    report = pipe.run([inputs])

    assert report.failed == 0
    assert report.done == 1, "来源派生密码被库候选挤掉了，包未能解出"
    assert report.needs_password == 0


def test_encrypted_bomb_rejected(tmp_path):
    """P0-1 回归：头部加密的包（-mhe=on）在空密码下探不到解压后大小。

    旧实现下 probe 只能返回 needs_password_for_listing=True、total_uncompressed=0，
    于是 check_bomb 的两个条件全都不触发——bomb 防线对加密包形同虚设。
    """
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    cfg.max_total_uncompressed = 1 * 1024 * 1024  # 压到 1MB 上限，便于触发

    (inputs / "zeros.bin").write_bytes(b"\0" * (5 * 1024 * 1024))
    _run7z(exe, ["a", "-t7z", "-mhe=on", "-psecret", "bomb.7z", "zeros.bin"], inputs)
    vault.add_manual("secret")

    report = pipe.run([inputs])

    assert report.done == 0, "加密 bomb 包被解压成功了——安全检查被绕过"
    assert report.failed == 1
    assert not list(cfg.workdir.rglob("zeros.bin")), "被拒后仍残留了解压产物"
