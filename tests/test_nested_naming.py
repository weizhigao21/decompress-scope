"""嵌套压缩包产物的命名与层级：每层包名各占一级目录。

需求原话：「它解压的时候，它使用的文件名没有使用最后一次文件名，而是使用第一个。
或者可以设计一个保留全部的文件名还有它的层级。」

选定「保留完整层级」。旧实现把内层包解在隔离目录后，**把内容拍平并入外层目录**：
`新建压缩文件.zip`（内含 `内层真名.zip`）解出来是 `_解压开镜/新建压缩文件/内容`——
内层包名彻底消失，用户拿到的东西看不出是从哪个包来的；一个外层套多个内层包时，
各包内容更是全部糊在同一层。

新规则：内层包的产物整目录搬到**它在父产物中的原位置**，并改名成自身包名。
`outer.zip → data/inner.zip → deep.txt` 得到 `<交付>/outer/data/inner/deep.txt`。
"""
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


def _zip_of(exe: Path, where: Path, name: str, *items: str) -> Path:
    """在 where 下把 items 打包成 name，返回包的完整路径。"""
    where.mkdir(parents=True, exist_ok=True)
    _run7z(exe, ["a", name, *items], where)
    return where / name


def _nested_pair(tmp_path: Path) -> Path:
    """造 `outer.zip` → `内层真名.zip` → `deep.txt`，返回外层包路径。

    内层特意用「内层真名」这种一眼可辨的名字，才能断言产物目录名取自哪一层。
    """
    exe = _sevenzip_or_skip()
    inner_stage = tmp_path / "inner_stage"
    (inner_stage).mkdir()
    (inner_stage / "deep.txt").write_text("inner", encoding="utf-8")
    inner = _zip_of(exe, inner_stage, "内层真名.zip", "deep.txt")

    outer_stage = tmp_path / "outer_stage"
    outer_stage.mkdir()
    shutil.move(str(inner), str(outer_stage / inner.name))
    return _zip_of(exe, outer_stage, "outer.zip", inner.name)


def _tree(root: Path) -> list[str]:
    """目录下所有条目（相对路径），失败信息里用它代替干巴巴的断言。"""
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# ---------- 核心：内层包名成为一级目录 ----------


def test_inner_archive_name_becomes_a_subdirectory(tmp_path):
    """内层包名必须留在产物里，且内容不再被拍平到外层目录。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    shutil.move(str(_nested_pair(tmp_path)), str(inputs / "outer.zip"))

    report = pipe.run([inputs])

    assert report.done == 2, report.warnings
    delivered = inputs / "_解压开镜" / "outer"
    assert (delivered / "内层真名" / "deep.txt").is_file(), (
        f"内层包名没成为目录；实际 {_tree(delivered)}"
    )
    # 旧行为：内层内容直接并入外层交付目录，包名随之丢失
    assert not (delivered / "deep.txt").exists(), "内层内容仍被拍平到外层目录"
    # 已消化的内层包不该交付给用户
    assert not list(delivered.rglob("*.zip")), "交付目录残留中间包"


def test_inner_archive_keeps_its_original_subdir_position(tmp_path):
    """内层包藏在父产物子目录里时，解出的目录要落在同一个子目录，而不是浮到顶层。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    inner_stage = tmp_path / "inner_stage"
    inner_stage.mkdir()
    (inner_stage / "deep.txt").write_text("inner", encoding="utf-8")
    inner = _zip_of(exe, inner_stage, "inner.zip", "deep.txt")

    outer_stage = tmp_path / "outer_stage"
    (outer_stage / "sub").mkdir(parents=True)
    shutil.move(str(inner), str(outer_stage / "sub" / "inner.zip"))
    # 再放一个无关文件：否则外层只含一个目录，会被「单目录上提」规则吃掉 sub/
    (outer_stage / "readme.txt").write_text("outer", encoding="utf-8")
    outer = _zip_of(exe, outer_stage, "outer.zip", "sub", "readme.txt")
    shutil.move(str(outer), str(inputs / "outer.zip"))

    pipe.run([inputs])

    delivered = inputs / "_解压开镜" / "outer"
    assert (delivered / "sub" / "inner" / "deep.txt").is_file(), (
        f"内层的相对位置没被保留；实际 {_tree(delivered)}"
    )
    assert (delivered / "readme.txt").is_file()
    assert not (delivered / "inner").exists(), "内层产物浮到了交付目录顶层"


def test_three_levels_keep_the_full_chain(tmp_path):
    """三层套娃 A → B → C：产物路径要完整反映三层名字。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    s1 = tmp_path / "s1"
    s1.mkdir()
    (s1 / "c.txt").write_text("c", encoding="utf-8")
    c = _zip_of(exe, s1, "C.zip", "c.txt")

    s2 = tmp_path / "s2"
    s2.mkdir()
    shutil.move(str(c), str(s2 / "C.zip"))
    b = _zip_of(exe, s2, "B.zip", "C.zip")

    s3 = tmp_path / "s3"
    s3.mkdir()
    shutil.move(str(b), str(s3 / "B.zip"))
    a = _zip_of(exe, s3, "A.zip", "B.zip")
    shutil.move(str(a), str(inputs / "A.zip"))

    report = pipe.run([inputs])

    assert report.done == 3, report.warnings
    delivered = inputs / "_解压开镜" / "A"
    assert (delivered / "B" / "C" / "c.txt").is_file(), (
        f"三层名字没保留完整；实际 {_tree(delivered)}"
    )
    assert not list(delivered.rglob("*.zip"))


def test_sibling_inner_archives_get_separate_dirs(tmp_path):
    """一个外层套多个内层包：各包内容各占一个目录，不能糊在一起。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    s = tmp_path / "s"
    s.mkdir()
    (s / "b.txt").write_text("from B", encoding="utf-8")
    (s / "c.txt").write_text("from C", encoding="utf-8")
    b = _zip_of(exe, s, "B.zip", "b.txt")
    c = _zip_of(exe, s, "C.zip", "c.txt")

    outer_stage = tmp_path / "outer_stage"
    outer_stage.mkdir()
    shutil.move(str(b), str(outer_stage / "B.zip"))
    shutil.move(str(c), str(outer_stage / "C.zip"))
    outer = _zip_of(exe, outer_stage, "outer.zip", "B.zip", "C.zip")
    shutil.move(str(outer), str(inputs / "outer.zip"))

    report = pipe.run([inputs])

    assert report.done == 3, report.warnings
    delivered = inputs / "_解压开镜" / "outer"
    assert (delivered / "B" / "b.txt").read_text(encoding="utf-8") == "from B"
    assert (delivered / "C" / "c.txt").read_text(encoding="utf-8") == "from C"


def test_inner_name_clash_does_not_overwrite(tmp_path):
    """内层包名撞上包内自带的同名目录时避让，两边内容都不能丢。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    s = tmp_path / "s"
    s.mkdir()
    (s / "deep.txt").write_text("from archive", encoding="utf-8")
    _zip_of(exe, s, "inner.zip", "deep.txt")
    # 包里同时自带一个叫 inner/ 的目录（用户在包里放的东西）
    (s / "inner").mkdir()
    (s / "inner" / "keep.txt").write_text("kept", encoding="utf-8")
    outer = _zip_of(exe, s, "outer.zip", "inner", "inner.zip")
    shutil.move(str(outer), str(inputs / "outer.zip"))

    report = pipe.run([inputs])

    assert report.done == 2, report.warnings
    delivered = inputs / "_解压开镜" / "outer"
    assert (delivered / "inner" / "keep.txt").read_text(encoding="utf-8") == "kept"
    assert (delivered / "inner (2)" / "deep.txt").is_file(), (
        f"同名冲突时内层产物被吞掉；实际 {_tree(delivered)}"
    )


# ---------- 两种输出模式都要成立 ----------


def test_workdir_mode_keeps_hierarchy_after_copy_back(tmp_path):
    """workdir + 复制回源：交回源目录的产物同样保留层级，且不留中间包。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_WORKDIR)
    shutil.move(str(_nested_pair(tmp_path)), str(inputs / "outer.zip"))

    report = pipe.run([inputs])

    assert report.done == 2, report.warnings
    delivered = inputs / "_解压开镜" / "outer"
    assert (delivered / "内层真名" / "deep.txt").is_file(), _tree(delivered)
    assert not list((inputs / "_解压开镜").rglob("*.zip")), "交付目录残留中间包"


def test_workdir_without_copy_back_keeps_hierarchy_in_workdir(tmp_path):
    """纯隔离模式：工作目录里的产物也要保留层级。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_WORKDIR)
    pipe.copy_back = False
    shutil.move(str(_nested_pair(tmp_path)), str(inputs / "outer.zip"))

    report = pipe.run([inputs])

    assert report.done == 2, report.warnings
    hits = list(cfg.workdir.rglob("deep.txt"))
    assert len(hits) == 1, f"工作目录里的产物份数不对：{hits}"
    assert hits[0].parent.name == "内层真名", f"没按内层包名建目录：{hits[0]}"
    assert hits[0].parent.parent.name == "outer", "外层名字那一级丢了"


def test_keeping_intermediate_leaves_archive_beside_its_dir(tmp_path):
    """关掉「删除中间层压缩包」时，内层包与它解出的目录并存，互不覆盖。

    旧实现下这一点是坏的：内层包被 _deliver_hierarchy 从内层 out_dir 里无条件
    unlink，于是「保留中间层」这个开关对嵌套包形同虚设。
    """
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    cfg.delete_intermediate = False
    shutil.move(str(_nested_pair(tmp_path)), str(inputs / "outer.zip"))

    report = pipe.run([inputs])

    assert report.done == 2, report.warnings
    delivered = inputs / "_解压开镜" / "outer"
    assert (delivered / "内层真名.zip").is_file(), "开关要求保留内层包，但它被删了"
    assert (delivered / "内层真名" / "deep.txt").is_file(), (
        f"内层包与它解出的目录没并存；实际 {_tree(delivered)}"
    )


# ---------- 安全性：绝不能凭空造目录 / 留垃圾 ----------


def test_inner_shell_is_pruned_from_workdir(tmp_path):
    """内层产物搬走后，工作目录里不该剩下 task_<id>/out 空壳。"""
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    shutil.move(str(_nested_pair(tmp_path)), str(inputs / "outer.zip"))

    pipe.run([inputs])

    leftovers = [p for p in cfg.workdir.rglob("out") if p.is_dir()]
    assert not leftovers, f"工作目录残留空壳目录：{leftovers}"


def test_failed_parent_does_not_materialize_inner_products(tmp_path):
    """父任务失败时不得把子产物搬进它的 out_dir。

    子任务只由「父任务解压成功」派生，正常路径下父任务必为 DONE；但
    _enqueue_children 与 _set(DONE) 之间若抛异常（库写入失败等），父任务会落到
    FAILED 而子任务已在队列里。此时把子产物搬进父的 out_dir 等于凭空复活一个
    已被收掉的目录——samedir 模式下它会直接出现在用户源目录里。

    注意用例必须让相对路径**可算**（子包确实位于父 out_dir 内），否则光靠
    relative_to 失败也会「通过」，那样就证明不了这道守卫在起作用。
    """
    exe, inputs, cfg, pipe = _make_env(tmp_path, OUTPUT_SAMEDIR)
    from core.models import Task, TaskStatus
    from core.output_plan import OutputPlan
    from core.pipeline import RunReport

    parent_out = cfg.workdir / "task_1" / "out" / "outer"
    parent = Task(id=1, archive_path=str(inputs / "outer.zip"), depth=0,
                  status=TaskStatus.FAILED)
    child = Task(id=2, archive_path=str(parent_out / "inner.zip"), parent_id=1,
                 depth=1, status=TaskStatus.DONE)
    child_out = cfg.workdir / "task_2" / "out" / "inner"
    child_out.mkdir(parents=True)
    (child_out / "deep.txt").write_text("inner", encoding="utf-8")
    pipe._plans[1] = OutputPlan(out_dir=parent_out,
                                final_dir=inputs / "_解压开镜" / "outer", copy_back=True)
    pipe._plans[2] = OutputPlan(out_dir=child_out)

    report = RunReport()
    pipe._deliver_hierarchy([parent, child], report)

    assert not parent_out.exists(), "父任务失败，却复活着它的 out_dir"
    assert child_out.is_dir(), "子产物不该被搬走（应留在工作目录里）"
