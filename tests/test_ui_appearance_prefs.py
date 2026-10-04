"""外观偏好守卫：窗口不透明度、以及「解压完成后自动打开」。

两个偏好都容易"改了没反应"：
- 不透明度存在主题**模块级**变量里，而底板样式是**显式传参**设给每个窗口的，
  只改常量不会波及已打开的窗口；
- `open_after` 是主窗口与右键窗口**共用**的开关，任一边漏读就会出现
  "主窗口关了、右键照开"。
"""
import os

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过外观偏好守卫")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from ui import theme  # noqa: E402


@pytest.fixture(scope="module")
def app():
    from ui.app import _apply_dark_palette

    inst = QApplication.instance() or QApplication([])
    inst.setStyle("Fusion")
    _apply_dark_palette(inst)
    inst.setStyleSheet(theme.build_stylesheet())
    return inst


@pytest.fixture(autouse=True)
def _restore_alpha():
    """每条用例后把底板不透明度复位，别污染同进程的其它界面用例。"""
    original = theme.GLASS_BASE_ALPHA
    yield
    theme.set_glass_alpha_percent(int(round(original * 100)))


# ---------- 窗口不透明度 ----------


def test_opacity_bounds_have_single_source():
    """区间只能来自 core 的边界表 —— UI 不得另写一份。"""
    from core.appconfig import clamp_bounds

    assert clamp_bounds("window_opacity") == (30, 100), (
        "边界表里的区间变了；设置窗口的数字框范围是从这里取的，"
        "两边不一致会出现「能调到 20、保存时被静默夹回 30」的鬼打墙")


def test_opacity_is_clamped_on_injection():
    """即使配置文件被手改成区间外的值，也不能真的生效。"""
    theme.set_glass_alpha_percent(5)
    assert theme.GLASS_BASE_ALPHA == 0.30, "低于下限没有被夹住"
    theme.set_glass_alpha_percent(500)
    assert theme.GLASS_BASE_ALPHA == 1.0, "高于上限没有被夹住"


def test_apply_opacity_reaches_every_open_window(app):
    """改不透明度必须**逐个窗口**重设底板。

    底板样式是显式传参设的（`glass_surface_qss(theme.GLASS_BASE_ALPHA)`），
    只改主题里的模块级常量，已打开的窗口不会有任何变化 —— 用户的感受就是
    "设置里改了、界面没变"。这是本功能最容易踩的一处。
    """
    from ui import glass
    from ui.main_window import MainWindow
    from ui.quick_extract_window import QuickExtractWindow

    main = MainWindow()
    quick = QuickExtractWindow(["C:/tmp/a.zip"], autostart=False)
    try:
        main.show()
        quick.show()
        app.processEvents()

        glass.apply_opacity(app, 45)

        assert abs(theme.GLASS_BASE_ALPHA - 0.45) < 1e-6, "主题常量没更新"
        for label, win in (("主窗口", main), ("右键窗口", quick)):
            css = win._glass_surface.styleSheet()
            assert "0.45" in css, (
                f"{label}的底板没跟着改（{css!r}）—— 用户会以为设置没保存成功")
    finally:
        quick.close()
        main.close()
        app.processEvents()


# ---------- 解压完成后自动打开 ----------


class _FakeReport:
    """只带 `_maybe_open_results` 用到的字段。"""

    def __init__(self, dirs):
        self.output_dirs = list(dirs)


def _quick_window():
    from ui.quick_extract_window import QuickExtractWindow

    win = QuickExtractWindow(["C:/tmp/a.zip"], autostart=False)
    opened = []
    # 换成记录器：既不真的弹资源管理器，也能看清"开没开、开了几个"
    win._open_results = lambda limit=None: opened.append(limit)
    return win, opened


def test_quick_window_obeys_open_after_none(app, monkeypatch):
    """偏好说"不打开"时，右键窗口不得自动弹目录。"""
    from ui import quick_extract_window as q

    win, opened = _quick_window()
    try:
        win._report = _FakeReport(["C:/tmp/out"])
        monkeypatch.setattr(
            q.AppConfig, "ensure",
            staticmethod(lambda _p: type("C", (), {"open_after": "none"})()))
        win._maybe_open_results()
        assert opened == [], "偏好是「不打开」却自动打开了目录"
    finally:
        win.close()
        app.processEvents()


def test_quick_window_opens_results_when_configured(app, monkeypatch):
    """偏好是"打开结果目录"时必须真的打开 —— 这正是用户报的缺口。"""
    from ui import quick_extract_window as q

    win, opened = _quick_window()
    try:
        win._report = _FakeReport(["C:/tmp/out"])
        monkeypatch.setattr(
            q.AppConfig, "ensure",
            staticmethod(lambda _p: type("C", (), {"open_after": "paths"})()))
        win._maybe_open_results()
        assert opened == [None], (
            "偏好是「打开结果目录」却没有打开 —— 右键解压完用户找不到产物")
    finally:
        win.close()
        app.processEvents()


def test_quick_window_caps_opened_dirs_on_auto(app, monkeypatch):
    """自动打开时目录很多只开一个：十几个资源管理器会淹没桌面。"""
    from ui import quick_extract_window as q

    win, opened = _quick_window()
    try:
        win._report = _FakeReport([f"C:/tmp/out{i}" for i in range(9)])
        monkeypatch.setattr(
            q.AppConfig, "ensure",
            staticmethod(lambda _p: type("C", (), {"open_after": "paths"})()))
        win._maybe_open_results()
        assert opened == [1], f"9 个目录应当只开 1 个，实际 limit={opened}"
    finally:
        win.close()
        app.processEvents()


def test_quick_window_does_not_open_without_outputs(app, monkeypatch):
    """没有产物时不打开（失败 / 跳过的情况都走这里）。"""
    from ui import quick_extract_window as q

    win, opened = _quick_window()
    try:
        win._report = _FakeReport([])
        monkeypatch.setattr(
            q.AppConfig, "ensure",
            staticmethod(lambda _p: type("C", (), {"open_after": "paths"})()))
        win._maybe_open_results()
        assert opened == [], "没有产物却去打开目录"
    finally:
        win.close()
        app.processEvents()
