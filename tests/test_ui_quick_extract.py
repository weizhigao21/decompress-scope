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
        # 输入区常驻：提交后**不收起**，只清空输入框并说明"已收到"。
        # 收起会让用户以为这一轮结束了，而 pipeline 可能还要重问（密码不对）。
        assert win.password_panel.isVisible(), "提交密码后输入区被收起来了"
        assert win.password_input.text() == "", "提交后没清空输入框"
        assert "已收到" in win.password_hint.text()
        assert win.skip_button.isEnabled() is False, "回答已交出，没有可跳过的了"

        win._on_need_password({"path": "C:/Downloads/pack.zip"})
        assert "密码不正确" in win.password_hint.text()
        win.skip_button.click()
        assert worker.answers == ["secret", None]
    finally:
        win._worker = None
        win.close()
        app.processEvents()


def test_quick_window_password_box_is_up_from_the_start(app):
    """密码输入区常驻：打开就能填，不用等后台把候选试完。"""
    from ui.quick_extract_window import QuickExtractWindow

    win = QuickExtractWindow(["C:/Downloads/pack.zip"], autostart=False)
    try:
        win.show()
        app.processEvents()
        assert win.password_panel.isVisible(), "密码输入区没有一开始就亮出来"
        assert win.password_input.isVisible()
        assert "自动尝试" in win.password_hint.text()
        # pipeline 还没开口问，没有什么可跳过的
        assert win.skip_button.isEnabled() is False
        assert "取消" in win.skip_button.toolTip()
    finally:
        win.close()
        app.processEvents()


def test_quick_window_typed_password_reaches_worker_without_being_asked(app):
    """先手输入：pipeline 还在试候选，用户就把密码交了 —— 必须送达 worker。"""
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
        app.processEvents()
        # 没有任何 need_password：用户自己先填了
        win.password_input.setText("secret9")
        win.password_button.click()

        assert worker.answers == ["secret9"], "先手输入的密码没送到 worker"
        assert win.password_panel.isVisible()
        assert win.password_input.text() == ""
        assert "已收到" in win.password_hint.text()
        assert "正在验证" in win.status_label.text()
    finally:
        win._worker = None
        win.close()
        app.processEvents()


def test_quick_window_empty_submit_hint_follows_waiting_state(app):
    """空提交的提示要分状态：pipeline 问着才提"跳过此包"，否则那按钮是禁用的。"""
    from ui.quick_extract_window import QuickExtractWindow

    win = QuickExtractWindow(["C:/Downloads/pack.zip"], autostart=False)
    try:
        win.show()
        app.processEvents()
        win.password_button.click()      # 空输入，pipeline 没问
        assert "跳过此包" not in win.password_hint.text(), (
            "pipeline 没开口就让人点「跳过此包」，而那个按钮此刻是禁用的")

        win._on_need_password({"path": "C:/Downloads/pack.zip"})
        win.password_button.click()      # 空输入，pipeline 正问着
        assert "跳过此包" in win.password_hint.text()
    finally:
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
    # 偏好也要隔离：否则 _maybe_open_results 会读到开发者本机的 config.json，
    # 在测试机上真的弹一个资源管理器窗口（还会顺手把本窗关掉）。
    from core.appconfig import OPEN_NONE, AppConfig

    AppConfig(open_after=OPEN_NONE).save(tmp_path / "config.json")
    monkeypatch.setattr(quick, "CONFIG_PATH", tmp_path / "config.json")
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
        # 输入区现在常驻，"面板可见"不再是"程序开口问了"的信号，
        # 改盯 _waiting_for_pipeline（need_password 到达才为真）。
        while not win._waiting_for_pipeline and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(20)
        assert win._waiting_for_pipeline, "工作线程没有现场询问密码"
        assert "自动密码未匹配" in win.password_hint.text()
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


def test_quick_window_typed_password_wins_before_candidates(app, tmp_path, monkeypatch):
    """用户**先手**填的密码：不等候选试完就开解，也不会被弹窗打扰。

    这是右键场景的主诉求：手里攥着密码的人，不该看着程序把候选挨个跑完
    （7z-AES 每个候选约 0.3 s）。文件名刻意不带密码线索，候选猜不到它。
    """
    from PySide6.QtTest import QTest
    from core.appconfig import OPEN_NONE, AppConfig
    from core.config import Config, detect_sevenzip
    import ui.quick_extract_window as quick

    try:
        sevenzip = detect_sevenzip()
    except FileNotFoundError:
        pytest.skip("未找到 7z.exe")
    source = tmp_path / "note.txt"
    source.write_text("typed ahead password", encoding="utf-8")
    archive = tmp_path / "pack.zip"
    result = subprocess.run(
        [str(sevenzip), "a", "-psecret9", str(archive), source.name],
        cwd=tmp_path, capture_output=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    source.unlink()

    monkeypatch.setattr(quick, "DEFAULT_DB", tmp_path / "app.db")
    AppConfig(open_after=OPEN_NONE).save(tmp_path / "config.json")
    monkeypatch.setattr(quick, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(
        quick.QuickExtractWindow, "_make_cfg",
        lambda self: Config.create(sevenzip=str(sevenzip), workdir=tmp_path / "wd"),
    )

    win = quick.QuickExtractWindow([str(archive)], autostart=False)
    asked: list[dict] = []
    phases: list[str] = []
    # 只记录、不碰 Qt。这两个槽会被工作线程**直接**调用（给信号连一个没有接收者
    # 上下文的普通可调用对象时，PySide6 是直连，不排队到主线程）—— 在这里更新
    # 界面就是跨线程碰控件，离屏环境下会 access violation 整个测试进程。
    # 本用例要证明的正是"程序一次都没问过"，所以不需要界面参与。
    win._on_need_password = asked.append
    win._on_event = lambda ev: (
        phases.append(str(ev.get("message")))
        if ev.get("kind") == "phase" else None
    )
    try:
        win.show()
        win._start()
        # _start() 回来就交密码：此刻 pipeline 还在探测/试候选，没开口问
        win.password_input.setText("secret9")
        win.password_button.click()
        assert "已收到" in win.password_hint.text()

        deadline = time.monotonic() + 20
        while win._report is None and time.monotonic() < deadline:
            app.processEvents()
            QTest.qWait(20)

        assert win._report is not None, "20 秒内没有跑完"
        assert win._report.done == 1, f"先手密码没解成功：{win._report}"
        assert win._report.needs_password == 0
        assert asked == [], "用户已经先手给了密码，程序还弹窗问了一次"
        assert "正在用你输入的密码解压" in phases, (
            f"没走先手密码那条路（phase={phases}）—— 候选没有被提前收手")
        assert any(p.read_text(encoding="utf-8") == "typed ahead password"
                   for p in tmp_path.rglob("note.txt"))
    finally:
        # 收尾预算必须**现算**：复用上面那个 deadline 时它可能已经过期，
        # 等待预算变成 0 → 直接 close() 把还在跑的 QThread 留给进程退出，
        # 离屏环境下就是一次 access violation（整个测试进程崩掉，而不是报失败）。
        if win._thread is not None and win._thread.isRunning():
            win._request_cancel()
            stop = time.monotonic() + 10
            while win._thread is not None and time.monotonic() < stop:
                app.processEvents()
                QTest.qWait(20)
        win.close()
        app.processEvents()


def test_quick_window_reports_repaired_names_after_done(app, tmp_path, monkeypatch):
    """「已修正 N 个乱码文件名」要活到"解压完成"那句里，例子挂 tooltip。

    notice 事件在 done 之前到达，窗口若直接 setText，会被紧接着的完成语盖掉
    （用户根本读不到）。例子一行放不下，就只能进 tooltip。
    """
    from core.pipeline import RunReport
    from ui.quick_extract_window import QuickExtractWindow

    out = tmp_path / "out"
    out.mkdir()
    win = QuickExtractWindow(["C:/Downloads/pack.zip"], autostart=False)
    # 自动打开结果目录是另一条链路（还会碰真实偏好/资源管理器），这里不参与
    monkeypatch.setattr(win, "_maybe_open_results", lambda: None)
    try:
        win._on_event({"kind": "task", "path": "C:/Downloads/pack.zip"})
        win._on_event({"kind": "notice",
                       "message": "已修正 7 个乱码文件名",
                       "detail": "已修正 7 个乱码文件名（Shift-JIS），例如 A.txt → B.txt"})
        win._on_event({"kind": "status", "status": "done"})

        report = RunReport(done=1, output_dirs=[str(out)])
        win._on_finished(report)

        text = win.status_label.text()
        assert "解压完成" in text
        assert "已修正 7 个乱码文件名" in text
        assert "Shift-JIS" in win.status_label.toolTip(), "例子没地方看"
    finally:
        win.close()
        app.processEvents()
