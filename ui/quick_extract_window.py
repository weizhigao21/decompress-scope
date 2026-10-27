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
from ui.worker import CloudQueryJob, ExtractWorker

PROJECT_ROOT = runtime_data_root(Path(__file__).resolve().parent.parent)
DEFAULT_DB = PROJECT_ROOT / "data" / "jieya.db"
CONFIG_PATH = PROJECT_ROOT / "config.json"

# 自动打开结果目录之后，本窗退场的延迟。不立刻 close()：那样前台焦点会先回到
# 此前的窗口，而资源管理器刚被拉起、还没来得及露头 —— 用户看到的是"窗口没了、
# 目录也没打开"。留一点时间让它先出现，再让本窗消失。
_CLOSE_AFTER_OPEN_DELAY_MS = 400

# 手动云解密的轮询间隔。后台线程只写结果、主线程来读：不做跨线程的 Qt 调用
# （那会让 PySide6 6.12 随机 abort 整个进程，详见 ui/worker.py::CloudQueryJob）。
# 150 ms 对"算完 MD5 + 发一个请求"这种秒级任务足够跟手，也不至于空转。
_CLOUD_POLL_MS = 150


class QuickExtractWindow(GlassWindowMixin, QDialog):
    """不显示选项的单路径解压窗口：打开即运行，例外才报错。

    密码输入区**常驻**：不必等后台把候选密码试完才亮出来。用户先手填好的密码会
    被 pipeline 在下一轮候选前插到最前面试（core 的 `password_ready` 探针）；
    用户不填就照旧自动匹配，matched 就直接解压。

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
        # pipeline 是否正阻塞在"现场问密码"上（need_password 已发、还没收到回答）。
        # 只有它为真时「跳过此包」才有意义 —— pipeline 自己还在试候选的时候，
        # 没有什么可跳过的（要停就点「取消」）。
        self._waiting_for_pipeline = False
        # 实际生效的密码（状态事件带回来的）。完成时显示出来：
        # 用户最想知道的就是"这个包最后是用什么密码解开的"。
        self._used_password = ""
        # 乱码文件名修复的结论（notice 事件带回来的），完成时一并显示
        self._repair_note = ""
        # 同一件事的完整版（含编码与例子）。状态栏一行放不下，只挂 tooltip：
        # 用户想知道"到底把什么改成了什么"时，得有个地方能看。
        self._repair_detail = ""
        # 手动「云端取密码」：后台线程跑一次查询，主线程定时轮询结果再上界面
        # （不跨线程发 Qt 信号，理由见 ui/worker.py::CloudQueryJob）。
        # 与 pipeline 里的自动云解密互不相干 —— 这里点了就查，不看偏好开关。
        self._cloud_job: CloudQueryJob | None = None
        self._cloud_timer = QTimer(self)
        self._cloud_timer.setInterval(_CLOUD_POLL_MS)
        self._cloud_timer.timeout.connect(self._poll_cloud)
        self._status_timer = QTimer(self)
        self._status_timer.setInterval(1000)
        self._status_timer.timeout.connect(self._refresh_status)

        self.setWindowTitle("解压开镜")
        # 密码行现在有四个控件（输入框 + 确认 + 云端 + 跳过），460 会把输入框
        # 挤成一条缝。加宽到 520，其余尺寸仍按内容自适应。
        self.setMinimumWidth(520)
        self._build_ui()
        # 密码输入区是**常驻**的：窗口按内容高度走，不再按"面板藏起来"的 190 算，
        # 否则输入框会被裁掉一截。
        self.adjustSize()
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

        # 密码输入区**常驻**：不必等后台把候选密码试完才亮出来。用户手里攥着密码时
        # （站点公告、封面注释、前人经验），打开窗口就能直接填，填完 pipeline 会在
        # 下一轮候选前把它插到最前面试（见 core 的 password_ready 探针）。
        # 留空也行 —— 自动匹配照常跑， matched 就直接解压，不用你管。
        self.password_panel = QWidget(self)
        password_layout = QVBoxLayout(self.password_panel)
        password_layout.setContentsMargins(0, 2, 0, 2)
        password_layout.setSpacing(8)
        self.password_hint = QLabel("已知密码可直接输入（留空 = 自动尝试）：")
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
        # 手动云解密：pipeline 的自动云查询要等本地候选全失败（7z-AES 每候选
        # 约 0.3 s），用户点这个按钮可以立刻按 MD5 去要密码。
        self.cloud_button = QPushButton("云端取密码")
        self.cloud_button.setToolTip(
            "按文件 MD5 向云端密码库要这个包的密码：\n"
            "· 不需要等本地候选试完，点了就查；\n"
            "· 要算一遍整个文件的校验值，大包会先等几秒；\n"
            "· 会消耗一次云端查询次数（与自动云解密共用额度）。")
        self.cloud_button.clicked.connect(self._query_cloud)
        password_row.addWidget(self.cloud_button)
        self.skip_button = QPushButton("跳过此包")
        self.skip_button.setEnabled(False)   # 只有 pipeline 开口问时才可跳
        self.skip_button.setToolTip(
            "等自动密码试完、程序开口问的时候才需要跳过；想整个停掉请点「取消」")
        self.skip_button.clicked.connect(self._skip_password)
        password_row.addWidget(self.skip_button)
        password_layout.addLayout(password_row)
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
        elif kind == "notice":
            # pipeline 的补充说明（目前只有"已修正 N 个乱码文件名"这一类）。
            # 记下来而不是立刻显示：这次事件发生在解压收尾阶段，紧接着就会
            # 来 status=done，直接 setText 会被下一句覆盖掉，用户根本读不到。
            text = str(event.get("message") or "").strip()
            if text:
                self._repair_note = text
                self._repair_detail = str(event.get("detail") or text)
                self._set_stage(text)
        elif kind == "status":
            status = event.get("status")
            if event.get("password_used"):
                # 云端/本地命中后 pipeline 会说用的是哪个密码：记下来，
                # 完成时摆在状态栏里（用户最常问的一句就是"到底用的什么密码"）。
                self._used_password = str(event["password_used"])
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
        """pipeline 现场问密码：候选全没命中，轮到用户。

        输入区本来就是亮的，这里只负责把状态切到"等回答"：换提示文案、聚焦、
        放开「跳过此包」。
        """
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
        # pipeline 会在 info 里带上最近一次云端结论（没启用/没查过则为空），
        # 让用户知道"自动的、云端的都试过了"，而不是只看到"请输入密码"。
        cloud_note = str(info.get("cloud") or "").strip()
        if self._password_prompts == 1:
            base, tail = "自动密码未匹配", "请输入密码"
        else:
            base, tail = "密码不正确", "请重新输入"
        hint = f"{cloud_note}。{base}，{tail}：" if cloud_note else f"{base}，{tail}："
        self.password_hint.setText(hint)
        self.password_input.clear()
        self._waiting_for_pipeline = True
        self.skip_button.setEnabled(True)
        self.password_input.setFocus(Qt.FocusReason.OtherFocusReason)
        self._set_stage("等待输入密码")

    def _submit_password(self) -> None:
        """提交密码。pipeline 问不问都收：问着就当场用，没问就算"先手输入"。"""
        pwd = self.password_input.text().strip()
        if not pwd:
            self.password_hint.setText(
                "请先输入密码，或点击“跳过此包”。" if self._waiting_for_pipeline
                else "请先输入密码。")
            self.password_input.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        # 输入区常驻：不清空、不收起，只把"已收到"说出来。清空输入框是给下一轮
        # （可能是这个包重问，也可能是下一个包）留的，用户一眼能看出该重新填了。
        self.password_input.clear()
        self.password_hint.setText("已收到，正在用这个密码解压…")
        self._waiting_for_pipeline = False
        self.skip_button.setEnabled(False)
        if self._worker is not None:
            # pipeline 正等着 → 当场解；还在试候选 → 进 worker 的待答队列，
            # 候选循环下一轮就收手用它（见 core 的 password_ready 探针）。
            self._set_stage("正在验证输入的密码")
            self._worker.submit_password(pwd)

    def _skip_password(self) -> None:
        """放弃当前这个包。只在 pipeline 开口问时可点（见 skip_button 的启用条件）。"""
        self.password_input.clear()
        self.password_hint.setText("已跳过当前压缩包")
        self._waiting_for_pipeline = False
        self.skip_button.setEnabled(False)
        self._set_stage("已跳过当前压缩包")
        if self._worker is not None:
            self._worker.submit_password(None)

    def _query_cloud(self) -> None:
        """手动向云端密码库要密码（后台线程，主线程轮询结果）。

        查谁：优先当前正在等密码的那个包（`_password_path`，pipeline 问过才有），
        否则第一个输入 —— 用户可能一开始就不想等，直接在满候选之前点它。
        """
        if self._cloud_job is not None and self._cloud_job.busy():
            return                       # 上一次查询还没回来
        target = self._password_path or (self._paths[0] if self._paths else "")
        if not target:
            return
        try:
            pref = AppConfig.ensure(CONFIG_PATH)
            udid = pref.cloud_unlock_udid
        except Exception:                # 偏好读不出来不影响查（走派生设备标识）
            udid = ""
        self.cloud_button.setEnabled(False)
        self.cloud_button.setText("查询中…")
        self.password_hint.setText("正在向云端密码库查询（要先把整个文件算一遍校验值）…")
        self._set_stage("正在向云端密码库查询")
        # 每次查询都用一个新的 job：上一个的线程可能刚结束，复用它会让
        # 「点第二次没反应」这种状态藏起来。
        self._cloud_job = CloudQueryJob(str(target), udid=udid)
        self._cloud_job.start()
        self._cloud_timer.start()

    def _poll_cloud(self) -> None:
        """主线程定时取结果。线程还在跑就等下一跳。"""
        job = self._cloud_job
        if job is None:
            self._cloud_timer.stop()
            return
        result = job.poll()
        if result is None:
            if not job.busy():
                # 线程已退但没留下结果：只可能是实现被改坏了，别让按钮永远转圈
                self._cloud_timer.stop()
                self.cloud_button.setEnabled(True)
                self.cloud_button.setText("云端取密码")
                self.password_hint.setText("云端查询没有返回结果")
            return
        self._cloud_timer.stop()
        self._apply_cloud_result(result)

    def _apply_cloud_result(self, result: dict) -> None:
        """云端结论上界面：命中就交给解压线程，没命中就把原话摆出来。"""
        result = result or {}
        password = str(result.get("password") or "").strip()
        message = str(result.get("message") or "").strip()
        self.cloud_button.setEnabled(True)
        self.cloud_button.setText("云端取密码")
        if not password:
            self.password_hint.setText(message or "云端密码库没有返回密码")
            self._set_stage("云端密码库未返回密码")
            return
        self.password_hint.setText(f"云端密码库命中：{password}，正在用它解压…")
        self._set_stage("正在用云端密码解压")
        if self._worker is None:
            # 解压线程已经收尾（没在等密码）：只把密码亮出来，别去戳已失效的对象
            return
        self._waiting_for_pipeline = False
        self.skip_button.setEnabled(False)
        self._worker.submit_password(password)

    def _on_finished(self, report) -> None:
        self._report = report
        self._status_timer.stop()
        self._cloud_timer.stop()
        # 跑完了：密码输入区没有存在意义，收起来（窗口也可能紧跟着自动退场）。
        self.password_panel.hide()
        self._waiting_for_pipeline = False
        self.skip_button.setEnabled(False)
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
            used = f"（密码：{self._used_password}）" if self._used_password else ""
            tail = f"；{self._repair_note}" if self._repair_note else ""
            self.status_label.setText(
                f"解压完成{used}，已输出 {len(report.output_dirs)} 个结果目录{tail}")
            # 一行放不下例子，挂 tooltip 兜住"改成了什么"这个问题
            self.status_label.setToolTip(self._repair_detail or "")
            self.open_button.setEnabled(True)
            self._maybe_open_results()
        elif report.skipped:
            self.status_label.setText("此前已解压，无需重复处理")
        else:
            self.status_label.setText("未找到可解压的压缩包")
            QMessageBox.warning(self, "未找到压缩包", "所选位置没有可处理的压缩包。")

    def _on_failed(self, message: str) -> None:
        self._status_timer.stop()
        self._cloud_timer.stop()
        self.password_panel.hide()
        self._waiting_for_pipeline = False
        self.skip_button.setEnabled(False)
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
        self._cloud_timer.stop()
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
