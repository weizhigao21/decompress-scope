"""紧凑右键解压窗口的静态行为守卫。"""
import os
import subprocess
import time
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过 GUI 测试")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication
    from ui import theme

    inst = QApplication.instance() or QApplication([])
    inst.setStyleSheet(theme.build_stylesheet())
    return inst


def test_quick_window_has_no_configuration_controls(app):
    from ui.quick_extract_window import QuickExtractWindow

    win = QuickExtractWindow(["C:/Downloads/pack.zip"], autostart=False)
    try:
        assert win.windowTitle() == "解压开镜"
        assert win.path_label.text() == "pack.zip"
        assert win.open_button.isEnabled() is False
        assert win.cancel_button.text() == "取消"
    finally:
        win.close()
        app.processEvents()


def test_quick_window_explains_progress_and_waiting_stage(app):
    """7-Zip 还没有吐百分比时，右键窗口也必须说明仍在工作。"""
    from ui.quick_extract_window import QuickExtractWindow

    win = QuickExtractWindow(["C:/Downloads/pack.zip"], autostart=False)
    try:
        win._on_event({"kind": "task", "path": "C:/Downloads/pack.zip"})
        win._on_event({"kind": "status", "status": "extracting"})
        assert win.progress.minimum() == 0
        assert win.progress.maximum() == 0
        assert "等待进度反馈" in win.status_label.text()

        win._on_event({"kind": "progress", "percent": 42})
        assert win.progress.value() == 42
        assert "42%" in win.status_label.text()
    finally:
        win.close()
        app.processEvents()


def test_quick_window_password_is_visible_and_submitted_inline(app):
    """密码请求不能藏在另一个模态窗口后面，让工作线程一直等待。"""
    from PySide6.QtWidgets import QLineEdit
    from PySide6.QtCore import Qt
    from ui.quick_extract_window import QuickExtractWindow

    class FakeWorker:
        def __init__(self):
            self.answers = []

        def submit_password(self, value):
            self.answers.append(value)

    win = QuickExtractWindow(["C:/Downloads/pack.zip"], autostart=False)
    worker = FakeWorker()
    win._worker = worker
    try:
        win.show()
        assert win.windowModality() == Qt.WindowModality.NonModal
        win._on_need_password({"path": "C:/Downloads/pack.zip"})
        app.processEvents()
        assert win.password_panel.isVisible()
        assert win.password_input.isVisible()
        assert win.password_input.echoMode() == QLineEdit.EchoMode.Password
        assert "等待输入密码" in win.status_label.text()
        assert win.progress.maximum() == 100  # 暂停等人时不应显示忙碌动画

        win.password_input.setText("secret")
        win.password_button.click()
        assert worker.answers == ["secret"]
        assert not win.password_panel.isVisible()

        win._on_need_password({"path": "C:/Downloads/pack.zip"})
        assert "密码不正确" in win.password_hint.text()
        win.skip_button.click()
        assert worker.answers == ["secret", None]
    finally:
        win._worker = None
        win.close()
        app.processEvents()


def test_quick_window_extracts_encrypted_archive_after_password(app, tmp_path, monkeypatch):
    """真实右键工作线程应显示输入区、接收密码并完成落地。"""
    from PySide6.QtTest import QTest
    from core.config import Config, detect_sevenzip
    import ui.quick_extract_window as quick

    try:
        sevenzip = detect_sevenzip()
    except FileNotFoundError:
        pytest.skip("未找到 7z.exe")
    source = tmp_path / "note.txt"
    source.write_text("right click password", encoding="utf-8")
    archive = tmp_path / "pack.zip"
    result = subprocess.run(
        [str(sevenzip), "a", "-psecret9", str(archive), source.name],
        cwd=tmp_path, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    source.unlink()

    monkeypatch.setattr(quick, "DEFAULT_DB", tmp_path / "app.db")
    monkeypatch.setattr(
        quick.QuickExtractWindow, "_make_cfg",
        lambda self: Config.create(sevenzip=str(sevenzip), workdir=tmp_path / "wd"),
    )
    win = quick.QuickExtractWindow([str(archive)], autostart=False)
    errors = []
    monkeypatch.setattr(win, "_show_error", errors.append)
    try:
        win.show()
        win._start()
        deadline = time.monotonic() + 12
        while not win.password_panel.isVisible() and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(20)
        assert win.password_panel.isVisible(), "工作线程未显示密码输入区"
        win.password_input.setText("secret9")
        win.password_button.click()
        while win._report is None and not errors and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(20)
        assert not errors
        assert win._report is not None and win._report.done == 1
        assert any(p.read_text(encoding="utf-8") == "right click password"
                   for p in tmp_path.rglob("note.txt"))
    finally:
        if win._thread is not None and win._thread.isRunning():
            win._request_cancel()
            while win._thread is not None and time.monotonic() < deadline + 3:
                app.processEvents()
                QTest.qWait(20)
        win.close()
        app.processEvents()
