"""后台解压线程：在工作线程跑 core Pipeline，把事件桥接为 Qt 信号。

注意：
- SQLite 连接（vault/store）在工作线程内创建并使用，不跨线程。
- 需要密码时，工作线程通过 need_password 信号请求 UI 回答，并阻塞等待
  submit_password() 的回答。
- 密码回答一律走 `_pwd_inbox` 队列 + `_pwd_event` 唤醒，不存"最后一个回答"的
  单值：用户可能**先手**把密码填好（pipeline 还在试候选、没开口问），也可能在
  pipeline 等着的时候才提交。两种时序共用一条路径，才不会出现"先手提交被清掉"
  或"上一轮的回答被下一轮误用"。
- 取消通过 threading.Event 传递：UI 置位后，当前任务完成即停止处理剩余任务。
"""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from core.config import Config
from core.pipeline import Pipeline
from core.store import TaskStore
from core.vault import PasswordVault


class ExtractWorker(QObject):
    task_event = Signal(dict)      # core pipeline 的 task/status/warning 事件
    need_password = Signal(dict)   # {"path": ...} 请求 UI 回答密码
    finished_ok = Signal(object)   # RunReport
    failed = Signal(str)

    def __init__(self, cfg: Config, db_path: Path, inputs: list[str],
                 source: str = "", cancel: threading.Event | None = None,
                 output_mode: str | None = None, subdir_name: str | None = None,
                 copy_back: bool | None = None):
        super().__init__()
        self._cfg = cfg
        self._db_path = Path(db_path)
        self._inputs = [Path(p) for p in inputs]
        self._source = source
        self._cancel = cancel or threading.Event()
        self._output_mode = output_mode
        self._subdir_name = subdir_name
        self._copy_back = copy_back
        self._pwd_event = threading.Event()
        self._pwd_lock = threading.Lock()
        self._pwd_inbox: list[str | None] = []

    # ---------- 工作线程内执行 ----------

    def run(self) -> None:
        try:
            vault = PasswordVault(self._db_path)
            store = TaskStore(self._db_path)
            try:
                pipeline = Pipeline(self._cfg, vault, store)
                if self._output_mode:
                    pipeline.output_mode = self._output_mode
                if self._subdir_name:
                    pipeline.subdir_name = self._subdir_name
                if self._copy_back is not None:
                    pipeline.copy_back = self._copy_back
                report = pipeline.run(
                    self._inputs,
                    source=self._source,
                    on_event=self.task_event.emit,
                    should_cancel=self._cancel.is_set,
                    ask_password=self._ask_password,
                    password_ready=self._password_ready,
                )
            finally:
                vault.close()
                store.close()
            self.finished_ok.emit(report)
        except Exception as exc:  # noqa: BLE001 线程内兜底，错误回 UI
            self.failed.emit(f"{exc.__class__.__name__}: {exc}")

    def _ask_password(self, info: dict) -> str | None:
        """工作线程内取 UI 的密码回答；None/空 = 放弃该包。

        先看队列里有没有**先手提交**的回答（用户在 pipeline 还在试候选时就填好了）：
        有就直接取走，不必再让 UI 回答一次。没有才发 need_password 并阻塞等待。
        回答一律从队列取，事件只当唤醒用 —— 这样两种时序不会互相踩。
        """
        with self._pwd_lock:
            if self._pwd_inbox:
                return self._pwd_inbox.pop(0)
        self._pwd_event.clear()
        self.need_password.emit(info)
        self._pwd_event.wait()
        with self._pwd_lock:
            return self._pwd_inbox.pop(0) if self._pwd_inbox else None

    def _password_ready(self) -> str | None:
        """候选循环的探针：用户是否已经先手提交了密码（不等 pipeline 开口问）。"""
        with self._pwd_lock:
            return self._pwd_inbox[0] if self._pwd_inbox else None

    # ---------- UI 线程调用 ----------

    def submit_password(self, pwd: str | None) -> None:
        """UI 线程提交密码回答（None/空 = 放弃该包）。

        pipeline 还没开口问时提交也安全：答案进队列躺着，`_password_ready()` 会让
        候选循环提前收手用它，`_ask_password()` 也会优先取走它。
        """
        value = (pwd or "").strip() or None
        with self._pwd_lock:
            self._pwd_inbox.append(value)
        self._pwd_event.set()
