"""真实 7z 集成测试：用 7z 现场构造嵌套/加密压缩包，验证端到端流水线。"""
import os
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
    # 断言用户实际拿到的地方：workdir 模式下产物会交付到源目录侧，
    # 同卷时还是 rename 搬走，工作目录里自然查不到（见 P1-4）。
    delivered = Path(report.output_dirs[0])
    notes = list(delivered.rglob("note.txt"))
    assert notes and notes[0].read_text(encoding="utf-8") == "hello 开镜"
    assert "pw1234" in vault.candidates_for("")
    # 中间层 inner.zip 已消化，不该交付给用户
    assert list(delivered.rglob("inner.zip")) == []
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
    delivered = Path(report.output_dirs[0])
    notes = list(delivered.rglob("note.txt"))
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
    delivered = Path(report.output_dirs[0])
    assert list(delivered.rglob("fake.docx")), "docx 应原样交付，而不是被展开成文件树"


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


def test_candidate_budget_follows_setting_and_reaches_other_sources(tmp_path):
    """端到端：候选配额必须真的跟随 max_password_attempts，且跨来源条目要能轮到。

    构造：库里 25 条无来源的历史密码 + 1 条记在**别的站点**名下的正确密码，
    包的密码就是后者。按优先级它排在第 26 位。

    - 配额 20：连「无来源」那一段都装不下，必然解不出（顺带钉住"不会无脑全试"）。
    - 配额 40：正确密码进入候选 → 解出。

    旧实现两条都过不去：`candidates_for(…, limit=20)` 把库配额硬编码成 20，
    而且只查 `source=?` 与 `source=''`，跨来源的正确密码根本不在候选里。
    用户表现为「设置里明明调到 40 了，还是报需要密码」——设置项说谎。
    """
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    for i in range(25):
        vault.add_manual(f"stale{i:02d}")            # 无来源：排在第二档
    vault.add_manual("target_pw", "other-site.com")  # 跨来源：排在第三档

    (inputs / "a.txt").write_text("x", encoding="utf-8")
    _run7z(exe, ["a", "-ptarget_pw", "pack.zip", "a.txt"], inputs)
    # 先建包再改名：7z 的通配符会把方括号文件名当模式解析
    (inputs / "pack.zip").rename(inputs / "pack[wnacg.com].zip")

    cfg.max_password_attempts = 20
    assert pipe.run([inputs]).needs_password == 1, \
        "配额 20 时装不下第 26 位的候选，此处就该解不出"

    cfg.max_password_attempts = 40
    report = pipe.run([inputs])
    assert report.failed == 0
    assert report.done == 1, \
        "配额放开后跨来源的正确密码仍未进入候选——配额跟随设置 + 跨来源检索两者有一条没生效"
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


# ---------- info 事件：把包体信息送到界面 ----------

def _infos(events: list[dict]) -> list[dict]:
    return [e for e in events if e.get("kind") == "info"]


def test_info_event_carries_format_and_size(tmp_path):
    """探测后必须发 info 事件，带上 7z 报出的格式与压缩包实际大小。"""
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    (inputs / "a.txt").write_text("x" * 4096, encoding="utf-8")
    _run7z(exe, ["a", "-tzip", "pack.zip", "a.txt"], inputs)
    size_on_disk = (inputs / "pack.zip").stat().st_size

    events: list[dict] = []
    pipe.run([inputs], on_event=events.append)

    infos = _infos(events)
    assert len(infos) == 1, "一次探测应只发一条 info"
    info = infos[0]
    assert info["format"] == "zip"
    assert info["archive_size"] == size_on_disk
    assert info["uncompressed"] == 4096
    assert info["files"] == 1
    assert info["encrypted"] is False
    assert isinstance(info["task_id"], int)


def test_info_event_emitted_before_bomb_rejection(tmp_path):
    """被判为 bomb 的包也要先报出类型与大小。

    顺序很关键：info 若发在 check_bomb 之后，被拒的包在界面上就是一行空白，
    用户根本不知道是什么文件、多大——正是最需要这个信息的时候。
    """
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    cfg.max_total_uncompressed = 1 * 1024 * 1024

    (inputs / "zeros.bin").write_bytes(b"\0" * (5 * 1024 * 1024))
    _run7z(exe, ["a", "-tzip", "bomb.zip", "zeros.bin"], inputs)

    events: list[dict] = []
    report = pipe.run([inputs], on_event=events.append)

    assert report.failed == 1
    infos = _infos(events)
    assert infos, "被拒的 bomb 包没有收到 info 事件"
    assert infos[0]["format"] == "zip"
    assert infos[0]["archive_size"] > 0


def test_info_event_for_header_encrypted_uses_magic_fallback(tmp_path):
    """头部加密的包：7z 列不出目录，`Type` 是空的，须靠 magic 兜底给出格式名。"""
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    (inputs / "a.txt").write_text("x" * 4096, encoding="utf-8")
    _run7z(exe, ["a", "-t7z", "-mhe=on", "-psecret", "hush.7z", "a.txt"], inputs)

    events: list[dict] = []
    report = pipe.run([inputs], on_event=events.append)

    assert report.needs_password == 1
    infos = _infos(events)
    assert len(infos) == 1
    assert infos[0]["format"] == "7z", "加密包头包未给出格式名（magic 兜底失效）"
    assert infos[0]["encrypted"] is True
    assert infos[0]["archive_size"] > 0


def test_info_event_updated_after_password_reprobe(tmp_path):
    """密码确定后复探 → 再发一条 info，补上真实的解压后大小与文件数。"""
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    (inputs / "a.txt").write_text("x" * 10000, encoding="utf-8")
    _run7z(exe, ["a", "-t7z", "-mhe=on", "-psecret", "hush.7z", "a.txt"], inputs)
    vault.add_manual("secret")

    events: list[dict] = []
    report = pipe.run([inputs], on_event=events.append)

    assert report.done == 1
    infos = _infos(events)
    assert len(infos) == 2, "复探后应补发一条 info，供界面更新解压后大小"
    assert infos[0]["uncompressed"] == 0, "空密码下本就探不到解压后大小"
    assert infos[-1]["uncompressed"] == 10000
    assert infos[-1]["files"] == 1


# ---------- 分卷包：大小按整组算 ----------

def _split_env(tmp_path, volume: str = "1m"):
    """造一个真分卷包，返回 (pipe, inputs, 全部按序分卷)。"""
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    (inputs / "big.bin").write_bytes(os.urandom(3 * 1024 * 1024))  # 不可压缩才会切开
    _run7z(exe, ["a", "-tzip", f"-v{volume}", "pack.zip", "big.bin"], inputs)
    vols = sorted(p for p in inputs.iterdir() if p.name.startswith("pack.zip."))
    assert len(vols) >= 2, f"没切成多卷（拿到 {[v.name for v in vols]}），用例失去意义"
    return pipe, inputs, vols


def test_split_volume_size_counts_whole_group(tmp_path):
    """分卷包报的大小必须是**整组**之和。

    任务里存的 archive_path 只是首卷（`pack.zip.001`），旧实现直接
    `stat().st_size` 拿首卷大小——界面上一个 3 MB 的分卷包显示成 1 MB，
    用户看到的数字"看着挺合理"，却跟实际占用差几倍。
    """
    pipe, inputs, vols = _split_env(tmp_path)
    total = sum(v.stat().st_size for v in vols)
    first = vols[0].stat().st_size
    assert total > first, "前置条件：整组必须明显大于首卷"

    events: list[dict] = []
    pipe.run([inputs], on_event=events.append)

    info = _infos(events)[0]
    assert info["volumes"] == len(vols)
    assert info["archive_size"] == total, \
        f"只报了首卷大小（{info['archive_size']} vs 整组 {total}）"
    assert info["archive_size"] != first


def test_split_volume_reports_inner_format_not_split(tmp_path, monkeypatch):
    """分卷包的类型必须是**里面**的格式，不是 "Split" 这个容器名。

    7z 对分卷首卷会报两行 Type：外层 `Split` 容器在前，内层真实的 `zip` 在后。
    只取第一个匹配，界面「类型」列就显示成 "Split"——用户问"这是什么包"，
    得到的回答是"这是分卷"，等于没说。

    **这里必须掐掉 magic 兜底**（`sniff_format` 一律返回空）。
    不掐的话这条测试就是摆设：兜底单靠自己也能读首卷文件头认出 zip，
    于是"7z 报告解析"整条主路径失效也测不出来——两条路互相掩盖，
    正是最典型的假绿。掐掉之后绿才算数，证明真格式确实从 7z 的报告里拿到了。

    用户最终看到什么的保障，由 UI 端到端那条（不掐兜底）负责，两层各司其职。
    """
    from core import probe as probe_mod

    monkeypatch.setattr(probe_mod, "sniff_format", lambda _p: "")

    pipe, inputs, vols = _split_env(tmp_path)

    events: list[dict] = []
    pipe.run([inputs], on_event=events.append)

    info = _infos(events)[0]
    assert info["format"] == "zip", f"类型列会显示 {info['format']!r}"
    assert info["format"] != "Split"


def test_split_volume_ratio_uses_group_total_not_first_volume(tmp_path):
    """压缩比的分母也用整组——否则正常的分卷包会被误判成 zip bomb。

    实测：3 MB 内容压成 4 卷共 124 KB，真比值 25:1；只算首卷（32 KB）则报成
    96:1。默认阈值 1000:1 下这只是"不准"，但阈值一旦调紧（或分卷数更多），
    正常包就会被安全机制**拒解**——所以这条必须钉住。

    阈值刻意卡在真比值与首卷比值之间：只算首卷必红，算整组才绿。
    """
    exe, inputs, cfg, vault, pipe = _make_env(tmp_path)
    # 头部 120 KB 不可压缩 + 其余全零：压缩后约 120 KB，解压后 3 MB
    payload = os.urandom(120 * 1024) + b"\0" * (3 * 1024 * 1024 - 120 * 1024)
    (inputs / "big.bin").write_bytes(payload)
    _run7z(exe, ["a", "-t7z", "-v32k", "pack.7z", "big.bin"], inputs)
    vols = sorted(p for p in inputs.iterdir() if p.name.startswith("pack.7z."))
    assert len(vols) >= 3, f"没切成多卷（{len(vols)} 卷），用例失去意义"

    total = sum(v.stat().st_size for v in vols)
    first = vols[0].stat().st_size
    ratio_whole = len(payload) / total
    ratio_first = len(payload) / first
    assert ratio_first > ratio_whole * 2, \
        f"首卷与整组的比值差别不够大（{ratio_first:.1f} vs {ratio_whole:.1f}），分辨不出对错"

    cfg.max_total_uncompressed = 10 * 1024 ** 3
    cfg.ratio_floor_bytes = 0                     # 隔离出"比值"这一条判据
    cfg.max_compression_ratio = (ratio_whole + ratio_first) / 2

    events: list[dict] = []
    report = pipe.run([inputs], on_event=events.append)

    assert report.failed == 0, (
        f"正常的分卷包被判成 zip bomb："
        f"{_infos(events)[0]['archive_size']} 字节的分母算成了首卷")
    assert report.done == 1
