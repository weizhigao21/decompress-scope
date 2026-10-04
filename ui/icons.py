# -*- coding: utf-8 -*-
"""面板按钮图标：自绘矢量图形，彻底不依赖字体。

**为什么要自绘**：这些按钮原先直接放符号字符当图标，用的是几何图形区与
数学运算符区的冷门码位（U+25A6 / U+25A9 / U+FF0B / U+22EF）。主流中文字体
（Microsoft YaHei UI）并未收录它们 → 真机上渲染成空心方块，也就是俗称的
"豆腐块"。注意**不要**改成"另一个更常见的符号"：这类码位的字体覆盖随系统
与字体版本漂移，换一个只是把问题推给下一台机器。自绘是唯一与字体无关的解法。

**颜色**：常态 `theme.TEXT_MUTED`，悬停 / 按下 `theme.ACCENT`。
刻意**不**读 QSS 的 `color`（即 `palette().color(ButtonText)`）——QSS 的颜色
是否同步进 palette 取决于样式引擎的 polish 时机，没必要把图标颜色押在这上面。
按钮的**底色**（含悬停底色）仍由 QSS 的 `QPushButton#iconBtn` 规则提供。

**网格**：所有图形都画在 `_GRID`×`_GRID` 的逻辑网格里，`IconButton` 负责
缩放到目标尺寸并居中。要调图形只需改网格内坐标，不用关心按钮实际多大。
"""
from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QPushButton

from . import theme

_GRID = 24.0        # 图标设计网格边长（逻辑单位）
_STROKE = 2.0       # 主笔画线宽（网格单位）


# ---------- 图形定义（全部在 24×24 网格内） ----------

def _draw_add(p: QPainter, color: QColor) -> None:
    """加号：添加任务。"""
    pen = QPen(color, _STROKE)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawLine(QPointF(12.0, 6.8), QPointF(12.0, 17.2))
    p.drawLine(QPointF(6.8, 12.0), QPointF(17.2, 12.0))


def _draw_more(p: QPainter, color: QColor) -> None:
    """三个横点：更多操作。"""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    for cx in (6.9, 12.0, 17.1):
        p.drawEllipse(QPointF(cx, 12.0), 1.75, 1.75)


def _draw_stats(p: QPainter, color: QColor) -> None:
    """柱状图：打卡统计。"""
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    bottom = 18.2
    # (中心 x, 顶端 y)：三根高低不同的圆角竖条
    for cx, top in ((7.0, 10.4), (12.0, 5.8), (17.0, 8.4)):
        p.drawRoundedRect(QRectF(cx - 1.75, top, 3.5, bottom - top), 1.2, 1.2)


def _draw_close(p: QPainter, color: QColor) -> None:
    """关闭：两条对角线组成的叉。"""
    pen = QPen(color, _STROKE)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(7.2, 7.2), QPointF(16.8, 16.8))
    p.drawLine(QPointF(16.8, 7.2), QPointF(7.2, 16.8))


def _draw_qr(p: QPainter, color: QColor) -> None:
    """二维码：三个定位图案 + 右下数据点。"""
    pen = QPen(color, 1.7)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    for ox, oy in ((4.9, 4.9), (14.4, 4.9), (4.9, 14.4)):
        p.drawRect(QRectF(ox, oy, 4.7, 4.7))

    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(color)
    # 定位图案的中心实心块
    for ox, oy in ((6.3, 6.3), (15.8, 6.3), (6.3, 15.8)):
        p.drawRect(QRectF(ox, oy, 1.9, 1.9))
    # 右下角的"数据点"——不用排满，有三两个小块就足以读成二维码
    p.drawRect(QRectF(14.6, 14.6, 2.2, 2.2))
    p.drawRect(QRectF(17.9, 17.9, 1.2, 1.2))
    p.drawRect(QRectF(14.6, 17.9, 1.2, 1.2))


def _draw_minimize(p: QPainter, color: QColor) -> None:
    """最小化：一条横线。"""
    pen = QPen(color, _STROKE)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    p.setPen(pen)
    p.drawLine(QPointF(7.0, 16.4), QPointF(17.0, 16.4))


def _draw_maximize(p: QPainter, color: QColor) -> None:
    """最大化：一个方框。

    画"框"而不是实心块：玻璃面板上层级靠描边区分，实心块会糊成一团。
    """
    pen = QPen(color, 1.7)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(QRectF(7.4, 7.4, 9.2, 9.2))


def _draw_restore(p: QPainter, color: QColor) -> None:
    """还原（从最大化退回）：两个错位叠放的框。

    只画三条边而不是两个完整矩形 —— 完整叠框在 24px 下会糊成一团黑，
    靠"断开右侧与底部"表达前后关系才清楚。
    """
    pen = QPen(color, 1.7)
    pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    p.drawRect(QRectF(10.2, 5.8, 8.0, 8.0))      # 后框（右上）
    p.drawLine(QPointF(5.8, 8.4), QPointF(5.8, 18.2))   # 前框左侧
    p.drawLine(QPointF(5.8, 18.2), QPointF(15.6, 18.2))  # 前框底侧


_DRAWERS = {
    "add": _draw_add,
    "more": _draw_more,
    "stats": _draw_stats,
    "qr": _draw_qr,
    "close": _draw_close,
    "minimize": _draw_minimize,
    "maximize": _draw_maximize,
    "restore": _draw_restore,
}


def icon_kinds() -> list[str]:
    """所有可用图标名。测试拿它遍历，新增图标自动纳入覆盖。"""
    return sorted(_DRAWERS)


class IconButton(QPushButton):
    """只画一个矢量图标、不带任何文字的按钮。

    文字恒为空字符串——这正是本模块存在的理由：字体里没有的码位画不出来，
    而不存在的文字也不会有人发现，直到有用户看见一排豆腐块。
    """

    def __init__(self, kind: str, box: int = 28, icon: int = 16, parent=None) -> None:
        super().__init__("", parent)
        if kind not in _DRAWERS:
            # 拼错图标名必须在构造期就炸，而不是安静地画出一个空按钮
            raise ValueError(
                f"未知图标 {kind!r}；可用：{'、'.join(icon_kinds())}")
        self._kind = kind
        self._icon_px = float(icon)
        self._hover = False
        self.setObjectName("iconBtn")
        self.setFixedSize(box, box)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    # ---- 悬停状态 ----
    def event(self, event) -> bool:
        """同时接住两套事件。

        `WA_Hover` 打开后，鼠标进出走的是 `HoverEnter/HoverLeave`，**不会再发**
        `Enter/Leave`；而 QSS 带 `:hover` 规则时样式引擎也会替我们打开 `WA_Hover`。
        两套都接住，就不用猜这一轮到底走哪套。
        """
        kind = event.type()
        if kind in (QEvent.Type.Enter, QEvent.Type.HoverEnter):
            self._hover = True
            self.update()
        elif kind in (QEvent.Type.Leave, QEvent.Type.HoverLeave):
            self._hover = False
            self.update()
        return super().event(event)

    def _icon_color(self) -> QColor:
        if not self.isEnabled():
            return QColor(theme.TEXT_MUTED)
        if self._hover or self.isDown():
            return QColor(theme.ACCENT)
        return QColor(theme.TEXT_MUTED)

    # ---- 绘制 ----
    def paintEvent(self, event) -> None:
        # 先让 QSS 画圆角底与悬停底色，再在它之上落图标
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        scale = self._icon_px / _GRID
        painter.translate((self.width() - self._icon_px) / 2.0,
                          (self.height() - self._icon_px) / 2.0)
        painter.scale(scale, scale)
        _DRAWERS[self._kind](painter, self._icon_color())
        painter.end()
