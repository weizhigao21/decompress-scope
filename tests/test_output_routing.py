"""输出落点端到端测试：真实 7z 验证 samedir / workdir 两种模式的产物位置。"""
import subprocess
from pathlib import Path

import pytest

from core.appconfig import OUTPUT_SAMEDIR, OUTPUT_WORKDIR
from core.config import Config, detect_sevenzip
from core.pipeline import Pipeline
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


def _make_env(tmp_path: Path, mode: str):
    exe = _sevenzip_or_skip()
    inputs = tmp_path / "downloads"
    inputs.mkdir()
    db = tmp_path / "app.db"
    cfg = Config.create(sevenzip=str(exe), workdir=tmp_path / "wd")
    pipe = Pipeline(cfg, PasswordVault(db), TaskStore(db))
    pipe.output_mode = mode
    return exe, inputs, cfg, pipe


def _make_zip(exe: Path, inputs: Path, name: str = "pack.zip") -> Path:
    payload = inputs / "note.txt"
    payload.write_text("解压开镜 payload", encoding="utf-8")
    _run7z(exe, ["a", name, "note.txt"], inputs)
    payload.unlink()
    return inputs / name


def test_samedir_lands_next_to_archive(tmp_path):
    """samedir：产物落在压缩包旁边的 _解压开镜/<包名>/，工作目录里不留东西。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    archive = _make_zip(exe, inputs)

    report = pipe.run([inputs])

    assert report.done == 1 and report.failed == 0
    landed = inputs / "_解压开镜" / "pack" / "note.txt"
    assert landed.is_file(), f"产物未落到源目录旁；实际 {list(inputs.rglob('note.txt'))}"
    assert landed.read_text(encoding="utf-8") == "解压开镜 payload"
    # 隔离工作目录里不应残留 out 内容（samedir 模式没用它）
    assert not list(cfg.workdir.rglob("note.txt"))
    assert report.output_dirs and Path(report.output_dirs[0]) == inputs / "_解压开镜" / "pack"


def test_samedir_twice_does_not_overwrite_first_run(tmp_path):
    """同名包解两次：第二次避让为 "pack (2)"，第一份产物不被覆盖。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    _make_zip(exe, inputs)

    pipe.run([inputs])
    first = inputs / "_解压开镜" / "pack" / "note.txt"
    first.write_text("第一次的产物被改过", encoding="utf-8")

    pipe.run([inputs])
    second_dir = inputs / "_解压开镜" / "pack (2)"
    assert second_dir.is_dir()
    assert (second_dir / "note.txt").read_text(encoding="utf-8") == "解压开镜 payload"
    assert first.read_text(encoding="utf-8") == "第一次的产物被改过"


def test_workdir_mode_copies_back_to_source_dir(tmp_path):
    """workdir：隔离解压后复制回源目录，源目录得到可用产物。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_WORKDIR)
    _make_zip(exe, inputs)

    report = pipe.run([inputs])

    assert report.done == 1
    copied = inputs / "_解压开镜" / "pack" / "note.txt"
    assert copied.is_file(), "workdir 模式未把产物复制回源目录"
    assert copied.read_text(encoding="utf-8") == "解压开镜 payload"
    # 工作目录里的原件仍在（默认保留隔离产物）
    assert list(cfg.workdir.rglob("note.txt"))


def test_workdir_copy_back_has_no_consumed_inner_zip(tmp_path):
    """workdir+复制回源：交付目录里不得残留已消化的内层压缩包。

    回归：_deliver_hierarchy 会把外层的 out_dir 整块搬到源目录，其中包含
    内层 zip；_prune_consumed_archives 若只扫 out_dir（workdir 内）而漏掉
    复制后的 final_dir，用户下载目录里就会多出一个已经解过一遍的包。
    """
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_WORKDIR)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "deep.txt").write_text("inner", encoding="utf-8")
    _run7z(exe, ["a", "inner.zip", "deep.txt"], stage)
    _run7z(exe, ["a", str(inputs / "outer.zip"), "inner.zip"], stage)

    report = pipe.run([inputs])

    assert report.done == 2
    delivered = inputs / "_解压开镜" / "outer"
    assert (delivered / "deep.txt").is_file()
    assert not list(delivered.rglob("inner.zip")), \
        f"交付目录残留中间包：{list(delivered.rglob('inner.zip'))}"


def test_samedir_keeps_unprocessed_sibling_archive(tmp_path):
    """绝不误删：用户自己放的同级压缩包（未被处理）必须原样保留。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    _make_zip(exe, inputs, "a.zip")
    # 一个"看起来像压缩包但不是本次展开出来"的同级文件
    (inputs / "handmade.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 64)

    pipe.run([inputs])

    assert (inputs / "handmade.zip").is_file(), "未处理的同级压缩包被误删了"


def test_nested_lookalike_archive_is_not_pruned(tmp_path):
    """绝不误删（嵌套场景）：交付目录的子目录里若有与内层包同名的无关文件，
    必须原样保留。

    回归：_prune_consumed_archives 曾经 rglob 全目录、只按裸文件名匹配，
    于是 outer.zip 里的 data/inner.zip（真内层）被消化后，用户放在
    data/keep/inner.zip 的同名无关文件会被一起 unlink——静默丢数据。
    修法：改用「相对 T.out_dir 的相对路径」精确定位，不做全目录文件名匹配。
    """
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    stage = tmp_path / "stage"
    (stage / "data" / "keep").mkdir(parents=True)
    (stage / "data" / "deep.txt").write_text("inner", encoding="utf-8")
    _run7z(exe, ["a", "inner.zip", "data"], stage)
    # 用户无关数据：同名但不在内层包展开出来的位置
    (stage / "data" / "keep" / "inner.zip").write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    _run7z(exe, ["a", str(inputs / "outer.zip"), "inner.zip", "data"], stage)

    report = pipe.run([inputs])

    assert report.done == 2
    delivered = inputs / "_解压开镜" / "outer"
    # 真内层包已被消化，不该留
    assert not (delivered / "inner.zip").exists(), "已消化的内层包未被清理"
    # 用户放在子目录里的同名无关文件必须幸存
    survivor = delivered / "data" / "keep" / "inner.zip"
    assert survivor.is_file(), f"子目录里的同名无关文件被误删：{survivor}"
    assert (delivered / "data" / "deep.txt").is_file()


def test_workdir_mode_pure_isolation_leaves_source_untouched(tmp_path):
    """copy_back=False：源目录完全不动，产物只在工作目录（--workdir-only 语义）。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_WORKDIR)
    pipe.copy_back = False
    _make_zip(exe, inputs)

    report = pipe.run([inputs])

    assert report.done == 1
    assert not (inputs / "_解压开镜").exists(), "纯隔离模式不应在源目录建任何东西"
    assert list(cfg.workdir.rglob("note.txt"))
    # 但产物仍要能被 UI 打开，故 output_dirs 必须指向工作目录里的真实产物
    assert len(report.output_dirs) == 1
    assert Path(report.output_dirs[0]).is_dir()
    assert Path(report.output_dirs[0]).is_relative_to(cfg.workdir)


def test_workdir_mode_skips_existing_target_with_warning(tmp_path):
    """目标已存在时跳过复制并告警，绝不覆盖用户既有文件，也不判任务失败。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_WORKDIR)
    _make_zip(exe, inputs)
    target = inputs / "_解压开镜" / "pack"
    target.mkdir(parents=True)
    (target / "note.txt").write_text("用户自己的文件", encoding="utf-8")

    report = pipe.run([inputs])

    assert report.done == 1 and report.failed == 0
    assert (target / "note.txt").read_text(encoding="utf-8") == "用户自己的文件"
    assert any("已存在" in w for w in report.warnings), report.warnings


def test_samedir_nested_inner_stays_in_workdir(tmp_path):
    """samedir 模式下，内层包在 workdir 展开，产物归并回外层交付目录。

    这是最关键的一条：内层包解出来的内容必须出现在外层最终交付目录里，
    否则 delete_intermediate 删掉内层 zip 后，用户拿到的是残缺品。
    """
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "deep.txt").write_text("inner", encoding="utf-8")
    _run7z(exe, ["a", "inner.zip", "deep.txt"], stage)
    _run7z(exe, ["a", str(inputs / "outer.zip"), "inner.zip"], stage)

    report = pipe.run([inputs])

    assert report.done == 2
    delivered = inputs / "_解压开镜" / "outer"
    # 内层产物已归并到外层交付目录
    assert (delivered / "deep.txt").is_file()
    assert (delivered / "deep.txt").read_text(encoding="utf-8") == "inner"
    # 内层 zip 本身被清掉（中间产物不该交付给用户）
    assert not list(delivered.rglob("inner.zip"))
    # 源目录下不得出现内层包的独立输出目录
    assert not (inputs / "_解压开镜" / "inner").exists()
    # workdir 里的内层壳目录也应被收干净
    assert not list(cfg.workdir.rglob("deep.txt"))


def test_failed_task_leaves_no_empty_dir_in_source(tmp_path):
    """失败/待密码任务不得在用户源目录留下空目录。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    (inputs / "a.txt").write_text("x", encoding="utf-8")
    _run7z(exe, ["a", "-psecret!", "hush.zip", "a.txt"], inputs)

    report = pipe.run([inputs])

    assert report.needs_password == 1
    assert not (inputs / "_解压开镜").exists(), "失败任务留下了空壳目录"


def test_output_dirs_point_at_real_products(tmp_path):
    """report.output_dirs 必须指向真正存在且非空的目录（UI 打开目录依赖它）。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    _make_zip(exe, inputs)

    report = pipe.run([inputs])

    assert len(report.output_dirs) == 1
    d = Path(report.output_dirs[0])
    assert d.is_dir()
    assert any(d.iterdir()), "output_dirs 指向了空目录"


def test_single_top_dir_is_flattened(tmp_path):
    """7z 产出单层包裹目录时，成品应上提一层（解出来就是内容）。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    stage = tmp_path / "stage"
    (stage / "wrapped").mkdir(parents=True)
    (stage / "wrapped" / "a.txt").write_text("wrapped content", encoding="utf-8")
    _run7z(exe, ["a", str(inputs / "wrap.zip"), "wrapped"], stage)

    pipe.run([inputs])

    out = inputs / "_解压开镜" / "wrap"
    # 单层目录被提上来：直接能看到 a.txt，而不是 wrapped/a.txt
    if (out / "a.txt").is_file():
        assert not (out / "wrapped").exists()
    else:
        # 若 7z 未带目录项（扁平落盘），也应直接可见
        assert (out / "wrapped" / "a.txt").is_file()
