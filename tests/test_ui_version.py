"""主界面版本号显示的守卫。

版本号有两份来源：`core/__init__.py` 的 `__version__`（运行时唯一来源）与
`pyproject.toml` 的 `version`（打包元数据）。两份漂移了**不会报错**，只会慢慢
对不上——发出去的包和界面显示的版本号各说各话。这里钉住它们一致。

界面侧盯三件事：显示格式、取值来自常量（不是写死的字符串）、
以及它作为元信息**不能比产品名更重**。
"""
import os
import re
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过版本号守卫")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    from ui import theme

    inst = QApplication.instance() or QApplication([])
    inst.setStyleSheet(theme.build_stylesheet())
    return inst


@pytest.fixture
def win(app):
    from ui.main_window import MainWindow

    w = MainWindow()
    w.resize(1000, 680)
    yield w
    w.close()
    app.processEvents()


def _title_label(win):
    from PySide6.QtWidgets import QLabel

    found = [lb for lb in win.findChildren(QLabel) if lb.text() == "解压开镜"]
    assert found, "任务栏头部找不到标题标签"
    return found[0]


# ---------- 单一来源：两份版本号必须一致 ----------

def test_pyproject_version_matches_core():
    from core import __version__

    text = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'^version\s*=\s*"([^"]+)"', text, re.M)
    assert m, "pyproject.toml 里没有 version 字段"

    assert m.group(1) == __version__, (
        f"pyproject 写的是 {m.group(1)}，core.__version__ 是 {__version__}——"
        "改版本号时漏改了一处")


def test_version_looks_like_semver():
    """顺带钉住格式：x.y.z。界面上的 v 前缀配 '0.1' 或 '1.0.0-beta' 都别扭。"""
    from core import __version__

    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__), (
        f"版本号 {__version__!r} 不是 x.y.z 形式")


# ---------- 界面显示 ----------

def test_version_shown_in_header(win):
    from core import __version__

    assert win.version_label.text() == f"v{__version__}"


def test_version_comes_from_the_constant(app, monkeypatch):
    """版本号必须取自常量，不能是写死的字面量。

    把常量换掉后重建窗口，界面就得跟着变；写死的话这条会红。
    （也顺带钉住这个可注入的名字：实现若改成直接引 `__version__`，
    这里 patch 不到，用例会红着提醒换个注入口，而不是静默失效。）
    """
    from ui import main_window

    monkeypatch.setattr(main_window, "APP_VERSION", "9.9.9")
    w = main_window.MainWindow()
    try:
        assert w.version_label.text() == "v9.9.9"
    finally:
        w.close()
        app.processEvents()


def test_version_mentioned_in_tooltip(win):
    """悬停给出完整软件名 + 版本号。"""
    from core import __version__

    tip = win.version_label.toolTip()
    assert "解压开镜" in tip and __version__ in tip


# ---------- 视觉：元信息不能盖过产品名 ----------

def _max_lightness(img) -> int:
    best = 0
    for y in range(img.height()):
        for x in range(img.width()):
            best = max(best, img.pixelColor(x, y).lightness())
    return best


def test_version_is_quieter_than_the_title(win):
    """版本号要比名字暗。

    只断言"文本内容对"抓不到这个：版本号写成和标题同样的强调色，
    内容照样对，但整条头部的主次就乱了。
    """
    from PySide6.QtWidgets import QApplication

    win.show()
    QApplication.processEvents()

    title_img = _title_label(win).grab().toImage()
    version_img = win.version_label.grab().toImage()
    assert title_img.width() > 4 and version_img.width() > 4, (
        f"没抓到画面（{title_img.width()}x{title_img.height()}），本用例会假绿")

    t_lit, v_lit = _max_lightness(title_img), _max_lightness(version_img)
    assert v_lit < t_lit - 40, (
        f"版本号最亮像素 {v_lit} 不比标题 {t_lit} 显著弱，看起来跟名字一样重")


def test_version_is_smaller_than_the_title(win):
    """版本号字号必须小于产品名。

    这条已经抓过一次真实事故：最初用 `setFont(setPointSize(9))` 设字号，
    被全局 `QWidget { font-size: 13px; }` 整个覆盖——版本号实际跟标题一样大。

    度量方式也有坑：QSS 用 px 指定字号时 `font().pointSize()` **恒为 -1**，
    拿它比较只会得到 `-1 < -1`（假失败）。改用 QFontMetrics 的行高。
    """
    from PySide6.QtGui import QFontMetrics

    v_h = QFontMetrics(win.version_label.font()).height()
    t_h = QFontMetrics(_title_label(win).font()).height()

    assert v_h < t_h, (
        f"版本号行高 {v_h} 不小于标题行高 {t_h}——字号没生效？"
        "（代码里 setFont 会被全局 QSS 的 font-size 覆盖，得在 theme 里用选择器设）")


def test_version_sits_between_title_and_subtitle(win):
    """位置：名字 → 版本号 → 分隔线 → 副标题。"""
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication

    win.show()
    QApplication.processEvents()

    tx = _title_label(win).mapTo(win, QPoint(0, 0)).x()
    vx = win.version_label.mapTo(win, QPoint(0, 0)).x()
    sx = win.subtitle.mapTo(win, QPoint(0, 0)).x()

    assert tx < vx, f"版本号没在名字右边（标题@{tx} 版本号@{vx}）"
    assert vx < sx, f"版本号跑到副标题后面去了（版本号@{vx} 副标题@{sx}）"
