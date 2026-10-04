"""暗色玻璃主题：设计令牌与 QSS 样式表。

设计取向：自用、极简高效、暗色沉浸 + 原生毛玻璃。
- 表面层级：canvas → surface → raised，靠**白色微透明叠加**而非深色压暗拉开层次
- 唯一强调色 #3B9EFF，只用于主操作与进度
- 状态色语义化，全部满足 WCAG AA 4.5:1

⚠️ 玻璃化后三条不可动摇的规则（都是实测踩出来的）：
1. **叠加一律用白色**（rgba(255,255,255,0.04~0.09)），不要用深色叠加。
   深色叠加沉到半透明底板之下，读出来就是"黑洞"；白色叠加在任何壁纸上
   都稳定"亮一档"。
2. **描边不要用亮白色**。底板半透明后，白描边会被提亮成一条刺眼的白框
   （本机实测 rgba(255,255,255,0.10) 明显可见）。改用暗色 rgba(0,0,0,0.28)
   或把白色压到 0.05 以下。
3. **顶层窗口的 QSS background 不会被绘制**。设了
   WA_TranslucentBackground 的窗口必须把底板放到一个子控件上，
   否则 grab() 出来是全透明（见 glass.py 的 _GlassDialogSurface）。
"""
from __future__ import annotations

from pathlib import Path

# ---------- 设计令牌 ----------

# 玻璃底板。半透明而不是纯黑 —— 这是"看起来还是纯黑"的解药。
GLASS_BASE = "#1B1D21"

# 底板不透明度 —— **全流程只有一个值**。
#
# 项目已放弃 Windows 原生模糊（见 `ui/glass.py` 的 `USE_NATIVE_BLUR`），
# 窗口的通透感**完全**由这一层半透明底板提供：静止、拖动、缩放、对话框降级，
# 全都是同一个形态。这样就不存在"有模糊 ↔ 无模糊"的状态切换 —— 而那个切换
# 本身就是观感不连贯的源头（用户原话："不太有连贯性"），还连带拖出一串问题
# （拖动迟滞、背景闪烁、松手后恢复延迟）。
#
# 值取 0.72：桌面内容清楚可见，同时把文字对比度托住（再透就掉）。
#
# 历史：曾经有两个值 —— `0.86`（静止/降级）与 `0.72`（拖动期，因为那时要撤掉
# 系统模糊、没有背景兜底）。放弃原生模糊后两者合并，少一个概念、也少一类
# "两个状态不一致"的缺陷。
GLASS_BASE_ALPHA = 0.72

CANVAS = "rgba(27, 29, 33, 0.72)"   # 窗口底（与底板同透明度，避免两层叠加变深）

SURFACE = "rgba(255, 255, 255, 0.04)"  # 面板 / 分组：白色微透明叠加
# 列表 / 树：**不画底**。
# 早先用 `rgba(0,0,0,0.14)` 压深，那是为"背景被系统模糊过、本身均匀"的场景设计
# 的。放弃模糊后底板直接透出桌面，任何深色叠加都会在那一块形成暗斑，而列表是
# 大面积区域，暗斑尤其显眼。区域边界交给 `border: 1px solid STROKE` 与行级
# hover / 选中反馈表达，整窗只保留**一层**底板。
SURFACE_LIST = "transparent"
RAISED = "rgba(255, 255, 255, 0.075)"   # 输入框 / 按钮：再亮一档
LINE = "rgba(255, 255, 255, 0.10)"        # 分隔线 / hover 边
LINE_SOFT = "rgba(255, 255, 255, 0.06)"   # 极淡分隔

# 描边一律用暗色：见文件头规则 2。白描边在半透明底板上会变成刺眼白框。
STROKE = "rgba(0, 0, 0, 0.28)"
STROKE_SOFT = "rgba(0, 0, 0, 0.18)"

TEXT = "#E6EDF3"        # 主要
TEXT_MUTED = "#9BA6B4"  # 次要（原 #8B949E 提亮：底板变浅后需补对比）
TEXT_FAINT = "#6E7783"  # 弱化 / 占位（原 #5E6773 仅 2.96:1，未达 AA）

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

# ---------- 圆角 ----------

R_PANEL = 14   # 面板：玻璃材质在更圆的角上才读得出来
R_LIST = 10    # 列表 / 树
R_CARD = 10    # 卡片
R_INPUT = 7    # 输入框 / 按钮
R_CHECK = 4    # 复选框

# 无边框窗口的自绘标题栏高度（glass.GlassDialogMixin 用）
DIALOG_TITLEBAR_H = 34


def bg_rgba(alpha: float) -> str:
    """玻璃底板色（带透明度）。

    从 GLASS_BASE 推导而不是各处写死 `rgba(27, 29, 33, …)` —— 同一颜色在 QSS
    与窗口代码里各存一份，改色时必漏一处（`#1B1D21` 与 `27,29,33` 是同一个值，
    肉眼根本看不出来）。
    """
    r, g, b = (int(GLASS_BASE[i:i + 2], 16) for i in (1, 3, 5))
    return f"rgba({r}, {g}, {b}, {alpha})"

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
/* ⚠️ 这里**绝不能**给 `QWidget` 兜底设背景色。
   Qt 的类型选择器匹配**所有子类**，于是窗口里每一层嵌套容器各画一层半透明
   深色 —— 而窗口是半透明的，这些层会**真实叠加**：

       1 层 → 桌面透过 28%      2 层 → 7.8%
       3 层 → 2.2%              4 层以上 → 几乎全黑

   主窗口里控件最深嵌套 6 层，于是界面上出现一块块深浅不一的矩形（用户的原话：
   "黑色的斑块，一点都不平滑"）。有系统模糊时背景本身均匀，叠加看不出来；
   放弃模糊后底板直接透出桌面，问题立刻暴露。

   底色**只**由窗口底板的 `QWidget#glassSurface` 那一层提供，其余容器一律透明。
   `color` / 字体这类不影响合成的属性照旧兜底。 */
QWidget {{
    /* 必须**显式**写 transparent，而不是省略这一行。
       省略 = 控件回落到系统调色板 —— 那些没有单独写底色的部件（表头、
       滚动区 viewport 等）会露出浅色系统底，实测 QHeaderView 直接变成一条
       白带。写 transparent 才能既统一（不叠加）又可控（不露底）。 */
    background-color: transparent;
    color: {TEXT};
    font-family: {FONT_FAMILY};
    font-size: 13px;
}}
/* 顶层窗口的 QSS background 本就不会被绘制（见文件头规则 3），
   这里同样不设背景，避免留下"看起来有底、实际不画"的误导性规则。 */
QMainWindow, QDialog {{ color: {TEXT}; }}

QToolTip {{
    background-color: {RAISED};
    color: {TEXT};
    border: 1px solid {LINE};
    border-radius: 6px;
    padding: 6px 8px;
}}

/* ===== 面板 ===== */
/* 描边用 STROKE（暗色）而非 LINE：底板半透明后，白描边会被提亮成刺眼白框。 */
QGroupBox {{
    background-color: {SURFACE};
    border: 1px solid {STROKE};
    border-radius: {R_PANEL}px;
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
    /* 原来用 CANVAS 做"抠洞"来盖住分组线。CANVAS 现在是半透明的，
       盖不住 —— 改用与面板同色的实底，否则标题处会透出下层内容。 */
    background-color: {GLASS_BASE};
}}

/* 无边框分组（靠内边距分区的面板） */
QFrame[panel="true"] {{
    background-color: {SURFACE};
    border: 1px solid {STROKE};
    border-radius: {R_PANEL}px;
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
    border: 1px solid {STROKE};
    border-radius: {R_INPUT}px;
    padding: 7px 14px;
    min-height: 18px;
}}
QPushButton:hover {{
    background-color: {LINE};
    border-color: {STROKE_SOFT};
}}
/* 按下态原来是硬编码 #38455A（一个不透明的灰块）。玻璃底上它会突兀地
   "凸"出来，改成比 hover 更亮一档的白色叠加，与整套层级语言一致。 */
QPushButton:pressed {{ background-color: rgba(255, 255, 255, 0.12); }}
QPushButton:disabled {{
    background-color: {SURFACE};
    color: {TEXT_FAINT};
    border-color: {STROKE_SOFT};
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
/* 输入控件必须保持不透明底：文字压在半透明层上会掉对比度。
   玻璃感靠描边与圆角给出，不靠底色透明。 */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox {{
    background-color: {RAISED};
    color: {TEXT};
    border: 1px solid {STROKE};
    border-radius: {R_INPUT}px;
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
    background-color: rgba(255, 255, 255, 0.12);
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
    border: 1px solid {STROKE};
    border-radius: {R_CHECK}px;
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
    border: 1px solid {STROKE};
    border-radius: {R_CHECK}px;
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
/* 导航用「白色叠加的层级台阶」表达选中：底板 SURFACE(白 0.04)、
   选中项 RAISED(白 0.075)。刻意不动用 ACCENT —— 强调色只留给主操作与进度。 */
QListWidget#settingsNav {{
    background-color: {SURFACE_LIST};
    border: none;
    border-right: 1px solid {STROKE};
    border-radius: 0;
    padding: 12px 10px;
    outline: none;
}}
QListWidget#settingsNav::item {{
    padding: 9px 12px;
    border-radius: {R_INPUT}px;
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

/* 工作目录残留计数：有残留才用警示色。
   与「危险开关」同一条原则——常驻的警示色会把真正的危险稀释成"又一个彩色文字"。
   属性随状态切换，切完必须重新 polish（见 SettingsWindow._set_warn）。 */
QLabel[role="residue"] {{ background: transparent; color: {TEXT_FAINT}; }}
QLabel[role="residue"][warn="true"] {{ background: transparent; color: {WARN}; }}

/* ===== 列表 / 树 ===== */
QListWidget, QTreeWidget, QTreeView {{
    background-color: {SURFACE_LIST};
    alternate-background-color: {SURFACE_LIST};
    border: 1px solid {STROKE};
    border-radius: {R_LIST}px;
    outline: none;
    padding: 4px;
}}
/* ⚠️ 这里**不能**写 `color`：QSS 规则的优先级高于 `QTreeWidgetItem.setForeground()`，
   一旦设了 color，状态列（pending/extracting/done/failed 语义色）与加密包类型列
   的前景色会被整片压成 TEXT 色 —— item 的 foreground 数据还在，渲染却全是白的。
   玻璃化后重渲时实测踩到：`item.foreground()` 返回 #3b9eff，grab() 里却一个
   偏蓝像素都没有。文字颜色一律交给 item 自身（默认继承 palette 的 Text 色）。 */
QListWidget::item, QTreeWidget::item {{
    padding: 5px 6px;
    border-radius: 5px;
}}
QListWidget::item:hover, QTreeWidget::item:hover {{ background-color: {RAISED}; }}
/* 选中态同理只给底色不给 color：否则选中行的状态色又会被压平。 */
QListWidget::item:selected, QTreeWidget::item:selected {{
    background-color: {LINE};
}}

/* 表头：**不画背景**，只留底部一条描边。
   早先给过 `rgba(0,0,0,0.18~0.20)` 的深色下沉，那是为"背景被系统模糊过、
   本身很均匀"的场景设计的；放弃模糊后底板直接透出桌面，任何深色叠加都会
   把那一条压成明显的暗带。区域边界交给描边表达，不再靠明暗差。 */
QHeaderView::section {{
    background: transparent;
    color: {TEXT_MUTED};
    border: none;
    border-bottom: 1px solid {STROKE};
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
QScrollBar::handle:vertical:hover {{ background: rgba(255, 255, 255, 0.28); }}
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
QScrollBar::handle:horizontal:hover {{ background: rgba(255, 255, 255, 0.28); }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ===== 状态栏 ===== */
/* 同表头：**不画背景**，只留顶部一条描边。横贯窗口底部的一条深色叠加在半透明
   底板上会被读成"黑带"，边界交给描边即可。

   ⚠️ 这里的内边距**不能**写在 QSS 的 padding 里 —— 实测 QStatusBar 不吃
   QSS padding（改了渲染毫无变化）。改用代码侧的 setContentsMargins，
   见 MainWindow._build_ui / GlassWindowMixin.init_glass。 */
QStatusBar {{
    background: transparent;
    color: {TEXT_MUTED};
    border-top: 1px solid {STROKE};
}}
QStatusBar::item {{ border: none; }}

/* ===== 分裂器 ===== */
QSplitter::handle {{ background-color: {STROKE_SOFT}; }}
QSplitter::handle:hover {{ background-color: {STROKE}; }}

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

/* ===== 无边框窗口的窗口控制按钮 =====
   自绘图标按钮（ui/icons.py）。close 单独一条：悬停即用错误色，
   这是 Windows 惯例 —— 用户在关窗口前就该得到"这一步不可逆"的提示，
   而其他按钮的悬停统一用强调色。 */
QPushButton#winCtl {{
    background: transparent;
    border: none;
    border-radius: {R_INPUT}px;
}}
QPushButton#winCtl:hover {{ background-color: rgba(255, 255, 255, 0.10); }}
QPushButton#winCtl:pressed {{ background-color: rgba(255, 255, 255, 0.16); }}
QPushButton#winCtl[danger="true"]:hover {{
    background-color: rgba(248, 81, 73, 0.22);
}}
QPushButton#winCtl[danger="true"]:pressed {{
    background-color: rgba(248, 81, 73, 0.34);
}}
"""


# ---------- 毛玻璃窗口 ----------

def dialog_qss(mode: str = "opaque", bg: str | None = None) -> str:
    """无边框玻璃窗口的样式表。

    `mode` 只有两个合法值，且**必须二选一**：
    - "glass"  → 原生 Acrylic 已在窗口后方生效，底板必须透明，否则会盖住它；
    - "opaque" → 自行绘制底板（降级态 / 拖动期原生模糊被撤下时）。

    两者同时不成立会出现「系统不画 + Qt 也没画」的透明帧，窗口直接露出桌面。
    把状态收进一个字符串参数而不是若干布尔量，就是为了让非法组合无法表达。

    `bg` 是拖动期用的底板色（来自定格快照的边缘采样）；它是快照之下的兜底，
    拿不到就用主题玻璃底。
    """
    if mode == "glass":
        plate = "background: transparent;"
    else:
        plate = f"background: {bg or bg_rgba(GLASS_BASE_ALPHA)};"
    return f"""
QWidget#glassDialog {{
    {plate}
}}
QWidget#glassDialogSurface {{
    {plate}
    border: 1px solid {STROKE};
    border-radius: {R_PANEL}px;
}}
QWidget#glassTitleBar {{
    background: transparent;
}}
QLabel#glassTitleLabel {{
    background: transparent;
    color: {TEXT};
    font-size: {DIALOG_TITLEBAR_H - 18}px;
    font-weight: 500;
}}
"""


def glass_surface_qss(alpha: float | None = None, bg: str | None = None) -> str:
    """主面板玻璃底板样式。三种形态，靠参数组合区分：

    - `glass_surface_qss()`（全空）→ **透明**：原生模糊已生效，把 Acrylic 让出来。
    - `glass_surface_qss(bg="#232427")` → **实色**：拖动期用。`bg` 一般来自定格
      快照的边缘实测色（随壁纸而变），比写死主题色更贴近静止观感。
    - `glass_surface_qss(0.86)` → **半透明底**：原生模糊不可用时的降级自绘。

    `bg` 同时是快照之下的兜底：万一快照没画上，窗口也不能变成一块透明
    （那正是"移动时毛玻璃直接没了、只剩颜色"的成因）。

    幂等由调用方保证（`setStyleSheet` 会触发重绘，拖动路径不该反复调）。
    """
    if alpha is None and bg is None:
        return (
            f"QWidget#glassSurface {{ background: transparent;"
            f" border: none; border-radius: {R_PANEL}px; }}"
        )
    return (
        f"QWidget#glassSurface {{"
        f" background: {bg or bg_rgba(alpha)};"
        f" border: 1px solid {STROKE};"
        f" border-radius: {R_PANEL}px; }}"
    )
