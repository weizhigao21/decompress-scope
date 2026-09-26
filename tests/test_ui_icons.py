"""界面图形守卫：确保「设了背景的子控件」真的画出了图形。

这类缺陷的共同根因只有一条：**QSS 一旦给子控件（`::indicator` / `::up-arrow` …）
设了任何一条规则，样式引擎就接管了该控件的整套绘制、不再转发给原生 style。**
于是"只设背景不设 image"的写法会留下一个纯色块：
- `QCheckBox::indicator:checked` → 蓝方块，没有勾
- `QSpinBox::up-button` → 灰条，没有三角

它们**只靠看代码发现不了**（"样式表里明明写了背景色"），必须真渲染取像素。

offscreen 下的两个坑（决定断言怎么写）：
1. 光标常驻 (10,10)，无父级控件 `show()` 后正好落在光标下 → 一显示就是悬停态，
   必须先投一个 Leave 事件把基线钉干净；
2. 0×0 的图取色恒返回无效色，断言会**假绿**，所以每个用例先断言图有尺寸。
"""
import os
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过界面图形守卫")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication, QCheckBox, QSpinBox  # noqa: E402

from ui import theme  # noqa: E402

# 判据阈值：勾是纯白(255)，三角是 TEXT_MUTED(#8B949E, 最小通道 139)；
# 而指示框蓝底(#3B9EFF, 最小通道 59)、按钮底 LINE(#2D3844, 45) 都远低于阈值，
# 所以「最小通道 > 120」只会命中的图形本身，不会命中背景。
GLYPH_MIN_CHANNEL = 120
CHECK_MIN_CHANNEL = 200


@pytest.fixture(scope="module")
def app():
    """按真实启动路径装配：Fusion + 暗色调色板 + 主题 QSS。"""
    from ui.app import _apply_dark_palette

    inst = QApplication.instance() or QApplication([])
    inst.setStyle("Fusion")
    _apply_dark_palette(inst)
    inst.setStyleSheet(theme.build_stylesheet())
    return inst


@pytest.fixture
def shown(app):
    """建控件 → 显示 → 摘掉悬停态 → 返回；用完关掉。"""
    made = []

    def _make(widget):
        widget.show()
        app.processEvents()
        # offscreen 光标常驻 (10,10)，不摘掉悬停态会读到悬停色
        QApplication.sendEvent(widget, QEvent(QEvent.Type.Leave))
        app.processEvents()
        made.append(widget)
        return widget

    yield _make
    for w in made:
        w.close()
    app.processEvents()


def _grab(widget) -> QImage:
    img = widget.grab().toImage()
    # 0×0 图的 pixelColor() 返回无效色，后面的断言会全部静默通过
    assert img.width() > 8 and img.height() > 8, (
        f"没抓到画面（{img.width()}x{img.height()}），本用例会假绿")
    return img


def _glyph_pixels(img: QImage, x_from: int, x_to: int, floor: int) -> int:
    """统计 [x_from, x_to) 区间内亮于 floor 的像素数（只看笔画核心，不管抗锯齿边缘）。"""
    n = 0
    for y in range(img.height()):
        for x in range(x_from, x_to):
            c = img.pixelColor(x, y)
            if min(c.red(), c.green(), c.blue()) > floor:
                n += 1
    return n


def _indicator_band(img: QImage, widget, logical_px: int = 20) -> int:
    """指示框所在的左侧带宽度（按设备像素算，防 DPR 不是 1）。"""
    return max(1, int(logical_px * img.width() / max(1, widget.width())))


def _button_band(img: QImage, widget, logical_px: int = 18) -> int:
    """数字框右侧按钮带：返回该带的起始 x。"""
    return img.width() - max(1, int(logical_px * img.width() / max(1, widget.width())))


# ---------- 资源本身 ----------

def test_icon_assets_exist_and_load():
    """图标文件在位且能被 Qt 解码——缺文件时界面会静默退化回色块。"""
    from PySide6.QtGui import QImageReader

    for name in ("check.svg", "arrow_up.svg", "arrow_down.svg"):
        path = Path(theme.asset_url(name))
        assert path.is_file(), f"图标缺失: {path}"
        reader = QImageReader(str(path))
        assert reader.canRead(), f"Qt 读不了这个图标（缺 svg 插件？）: {path}"


def test_asset_url_is_absolute_posix():
    """QSS 的相对路径按进程工作目录解析，必须给绝对路径且转成正斜杠。"""
    url = theme.asset_url("check.svg")
    assert Path(url).is_absolute(), "必须是绝对路径，否则换目录启动就找不到图标"
    assert "\\" not in url, "反斜杠在 QSS 字符串里是转义符，须用正斜杠"


# ---------- 复选框的勾 ----------

def test_checked_checkbox_draws_a_checkmark(shown):
    """勾选态必须画出对勾，不能只是一块蓝色方块。

    旧实现在这里**实测 0 个亮像素**（指示框是纯 #3B9EFF 填充）。
    """
    box = shown(QCheckBox("拖入即开始"))
    box.setChecked(True)
    QApplication.processEvents()

    img = _grab(box)
    band = _indicator_band(img, box)
    bright = _glyph_pixels(img, 0, band, CHECK_MIN_CHANNEL)

    assert bright >= 8, (
        f"勾选态只有 {bright} 个亮像素——指示框是个纯色方块，没画对勾。"
        "（是否丢了 QCheckBox::indicator:checked 的 image 规则？）")


def test_unchecked_checkbox_draws_no_checkmark(shown):
    """反向用例：未勾选态不得出现对勾，否则"勾"传达不出状态差异。"""
    box = shown(QCheckBox("拖入即开始"))
    box.setChecked(False)
    QApplication.processEvents()

    img = _grab(box)
    band = _indicator_band(img, box)
    bright = _glyph_pixels(img, 0, band, CHECK_MIN_CHANNEL)

    assert bright == 0, f"未勾选态出现了 {bright} 个亮像素，对勾被无条件画了出来"


# ---------- 数字框的三角 ----------

def test_spinbox_draws_arrow_glyphs(shown):
    """数字框右侧按钮必须画出三角，而不是一条纯灰色。

    旧实现在这里**实测 0 个亮像素**（按钮是纯 #2D3844 填充）。
    """
    spin = shown(QSpinBox())
    spin.setRange(0, 9999)
    spin.setValue(1200)
    spin.resize(140, 30)
    QApplication.processEvents()

    img = _grab(spin)
    left = _button_band(img, spin)
    bright = _glyph_pixels(img, left, img.width(), GLYPH_MIN_CHANNEL)

    assert bright >= 8, (
        f"数字框按钮带只有 {bright} 个亮像素——只有灰条没有三角。"
        "（是否丢了 QSpinBox::up-arrow / down-arrow 的 image 规则？）")


def _ink_rows(img: QImage, x_from: int, x_to: int, y_from: int, y_to: int) -> list[int]:
    """逐行的亮像素数（用于判断三角的朝向）。"""
    rows = []
    for y in range(y_from, y_to):
        rows.append(sum(
            1 for x in range(x_from, x_to)
            if min(img.pixelColor(x, y).red(), img.pixelColor(x, y).green(),
                   img.pixelColor(x, y).blue()) > GLYPH_MIN_CHANNEL))
    return rows


def test_spinbox_arrows_point_opposite_ways(shown):
    """两个三角必须朝向相反。

    只断言"有三角"抓不到复制粘贴事故——上下都指同一个方向照样会通过。
    判据用「首行墨迹宽 vs 末行墨迹宽」：上三角顶点在上（下宽），下三角相反。
    """
    spin = shown(QSpinBox())
    spin.setRange(0, 9999)
    spin.resize(140, 32)
    QApplication.processEvents()

    img = _grab(spin)
    left = _button_band(img, spin)
    mid = img.height() // 2

    # 左右各内缩 2px 躲开按钮自身的边框（其抗锯齿像素足够亮，会污染首末行）
    x0, x1 = left + 2, img.width() - 2

    up_rows = _ink_rows(img, x0, x1, 2, mid - 2)
    down_rows = _ink_rows(img, x0, x1, mid + 2, img.height() - 2)

    def first_last(rows):
        ink = [i for i, n in enumerate(rows) if n > 0]
        assert ink, "这一段里一个三角像素都没有——守卫失去区分力"
        return rows[ink[0]], rows[ink[-1]]

    up_first, up_last = first_last(up_rows)
    down_first, down_last = first_last(down_rows)

    assert up_last > up_first, (
        f"上半部三角是上宽下窄（首行 {up_first} / 末行 {up_last}）——像是下三角画到了加号按钮上")
    assert down_first > down_last, (
        f"下半部三角是下宽上窄（首行 {down_first} / 末行 {down_last}）——像是上三角画到了减号按钮上")


def test_up_and_down_assets_are_different():
    """两个箭头资源不能是同一份内容（复制粘贴后忘了翻向）。"""
    up = Path(theme.asset_url("arrow_up.svg")).read_text(encoding="utf-8")
    down = Path(theme.asset_url("arrow_down.svg")).read_text(encoding="utf-8")
    assert up != down, "上下箭头文件内容相同，两个按钮会画成同一个朝向"
