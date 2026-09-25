"""拖入即开始的端到端守卫。

用户反馈「拖入启动没有实现」——根因是只有 108px 高的输入列表接受拖放，
拖到窗口其他任何地方都被 Qt 丢弃。这组测试把「窗口整体是拖放目标」钉死，
并覆盖拖入后自动启动的完整链路。
"""
import os
import subprocess
import time

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过 GUI 拖放测试")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    from ui import theme

    inst = QApplication.instance() or QApplication([])
    inst.setStyleSheet(theme.build_stylesheet())
    return inst


def _drop_payload(tmp_path, name="pack.zip"):
    """造一个真实存在的待拖入文件（add_paths 会过滤不存在的路径）。"""
    p = tmp_path / name
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    return p


def _mime(paths):
    from PySide6.QtCore import QMimeData, QUrl

    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
    return mime


def _drag_drop(target, mime) -> tuple[bool, bool]:
    """完整模拟一次真实拖放：dragEnter → dragMove → drop。

    必须带上前半段——item view 的 drop 依赖 dragEnter 阶段建立的落点状态，
    只投 drop 会得到一个与真实使用无关的假失败。

    只返回布尔值：把 Qt 事件对象交给 pytest 断言，saferepr 会去读已被回收的
    C++ 对象，直接段错误。
    """
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
    from PySide6.QtWidgets import QApplication

    pos = QPoint(10, 10)
    enter = QDragEnterEvent(pos, Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(target, enter)
    move = QDragMoveEvent(pos, Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(target, move)
    drop = QDropEvent(pos, Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    QApplication.sendEvent(target, drop)
    return bool(enter.isAccepted()), bool(drop.isAccepted())


def test_window_drag_enter_accepts_file_urls(app, tmp_path):
    """窗口本体必须接受文件拖入。

    QMainWindow 的 acceptDrops 默认为 true（Qt 为工具栏/停靠区打开），但
    QWidget 默认的 dragEnterEvent 会直接 ignore——不实现它，整个窗口就是
    拖放死区：光标显示禁止、松手什么都不发生。
    """
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        accepted, _ = _drag_drop(win, _mime([_drop_payload(tmp_path)]))
        assert accepted is True, "主窗口未接受文件拖入，窗口大部分区域是死区"
    finally:
        win.close()
    app.processEvents()


def test_drop_on_window_body_adds_input(tmp_path, app):
    """拖到窗口本体（非输入列表）：入列表 + 启动合并窗口计时器。"""
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        payload = _drop_payload(tmp_path)
        _drag_drop(win, _mime([payload]))
        app.processEvents()

        assert win.input_list.real_paths() == [str(payload)], "拖到窗口空白区未加入输入"
        assert win._autorun_timer.isActive(), "拖到窗口空白区后未触发自动开始"
    finally:
        win.close()
    app.processEvents()


def test_drop_on_input_list_still_works(tmp_path, app):
    """回归守护：输入列表自身的拖放通路不能被改动破坏。

    真实拖放里事件落在列表的 viewport 上（子控件覆盖了列表本体）。
    """
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        payload = _drop_payload(tmp_path)
        _drag_drop(win.input_list.viewport(), _mime([payload]))
        app.processEvents()

        assert win.input_list.real_paths() == [str(payload)]
        assert win._autorun_timer.isActive()
    finally:
        win.close()
    app.processEvents()


def test_drop_dir_and_file_together(tmp_path, app):
    """一次拖入目录 + 文件：两者都成为输入，且只合并成一次启动。"""
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        folder = tmp_path / "batch"
        folder.mkdir()
        payload = _drop_payload(tmp_path)
        _drag_drop(win, _mime([folder, payload]))
        app.processEvents()

        assert set(win.input_list.real_paths()) == {str(folder), str(payload)}
        assert win._autorun_timer.isActive()
    finally:
        win.close()
    app.processEvents()


def test_autorun_starts_extraction_after_delay(tmp_path, app, monkeypatch):
    """合并窗口到点后必须真的启动解压（不只是计时器起来）。"""
    from PySide6.QtTest import QTest

    from ui import main_window as mw

    win = mw.MainWindow()
    try:
        win._cfg = win._cfg.with_changes(autorun_delay_ms=30)
        started = []
        monkeypatch.setattr(win, "_start", lambda: started.append(True))

        payload = _drop_payload(tmp_path)
        _drag_drop(win, _mime([payload]))
        QTest.qWait(400)

        assert started == [True], "拖入后自动启动未发生"
    finally:
        win.close()
    app.processEvents()


def test_autorun_off_does_not_arm(tmp_path, app):
    """关掉自动开始后，拖入只入列表、不启动。"""
    from core.appconfig import AUTORUN_OFF
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        win._cfg = win._cfg.with_changes(autorun_mode=AUTORUN_OFF)
        payload = _drop_payload(tmp_path)
        _drag_drop(win, _mime([payload]))
        app.processEvents()

        assert win.input_list.real_paths() == [str(payload)]
        assert not win._autorun_timer.isActive()
    finally:
        win.close()
    app.processEvents()


def test_autorun_end_to_end_produces_files(tmp_path, app):
    """拖入 → 真的解压出文件。

    前一条把 _start 换成了探针，只能证明「启动了」；这条用真 7z 跑完全程，
    证明用户拖进去之后桌面上确实出现产物。
    """
    from core.config import detect_sevenzip

    try:
        exe = detect_sevenzip()
    except FileNotFoundError:
        pytest.skip("未找到 7z.exe，跳过拖入端到端测试")

    from PySide6.QtTest import QTest

    from core.appconfig import AUTORUN_DIRECT, OPEN_NONE, OUTPUT_SAMEDIR
    from ui.main_window import MainWindow

    src = tmp_path / "dl"
    src.mkdir()
    (src / "note.txt").write_text("拖入即解压", encoding="utf-8")
    subprocess.run([str(exe), "a", "pack.zip", "note.txt"],
                   cwd=str(src), capture_output=True)
    (src / "note.txt").unlink()  # 只剩包，产物才是解压出来的

    win = MainWindow()
    try:
        win._cfg = win._cfg.with_changes(
            autorun_mode=AUTORUN_DIRECT, autorun_delay_ms=20,
            output_mode=OUTPUT_SAMEDIR, subdir_name="_解压开镜",
            open_after=OPEN_NONE,  # 否则跑完会去拉 Explorer
        )
        _drag_drop(win, _mime([src / "pack.zip"]))

        out = src / "_解压开镜" / "pack" / "note.txt"
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            QTest.qWait(50)
            if out.is_file() and win._thread is None:
                break

        assert out.is_file(), "拖入后没有真的解压出文件"
        assert out.read_text(encoding="utf-8") == "拖入即解压"
    finally:
        win.close()
    app.processEvents()


def test_non_file_drag_not_taken_over(app):
    """纯文本拖放不接管：窗口只认文件 URL。"""
    from PySide6.QtCore import QMimeData

    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        text = QMimeData()
        text.setText("hello")
        accepted, _ = _drag_drop(win, text)
        assert accepted is False, "非文件拖放不应被窗口接管"
    finally:
        win.close()
    app.processEvents()
