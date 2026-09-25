"""后台解压线程：在工作线程跑 core Pipeline，把事件桥接为 Qt 信号。

注意：
- SQLite 连接（vault/store）在工作线程内创建并使用，不跨线程。
- 需要密码时，工作线程通过 need_password 信号请求 UI 弹窗，并阻塞等待
  submit_password() 的回答（threading.Event 同步）。
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
    need_password = Signal(dict)   # {"path": ...} 请求 UI 弹窗要密码
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
        self._pwd_reply: str | None = None

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
                )
            finally:
                vault.close()
                store.close()
            self.finished_ok.emit(report)
        except Exception as exc:  # noqa: BLE001 线程内兜底，错误回 UI
            self.failed.emit(f"{exc.__class__.__name__}: {exc}")

    def _ask_password(self, info: dict) -> str | None:
        """工作线程内阻塞等 UI 回答密码；None/空 = 放弃该包。"""
        self._pwd_event.clear()
        self._pwd_reply = None
        self.need_password.emit(info)
        self._pwd_event.wait()
        return self._pwd_reply

    # ---------- UI 线程调用 ----------

    def submit_password(self, pwd: str | None) -> None:
        self._pwd_reply = (pwd or "").strip() or None
        self._pwd_event.set()
