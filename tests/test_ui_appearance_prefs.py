"""外观偏好守卫：窗口不透明度、以及「解压完成后自动打开」与其后续的关窗。

两个偏好都容易"改了没反应"：
- 不透明度存在主题**模块级**变量里，而底板样式是**显式传参**设给每个窗口的，
  只改常量不会波及已打开的窗口；
- `open_after` 是主窗口与右键窗口**共用**的开关，任一边漏读就会出现
  "主窗口关了、右键照开"。

`close_quick_after_open`（右键窗口在结果目录打开后退场）另有一层陷阱：它挂在
"**打开成功**"这个前提上。挂到"解压完成"上的话，交付失败、目录被挪走、用户关掉
自动打开这几种情况下窗口都会消失，而用户既看不到产物位置，也没有「打开结果目录」
可点 —— 所以下面把"什么时候**不**关"也一并钉住。
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
    from core.appconfig import OPEN_NONE, AppConfig
    from ui import quick_extract_window as q

    win, opened = _quick_window()
    try:
        win._report = _FakeReport(["C:/tmp/out"])
        monkeypatch.setattr(
            q.AppConfig, "ensure",
            staticmethod(lambda _p: AppConfig(open_after=OPEN_NONE)))
        win._maybe_open_results()
        assert opened == [], "偏好是「不打开」却自动打开了目录"
    finally:
        win.close()
        app.processEvents()


def test_quick_window_opens_results_when_configured(app, monkeypatch):
    """偏好是"打开结果目录"时必须真的打开 —— 这正是用户报的缺口。"""
    from core.appconfig import OPEN_PATHS, AppConfig
    from ui import quick_extract_window as q

    win, opened = _quick_window()
    try:
        win._report = _FakeReport(["C:/tmp/out"])
        monkeypatch.setattr(
            q.AppConfig, "ensure",
            staticmethod(lambda _p: AppConfig(open_after=OPEN_PATHS,
                                              close_quick_after_open=False)))
        win._maybe_open_results()
        assert opened == [None], (
            "偏好是「打开结果目录」却没有打开 —— 右键解压完用户找不到产物")
    finally:
        win.close()
        app.processEvents()


def test_quick_window_caps_opened_dirs_on_auto(app, monkeypatch):
    """自动打开时目录很多只开一个：十几个资源管理器会淹没桌面。"""
    from core.appconfig import OPEN_PATHS, AppConfig
    from ui import quick_extract_window as q

    win, opened = _quick_window()
    try:
        win._report = _FakeReport([f"C:/tmp/out{i}" for i in range(9)])
        monkeypatch.setattr(
            q.AppConfig, "ensure",
            staticmethod(lambda _p: AppConfig(open_after=OPEN_PATHS,
                                              close_quick_after_open=False)))
        win._maybe_open_results()
        assert opened == [1], f"9 个目录应当只开 1 个，实际 limit={opened}"
    finally:
        win.close()
        app.processEvents()


def test_quick_window_does_not_open_without_outputs(app, monkeypatch):
    """没有产物时不打开（失败 / 跳过的情况都走这里）。"""
    from core.appconfig import OPEN_PATHS, AppConfig
    from ui import quick_extract_window as q

    win, opened = _quick_window()
    try:
        win._report = _FakeReport([])
        monkeypatch.setattr(
            q.AppConfig, "ensure",
            staticmethod(lambda _p: AppConfig(open_after=OPEN_PATHS)))
        win._maybe_open_results()
        assert opened == [], "没有产物却去打开目录"
    finally:
        win.close()
        app.processEvents()


# ---------- 结果目录打开后关闭右键窗口 ----------


def _wired_quick_window(tmp_path, monkeypatch, **prefs):
    """偏好读自**真实配置文件**的右键窗口，外加一个不弹资源管理器的 startfile。

    这里刻意不用手搓的假配置对象：这条偏好是靠**字段名**接到窗口上的，假对象
    测不出"字段名写错"或"忘了接线"——两种情况实现都坏了，测试却照样绿。
    """
    from core.appconfig import AppConfig
    from ui import quick_extract_window as q

    cfg_path = tmp_path / "config.json"
    AppConfig(**prefs).save(cfg_path)
    monkeypatch.setattr(q, "CONFIG_PATH", cfg_path)
    monkeypatch.setattr(q, "_CLOSE_AFTER_OPEN_DELAY_MS", 0)   # 退场延迟不参与断言
    opened: list[str] = []
    monkeypatch.setattr(q.os, "startfile", lambda p: opened.append(str(p)), raising=False)
    return q.QuickExtractWindow(["C:/tmp/a.zip"], autostart=False), opened


def _pump(ms: int = 80) -> None:
    """跑一小段事件循环，让退场用的 singleShot 真正触发。"""
    from PySide6.QtTest import QTest

    QTest.qWait(ms)


def test_quick_window_closes_after_opening_results(app, tmp_path, monkeypatch):
    """用户要的动作：解压完、结果目录打开之后，进度窗口自己退场。"""
    from core.appconfig import OPEN_PATHS

    out = tmp_path / "out"
    out.mkdir()
    win, opened = _wired_quick_window(
        tmp_path, monkeypatch, open_after=OPEN_PATHS, close_quick_after_open=True)
    try:
        win._report = _FakeReport([str(out)])
        win.show()
        app.processEvents()
        assert win.isVisible(), "前置条件：窗口本应是打开的"

        win._maybe_open_results()
        _pump()

        assert opened == [str(out)], "结果目录没被打开，后面的关窗断言就没有意义"
        assert not win.isVisible(), "结果目录都打开了，右键窗口却还杵在桌面上"
    finally:
        win.close()
        app.processEvents()


def test_quick_window_stays_when_close_option_is_off(app, tmp_path, monkeypatch):
    """偏好关掉时窗口留着：用户自己点「关闭」或「打开结果目录」。"""
    from core.appconfig import OPEN_PATHS

    out = tmp_path / "out"
    out.mkdir()
    win, opened = _wired_quick_window(
        tmp_path, monkeypatch, open_after=OPEN_PATHS, close_quick_after_open=False)
    try:
        win._report = _FakeReport([str(out)])
        win.show()
        app.processEvents()

        win._maybe_open_results()
        _pump()

        assert opened == [str(out)]
        assert win.isVisible(), "偏好明明是关的，窗口却自己消失了"
    finally:
        win.close()
        app.processEvents()


def test_quick_window_stays_when_auto_open_is_off(app, tmp_path, monkeypatch):
    """不自动打开目录时，勾着「关窗」也不许关。

    关了就两头落空：既看不到产物落在哪，也没有「打开结果目录」可点。
    这正是"关窗"必须挂在"打开成功"上、而不是挂在"解压完成"上的理由。
    """
    from core.appconfig import OPEN_NONE

    out = tmp_path / "out"
    out.mkdir()
    win, opened = _wired_quick_window(
        tmp_path, monkeypatch, open_after=OPEN_NONE, close_quick_after_open=True)
    try:
        win._report = _FakeReport([str(out)])
        win.show()
        app.processEvents()

        win._maybe_open_results()
        _pump()

        assert opened == [], "偏好是「不打开」却打开了目录"
        assert win.isVisible(), "没打开目录就把窗口关了，用户无从知道产物在哪"
    finally:
        win.close()
        app.processEvents()


def test_quick_window_stays_when_nothing_could_be_opened(app, tmp_path, monkeypatch):
    """目录已被挪走 / 打开失败：没有"打开之后"这一步，窗口必须留着报信。"""
    from core.appconfig import OPEN_PATHS

    win, opened = _wired_quick_window(
        tmp_path, monkeypatch, open_after=OPEN_PATHS, close_quick_after_open=True)
    try:
        win._report = _FakeReport([str(tmp_path / "gone")])
        win.show()
        app.processEvents()

        win._maybe_open_results()
        _pump()

        assert opened == []
        assert win.isVisible(), "一个目录都没打开，窗口却先关了"
    finally:
        win.close()
        app.processEvents()


def test_finished_run_closes_quick_window(app, tmp_path, monkeypatch):
    """从「解压完成」到「窗口退场」的整条接线。

    只测 `_maybe_open_results` 不够：谁把 `_on_finished` 里那次调用删掉，
    症状（窗口不再自己关）一模一样，而直接调 `_maybe_open_results` 的用例照样绿。
    """
    from core.appconfig import OPEN_PATHS
    from core.pipeline import RunReport

    out = tmp_path / "out"
    out.mkdir()
    win, opened = _wired_quick_window(
        tmp_path, monkeypatch, open_after=OPEN_PATHS, close_quick_after_open=True)
    try:
        win.show()
        app.processEvents()

        win._on_finished(RunReport(done=1, output_dirs=[str(out)]))
        _pump()

        assert opened == [str(out)], "完成回调没有去打开结果目录"
        assert not win.isVisible(), "解压完成、目录也打开了，右键窗口却没有退场"
    finally:
        win.close()
        app.processEvents()


def test_close_after_open_defers_while_worker_still_running(app, tmp_path, monkeypatch):
    """线程还没退完时不许直接关窗。

    直接 close() 会落进 `closeEvent` 的"仍在运行"分支，也就是**取消**那条路：
    按钮被禁用、状态改成"正在取消"。可此刻解压早就成功了，用户会看到窗口在最后
    一刻闪出一个取消态 —— 所以退场必须挂到线程收尾上，而不是就地关。
    线程真跑起来反而测不准这一瞬间，这里只求 `isRunning()` 为真。
    """
    from core.appconfig import OPEN_PATHS

    out = tmp_path / "out"
    out.mkdir()
    win, _ = _wired_quick_window(tmp_path, monkeypatch, open_after=OPEN_PATHS)

    class _BusyThread:
        def isRunning(self) -> bool:
            return True

        def deleteLater(self) -> None:
            pass

    try:
        win.show()
        app.processEvents()
        win._thread = _BusyThread()

        win._close_when_done()
        assert win.isVisible()
        assert win._close_when_finished is True, "退场请求没交给线程收尾去执行"
        assert win.cancel_button.isEnabled() is True, "退场被当成了「取消」"
        assert "取消" not in win.status_label.text(), \
            "解压早已成功，窗口却在退场前闪了一下取消态"

        win._thread = None
        win._on_thread_finished()
        app.processEvents()
        assert not win.isVisible(), "线程退完后窗口没有自己关掉"
    finally:
        win._thread = None
        win.close()
        app.processEvents()
