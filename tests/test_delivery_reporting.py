"""「解压成功但产物没交付」必须落库并双通道上报的守卫。

背景（2026-09-26 实测）：`.workspace/task_83` 里躺着 5.38 GB，而库里那条记录是
DONE、界面显示「完成」。旧实现里交付/归并失败只有两种收场：

- `report.warnings.append(msg) + continue` —— 警告只活在本次运行的内存里，
  窗口一关就没了，库里没有任何一行能回答"我上次那 5 GB 去哪了"；
- `except ValueError: continue` —— **连警告都没有**，整个失败静默消失。

这组测试钉住四件事：
1. 失败要**落库**（delivery_error），不是只发一句警告；
2. 状态**仍然是 done**——产物确实解出来了，判 FAILED 会让用户跑去重新解压；
3. 失败必须同时进 report（CLI 读）与事件流（UI 读），两边都不能瞎；
4. 交付成功时 extracted_dir 要指向**交付位置**，而不是工作目录里的兜底副本。
"""
import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from core import pipeline as pipeline_mod
from core.config import Config
from core.models import Task, TaskStatus
from core.output_plan import OutputPlan
from core.pipeline import Pipeline, RunReport
from core.store import TaskStore
from core.vault import PasswordVault


def _mk_pipe(tmp_path):
    """不依赖 7z 的 Pipeline：这些用例只驱动交付/归并环节，不解压。"""
    db = tmp_path / "app.db"
    cfg = Config(sevenzip_path=tmp_path / "7z.exe", workdir=tmp_path / "wd")
    store = TaskStore(db)
    pipe = Pipeline(cfg, PasswordVault(db), store)
    return pipe, store, cfg


def _mk_out_dir(workdir: Path, task_id: int, name: str) -> Path:
    d = workdir / f"task_{task_id}" / "out" / name
    d.mkdir(parents=True)
    (d / "payload.bin").write_bytes(b"x" * 64)
    return d


def _mk_task(store, **kw) -> Task:
    t = Task(**kw)
    t.id = store.create(t)
    return t


def _events(pipe):
    """把事件流收集起来（core 只通过回调吐事件，UI 就是这么接的）。"""
    got: list[dict] = []
    pipe._on_event = got.append
    return got


# ---------- 内层产物归并失败 ----------


def test_hierarchy_move_failure_is_persisted(tmp_path, monkeypatch):
    """搬入父产物失败：落库 + 告警 + 状态仍 done，产物原地留着不丢。"""
    pipe, store, cfg = _mk_pipe(tmp_path)
    parent = _mk_task(store, archive_path=str(tmp_path / "outer.zip"), depth=0,
                      status=TaskStatus.DONE)
    parent_dir = _mk_out_dir(cfg.workdir, parent.id, "outer")
    child_dir_candidate = parent_dir / "inner.zip"
    child = _mk_task(store, archive_path=str(child_dir_candidate), depth=1,
                     status=TaskStatus.DONE, parent_id=parent.id)
    child_out = _mk_out_dir(cfg.workdir, child.id, "inner")
    pipe._plans[parent.id] = OutputPlan(out_dir=parent_dir, final_dir=parent_dir)
    pipe._plans[child.id] = OutputPlan(out_dir=child_out)
    events = _events(pipe)

    def boom(*_a, **_kw):
        raise OSError("磁盘空间不足")

    monkeypatch.setattr(pipeline_mod.shutil, "move", boom)
    report = RunReport()
    pipe._deliver_hierarchy([parent, child], report)

    assert child.delivery_error, "归并失败没有记进任务对象"
    assert store.get(child.id).delivery_error == child.delivery_error, "交付失败没落库"
    assert child.status == TaskStatus.DONE, "交付失败不该把任务改成 FAILED"
    assert child_out.is_dir(), "产物原地留着（可能是唯一副本），不能顺手删"
    assert any("内层产物落到外层失败" in w for w in report.warnings), report.warnings
    assert any(e.get("kind") == "warning" for e in events), "UI 的事件通道没收到告警"
    status_events = [e for e in events if e.get("kind") == "status"]
    assert status_events and status_events[-1].get("delivery_error"), \
        "状态事件里没带 delivery_error，界面就标不出「未交付」"
    store.close()


def test_hierarchy_unlocatable_archive_is_reported_not_silent(tmp_path):
    """内层包不在父产物里（无从定位）：必须报告，**不能**静默 continue。

    旧实现的这条分支连 warnings 都不写：产物留在工作目录里、库里是 done、
    界面上什么都看不出来。这是"静默丢数据"的另一种形态——数据没丢，但用户
    永远找不到它。
    """
    pipe, store, cfg = _mk_pipe(tmp_path)
    parent = _mk_task(store, archive_path=str(tmp_path / "outer.zip"), depth=0,
                      status=TaskStatus.DONE)
    parent_dir = _mk_out_dir(cfg.workdir, parent.id, "outer")
    # 内层包被搬到别处去了：不在 parent_dir 之下
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    child = _mk_task(store, archive_path=str(elsewhere / "inner.zip"), depth=1,
                     status=TaskStatus.DONE, parent_id=parent.id)
    child_out = _mk_out_dir(cfg.workdir, child.id, "inner")
    pipe._plans[parent.id] = OutputPlan(out_dir=parent_dir, final_dir=parent_dir)
    pipe._plans[child.id] = OutputPlan(out_dir=child_out)
    events = _events(pipe)

    report = RunReport()
    pipe._deliver_hierarchy([parent, child], report)

    assert child.delivery_error, "无从定位的产物被静默放弃了"
    assert store.get(child.id).delivery_error, "没落库"
    assert any("无法定位归属" in w for w in report.warnings), report.warnings
    assert any(e.get("kind") == "warning" for e in events), "UI 没收到告警"
    store.close()


def test_delivery_failed_counted_separately_from_failed(tmp_path):
    """交付失败单独计数：不能算 failed，也不能只躺在 warnings 里。"""
    pipe, store, cfg = _mk_pipe(tmp_path)
    parent = _mk_task(store, archive_path=str(tmp_path / "outer.zip"), depth=0,
                      status=TaskStatus.DONE)
    parent_dir = _mk_out_dir(cfg.workdir, parent.id, "outer")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    child = _mk_task(store, archive_path=str(elsewhere / "inner.zip"), depth=1,
                     status=TaskStatus.DONE, parent_id=parent.id)
    child_out = _mk_out_dir(cfg.workdir, child.id, "inner")
    pipe._plans[parent.id] = OutputPlan(out_dir=parent_dir, final_dir=parent_dir)
    pipe._plans[child.id] = OutputPlan(out_dir=child_out)

    report = RunReport()
    pipe._deliver_hierarchy([parent, child], report)
    pipe._fill_report(report, [parent, child])

    assert report.done == 2, "两个包都解压成功了"
    assert report.failed == 0, "交付失败被误算成解压失败"
    assert report.delivery_failed == 1
    assert [t.id for t in report.delivery_failed_tasks] == [child.id]
    store.close()


# ---------- 最外层交付回源 ----------


def test_copy_back_failure_is_persisted(tmp_path, monkeypatch):
    """交付回源目录失败：落库 + 告警，任务状态仍是 done，工作目录产物不丢。"""
    pipe, store, cfg = _mk_pipe(tmp_path)
    task = _mk_task(store, archive_path=str(tmp_path / "downloads" / "pack.zip"),
                    depth=0, status=TaskStatus.DONE)
    out_dir = _mk_out_dir(cfg.workdir, task.id, "pack")
    final = tmp_path / "downloads" / "pack"
    pipe._plans[task.id] = OutputPlan(out_dir=out_dir, final_dir=final, copy_back=True)
    events = _events(pipe)

    monkeypatch.setattr(pipeline_mod, "same_volume", lambda a, b: False)

    def boom(*_a, **_kw):
        raise OSError("磁盘空间不足")

    monkeypatch.setattr(pipeline_mod.shutil, "copytree", boom)
    report = RunReport()
    pipe._deliver([task], report)

    assert task.delivery_error, "交付失败没有记进任务对象"
    stored = store.get(task.id)
    assert stored.delivery_error == task.delivery_error, "交付失败没落库"
    assert stored.status == TaskStatus.DONE, "交付失败不该把任务改成 FAILED"
    assert out_dir.is_dir(), "失败后工作目录里的产物必须留着"
    assert f"仍在工作目录 {out_dir}" in task.delivery_error, \
        "原因里要写明产物现在在哪，否则用户去找都不知道去哪找"
    assert any("交付到源目录失败" in w for w in report.warnings), report.warnings
    assert any(e.get("kind") == "warning" for e in events)

    pipe._fill_report(report, [task])
    assert report.delivery_failed == 1 and report.failed == 0
    store.close()


def test_successful_delivery_points_extracted_dir_at_target(tmp_path):
    """交付成功后 extracted_dir 指向交付位置，且 delivery_error 保持为空。

    跨卷时 _place_tree 走 copytree，工作目录里的源目录不会消失（兜底副本）。
    若此时 extracted_dir 还指着工作目录：界面「打开目录」开到隔离区、
    残留盘点也会把这份冗余副本误判成"还没交付"。
    """
    pipe, store, cfg = _mk_pipe(tmp_path)
    task = _mk_task(store, archive_path=str(tmp_path / "downloads" / "pack.zip"),
                    depth=0, status=TaskStatus.DONE)
    out_dir = _mk_out_dir(cfg.workdir, task.id, "pack")
    final = tmp_path / "downloads" / "pack"
    pipe._plans[task.id] = OutputPlan(out_dir=out_dir, final_dir=final, copy_back=True)

    report = RunReport()
    pipe._deliver([task], report)   # 同卷 → rename，out_dir 被搬走

    stored = store.get(task.id)
    assert stored.delivery_error == ""
    assert stored.extracted_dir == str(final), "落点没更新到交付位置"
    assert final.is_dir() and (final / "payload.bin").is_file()
    assert not out_dir.exists(), "同卷应搬走而非留副本"
    pipe._fill_report(report, [task])
    assert report.delivery_failed == 0
    store.close()


def test_no_copy_back_does_not_report_delivery_failure(tmp_path):
    """纯隔离模式（copy_back=False）不交付，也**不该**被记成交付失败。"""
    pipe, store, cfg = _mk_pipe(tmp_path)
    task = _mk_task(store, archive_path=str(tmp_path / "downloads" / "pack.zip"),
                    depth=0, status=TaskStatus.DONE)
    out_dir = _mk_out_dir(cfg.workdir, task.id, "pack")
    pipe._plans[task.id] = OutputPlan(out_dir=out_dir, final_dir=None, copy_back=False)

    report = RunReport()
    pipe._deliver([task], report)
    pipe._fill_report(report, [task])

    assert store.get(task.id).delivery_error == ""
    assert report.delivery_failed == 0
    assert out_dir.is_dir()
    store.close()


# ---------- CLI 呈现 ----------


def test_cli_report_shows_delivery_failure_section(tmp_path):
    """CLI 必须把未交付单独成段——混在「警告」里用户会直接略过。"""
    import cli

    t = Task(id=83, archive_path=str(tmp_path / "inner.zip"), depth=1,
             status=TaskStatus.DONE,
             delivery_error="交付到源目录失败（产物仍在工作目录 D:/wd/task_83/out）：磁盘空间不足")
    report = RunReport(done=1, delivery_failed=1, delivery_failed_tasks=[t])
    cfg = Config(sevenzip_path=tmp_path / "7z.exe", workdir=tmp_path / "wd")

    buf = io.StringIO()
    with redirect_stdout(buf):
        cli._print_report(report, cfg)
    out = buf.getvalue()

    assert "未交付 1" in out
    assert "解压成功但产物未交付" in out
    assert "磁盘空间不足" in out
    assert "clean-workdir" in out, "必须给出一条可执行的下一步"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
