"""主窗口：极简高效的暗色界面。

设计要点：
- 三段式信息架构：顶部工具条 → 输入/概览 → 任务列表 → 底部状态条
- 低频参数（深度/上限/来源/开关）默认折叠，保持默认视图安静
- 底部折叠密码库，待密码任务可一键补录
- 全量操作走主题 QSS，颜色只从 ui.theme 取
- 用户偏好（输出落点/拖入即开始/完成后开目录）持久化到 config.json，
  主窗口只读不改，改由 ui.settings_window 负责
"""
from __future__ import annotations

import os
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QSize, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.appconfig import (
    AUTORUN_CONFIRM,
    AUTORUN_DIRECT,
    AUTORUN_OFF,
    OPEN_NONE,
    OPEN_PATHS,
    OPEN_WORKDIR,
    AppConfig,
)
from core.config import Config
from core.vault import PasswordVault
from ui import theme
from ui.settings_window import SettingsWindow
from ui.vault_window import VaultWindow
from ui.worker import ExtractWorker

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "jieya.db"
CONFIG_PATH = PROJECT_ROOT / "config.json"

STATUS_TEXT = {
    "pending": "等待",
    "probing": "探测中",
    "extracting": "解压中",
    "needs_password": "待密码",
    "done": "完成",
    "failed": "失败",
}

FILE_FILTER = "压缩包 (*.zip *.rar *.7z *.cbz *.cbr *.tar *.gz *.bz2 *.xz *.cab *.iso *.001);;所有文件 (*)"



def _sep() -> QFrame:
    """1px 分隔线（横向）。"""
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background-color: {theme.LINE_SOFT}; border: none;")
    return line


class InputList(QListWidget):
    """支持拖拽的输入路径列表；空态显示引导条目，避免大面积死区。"""

    EMPTY_HINT = "拖入压缩包或文件夹，也可以拖整个目录递归扫描"

    # 拖入完成后发出，参数是本次新增的真实路径。MainWindow 用它触发「拖入即开始」。
    dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setAlternatingRowColors(False)
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self._show_placeholder()

    def _show_placeholder(self) -> None:
        """空列表时插入一条不可选的引导项。"""
        self.clear()
        hint = QListWidgetItem(self.EMPTY_HINT)
        hint.setFlags(Qt.NoItemFlags)  # 不可选、不可拖，纯提示
        hint.setForeground(QColor(theme.TEXT_FAINT))
        self.addItem(hint)

    def _is_placeholder(self, item) -> bool:
        return item is not None and not (item.flags() & Qt.ItemIsSelectable)

    def real_paths(self) -> list[str]:
        return [
            self.item(i).text()
            for i in range(self.count())
            if not self._is_placeholder(self.item(i))
        ]

    def add_paths(self, paths) -> None:
        existing = set(self.real_paths())
        if existing == set() and self.count() and self._is_placeholder(self.item(0)):
            self.clear()
        for p in paths:
            if not p:
                continue
            path = Path(p)
            if path.exists() and str(path) not in existing:
                self.addItem(str(path))
                existing.add(str(path))
        if self.count() == 0:
            self._show_placeholder()

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        before = set(self.real_paths())
        self.add_paths(
            url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()
        )
        event.acceptProposedAction()
        added = [p for p in self.real_paths() if p not in before]
        if added:
            self.dropped.emit(added)


class StatCard(QFrame):
    """概览小卡：一个数字 + 一个标签。背景透明，融入所在面板。"""

    def __init__(self, label: str, color: str, parent=None):
        super().__init__(parent)
        self._color = color
        self.setFrameShape(QFrame.NoFrame)
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(2)

        self.value = QLabel("0")
        value_font = QFont()
        value_font.setPointSize(15)
        value_font.setWeight(QFont.DemiBold)
        self.value.setFont(value_font)
        self.value.setProperty("stat", "value")

        self.label = QLabel(label)
        self.label.setProperty("stat", "label")

        box.addWidget(self.value)
        box.addWidget(self.label)

    def set_value(self, n: int) -> None:
        self.value.setText(str(n))
        # 归零时降为弱色，有数据才点亮
        self.value.setStyleSheet(
            f"color: {self._color if n else theme.TEXT_FAINT}; background: transparent;")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("解压开镜")
        self.resize(1000, 680)
        self._thread: QThread | None = None
        self._worker: ExtractWorker | None = None
        self._cancel = threading.Event()
        self._task_items: dict[int, QTreeWidgetItem] = {}
        self._stats = {"pending": 0, "done": 0, "needs_password": 0, "failed": 0}
        self._vault_window: VaultWindow | None = None
        self._settings_window: SettingsWindow | None = None
        self._cfg = AppConfig.ensure(CONFIG_PATH)

        # 拖入即开始的合并窗口：连续拖入多个文件只启动一次
        self._autorun_timer = QTimer(self)
        self._autorun_timer.setSingleShot(True)
        self._autorun_timer.timeout.connect(self._autorun_fire)

        self._build_ui()
        self._apply_cfg_to_ui()
        self._refresh_vault_label()
        self._refresh_empty_state()
        self._restore_last_inputs()

    # ---------- 配置 ----------

    def _apply_cfg_to_ui(self) -> None:
        """把偏好映射到界面。开关控件对用户可见，但不在此处触发任何解压。

        折叠区的高级参数也从偏好初始化——它既是「本次覆盖」入口，也是偏好的
        可视化。若不同步，用户会看到与实际生效值不符的数字。
        """
        cfg = self._cfg
        self.auto_run.setChecked(cfg.autorun_mode != AUTORUN_OFF)
        self.auto_open.setChecked(cfg.open_after != OPEN_NONE)
        self.auto_open.setToolTip(
            "完成后打开解压结果所在目录" if cfg.open_after == OPEN_PATHS
            else "完成后打开隔离工作目录")

        self.depth_spin.setValue(cfg.max_depth)
        self.total_spin.setValue(cfg.max_total_gb)
        self.sniff.setChecked(cfg.sniff_archives)
        self.keep_intermediate.setChecked(not cfg.delete_intermediate)
        self.delete_original.setChecked(not cfg.keep_original)

    def _save_cfg(self) -> None:
        try:
            self._cfg.save(CONFIG_PATH)
        except OSError as exc:
            self.statusBar().showMessage(f"配置保存失败：{exc}", 8000)

    def _on_settings_saved(self, cfg: AppConfig) -> None:
        self._cfg = cfg
        self._apply_cfg_to_ui()
        self.statusBar().showMessage("设置已保存", 4000)

    def _restore_last_inputs(self) -> None:
        """启动时按偏好恢复上次输入。默认关闭——一开程序就自动解压很吓人。"""
        if not self._cfg.restore_last_inputs or not self._cfg.remember_inputs:
            return
        existing = [p for p in self._cfg.recent_inputs if Path(p).exists()]
        if existing:
            self.input_list.add_paths(existing)
            self._refresh_empty_state()

    def _open_settings_window(self) -> None:
        """设置窗口单例；沿用与密码库相同的 closed 信号清理手法。"""
        if self._settings_window is None:
            self._settings_window = SettingsWindow(
                self._cfg, CONFIG_PATH, PROJECT_ROOT, parent=self)
            self._settings_window.saved.connect(self._on_settings_saved)
            self._settings_window.closed.connect(self._on_settings_closed)
            self._settings_window.show()
        else:
            self._settings_window.show()
            self._settings_window.raise_()
            self._settings_window.activateWindow()

    def _on_settings_closed(self) -> None:
        self._settings_window = None


    # ---------- UI 构建 ----------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 12, 16, 12)
        root.setSpacing(10)

        root.addLayout(self._build_header())
        # 输入区固定高度：避免它跟任务树抢弹性空间
        input_area = self._build_input_area()
        input_area.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        root.addWidget(input_area)
        root.addWidget(self._build_action_bar())
        root.addWidget(self._build_params_panel())
        root.addWidget(self._build_tree(), stretch=1)
        root.addWidget(self._build_vault_panel())

        self.statusBar().showMessage("就绪")

    def _build_header(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(10)

        title = QLabel("解压开镜")
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setWeight(QFont.DemiBold)
        title.setFont(title_font)

        # 标题与副标题之间加固定间隔：两者字号相近，紧挨着会读成一句话
        self.subtitle = QLabel("拖入文件即可开始")
        self.subtitle.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; background: transparent;")

        sep = QFrame()
        sep.setFixedWidth(1)
        sep.setFixedHeight(13)
        sep.setStyleSheet(f"background-color: {theme.LINE}; border: none;")

        row.addWidget(title)
        row.addWidget(sep)
        row.addWidget(self.subtitle)
        row.addStretch(1)

        self.auto_run = QCheckBox("拖入即开始")
        self.auto_run.setToolTip("添加文件后自动启动解压（详细档位见「设置」）")
        self.auto_run.toggled.connect(self._on_autorun_toggled)
        row.addWidget(self.auto_run)

        self.auto_open = QCheckBox("完成后打开目录")
        self.auto_open.toggled.connect(self._on_autoopen_toggled)
        row.addWidget(self.auto_open)

        self.settings_btn = QPushButton("设置")
        self.settings_btn.setProperty("ghost", "true")
        self.settings_btn.setCursor(Qt.PointingHandCursor)
        self.settings_btn.setToolTip("输出位置、启动方式、解压参数")
        self.settings_btn.clicked.connect(self._open_settings_window)
        row.addWidget(self.settings_btn)
        return row

    def _on_autorun_toggled(self, on: bool) -> None:
        """工具条开关是快捷档位：勾选=直接开始，取消=关闭；细档位去设置里调。"""
        if on:
            if self._cfg.autorun_mode == AUTORUN_OFF:
                self._cfg = self._cfg.with_changes(autorun_mode=AUTORUN_DIRECT)
        else:
            self._cfg = self._cfg.with_changes(autorun_mode=AUTORUN_OFF)
        self._save_cfg()

    def _on_autoopen_toggled(self, on: bool) -> None:
        self._cfg = self._cfg.with_changes(open_after=OPEN_PATHS if on else OPEN_NONE)
        self._save_cfg()

    def _build_input_area(self) -> QWidget:
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        # 左：输入列表
        left = QFrame()
        left.setProperty("panel", "true")
        left_box = QVBoxLayout(left)
        left_box.setContentsMargins(12, 10, 12, 12)
        left_box.setSpacing(8)

        head = QHBoxLayout()
        self.input_title = QLabel("输入")
        self.input_title.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; background: transparent;")
        head.addWidget(self.input_title)
        head.addStretch(1)
        btn_add = QPushButton("添加")
        btn_remove = QPushButton("移除")
        btn_clear = QPushButton("清空")
        for b in (btn_add, btn_remove, btn_clear):
            b.setProperty("ghost", "true")
            head.addWidget(b)
        left_box.addLayout(head)

        self.input_list = InputList()
        self.input_list.setFixedHeight(108)
        self.input_list.dropped.connect(self._on_dropped)
        left_box.addWidget(self.input_list)

        btn_add.clicked.connect(self._pick_files)
        btn_remove.clicked.connect(self._remove_selected)
        btn_clear.clicked.connect(self._clear_inputs)

        # 右：概览
        right = QFrame()
        right.setProperty("panel", "true")
        right.setFixedWidth(240)
        right_box = QVBoxLayout(right)
        right_box.setContentsMargins(16, 12, 16, 12)
        right_box.setSpacing(10)

        overview_title = QLabel("本次任务")
        overview_title.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; background: transparent;")
        right_box.addWidget(overview_title)

        cards = QGridLayout()
        cards.setHorizontalSpacing(20)
        cards.setVerticalSpacing(6)
        self.card_pending = StatCard("待处理", theme.TEXT_MUTED)
        self.card_done = StatCard("已完成", theme.OK)
        self.card_needs = StatCard("待密码", theme.WARN)
        self.card_failed = StatCard("失败", theme.ERR)
        cards.addWidget(self.card_pending, 0, 0)
        cards.addWidget(self.card_done, 0, 1)
        cards.addWidget(self.card_needs, 1, 0)
        cards.addWidget(self.card_failed, 1, 1)
        right_box.addLayout(cards)
        right_box.addStretch(1)

        row.addWidget(left, stretch=1)
        row.addWidget(right)
        return wrapper

    def _build_action_bar(self) -> QWidget:
        wrapper = QWidget()
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(12)

        self.start_btn = QPushButton("开始解压")
        self.start_btn.setProperty("primary", "true")
        self.start_btn.setMinimumHeight(38)
        self.start_btn.setMinimumWidth(150)
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.clicked.connect(self._start)
        row.addWidget(self.start_btn)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(8)
        self.progress.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        row.addWidget(self.progress, stretch=1)

        self.progress_label = QLabel("")
        self.progress_label.setFixedWidth(44)
        self.progress_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.progress_label.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; background: transparent;")
        row.addWidget(self.progress_label)

        self.open_dir_btn = QPushButton("打开目录")
        self.open_dir_btn.setProperty("ghost", "true")
        self.open_dir_btn.clicked.connect(self._open_workdir)
        row.addWidget(self.open_dir_btn)
        return wrapper

    def _build_params_panel(self) -> QWidget:
        wrapper = QWidget()
        wrapper.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        box = QVBoxLayout(wrapper)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)

        self.params_toggle = QToolButton()
        self.params_toggle.setProperty("section", "true")
        self.params_toggle.setText("高级参数")
        self.params_toggle.setCheckable(True)
        self.params_toggle.setChecked(False)
        self.params_toggle.setCursor(Qt.PointingHandCursor)
        self.params_toggle.setToolTip("解压深度、大小上限、密码来源等默认值已够用")
        box.addWidget(self.params_toggle)

        self.params_body = QFrame()
        self.params_body.setProperty("panel", "true")
        self.params_body.setVisible(False)
        body = QHBoxLayout(self.params_body)
        body.setContentsMargins(14, 12, 14, 12)
        body.setSpacing(18)

        body.addWidget(self._labeled("密码来源", self._make_source_edit(), 0))
        body.addWidget(self._labeled("最大深度", self._make_depth_spin(), 0))
        body.addWidget(self._labeled("大小上限 GB", self._make_total_spin(), 0))

        checks = QVBoxLayout()
        checks.setSpacing(4)
        self.keep_intermediate = QCheckBox("保留中间层压缩包")
        self.delete_original = QCheckBox("完成后删除原件")
        self.sniff = QCheckBox("文件头嗅探伪装包")
        self.sniff.setChecked(True)
        self.sniff.setToolTip("扩展名不认识时读 magic bytes 判断是否为压缩包")
        checks.addWidget(self.keep_intermediate)
        checks.addWidget(self.delete_original)
        checks.addWidget(self.sniff)
        checks_wrap = QWidget()
        checks_wrap.setLayout(checks)
        body.addWidget(checks_wrap)
        body.addStretch(1)

        box.addWidget(self.params_body)
        self.params_toggle.toggled.connect(self._on_params_toggled)
        return wrapper

    def _labeled(self, text: str, widget: QWidget, width: int) -> QWidget:
        wrap = QWidget()
        col = QVBoxLayout(wrap)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(4)
        label = QLabel(text)
        label.setStyleSheet(f"color: {theme.TEXT_FAINT}; background: transparent; font-size: 12px;")
        col.addWidget(label)
        if width:
            widget.setFixedWidth(width)
        col.addWidget(widget)
        return wrap

    def _make_source_edit(self) -> QLineEdit:
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("留空则从文件名识别")
        self.source_edit.setMinimumWidth(180)
        return self.source_edit

    def _make_depth_spin(self) -> QSpinBox:
        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(1, 10)
        self.depth_spin.setValue(5)
        self.depth_spin.setFixedWidth(70)
        return self.depth_spin

    def _make_total_spin(self) -> QSpinBox:
        self.total_spin = QSpinBox()
        self.total_spin.setRange(1, 2000)
        self.total_spin.setValue(50)
        self.total_spin.setFixedWidth(90)
        self.total_spin.setToolTip("解压后总大小超过此值将拒绝（防 zip 炸弹）")
        return self.total_spin

    def _build_tree(self) -> QWidget:
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["压缩包", "状态", "密码", "进度", "信息"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setMinimumHeight(150)
        self.tree.setColumnWidth(0, 380)
        self.tree.setColumnWidth(1, 84)
        self.tree.setColumnWidth(2, 130)
        self.tree.setColumnWidth(3, 70)
        self.tree.header().setStretchLastSection(True)
        self.tree.setStyleSheet(
            f"QTreeWidget::item {{ height: 30px; }}"
            f"QTreeWidget::branch {{ background: transparent; }}"
        )
        return self.tree

    def _build_vault_panel(self) -> QWidget:
        """密码库入口：一行 = 标题 + 计数 + 「打开密码库」按钮。

        完整增删改查迁移到独立窗口（VaultWindow），主窗口只保留轻量入口。
        """
        wrapper = QWidget()
        wrapper.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        title = QLabel("密码库")
        title.setStyleSheet(f"color: {theme.TEXT}; background: transparent; font-weight: 500;")
        row.addWidget(title)

        self.vault_label = QLabel("0 条")
        self.vault_label.setStyleSheet(f"color: {theme.TEXT_FAINT}; background: transparent;")
        row.addWidget(self.vault_label)

        row.addStretch(1)

        self.vault_open_btn = QPushButton("打开密码库")
        self.vault_open_btn.setProperty("ghost", "true")
        self.vault_open_btn.setCursor(Qt.PointingHandCursor)
        self.vault_open_btn.clicked.connect(self._open_vault_window)
        row.addWidget(self.vault_open_btn)
        return wrapper

    # ---------- 折叠交互 ----------

    def _on_params_toggled(self, on: bool) -> None:
        self.params_body.setVisible(on)
        self.params_toggle.setText(("收起高级参数" if on else "高级参数"))

    # ---------- 输入管理 ----------

    def _pick_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "选择压缩包", "", FILE_FILTER)
        if files:
            self.input_list.add_paths(files)
            self._remember(files)
            self._refresh_empty_state()
            self._maybe_autorun()

    def _pick_dir(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if d:
            self.input_list.add_paths([d])
            self._remember([d])
            self._refresh_empty_state()
            self._maybe_autorun()

    def _remove_selected(self) -> None:
        for item in self.input_list.selectedItems():
            if not self.input_list._is_placeholder(item):
                self.input_list.takeItem(self.input_list.row(item))
        self._refresh_empty_state()

    def _clear_inputs(self) -> None:
        self.input_list.clear()
        self._refresh_empty_state()

    def _maybe_autorun(self) -> None:
        """拖入即开始：开一个合并窗口，连续拖入只启动一次。

        直接启动会踩两个坑：一是拖 5 个文件触发 5 次 run，二是拖入瞬间用户
        可能还在继续拖。延迟由 autorun_delay_ms 控制，重入即重置计时器。
        """
        if self._cfg.autorun_mode == AUTORUN_OFF or self._thread is not None:
            return
        if not self.input_list.real_paths():
            return
        self._autorun_timer.start(max(0, self._cfg.autorun_delay_ms))

    def _autorun_fire(self) -> None:
        """合并窗口到点：确认真实输入后启动。"""
        if self._thread is not None:
            return
        inputs = self.input_list.real_paths()
        if not inputs:
            return
        if self._cfg.autorun_mode == AUTORUN_CONFIRM:
            ret = QMessageBox.question(
                self, "开始解压",
                f"检测到 {len(inputs)} 个输入，现在开始解压吗？",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                return
        self._start()

    def _on_dropped(self, paths: list) -> None:
        """拖入完成：记历史 + 刷新空态 + 触发（可能是延迟的）自动启动。"""
        self._remember(paths)
        self._refresh_empty_state()
        self._maybe_autorun()

    def _remember(self, paths: list) -> None:
        """把输入并入历史（供「最近输入」用），并按需落盘。"""
        if not self._cfg.remember_inputs or not paths:
            return
        before = list(self._cfg.recent_inputs)
        self._cfg.note_inputs([str(p) for p in paths])
        if self._cfg.recent_inputs != before:
            self._save_cfg()

    def _refresh_empty_state(self) -> None:
        """空列表时恢复引导项；标题随输入数量变化。"""
        if self.input_list.count() == 0:
            self.input_list._show_placeholder()
        n = len(self.input_list.real_paths())
        self.input_title.setText("输入" if n else "输入（拖拽到下方）")
        if n == 0 and self._thread is None:
            self.subtitle.setText("拖入文件即可开始")

    def _open_workdir(self) -> None:
        cfg = self._make_cfg()
        cfg.workdir.mkdir(parents=True, exist_ok=True)
        self._open_path(cfg.workdir, "无法打开目录")

    def _open_path(self, target: Path, err_title: str = "无法打开") -> None:
        """用系统文件管理器打开路径；失败只弹提示，不影响任务结果。"""
        try:
            os.startfile(str(target))  # noqa: S606 Windows 专用
        except OSError as exc:
            QMessageBox.warning(self, err_title, f"{target}\n\n{exc}")

    def _open_results(self, dirs: list[str]) -> None:
        """按偏好打开解压结果：samedir 模式下每个包一个目录，最多开前 5 个。

        开十几个窗口会淹没用户桌面，所以超过 5 个时只开第一个并在状态栏说明；
        用户想全看可以点「打开目录」。
        """
        existing: list[Path] = []
        seen: set[str] = set()
        for d in dirs:
            p = Path(d)
            key = str(p)
            if key in seen or not p.is_dir():
                continue
            seen.add(key)
            existing.append(p)
        if not existing:
            return
        for p in existing[:5]:
            self._open_path(p, "无法打开结果目录")
        if len(existing) > 5:
            self.statusBar().showMessage(
                f"已打开前 5 个结果目录（共 {len(existing)} 个）", 8000)

    def _make_cfg(self) -> Config:
        """单次运行参数：从偏好派生，再叠加折叠区里临时改过的高级参数。

        折叠区是「本次覆盖」，不写回配置——用户调完不用怕污染长期偏好。
        """
        overrides = self._cfg.as_overrides(PROJECT_ROOT)
        overrides["max_depth"] = self.depth_spin.value()
        overrides["max_total_uncompressed"] = self.total_spin.value() * (1024 ** 3)
        overrides["delete_intermediate"] = not self.keep_intermediate.isChecked()
        overrides["keep_original"] = not self.delete_original.isChecked()
        overrides["sniff_archives"] = self.sniff.isChecked()
        return Config.create(**overrides)

    # ---------- 运行 ----------

    def _start(self) -> None:
        if self._thread is not None:
            return
        inputs = self.input_list.real_paths()
        if not inputs:
            QMessageBox.information(self, "提示", "请先添加要解压的文件或文件夹")
            return
        self._task_items.clear()
        self.tree.clear()
        self._reset_stats()
        self.start_btn.setEnabled(False)
        self.start_btn.setText("解压中…")
        self.progress.setValue(0)
        self.progress_label.setText("")
        self.statusBar().showMessage("正在处理…")

        cfg = self._make_cfg()
        cfg.workdir.mkdir(parents=True, exist_ok=True)
        self._cancel.clear()
        self._thread = QThread()
        self._worker = ExtractWorker(cfg, DEFAULT_DB, inputs,
                                     source=self.source_edit.text().strip(),
                                     cancel=self._cancel,
                                     output_mode=self._cfg.output_mode,
                                     subdir_name=self._cfg.subdir_name,
                                     copy_back=self._cfg.copy_back_to_source)
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

    def _on_need_password(self, info: dict) -> None:
        """工作线程卡在密码问询上，这里弹窗收答案（须把控制权交回事件循环）。"""
        pwd, ok = QInputDialog.getText(
            self, "需要密码",
            f"压缩包：{info.get('path', '')}\n\n"
            f"自动尝试的密码都不匹配，请输入：\n（留空或取消 = 跳过该包）",
        )
        if self._worker is not None:
            self._worker.submit_password(pwd if ok else None)

    def _on_thread_finished(self) -> None:
        """线程真正退出后才清理对象，避免 'QThread: Destroyed while running'。"""
        if self._worker is not None:
            self._worker.deleteLater()
        if self._thread is not None:
            self._thread.deleteLater()
        self._worker = None
        self._thread = None

    # ---------- 事件桥接 ----------

    def _reset_stats(self) -> None:
        self._stats = {"pending": 0, "done": 0, "needs_password": 0, "failed": 0}
        self._render_stats()

    def _bump(self, status: str, delta: int) -> None:
        if status not in self._stats:
            return
        self._stats[status] = max(0, self._stats[status] + delta)
        self._render_stats()

    def _render_stats(self) -> None:
        self.card_pending.set_value(self._stats["pending"])
        self.card_done.set_value(self._stats["done"])
        self.card_needs.set_value(self._stats["needs_password"])
        self.card_failed.set_value(self._stats["failed"])

    def _on_event(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == "task":
            item = QTreeWidgetItem()
            item.setData(0, Qt.UserRole, event["task_id"])
            item.setText(0, Path(event["path"]).name)
            item.setToolTip(0, event["path"])
            item.setText(1, STATUS_TEXT["pending"])
            item.setForeground(1, QColor(theme.STATUS_COLORS["pending"]))
            depth = event.get("depth", 0)
            if depth:
                item.setText(4, f"嵌套第 {depth} 层")
            parent_item = self._task_items.get(event.get("parent_id"))
            if parent_item is None:
                self.tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
                parent_item.setExpanded(True)
            self._task_items[event["task_id"]] = item
            self._bump("pending", 1)
            self.tree.scrollToItem(item)
        elif kind == "status":
            item = self._task_items.get(event.get("task_id"))
            if item is None:
                return
            status = event.get("status", "")
            old = item.data(2, Qt.UserRole) or "pending"
            item.setText(1, STATUS_TEXT.get(status, status))
            color = theme.STATUS_COLORS.get(status)
            if color:
                item.setForeground(1, QColor(color))
            item.setData(2, Qt.UserRole, status)

            # 统计：老状态出队、新状态入队
            if old != status:
                self._bump(old, -1)
                self._bump(status, 1)

            pwd = event.get("password_used")
            if pwd:
                item.setText(2, pwd)
            err = event.get("error")
            if err:
                item.setText(4, err[:140])
                item.setToolTip(4, err)
            if status == "extracting":
                item.setText(3, "0%")
            elif status == "done":
                item.setText(3, "100%")
            elif status == "failed":
                item.setText(3, "—")
        elif kind == "progress":
            pct = max(0, min(100, int(event.get("percent", 0))))
            self.progress.setValue(pct)
            self.progress_label.setText(f"{pct}%" if pct else "")
            item = self._task_items.get(event.get("task_id"))
            if item is not None:
                item.setText(3, f"{pct}%")
        elif kind == "warning":
            self.statusBar().showMessage(f"警告：{event.get('message', '')}", 8000)

    def _on_finished(self, report) -> None:
        self.start_btn.setEnabled(True)
        self.start_btn.setText("开始解压")
        n = len(self.input_list.real_paths())
        self.subtitle.setText(f"{n} 个输入 · 成功 {report.done}")
        self.statusBar().showMessage(
            f"完成：成功 {report.done} | 失败 {report.failed} | 待密码 {report.needs_password}", 0)

        if report.output_dirs:
            self.progress_label.setText("")
            open_target = self._cfg.open_after
            if open_target == OPEN_PATHS:
                self._open_results(list(report.output_dirs))
            elif open_target == OPEN_WORKDIR:
                self._open_workdir()
            # OPEN_NONE：什么都不做（包括旧的「不管开不开都存一条」的隐式行为）
        if report.needs_password:
            names = "\n".join(t.archive_path for t in report.needs_password_tasks)
            QMessageBox.warning(
                self, "有任务需要密码",
                f"以下压缩包已跳过（密码不匹配）：\n{names}\n\n"
                f"可点右下角「打开密码库」补充密码后重新解压。",
            )
        if report.warnings:
            # 复制失败/同名避让这类非致命问题只提示，不打断
            self.statusBar().showMessage(f"注意：{report.warnings[0]}", 10000)
        self._refresh_vault_label()

    def _on_failed(self, message: str) -> None:
        self.start_btn.setEnabled(True)
        self.start_btn.setText("开始解压")
        self.subtitle.setText("运行出错")
        QMessageBox.critical(self, "错误", message)

    def closeEvent(self, event) -> None:
        if self._thread is not None and self._thread.isRunning():
            ret = QMessageBox.question(
                self, "退出确认",
                "解压仍在进行，确定退出吗？\n（当前正在处理的压缩包会先完成，剩余任务取消）",
                QMessageBox.Yes | QMessageBox.No,
            )
            if ret != QMessageBox.Yes:
                event.ignore()
                return
            self._cancel.set()
            if self._worker is not None:
                self._worker.submit_password(None)  # 若正卡在密码弹窗，先放行
            self._thread.quit()
            self._thread.wait()
        if self._vault_window is not None:
            self._vault_window.close()
            self._vault_window = None
        if self._settings_window is not None:
            self._settings_window.close()
            self._settings_window = None
        # 退出前把输入历史落盘，下次启动可复用
        self._remember(self.input_list.real_paths())
        event.accept()

    # ---------- 密码库 ----------

    def _open_vault_window(self) -> None:
        """打开密码库窗口（单例复用，非模态，不阻塞解压）。

        注意：close() 不会触发 QObject.destroyed，故通过 VaultWindow.closed
        信号在关闭时清空引用，避免复用已 close 的 sqlite 连接。
        """
        if self._vault_window is None:
            self._vault_window = VaultWindow(DEFAULT_DB, parent=self)
            self._vault_window.vault_changed.connect(self._refresh_vault_label)
            self._vault_window.closed.connect(self._on_vault_window_closed)
            self._vault_window.destroyed.connect(self._on_vault_window_destroyed)
            self._vault_window.show()
        else:
            self._vault_window.show()
            self._vault_window.raise_()
            self._vault_window.activateWindow()

    def _on_vault_window_closed(self) -> None:
        """用户点 X 关闭窗口：连接已在 closeEvent 中释放，这里置空引用以便下次重建。"""
        self._vault_window = None

    def _on_vault_window_destroyed(self, *_args) -> None:
        """析构兜底（deleteLater 时触发）。"""
        self._vault_window = None

    def _refresh_vault_label(self) -> None:
        vault = PasswordVault(DEFAULT_DB)
        try:
            count = vault.count()
        finally:
            vault.close()
        self.vault_label.setText(f"共 {count} 条")
