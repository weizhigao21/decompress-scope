"""暗色主题：设计令牌与 QSS 样式表。

设计取向：自用、极简高效、暗色沉浸。
- 表面层级：canvas → surface → raised，靠亮度而非边框拉开层次
- 唯一强调色 #3B9EFF，只用于主操作与进度
- 状态色语义化，全部满足 WCAG AA 4.5:1
"""
from __future__ import annotations

from pathlib import Path

# ---------- 设计令牌 ----------

CANVAS = "#0D1117"      # 窗口底
SURFACE = "#161B22"     # 面板 / 列表
RAISED = "#1F2630"      # 输入框 / 按钮
LINE = "#2D3844"        # 分隔线 / hover 边
LINE_SOFT = "#232B36"   # 极淡分隔

TEXT = "#E6EDF3"        # 主要
TEXT_MUTED = "#8B949E"  # 次要
TEXT_FAINT = "#5E6773"  # 弱化 / 占位

ACCENT = "#3B9EFF"
ACCENT_HOVER = "#57AEFF"
ACCENT_DOWN = "#2B8AE6"
ACCENT_TEXT = "#0D1117"  # 强调色上的文字（深色底 + 亮色填充）

OK = "#3FB950"
WARN = "#D29922"
ERR = "#F85149"

# 状态 → 颜色（供任务树等复用）
STATUS_COLORS: dict[str, str] = {
    "pending": TEXT_FAINT,
    "probing": ACCENT,
    "extracting": ACCENT,
    "needs_password": WARN,
    "done": OK,
    "failed": ERR,
}

# 「已完成但产物未交付」是**界面呈现态**，不是任务状态：core 里这些任务的
# status 就是 done（解压确实成功了），只是 delivery_error 非空。
#
# 刻意不塞进 STATUS_COLORS：那张表的键必须与 core.models.TaskStatus 一一对应
# （test_ui_smoke 里有守卫钉着），混入非状态值会让它不再是"状态表"。
UNDELIVERED = WARN


def status_color(state: str) -> str:
    """任务树状态列取色。入参可以是真实状态，也可以是 done_undelivered。

    用警示色而不是 OK/ERR 表达未交付：「解出来但没到你手里」既不是成功，
    也不等于失败——判失败会让用户跑去重新解压一个其实已经解好的包。
    """
    if state == "done_undelivered":
        return UNDELIVERED
    return STATUS_COLORS.get(state, "")

FONT_FAMILY = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", system-ui, sans-serif'
FONT_MONO = '"Cascadia Mono", "Consolas", "JetBrains Mono", monospace'

# ---------- 资源 ----------

_ASSETS_DIR = Path(__file__).resolve().parent / "assets"


def asset_url(name: str) -> str:
    """资源文件在 QSS 里的 url() 写法。

    两个必须踩对的点：
    - QSS 的相对路径是按**进程工作目录**解析的（不是相对本文件），所以必须给
      绝对路径，否则换个目录启动就找不到图标；
    - Windows 的反斜杠在 QSS 字符串里是转义符，必须用 as_posix() 转成正斜杠。
    """
    return (_ASSETS_DIR / name).as_posix()


def build_stylesheet() -> str:
    """生成全局 QSS。"""
    return f"""
/* ===== 基础 ===== */
QWidget {{
    background-color: {CANVAS};
    color: {TEXT};
    font-family: {FONT_FAMILY};
    font-size: 13px;
}}
QMainWindow, QDialog {{ background-color: {CANVAS}; }}

QToolTip {{
    background-color: {RAISED};
    color: {TEXT};
    border: 1px solid {LINE};
    border-radius: 6px;
    padding: 6px 8px;
}}

/* ===== 面板 ===== */
QGroupBox {{
    background-color: {SURFACE};
    border: 1px solid {LINE};
    border-radius: 10px;
    margin-top: 14px;
    padding: 14px 12px 12px 12px;
    font-weight: 500;
}}
QGroupBox::title {{
    subcontrol-origin: margin;
    subcontrol-position: top left;
    left: 12px;
    padding: 0 6px;
    color: {TEXT_MUTED};
    background-color: {CANVAS};
}}

/* 无边框分组（靠内边距分区的面板） */
QFrame[panel="true"] {{
    background-color: {SURFACE};
    border: 1px solid {LINE};
    border-radius: 10px;
}}

/* ===== 折叠标题栏 ===== */
QToolButton[section="true"] {{
    background-color: transparent;
    color: {TEXT};
    border: none;
    padding: 6px 4px;
    font-weight: 500;
    text-align: left;
}}
QToolButton[section="true"]:hover {{ color: {ACCENT}; }}

/* ===== 按钮 ===== */
QPushButton {{
    background-color: {RAISED};
    color: {TEXT};
    border: 1px solid {LINE};
    border-radius: 7px;
    padding: 7px 14px;
    min-height: 18px;
}}
QPushButton:hover {{
    background-color: {LINE};
    border-color: {LINE};
}}
QPushButton:pressed {{ background-color: #38455A; }}
QPushButton:disabled {{
    background-color: {SURFACE};
    color: {TEXT_FAINT};
    border-color: {LINE_SOFT};
}}

/* 主操作：唯一使用强调色的按钮 */
QPushButton[primary="true"] {{
    background-color: {ACCENT};
    color: {ACCENT_TEXT};
    border: none;
    font-weight: 500;
    padding: 9px 16px;
}}
QPushButton[primary="true"]:hover {{ background-color: {ACCENT_HOVER}; }}
QPushButton[primary="true"]:pressed {{ background-color: {ACCENT_DOWN}; }}
QPushButton[primary="true"]:disabled {{
    background-color: {RAISED};
    color: {TEXT_FAINT};
}}

/* 幽灵按钮：文字类次级操作 */
QPushButton[ghost="true"] {{
    background-color: transparent;
    border: none;
    color: {TEXT_MUTED};
    padding: 4px 8px;
}}
QPushButton[ghost="true"]:hover {{
    background-color: {RAISED};
    color: {TEXT};
}}

/* ===== 输入控件 ===== */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {RAISED};
    color: {TEXT};
    border: 1px solid {LINE};
    border-radius: 7px;
    padding: 6px 9px;
    selection-background-color: {ACCENT};
    selection-color: {ACCENT_TEXT};
}}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus {{
    border-color: {ACCENT};
}}
QLineEdit:disabled, QSpinBox:disabled {{ color: {TEXT_FAINT}; }}
QLineEdit::placeholder {{ color: {TEXT_FAINT}; }}

QSpinBox::up-button, QSpinBox::down-button,
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button {{
    background-color: {LINE};
    border: none;
    width: 16px;
}}
QSpinBox::up-button:hover, QSpinBox::down-button:hover {{
    background-color: #38455A;
}}
/* 箭头必须显式给图：一旦给 ::up-button/::down-button 设了背景，Qt 就不再画
   默认箭头，只留一块纯色 —— 表现就是「有灰条没三角」。 */
QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{
    image: url("{asset_url('arrow_up.svg')}");
    width: 10px;
    height: 6px;
}}
QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{
    image: url("{asset_url('arrow_down.svg')}");
    width: 10px;
    height: 6px;
}}
QSpinBox::up-arrow:disabled, QDoubleSpinBox::up-arrow:disabled,
QSpinBox::down-arrow:disabled, QDoubleSpinBox::down-arrow:disabled {{
    image: none;
}}
/* 下拉框改用同一套三角，不再依赖 Fusion 的默认图标，视觉与数字框一致 */
QComboBox::down-arrow {{
    image: url("{asset_url('arrow_down.svg')}");
    width: 10px;
    height: 6px;
}}

/* ===== 复选框 ===== */
QCheckBox {{ spacing: 7px; color: {TEXT_MUTED}; }}
QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {LINE};
    border-radius: 4px;
    background-color: {RAISED};
}}
QCheckBox::indicator:hover {{ border-color: {ACCENT}; }}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    /* 同理：设了背景就必须自己给出图形，否则只剩一个蓝方块 */
    image: url("{asset_url('check.svg')}");
}}
QCheckBox::indicator:checked:disabled {{
    background-color: {LINE};
    border-color: {LINE};
    image: none;
}}

/* 危险开关：警示只在「真的打开」那一刻出现——未勾选时它什么也没做，
   挂着红字只会制造常驻噪音、让真正的危险被稀释。
   （此前 QCheckBox[danger="true"] 一条规则都没有，这个属性是死的：
     「完成后删除原始压缩包」全程零警示。） */
QCheckBox[danger="true"]::indicator:checked {{
    background-color: {ERR};
    border-color: {ERR};
}}
QCheckBox[danger="true"]:checked {{ color: {ERR}; }}

/* 列表/树里的复选框（如工作目录残留窗口的选择列）。
   必须自己给图形：QCheckBox::indicator 那套**不覆盖** item view 的指示器，
   不给就落到平台默认样式——Fusion 在暗色底上只画一个近同色的方框，
   勾选态长什么样完全取决于平台（Ubuntu 的 Qt 又是另一套观感）。
   这里复用与 QCheckBox 完全相同的画法，两处观感才能一致。 */
QTreeView::indicator, QTreeWidget::indicator,
QTableView::indicator, QListView::indicator {{
    width: 15px;
    height: 15px;
    border: 1px solid {LINE};
    border-radius: 4px;
    background-color: {RAISED};
}}
QTreeView::indicator:hover, QTreeWidget::indicator:hover,
QTableView::indicator:hover, QListView::indicator:hover {{
    border-color: {ACCENT};
}}
QTreeView::indicator:checked, QTreeWidget::indicator:checked,
QTableView::indicator:checked, QListView::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT};
    image: url("{asset_url('check.svg')}");
}}
QTreeView::indicator:checked:disabled, QTreeWidget::indicator:checked:disabled,
QTableView::indicator:checked:disabled, QListView::indicator:checked:disabled {{
    background-color: {LINE};
    border-color: {LINE};
    image: none;
}}

/* ===== 设置窗口：左侧模块导航 ===== */
/* 导航用「亮度台阶」表达选中：底板 SURFACE(#161B22)、选中项 RAISED(#1F2630)。
   刻意不动用 ACCENT —— 强调色只留给主操作（保存）与进度。 */
QListWidget#settingsNav {{
    background-color: {SURFACE};
    border: none;
    border-right: 1px solid {LINE};
    border-radius: 0;
    padding: 12px 10px;
    outline: none;
}}
QListWidget#settingsNav::item {{
    padding: 9px 12px;
    border-radius: 7px;
    color: {TEXT_MUTED};
}}
QListWidget#settingsNav::item:hover {{
    background-color: {LINE_SOFT};
    color: {TEXT};
}}
QListWidget#settingsNav::item:selected {{
    background-color: {RAISED};
    color: {TEXT};
    font-weight: 500;
}}

/* 设置页：页面自身滚动，操作条不滚（short 屏上「保存」必须始终够得着） */
QScrollArea[role="page"] {{
    background: transparent;
    border: none;
}}
QScrollArea[role="page"] > QWidget > QWidget {{ background: transparent; }}

QLabel[role="pageTitle"] {{
    background: transparent;
    color: {TEXT};
    font-size: 15px;
    font-weight: 600;
}}
QLabel[role="pageDesc"], QLabel[role="hint"] {{
    background: transparent;
    color: {TEXT_FAINT};
    font-size: 12px;
}}
QLabel[role="fieldLabel"] {{ background: transparent; color: {TEXT_MUTED}; }}

/* ===== 列表 / 树 ===== */
QListWidget, QTreeWidget, QTreeView {{
    background-color: {SURFACE};
    alternate-background-color: {SURFACE};
    border: 1px solid {LINE};
    border-radius: 8px;
    outline: none;
    padding: 4px;
}}
QListWidget::item, QTreeWidget::item {{
    padding: 5px 6px;
    border-radius: 5px;
    color: {TEXT};
}}
QListWidget::item:hover, QTreeWidget::item:hover {{ background-color: {RAISED}; }}
QListWidget::item:selected, QTreeWidget::item:selected {{
    background-color: {LINE};
    color: {TEXT};
}}

QHeaderView::section {{
    background-color: {SURFACE};
    color: {TEXT_MUTED};
    border: none;
    border-bottom: 1px solid {LINE};
    padding: 7px 8px;
    font-weight: 400;
}}
QHeaderView::section:hover {{ color: {TEXT}; }}

/* ===== 进度条 ===== */
QProgressBar {{
    background-color: {RAISED};
    border: none;
    border-radius: 4px;
    height: 8px;
    text-align: center;
    color: transparent;
}}
QProgressBar::chunk {{
    background-color: {ACCENT};
    border-radius: 4px;
}}

/* ===== 滚动条 ===== */
QScrollBar:vertical {{
    background: transparent;
    width: 10px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: {LINE};
    border-radius: 5px;
    min-height: 28px;
}}
QScrollBar::handle:vertical:hover {{ background: #38455A; }}
QScrollBar:horizontal {{
    background: transparent;
    height: 10px;
    margin: 2px;
}}
QScrollBar::handle:horizontal {{
    background: {LINE};
    border-radius: 5px;
    min-width: 28px;
}}
QScrollBar::handle:horizontal:hover {{ background: #38455A; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ===== 状态栏 ===== */
QStatusBar {{
    background-color: {SURFACE};
    color: {TEXT_MUTED};
    border-top: 1px solid {LINE};
}}
QStatusBar::item {{ border: none; }}

/* ===== 分裂器 ===== */
QSplitter::handle {{ background-color: {LINE_SOFT}; }}
QSplitter::handle:hover {{ background-color: {LINE}; }}

/* ===== 标签辅助 ===== */
QLabel[hint="true"] {{ color: {TEXT_MUTED}; }}
QLabel[faint="true"] {{ color: {TEXT_FAINT}; }}

/* 危险操作：幽灵按钮 + danger，hover 时用错误色示警 */
QPushButton[ghost="true"][danger="true"]:hover {{
    background-color: transparent;
    color: {ERR};
}}

/* 统计卡：数字与标签透明融入面板，不叠加异色底 */
QLabel[stat="value"] {{ background: transparent; }}
QLabel[stat="label"] {{ background: transparent; color: {TEXT_FAINT}; font-size: 12px; }}

/* 头部版本号：弱色小字。
   字号**必须**在 QSS 里给——`QWidget {{ font-size: 13px; }}` 优先级高于
   `setFont()`，代码里 setPointSize 会被整个覆盖，版本号会跟产品名一样大。 */
QLabel[role="version"] {{ background: transparent; color: {TEXT_FAINT}; font-size: 11px; }}
"""
