"""Explorer 右键入口使用的紧凑自动解压窗口。"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QTimer
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.appconfig import OPEN_NONE, OUTPUT_SAMEDIR, AppConfig
from core.config import Config
from core.runtime_paths import runtime_data_root
from ui import theme
from ui.glass import GlassWindowMixin
from ui.worker import ExtractWorker

PROJECT_ROOT = runtime_data_root(Path(__file__).resolve().parent.parent)
DEFAULT_DB = PROJECT_ROOT / "data" / "jieya.db"
CONFIG_PATH = PROJECT_ROOT / "config.json"

# 自动打开结果目录之后，本窗退场的延迟。不立刻 close()：那样前台焦点会先回到
# 此前的窗口，而资源管理器刚被拉起、还没来得及露头 —— 用户看到的是"窗口没了、
# 目录也没打开"。留一点时间让它先出现，再让本窗消失。
_CLOSE_AFTER_OPEN_DELAY_MS = 400


class QuickExtractWindow(GlassWindowMixin, QDialog):
    """不显示选项的单路径解压窗口：打开即运行，例外才询问密码或报错。

    同样是**无边框 + 半透明底板**：右键进入的窗口与主界面必须是一套外观，
    否则用户会觉得从右键打开的是"另一个软件"。
    """

    def __init__(self, paths: list[str], *, autostart: bool = True):
        super().__init__()
        self._paths = [str(Path(p)) for p in paths]
        self._thread: QThread | None = None
        self._worker: ExtractWorker | None = None
        self._cancel = threading.Event()
        self._report = None
        self._close_when_finished = False
        self._started_at: float | None = None
        self._stage = "准备中"
        self._last_percent: int | None = None
        self._current_name = ""
        self._password_prompts = 0
        self._password_path = ""
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_status)

        self.setWindowTitle("解压开镜")
        self.setMinimumWidth(460)
        self.resize(460, 190)
        self._build_ui()
        if autostart:
            QTimer.singleShot(0, self._start)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(10)

        # 标题右侧挂窗口控制按钮：无边框窗口没有系统标题栏，不给关闭按钮
        # 这个窗口就关不掉。尺寸固定的紧凑窗口只给关闭 —— 最小化只会让它
        # 消失、反而找不回来（与设置/密码库/工作目录三个子窗口同一取舍）。
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        title = QLabel("正在自动解压")
        title.setStyleSheet(
            f"font-size: 16px; font-weight: 600; background: transparent;"
            f"color: {theme.TEXT};")
        head.addWidget(title)
        head.addStretch(1)
        head.addWidget(self.build_window_buttons(
            minimize=False, maximize=False, box=24, icon=12))
        layout.addLayout(head)

        self.path_label = QLabel("\n".join(Path(p).name for p in self._paths) or "未选择文件")
        self.path_label.setWordWrap(True)
        self.path_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; background: transparent;")
        layout.addWidget(self.path_label)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)
        self.status_label = QLabel("准备中…")
        self.status_label.setStyleSheet(f"color: {theme.TEXT_MUTED}; background: transparent;")
        layout.addWidget(self.status_label)

        self.password_panel = QWidget(self)
        password_layout = QVBoxLayout(self.password_panel)
        password_layout.setContentsMargins(0, 2, 0, 2)
        password_layout.setSpacing(8)
        self.password_hint = QLabel("请输入压缩包密码：")
        password_layout.addWidget(self.password_hint)
        password_row = QHBoxLayout()
        self.password_input = QLineEdit()
        self.password_input.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_input.setPlaceholderText("输入密码")
        self.password_input.returnPressed.connect(self._submit_password)
        password_row.addWidget(self.password_input, 1)
        self.password_button = QPushButton("确认密码")
        self.password_button.clicked.connect(self._submit_password)
        password_row.addWidget(self.password_button)
        self.skip_button = QPushButton("跳过此包")
        self.skip_button.clicked.connect(self._skip_password)
        password_row.addWidget(self.skip_button)
        password_layout.addLayout(password_row)
        self.password_panel.hide()
        layout.addWidget(self.password_panel)

        row = QHBoxLayout()
        row.addStretch(1)
        self.open_button = QPushButton("打开结果目录")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_results)
        row.addWidget(self.open_button)
        self.cancel_button = QPushButton("取消")
        self.cancel_button.clicked.connect(self._request_cancel)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)

        # 玻璃底板铺满整窗 —— 必须在所有子控件加完之后：它要 lower() 到最底，
        # 而 resizeEvent 又依赖它已经存在。
        #
        # `edge_margin=0`：这是个尺寸固定的紧凑窗口（460×190，本身低于四边缩放
        # 的最小尺寸），留缩放热区只会让边缘的拖拽被当成缩放、结果什么也缩不动。
        self.init_glass(self, edge_margin=0)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 底板形态必须等 show 之后确认：`enable_glass` 拿 winId 才谈得上系统模糊
        # （当前 `USE_NATIVE_BLUR=False`，它什么都不做；留着是为了开关一旦打开，
        # 这个窗口不用再改）。
        self.enable_glass()

    def _make_cfg(self) -> Config:
        pref = AppConfig.ensure(CONFIG_PATH)
        return Config.create(**pref.as_overrides(PROJECT_ROOT))

    def _start(self) -> None:
        if self._thread is not None:
            return
        if not self._paths:
            self._show_error("没有收到要解压的文件或文件夹。")
            return
        try:
            cfg = self._make_cfg()
        except (FileNotFoundError, OSError) as exc:
            self._show_error(str(exc))
            return
        cfg.workdir.mkdir(parents=True, exist_ok=True)
        self._started_at = time.monotonic()
        self._status_timer.start()
        self._thread = QThread()
        self._worker = ExtractWorker(
            cfg,
            DEFAULT_DB,
            self._paths,
            cancel=self._cancel,
            # 右键入口固定交付到输入文件所在目录，不读取主界面的输出偏好。
            output_mode=OUTPUT_SAMEDIR,
            subdir_name="",
            copy_back=False,
        )
        self._worker.moveToThread(self._thread)
        self._thread.started.connect(self._worker.run)
        self._worker.task_event.connect(self._on_event)
        self._worker.need_password.connect(self._on_need_password)
        self._worker.finished_ok.connect(self._on_finished)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished_ok.connect(self._thread.quit)
        self._worker.failed.connect(self._thread.quit)
        self._thread.finished.connect(self._on_thread_finished)
        self._thread.start()

    def _on_event(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == "task":
            self._current_name = Path(event.get("path", "")).name
            self.path_label.setText(self._current_name)
            self._set_stage("正在检查压缩包")
        elif kind == "info":
            format_name = str(event.get("format") or "压缩包")
            self._set_stage(f"已识别 {format_name}，正在准备解压")
        elif kind == "status":
            status = event.get("status")
            if status == "probing":
                self._set_stage("正在检查压缩包内容")
            elif status == "extracting":
                # 7-Zip 对某些格式会在准备阶段很久才吐出第一个百分比；保持
                # 不确定进度条并按秒刷新状态，避免 0% 看起来像窗口卡住。
                self._last_percent = None
                self.progress.setRange(0, 0)
                self._set_stage("正在解压")
        elif kind == "progress":
            self._last_percent = max(0, min(100, int(event.get("percent", 0))))
            self.progress.setRange(0, 100)
            self.progress.setValue(self._last_percent)
            self._refresh_status()
        elif kind == "phase":
            if event.get("path"):
                self.path_label.setText(Path(event["path"]).name)
            self._last_percent = None
            self.progress.setRange(0, 100)
            self.progress.setValue(int(event.get("percent", 0)))
            self._set_stage(event.get("message", "正在准备压缩包"))

    def _set_stage(self, stage: str) -> None:
        self._stage = stage
        self._refresh_status()

    def _refresh_status(self) -> None:
        """用计时器给 7-Zip 的无百分比准备阶段持续提供可见反馈。"""
        elapsed = ""
        if self._started_at is not None:
            seconds = max(0, int(time.monotonic() - self._started_at))
            elapsed = f"（已用时 {seconds // 60}:{seconds % 60:02d}）"
        if self._stage == "正在解压" and self._last_percent is not None:
            text = f"正在解压… {self._last_percent}%{elapsed}"
        elif self._stage == "正在解压":
            text = f"正在解压，等待进度反馈…{elapsed}"
        else:
            text = f"{self._stage}…{elapsed}"
        self.status_label.setText(text)

    def _on_need_password(self, info: dict) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._last_percent = None
        password_path = str(info.get("path", ""))
        if password_path != self._password_path:
            self._password_path = password_path
            self._password_prompts = 0
        self._password_prompts += 1
        self._current_name = Path(password_path).name
        self.path_label.setText(self._current_name)
        self.password_hint.setText(
            "自动密码未匹配，请输入密码：" if self._password_prompts == 1
            else "密码不正确，请重新输入："
        )
        self.password_input.clear()
        self.password_panel.show()
        self.adjustSize()
        self.password_input.setFocus(Qt.FocusReason.OtherFocusReason)
        self._set_stage("等待输入密码")

    def _submit_password(self) -> None:
        pwd = self.password_input.text().strip()
        if not pwd:
            self.password_hint.setText("请先输入密码，或点击“跳过此包”。")
            self.password_input.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        self.password_panel.hide()
        self.password_input.clear()
        self.adjustSize()
        if self._worker is not None:
            self._set_stage("正在验证输入的密码")
            self._worker.submit_password(pwd)

    def _skip_password(self) -> None:
        self.password_panel.hide()
        self.password_input.clear()
        self.adjustSize()
        self._set_stage("已跳过当前压缩包")
        if self._worker is not None:
            self._worker.submit_password(None)

    def _on_finished(self, report) -> None:
        self._report = report
        self._status_timer.stop()
        self.password_panel.hide()
        self.cancel_button.setText("关闭")
        self.cancel_button.clicked.disconnect()
        self.cancel_button.clicked.connect(self.close)
        if report.failed or report.delivery_failed:
            self.status_label.setText("解压失败")
            problems = [t.error or t.delivery_error or Path(t.archive_path).name
                        for t in report.failed_tasks]
            if not problems:
                problems = list(report.warnings) or ["解压过程出现错误。"]
            self._show_error("\n".join(problems[:3]))
        elif report.needs_password:
            self.status_label.setText("未提供密码，已跳过")
            QMessageBox.warning(self, "需要密码", "未输入有效密码，压缩包尚未解压。")
        elif report.output_dirs:
            self.progress.setRange(0, 100)
            self.progress.setValue(100)
            self.status_label.setText(f"解压完成，已输出 {len(report.output_dirs)} 个结果目录")
            self.open_button.setEnabled(True)
            self._maybe_open_results()
        elif report.skipped:
            self.status_label.setText("此前已解压，无需重复处理")
        else:
            self.status_label.setText("未找到可解压的压缩包")
            QMessageBox.warning(self, "未找到压缩包", "所选位置没有可处理的压缩包。")

    def _on_failed(self, message: str) -> None:
        self._status_timer.stop()
        self.password_panel.hide()
        self.cancel_button.setText("关闭")
        self._show_error(message)

    def _show_error(self, message: str) -> None:
        QMessageBox.critical(self, "解压开镜", message)

    def _open_results(self, limit: int | None = None) -> int:
        """打开结果目录，返回**实际打开**的目录数。

        返回值是给 `_maybe_open_results` 判断"到底开成了没有"用的：偏好开着、
        但目录一个都不存在（交付失败、被手动挪走）时绝不能顺手把窗口关掉 ——
        那会让用户连"产物在哪"这句提示都看不到。
        `limit` 用于自动打开时的抑制（见 `_maybe_open_results`）。
        """
        if self._report is None:
            return 0
        dirs = [Path(raw) for raw in (self._report.output_dirs or [])]
        dirs = [d for d in dirs if d.is_dir()]
        if limit is not None:
            dirs = dirs[:limit]
        opened = 0
        for path in dirs:
            try:
                os.startfile(str(path))  # noqa: S606 Windows Explorer 入口
            except OSError:
                continue
            opened += 1
        return opened

    def _maybe_open_results(self) -> None:
        """按偏好决定是否**自动**打开结果目录，并按偏好让本窗退场。

        偏好是 `open_after`，与主窗口共用同一个开关 —— 否则用户会发现
        "主窗口里关掉了、右键进来还是照开"。右键进入的多是"解完就想看"的
        场景，所以默认值是打开的；关掉时这里必须什么都不做。

        「打开隔离工作目录」那一档是 workdir 时代的旧行为，右键窗口没有
        workdir 视图，按"打开产物目录"处理 —— 那才是用户此刻想去的地方。

        `close_quick_after_open`（设置 → 解压行为）是右键窗口独有的后续动作：
        目录**真的打开之后**把本窗收掉。它挂在"打开成功"这个前提上，而不是挂在
        "解压完成"上 —— 没打开目录就把窗口关掉，用户会既看不到产物位置、也没
        「打开结果目录」可点。
        """
        if self._report is None or not self._report.output_dirs:
            return
        try:
            pref = AppConfig.ensure(CONFIG_PATH)
        except Exception:
            return                  # 偏好读不出来就按"不自动开"，别擅自弹窗
        if pref.open_after == OPEN_NONE:
            return
        # 与主窗口同一策略：一次解出十几个目录时全开会淹没桌面，超过 5 个只开第一个。
        opened = self._open_results(limit=1 if len(self._report.output_dirs) > 5 else None)
        if opened and pref.close_quick_after_open:
            self._schedule_close_after_open()

    def _schedule_close_after_open(self) -> None:
        """把退场排在资源管理器露头之后（延迟值见 `_CLOSE_AFTER_OPEN_DELAY_MS`）。"""
        QTimer.singleShot(_CLOSE_AFTER_OPEN_DELAY_MS, self._close_when_done)

    def _close_when_done(self) -> None:
        """关窗：线程还在收尾就交给 `_on_thread_finished`，不重复走取消流程。"""
        self._close_when_finished = True
        if self._thread is None or not self._thread.isRunning():
            self.close()

    def _request_cancel(self) -> None:
        if self._thread is None or not self._thread.isRunning():
            self.close()
            return
        self._close_when_finished = True
        self.cancel_button.setEnabled(False)
        self.password_panel.hide()
        self._set_stage("正在取消")
        self._cancel.set()
        if self._worker is not None:
            self._worker.submit_password(None)

    def _on_thread_finished(self) -> None:
        self._status_timer.stop()
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None
        if self._close_when_finished:
            self.close()

    def closeEvent(self, event) -> None:
        if self._thread is not None and self._thread.isRunning():
            self._request_cancel()
            event.ignore()
            return
        event.accept()
