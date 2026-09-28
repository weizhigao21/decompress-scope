"""设置窗口：左侧模块导航 + 右侧内容页。

为什么改成导航式：原先 4 个分组纵向堆成一列，实测窗口被 minimumSizeHint 顶到
**1220px**（代码里的 resize(620,660) 形同废纸），1080p 屏上底部的「保存」根本看不见；
20 个控件连成一条长卷轴，模块之间也没有边界感。现在按「这个设置管什么」切成 6 个
模块：左列选模块、右侧只渲染当前模块，底部操作条固定住不跟着滚。

模块划分规则：**数字归数字、开关归开关**。
「解压阈值」页全是数值、「解压行为」页全是开关。此前 9 个开关都以
`addRow("", cbox)` 的形式塞在字段列里、紧贴上一行输入框，看起来像是那个输入框的
附属说明；现在每个开关都是**有标签的一行**（标签列写设置主题、复选框写动作）。

本窗口是**所有低频设置的唯一去处**：主窗口头部的两个开关只是快捷档位，
其余参数（解压深度/上限、密码来源、各类解压开关、工作目录残留的盘点入口）
一律在这里改。同一个值有两处入口，迟早会出现"界面上显示 50、实际生效 3"。

其余设计要点：
- 底部操作条放在滚动区**外面**：任何页面、任何滚动位置，「保存」都够得着。
- 依赖方向单向 ui → core：本文件不写 JSON，只改 AppConfig 再交给 core 落盘。
- 保存前先 cfg.normalize()，再把规范化后的值回填到控件，用户能立刻看到被纠正的结果。
- 控件属性名（output_combo / depth_spin …）保持原样：它们是既有测试的抓手。
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
    QMainWindow,
)

from core.appconfig import (
    AUTORUN_CONFIRM,
    AUTORUN_DIRECT,
    AUTORUN_OFF,
    OUTPUT_SAMEDIR,
    OUTPUT_WORKDIR,
    AppConfig,
    clamp_bounds,
)
from core.formatting import human_size
from core.workdir_cleanup import iter_task_dirs
from core.shell_menu import (
    install_context_menu,
    is_context_menu_installed,
    menu_icon,
    quick_command,
    uninstall_context_menu,
)
from ui import theme
from ui.workdir_window import WorkdirWindow

_AUTORUN_LABELS = {
    AUTORUN_OFF: "关闭（手动点开始）",
    AUTORUN_DIRECT: "直接开始",
    AUTORUN_CONFIRM: "先弹确认",
}
_AUTORUN_HINTS = {
    AUTORUN_OFF: "添加文件后不自动解压，需要手动点「开始解压」。",
    AUTORUN_DIRECT: "拖入文件后自动开始解压。连续拖入会合并成一次任务。",
    AUTORUN_CONFIRM: "拖入后弹窗确认真实数量，避免误拖整个磁盘。",
}
_OUTPUT_HINTS = {
    OUTPUT_SAMEDIR: "每个压缩包解到它附近：<所在目录> / <包名> /",
    OUTPUT_WORKDIR: "统一解到隔离工作目录，成功后按需复制一份回压缩包所在目录。",
}

# 标签列的固定宽度：左对齐后各页的行首才能连成一条竖线。
# 取「最大嵌套深度」（6 字 ≈ 78px）+ 余量，各页共用同一个值。
_LABEL_W = 88

# 未加速格式的单个密码候选实测开销，用来给「密码尝试」估算耗时。
# RAR/7z 等格式每次尝试要新起一个 7z.exe 并重新派生密钥
# （7z 格式 = SHA-256 迭代 2^19 轮），实测 19~31 ms，取 20 ms。
# 实测方法与全部数据见 docs/extract-optimization-review.md 第四节。
_MS_PER_CANDIDATE = 20


class SettingsWindow(QMainWindow):
    """偏好设置窗口。保存成功后 emit saved(cfg)。"""

    saved = Signal(object)     # AppConfig
    closed = Signal()

    def __init__(self, cfg: AppConfig, config_path: Path, project_root: Path,
                 db_path: Path | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(780, 620)
        self.setMinimumSize(660, 480)
        self._cfg = cfg
        self._config_path = Path(config_path)
        self._project_root = Path(project_root)
        # 残留盘点要用任务库判定「这份残留是否已交付过」。不传则按项目内的
        # 默认库位置推——显式传参的调用方（主窗口）给的才是真正在用的那个库。
        self._db_path = Path(db_path) if db_path else (self._project_root / "data" / "jieya.db")
        self._workdir_window: WorkdirWindow | None = None
        self._workdir_window_dir: Path | None = None
        self._build_ui()
        self._load_from_cfg(cfg)

    # ---------- 骨架 ----------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # 左：模块导航。分割线由 QSS 的 border-right 给，不再加独立分隔控件。
        self.nav = QListWidget()
        self.nav.setObjectName("settingsNav")
        self.nav.setFixedWidth(172)
        root.addWidget(self.nav)

        # 右：内容区（页面栈）+ 固定操作条
        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(20, 16, 20, 16)
        col.setSpacing(14)
        self.stack = QStackedWidget()
        col.addWidget(self.stack, 1)
        col.addWidget(self._build_actions())
        root.addWidget(body, 1)

        self._add_page("输出位置", "产物落到哪里、同名冲突怎么处理", self._page_output)
        self._add_page("启动与拖入", "什么情况下自动开始解压", self._page_start)
        self._add_page("解压阈值", "安全边界：超过任一上限即拒绝解压", self._page_limits)
        self._add_page("解压行为", "解压过程中的开关", self._page_behavior)
        self._add_page("密码", "密码从哪些线索里找、每个包最多试几个", self._page_password)
        self._add_page("记录与历史", "输入路径与任务记录的保留策略", self._page_history)

        self.nav.currentRowChanged.connect(self.stack.setCurrentIndex)
        self.nav.setCurrentRow(0)
        self.statusBar().showMessage(f"配置文件：{self._config_path}")

    def _add_page(self, title: str, subtitle: str, build) -> None:
        """装配一个模块页：标题 + 一行说明 + 内容，整体可滚动。

        页面自己滚、操作条不滚——否则短屏上「保存」会被挤到视口外面去。
        """
        page = QWidget()
        col = QVBoxLayout(page)
        col.setContentsMargins(2, 2, 2, 2)
        col.setSpacing(8)

        heading = QLabel(title)
        heading.setProperty("role", "pageTitle")
        desc = QLabel(subtitle)
        desc.setProperty("role", "pageDesc")
        desc.setWordWrap(True)
        col.addWidget(heading)
        col.addWidget(desc)
        col.addSpacing(4)

        build(col)
        col.addStretch(1)

        scroll = QScrollArea()
        scroll.setProperty("role", "page")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setWidget(page)
        self.stack.addWidget(scroll)
        self.nav.addItem(QListWidgetItem(title))

    # ---------- 页面 ----------

    def _page_output(self, col: QVBoxLayout) -> None:
        form = self._form(col)

        self.output_combo = QComboBox()
        self.output_combo.addItem("解压到压缩包所在目录", OUTPUT_SAMEDIR)
        self.output_combo.addItem("解压到隔离工作目录", OUTPUT_WORKDIR)
        self.output_combo.currentIndexChanged.connect(self._on_output_changed)
        self.output_hint = self._hint()
        form.addRow(self._field_label("默认落点"),
                    self._stack(self.output_combo, self.output_hint))

        self.subdir_edit = QLineEdit()
        self.subdir_edit.setPlaceholderText("留空 = 不建容器，产物直接落在压缩包所在目录")
        self.subdir_edit.setToolTip(
            "留空（默认）：解出 <所在目录>/<包名>/。\n"
            "填了名字才会多一层 <所在目录>/<容器名>/<包名>/ —— "
            "下载目录很乱、想把解压产物圈在一起时再填。")
        form.addRow(self._field_label("容器目录名"), self.subdir_edit)

        work_row = QWidget()
        work_box = QHBoxLayout(work_row)
        work_box.setContentsMargins(0, 0, 0, 0)
        work_box.setSpacing(6)
        self.workdir_edit = QLineEdit()
        self.workdir_edit.setPlaceholderText("留空 = 项目目录下的 .workspace")
        self.workdir_edit.setToolTip(
            "隔离解压与内层包展开都在这里进行。留空则用项目目录下的 .workspace")
        # 改了路径立刻重算残留计数：用户改完就想知道"这个目录里现在有没有东西"
        self.workdir_edit.textChanged.connect(self._refresh_residue)
        work_box.addWidget(self.workdir_edit, stretch=1)
        btn_pick = QPushButton("选择…")
        btn_pick.setProperty("ghost", "true")
        btn_pick.clicked.connect(self._pick_workdir)
        work_box.addWidget(btn_pick)
        form.addRow(self._field_label("工作目录"), work_row)

        col.addSpacing(6)
        form2 = self._form(col)
        self.copy_back_check = QCheckBox("隔离解压后复制一份回压缩包所在目录")
        self.copy_back_check.setToolTip(
            "仅「解压到隔离工作目录」模式有意义。关闭 = 源目录完全不被动过")
        form2.addRow(self._field_label("复制回源"), self.copy_back_check)

        self.overwrite_check = QCheckBox("直接覆盖，不自动改名避让")
        self.overwrite_check.setToolTip(
            "关闭时（推荐）遇到同名目录自动改名为「xxx (2)」，绝不覆盖既有产物")
        form2.addRow(self._field_label("同名产物"), self.overwrite_check)

        self.context_menu_check = QCheckBox("在资源管理器右键菜单显示“解压开镜”")
        self.context_menu_check.setToolTip(
            "勾选后，右键任意文件或文件夹都可直接自动解压；取消勾选会移除该菜单。"
            "此项立即生效，不需要点保存。")
        self.context_menu_check.toggled.connect(self._on_context_menu_toggled)
        form2.addRow(self._field_label("右键菜单"), self.context_menu_check)

        col.addSpacing(6)
        col.addWidget(self._build_residue_panel())

    def _build_residue_panel(self) -> QWidget:
        """工作目录残留入口：一行 = 标题 + 残留项数 + 「查看/清理」。

        为什么需要它：工作目录里的东西**全都**是「解压成功」状态（库里 done、
        任务树显示"完成"），用户没有任何线索知道磁盘被吃了多少，也没有入口清掉
        ——实测 task_83 一次就留下 5.38 GB。这个入口原先常驻在主界面底部，
        现在随其余设置一起收进这里，主界面只留「添加输入 → 开始解压」一条主线。

        计数只用一层 iterdir，绝不递归统计大小：这段代码在切页时就会跑，为了
        一个数字去走几十万个文件会把窗口卡住。真实占用点开「查看/清理」才算。
        """
        panel = QFrame()
        panel.setProperty("panel", "true")
        row = QHBoxLayout(panel)
        row.setContentsMargins(12, 10, 12, 10)
        row.setSpacing(8)

        title = QLabel("工作目录残留")
        row.addWidget(title)

        # 颜色走 QSS 的 [warn="true"] 规则而不是内联样式表：这里的状态会在
        # 「有残留 / 无残留」之间反复切换，内联写法每切一次就重铺一遍样式表。
        self.residue_label = QLabel("")
        self.residue_label.setProperty("role", "residue")
        row.addWidget(self.residue_label)

        row.addStretch(1)

        self.residue_open_btn = QPushButton("查看/清理")
        self.residue_open_btn.setProperty("ghost", "true")
        self.residue_open_btn.setCursor(Qt.PointingHandCursor)
        self.residue_open_btn.setToolTip("盘点隔离工作目录里的残留产物，确认后清理")
        self.residue_open_btn.clicked.connect(self._open_workdir_window)
        row.addWidget(self.residue_open_btn)
        return panel

    def _page_start(self, col: QVBoxLayout) -> None:
        form = self._form(col)

        self.autorun_combo = QComboBox()
        for mode in (AUTORUN_DIRECT, AUTORUN_CONFIRM, AUTORUN_OFF):
            self.autorun_combo.addItem(_AUTORUN_LABELS[mode], mode)
        self.autorun_combo.currentIndexChanged.connect(self._on_autorun_changed)
        self.autorun_hint = self._hint()
        form.addRow(self._field_label("拖入即开始"),
                    self._stack(self.autorun_combo, self.autorun_hint))

        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 10000)
        self.delay_spin.setSingleStep(200)
        self.delay_spin.setSuffix(" ms")
        self.delay_spin.setToolTip(
            "连续拖入多个文件的合并窗口：拖完这么久才真正启动，避免拖 5 个启动 5 次")
        form.addRow(self._field_label("启动延迟"), self.delay_spin)

    def _page_limits(self, col: QVBoxLayout) -> None:
        form = self._form(col)

        self.depth_spin = self._spin("max_depth")
        self.depth_spin.setSuffix(" 层")
        self.depth_spin.setToolTip("内层压缩包递归展开的层数上限，超过的内层包不再展开")
        form.addRow(self._field_label("嵌套深度"), self.depth_spin)

        self.total_spin = self._spin("max_total_gb")
        self.total_spin.setSuffix(" GB")
        self.total_spin.setToolTip("解压后总大小超过此值将拒绝，防 zip 炸弹")
        form.addRow(self._field_label("大小上限"), self.total_spin)

        self.ratio_spin = self._spin("max_ratio")
        self.ratio_spin.setSuffix(" : 1")
        self.ratio_spin.setToolTip("解压后大小 ÷ 压缩包大小超过此值即拒绝，防 zip 炸弹")
        form.addRow(self._field_label("压缩比上限"), self.ratio_spin)

        self.timeout_spin = self._spin("extract_timeout")
        self.timeout_spin.setSingleStep(300)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setToolTip(
            "单个压缩包的解压超时。超大包（几十 GB）在中低速磁盘上可能超过默认的 1 小时，"
            "被中途杀掉时会报解压失败，酌情调大")
        form.addRow(self._field_label("单包超时"), self.timeout_spin)

    def _page_password(self, col: QVBoxLayout) -> None:
        """密码相关：来源标识 + 候选数量。

        这两项原先分居两处——「密码来源」在主界面折叠区、「密码尝试」在本窗口的
        阈值页——而它们说的本来就是同一件事（拿什么去试、试几个）。聚到一页。
        """
        form = self._form(col)

        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("留空 = 每个包从自己的文件名识别")
        self.source_edit.setToolTip(
            "候选密码按「文件名 → 来源派生 → 密码库 → 常用字典」的顺序尝试。"
            "来源通常填站点域名，程序会拿它和 www.<域名> 一起当候选。"
            "留空时每个包各自从自己的文件名里识别来源")
        # QLineEdit 默认吃拖放（把文件路径当文本插进去），关掉避免误拖出乱码
        self.source_edit.setAcceptDrops(False)
        form.addRow(self._field_label("密码来源"), self.source_edit)

        self.attempts_spin = self._spin("max_password_attempts")
        self.attempts_spin.setSuffix(" 个")
        # 上限从 core 的边界里取，不在 tooltip 里再写一遍数字
        _lo, _hi = clamp_bounds("max_password_attempts") or (1, 200)
        self.attempts_spin.setToolTip(
            f"每个压缩包最多试几个候选密码（{_lo}–{_hi}），试完仍失败则标记「待密码」。\n\n"
            "它同时决定密码库能贡献几条：调大才会让库里靠后的条目轮到"
            "（库按「同来源 → 无来源 → 其他来源」排序，高命中率的排在前面）。\n\n"
            "ZIP 会自动快速排除多数错误候选；其他格式每个候选仍需由 7-Zip 验证。"
            "下方按未加速格式估算耗时，实际速度取决于格式、设备与密码。非加密包不受影响。")
        self.attempts_hint = self._hint()
        # 实时把"这个数字意味着等多久"摆在用户眼前。上限一旦放开，没有这行提示，
        # 用户会毫无察觉地把单包等待时间设成几十秒，然后以为程序卡死了。
        self.attempts_spin.valueChanged.connect(self._refresh_attempts_hint)
        form.addRow(self._field_label("密码尝试"),
                    self._stack(self.attempts_spin, self.attempts_hint))

    def _page_behavior(self, col: QVBoxLayout) -> None:
        """纯开关页：整页只有开关，所以不必担心哪个控件被误读成上一行的附属说明。"""
        form = self._form(col, spacing=14)

        self.sniff_check = QCheckBox("识别伪装压缩包和视频中的内嵌 ZIP")
        self.sniff_check.setToolTip("读取文件头和视频尾部的 ZIP 目录，识别改后缀或视频外壳压缩包")
        form.addRow(self._field_label("文件识别"), self.sniff_check)

        self.skip_done_check = QCheckBox("跳过已成功解压且产物仍在的包")
        self.skip_done_check.setToolTip(
            "开启后，之前已成功解压且产物仍在那里的包不再重复解压，避免同名目录越解越多。"
            "产物被手动删除时会自动重新解压。需要强制重解时用命令行 --force")
        form.addRow(self._field_label("重复解压"), self.skip_done_check)

        self.keep_mid_check = QCheckBox("保留中间层压缩包（不随解压清理）")
        self.keep_mid_check.setToolTip("内层包解完默认会被清掉；开启后与它解出的目录并存")
        form.addRow(self._field_label("中间层"), self.keep_mid_check)

        self.delete_orig_check = QCheckBox("完成后删除原始压缩包")
        self.delete_orig_check.setToolTip("危险：原件将被删除，仅在你确认产物完好后再开启")
        self.delete_orig_check.setProperty("danger", "true")
        form.addRow(self._field_label("原始包"), self.delete_orig_check)

    def _page_history(self, col: QVBoxLayout) -> None:
        form = self._form(col)

        self.remember_check = QCheckBox("记住最近使用的输入路径")
        self.remember_check.setToolTip("关闭后程序不保存任何输入路径")
        form.addRow(self._field_label("输入历史"), self.remember_check)

        self.restore_check = QCheckBox("启动时把上次的输入放回输入区")
        self.restore_check.setToolTip(
            "默认关闭：程序一开就自动解压上次的内容通常不是你想要的")
        form.addRow(self._field_label("启动恢复"), self.restore_check)

        self.recent_spin = self._spin("recent_inputs_limit")
        self.recent_spin.setSuffix(" 条")
        self.recent_spin.setToolTip("0 = 不记录任何历史")
        form.addRow(self._field_label("保留条数"), self.recent_spin)

        self.keep_days_spin = self._spin("session_days")
        self.keep_days_spin.setSuffix(" 天")
        self.keep_days_spin.setToolTip("任务记录（不是输入历史）在数据库里的保留天数")
        form.addRow(self._field_label("任务记录"), self.keep_days_spin)

        col.addSpacing(6)
        panel = QFrame()
        panel.setProperty("panel", "true")
        pl = QHBoxLayout(panel)
        pl.setContentsMargins(12, 10, 12, 10)
        pl.setSpacing(10)
        self.history_label = QLabel()
        self.history_label.setProperty("role", "hint")
        self.history_label.setWordWrap(True)
        pl.addWidget(self.history_label, 1)
        self.clear_history_btn = QPushButton("清空历史")
        self.clear_history_btn.setProperty("ghost", "true")
        self.clear_history_btn.setProperty("danger", "true")
        self.clear_history_btn.clicked.connect(self._clear_history)
        pl.addWidget(self.clear_history_btn, 0, Qt.AlignTop)
        col.addWidget(panel, 0)

    # ---------- 通用零件 ----------

    @staticmethod
    def _form(col: QVBoxLayout, spacing: int = 12) -> QFormLayout:
        form = QFormLayout()
        form.setSpacing(spacing)
        form.setContentsMargins(0, 0, 0, 0)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFormAlignment(Qt.AlignLeft | Qt.AlignTop)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        col.addLayout(form)
        return form

    @staticmethod
    def _field_label(text: str) -> QLabel:
        """行首标签。给固定宽度，各页的行首才会落在同一条竖线上。"""
        lab = QLabel(text)
        lab.setProperty("role", "fieldLabel")
        lab.setMinimumWidth(_LABEL_W)
        lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        return lab

    @staticmethod
    def _hint(text: str = "") -> QLabel:
        lab = QLabel(text)
        lab.setProperty("role", "hint")
        lab.setWordWrap(True)
        return lab

    @staticmethod
    def _stack(*widgets: QWidget) -> QWidget:
        """把控件与它的说明竖排成一个字段，说明贴在控件正下方而不是另起一行。"""
        wrap = QWidget()
        col = QVBoxLayout(wrap)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(5)
        for w in widgets:
            col.addWidget(w)
        return wrap

    @staticmethod
    def _spin(name: str):
        """按 core 的边界建一个数字框（float 边界 → QDoubleSpinBox）。

        范围**只**取自 `core.appconfig._CLAMP`，这里绝不再写一份常量。两边各写
        一份时，界面会允许用户调到一个 core 不认的值，然后在保存时被静默夹回去
        —— 用户看到自己的输入凭空消失，却说不出是谁干的。`_make_cfg` 那类
        "两个入口必然不一致"的坑，根子都在这里。
        """
        lo, hi = clamp_bounds(name) or (0, 0)
        if isinstance(lo, float):
            sp = QDoubleSpinBox()
            sp.setDecimals(0)
        else:
            sp = QSpinBox()
        sp.setRange(lo, hi)
        return sp

    def _build_actions(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.reset_btn = QPushButton("恢复默认")
        self.reset_btn.setProperty("ghost", "true")
        self.reset_btn.setProperty("danger", "true")
        self.reset_btn.clicked.connect(self._on_reset)
        row.addWidget(self.reset_btn)

        row.addStretch(1)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setProperty("ghost", "true")
        self.cancel_btn.clicked.connect(self.close)
        row.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("保存")
        self.save_btn.setProperty("primary", "true")
        self.save_btn.setMinimumHeight(34)
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self._on_save)
        row.addWidget(self.save_btn)
        return bar

    # ---------- 数据绑定 ----------

    def _load_from_cfg(self, cfg: AppConfig) -> None:
        """控件 ← 配置。先 blockSignals，避免程序化赋值触发联动与"已修改"态。"""
        self.output_combo.blockSignals(True)
        self.autorun_combo.blockSignals(True)

        idx = self.output_combo.findData(cfg.output_mode)
        self.output_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.output_hint.setText(_OUTPUT_HINTS.get(cfg.output_mode, ""))

        idx = self.autorun_combo.findData(cfg.autorun_mode)
        self.autorun_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.autorun_hint.setText(_AUTORUN_HINTS.get(cfg.autorun_mode, ""))

        self.output_combo.blockSignals(False)
        self.autorun_combo.blockSignals(False)

        self.subdir_edit.setText(cfg.subdir_name)
        self.workdir_edit.setText(cfg.workdir)
        self.overwrite_check.setChecked(cfg.overwrite_existing)
        self.copy_back_check.setChecked(cfg.copy_back_to_source)
        self.context_menu_check.blockSignals(True)
        self.context_menu_check.setChecked(is_context_menu_installed())
        self.context_menu_check.blockSignals(False)

        self.delay_spin.setValue(cfg.autorun_delay_ms)
        self.remember_check.setChecked(cfg.remember_inputs)
        self.restore_check.setChecked(cfg.restore_last_inputs)
        self.recent_spin.setValue(cfg.recent_inputs_limit)

        self.depth_spin.setValue(cfg.max_depth)
        self.total_spin.setValue(cfg.max_total_gb)
        self.ratio_spin.setValue(cfg.max_ratio)
        self.source_edit.setText(cfg.password_source)
        self.attempts_spin.setValue(cfg.max_password_attempts)
        self.timeout_spin.setValue(cfg.extract_timeout)
        self.skip_done_check.setChecked(cfg.skip_done)
        self.sniff_check.setChecked(cfg.sniff_archives)
        self.keep_mid_check.setChecked(cfg.delete_intermediate is False)
        self.delete_orig_check.setChecked(cfg.keep_original is False)
        self.keep_days_spin.setValue(cfg.session_days)

        self._sync_output_dependents()
        self._refresh_attempts_hint()
        self._refresh_history_label(cfg)
        self._refresh_residue()

    def _collect(self) -> AppConfig:
        """控件 → 配置（基于原 cfg 派生，保留本窗口未暴露的字段）。"""
        return replace(
            self._cfg,
            output_mode=self.output_combo.currentData() or OUTPUT_SAMEDIR,
            subdir_name=self.subdir_edit.text().strip(),
            workdir=self.workdir_edit.text().strip(),
            overwrite_existing=self.overwrite_check.isChecked(),
            copy_back_to_source=self.copy_back_check.isChecked(),
            autorun_mode=self.autorun_combo.currentData() or AUTORUN_DIRECT,
            autorun_delay_ms=self.delay_spin.value(),
            remember_inputs=self.remember_check.isChecked(),
            restore_last_inputs=self.restore_check.isChecked(),
            recent_inputs_limit=self.recent_spin.value(),
            max_depth=self.depth_spin.value(),
            max_total_gb=self.total_spin.value(),
            max_ratio=self.ratio_spin.value(),
            password_source=self.source_edit.text().strip(),
            max_password_attempts=self.attempts_spin.value(),
            extract_timeout=self.timeout_spin.value(),
            skip_done=self.skip_done_check.isChecked(),
            sniff_archives=self.sniff_check.isChecked(),
            delete_intermediate=not self.keep_mid_check.isChecked(),
            keep_original=not self.delete_orig_check.isChecked(),
            session_days=self.keep_days_spin.value(),
        ).normalize()

    def _refresh_attempts_hint(self) -> None:
        """把「密码尝试」的数字翻译成用户真正关心的东西：单个包最坏要等多久。

        只算**最坏**（全部候选都不匹配）。真实情况下命中往往在前几条，因为库是按
        命中率排序的；这里不做乐观估计，宁可让用户以为慢一点。
        """
        n = self.attempts_spin.value()
        secs = n * _MS_PER_CANDIDATE / 1000.0
        self.attempts_hint.setText(
            f"未加速格式参考 ≈ {secs:.1f} 秒/包（每个候选约 {_MS_PER_CANDIDATE} ms）；"
            "ZIP 快速筛选通常更快，非加密包不受影响。")

    def _sync_output_dependents(self) -> None:
        """按输出模式启用/禁用只在某模式下才成立的开关。

        「复制回源」在 samedir 模式下会被 pipeline 完全忽略（见 output_plan 的
        `mode == OUTPUT_WORKDIR` 条件）。留着可勾选就是界面在说谎——用户会以为
        开了就生效。就地禁用，并把原因写进 tooltip。
        """
        isolated = (self.output_combo.currentData() or OUTPUT_SAMEDIR) == OUTPUT_WORKDIR
        self.copy_back_check.setEnabled(isolated)
        self.copy_back_check.setToolTip(
            "仅「解压到隔离工作目录」模式有意义。关闭 = 源目录完全不被动过"
            if isolated else
            "当前是「解压到压缩包所在目录」模式，产物直接落在源目录旁，无需复制回来")

    def _refresh_history_label(self, cfg: AppConfig) -> None:
        n = len(cfg.recent_inputs or [])
        if not cfg.remember_inputs:
            self.history_label.setText("已关闭记录。程序不会保存任何输入路径。")
        elif n == 0:
            self.history_label.setText("暂无记录。")
        else:
            preview = "、".join(Path(p).name for p in cfg.recent_inputs[:3])
            more = f" 等 {n} 条" if n > 3 else ""
            self.history_label.setText(f"已记录 {n} 条：{preview}{more}")

    # ---------- 工作目录残留 ----------

    def _effective_workdir(self) -> Path:
        """面板里那个工作目录的绝对路径。

        以**输入框**为准而不是已保存的配置：用户改完路径还没点保存时，
        他看的就是这个目录里的东西，计数跟着它走才不会让人以为改了个假的。
        留空时的回落逻辑复用 AppConfig.resolve_workdir，不在这里再写一份。
        """
        return replace(self._cfg, workdir=self.workdir_edit.text().strip()) \
            .resolve_workdir(self._project_root)

    def _refresh_residue(self) -> None:
        """重算残留项数。只读一层目录，不递归。"""
        n = len(iter_task_dirs(self._effective_workdir()))
        self.residue_label.setText(f"{n} 项残留" if n else "无残留")
        self._set_warn(self.residue_label, bool(n))

    @staticmethod
    def _set_warn(widget: QWidget, on: bool) -> None:
        """切换警示色。

        动态属性改动后**必须**重新 polish：QSS 的属性选择器不会自己重算，
        只 setProperty 的话颜色会一直停在初始那一档（表现为"有残留也不变色"）。
        """
        if bool(widget.property("warn")) == on:
            return
        widget.setProperty("warn", "true" if on else None)
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    def _open_workdir_window(self) -> None:
        """残留窗口单例；沿用密码库那套 closed 信号清理引用。"""
        wd = self._effective_workdir()
        if self._workdir_window is not None and self._workdir_window_dir != wd:
            # 工作目录被改过：旧窗口盘的是另一个目录，换掉而不是复用
            self._workdir_window.close()
        if self._workdir_window is None:
            self._workdir_window = WorkdirWindow(wd, self._db_path, parent=self)
            self._workdir_window.cleaned.connect(self._on_residue_cleaned)
            self._workdir_window.closed.connect(self._on_residue_closed)
            self._workdir_window_dir = wd
            self._workdir_window.show()
        else:
            self._workdir_window.refresh()
            self._workdir_window.show()
            self._workdir_window.raise_()
            self._workdir_window.activateWindow()

    def _on_residue_cleaned(self, freed: int) -> None:
        self._refresh_residue()
        self.statusBar().showMessage(f"已清理工作目录残留，释放 {human_size(freed)}", 8000)

    def _on_residue_closed(self) -> None:
        self._workdir_window = None
        self._workdir_window_dir = None
        self._refresh_residue()

    # ---------- 交互 ----------

    def _on_output_changed(self, _i: int) -> None:
        self.output_hint.setText(_OUTPUT_HINTS.get(self.output_combo.currentData(), ""))
        self._sync_output_dependents()

    def _on_context_menu_toggled(self, enabled: bool) -> None:
        """右键菜单是系统集成，不是延迟到「保存」才生效的应用偏好。"""
        try:
            if enabled:
                install_context_menu(
                    quick_command(self._project_root), menu_icon(self._project_root),
                )
                self.statusBar().showMessage("已启用资源管理器右键菜单", 5000)
            else:
                uninstall_context_menu()
                self.statusBar().showMessage("已移除资源管理器右键菜单", 5000)
        except OSError as exc:
            # 注册表写失败时必须还原勾选状态；保留“已开启”的视觉会让用户以为
            # 下次右键一定能看到入口，实际却没有。
            self.context_menu_check.blockSignals(True)
            self.context_menu_check.setChecked(not enabled)
            self.context_menu_check.blockSignals(False)
            QMessageBox.critical(self, "右键菜单设置失败", str(exc))

    def _on_autorun_changed(self, _i: int) -> None:
        self.autorun_hint.setText(_AUTORUN_HINTS.get(self.autorun_combo.currentData(), ""))

    def _pick_workdir(self) -> None:
        start = self.workdir_edit.text().strip() or str(self._project_root)
        d = QFileDialog.getExistingDirectory(self, "选择工作目录", start)
        if d:
            self.workdir_edit.setText(d)

    def _clear_history(self) -> None:
        if not self._cfg.recent_inputs:
            QMessageBox.information(self, "清空历史", "当前没有历史记录。")
            return
        ret = QMessageBox.question(
            self, "清空历史",
            f"将删除 {len(self._cfg.recent_inputs)} 条输入路径记录，确定吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        self._cfg.recent_inputs = []
        self._refresh_history_label(self._cfg)

    def _on_reset(self) -> None:
        ret = QMessageBox.question(
            self, "恢复默认设置",
            "所有设置将恢复为出厂默认值（不影响密码库与已解压的文件）。\n\n确定吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        fresh = AppConfig()
        self._load_from_cfg(fresh)
        self.statusBar().showMessage("已恢复默认值，点「保存」生效", 6000)

    def _on_save(self) -> None:
        cfg = self._collect()
        # 工作目录若填了值，先确认可写，避免保存后每次解压都失败
        if cfg.workdir:
            wd = Path(cfg.workdir).expanduser()
            try:
                wd.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.critical(
                    self, "工作目录不可用",
                    f"无法创建或写入：{wd}\n\n{exc}")
                return
        try:
            cfg.save(self._config_path)
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", f"写入配置失败：{exc}")
            return
        self._cfg = cfg
        # 回填规范化后的值，让用户立刻看到被纠正的结果（如深度被截到 10）
        self._load_from_cfg(cfg)
        self.statusBar().showMessage("已保存", 4000)
        self.saved.emit(cfg)
        self.close()

    # ---------- 生命周期 ----------

    def showEvent(self, event) -> None:
        """每次显示都重算残留：窗口开着的时候可能又解压过好几轮。"""
        self._refresh_residue()
        super().showEvent(event)

    def closeEvent(self, event) -> None:
        # 残留窗口挂在本窗口下，主窗口关掉设置时它不能变成孤儿
        if self._workdir_window is not None:
            self._workdir_window.close()
            self._workdir_window = None
            self._workdir_window_dir = None
        self.closed.emit()
        super().closeEvent(event)
