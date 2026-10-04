# -*- coding: utf-8 -*-
"""Windows 毛玻璃后端：原生 Acrylic/Mica 模糊 + 失败自动降级。

调用方约定：
    enabled = apply_glass(hwnd)
    enabled 为 True → 原生模糊已生效，Qt 侧只需半透明底板；
    enabled 为 False → 原生不可用，Qt 侧改用自绘玻璃（theme 的不透明底板）。

实现层级（自上而下依次尝试）：
    1. Win11 22H2+  DWMWA_SYSTEMBACKDROP_TYPE = 3 (Acrylic)
    2. Win11 21H2    DWMWA_MICA_EFFECT      = 1
    3. Win10 1803+   SetWindowCompositionAttribute ACCENT_ENABLE_ACRYLICBLURBEHIND
    4. 任意失败 → apply_glass() 返回 False，由调用方降级为自绘玻璃

Win10 那条通路（第 3 级）在窗口移动/缩放时会严重迟滞，属于 OS bug，
缓解办法是移动期临时**撤掉**它（`suspend()`）。但撤掉之后窗口不能只剩一块纯色
——那正是用户反馈的"移动的时候毛玻璃效果直接没有了，也只有颜色"。
所以移动期由 `capture_window_surface()` 在撤之前拍一张**定格快照**（模糊后的
桌面 + 深色 tint + 控件本身），移动全程把这一张图当作背景画回去，观感与静止时
一致；详见该函数的说明。

抓屏一律用**一次分区 BitBlt**（`sct.grab`），绝不逐像素读——那会把 GUI 线程
钉死成"程序卡死"（单次调用≈一帧，实测数据见 `capture_window_surface` 内注释）。
不要试图改用 ACCENT_ENABLE_BLURBEHIND "降级" —— 本机实测它输出纯白，见文件末尾。

依赖：ctypes（Win32）+ `core.screen`（复用项目统一的截屏入口，不直接引 mss）。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from typing import NamedTuple

from PySide6.QtCore import QPoint, QRect, QRectF, Qt
from PySide6.QtGui import QPainter, QPainterPath
from PySide6.QtWidgets import QWidget

_IS_WINDOWS = sys.platform == "win32"

# ---- DWM 属性常量 ----
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMWA_MICA_EFFECT = 1029  # Win11 21H2 私有属性

# DWMSBT_* 背景类型（Win11 22H2+）
_DWMSBT_AUTO = 0
_DWMSBT_NONE = 1
_DWMSBT_MICA2 = 2      # Mica（主窗背景采样）
_DWMSBT_ACRYLIC3 = 3   # Acrylic（桌面背景采样，悬浮窗用这个）

# SetWindowCompositionAttribute（Win10 兜底）
_WCA_ACCENT_POLICY = 19
_ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
_ACCENT_ENABLE_BLURBEHIND = 3   # ⚠️ 见文件末尾说明：本机实测不可用，勿启用


class _AccentPolicy(ctypes.Structure):
    _fields_ = [
        ("AccentState", ctypes.c_int),
        ("AccentFlags", ctypes.c_int),
        ("GradientColor", ctypes.c_uint),   # AABBGGRR
        ("AnimationId", ctypes.c_int),
    ]


class _WinCompAttrData(ctypes.Structure):
    _fields_ = [
        ("Attribute", ctypes.c_int),
        ("Data", ctypes.c_void_p),
        ("SizeOfData", ctypes.c_size_t),
    ]


def _dwm_set(hwnd: int, attr: int, value: int) -> bool:
    """写入一个 DWORD 型 DWM 属性，成功返回 True。"""
    try:
        dwmapi = ctypes.WinDLL("dwmapi")
        val = ctypes.c_int(value)
        res = dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), ctypes.c_uint(attr),
            ctypes.byref(val), ctypes.sizeof(val),
        )
        return res == 0
    except Exception:
        return False


def _apply_win11_acrylic(hwnd: int) -> bool:
    """Win11 22H2+：系统级 Acrylic 背景。"""
    return _dwm_set(hwnd, _DWMWA_SYSTEMBACKDROP_TYPE, _DWMSBT_ACRYLIC3)


def _apply_win11_mica(hwnd: int) -> bool:
    """Win11 21H2：Mica 效果（早期版本）。"""
    return _dwm_set(hwnd, _DWMWA_MICA_EFFECT, 1)


def _set_win10_accent(hwnd: int, state: int, tint_abgr: int = 0) -> bool:
    """写一次 SetWindowCompositionAttribute(ACCENT_POLICY)，成功返回 True。

    两种 AccentState 共用这一条通路：
        _ACCENT_ENABLE_ACRYLICBLURBEHIND 硬模糊（静止时用，移动会迟滞）
        0 (ACCENT_DISABLED)              完全关闭（移动期撤掉时用）
    """
    try:
        user32 = ctypes.WinDLL("user32")
        set_attr = user32.SetWindowCompositionAttribute
        set_attr.argtypes = [wintypes.HWND, ctypes.POINTER(_WinCompAttrData)]
        set_attr.restype = ctypes.c_int

        policy = _AccentPolicy()
        policy.AccentState = state
        policy.AccentFlags = 0x20 | 0x40 | 0x80 | 0x100  # 四边都生效，避免边框描边
        policy.GradientColor = tint_abgr

        data = _WinCompAttrData()
        data.Attribute = _WCA_ACCENT_POLICY
        data.Data = ctypes.cast(ctypes.byref(policy), ctypes.c_void_p)
        data.SizeOfData = ctypes.sizeof(policy)
        return bool(set_attr(wintypes.HWND(hwnd), ctypes.byref(data)))
    except Exception:
        return False


def _apply_win10_acrylic(hwnd: int, tint_abgr: int = 0xB31B1D21) -> bool:
    """Win10 1803+：SetWindowCompositionAttribute 亚克力模糊（硬模糊）。

    tint_abgr 为 AABBGGRR 格式，默认深灰 #1B1D21 带 0xB3(70%) 不透明度。
    """
    return _set_win10_accent(hwnd, _ACCENT_ENABLE_ACRYLICBLURBEHIND, tint_abgr)


def _disable_win10_acrylic(hwnd: int) -> None:
    """关闭 Win10 亚克力（拖动期间临时关闭用）。"""
    _set_win10_accent(hwnd, 0)


def apply_dark_titlebar(hwnd: int) -> bool:
    """让原生标题栏（对话框用）跟随暗色。"""
    return _dwm_set(hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, 1)


def _client_rect(hwnd: int):
    """取窗口**客户区**（不含标题栏与边框）在屏幕上的矩形，物理像素。

    用客户区而不是 `GetWindowRect`：快照要贴回 Qt 控件自己的坐标系，
    带上标题栏/阴影边框就对不齐了。

    必须显式写 argtypes —— 不写的话 ctypes 会把手柄当 32 位 C int 传，
    64 位下句柄被截断，调用直接失败。
    """
    try:
        user32 = ctypes.WinDLL("user32")
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        user32.ClientToScreen.restype = wintypes.BOOL

        box = wintypes.RECT()
        if not user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(box)):
            return None
        origin = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(origin)):
            return None
        return origin.x, origin.y, box.right - box.left, box.bottom - box.top
    except Exception:
        return None


class SurfaceShot(NamedTuple):
    """窗口客户区的一次屏幕快照（移动期冒充原生模糊用）。"""

    width: int
    height: int
    pixels: bytes        # BGRA，可直接喂 QImage.Format_RGB32
    color: str | None    # 边缘采样得到的中位色；取不到为 None


# 采样既有下限也有上限：太低会让"磨砂灰"直接 fallback 成纯黑，太高则移动的
# 瞬间会闪出一块浅色，两者都偏离"暗色玻璃"的设计。
_SAMPLE_MIN = 28
_SAMPLE_MAX = 110

# 边缘采样竖带距客户区左右边的距离，以及竖带宽度。取边缘是因为那里没有被
# 控件盖住，是纯底板；用户看到的"磨砂灰"就是这个值。
_EDGE_INSET = 8
_EDGE_BAND = 3

# 复用的截屏实例。不要每次采图都新建：`open_screen()` 的构造有实打实的代价
# （本机实测首次 44ms、复用后稳定 12ms），而这段代码跑在拖动路径上。
# 更进一步：`warm_up_screen()` 在启动时就构造好，把这个代价彻底挪出拖动路径。
_sct = None


def _screen():
    """懒加载并复用截屏实例（失败则返回 None，由调用方回退）。"""
    global _sct
    if _sct is None:
        # ⚠️ 必须是**绝对**导入：本文件从参考项目的 `src/ui/` 移植而来，
        # 那里 `..core.screen` 恰好成立；本项目 `ui/` 是顶层包，相对导入会
        # 抛 `attempted relative import beyond top-level package`，
        # 于是截屏静默失败 → `capture_window_surface()` 恒返回 None →
        # 移动期拿不到定格快照、只剩一块纯色（正是"移动时毛玻璃就没了"）。
        from core.screen import open_screen

        _sct = open_screen()
    return _sct


def warm_up_screen() -> None:
    """提前构造截屏实例（可选优化）。

    ⚠️ **主窗口路径当前不调用它** —— `GlassWindowMixin` 的拖动让路走"半透明
    底板"，全程不抓屏。只有 `GlassDialogMixin` 的对话框路径会用
    `capture_window_surface()`，那里若担心首次 44ms 的 mss 构造落在拖动帧上，
    可以在启动期主动调一次本函数预热。

    失败一律吞掉：拿不到屏幕不应该妨碍程序启动（真正的截屏路径各自有兜底）。
    """
    try:
        _screen()
    except Exception:
        pass


def _edge_color(pixels: bytes, width: int, height: int) -> str | None:
    """从快照里取左右内侧窄竖带的中位色，即"用户眼中的静止底板色"。

    静止观感 = Acrylic 的 tint(70% 深色) 叠在**模糊后的桌面**上，所以它随壁纸
    而变，写死主题深色会让移动瞬间从"磨砂灰"跳成"纯黑"（实测静止
    `RGB(85,82,81)`，而主题色只有 `RGB(27,29,33)`）。用中位数而不是均值来抵抗
    个别像素（描边、圆角过渡）的干扰。
    """
    if not pixels or width <= 0 or height <= 0:
        return None
    # 左右各一条 _EDGE_BAND 宽的竖带（不是单列）：带内取中位数才能如注释所说
    # 抵抗描边、圆角过渡像素的干扰，单列采样一被 1px 描边命中就整体偏色。
    xs = [x for x in range(_EDGE_INSET, _EDGE_INSET + _EDGE_BAND) if 0 <= x < width]
    xs += [
        x
        for x in range(width - _EDGE_BAND - _EDGE_INSET, width - _EDGE_INSET)
        if 0 <= x < width
    ]
    if not xs:
        return None
    samples = []
    for y in range(height):
        row = y * width
        for x in xs:
            i = (row + x) * 4          # BGRA
            samples.append((pixels[i + 2], pixels[i + 1], pixels[i]))
    if not samples:
        return None

    def median(channel: int) -> int:
        vals = sorted(p[channel] for p in samples)
        return vals[len(vals) // 2]

    rgb = [max(_SAMPLE_MIN, min(_SAMPLE_MAX, median(i))) for i in range(3)]
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def capture_window_surface(hwnd: int) -> SurfaceShot | None:
    """趁原生模糊还在，抓一张窗口客户区的**真实屏幕像素**。

    为什么需要它：移动期必须撤掉会迟滞的 Win10 Acrylic（见下方 OS bug 说明），
    而 Qt 自己补的底板只能是一块**纯色** —— 用户看到的就是"毛玻璃效果直接没有
    了，也只有颜色"。所以这里在撤之前把窗口此刻的样子整体拍下来（模糊后的桌面
    + 深色 tint + 控件本身），移动期间由 Qt 把这一张**定格图**画回窗口：拖动全程
    与静止时看起来一模一样，而系统那边只是在合成一个普通窗口（跟手、无迟滞）。

    为什么定格不会露馅：Acrylic 的模糊是极低频的，定格的模糊背景与实时模糊在
    视觉上无法区分；Windows 自己对付"卡住的窗口"用的也是同一招（ghost window）。

    代价：一次 BitBlt + 一次中位色统计，本机实测约 12ms，且**每次拖动只做一次**、
    不是每帧（拖动过程中不需要再拍）。

    抓屏一律分区一次 BitBlt（下面这一句 `sct.grab`），绝不逐像素读 —— 细节见
    函数体内的注释与 `tests/test_screen.py` 的静态守卫。

    失败一律返回 None，由调用方回退到主题底板色。
    """
    rect = _client_rect(hwnd)
    if rect is None:
        return None
    left, top, width, height = rect
    if width < 40 or height < 40:
        return None
    # ⚠️ 绝不要改成逐像素读（GetPixel）：屏幕 DC 上每次调用都会强制同步一次桌面
    # 合成，实测**恰好一帧**（本机 12.12ms，中位=最大）。逐像素读一块 3×36 的窄带
    # 就是 108 × 12ms ≈ 1.3s，而这段跑在 GUI 线程上，现象就是整个程序卡死。
    # 分区一次 BitBlt（下面这句）约 12ms。静态守卫同上。
    try:
        sct = _screen()
        if sct is None:
            return None
        raw = bytes(sct.grab({
            "left": left, "top": top, "width": width, "height": height,
        }).raw)
    except Exception:
        # 实例可能已失效（分辨率变化等）：丢掉缓存，下次重建。
        global _sct
        _sct = None
        return None
    if len(raw) < width * height * 4:
        return None
    return SurfaceShot(width, height, raw, _edge_color(raw, width, height))


def shot_to_pixmap(shot: SurfaceShot):
    """把快照转成可绘制的 QPixmap（失败返回 None）。

    直接复用 BGRA 字节：Qt 的 `Format_RGB32` 在 little-endian 上的内存布局就是
    B,G,R,X，与 mss 的输出一致，不需要逐像素转换。必须 `copy()` —— QImage 只是
    引用这块内存，而 bytes 随时可能被回收。
    """
    try:
        from PySide6.QtGui import QImage, QPixmap
    except Exception:
        return None
    if not shot.pixels:
        return None
    image = QImage(shot.pixels, shot.width, shot.height,
                   QImage.Format.Format_RGB32).copy()
    if image.isNull():
        return None
    return QPixmap.fromImage(image)


def apply_glass(hwnd: int) -> bool:
    """给窗口应用原生毛玻璃。

    返回 True 表示原生模糊生效；False 表示环境不支持，调用方应降级自绘玻璃。
    """
    if not _IS_WINDOWS or not hwnd:
        return False
    # Win11 22H2 Acrylic → Win11 Mica → Win10 Acrylic
    if _apply_win11_acrylic(hwnd):
        return True
    if _apply_win11_mica(hwnd):
        return True
    return _apply_win10_acrylic(hwnd)


# ---------- Windows 10 的"移动期 Acrylic 迟滞" ----------
# 这是**未修复的 OS bug**，不是本项目的代码问题：Win10 1903+ 上用
# SetWindowCompositionAttribute 挂 ACCENT_ENABLE_ACRYLICBLURBEHIND 之后，
# 窗口被拖动/缩放时会严重滞后于鼠标（高轮询率鼠标更明显，松手后窗口还会
# "追尾"，期间 CPU 飙升），观感就是"拖不动、有奶油感"。
# 三个独立来源可复现：EarTrumpet #349（该方案原始文档作者本人确认是 OS bug）、
# FluentWPF #42、DevToys #1258（Win10 19045 上甚至完全不渲染却照样卡）。
#
# 缓解办法是移动期**把硬模糊整个撤掉**（`suspend()`），同时由调用方把撤之前
# 拍下的**定格快照**画回窗口（`capture_window_surface()`）——只撤不补就是用户
# 反馈的"移动时毛玻璃效果直接没有了，也只有颜色"。
#
# ⚠️ 别用 ACCENT_ENABLE_BLURBEHIND"降级"（这是 FluentWPF 推荐的做法，但在本机无效）。
# 实测（Win10 19041 / 纯白衬底 / 抓真实屏幕，窗口底板样区）：
#     ACRYLICBLURBEHIND(4) + tint       → RGB(85,82,81)  深灰磨砂（正常）
#     BLURBEHIND(3)  tint=0             → RGB(255,255,255) 纯白
#     BLURBEHIND(3)  tint=0xB31B1D21    → RGB(255,255,255) 纯白（不吃 GradientColor）
#     BLURBEHIND(3)  tint=0xE61B1D21    → RGB(255,255,255) 纯白
#     之后重新 apply_glass()            → RGB(255,255,255) 切不回 Acrylic
# 即：BLURBEHIND 在本机会把窗口整块变成纯白、且恢复不了（用户反馈的"拖动时变纯白"）。
# 撤掉(ACCENT_DISABLED) + 不透明底板的实测值是 RGB(27,29,33)，稳定不发白——所以走它。
# 是否启用 Windows 原生模糊（Acrylic / Mica）。
#
# **默认关闭，且这是当前的产品决策**。原生模糊会拖出一串无法根治的问题：
#   · Win10 挂着 ACRYLICBLURBEHIND 时拖动严重迟滞（OS bug，见本节开头），
#     只能在拖动期把它临时撤掉；
#   · 撤掉之后窗口就没背景了，得靠半透明底板或定格快照兜底；
#   · 恢复又是**异步**的（DWM 要一整帧才画出来），松手时容易闪一下；
#   · 于是"静止模糊 ↔ 拖动不模糊"这个**状态切换本身**成了观感不连贯的源头 ——
#     用户的评价是"不太有连贯性"。
#
# 关掉之后：全程只有一种形态（半透明底板 `theme.GLASS_BASE_ALPHA`），
# 没有任何 `suspend`/`resume`，也就没有迟滞、闪烁、恢复延迟。
# 代价是没有背景模糊 —— 但 Win10 上"实时模糊 + 流畅拖动"本来就不可兼得。
#
# 想开回来：改成 True 即可。让路机制（`_enter_drag_mode` / `_restore_glass` /
# `_finish_restore`）与相关守卫都完整保留着，不需要重写。
USE_NATIVE_BLUR = False

# 松手之后多久开始恢复硬模糊（毫秒）。
#
# ⚠️ 这个值**直接决定用户能不能感觉到"模糊没了"**：松手到模糊回来 = 本值 +
# `_RESTORE_GRACE_MS`。原先是 260ms，用户的实际反馈是"你动了之后，模糊效果
# 会消失"—— 300ms 的空白足以被感知成"模糊没恢复"。
#
# 260ms 当初是为了合并"松手后立刻又拖"的抖动。但现在让路只做一次样式表变更
# （实测 4.72ms，不再抓屏），频繁切换的代价很低，没必要为它留这么长的窗口。
# 40ms 仍能覆盖"同一次拖动中的抖动"（人手松手到再次按下通常 > 60ms），
# 同时把总恢复延迟压到 ~80ms —— 低于"能感觉到模糊没了"的阈值。
_MOVE_SETTLE_MS = 40
_ARM_DELAY_MS = 450     # 开窗落位后多久才启用移动期让路（否则开窗时会闪一下）

# `resume()` 之后到"撤掉快照/底板转透明"之间的等待（毫秒）。
#
# `resume()` 只是把模糊意图交给 DWM，真正画出来要一整帧；而 Qt 的底板/快照
# 可以立刻消失。不隔开就会露出一帧"透明底板 + 还没画好的 Acrylic"—— 用户
# 看到的正是**松手时闪一下**。取两帧（32ms）留足余量；这期间画面完全不变，
# 用户感知不到等待。
_RESTORE_GRACE_MS = 32

# 判定"点击"还是"拖动"的位移阈值（像素）。
#
# 太小的代价很直接：点击时手指必然有几像素抖动，阈值一旦偏小，"点一下"就会被
# 当成"拖动"—— 每轮误触发一次让路（含 13ms 抓屏 + 2.7ms repaint）+ 一次
# 窗口位移，表现就是"点一下卡一下，然后它跳一下"。
# 8px 在 100% 与 200% 缩放下都能把两者稳稳分开，且不影响正常拖拽起手。
_DRAG_THRESHOLD = 8


class _GlassDialogSurface(QWidget):
    """对话框自己的玻璃底板。

    对话框不能像普通 QWidget 那样直接在 self 上画 QSS 背景：顶层窗口设了
    `WA_TranslucentBackground` + `FramelessWindowHint` 后，Qt 不会再绘制顶层
    的 QSS `background`（offscreen 实测 grab() 为全透明）。因此背景单独放一个
    子控件，原生模糊生效时它透明、降级/移动期它是不透明底色，内容控件照常
    叠在它上面。
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._freeze = None
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)

    def set_freeze(self, pixmap) -> None:
        """设置 / 清除移动期的定格快照。"""
        self._freeze = pixmap
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        super().paintEvent(event)
        pixmap = self._freeze
        if pixmap is None:
            return
        from . import theme

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        radius = float(theme.R_PANEL)
        path.addRoundedRect(QRectF(self.rect()), radius, radius)
        painter.setClipPath(path)
        painter.drawPixmap(0, 0, pixmap)
        painter.end()


class GlassDialogMixin:
    """给任意 QDialog 提供一致的毛玻璃观感（原生成功则透背景，否则降级）。

    对话框采用 frameless + WA_TranslucentBackground，并使用自绘标题栏：
    Win10 原生非客户区无法透明，且移动期撤掉 accent 后会被 DWM 画成纯黑；
    自绘标题栏既避开这个合成问题，也让窗口名与主面板的透明玻璃面一致。

    用法：
        class MyDialog(GlassDialogMixin, QDialog):
            def __init__(self):
                super().__init__()
                self.setup_glass()

            # 需要记住窗口位置时：
                self.setup_glass(db=db, state_key=window_state.WINDOW_TASK_DIALOG)
    """

    def setup_glass(self, db=None, state_key: str = "") -> None:
        """在 __init__ 末尾调用：装好降级样式，首次显示时再尝试原生模糊。

        传 `db` + `state_key` 即启用**窗口位置记忆**（需要本项目提供
        `ui.window_state`；不传则该功能整体关闭，毛玻璃不受影响）。
        显示时还原上次的位置，隐藏时记下当前位置。
        """
        from PySide6.QtCore import Qt, QTimer

        from . import theme

        self.setObjectName("glassDialog")
        # 无边框 + 半透明是原生 Acrylic 能透出来的前提，也让标题区与主面板
        # 坐在同一层玻璃上。Win10 原生非客户区无法透明，而且移动期撤 accent
        # 后会被 DWM 画成纯黑，所以对话框不再保留系统标题栏，改自绘。
        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 顶层窗口在部分平台不会绘制自己的 QSS background，所以背景必须由
        # 一个子控件承担；它也是降级态/移动期定格快照的画布。
        self._dialog_surface = _GlassDialogSurface(self)
        self._dialog_surface.setObjectName("glassDialogSurface")
        self._dialog_surface.lower()
        self._build_titlebar()
        if hasattr(self, "setSizeGripEnabled"):
            self.setSizeGripEnabled(True)   # 无边框后仍保留右下角缩放能力
        # 先按"无原生模糊"着色：即使原生模糊永远失败，观感也一致且文字可读
        self.setStyleSheet(theme.dialog_qss("opaque"))
        # ---- 毛玻璃状态机 ----
        # `_glass_qss_mode` 是**唯一**的底板样式状态（"glass" / "opaque"），不拆成
        # 多个布尔量：第五轮"收回背景消失"就是状态漏组合造成的，用一个变量表示
        # 可以让非法组合根本无法表达。定格快照是画在 `_dialog_surface` 上的一层，
        # 不参与这个状态。
        self._glass_resolved = False    # 是否已尝试过原生模糊（只做一次）
        self._glass_on = False          # 原生模糊当前是否生效
        self._glass_qss_mode = "opaque"
        self._glass_qss_bg = None       # 当前底板用的色（None = 主题默认）
        self._glass_freeze = None       # 移动期的定格快照（QPixmap）
        self._glass_move_armed = False  # 开窗落位前不响应 move/resize
        self._glass_shown_at = 0.0
        self._glass_geo = None          # 上次处理过的几何，挡掉纯重排事件
        self._glass_settle = QTimer(self)
        self._glass_settle.setSingleShot(True)
        self._glass_settle.setInterval(_MOVE_SETTLE_MS)
        self._glass_settle.timeout.connect(self._restore_glass)
        # ---- 窗口位置记忆 ----
        self._state_db = db
        self._state_key = state_key if db is not None else ""
        self._state_restored = False

    def _build_titlebar(self) -> None:
        """创建自绘标题栏：窗口名 + 关闭按钮，并接管它的拖动。"""
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QSizePolicy, QWidget

        from . import theme
        from .icons import IconButton

        bar = QWidget(self)
        bar.setObjectName("glassTitleBar")
        bar.setFixedHeight(theme.DIALOG_TITLEBAR_H)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 8, 0)
        lay.setSpacing(8)

        self._title_label = QLabel(self.windowTitle(), bar)
        self._title_label.setObjectName("glassTitleLabel")
        # 点击标题文字也要能拖动窗口，所以让鼠标事件落到标题栏本身。
        self._title_label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self._title_label.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        lay.addWidget(self._title_label, 1)

        self._close_btn = IconButton("close", box=28, icon=14, parent=bar)
        self._close_btn.setToolTip("关闭")
        self._close_btn.clicked.connect(self.close)
        lay.addWidget(self._close_btn, 0, Qt.AlignmentFlag.AlignVCenter)

        self._title_bar = bar
        self._title_drag_pos = None
        bar.installEventFilter(self)
        self._title_label.installEventFilter(self)

    def setWindowTitle(self, title: str) -> None:  # noqa: N802
        """同步系统窗口名与自绘标题文字。"""
        super().setWindowTitle(title)
        label = getattr(self, "_title_label", None)
        if label is not None:
            label.setText(title)

    def _sync_chrome(self) -> None:
        """让底板铺满、标题栏贴顶，并给内容布局让出顶部空间。"""
        surface = getattr(self, "_dialog_surface", None)
        if surface is not None:
            surface.setGeometry(0, 0, self.width(), self.height())
            surface.lower()
        bar = getattr(self, "_title_bar", None)
        if bar is None:
            return
        bar.setGeometry(0, 0, self.width(), bar.height())
        bar.raise_()
        layout = self.layout()
        if layout is None:
            return
        margins = layout.contentsMargins()
        if margins.top() < bar.height():
            layout.setContentsMargins(
                margins.left(), bar.height(), margins.right(), margins.bottom())

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """拖动自绘标题栏移动窗口（只处理标题区，关闭按钮走自己的点击）。"""
        title_bar = getattr(self, "_title_bar", None)
        title_label = getattr(self, "_title_label", None)
        if obj is title_bar or obj is title_label:
            from PySide6.QtCore import QEvent, Qt

            et = event.type()
            if (et == QEvent.Type.MouseButtonPress
                    and event.button() == Qt.MouseButton.LeftButton):
                self._title_drag_pos = (
                    event.globalPosition().toPoint() - self.frameGeometry().topLeft())
                return True
            if (et == QEvent.Type.MouseMove
                    and self._title_drag_pos is not None
                    and event.buttons() & Qt.MouseButton.LeftButton):
                self.move(event.globalPosition().toPoint() - self._title_drag_pos)
                return True
            if (et == QEvent.Type.MouseButtonRelease
                    and event.button() == Qt.MouseButton.LeftButton):
                self._title_drag_pos = None
                return True
        return super().eventFilter(obj, event)

    def showEvent(self, event) -> None:  # noqa: N802
        self._sync_chrome()
        super().showEvent(event)
        self._sync_chrome()
        import time

        self._glass_shown_at = time.monotonic()
        self._glass_geo = None
        if self._state_key and not self._state_restored:
            # 首次显示时布局已经定型，此刻还原位置最准（构造期尺寸还没定）。
            # 本移植版不引入 window_state 模块：位置记忆与毛玻璃无关，
            # 不传 db/state_key 时这段完全不会执行。
            self._state_restored = True
            try:
                from . import window_state  # type: ignore[attr-defined]
            except ImportError:
                pass
            else:
                window_state.restore(self._state_db, self._state_key, self)
                self._glass_geo = None      # 还原动作会引发 moveEvent，别让它算作用户拖动
        if self._glass_resolved:
            return
        self._glass_resolved = True
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        if USE_NATIVE_BLUR and apply_glass(hwnd):
            # 原生模糊已在窗口后方生效 → 底板转透明，让系统画的 Acrylic
            # 真正透出来（与主面板、GlassMenu 同一条路径）。
            self._glass_on = True
            self._apply_dialog_qss("glass")
            self._glass_move_armed = True

    def hideEvent(self, event) -> None:  # noqa: N802
        super().hideEvent(event)
        if not self._state_key:
            return
        try:
            from . import window_state  # type: ignore[attr-defined]
        except ImportError:
            return
        window_state.remember(self._state_db, self._state_key, self)

    def paintEvent(self, event) -> None:  # noqa: N802
        """顶层只负责透明；实际底板/定格快照画在 `_dialog_surface`。"""
        super().paintEvent(event)

    def _apply_dialog_qss(self, mode: str, bg: str | None = None) -> None:
        """幂等切换底板：透明（透出原生 Acrylic）/ 不透明（自己画底）。

        `bg` 是移动期用的底板色（来自定格快照的边缘采样），所以幂等键必须同时
        含颜色——否则"模式没变但颜色该换"会被漏掉。它只是快照之下的兜底：
        万一快照没画上，窗口也不会变成透明。

        必须幂等：本方法会被 moveEvent 频繁调用，而 `setStyleSheet` 会重新
        polish 整个对话框子树，重复调用既浪费又可能触发连锁重排。
        """
        if self._glass_qss_mode == mode and self._glass_qss_bg == bg:
            return
        from . import theme

        self._glass_qss_mode = mode
        self._glass_qss_bg = bg
        self.setStyleSheet(theme.dialog_qss(mode, bg))

    def _set_freeze(self, shot: SurfaceShot | None) -> None:
        """设置 / 清除移动期的定格快照。"""
        pixmap = shot_to_pixmap(shot) if shot is not None else None
        if pixmap is not None:
            # 快照按物理像素抓的，换算成逻辑尺寸铺满控件
            pixmap.setDevicePixelRatio(self.devicePixelRatioF() or 1.0)
        self._glass_freeze = pixmap
        surface = getattr(self, "_dialog_surface", None)
        if surface is not None:
            surface.set_freeze(pixmap)
        else:
            self.update()

    def _repaint_surface(self) -> None:
        """同步落地底板重绘（QSS 变更本身是异步的，拖动帧不能等）。"""
        surface = getattr(self, "_dialog_surface", None)
        if surface is not None:
            surface.repaint()
        else:
            self.repaint()

    def _freeze_stale(self) -> bool:
        """快照尺寸是否已经和当前客户区对不上（拖动中被缩放）。"""
        pixmap = self._glass_freeze
        if pixmap is None:
            return False
        ratio = pixmap.devicePixelRatio() or 1.0
        return (round(pixmap.width() / ratio), round(pixmap.height() / ratio)) \
            != (self.width(), self.height())

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self._throttle_glass()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_chrome()
        self._throttle_glass(resized=True)

    def _throttle_glass(self, *, resized: bool = False) -> None:
        """窗口被移动/缩放时撤掉 Acrylic —— Win10 上它是迟滞元凶。

        撤之前先拍一张定格快照（`capture_window_surface`）并把它画回窗口，
        这样拖动过程中玻璃观感仍在（用户反馈的"移动时毛玻璃直接没了、只剩颜色"
        就是缺了这一步）。每次几何变化都重置计时，因此只有真正停下来之后才会
        恢复 Acrylic。

        不要试图改成 ACCENT_ENABLE_BLURBEHIND（FluentWPF 的做法）——本机实测
        它输出纯白且切不回来，见文件上方实测记录。
        """
        import time

        from . import theme

        if not self._glass_on or not self._glass_move_armed:
            return
        geo = self.geometry()
        if geo == self._glass_geo:
            return      # 纯重排（样式表变更引起的）不算"在移动"，否则会来回抖
        self._glass_geo = geo
        if time.monotonic() - self._glass_shown_at < _ARM_DELAY_MS / 1000:
            return      # 开窗落位阶段：此时撤模糊会看见"闪一下不透明"
        if self._glass_qss_mode == "glass" or self._freeze_stale():
            try:
                hwnd = int(self.winId())
            except Exception:
                return
            # 拖动中被缩放 → 旧快照尺寸对不上，先丢掉（宁可退成纯色，也不画歪）。
            # 缩放得不到可用快照，所以只画纯色；等本轮拖动结束、下次拖动再重拍。
            self._set_freeze(None)
            shot = None if resized else capture_window_surface(hwnd)
            # ⚠️ 必须赶在 suspend() 之前拍：此刻系统还在画模糊，快照里才是
            # "用户眼中的静止观感"。撤掉之后就只能拍到自绘底板了。
            bg = (shot.color if shot else None) or theme.GLASS_BASE
            # ⚠️ 顺序是正确性的一部分，与主面板一致：先把底板/快照画上去并同步
            # 落地，**再**撤系统那层 Acrylic。反过来就是"系统不画 + Qt 还没画"
            # 的一帧 —— 对话框会露出窗口底色（黑）。
            self._set_freeze(shot)
            self._apply_dialog_qss("opaque", bg)
            self._repaint_surface()
            try:
                suspend(hwnd)
            except Exception:
                pass
        self._glass_settle.start()

    def _restore_glass(self) -> None:
        """窗口停止移动后恢复硬模糊，并撤掉定格快照。"""
        if not self._glass_on or self._glass_qss_mode == "glass":
            return      # 未启用原生模糊，或本来就在玻璃态（重复恢复）→ 无事可做
        from PySide6.QtCore import Qt
        from PySide6.QtWidgets import QApplication

        if QApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            # 拖动中途只是手停下来（鼠标仍按住）：不能恢复模糊，否则 Win10
            # 迟滞回归，要等下一次 moveEvent 才重新撤掉+重拍快照（还带一次闪烁）。
            # 延后到松手后的 settle 再恢复。
            self._glass_settle.start()
            return
        try:
            hwnd = int(self.winId())
        except Exception:
            return
        if resume(hwnd):
            # 先把系统背景装回来，再撤快照/底板转透明：此刻顶多一帧"双背景"，
            # 远轻于"无背景"。
            self._set_freeze(None)
            self._apply_dialog_qss("glass")
            self._repaint_surface()
        # 恢复失败 → 保持当前底板与快照：宁可少一层模糊，也不能让底板消失。
        # 把几何基线推到当前值：Windows 的拖动循环退出后还会补投一批 moveEvent，
        # 它们携带的已经是最终几何，跟基线一比就成了"又移动了"——不更新的话刚恢复
        # 就被再次撤掉，窗口会一直停在无模糊的纯色底板上。
        self._glass_geo = self.geometry()


def suspend(hwnd: int) -> None:
    """移动/拖动开始：临时关闭原生模糊。

    关掉的是 Win10 上那个会导致拖动迟滞的 ACCENT_ENABLE_ACRYLICBLURBEHIND
    （见本文件顶部的 OS bug 说明）。**撤背景之前，调用方必须先让 Qt 把不透明
    底板画上去并同步落地**，否则会出现"系统不画、Qt 也没画"的透明帧。
    """
    if not _IS_WINDOWS or not hwnd:
        return
    if _dwm_set(hwnd, _DWMWA_SYSTEMBACKDROP_TYPE, _DWMSBT_NONE):
        return
    _disable_win10_acrylic(hwnd)


def resume(hwnd: int) -> bool:
    """移动/拖动结束：恢复原生模糊。返回 True 表示已恢复。"""
    return apply_glass(hwnd)


def apply_opacity(app, pct: int) -> None:
    """把新的底板不透明度应用到**所有已打开的窗口**，并立刻重绘。

    两步缺一不可：
    1. `theme.set_glass_alpha_percent` —— 更新模块级值（之后新建的窗口会用它）；
    2. 逐个窗口重设 `_glass_surface` 的样式表 —— 底板样式是**显式传参**设的
       （`glass_surface_qss(theme.GLASS_BASE_ALPHA)`），只改模块级变量不会自动
       生效。这是"改了设置但界面没变"最容易踩的一处。

    单窗口成本约 0.04 ms（单个控件 `setStyleSheet` 的实测值），窗口数量是个位
    数，所以设置窗口保存时同步做完即可，不需要异步或防抖。
    """
    from . import theme

    theme.set_glass_alpha_percent(pct)
    for win in app.topLevelWidgets():
        surface = getattr(win, "_glass_surface", None)
        if surface is None:
            continue
        surface.setStyleSheet(theme.glass_surface_qss(theme.GLASS_BASE_ALPHA))
        surface.repaint()


# ---------- 通用接入（QMainWindow 子窗口用） ----------

class GlassWindowMixin:
    """给 QMainWindow 子窗口一套完整的毛玻璃 + 无边框能力。

    与 `GlassDialogMixin` 的分工：那个自带自绘标题栏、只适合 QDialog；
    本 mixin 假定调用方的标题栏**已在布局里**（子窗口通常把窗口名放进内容区），
    所以只补三件缺的事：去边框、玻璃底板、按住空白拖动窗口。

    用法（三步，顺序不能换）：
        class MyWindow(GlassWindowMixin, QMainWindow):
            def _build_ui(self):
                ...
                self.init_glass(central)      # ① 建底板 + 去边框
            def showEvent(self, e):
                super().showEvent(e)
                self.enable_glass()            # ② show 之后才能拿到 hwnd

        # ③ 给承载「空白拖动区」的控件装过滤器：
        #    win.installEventFilter(win) 或让该控件 WA_TransparentForMouseEvents
    """

    def init_glass(self, central: QWidget, *, edge_margin: int = 6) -> None:
        """① 建玻璃底板并去掉系统边框。必须在布局搭好之后调用。

        底板挂在**窗口**上而不是 centralWidget：QMainWindow 除了 central 还会有
        状态栏等区域，只铺 central 会让状态栏落在玻璃之外、露成一块纯黑
        （实测：状态栏 y=590-620 完全没底板）。
        """
        import time

        from PySide6.QtCore import QTimer

        from . import theme

        self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._glass_surface = _GlassDialogSurface(self)
        self._glass_surface.setObjectName("glassSurface")
        self._glass_surface.lower()
        self._glass_surface.setStyleSheet(
            theme.glass_surface_qss(theme.GLASS_BASE_ALPHA))
        self._glass_surface.setGeometry(self.rect())
        # 记住 central 只为 resize 时对齐参考；底板本身跟随窗口尺寸
        self._glass_central = central
        # ---- 移动期让路（Win10 Acrylic 拖动迟滞的 OS bug） ----
        #
        # 只用两个状态：`_glass_suspended`（当前是否已让路）与 `_dragging`
        # （用户是否正在拖窗口）。早期版本还维护了 `_glass_freeze` / `_glass_geo`
        # / `_glass_shown_at` / `_glass_move_armed` 四个字段来支撑"定格快照 +
        # 几何去抖 + 开窗落位延迟"，但那条路已经被证明是卡顿的来源，一并删掉。
        self._glass_suspended = False   # 原生模糊当前是否被撤下
        # 恢复分两步：`_restore_timer` 到期后才 `_finish_restore`（撤遮挡）。
        # `_restore_pending` 挡住等待期间的重复调用。
        self._restore_pending = False
        self._restore_timer = QTimer(self)
        self._restore_timer.setSingleShot(True)
        self._restore_timer.setInterval(_RESTORE_GRACE_MS)
        self._restore_timer.timeout.connect(self._finish_restore)
        self._glass_settle = QTimer(self)
        self._glass_settle.setSingleShot(True)
        self._glass_settle.setInterval(_MOVE_SETTLE_MS)
        self._glass_settle.timeout.connect(self._restore_glass)
        # ⚠️ 这里**不**预热截屏实例：主窗口的拖动让路走"半透明底板"，
        # 全程不抓屏（`capture_window_surface` 只留给 `GlassDialogMixin` 的
        # 对话框路径）。旧版用定格快照时要预热 mss（首次构造 ~44ms），现在
        # 那是纯浪费启动时间。
        # 无边框窗口没有系统标题栏那条天然留白，状态栏文字会直接贴着下边缘，
        # 看起来像被切掉一截。QSS 的 padding 对 QStatusBar 无效（实测改了
        # 渲染毫无变化），只能走 contentsMargins —— 三个窗口都受影响，
        # 所以放在 mixin 里统一处理，而不是让每个窗口各写一遍。
        try:
            bar = self.statusBar()
            bar.setContentsMargins(12, 4, 12, 4)
        except Exception:
            pass

        self._glass_hwnd: int | None = None
        self._glass_on = False
        self._edge_margin = edge_margin
        self._resize_edge = 0
        self._resize_origin = None
        self._press_pos = None          # 按下点（窗口本地坐标）
        self._press_global = None       # 按下点的全局坐标
        self._drag_origin = None        # 按下那一刻的窗口左上角
        self._dragging = False          # 是否已确认进入拖动
        self._title_drag_pos = None
        # ⚠️ 刻意**不**重置 `_btn_max` / `_title_bar`：它们由 build_title_bar /
        # build_window_buttons 在**布局构建期**建立，而 init_glass 是在 _build_ui
        # 之后才被调用的（三个窗口都按「先搭布局、再挂玻璃」的顺序）。在这里清空
        # 会把刚建好的标题栏引用抹掉，症状是标题栏还在界面上、但窗口再也不会
        # 响应它的拖动与双击（eventFilter 认不出 _is_titlebar 之外的状态）。
        # 想清空就在构造期清，这里只补 mixin 自己需要的状态。

    # ---------- 窗口控制按钮（无边框的必答题） ----------

    def build_title_bar(self, title: str, *, maximize: bool = False,
                        box: int = 26, icon: int = 13):
        """子窗口的顶部标题栏：窗口名在左，窗口控制按钮在右。

        子窗口（设置 / 密码库 / 工作目录）与主窗口的布局结构不同 —— 它们没有
        主窗口那条"标题 + 副标题 + 开关 + 按钮"的头部，塞不进同一处。与其改
        三个窗口的布局去迁就，不如给它们一条统一的标题栏：结构一致、行为一致，
        也让"按住这里拖窗口"有个明确的落点（比在任意空白处都能拖更可预期）。
        """
        from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 6, 8, 6)
        lay.setSpacing(8)

        name = QLabel(title, bar)
        name.setStyleSheet(
            f"color: {self._title_color()}; background: transparent; font-weight: 500;")
        # 标题文字不吃鼠标事件，事件落到 bar 上 —— 这样按住文字也能拖窗口，
        # 而不是出现"文字处拖不动、旁边空白能拖"的怪现象。
        name.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        lay.addWidget(name)
        lay.addStretch(1)
        lay.addWidget(self.build_window_buttons(minimize=False, maximize=maximize,
                                                 box=box, icon=icon))

        # 标题栏本身接管拖动（双击 = 最大化/还原，与所有桌面应用一致）
        bar.installEventFilter(self)
        bar._is_titlebar = True
        self._title_bar = bar
        return bar

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        """标题栏拖动 + 双击切换最大化。

        只处理标题栏上的事件；其余对象一律放行。区分「按下」与「按下后移动」
        是为了不影响按钮点击 —— 按钮自己会 consume 掉 mousePress。
        """
        from PySide6.QtCore import QEvent

        if not getattr(obj, "_is_titlebar", False):
            return super().eventFilter(obj, event)

        et = event.type()
        if et == QEvent.Type.MouseButtonPress \
                and event.button() == Qt.MouseButton.LeftButton:
            self._title_drag_pos = (event.globalPosition().toPoint()
                                    - self.frameGeometry().topLeft())
            return True
        if et == QEvent.Type.MouseMove and self._title_drag_pos is not None \
                and event.buttons() & Qt.MouseButton.LeftButton:
            self.move(event.globalPosition().toPoint() - self._title_drag_pos)
            return True
        if et == QEvent.Type.MouseButtonRelease:
            self._title_drag_pos = None
            return True
        if et == QEvent.Type.MouseButtonDblClick \
                and event.button() == Qt.MouseButton.LeftButton:
            self._title_drag_pos = None
            self._toggle_maximize()
            return True
        return super().eventFilter(obj, event)

    @staticmethod
    def _title_color() -> str:
        from . import theme

        return theme.TEXT

    def build_window_buttons(self, *, minimize: bool = True,
                             maximize: bool = True,
                             box: int = 28, icon: int = 14):
        """生成窗口控制图标按钮，横向排布。缺哪个由参数决定。

        无边框窗口没有系统标题栏，于是这些按钮就成了**用户唯一能看见的
        窗口控制入口** —— 不补上，窗口就没法关。所以它是玻璃化的必答题，
        不是可选装饰。

        `minimize/maximize=False` 用于设置这类子窗口：它们尺寸固定，
        最小化没有意义（只是把它藏起来，反而让用户找不到），只给「关闭」
        更干净，也让用户一眼看出这个按钮会关掉窗口而不是藏起来。

        返回可直接塞进布局的 QWidget（已 setLayout）。
        """
        from PySide6.QtWidgets import QHBoxLayout, QWidget

        from .icons import IconButton

        bar = QWidget()
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)      # 贴得比常规间距紧，读起来才像一个按钮组

        btn_min = None
        if minimize:
            btn_min = IconButton("minimize", box=box, icon=icon, parent=bar)
            btn_min.setObjectName("winCtl")
            btn_min.setToolTip("最小化")
            btn_min.clicked.connect(self.showMinimized)
            lay.addWidget(btn_min)

        btn_max = None
        if maximize:
            btn_max = IconButton("maximize", box=box, icon=icon, parent=bar)
            btn_max.setObjectName("winCtl")
            btn_max.setToolTip("最大化")
            btn_max.clicked.connect(self._toggle_maximize)
            lay.addWidget(btn_max)

        btn_close = IconButton("close", box=box, icon=icon, parent=bar)
        btn_close.setObjectName("winCtl")
        btn_close.setProperty("danger", "true")
        btn_close.setToolTip("关闭")
        btn_close.clicked.connect(self.close)
        lay.addWidget(btn_close)

        self._btn_max = btn_max
        return bar

    def _toggle_maximize(self) -> None:
        """最大化 / 还原来回切，并同步图标（最大化时应显示"还原"）。"""
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()
        self._sync_maximize_icon()

    def _sync_maximize_icon(self) -> None:
        """窗口最大化状态变化时更新按钮图标与提示。"""
        from .icons import IconButton

        btn = getattr(self, "_btn_max", None)
        if btn is None:
            return
        kind = "restore" if self.isMaximized() else "maximize"
        btn._kind = kind
        btn.setToolTip("还原" if self.isMaximized() else "最大化")
        btn.update()

    def changeEvent(self, event) -> None:  # noqa: N802
        """最大化状态由系统改变时同步图标。

        双击标题栏、Win+↑、拖到屏幕边缘等路径不走我们的按钮，
        必须在这里补一刀，否则图标会停在旧状态。
        """
        super().changeEvent(event)
        try:
            from PySide6.QtCore import QEvent
            if event.type() == QEvent.Type.WindowStateChange:
                self._sync_maximize_icon()
        except Exception:
            pass

    def enable_glass(self) -> bool:
        """② 应用原生模糊（幂等）。必须在 showEvent 之后调用。

        `USE_NATIVE_BLUR` 为 False（当前默认）时**什么都不做**：底板已经在
        `init_glass` 里铺成半透明（`theme.GLASS_BASE_ALPHA`），那就是最终形态。

        `_glass_on` 保持 False，于是让路逻辑（`_enter_drag_mode` /
        `_restore_glass`）自动全部失效 —— 这正是"全程观感一致"的实现方式：
        **不存在第二个状态，就没有切换可言**，也就没有迟滞/闪烁/恢复延迟。
        """
        from . import theme

        if not USE_NATIVE_BLUR:
            return False

        try:
            hwnd = int(self.winId())
        except Exception:
            return False
        if self._glass_hwnd == hwnd and self._glass_on:
            return True
        self._glass_hwnd = hwnd
        self._glass_on = apply_glass(hwnd)
        # apply_glass 已把系统背景装回来了，挂起状态随之失效
        self._glass_suspended = False
        # 原生生效 → 底板透明把 Acrylic 让出来；失败 → 保留自绘半透明底
        self._glass_surface.setStyleSheet(
            theme.glass_surface_qss(
                None if self._glass_on else theme.GLASS_BASE_ALPHA))
        return self._glass_on

    # ---- 移动期让路：Win10 Acrylic 拖动迟滞的 OS bug ----
    #
    # 实测（本机 Win10 19041）：**这段的耗时根本不在 Python 回调里**。挂上
    # ACRYLICBLURBEHIND 后拖动窗口，40 帧的全部回调加起来只有 0.59 ms（每帧
    # 0.04 ms），与撤掉 Acrylic 后基本无差别 —— 迟滞发生在 **DWM 合成器线程**
    # 的同步处理里，由 Windows 的 modal move loop 驱动，Qt 根本看不到。
    # 所以测 Python 侧的耗时永远得不出结论，只能按"已知 OS bug"处理。
    #
# 修法是移动期**把硬模糊整个撤掉**（`suspend()`）。撤掉之后窗口就没背景了，
# 于是底板转半透明（`GLASS_BASE_ALPHA`），让桌面**实时**透出来 —— 为什么不是
# "抓一张定格快照垫底"，见 `_enter_drag_mode` 的说明。

    def _enter_drag_mode(self) -> None:
        """拖动/缩放开始时让路：底板转半透明 + 撤掉系统硬模糊。

        **为什么是半透明、不抓快照**：撤掉 Acrylic 后窗口就没背景了，此时有两种
        收尾方式，用户明确选了后者：

        - **定格快照**（拖动前抓一张屏垫底）：背景会**冻住**不跟着窗口动，
          松手才更新 —— 实测用户的疑问正是"为什么背景没有实时更新啊"，
          而且起手要多付 12ms 抓屏（`sct.grab` 6.02 + 取色 1.18 + 转 QPixmap 0.76）。
        - **半透明底板**（当前）：桌面**实时**透出来，跟着窗口一起动；
          起手几乎零成本，只做一次 `setStyleSheet`（实测 0.036ms）。

        代价是拖动期没有模糊（Win10 做不到"实时模糊 + 流畅拖动"兼得，
        Acrylic 一旦挂着拖动就会严重迟滞）。透明度取 `GLASS_BASE_ALPHA`，
        与静止时的 tint 深浅接近，差别只在"糊不糊"。

        **顺序不可换**：先把底板画上去并**同步落地**，**再**撤系统那层。
        反过来就是"系统不画 + Qt 还没画"的一帧，窗口会直接露出桌面原样。
        """
        from . import theme

        if not self._glass_on:
            # 未启用原生模糊（`USE_NATIVE_BLUR = False`，当前默认）：底板在
            # `init_glass` 里就已经是最终形态了，没有需要"让路"的东西。
            # 这里早退既省掉每轮一次无谓调用，也保证了**拖动不改动外观** ——
            # 全程只有一个形态，观感自然连贯。
            return

        # 上一轮恢复可能正处在"等 Acrylic 就绪"的等待中：取消它，否则
        # `_finish_restore` 到期后会把我们刚设好的底板改掉。
        self._restore_pending = False
        self._restore_timer.stop()

        try:
            hwnd = int(self.winId())
        except Exception:
            hwnd = 0

        # 清掉可能残留的快照（上一版方案会设），并铺半透明底板。
        self._glass_surface.set_freeze(None)
        self._glass_surface.setStyleSheet(
            theme.glass_surface_qss(theme.GLASS_BASE_ALPHA))
        self._glass_surface.repaint()

        if hwnd:
            try:
                suspend(hwnd)
            except Exception:
                pass
        self._glass_suspended = True

    def _restore_glass(self) -> None:
        """停止拖动后恢复原生模糊。

        由 `_glass_settle` 定时器（`_MOVE_SETTLE_MS`）在最后一次移动之后触发。
        """
        from . import theme

        if not self._glass_on or not self._glass_suspended:
            return      # 未启用原生模糊，或本来就没让路 → 无事可做
        if self._restore_pending:
            return      # 已经进入"等 Acrylic 就绪"的收尾阶段，别重复 resume

        # ---- 判断"手势是否还在进行" ----
        #
        # 判据是**我们自己维护的拖动/缩放标记**，**不用 `mouseButtons()`**。
        #
        # `mouseButtons()` 在这里是双重危害：
        # ① 它不可靠 —— 拖动无边框窗口时可能报 NoButton（早期实现靠它轮询，
        #    结果拖动中途 411ms 就冒出一次 resume，背景来回闪）；
        # ② 更糟的是**误判的后果**：一旦把它当"手已松开"而清掉 `_dragging`，
        #    就会 resume() 把 Acrylic 装回来 —— 而用户其实还在拖！Acrylic 一
        #    回来，Win10 的拖动迟滞立刻生效，表现就是**"鼠标已经停在那里了，
        #    窗口才慢慢挪过来"**（用户报的"左右移动有几率变慢"就是这个）。
        #
        # `_dragging` 由 `mousePressEvent` / `mouseReleaseEvent` 成对维护；
        # Qt 在按下时会 grab mouse，松手事件一定会送到本窗口，所以它可靠。
        if self._dragging or self._resize_edge:
            return      # 手势仍在进行：不恢复。
                        # 也**不重启定时器** —— 恢复统一交给 mouseReleaseEvent。

        try:
            hwnd = int(self.winId())
        except Exception:
            self._glass_suspended = False
            return
        ok = False
        try:
            ok = bool(resume(hwnd))
        except Exception:
            ok = False
        if ok:
            # ⚠️ 这里**不能**立刻撤快照/转透明。
            #
            # `resume()` 只是把系统模糊的意图通知给 DWM，真正画出 Acrylic 需要
            # 一整帧；而 Qt 这边的底板/快照可以立刻消失。若两者之间没有等待，
            # 中间就会露出一帧"底板透明 + Acrylic 还没画"的画面 —— 用户看到的
            # 正是**松手时闪一下**（一瞬间没有模糊背景，露出桌面原样）。
            #
            # 所以保持当前画面（快照 + 实色底板）再等一小段，等 Acrylic 就绪后
            # 才切换。这期间画面完全不变，用户感知不到等待。
            self._restore_pending = True
            self._restore_timer.start()
            return
        # 恢复失败：保持快照与实色底板（宁可少一层模糊，也不能让底板消失），
        # 并把挂起标记归位 —— 标记卡在 True 时后续拖动会以为"已经让过路了"
        # 而不再尝试恢复，窗口就永久停在无模糊态。
        self._glass_suspended = False

    def _finish_restore(self) -> None:
        """等待期满：Acrylic 已就绪，撤掉快照并让底板转透明。

        与 `_restore_glass` 拆成两步，是为了让"系统画好模糊"和"Qt 撤掉遮挡"
        之间隔开一帧 —— 顺序见 `_restore_glass` 的注释。
        """
        from . import theme

        # ⚠️ 必须先清标记再判断，否则"因用户又拖动而提前返回"会把
        # `_restore_pending` 永久留在 True —— 之后 `_restore_glass` 会被自己的
        # 防重复检查挡住，窗口再也回不到模糊态。
        if not self._restore_pending:
            return      # 已被新一轮拖动取消（`_enter_drag_mode` 清的）
        self._restore_pending = False
        if self._dragging or self._resize_edge:
            # 等待期间用户又开始拖动：交给 `_enter_drag_mode` 接管。
            # 这里保持画面不动（什么都别撤），避免闪一下。
            return
        # Acrylic 已经在后面画好了。底板转透明 → 平滑地"露出"实时模糊背景。
        # 只刷底板即可：没有快照层，不存在上层控件与旧像素错位的问题，
        # 比整窗 repaint（2.75ms）省。
        self._glass_surface.setStyleSheet(theme.glass_surface_qss())
        self._glass_surface.repaint()
        self._glass_suspended = False

    def resizeEvent(self, event) -> None:  # noqa: N802
        """窗口尺寸变化时把玻璃底板重新铺满。

        必须留在 mixin 里：主窗口早期自己写过一份，改用 mixin 后漏掉了，
        结果是**窗口缩放时底板不再跟随**（右下角露出一条无底板的原始底色）。
        """
        super().resizeEvent(event)
        surface = getattr(self, "_glass_surface", None)
        if surface is not None:
            # 铺满整个窗口（不只是 centralWidget）：状态栏等区域也在玻璃之内
            surface.setGeometry(self.rect())
            surface.lower()

    def moveEvent(self, event) -> None:  # noqa: N802
        """移动时**不做任何重活**。

        让路改由 `mouseMoveEvent` 在“确认是用户在拖动”那一刻触发 ——
        只有那里才知道这是一次真实拖动，而不是程序化 `move()`、系统重排、
        或输入法候选框导致的位移。

        早期实现在这里挂了 `_throttle_glass()`，对**每一次** moveEvent 都做
        `capture_window_surface()`（实测 13.2ms）+ `setStyleSheet` + `repaint`
        （2.75ms）—— 合计 18ms/次。配合 4px 的拖动阈值，"点一下"就会被判成
        拖动，于是每点一下就卡一下。这里保持空实现，杜绝重活混进位移路径。
        """
        super().moveEvent(event)

    def showEvent(self, event) -> None:  # noqa: N802
        """每次显示后确保原生模糊已挂上（hwnd 到这时才存在）。"""
        super().showEvent(event)
        self.enable_glass()

    # ---- 无边框后自己实现拖动与缩放 ----

    # 缩放边用**自实现的整数常量**，不用 `Qt.Edge`。
    #
    # 两个原因（都是实测踩出来的）：
    # 1. `Qt.Edge` 只有 LeftEdge/RightEdge/TopEdge/BottomEdge **四个单边成员**，
    #    **不存在** TopLeft / BottomRight 这类角名 —— 写 `Qt.Edge.TopLeft` 会
    #    AttributeError，而它发生在 mouseMoveEvent 里，于是每移动一帧就刷一条
    #    栈、缩放功能彻底失效。
    # 2. 角在 Qt 里是**按位组合**（`TopEdge | LeftEdge`），而组合值**不等于任何
    #    单边**，`e in (LeftEdge, TopEdge)` 判断不到角。所以哪怕把名字补对，
    #    `in` 那套写法对角仍然是错的 —— 角会一条 if 都不命中，窗口只跟着鼠标
    #    平移而不改变尺寸。
    _EDGE_L = 0x1
    _EDGE_R = 0x2
    _EDGE_T = 0x4
    _EDGE_B = 0x8

    def _hit_edge(self, pos) -> int:
        """指针落在哪条缩放边上（全不落返回 0）。

        角返回**两条边的按位或**（如 _EDGE_T|_EDGE_L），调用方用 `_edge_has`
        逐位判断 —— 直接用 `in` 对组合值无效，见上面注释。
        """
        m = self._edge_margin
        r = self.rect()
        x, y = pos.x(), pos.y()
        e = 0
        if x <= m:
            e |= self._EDGE_L
        elif x >= r.width() - m:
            e |= self._EDGE_R
        if y <= m:
            e |= self._EDGE_T
        elif y >= r.height() - m:
            e |= self._EDGE_B
        return e

    @classmethod
    def _edge_has(cls, e: int, flag: int) -> bool:
        """边集合里是否含某条边。"""
        return bool(e & flag)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            edge = self._hit_edge(event.position().toPoint())
            if edge:
                self._resize_edge = edge
                self._resize_origin = (event.globalPosition().toPoint(),
                                       self.geometry())
                event.accept()
                return
            # 记下**按下点**与**当时的窗口左上角**，两者的差就是真实抓取偏移。
            # 早期实现把它硬编码成 QPoint(120, 14)，于是不管在哪儿按下，窗口左上
            # 角都会跳到「鼠标位置 − (120,14)」—— 表现为一按下去窗口就"跳一下"，
            # 点击任务树中间时尤其明显（窗口猛地窜到鼠标左上方）。
            self._press_pos = event.position().toPoint()
            self._press_global = event.globalPosition().toPoint()
            self._drag_origin = self.frameGeometry().topLeft()
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._resize_edge:
            origin, geo = self._resize_origin
            d = event.globalPosition().toPoint() - origin
            new = QRect(geo)
            e = self._resize_edge
            # 逐边判断：角会同时命中两条边，横竖一起改，正是角落缩放该有的行为
            if self._edge_has(e, self._EDGE_L):
                new.setLeft(geo.left() + d.x())
            if self._edge_has(e, self._EDGE_R):
                new.setRight(geo.right() + d.x())
            if self._edge_has(e, self._EDGE_T):
                new.setTop(geo.top() + d.y())
            if self._edge_has(e, self._EDGE_B):
                new.setBottom(geo.bottom() + d.y())
            if new.width() >= 420 and new.height() >= 300:
                self.setGeometry(new)
            if self._glass_on and (not self._glass_suspended
                                   or self._restore_pending):
                self._enter_drag_mode()
            # ⚠️ 这里**不**启动 settle 定时器。缩放进行中重启它没有意义，反而会
            # 让定时器在手指停顿时触发恢复（见 `_restore_glass` 的说明）——
            # 恢复/撤下交替就是用户看到的"背景闪烁"。恢复统一由松手触发。
            return
        if self._press_pos is not None \
                and event.buttons() & Qt.MouseButton.LeftButton:
            gp = event.globalPosition().toPoint()
            delta = gp - self._press_global
            if not self._dragging:
                # 阈值：点击时手指会有几像素抖动，阈值太小会让"点一下"被当成
                # "拖动"—— 每轮误触发一次让路（含 13ms 抓屏），表现就是
                # "点一下卡一下"。8px 能稳稳分开点击与拖动。
                if delta.manhattanLength() <= _DRAG_THRESHOLD:
                    return
                self._dragging = True
                # 一旦确认是拖动就**立刻**让路：等 moveEvent 里的延迟判定会让
                # 前几帧很卡，而拖动正是最需要跟手的时刻。
                #
                # ⚠️ 判据里必须带上 `_restore_pending`：上一轮拖动的恢复正处在
                # "等 Acrylic 就绪"的 grace 阶段时，Acrylic **已经装回来了**
                # （`_restore_glass` 里调过 resume），可 `_glass_suspended` 还是
                # True —— 只看它就会跳过让路，于是这一整轮拖动都在 Acrylic 下
                # 进行，迟滞回归（"窗口慢慢才挪过来"）。
                if self._glass_on and (not self._glass_suspended
                                       or self._restore_pending):
                    self._enter_drag_mode()
            # 用**按下时记下的偏移**平移：窗口不会跳，鼠标始终抓着同一个点。
            self.move(self._drag_origin + delta)
            # ⚠️ 拖动全程只在起手让路一次，中间**不**碰定时器、也不碰样式表。
            # 一旦在移动路径里重启 settle 定时器，手指停顿超过 settle 间隔就会
            # 触发一次"恢复→下次移动又撤下"，背景于是闪烁。
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._resize_edge = 0
        self._press_pos = None
        self._press_global = None
        self._dragging = False
        # 手势结束 → 由这里统一触发恢复（`_restore_glass` 负责延迟与所有
        # 边界情况）。放在松手处而不是移动路径里，是因为"是否还在拖动"
        # 只有按下/抬起这对事件说得清 —— 中间靠轮询判断必然出错。
        if self._glass_suspended:
            self._glass_settle.start()
        super().mouseReleaseEvent(event)
