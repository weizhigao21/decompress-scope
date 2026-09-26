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
    delivery_error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

# 老库补列。CREATE TABLE IF NOT EXISTS 对**已存在**的表是空操作，所以给表加列
# 必须显式 ALTER —— 否则老库一读就 "no such column: delivery_error"，
# 而用户的库里恰恰躺着上次那批任务的记录（正是需要显示「未交付」的那批）。
_COLUMN_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("delivery_error", "ALTER TABLE tasks ADD COLUMN delivery_error TEXT NOT NULL DEFAULT ''"),
)

_SELECT_COLS = (
    "id, archive_path, parent_id, depth, source, status, password_used,"
    " error, extracted_dir, delivery_error, created_at, updated_at"
)


class TaskStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.executescript(_SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        have = {row[1] for row in self.conn.execute("PRAGMA table_info(tasks)")}
        for name, ddl in _COLUMN_MIGRATIONS:
            if name not in have:
                self.conn.execute(ddl)

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
            "UPDATE tasks SET status=?, password_used=?, error=?, extracted_dir=?,"
            " delivery_error=?, updated_at=? WHERE id=?",
            (task.status.value, task.password_used, task.error, task.extracted_dir,
             task.delivery_error, task.updated_at, task.id),
        )
        self.conn.commit()

    def get(self, task_id: int) -> Task | None:
        row = self.conn.execute(
            f"SELECT {_SELECT_COLS} FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        return self._to_task(row) if row else None

    def find_done(self, archive_path: str) -> Task | None:
        """该路径**最近一次**结论为 DONE 的记录；否则 None（供幂等跳过判定）。

        为什么取"最近一次"而不是"有没有过 DONE"：同一路径可能被反复处理，
        留下的历史里既有成功的也有后来失败的。只有最近一次是 DONE 才说明
        上次确实解出来了；最近一次是 FAILED 时应当重新处理，不能被更早的
        一条成功记录骗过去。
        """
        row = self.conn.execute(
            f"SELECT {_SELECT_COLS} FROM tasks WHERE archive_path=? ORDER BY id DESC LIMIT 1",
            (archive_path,),
        ).fetchone()
        if row is None:
            return None
        task = self._to_task(row)
        return task if task.status == TaskStatus.DONE else None

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
            extracted_dir=row[8], delivery_error=row[9],
            created_at=row[10], updated_at=row[11],
        )
