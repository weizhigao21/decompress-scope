"""无边框窗口的「可关闭性」守卫。

背景：玻璃化要去掉系统标题栏（`FramelessWindowHint`），那条边正是用户反馈的
"窗口上边那条白边"的来源。但去掉标题栏的同时，**最小化 / 最大化 / 关闭这三个
入口也一起消失了** —— 它们原本由系统绘制。四个窗口因此都变成"关不掉"。

这几个测试钉住的就是这条生命线：只要窗口还是无边框，就必须自带窗口控制按钮。
它们不检查图标画得好不好看（那是像素守卫的事），只检查"按钮在、且接对了动作"，
因为**静默失效**（按钮还在但不响应）比消失更糟。
"""
import os

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过 GUI 冒烟测试")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    from ui import theme

    inst = QApplication.instance() or QApplication([])
    inst.setStyleSheet(theme.build_stylesheet())
    return inst


def _window_buttons(win):
    """收集窗口里所有窗口控制按钮（无论挂在标题栏还是直接挂在头部）。"""
    from PySide6.QtWidgets import QPushButton

    return [b for b in win.findChildren(QPushButton)
            if b.objectName() == "winCtl"]


def _make_main():
    from ui.main_window import MainWindow

    return MainWindow()


def test_main_window_is_frameless(app):
    """主窗口是无边框 —— 这是"必须自带窗口按钮"的前提。

    哪天改回有边框，这三条按钮测试就可以放宽；反过来若有人只加了按钮却忘了
    去边框，会多出一套重复控制，也该被这条挡住。
    """
    from PySide6.QtCore import Qt

    win = _make_main()
    try:
        assert win.windowFlags() & Qt.WindowType.FramelessWindowHint
    finally:
        win.close()
        app.processEvents()


def test_main_window_has_all_three_controls(app):
    """主窗口必须同时有最小化 / 最大化 / 关闭，缺一个都不行。

    少任何一个都是"功能缺失"而非"设计取舍"：用户会先试关闭，试不到就只能
    去任务管理器结束进程。
    """
    win = _make_main()
    try:
        btns = _window_buttons(win)
        kinds = {b._kind for b in btns}
        assert kinds == {"minimize", "maximize", "close"}, (
            f"窗口控制按钮不齐：{sorted(kinds)}")
    finally:
        win.close()
        app.processEvents()


def test_window_buttons_are_wired_to_real_actions(app):
    """按钮必须真的接上动作，不能只是"画出来好看"。

    用可观测副作用断言：点下去之后窗口状态真的变了（而不是断 returncode
    或"clicked 信号有连接"这种写死实现的断言 —— 实现换了照样通过）。
    """
    from PySide6.QtCore import Qt

    win = _make_main()
    try:
        win.show()
        app.processEvents()

        btn_close = next(b for b in _window_buttons(win) if b._kind == "close")
        btn_max = next(b for b in _window_buttons(win) if b._kind == "maximize")

        # 最大化：点击后 isMaximized() 真的变 True
        btn_max.click()
        app.processEvents()
        assert win.isMaximized(), "最大化按钮点了但窗口没最大化"

        # 再点一次（此时图标应切成 restore）回到普通态
        btn_max.click()
        app.processEvents()
        assert not win.isMaximized(), "第二次点击应从最大化还原"

        # 关闭：点击后窗口真的关闭
        btn_close.click()
        app.processEvents()
        assert not win.isVisible(), "关闭按钮点了但窗口仍然可见"

        assert btn_close.property("danger") == "true", (
            "关闭按钮应带 danger 属性（悬停转错误色，提示这一步不可逆）")
    finally:
        win.close()
        app.processEvents()


def test_subwindows_have_close_button(app):
    """三个子窗口也必须能关。

    子窗口走的是 `build_title_bar`（结构与主窗口不同），但"必须有关闭入口"
    这条要求一样 —— 否则用户打开设置后就关不掉，只能杀进程。
    """
    from pathlib import Path

    from core.appconfig import AppConfig
    from ui.settings_window import SettingsWindow
    from ui.vault_window import VaultWindow
    from ui.workdir_window import WorkdirWindow

    root = Path(__file__).resolve().parent.parent
    db = root / "data" / "jieya.db"

    cfg = AppConfig.ensure(root / "config.json")
    windows = [
        SettingsWindow(cfg, root / "config.json", root, db_path=db),
        VaultWindow(db),
        WorkdirWindow(root / ".workspace", db),
    ]
    try:
        for win in windows:
            kinds = {b._kind for b in _window_buttons(win)}
            assert "close" in kinds, f"{type(win).__name__} 缺少关闭按钮"
            # 子窗口尺寸固定，最小化没有意义（藏起来反而让用户找不到）
            assert "minimize" not in kinds, (
                f"{type(win).__name__} 不该有最小化按钮")
            assert win._title_bar is not None, (
                f"{type(win).__name__} 的标题栏没建起来 —— "
                "多半是 init_glass 把 _title_bar 重置成 None 了")
    finally:
        for win in windows:
            win.close()
        app.processEvents()


def test_icons_cover_window_controls():
    """图标表必须覆盖三个窗口按钮，且都是自绘矢量（不依赖字体码位）。

    用符号字体当图标在中文 Windows 上会渲染成豆腐块；本项目的图标全部
    自绘，所以这张表就是"有没有漏画"的唯一真相源。
    """
    from ui.icons import _DRAWERS

    for kind in ("minimize", "maximize", "restore", "close"):
        assert kind in _DRAWERS, f"缺少图标：{kind}"


def test_glass_surface_covers_whole_window(app):
    """玻璃底板必须铺满**整个窗口**，不只是 centralWidget。

    只铺 central 的话，QMainWindow 的状态栏会落在玻璃之外、露成一块纯黑
    （实测状态栏 y=590~620 完全没底板，文字压在纯黑上像被切掉一截）。
    """
    win = _make_main()
    try:
        win.show()
        app.processEvents()
        surface = win._glass_surface
        assert surface.geometry() == win.rect(), (
            f"玻璃底板 {surface.geometry()} 未覆盖整个窗口 {win.rect()}")
    finally:
        win.close()
        app.processEvents()


# ---- 无边框缩放：曾经整段失效过 ----
#
# 早期实现写的是 `Qt.Edge.TopLeft` 这类角名，但 `Qt.Edge` **只有四个单边成员**
# （LeftEdge / RightEdge / TopEdge / BottomEdge），角名不存在 → 每次鼠标移动
# 都抛 AttributeError，缩放功能整体失效，只在 stderr 里刷栈（用户能看到满屏
# 红字，界面却毫无反应）。
#
# 下面这几项守的是**可观测行为**——真的发鼠标事件、看窗口尺寸有没有变。
# 只断言"边常量存在"是恒判通过的那种写法：常量在，逻辑照样可以错。

def _drag(app, win, pos, dx, dy):
    """在 pos 按下并拖动 (dx, dy)，返回窗口尺寸变化 (dw, dh)。"""
    from PySide6.QtCore import Qt, QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    app.processEvents()
    g0 = win.geometry()
    local = QPointF(pos)
    g0p = QPointF(g0.x() + pos.x(), g0.y() + pos.y())
    gp = QPointF(g0p.x() + dx, g0p.y() + dy)

    app.sendEvent(win, QMouseEvent(
        QEvent.Type.MouseButtonPress, local, g0p,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    app.sendEvent(win, QMouseEvent(
        QEvent.Type.MouseMove, local, gp,
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    app.processEvents()
    g1 = win.geometry()
    app.sendEvent(win, QMouseEvent(
        QEvent.Type.MouseButtonRelease, local, gp,
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))
    return g1.width() - g0.width(), g1.height() - g0.height()


def test_hit_edge_detects_all_eight_zones(app):
    """八个区域（四条边 + 四个角）都要能判定，中央必须为 0（不是缩放热区）。"""
    from PySide6.QtCore import QPoint

    win = _make_main()
    win.resize(1000, 620)
    try:
        win.show()
        app.processEvents()
        L, R, T, B = (win._EDGE_L, win._EDGE_R, win._EDGE_T, win._EDGE_B)
        # 角 = 两条边的按位或；中央 = 0
        cases = {
            "左上角": (QPoint(2, 2), L | T),
            "上边中": (QPoint(500, 2), T),
            "右上角": (QPoint(998, 2), T | R),
            "左边中": (QPoint(2, 300), L),
            "中央": (QPoint(500, 300), 0),
            "右边中": (QPoint(998, 300), R),
            "左下角": (QPoint(2, 618), L | B),
            "下边中": (QPoint(500, 618), B),
            "右下角": (QPoint(998, 618), R | B),
        }
        for label, (pos, expect) in cases.items():
            assert win._hit_edge(pos) == expect, f"{label} 判定错误"
    finally:
        win.close()
        app.processEvents()


def test_corner_drag_resizes_both_axes(app):
    """拖角必须同时改宽和高 —— 这正是"角"存在的意义。

    早期实现把角当成 `Qt.Edge.TopLeft`（不存在），退化成抛异常；
    更早一版用 `e in (单边...)` 判断，组合值不等于任何单边，角会一条 if 都不
    命中 —— 窗口只会平移、不改尺寸。两条路都会让这条断言变红。
    """
    from PySide6.QtCore import QPoint

    win = _make_main()
    try:
        win.resize(1000, 620)
        win.show()
        app.processEvents()
        dw, dh = _drag(app, win, QPoint(2, 2), 40, 30)
        # 左/上边界右移/下移 → 宽度与高度同时变小
        assert dw != 0 and dh != 0, (
            f"拖角只改了 {dw}×{dh} —— 角必须同时改宽高")
    finally:
        win.close()
        app.processEvents()


def test_side_drag_changes_only_one_axis(app):
    """拖单边只改一个方向，绝不误改另一个。

    这条能挡住"角被当成单边"或"两个 if 写重了"这类实现错误。
    """
    from PySide6.QtCore import QPoint

    win = _make_main()
    try:
        for pos, dx, dy in ((QPoint(2, 300), 30, 0),      # 左
                            (QPoint(500, 2), 0, 40)):     # 上
            win.resize(1000, 620)
            win.show()
            app.processEvents()
            dw, dh = _drag(app, win, pos, dx, dy)
            assert (dw == 0) != (dh == 0), (
                f"单边拖动同时改了两轴：dw={dw} dh={dh}")
    finally:
        win.close()
        app.processEvents()


# ---- 移动期让路：Win10 Acrylic 拖动迟滞的 OS bug ----
#
# 这组守的是**性能修法本身**。那段迟滞不在 Python 回调里（实测 40 帧全部回调
# 只有 0.59 ms），而是 DWM 合成器线程的同步处理 —— 所以测 Python 侧耗时永远
# 得不出结论，只能按"已知 OS bug"处理：移动期把硬模糊整个撤掉。
#
# 少了这套逻辑，Windows 上拖窗口就是"拖不动、有奶油感"（三个独立来源可复现：
# EarTrumpet #349 / FluentWPF #42 / DevToys #1258）。

def _press(win, app, pos, gy=None):
    """在窗口本地坐标 pos 处按下左键，返回全局按下点。"""
    from PySide6.QtCore import Qt, QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    g = win.geometry()
    lp = QPointF(pos)
    gp = QPointF(g.x() + pos.x(), g.y() + pos.y())
    app.sendEvent(win, QMouseEvent(
        QEvent.Type.MouseButtonPress, lp, gp,
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))
    return gp


def _move(win, app, local, gp):
    from PySide6.QtCore import Qt, QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    app.sendEvent(win, QMouseEvent(
        QEvent.Type.MouseMove, QPointF(local), gp,
        Qt.MouseButton.NoButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier))


def _release(win, app, local, gp):
    from PySide6.QtCore import Qt, QEvent, QPointF
    from PySide6.QtGui import QMouseEvent

    app.sendEvent(win, QMouseEvent(
        QEvent.Type.MouseButtonRelease, QPointF(local), gp,
        Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier))
    app.processEvents()


# ---- 拖动让路：Win10 Acrylic 拖动迟滞的 OS bug ----
#
# 这组守的是**性能修法本身**。那段迟滞不在 Python 回调里（实测 40 帧全部回调
# 只有 0.59 ms），而是 DWM 合成器线程的同步处理 —— 所以测 Python 侧耗时永远
# 得不出结论，只能按"已知 OS bug"处理：拖动期把硬模糊整个撤掉。
#
# 实测各环节成本（本机）：surface.setStyleSheet 0.036ms、
# surface.repaint 2.75ms、capture_window_surface **13.2ms**。
# 所以拖动态**刻意不抓快照** —— 那 13ms 正是"每次开始拖动都卡一下"的元凶。

def test_click_does_not_trigger_drag_mode(app):
    """**点击（含手抖）绝不能触发拖动态。**

    这条直接对应"点一下卡一下"：阈值一旦偏小，点击时几像素的手抖就会被判成
    拖动，于是每点一次都白付一次让路开销（含早先版本 13ms 的抓屏）。
    """
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QColor

    win = _make_main()
    try:
        win.resize(1000, 620)
        win.show()
        app.processEvents()
        win._glass_on = True
        win._glass_suspended = False

        gp = _press(win, app, QPointF(500, 300))
        # 模拟点击时的手抖：6px。这个值刻意落在"旧阈值 4px"与"现阈值 8px"
        # 之间 —— 阈值一旦被改回 4px，这条立刻变红，能真正区分两种实现。
        _move(win, app, QPointF(500, 300), QPointF(gp.x() + 5, gp.y() + 3))
        app.processEvents()

        assert win._glass_suspended is False, (
            "6px 的手抖被判成了拖动 —— 用户点一下就会卡一下")
        assert win._dragging is False
    finally:
        win.close()
        app.processEvents()


def test_drag_does_not_jump_window(app):
    """**拖动时窗口必须跟手，不能跳。**

    早期实现把抓取偏移硬编码成 QPoint(120, 14)：不管在哪儿按下，窗口左上角都
    跳到「鼠标位置 − (120,14)」—— 在任务树中间按下时窗口会猛窜到鼠标左上方。
    正确做法是记下按下那一刻的窗口左上角，按**位移增量**平移。
    """
    from PySide6.QtCore import QPointF

    win = _make_main()
    try:
        win.resize(1000, 620)
        win.show()
        app.processEvents()
        win._glass_on = True

        # 在**远离标题栏**的位置按下（旧实现在这里跳得最明显）
        local = QPointF(500, 400)
        gp0 = _press(win, app, local)
        before = win.geometry().topLeft()

        dx, dy = 60, 40
        _move(win, app, local, QPointF(gp0.x() + dx, gp0.y() + dy))
        app.processEvents()
        after = win.geometry().topLeft()

        assert (after.x() - before.x(), after.y() - before.y()) == (dx, dy), (
            f"窗口位移 {(after.x()-before.x(), after.y()-before.y())} "
            f"不等于鼠标位移 {(dx, dy)} —— 窗口跳了，没有跟手")
    finally:
        win.close()
        app.processEvents()


def test_drag_suspends_acrylic_and_restores_after(app):
    """拖动开始必须让路，松手后必须恢复。"""
    from PySide6.QtCore import QPointF

    win = _make_main()
    try:
        win.resize(1000, 620)
        win.show()
        app.processEvents()
        win._glass_on = True
        win._glass_suspended = False

        local = QPointF(500, 400)
        gp0 = _press(win, app, local)
        _move(win, app, local, QPointF(gp0.x() + 40, gp0.y() + 30))
        app.processEvents()

        assert win._glass_suspended, (
            "拖动后没有让路 —— Win10 上拖窗口会严重迟滞")

        _release(win, app, local, QPointF(gp0.x() + 40, gp0.y() + 30))
        win._restore_glass()
        app.processEvents()

        # 无论 resume() 成功与否，挂起标记都必须归位：标记卡在 True 时
        # 后续拖动会以为"已经让过路了"而不再尝试恢复。
        assert not win._glass_suspended, (
            "松手后挂起标记没清 —— 窗口会永远停在无模糊的实色底板上")
    finally:
        win.close()
        app.processEvents()


def test_drag_mode_does_no_heavy_work_per_frame(app):
    """拖动期间**不得逐帧做重活**。

    让路本身（含一次 12ms 抓屏）只该在"确认进入拖动"那一次发生 —— 抓屏换来的是
    "拖动全程保持模糊观感"，这笔交换是划算的。但若混进**每帧路径**，
    `capture_window_surface`(12.09ms) + `repaint`(2.75ms) 会以每秒 60 次叠加，
    比不开玻璃还卡（这正是早期版本"点一下卡一下"的成因）。

    所以这里钉的是**位置**而不是"有没有抓屏"：
    `_enter_drag_mode`（一次性）可以有重活，`moveEvent`（每帧）绝不能有。
    """
    from pathlib import Path as _P
    import re as _re

    src = _P(__file__).resolve().parent.parent / "ui" / "glass.py"
    text = src.read_text(encoding="utf-8")

    # 静态层：每帧路径里不许出现任何重活
    m = _re.search(r"    def moveEvent\(self, event\).*?(?=\n    def )", text, _re.S)
    assert m, "找不到 moveEvent"
    body = m.group(0)
    for heavy in ("capture_window_surface", "repaint", "setStyleSheet"):
        assert heavy not in body, (
            f"moveEvent 里出现了 {heavy} —— 每帧都会执行，必卡。"
            f"让路应当只由 _enter_drag_mode 一次性完成")

    # 一次性路径允许抓屏，但必须真的只抓一次（下面动态层验证）
    m2 = _re.search(r"    def _enter_drag_mode.*?(?=\n    def )", text, _re.S)
    assert m2, "找不到 _enter_drag_mode"

    # 动态层：连续拖动多帧，让路只做一次
    from PySide6.QtCore import QPointF

    win = _make_main()
    try:
        win.resize(1000, 620)
        win.show()
        app.processEvents()
        win._glass_on = True
        win._glass_suspended = False

        calls = {"n": 0}
        orig = win._enter_drag_mode
        def counting():
            calls["n"] += 1
            return orig()
        win._enter_drag_mode = counting

        local = QPointF(500, 400)
        gp0 = _press(win, app, local)
        for i in range(12):
            _move(win, app, local, QPointF(gp0.x() + 12 + i * 6,
                                           gp0.y() + 12))
            app.processEvents()

        assert calls["n"] == 1, (
            f"12 帧里让路了 {calls['n']} 次 —— 应当只在首次进入拖动时做一次")
    finally:
        win.close()
        app.processEvents()


def test_freeze_keeps_background_under_translucent_plate(app):
    """移动期底板必须有**兜底**，不能是全透明。

    只有撤模糊、不补底板（且快照也失败）就是"移动时毛玻璃直接没了、只剩
    一块纯色"。这条钉住"宁可少一层模糊，也不能让底板消失"。
    """
    from ui import theme

    css = theme.glass_surface_qss(theme.GLASS_BASE_ALPHA, "#232427")
    assert "background: #232427" in css, "移动期底板必须是不透明兜底色"
    assert "background: transparent" not in css, "移动期底板不能是全透明"

    # 自绘玻璃形态（非 glass 态）同样必须有底
    css2 = theme.glass_surface_qss(theme.GLASS_BASE_ALPHA)
    assert "background: " in css2 and "transparent" not in css2


def test_no_restore_while_dragging(app):
    """**拖动期间绝不能再 resume** —— 那是"背景闪烁"的根因。

    早期实现靠 `QApplication.mouseButtons()` 轮询判断"手是否还按着"，并在每次
    移动时重启 settle 定时器。于是手指一旦停顿超过 settle 间隔（260ms），定时器
    就触发一次恢复 → 下次移动又撤下 → 循环。用户看到的就是背景一闪一闪。
    实测时序（拖动中每帧停顿 400ms）：suspend 之后 411ms 就冒出一次 resume，
    而那时 `_dragging` 仍为 True。

    修法是：拖动期间**不碰定时器**，恢复统一由 `mouseReleaseEvent` 触发；
    判据改用我们自己维护的 `_dragging` / `_resize_edge`。

    这条守卫刻意让每帧停顿**超过 settle 间隔** —— 否则复现不出原 bug。
    """
    import time as _t

    from PySide6.QtCore import QPointF
    from ui import glass as _glass_mod

    win = _make_main()
    try:
        win.resize(1000, 620)
        win.show()
        app.processEvents()
        win._glass_on = True
        win._glass_suspended = False

        calls = {"resume": 0}
        orig_resume = _glass_mod.resume

        def counting_resume(hwnd):
            calls["resume"] += 1
            return orig_resume(hwnd)

        _glass_mod.resume = counting_resume
        try:
            local = QPointF(500, 400)
            gp0 = _press(win, app, local)
            _move(win, app, local, QPointF(gp0.x() + 40, gp0.y() + 30))
            app.processEvents()
            assert win._glass_suspended, "起手没让路"

            # 每帧停顿 > settle 间隔：旧实现在这里会误恢复
            for i in range(3):
                _t.sleep(0.35)
                app.processEvents()
                assert win._glass_suspended, (
                    f"第 {i+1} 次停顿时让路被撤掉了 —— 背景会闪烁")
                _move(win, app, local, QPointF(gp0.x() + 50 + i * 10,
                                               gp0.y() + 30))
                app.processEvents()

            assert calls["resume"] == 0, (
                f"拖动期间 resume 被调用了 {calls['resume']} 次 —— "
                f"这正是'移动窗口时背景闪烁'的成因")

            # 松手后必须能正常恢复（别修好闪烁却恢复不了）
            _release(win, app, local, QPointF(gp0.x() + 90, gp0.y() + 30))
            deadline = _t.time() + 1.5
            while _t.time() < deadline:
                app.processEvents()
                _t.sleep(0.03)
                if not win._glass_suspended:
                    break
            assert not win._glass_suspended, "松手后没有恢复 —— 永久停在无模糊态"
            assert calls["resume"] >= 1, "松手后没有调用 resume"
        finally:
            _glass_mod.resume = orig_resume
    finally:
        win.close()
        app.processEvents()


def test_second_drag_does_not_resume_acrylic(app):
    """连续拖动时，第二次拖动**不得**把 Acrylic 装回来。

    这是"左右移动有几率变慢"的根因：用户在上一轮拖动的 settle 窗口内又开始拖，
    此时 `_restore_glass` 触发，旧实现会用 `QApplication.mouseButtons()` 判断
    "手是否还按着" —— 一旦误判成已松开，就清掉 `_dragging` 并 `resume()`。
    Acrylic 一回来，Win10 的拖动迟滞立刻生效，窗口就"慢慢才挪过来"。

    离屏环境下 `mouseButtons()` 恒为 `NoButton`，所以这条在旧实现上**必然**复现。
    """
    from PySide6.QtCore import QPointF
    from ui import glass as _glass_mod

    win = _make_main()
    try:
        win.resize(1000, 620)
        win.show()
        app.processEvents()
        win._glass_on = True
        win._glass_suspended = True      # 上一轮拖动的挂起还没恢复
        win._restore_pending = False
        win._dragging = False

        # 第二次拖动开始
        local = QPointF(500, 400)
        gp0 = _press(win, app, local)
        _move(win, app, local, QPointF(gp0.x() + 40, gp0.y() + 30))
        app.processEvents()
        assert win._dragging, "第二次拖动没有进入拖动状态"
        assert win._glass_suspended, "第二次拖动不该重新让路（Acrylic 本来就挂着）"

        # 模拟上一轮的 settle 到期
        calls = {"resume": 0}
        orig_resume = _glass_mod.resume

        def counting(hwnd):
            calls["resume"] += 1
            return orig_resume(hwnd)

        _glass_mod.resume = counting
        try:
            win._restore_glass()
            app.processEvents()
        finally:
            _glass_mod.resume = orig_resume

        assert calls["resume"] == 0, (
            "第二次拖动期间调用了 resume() —— Acrylic 被装回来，"
            "拖动会迟滞（'窗口慢慢才挪过来'）")
        assert win._dragging, (
            "`_dragging` 被误判清掉了 —— 手势标记不该依赖 mouseButtons()")
        assert win._glass_suspended, "让路状态不该被撤掉"
    finally:
        win.close()
        app.processEvents()


def test_drag_during_restore_grace_re_suspends(app):
    """grace 收尾期间又开始拖动 → 必须**重新**撤下 Acrylic。

    grace 阶段 Acrylic 已经被 `resume()` 装回来了，只是快照还挡着。
    此时若只看 `_glass_suspended`（它还是 True），就会跳过让路 ——
    于是整轮拖动都在 Acrylic 下进行，迟滞回归。
    """
    from PySide6.QtCore import QPointF
    from ui import glass as _glass_mod

    win = _make_main()
    try:
        win.resize(1000, 620)
        win.show()
        app.processEvents()
        win._glass_on = True

        orig_resume = _glass_mod.resume
        _glass_mod.resume = lambda hwnd: True
        try:
            # 摆出"grace 收尾中"的状态
            win._glass_suspended = True
            win._dragging = False
            win._resize_edge = 0
            win._restore_pending = False
            win._restore_glass()          # → resume 成功，进入 pending
            app.processEvents()
            assert win._restore_pending, "没有进入 grace 阶段"
            resets = {"suspend": 0}

            orig_suspend = _glass_mod.suspend

            def counting_suspend(hwnd):
                resets["suspend"] += 1
                return orig_suspend(hwnd)

            _glass_mod.suspend = counting_suspend
            try:
                local = QPointF(500, 400)
                gp0 = _press(win, app, local)
                _move(win, app, local, QPointF(gp0.x() + 40, gp0.y() + 30))
                app.processEvents()
            finally:
                _glass_mod.suspend = orig_suspend

            assert resets["suspend"] >= 1, (
                "grace 期间开始拖动却没有重新撤下 Acrylic —— 拖动会迟滞")
            assert not win._restore_pending, (
                "grace 收尾没有被取消，到期后会把新快照撤掉（又闪一下）")
        finally:
            _glass_mod.resume = orig_resume
    finally:
        win.close()
        app.processEvents()


def test_restore_finishes_quickly_after_release(app):
    """松手后必须在 ~150ms 内恢复模糊。

    用户实测反馈是"**你动了之后，模糊效果会消失**" —— 拖动期间本来就没模糊
    （半透明底板），松手后再空一段，"无模糊"的持续感会很强。实测 300ms 就会被
    明确感知成"模糊没恢复"。

    所以 `_MOVE_SETTLE_MS` 不能想当然地留长：它的合理值取决于**让路有多贵**。
    还抓快照时（12ms）留长一点合并抖动是值得的；改用半透明底板后让路只要
    4.72ms，就该把间隔一起压短（当前 40ms + grace 32ms ≈ 70~120ms）。

    这条守卫把"总延迟"钉成一个上界 —— 以后谁把 settle 调回去，立刻变红。
    """
    import time as _t

    from PySide6.QtCore import QPointF
    from ui import glass as _glass_mod

    win = _make_main()
    try:
        win.resize(1000, 620)
        win.show()
        app.processEvents()
        win._glass_on = True

        orig_resume = _glass_mod.resume
        _glass_mod.resume = lambda hwnd: True     # 强制走成功路径（含 grace）
        try:
            win._glass_suspended = False
            win._restore_pending = False
            win._dragging = False
            win._resize_edge = 0

            local = QPointF(500, 400)
            gp0 = _press(win, app, local)
            _move(win, app, local, QPointF(gp0.x() + 40, gp0.y() + 30))
            app.processEvents()
            assert win._glass_suspended, "拖动没让路"

            _release(win, app, local, QPointF(gp0.x() + 40, gp0.y() + 30))
            t0 = _t.time()
            while _t.time() - t0 < 1.0:
                app.processEvents()
                _t.sleep(0.004)
                if not win._glass_suspended and not win._restore_pending:
                    break
            elapsed = (_t.time() - t0) * 1000

            assert not win._glass_suspended, "松手后没有恢复"
            assert elapsed < 250, (
                f"松手到恢复用了 {elapsed:.0f} ms —— 超过 ~300ms 用户会明确"
                f"感觉成'模糊没恢复'。检查 _MOVE_SETTLE_MS / _RESTORE_GRACE_MS")
        finally:
            _glass_mod.resume = orig_resume
    finally:
        win.close()
        app.processEvents()


def test_restore_waits_for_acrylic_before_clearing(app):
    """`resume()` 之后必须**等一小段**才撤快照、底板转透明。

    `resume()` 只是把模糊意图交给 DWM，真正画出 Acrylic 需要一整帧；而 Qt 的
    快照/底板可以立刻消失。两者之间不隔开，就会露出一帧"底板透明 + Acrylic 还
    没画"的画面 —— 用户的原话是**"放下来的时候就闪一下"**。

    离屏环境下 `resume()` 恒失败，走不到这条路径，所以这里把它 patch 成成功，
    专门验证"成功路径"的时序。
    """
    import time as _t

    from ui import glass as _glass_mod

    win = _make_main()
    try:
        win.resize(1000, 620)
        win.show()
        app.processEvents()

        orig_resume = _glass_mod.resume
        _glass_mod.resume = lambda hwnd: True
        try:
            win._glass_on = True
            win._glass_suspended = True
            win._dragging = False
            win._resize_edge = 0
            # 摆出"拖动刚结束"的底板（半透明 + 已挂起），等待恢复
            from ui import theme as _theme
            win._glass_surface.setStyleSheet(
                _theme.glass_surface_qss(_theme.GLASS_BASE_ALPHA))
            win._restore_pending = False

            win._restore_glass()
            app.processEvents()

            # 立刻检查：**不能**已经撤掉遮挡
            assert win._restore_pending, (
                "resume() 后立刻就撤了快照 —— 会露出一帧没画好的背景，"
                "用户看到的就是'松手时闪一下'")
            assert "transparent" not in win._glass_surface.styleSheet(), (
                "resume() 后立刻把底板转透明了 —— 同上")
            assert win._glass_suspended, (
                "resume() 后立刻清了挂起标记，遮挡却还在，状态不自洽")

            # 等 grace 期满
            deadline = _t.time() + 1.0
            while _t.time() < deadline:
                app.processEvents()
                _t.sleep(0.01)
                if not win._restore_pending:
                    break

            assert not win._restore_pending, "grace 之后没有收尾"
            assert "transparent" in win._glass_surface.styleSheet(), (
                "收尾时底板没有转透明")
            assert not win._glass_suspended, "收尾时没清挂起标记"
            css = win._glass_surface.styleSheet()
            assert "transparent" in css, "收尾时底板没有转透明"
        finally:
            _glass_mod.resume = orig_resume
    finally:
        win.close()
        app.processEvents()


def test_native_blur_is_off_by_default():
    """原生模糊默认**关闭** —— 这是当前的产品决策，不能被随手打开。

    打开它会重新引入整条问题链：拖动迟滞（Win10 OS bug）→ 只能在拖动期临时
    撤掉模糊 → 于是有了"静止模糊 ↔ 拖动不模糊"两个状态 → 观感不连贯，还连带
    出闪烁、松手恢复延迟、"窗口慢慢才挪过来"。

    用户对那条路的评价是"**不太有连贯性**"，并明确要求"平时也直接透明，
    而不使用模糊效果"。所以这条守卫把开关钉死；真要开回来，必须同时改掉
    本文件里所有拖动让路守卫的语义。
    """
    from ui import glass as _glass_mod

    assert _glass_mod.USE_NATIVE_BLUR is False, (
        "USE_NATIVE_BLUR 被打开了 —— 会重新引入「静止模糊 ↔ 拖动不模糊」的"
        "状态切换，用户明确要求避免这种不连贯")


def test_plate_stays_identical_throughout_drag(app):
    """**底板形态必须全程唯一**：拖动不得改变外观。

    这是"观感连贯性"的直接守卫。放弃原生模糊后，静止 / 拖动 / 缩放 / 松手
    应当全都是同一个半透明底板 —— 样式字符串必须**完全一致**，且一次
    `suspend`/`resume` 都不发生。

    对比历史：早先方案静止时是模糊玻璃、拖动时把模糊撤下换成半透明，两个
    状态来回切。用户的原话是"不太有连贯性"。
    """
    import re as _re

    from PySide6.QtCore import QPointF
    from ui import glass as _glass_mod

    win = _make_main()
    try:
        win.resize(1000, 620)
        win.show()
        app.processEvents()

        def plate() -> str:
            m = _re.search(r"background:\s*([^;]+);",
                           win._glass_surface.styleSheet())
            assert m, "底板样式里没有 background"
            return m.group(1).strip()

        calls = {"n": 0}
        orig_suspend, orig_resume = _glass_mod.suspend, _glass_mod.resume

        def counting_suspend(hwnd):
            calls["n"] += 1
            return orig_suspend(hwnd)

        def counting_resume(hwnd):
            calls["n"] += 1
            return orig_resume(hwnd)

        _glass_mod.suspend = counting_suspend
        _glass_mod.resume = counting_resume
        try:
            before = plate()

            local = QPointF(500, 400)
            gp0 = _press(win, app, local)
            _move(win, app, local, QPointF(gp0.x() + 60, gp0.y() + 40))
            app.processEvents()
            during = plate()

            _release(win, app, local, QPointF(gp0.x() + 60, gp0.y() + 40))
            for _ in range(30):
                app.processEvents()
            after = plate()
        finally:
            _glass_mod.suspend = orig_suspend
            _glass_mod.resume = orig_resume

        assert before == during == after, (
            f"底板在拖动中变了：{before!r} → {during!r} → {after!r}\n"
            f"任何形态切换都会被感知成'不连贯'")
        assert calls["n"] == 0, (
            f"拖动过程发生了 {calls['n']} 次 suspend/resume —— "
            f"原生模糊已关闭，不该有任何让路行为")
    finally:
        win.close()
        app.processEvents()


def test_drag_plate_is_translucent_not_transparent(app):
    """拖动期底板必须是**半透明**：透得出去，又不能全透明。

    拖动时系统模糊被撤下，底板直接决定观感。用户的取舍是"**背景要实时更新**"
    （而不是定格的快照、也不是完全看不到背景的实色块）：

    - 做成 `transparent` → 桌面**原样**透出，没有压暗，文字对比度掉下来；
    - alpha 太高（≈1.0） → 等同于实色块，背景等于看不见；
    - 所以取一个中间值，既让桌面实时可见，又保住文字可读。

    这条同时挡两边的回归：既不许退回"完全不透明"，也不许图省事写 `transparent`。
    """
    import re as _re

    win = _make_main()
    try:
        win.resize(1000, 620)
        win.show()
        app.processEvents()
        win._glass_on = True
        win._enter_drag_mode()
        app.processEvents()

        css = win._glass_surface.styleSheet()
        m = _re.search(r"background:\s*([^;]+);", css)
        assert m, f"底板样式里没有 background：{css!r}"
        bg = m.group(1).strip()
        assert "transparent" not in bg, (
            "拖动期底板写成了 transparent —— 桌面会原样透出、没有压暗，"
            "文字对比度会掉下来")
        assert bg.startswith("rgba"), (
            f"拖动期底板 {bg!r} 不是半透明色 —— 背景没法实时透出来")
        alpha = float(bg.rstrip(")").split(",")[-1])
        assert 0.3 <= alpha <= 0.95, (
            f"拖动期底板透明度 {alpha} 超出合理区间："
            f"太低则文字读不清，太高则等于看不到背景")
    finally:
        win.close()
        app.processEvents()


def test_screen_capture_import_is_top_level(app):
    """截屏模块必须用**绝对**导入。

    本项目 `ui/` 是顶层包，而 `glass.py` 是从 `src/ui/` 移植来的 —— 那里
    `from ..core.screen` 恰好成立，移植后不改会抛
    `attempted relative import beyond top-level package`，且该异常被
    `capture_window_surface` 吞掉 → 恒返回 None → 移动期拿不到定格快照，
    只剩一块纯色。症状与"让路机制没写"完全一样，极难定位。
    """
    from pathlib import Path

    src = Path(__file__).resolve().parent.parent / "ui" / "glass.py"
    text = src.read_text(encoding="utf-8")
    assert "from ..core" not in text, (
        "glass.py 里仍有越层的 `from ..core` 相对导入 —— "
        "移植后 ui/ 是顶层包，会导致截屏静默失败")
    assert "from core.screen import" in text