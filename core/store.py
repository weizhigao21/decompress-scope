"""任务表 SQLite 存储，含状态迁移校验。"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from .models import Task, TaskStatus, now_iso, require_transition

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_path TEXT NOT NULL,
    parent_id INTEGER,
    depth INTEGER NOT NULL DEFAULT 0,
    source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    password_used TEXT,
    error TEXT NOT NULL DEFAULT '',
    extracted_dir TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_SELECT_COLS = (
    "id, archive_path, parent_id, depth, source, status, password_used,"
    " error, extracted_dir, created_at, updated_at"
)


class TaskStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def create(self, task: Task) -> int:
        now = now_iso()
        task.created_at = now
        task.updated_at = now
        cur = self.conn.execute(
            "INSERT INTO tasks(archive_path, parent_id, depth, source, status, created_at, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (task.archive_path, task.parent_id, task.depth, task.source, task.status.value, now, now),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def update(self, task: Task) -> None:
        row = self.conn.execute("SELECT status FROM tasks WHERE id=?", (task.id,)).fetchone()
        task.updated_at = now_iso()
        if row is not None:
            require_transition(TaskStatus(row[0]), task.status)
        self.conn.execute(
            "UPDATE tasks SET status=?, password_used=?, error=?, extracted_dir=?, updated_at=?"
            " WHERE id=?",
            (task.status.value, task.password_used, task.error, task.extracted_dir, task.updated_at, task.id),
        )
        self.conn.commit()

    def get(self, task_id: int) -> Task | None:
        row = self.conn.execute(
            f"SELECT {_SELECT_COLS} FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        return self._to_task(row) if row else None

    def list_all(self) -> list[Task]:
        rows = self.conn.execute(
            f"SELECT {_SELECT_COLS} FROM tasks ORDER BY id"
        ).fetchall()
        return [self._to_task(r) for r in rows]

    @staticmethod
    def _to_task(row: tuple) -> Task:
        return Task(
            id=row[0], archive_path=row[1], parent_id=row[2], depth=row[3], source=row[4],
            status=TaskStatus(row[5]), password_used=row[6], error=row[7],
            extracted_dir=row[8], created_at=row[9], updated_at=row[10],
        )
