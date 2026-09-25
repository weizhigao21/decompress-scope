"""数据模型与任务状态机。"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path


class TaskStatus(str, Enum):
    PENDING = "pending"
    PROBING = "probing"
    EXTRACTING = "extracting"
    NEEDS_PASSWORD = "needs_password"
    DONE = "done"
    FAILED = "failed"


_TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.PENDING: {TaskStatus.PROBING, TaskStatus.FAILED},
    TaskStatus.PROBING: {TaskStatus.EXTRACTING, TaskStatus.NEEDS_PASSWORD, TaskStatus.FAILED},
    TaskStatus.EXTRACTING: {TaskStatus.DONE, TaskStatus.NEEDS_PASSWORD, TaskStatus.FAILED},
    TaskStatus.NEEDS_PASSWORD: {TaskStatus.EXTRACTING, TaskStatus.FAILED},
    TaskStatus.DONE: set(),
    TaskStatus.FAILED: {TaskStatus.PENDING},  # 允许手动重试
}


def can_transition(old: TaskStatus, new: TaskStatus) -> bool:
    return new in _TRANSITIONS[old]


class TransitionError(ValueError):
    pass


def require_transition(old: TaskStatus, new: TaskStatus) -> None:
    if old == new:
        return
    if not can_transition(old, new):
        raise TransitionError(f"非法状态迁移: {old.value} -> {new.value}")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class ArchiveEntry:
    path: str
    size: int
    is_dir: bool = False


@dataclass
class ArchiveInfo:
    path: Path
    format_name: str = ""
    archive_size: int = 0
    file_count: int = 0
    dir_count: int = 0
    total_uncompressed: int = 0
    flag_encrypted: bool = False
    needs_password_for_listing: bool = False
    entries: list[ArchiveEntry] = field(default_factory=list)

    @property
    def compression_ratio(self) -> float:
        if self.archive_size <= 0:
            return 0.0
        return self.total_uncompressed / self.archive_size


@dataclass
class AttemptOutcome:
    """一轮密码尝试的结果。"""

    password: str | None = None
    message: str = ""
    password_issue: bool = False


@dataclass
class Task:
    id: int | None = None
    archive_path: str = ""
    parent_id: int | None = None
    depth: int = 0
    source: str = ""
    status: TaskStatus = TaskStatus.PENDING
    password_used: str | None = None
    error: str = ""
    extracted_dir: str = ""
    created_at: str = ""
    updated_at: str = ""
