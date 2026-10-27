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

from core.cloud_unlock import STATUS_ERROR, CloudUnlocker
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


class CloudQueryJob:
    """手动「云端取密码」按钮的后台任务：线程跑查询，主线程用 QTimer 轮询结果。

    为什么是**轮询**而不是 Qt 信号：实测（Python 3.10 + PySide6 6.12）从普通
    Python 线程 emit 一个 Qt 信号，会让整个进程随机
    `Fatal Python error: Aborted`（崩在 `thread.join` 上，无栈无提示；
    `tests/test_ui_worker.py` 那批用例在 0.7.1 上就能复现）。改成"线程写结果、
    主线程来读"之后，跨线程只传一个 Python 对象引用，读取发生在主线程，
    没有任何跨线程的 Qt 调用。

    也刻意不用 QThread：右键那个紧凑窗口退场很快（结果目录一打开就关），用户
    完全可能在查询途中把窗口关掉，而 QThread 在还跑着的时候被销毁会让进程崩溃；
    daemon 线程随进程退出，不存在这个时序。

    与 pipeline 内部的自动云端查询是两条独立路径：
    - 自动那条在 core 里，本地候选跑过一段才走；
    - 这条是手动的，用户点按钮就要立刻查，不看他有没有把自动云解密关掉。
    """

    def __init__(self, archive: str, udid: str = "", timeout: float = 10.0,
                 unlocker=None):
        self._archive = Path(archive)
        self._unlocker = unlocker or CloudUnlocker(timeout=timeout, udid=udid)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._result: dict | None = None

    def start(self) -> bool:
        """起一个后台线程；已经在跑就不重复起。返回是否真的起了一个。"""
        if self.busy():
            return False
        self._thread = threading.Thread(
            target=self._run, name="cloud-unlock", daemon=True)
        self._thread.start()
        return True

    def busy(self) -> bool:
        """还没拿到结果（界面用它禁掉重复点击）。"""
        with self._lock:
            if self._result is not None:
                return False
        return self._thread is not None and self._thread.is_alive()

    def poll(self) -> dict | None:
        """主线程调用：有结果就取走（只取到一次），否则 None。"""
        with self._lock:
            payload, self._result = self._result, None
        return payload

    def wait(self, timeout: float | None = None) -> None:
        """测试用：等后台线程结束（正常流程不调用）。"""
        thread = self._thread
        if thread is not None:
            thread.join(timeout)

    def _run(self) -> None:
        try:
            result = self._unlocker.query(self._archive)
            payload = {
                "status": result.status,
                "password": result.password,
                "message": result.message,
            }
        except Exception as exc:  # noqa: BLE001 线程内兜底，错误回窗口
            payload = {
                "status": STATUS_ERROR,
                "password": None,
                "message": f"{exc.__class__.__name__}: {exc}",
            }
        with self._lock:
            self._result = payload
