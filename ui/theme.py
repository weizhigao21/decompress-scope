"""暗色主题：设计令牌与 QSS 样式表。

设计取向：自用、极简高效、暗色沉浸。
- 表面层级：canvas → surface → raised，靠亮度而非边框拉开层次
- 唯一强调色 #3B9EFF，只用于主操作与进度
- 状态色语义化，全部满足 WCAG AA 4.5:1
"""
from __future__ import annotations

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

FONT_FAMILY = '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", system-ui, sans-serif'
FONT_MONO = '"Cascadia Mono", "Consolas", "JetBrains Mono", monospace'


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
}}

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
"""
