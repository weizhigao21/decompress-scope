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
