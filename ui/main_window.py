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

from core import __version__ as APP_VERSION
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
from core.formatting import human_count, human_size
from core.vault import PasswordVault
from core.workdir_cleanup import iter_task_dirs
from ui import theme
from ui.settings_window import SettingsWindow
from ui.vault_window import VaultWindow
from ui.worker import ExtractWorker
from ui.workdir_window import WorkdirWindow

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "data" / "jieya.db"
CONFIG_PATH = PROJECT_ROOT / "config.json"

# 任务树列号。用命名常量而不是散落的字面量：往中间插一列会把后面所有下标
# 平移，而"状态写成文本却落在密码列"这种错位编译期查不出来。
# 与 ui/vault_window.py 的 _COL_* 保持同一写法。
(_COL_NAME, _COL_TYPE, _COL_SIZE, _COL_STATUS,
 _COL_PWD, _COL_PROG, _COL_INFO) = range(7)

# 列宽：文件名占大头，信息列吃掉剩余（setStretchLastSection）
# 短列按"表头 / 内容最宽者 + 左右各 11px 内边距 + 一点余量"取值，
# 列之间才会留出均匀的呼吸，而不是有的列空一大片、有的列紧贴。
_COL_WIDTHS = ((_COL_NAME, 340), (_COL_TYPE, 72), (_COL_SIZE, 84),
               (_COL_STATUS, 80), (_COL_PWD, 118), (_COL_PROG, 72))

# 任务的当前状态另存一个 role：列 0 的 UserRole 已经放了 task_id
_STATUS_ROLE = Qt.UserRole + 1

STATUS_TEXT = {
    "pending": "等待",
    "probing": "探测中",
    "extracting": "解压中",
    "needs_password": "待密码",
    "done": "完成",
    "failed": "失败",
    # 不是 core 的 TaskStatus 值，而是「状态 done + delivery_error 非空」在界面上的
    # 呈现：解压成功了，但产物没送到该去的地方。列宽只有 80px，短词优先，
    # 完整原因在 tooltip 与信息列里。
    "done_undelivered": "未交付",
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

    def accept_urls(self, urls) -> list[str]:
        """把拖入的本地 URL 并入列表，返回本次新增的真实路径。

        主窗口与列表共用这一条通路——「哪些算新增」只在这里判一次，两处各写
        一份迟早会漂移。
        """
        before = set(self.real_paths())
        self.add_paths(u.toLocalFile() for u in urls if u.isLocalFile())
        return [p for p in self.real_paths() if p not in before]

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragMoveEvent(self, event) -> None:
        event.acceptProposedAction()

    def dropEvent(self, event) -> None:
        added = self.accept_urls(event.mimeData().urls())
        event.acceptProposedAction()
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
    DROP_HINT = "松开即可加入并开始解压"

    def __init__(self):
        super().__init__()
        self.setWindowTitle("解压开镜")
        self.resize(1000, 680)
        # 窗口整体是拖放目标：拖到任务树/空白区也能加入。
        # QMainWindow 的 acceptDrops 本就是 true，但真正生效还要靠下面自己实现
        # 的 dragEnterEvent/dropEvent——QWidget 默认实现会 ignore 掉拖放。
        self.setAcceptDrops(True)
        self._thread: QThread | None = None
        self._worker: ExtractWorker | None = None
        self._cancel = threading.Event()
        self._task_items: dict[int, QTreeWidgetItem] = {}
        self._stats = {"pending": 0, "done": 0, "needs_password": 0, "failed": 0}
        self._vault_window: VaultWindow | None = None
        self._settings_window: SettingsWindow | None = None
        self._workdir_window: WorkdirWindow | None = None
        self._cfg = AppConfig.ensure(CONFIG_PATH)

        # 拖入即开始的合并窗口：连续拖入多个文件只启动一次
        self._autorun_timer = QTimer(self)
        self._autorun_timer.setSingleShot(True)
        self._autorun_timer.timeout.connect(self._autorun_fire)

        self._build_ui()
        self._apply_cfg_to_ui()
        self._refresh_vault_label()
        self._refresh_workdir_label()
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
        root.addWidget(self._build_workdir_panel())
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

        # 版本号挂在名字右侧，弱色 + 小一号：它属于"这是哪个版本"的元信息，
        # 用户会专程来找，所以不放状态栏角落；但也不该跟名字抢注意力。
        # 取值只用 core.__version__ 一处，别在这里写死字符串（会跟 pyproject 漂移）。
        # 字号走 theme 的 QLabel[role="version"]：这里 setFont 会被全局
        # `QWidget { font-size: 13px; }` 覆盖，设了等于没设。
        self.version_label = QLabel(f"v{APP_VERSION}")
        self.version_label.setProperty("role", "version")
        self.version_label.setToolTip(f"解压开镜 {APP_VERSION}")

        title_row = QHBoxLayout()
        title_row.setSpacing(6)  # 比 row 的主间距紧：让版本号看起来是名字的附着物
        title_row.setContentsMargins(0, 0, 0, 0)
        title_row.addWidget(title)
        title_row.addWidget(self.version_label)

        # 标题与副标题之间加固定间隔：两者字号相近，紧挨着会读成一句话
        self.subtitle = QLabel("拖入文件即可开始")
        self.subtitle.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; background: transparent;")

        sep = QFrame()
        sep.setFixedWidth(1)
        sep.setFixedHeight(13)
        sep.setStyleSheet(f"background-color: {theme.LINE}; border: none;")

        row.addLayout(title_row)
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
        # QLineEdit 默认吃拖放（把文件路径当文本插入）。放行给窗口，
        # 否则拖到这一条上既不加入输入、又冒出一串乱码路径。
        self.source_edit.setAcceptDrops(False)
        return self.source_edit

    def _make_depth_spin(self) -> QSpinBox:
        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(1, 10)
        self.depth_spin.setValue(5)
        self.depth_spin.setFixedWidth(70)
        self.depth_spin.setAcceptDrops(False)
        return self.depth_spin

    def _make_total_spin(self) -> QSpinBox:
        self.total_spin = QSpinBox()
        self.total_spin.setRange(1, 2000)
        self.total_spin.setValue(50)
        self.total_spin.setFixedWidth(90)
        self.total_spin.setToolTip("解压后总大小超过此值将拒绝（防 zip 炸弹）")
        self.total_spin.setAcceptDrops(False)
        return self.total_spin

    def _build_tree(self) -> QWidget:
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(
            ["压缩包", "类型", "大小", "状态", "密码", "进度", "信息"])
        self.tree.setRootIsDecorated(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setMinimumHeight(150)
        for col, width in _COL_WIDTHS:
            self.tree.setColumnWidth(col, width)
        self.tree.header().setStretchLastSection(True)
        # 整表一律左对齐，且表头的对齐必须与内容一致。
        #
        # 曾经只把「大小」设成右对齐（数字列好扫位数），结果表头行里只有它贴右：
        # 实测它与左侧「类型」表头隔了 101px、与右侧「状态」只隔 22px，
        # 六个表头挤一坨又突然拉开——这就是"东倒西歪"的来源。而且同为数字的
        # 「进度」是左对齐，两个数字列一个贴左一个贴右，更加坐实了错乱感。
        # 一致（疏密均匀）比"数字右对齐"值钱：这几列的取值宽度差只有几像素，
        # 右对齐换不来可读性，却破坏了整行的左起视觉基线。
        self.tree.setStyleSheet(
            f"QTreeWidget::item {{ height: 30px; }}"
            f"QTreeWidget::branch {{ background: transparent; }}"
        )
        return self.tree

    def _build_workdir_panel(self) -> QWidget:
        """工作目录残留入口：一行 = 标题 + 残留项数 + 「查看/清理」按钮。

        为什么要在主界面常驻一行：工作目录里的东西**全都**是「解压成功」状态
        （库里 done、任务树显示"完成"），用户没有任何线索知道磁盘被吃了多少，
        也没有入口清掉。5.38 GB 就是这么静静躺着的。

        计数只用一层 iterdir，绝不递归统计大小——常驻控件不能为了一个数字去走
        几十万个文件，那样开窗就会卡住。真实占用在弹窗里点开才算。
        """
        wrapper = QWidget()
        wrapper.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
        row = QHBoxLayout(wrapper)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        title = QLabel("工作目录")
        title.setStyleSheet(f"color: {theme.TEXT}; background: transparent; font-weight: 500;")
        row.addWidget(title)

        self.workdir_label = QLabel("")
        self.workdir_label.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; background: transparent;")
        row.addWidget(self.workdir_label)

        row.addStretch(1)

        self.workdir_open_btn = QPushButton("查看/清理")
        self.workdir_open_btn.setProperty("ghost", "true")
        self.workdir_open_btn.setCursor(Qt.PointingHandCursor)
        self.workdir_open_btn.setToolTip("盘点隔离工作目录里的残留产物，确认后清理")
        self.workdir_open_btn.clicked.connect(self._open_workdir_window)
        row.addWidget(self.workdir_open_btn)
        return wrapper

    def _refresh_workdir_label(self) -> None:
        try:
            n = len(iter_task_dirs(self._cfg.resolve_workdir(PROJECT_ROOT)))
        except OSError:
            n = 0
        self.workdir_label.setText(f"{n} 项残留" if n else "无残留")
        color = theme.WARN if n else theme.TEXT_FAINT
        self.workdir_label.setStyleSheet(f"color: {color}; background: transparent;")

    def _open_workdir_window(self) -> None:
        """残留窗口单例；复用密码库那套 closed 信号清理引用。"""
        if self._workdir_window is None:
            self._workdir_window = WorkdirWindow(
                self._cfg.resolve_workdir(PROJECT_ROOT), DEFAULT_DB, parent=self)
            self._workdir_window.cleaned.connect(self._on_workdir_cleaned)
            self._workdir_window.closed.connect(self._on_workdir_closed)
            self._workdir_window.show()
        else:
            self._workdir_window.refresh()
            self._workdir_window.show()
            self._workdir_window.raise_()
            self._workdir_window.activateWindow()

    def _on_workdir_cleaned(self, freed: int) -> None:
        self._refresh_workdir_label()
        self.statusBar().showMessage(
            f"已清理工作目录残留，释放 {human_size(freed)}", 8000)

    def _on_workdir_closed(self) -> None:
        self._workdir_window = None
        self._refresh_workdir_label()

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

    # ---------- 拖放：窗口整体是拖放目标 ----------
    #
    # Qt 只把拖放交给「光标下第一个 acceptDrops 的控件」，找不到就沿父链上溯。
    # 子控件都不接受时最终落到主窗口，所以在这里实现一次即可覆盖整窗。
    # 提示走输入区标题而非状态栏——状态栏上可能正挂着解压进度，不能被拖放清掉。

    def dragEnterEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
            self.input_title.setText(self.DROP_HINT)
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dragLeaveEvent(self, event) -> None:
        self._refresh_empty_state()  # 标题交回状态函数，避免和真实输入数量脱节
        event.accept()

    def dropEvent(self, event) -> None:
        added = self.input_list.accept_urls(event.mimeData().urls())
        event.acceptProposedAction()
        if added:
            self._on_dropped(added)

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
            item.setData(_COL_NAME, Qt.UserRole, event["task_id"])
            item.setText(_COL_NAME, Path(event["path"]).name)
            item.setToolTip(_COL_NAME, event["path"])
            item.setText(_COL_STATUS, STATUS_TEXT["pending"])
            item.setForeground(_COL_STATUS, QColor(theme.STATUS_COLORS["pending"]))
            # 包体信息要等探测完才知道。先摆占位符：空白单元格让人分不清
            # "还没探到"和"这行根本没这两项"，"—"则明确表示未知。
            for col in (_COL_TYPE, _COL_SIZE):
                item.setText(col, "—")
                item.setForeground(col, QColor(theme.TEXT_FAINT))
            depth = event.get("depth", 0)
            if depth:
                item.setText(_COL_INFO, f"嵌套第 {depth} 层")
            parent_item = self._task_items.get(event.get("parent_id"))
            if parent_item is None:
                self.tree.addTopLevelItem(item)
            else:
                parent_item.addChild(item)
                parent_item.setExpanded(True)
            self._task_items[event["task_id"]] = item
            self._bump("pending", 1)
            self.tree.scrollToItem(item)
        elif kind == "info":
            self._apply_archive_info(event)
        elif kind == "status":
            item = self._task_items.get(event.get("task_id"))
            if item is None:
                return
            status = event.get("status", "")
            delivery_error = event.get("delivery_error") or ""
            old = item.data(_COL_STATUS, _STATUS_ROLE) or "pending"
            # 交付失败要抢在状态之前显示：状态是 done（解压确实成功了），
            # 但对用户而言"东西没到我手上"才是这条记录的真实含义。
            #
            # 注意 role 里存的是**真实状态**（status），不是呈现态（shown）：
            # 统计与状态迁移都只认真实状态。否则 done → 未交付 会被记成
            # "done 减一"，四张统计卡的总和对不上任务树里的行数。
            shown = "done_undelivered" if (status == "done" and delivery_error) else status
            item.setText(_COL_STATUS, STATUS_TEXT.get(shown, status))
            color = theme.status_color(shown)
            if color:
                item.setForeground(_COL_STATUS, QColor(color))
            item.setData(_COL_STATUS, _STATUS_ROLE, status)

            # 统计：老状态出队、新状态入队
            if old != status:
                self._bump(old, -1)
                self._bump(status, 1)

            pwd = event.get("password_used")
            if pwd:
                item.setText(_COL_PWD, pwd)
            err = event.get("error")
            if delivery_error:
                # 交付原因放信息列 + tooltip：它是唯一能指向"我上次那 5GB 去哪了"
                # 的线索，不能只留在本次运行的 warnings 里随窗口一起消失。
                item.setText(_COL_INFO, delivery_error[:140])
                item.setToolTip(_COL_INFO, delivery_error)
                item.setToolTip(_COL_STATUS, delivery_error)
            elif err:
                item.setText(_COL_INFO, err[:140])
                item.setToolTip(_COL_INFO, err)
            if status == "extracting":
                item.setText(_COL_PROG, "0%")
            elif status == "done":
                item.setText(_COL_PROG, "—" if delivery_error else "100%")
            elif status == "failed":
                item.setText(_COL_PROG, "—")
        elif kind == "progress":
            pct = max(0, min(100, int(event.get("percent", 0))))
            self.progress.setValue(pct)
            self.progress_label.setText(f"{pct}%" if pct else "")
            item = self._task_items.get(event.get("task_id"))
            if item is not None:
                item.setText(_COL_PROG, f"{pct}%")
        elif kind == "warning":
            self.statusBar().showMessage(f"警告：{event.get('message', '')}", 8000)

    def _apply_archive_info(self, event: dict) -> None:
        """把探测到的包体信息写进「类型 / 大小」两列。

        加密封包头（-mhe=on）拿不到 7z 的 `Type`，此时 core 已用 magic 兜底；
        真兜不出来才显示 "—"，不编造。
        """
        item = self._task_items.get(event.get("task_id"))
        if item is None:
            return  # 任务已不在树上（如新一轮已清空），静默丢弃

        fmt = (event.get("format") or "").strip()
        encrypted = bool(event.get("encrypted"))
        item.setText(_COL_TYPE, fmt or "—")
        # 加密包的类型用警示色标出——扫一眼就知道这行需要密码
        item.setForeground(_COL_TYPE, QColor(theme.WARN if encrypted else theme.TEXT_MUTED))
        item.setToolTip(
            _COL_TYPE, f"格式：{fmt or '未知'}" + ("　已加密" if encrypted else ""))

        item.setText(_COL_SIZE, human_size(event.get("archive_size") or 0))
        item.setToolTip(_COL_SIZE, self._size_tooltip(event))

    @staticmethod
    def _size_tooltip(event: dict) -> str:
        """大小列的补充说明：压缩包本体 + 解压后大小 + 文件数（探不到的项不编）。"""
        size = human_size(event.get("archive_size") or 0)
        # 分卷包的大小是**整组**之和，比用户拖进来的那个 .001 文件大得多。
        # 不写明卷数，用户会以为这次又算错了——正是他反馈过的问题。
        volumes = event.get("volumes") or 1
        parts = [f"压缩包 {size}（{volumes} 个分卷）" if volumes > 1
                 else f"压缩包 {size}"]
        # bzip2/xz 这类流式格式 7z 报不出解压后大小（值为 0），
        # 此时宁可不说，也不能写成"解压后 0 B"。
        if event.get("uncompressed"):
            parts.append(f"解压后 {human_size(event['uncompressed'])}")
        if event.get("files"):
            parts.append(f"{human_count(event['files'])} 个文件")
        return " · ".join(parts)

    def _on_finished(self, report) -> None:
        self.start_btn.setEnabled(True)
        self.start_btn.setText("开始解压")
        n = len(self.input_list.real_paths())
        tail = f" · 跳过 {report.skipped}" if report.skipped else ""
        if getattr(report, "delivery_failed", 0):
            tail += f" · 未交付 {report.delivery_failed}"
        self.subtitle.setText(f"{n} 个输入 · 成功 {report.done}{tail}")
        line = f"完成：成功 {report.done} | 失败 {report.failed} | 待密码 {report.needs_password}"
        if report.skipped:
            line += f" | 已跳过 {report.skipped}"
        if getattr(report, "delivery_failed", 0):
            line += f" | 未交付 {report.delivery_failed}"
        self.statusBar().showMessage(line, 0)

        # 全部输入都被跳过时，界面上没有任何新产物——必须明确解释，
        # 否则用户会以为程序没反应或解压失败了。
        if report.skipped and not report.output_dirs:
            self.progress_label.setText(
                f"{report.skipped} 个包此前已成功解压，本次未重复处理。"
                f"产物仍在原处；如需重新解压请用命令行 --force。"
            )

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
        delivery_failed = getattr(report, "delivery_failed", 0)
        if delivery_failed:
            # 必须弹窗，不能只落在状态栏里：这是最容易误判成"已经好了"的情况——
            # 任务树显示完成、库里记着 done，而用户的目标目录里什么都没有。
            failed = getattr(report, "delivery_failed_tasks", [])
            names = "\n".join(f"· {Path(t.archive_path).name}" for t in failed[:10])
            more = f"\n…… 另 {len(failed) - 10} 个" if len(failed) > 10 else ""
            first = failed[0].delivery_error if failed else ""
            QMessageBox.warning(
                self, "有产物没能交付",
                f"{delivery_failed} 个包**已经解压成功**，但产物没能交付到目标位置，"
                f"现在仍留在隔离工作目录里：\n\n{names}{more}\n\n"
                f"原因：{first}\n\n"
                f"这些任务在列表里标为「未交付」。可以到下方「工作目录 → 查看/清理」"
                f"里查看它们占了多少空间。",
            )
        if report.warnings:
            # 复制失败/同名避让这类非致命问题只提示，不打断
            self.statusBar().showMessage(f"注意：{report.warnings[0]}", 10000)
        self._refresh_vault_label()
        self._refresh_workdir_label()

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
