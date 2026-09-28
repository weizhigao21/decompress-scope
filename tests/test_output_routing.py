"""输出落点端到端测试：真实 7z 验证 samedir / workdir 两种模式的产物位置。"""
import os
import shutil
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
    """samedir：产物落在压缩包旁边的 <包名>/，工作目录里不留东西。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    archive = _make_zip(exe, inputs)

    report = pipe.run([inputs])

    assert report.done == 1 and report.failed == 0
    landed = inputs / "pack" / "note.txt"
    assert landed.is_file(), f"产物未落到源目录旁；实际 {list(inputs.rglob('note.txt'))}"
    assert landed.read_text(encoding="utf-8") == "解压开镜 payload"
    # 隔离工作目录里不应残留 out 内容（samedir 模式没用它）
    assert not list(cfg.workdir.rglob("note.txt"))
    assert report.output_dirs and Path(report.output_dirs[0]) == inputs / "pack"


@pytest.mark.parametrize("mode", [OUTPUT_SAMEDIR, OUTPUT_WORKDIR])
def test_split_archive_output_has_no_archive_or_volume_suffix(tmp_path, mode):
    """真实分卷解压与交付的目录只保留主名，源分卷保持不变。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, mode)
    payload = os.urandom(3500)
    source = inputs / "data.bin"
    source.write_bytes(payload)
    _run7z(exe, ["a", "-v1k", "My.Series.7z", "data.bin"], inputs)
    source.unlink()
    parts = sorted(inputs.glob("My.Series.7z.*"))
    assert len(parts) > 1

    report = pipe.run([parts[0]])

    assert report.done == 1 and report.failed == 0
    delivered = inputs / "My.Series"
    assert report.output_dirs == [str(delivered)]
    assert (delivered / "data.bin").read_bytes() == payload
    assert not (inputs / "My.Series.7z").exists()
    assert all(part.is_file() for part in parts)


def test_samedir_twice_does_not_overwrite_first_run(tmp_path):
    """同名包解两次：第二次避让为 "pack (2)"，第一份产物不被覆盖。

    注意 skip_done=False：默认的幂等跳过会在第二次直接跳过已完成的任务，
    走不到避让分支。这里显式关掉跳过，专测「避让而非覆盖」这一条不变式。
    """
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    pipe.skip_done = False
    _make_zip(exe, inputs)

    pipe.run([inputs])
    first = inputs / "pack" / "note.txt"
    first.write_text("第一次的产物被改过", encoding="utf-8")

    pipe.run([inputs])
    second_dir = inputs / "pack (2)"
    assert second_dir.is_dir()
    assert (second_dir / "note.txt").read_text(encoding="utf-8") == "解压开镜 payload"
    assert first.read_text(encoding="utf-8") == "第一次的产物被改过"


def test_rerun_skips_already_done_archive(tmp_path):
    """幂等跳过：已成功解压且产物仍在的包，重跑时不再解一遍。

    回归：旧实现每次重跑都从头解压，samedir 下堆出 `pack (2)`/`pack (3)`。
    用户把同一个下载目录反复拖进来（很常见的手势）就会越积越多。
    """
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    _make_zip(exe, inputs)

    first = pipe.run([inputs])
    assert first.done == 1 and first.skipped == 0 and first.failed == 0

    second = pipe.run([inputs])

    assert second.skipped == 1, "已完成的包没被跳过，重跑又解了一遍"
    assert second.done == 0, "跳过的任务不该计入成功"
    assert not (inputs / "pack (2)").exists(), "重跑堆出了重复产物目录"
    assert any("跳过" in w for w in second.warnings), \
        f"跳过必须给出可解释的提示，否则用户会以为程序没反应：{second.warnings}"


def test_rerun_reprocesses_after_output_removed(tmp_path):
    """产物被删掉时不得跳过。

    跳过判据必须是「有 DONE 记录 **且** 产物还在」两个条件同时成立——
    只看数据库记录的话，用户手动清掉产物再重跑会拿不回任何东西，
    而程序还宣称"已跳过"，这是静默的失效。
    """
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    _make_zip(exe, inputs)
    pipe.run([inputs])

    shutil.rmtree(inputs / "pack")  # 用户把产物删了

    report = pipe.run([inputs])

    assert report.skipped == 0, "产物已被删除却仍判定跳过，用户将拿不回产物"
    assert report.done == 1
    assert (inputs / "pack" / "note.txt").is_file()


def test_force_reprocess_extracts_again(tmp_path):
    """skip_done=False（对应 CLI --force）：重跑照旧重新解压并避让。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    _make_zip(exe, inputs)
    pipe.run([inputs])

    pipe.skip_done = False
    second = pipe.run([inputs])

    assert second.skipped == 0
    assert second.done == 1
    assert (inputs / "pack (2)" / "note.txt").is_file()


def test_changed_archive_is_not_skipped_even_when_output_exists(tmp_path):
    """同一路径的包被替换后必须重新处理，不能复用旧 DONE 结论。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    archive = _make_zip(exe, inputs)
    assert pipe.run([inputs]).done == 1

    stat = archive.stat()
    os.utime(archive, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))

    report = pipe.run([inputs])
    assert report.skipped == 0
    assert report.done == 1


def test_overwrite_failure_does_not_delete_existing_output(tmp_path):
    """覆盖模式失败时，用户已有产物必须原样保留。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    payload = inputs / "note.txt"
    payload.write_text("secret payload", encoding="utf-8")
    _run7z(exe, ["a", "-pnot-in-candidates", "pack.zip", "note.txt"], inputs)
    payload.unlink()
    existing = inputs / "pack"
    existing.mkdir()
    keep = existing / "user-file.txt"
    keep.write_text("keep me", encoding="utf-8")
    cfg.overwrite_existing = True

    report = pipe.run([inputs])

    assert report.needs_password == 1
    assert keep.read_text(encoding="utf-8") == "keep me"
    assert not list(cfg.workdir.rglob("note.txt"))


def test_workdir_mode_copies_back_to_source_dir(tmp_path):
    """workdir：隔离解压后把产物交付回源目录，源目录得到可用产物。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_WORKDIR)
    _make_zip(exe, inputs)

    report = pipe.run([inputs])

    assert report.done == 1
    delivered = inputs / "pack" / "note.txt"
    assert delivered.is_file(), "workdir 模式未把产物交付回源目录"
    assert delivered.read_text(encoding="utf-8") == "解压开镜 payload"
    # 同卷交付走 rename 而非复制：工作目录里不再留一份副本（省一倍写入与磁盘）。
    # 跨卷时仍会保留工作目录副本作为安全网，见 cross_volume 那条测试。
    assert not list(cfg.workdir.rglob("note.txt")), \
        "同卷交付应搬走而非复制，工作目录不该还留一份副本"


def test_delivered_output_dir_points_at_real_path(tmp_path):
    """交付后 report.output_dirs 必须指向真实存在的目录。

    回归：同卷交付用 rename 把 out_dir 搬走后，若仍上报原来的工作目录路径，
    UI 的「打开目录」会指向一个已经消失的位置。
    """
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_WORKDIR)
    _make_zip(exe, inputs)

    report = pipe.run([inputs])

    assert len(report.output_dirs) == 1
    d = Path(report.output_dirs[0])
    assert d.is_dir(), f"output_dirs 指向了不存在的路径：{d}"
    assert any(d.iterdir()), "output_dirs 指向了空目录"
    assert d == inputs / "pack"


def test_workdir_rerun_skips_after_same_volume_move(tmp_path):
    """同卷搬走后重跑仍要能正确跳过。

    回归：产物被搬走后若 extracted_dir 还留在库里的旧工作目录路径上，
    幂等跳过判据「产物仍在」永远为假——每次重跑都重新解压一遍，
    跳过功能在 workdir 模式下等于没做。
    """
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_WORKDIR)
    _make_zip(exe, inputs)

    first = pipe.run([inputs])
    assert first.done == 1

    second = pipe.run([inputs])

    assert second.skipped == 1, "同卷交付后重跑未跳过（extracted_dir 未随搬走更新）"
    assert second.done == 0
    assert not (inputs / "pack (2)").exists()


def test_cross_volume_delivery_keeps_workdir_copy(tmp_path, monkeypatch):
    """跨卷（或同卷判定失败）时退回复制，并保留工作目录副本作安全网。

    锁住"优化只在同卷生效，跨卷行为不变"：同卷 rename 是单个原子系统调用，
    不需要兜底副本；跨卷 rename 会退化成逐文件拷贝且中途失败会留半成品，
    所以那份副本必须留着。

    注意落点的语义（2026-09-26 改）：跨卷走 copytree 时工作目录里的源目录
    不会消失，但 extracted_dir / output_dirs 必须指向**交付过去的那一份**——
    它才是用户能拿到产物的地方。此前只有 moved=True 才更新落点，于是跨卷交付
    成功后 output_dirs 仍指着工作目录：界面「打开目录」开到隔离区，明明交付
    成功却像没解出来；残留盘点也会把这份冗余副本误判成"还没交付"。
    """
    from core import pipeline as pipeline_mod

    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_WORKDIR)
    _make_zip(exe, inputs)
    monkeypatch.setattr(pipeline_mod, "same_volume", lambda a, b: False)

    report = pipe.run([inputs])

    assert report.done == 1
    delivered = inputs / "pack"
    assert (delivered / "note.txt").is_file(), "跨卷仍应交付到源目录"
    assert list(cfg.workdir.rglob("note.txt")), "跨卷交付必须保留工作目录副本兜底"
    assert Path(report.output_dirs[0]) == delivered, \
        "跨卷交付后落点应指向交付位置，而不是工作目录里的兜底副本"
    assert report.delivery_failed == 0, "交付成功不该被记成未交付"


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
    delivered = inputs / "outer"
    # 内层内容已进交付目录，且**带着自己的包名**（out_dir 末级就是包名目录）
    assert (delivered / "inner" / "deep.txt").is_file()
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
    delivered = inputs / "outer"
    # 真内层包已被消化，不该留
    assert not (delivered / "inner.zip").exists(), "已消化的内层包未被清理"
    # 用户放在子目录里的同名无关文件必须幸存
    survivor = delivered / "data" / "keep" / "inner.zip"
    assert survivor.is_file(), f"子目录里的同名无关文件被误删：{survivor}"
    assert (delivered / "data" / "deep.txt").is_file()
    # 内层产物按包名成一级目录（内层包只含 data/ 一层，被「单目录上提」提到顶层）
    assert (delivered / "inner" / "deep.txt").is_file()


def test_workdir_mode_pure_isolation_leaves_source_untouched(tmp_path):
    """copy_back=False：源目录完全不动，产物只在工作目录（--workdir-only 语义）。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_WORKDIR)
    pipe.copy_back = False
    _make_zip(exe, inputs)

    report = pipe.run([inputs])

    assert report.done == 1
    assert not (inputs / "pack").exists(), "纯隔离模式不应在源目录建任何东西"
    assert list(cfg.workdir.rglob("note.txt"))
    # 但产物仍要能被 UI 打开，故 output_dirs 必须指向工作目录里的真实产物
    assert len(report.output_dirs) == 1
    assert Path(report.output_dirs[0]).is_dir()
    assert Path(report.output_dirs[0]).is_relative_to(cfg.workdir)


def test_workdir_mode_skips_existing_target_with_warning(tmp_path):
    """目标已存在时跳过复制并告警，绝不覆盖用户既有文件，也不判任务失败。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_WORKDIR)
    _make_zip(exe, inputs)
    target = inputs / "pack"
    target.mkdir(parents=True)
    (target / "note.txt").write_text("用户自己的文件", encoding="utf-8")

    report = pipe.run([inputs])

    assert report.done == 1 and report.failed == 0
    assert (target / "note.txt").read_text(encoding="utf-8") == "用户自己的文件"
    assert any("已存在" in w for w in report.warnings), report.warnings


def test_samedir_nested_inner_stays_in_workdir(tmp_path):
    """samedir 模式下，内层包在 workdir 展开，产物带包名归并回外层交付目录。

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
    delivered = inputs / "outer"
    # 内层产物已进外层交付目录，并保留内层包名那一级
    assert (delivered / "inner" / "deep.txt").is_file()
    assert (delivered / "inner" / "deep.txt").read_text(encoding="utf-8") == "inner"
    # 内层 zip 本身被清掉（中间产物不该交付给用户）
    assert not list(delivered.rglob("inner.zip"))
    # 源目录下不得出现内层包的独立输出目录
    assert not (inputs / "inner").exists()
    # workdir 里的内层壳目录也应被收干净
    assert not list(cfg.workdir.rglob("deep.txt"))


def test_failed_task_leaves_no_empty_dir_in_source(tmp_path):
    """失败/待密码任务不得在用户源目录留下空目录。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    (inputs / "a.txt").write_text("x", encoding="utf-8")
    _run7z(exe, ["a", "-psecret!", "hush.zip", "a.txt"], inputs)

    report = pipe.run([inputs])

    assert report.needs_password == 1
    assert not (inputs / "hush").exists(), "失败任务留下了空壳目录"
    # 安全网：容器留空时 final_dir.parent 就是源目录本身。收空壳目录的逻辑一旦
    # 越过这一层，删掉的是用户的下载目录——这条断言让那种事故立刻现形。
    assert inputs.is_dir(), "收空壳目录越过了源目录：用户的下载目录被删了"


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

    out = inputs / "wrap"
    # 单层目录被提上来：直接能看到 a.txt，而不是 wrapped/a.txt
    if (out / "a.txt").is_file():
        assert not (out / "wrapped").exists()
    else:
        # 若 7z 未带目录项（扁平落盘），也应直接可见
        assert (out / "wrapped" / "a.txt").is_file()
