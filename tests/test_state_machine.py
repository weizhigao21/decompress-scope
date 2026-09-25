import pytest

from core.models import Task, TaskStatus, can_transition, require_transition
from core.store import TaskStore


def test_valid_transitions():
    assert can_transition(TaskStatus.PENDING, TaskStatus.PROBING)
    assert can_transition(TaskStatus.EXTRACTING, TaskStatus.DONE)
    assert can_transition(TaskStatus.NEEDS_PASSWORD, TaskStatus.EXTRACTING)
    assert can_transition(TaskStatus.FAILED, TaskStatus.PENDING)


def test_invalid_transitions():
    with pytest.raises(Exception):
        require_transition(TaskStatus.PENDING, TaskStatus.DONE)
    with pytest.raises(Exception):
        require_transition(TaskStatus.DONE, TaskStatus.PROBING)


def test_store_enforces_transitions(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    task = Task(archive_path="x.zip")
    task.id = store.create(task)
    task.status = TaskStatus.PROBING
    store.update(task)
    task.status = TaskStatus.DONE
    with pytest.raises(Exception):
        store.update(task)
    store.close()


def _drive(store: TaskStore, path: str, *statuses) -> int:
    """按顺序推进一个任务的状态，返回其 id。"""
    task = Task(archive_path=path)
    task.id = store.create(task)
    for st in statuses:
        task.status = st
        store.update(task)
    return task.id


def test_find_done_returns_none_for_unknown_path(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    assert store.find_done("never-seen.zip") is None
    store.close()


def test_find_done_requires_done_status(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    _drive(store, "a.zip", TaskStatus.PROBING, TaskStatus.EXTRACTING)
    assert store.find_done("a.zip") is None, "未完成的任务不该被当成已完成"
    store.close()


def test_find_done_returns_the_done_row(tmp_path):
    store = TaskStore(tmp_path / "t.db")
    tid = _drive(store, "a.zip", TaskStatus.PROBING, TaskStatus.EXTRACTING, TaskStatus.DONE)
    found = store.find_done("a.zip")
    assert found is not None
    assert found.id == tid and found.status == TaskStatus.DONE
    store.close()


def test_find_done_uses_latest_conclusion(tmp_path):
    """同一路径最近一次是 FAILED 时，不得翻出更早的那条 DONE。

    否则"上次解失败了"的包在重跑时会被幂等跳过：既没有任何产出，程序还
    宣称已处理过——比重复解压隐蔽得多。
    """
    store = TaskStore(tmp_path / "t.db")
    _drive(store, "a.zip", TaskStatus.PROBING, TaskStatus.EXTRACTING, TaskStatus.DONE)
    assert store.find_done("a.zip") is not None

    _drive(store, "a.zip", TaskStatus.PROBING, TaskStatus.FAILED)

    assert store.find_done("a.zip") is None, "最近一次是失败，却翻出了更早的成功记录"
    store.close()
