"""输出落点决策测试：samedir / workdir 两种模式 + 同名避让 + 嵌套任务规则。"""
from pathlib import Path

from core.appconfig import OUTPUT_SAMEDIR, OUTPUT_WORKDIR
from core.config import Config
from core.models import Task, TaskStatus
from core.output_plan import plan_output, same_volume, unique_path


def _cfg(tmp_path: Path, **overrides) -> Config:
    cfg = Config(sevenzip_path=Path("7z.exe"), workdir=tmp_path / "wd")
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _task(tmp_path: Path, name: str = "pack.zip", depth: int = 0, tid: int = 1) -> Task:
    t = Task(archive_path=str(tmp_path / "src" / name), depth=depth)
    t.id = tid
    return t


def test_samedir_extracts_next_to_archive(tmp_path):
    """samedir：解到 <源目录>/<_解压开镜>/<包名>/。"""
    cfg = _cfg(tmp_path)
    t = _task(tmp_path, "comic.cbz")
    plan = plan_output(t, cfg, OUTPUT_SAMEDIR)

    expect = tmp_path / "src" / "_解压开镜" / "comic"
    assert plan.out_dir == expect
    assert plan.out_dir == plan.final_dir
    assert plan.copy_back is False


def test_samedir_avoids_overwriting_existing(tmp_path):
    """同名目录已存在 → 自动避让为 "包名 (2)"，绝不覆盖既有产物。"""
    cfg = _cfg(tmp_path)
    base = tmp_path / "src" / "_解压开镜" / "comic"
    base.mkdir(parents=True)

    plan = plan_output(_task(tmp_path, "comic.cbz"), cfg, OUTPUT_SAMEDIR)
    assert plan.out_dir.name == "comic (2)"
    assert plan.out_dir.parent == base.parent

    # 再解一次 → (3)
    (base.parent / "comic (2)").mkdir()
    plan2 = plan_output(_task(tmp_path, "comic.cbz"), cfg, OUTPUT_SAMEDIR)
    assert plan2.out_dir.name == "comic (3)"


def test_samedir_overwrite_when_enabled(tmp_path):
    """显式开启 overwrite_existing 时直接复用同名目录。"""
    cfg = _cfg(tmp_path, overwrite_existing=True)
    base = tmp_path / "src" / "_解压开镜" / "comic"
    base.mkdir(parents=True)
    plan = plan_output(_task(tmp_path, "comic.cbz"), cfg, OUTPUT_SAMEDIR)
    assert plan.out_dir == base


def test_samedir_force_new_ignores_overwrite_flag(tmp_path):
    """force_new 优先于全局开关（给"再解一份"用）。"""
    cfg = _cfg(tmp_path, overwrite_existing=True)
    base = tmp_path / "src" / "_解压开镜" / "comic"
    base.mkdir(parents=True)
    plan = plan_output(_task(tmp_path, "comic.cbz"), cfg, OUTPUT_SAMEDIR, force_new=True)
    assert plan.out_dir.name == "comic (2)"


def test_nested_task_always_stays_in_workdir(tmp_path):
    """嵌套（内层）压缩包是中间产物，任何模式都必须留在 workdir 内。"""
    cfg = _cfg(tmp_path)
    inner = _task(tmp_path, "inner.zip", depth=1, tid=9)
    for mode in (OUTPUT_SAMEDIR, OUTPUT_WORKDIR):
        plan = plan_output(inner, cfg, mode)
        assert plan.out_dir == cfg.workdir / "task_9" / "out"
        assert plan.final_dir is None
        assert plan.copy_back is False


def test_workdir_mode_copies_back_to_source_dir(tmp_path):
    """workdir 模式：隔离解压，但成功后要复制回源压缩包目录。"""
    cfg = _cfg(tmp_path)
    plan = plan_output(_task(tmp_path, "pack.zip"), cfg, OUTPUT_WORKDIR)
    assert plan.out_dir == cfg.workdir / "task_1" / "out"
    assert plan.final_dir == tmp_path / "src" / "_解压开镜" / "pack"
    assert plan.copy_back is True


def test_workdir_mode_subdir_name_custom(tmp_path):
    """自定义子目录名生效。"""
    cfg = _cfg(tmp_path)
    plan = plan_output(_task(tmp_path), cfg, OUTPUT_WORKDIR, subdir_name="unzipped")
    assert plan.final_dir == tmp_path / "src" / "unzipped" / "pack"


def test_subdir_name_with_separators_is_sanitized(tmp_path):
    """子目录名含分隔符/上跳点必须被压平成单层，防止路径逃逸。"""
    cfg = _cfg(tmp_path)
    plan = plan_output(_task(tmp_path), cfg, OUTPUT_SAMEDIR, subdir_name="../../evil")
    # 分隔符成下划线、".." 被收掉、结果停留在源目录之内
    assert plan.out_dir.parent.parent == tmp_path / "src"
    assert plan.out_dir.parent.name == "____evil"

    # 绝对路径同样只能得到最后一层的扁平名字（盘符冒号也被中和，见下条测试）
    plan2 = plan_output(_task(tmp_path), cfg, OUTPUT_SAMEDIR, subdir_name="C:\\Windows\\sys")
    assert plan2.out_dir.parent == tmp_path / "src" / "C__Windows_sys"


def test_subdir_name_with_drive_letter_stays_inside_source(tmp_path):
    """Windows 盘符是最隐蔽的逃逸口：Path("Z:foo") 会被 pathlib 认成盘符相对路径
    （anchor="Z:"），拼进源目录后产物会落到源目录之外——甚至跳到别的盘。

    实测旧实现（只 replace 分隔符、不中和冒号）：subdir="Z:/payload" 会让
    plan.out_dir.parent 变成 `Z:_payload`，产物被丢到 Z 盘根下。
    注意不能只断言父目录的 name（pathlib 会把 "Z:" 吃进 drive，name 只剩 "_payload"），
    必须直接比对完整父路径字符串，这才是与当前盘符无关的正确判据。
    """
    cfg = _cfg(tmp_path)
    src = tmp_path / "src"
    cases = {
        "C:/Windows/sys": "C__Windows_sys",
        "C:\\evil": "C__evil",
        "D:payload": "D_payload",
        "Z:/payload": "Z__payload",
        "D:evil": "D_evil",
    }
    for dangerous, expected in cases.items():
        plan = plan_output(_task(tmp_path), cfg, OUTPUT_SAMEDIR, subdir_name=dangerous)
        assert plan.out_dir.parent == src / expected, (
            f"{dangerous!r} 逃出了源目录：{plan.out_dir.parent}（期望 {src / expected}）"
        )
        assert plan.out_dir.resolve().is_relative_to(src.resolve())


def test_archive_without_suffix_uses_stem(tmp_path):
    """无扩展名的包：stem 就是原名，不会产生空目录名。"""
    cfg = _cfg(tmp_path)
    t = _task(tmp_path, "noext")
    plan = plan_output(t, cfg, OUTPUT_SAMEDIR)
    assert plan.out_dir.name == "noext"


def test_plan_is_pure_no_side_effects(tmp_path):
    """决策函数不得创建任何目录（只有状态判定与路径拼装）。"""
    cfg = _cfg(tmp_path)
    plan_output(_task(tmp_path), cfg, OUTPUT_SAMEDIR)
    plan_output(_task(tmp_path), cfg, OUTPUT_WORKDIR)
    assert not (tmp_path / "src").exists()
    assert not cfg.workdir.exists()


def test_workdir_mode_without_copy_back_is_pure_isolation(tmp_path):
    """copy_back=False：纯隔离，源目录完全不被动过。

    这是 --workdir-only 的语义——用户明确要求「别碰我的下载目录」。
    """
    cfg = _cfg(tmp_path)
    plan = plan_output(_task(tmp_path), cfg, OUTPUT_WORKDIR, copy_back=False)
    assert plan.out_dir == cfg.workdir / "task_1" / "out"
    assert plan.final_dir is None
    assert plan.copy_back is False


def test_copy_back_flag_ignored_in_samedir_mode(tmp_path):
    """samedir 模式本就落源目录，copy_back 无意义，不应造成异常。"""
    cfg = _cfg(tmp_path)
    plan = plan_output(_task(tmp_path), cfg, OUTPUT_SAMEDIR, copy_back=False)
    assert plan.out_dir == tmp_path / "src" / "_解压开镜" / "pack"
    assert plan.final_dir == plan.out_dir


def test_unique_path_directory(tmp_path):
    """目录避让：`pack` → `pack (2)`；序号递增。"""
    (tmp_path / "pack").mkdir()
    assert unique_path(tmp_path / "pack").name == "pack (2)"
    (tmp_path / "pack (2)").mkdir()
    assert unique_path(tmp_path / "pack").name == "pack (3)"


def test_unique_path_file_keeps_extension_last(tmp_path):
    """文件避让：`data.txt` → `data (2).txt`。

    回归：早期误用目录命名法得到 `data.txt (2)`，扩展名不再是最后一个
    后缀，按后缀识别类型的程序会认不出该文件。
    """
    (tmp_path / "data.txt").write_text("x", encoding="utf-8")
    got = unique_path(tmp_path / "data.txt")
    assert got.name == "data (2).txt"
    assert got.suffix == ".txt", "扩展名必须是最后一个后缀"


def test_unique_path_multi_suffix(tmp_path):
    """多段后缀：只保最后一段，序号插在主名后（多段后缀无法与主名里的点区分）。"""
    (tmp_path / "archive.tar.gz").write_text("x", encoding="utf-8")
    assert unique_path(tmp_path / "archive.tar.gz").name == "archive.tar (2).gz"


def test_unique_path_no_suffix(tmp_path):
    """无扩展名：序号直接接在名字后。"""
    (tmp_path / "README").write_text("x", encoding="utf-8")
    assert unique_path(tmp_path / "README").name == "README (2)"


def test_unique_path_returns_base_when_free(tmp_path):
    """不冲突时原样返回，不画蛇添足加序号。"""
    target = tmp_path / "fresh.zip"
    assert unique_path(target) == target


def test_task_status_not_touched(tmp_path):
    """输出决策不应改动任务状态（状态机只由 Pipeline._set 驱动）。"""
    cfg = _cfg(tmp_path)
    t = _task(tmp_path)
    plan_output(t, cfg, OUTPUT_SAMEDIR)
    assert t.status == TaskStatus.PENDING


def test_same_volume_true_within_tmp(tmp_path):
    """同一临时目录下的两个路径必然同卷。"""
    a = tmp_path / "x"
    a.mkdir()
    assert same_volume(a, tmp_path) is True


def test_same_volume_false_when_stat_fails(tmp_path):
    """路径不存在时按"不同卷"处理。

    这是有意的保守取向：判错成不同卷只是慢一点（退回复制），
    判错成同卷则会直接 rename 抛错、任务失败。
    """
    assert same_volume(tmp_path / "gone", tmp_path) is False
    assert same_volume(tmp_path, tmp_path / "no" / "such" / "dir") is False
