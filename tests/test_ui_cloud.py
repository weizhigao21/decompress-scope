"""云解密的界面接入守卫（offscreen）。

覆盖三处入口：
1. 设置窗口「密码」页的开关与设备标识（含保存往返）；
2. 右键紧凑窗口的「云端取密码」按钮（手动查询 → 直接把密码交给解压线程）；
3. 密码问询里必须交代云端结论，用户才知道下一步该找谁。

注意：`_query_cloud` 会构造 `CloudQueryJob`，这里一律用假的替换掉，
测试绝不发真请求。
"""
from __future__ import annotations

import os
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


# ---------- 设置窗口 ----------

def test_settings_password_page_exposes_cloud_switch(app, tmp_path):
    from core.appconfig import AppConfig
    from ui.settings_window import SettingsWindow

    cfg = AppConfig(cloud_unlock=True, cloud_unlock_udid="a" * 32)
    win = SettingsWindow(cfg, tmp_path / "cfg.json", tmp_path)
    try:
        assert win.cloud_check.isChecked() is True
        assert win.cloud_udid_edit.text() == "a" * 32
        assert "同时开两条线" in win.cloud_hint.text()
    finally:
        win.close()
        app.processEvents()


def test_cloud_switch_off_changes_the_explanation_and_saves(app, tmp_path):
    """关掉之后说明要变成"不发任何云端请求"，保存后偏好也要真的落下去。"""
    from core.appconfig import AppConfig
    from ui.settings_window import SettingsWindow

    win = SettingsWindow(AppConfig(cloud_unlock=True), tmp_path / "cfg.json", tmp_path)
    try:
        win.cloud_check.setChecked(False)
        assert "不会发出任何云端请求" in win.cloud_hint.text()

        win.cloud_udid_edit.setText("  " + "b" * 32 + "  ")
        collected = win._collect()
        assert collected.cloud_unlock is False
        assert collected.cloud_unlock_udid == "b" * 32, "设备标识没去掉首尾空白"

        win._on_save()
        saved = AppConfig.load(tmp_path / "cfg.json")
        assert saved.cloud_unlock is False
        assert saved.cloud_unlock_udid == "b" * 32
    finally:
        win.close()
        app.processEvents()


def test_cloud_defaults_to_on_and_reaches_runtime_config(app, tmp_path):
    """偏好默认开；as_overrides 必须把它带进 Config，否则设置项在说谎。"""
    from core.appconfig import AppConfig
    from core.config import Config, detect_sevenzip

    try:
        detect_sevenzip()
    except FileNotFoundError:
        pytest.skip("未找到 7z.exe")
    overrides = AppConfig().as_overrides(tmp_path)
    assert overrides["cloud_unlock"] is True
    cfg = Config.create(sevenzip=str(detect_sevenzip()), **overrides)
    assert cfg.cloud_unlock is True


# ---------- 右键紧凑窗口 ----------

def _quick_window(paths: list[str]):
    from ui.quick_extract_window import QuickExtractWindow

    return QuickExtractWindow(paths, autostart=False)


class _FakeExtractWorker:
    def __init__(self):
        self.answers: list[str | None] = []

    def submit_password(self, value):
        self.answers.append(value)


def test_quick_window_shows_the_password_it_used(app, tmp_path, monkeypatch):
    """解完之后把真正生效的密码摆出来 —— 用户最想知道的就是"这个包用的什么密码"。

    密码来自 pipeline 的 status 事件（云端命中/本地命中都会带上），不是窗口自己猜的。
    """
    from core.appconfig import OPEN_NONE, AppConfig
    import ui.quick_extract_window as quick

    AppConfig(open_after=OPEN_NONE).save(tmp_path / "config.json")
    monkeypatch.setattr(quick, "CONFIG_PATH", tmp_path / "config.json")

    class _Report:
        failed = 0
        delivery_failed = 0
        needs_password = 0
        skipped = 0
        warnings: list[str] = []
        output_dirs = [str(tmp_path)]

    win = _quick_window(["C:/Downloads/pack.zip"])
    try:
        win._on_event({"kind": "status", "status": "done", "password_used": "cloudpw7788"})
        win._on_finished(_Report())

        assert "cloudpw7788" in win.status_label.text()
    finally:
        win.close()
        app.processEvents()


def test_quick_window_has_cloud_button(app):
    win = _quick_window(["C:/Downloads/pack.zip"])
    try:
        assert win.cloud_button.text() == "云端取密码"
        assert win.cloud_button.isEnabled() is True
        assert "MD5" in win.cloud_button.toolTip()
    finally:
        win.close()
        app.processEvents()


def test_quick_window_cloud_hit_is_handed_to_the_worker(app):
    """云端命中：密码直接交给解压线程，而不是再弹一个输入框让人手抄。"""
    win = _quick_window(["C:/Downloads/pack.zip"])
    worker = _FakeExtractWorker()
    win._worker = worker
    try:
        win._apply_cloud_result({"status": "ok", "password": "cloudpw7788", "message": "命中"})

        assert worker.answers == ["cloudpw7788"]
        assert "cloudpw7788" in win.password_hint.text()
        assert win.cloud_button.isEnabled() is True, "查询结束后按钮必须恢复可点"
    finally:
        win._worker = None
        win.close()
        app.processEvents()


def test_quick_window_cloud_miss_shows_the_reason(app):
    win = _quick_window(["C:/Downloads/pack.zip"])
    try:
        win._apply_cloud_result({"status": "not_found",
                            "password": None,
                            "message": "云端密码库未收录该文件（失败！）"})

        assert "未收录" in win.password_hint.text()
        assert "未返回密码" in win.status_label.text()
        assert win.cloud_button.isEnabled() is True
    finally:
        win.close()
        app.processEvents()


def test_quick_window_cloud_result_is_ignored_after_extract_finished(app):
    """解压线程已收尾时拿到云端密码：只显示，不许去戳已失效的 worker。"""
    win = _quick_window(["C:/Downloads/pack.zip"])
    try:
        win._worker = None
        win._apply_cloud_result({"status": "ok", "password": "late", "message": "命中"})

        assert "late" in win.password_hint.text()
    finally:
        win.close()
        app.processEvents()


def test_quick_window_need_password_shows_cloud_verdict(app):
    win = _quick_window(["C:/Downloads/pack.zip"])
    try:
        win._on_need_password({
            "path": "C:/Downloads/pack.zip",
            "cloud": "云端密码库未收录该文件（失败！）",
        })

        hint = win.password_hint.text()
        assert "未收录" in hint and "自动密码未匹配" in hint
        # 第二轮仍要能看出云端结论，否则用户会以为程序没查过
        win._on_need_password({
            "path": "C:/Downloads/pack.zip",
            "cloud": "云端密码库未收录该文件（失败！）",
        })
        assert "密码不正确" in win.password_hint.text()
    finally:
        win.close()
        app.processEvents()


class _FakeCloudJob:
    """假的云端任务：start() 之后第一次 poll() 就给结果。"""

    instances: list["_FakeCloudJob"] = []

    def __init__(self, archive, udid="", timeout=10.0, unlocker=None):
        self.archive = str(archive)
        self.started = False
        self._result = {"status": "ok", "password": "btnpw7788", "message": "命中"}
        _FakeCloudJob.instances.append(self)

    def start(self) -> bool:
        self.started = True
        return True

    def busy(self) -> bool:
        return self.started and self._result is not None

    def poll(self):
        result, self._result = self._result, None
        return result


def test_quick_window_cloud_button_starts_a_job_and_polls(app, tmp_path, monkeypatch):
    """点按钮 → 起后台任务 → 轮询拿到结果 → 密码交给解压线程。

    刻意**不走 Qt 信号**：后台线程只写结果，主线程用定时器来读（见
    ui/worker.py::CloudQueryJob 里的原因 —— 从普通 Python 线程 emit Qt 信号会让
    PySide6 随机 abort 整个进程）。
    """
    from core.appconfig import AppConfig
    import ui.quick_extract_window as quick

    AppConfig().save(tmp_path / "config.json")
    monkeypatch.setattr(quick, "CONFIG_PATH", tmp_path / "config.json")
    _FakeCloudJob.instances.clear()
    monkeypatch.setattr(quick, "CloudQueryJob", _FakeCloudJob)

    win = _quick_window([str(tmp_path / "pack.zip")])
    worker = _FakeExtractWorker()
    win._worker = worker
    try:
        win.cloud_button.click()

        job = _FakeCloudJob.instances[-1]
        assert job.archive == str(tmp_path / "pack.zip")
        assert job.started is True
        assert win.cloud_button.isEnabled() is False, "查询中按钮必须禁掉，防重复点击"
        assert win._cloud_timer.isActive(), "没有开轮询，结果永远上不了界面"

        win._poll_cloud()          # 主线程取结果

        assert worker.answers == ["btnpw7788"], "点按钮查到的密码没送到解压线程"
        assert win.cloud_button.text() == "云端取密码"
        assert win.cloud_button.isEnabled() is True
        assert not win._cloud_timer.isActive(), "拿到结果后定时器要停"
    finally:
        win._worker = None
        win.close()
        app.processEvents()


def test_cloud_poll_waits_while_the_job_is_running(app, tmp_path, monkeypatch):
    """线程还没出结果：轮询什么都不做，界面保持"查询中"。"""
    from core.appconfig import AppConfig
    import ui.quick_extract_window as quick

    AppConfig().save(tmp_path / "config.json")
    monkeypatch.setattr(quick, "CONFIG_PATH", tmp_path / "config.json")

    class SlowJob(_FakeCloudJob):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self._result = None

        def busy(self):
            return True

    monkeypatch.setattr(quick, "CloudQueryJob", SlowJob)

    win = _quick_window([str(tmp_path / "pack.zip")])
    try:
        win.cloud_button.click()
        win._poll_cloud()

        assert win.cloud_button.text() == "查询中…"
        assert win._cloud_timer.isActive()
    finally:
        win.close()
        app.processEvents()
