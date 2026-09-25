"""设置窗口 + 主窗口偏好联动的 GUI 测试（offscreen）。

注意：conftest 已把 main_window.CONFIG_PATH / PROJECT_ROOT / DEFAULT_DB
重定向到 tmp_path，故这些测试不会碰真实配置与密码库。
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


def _cfg():
    from core.appconfig import AppConfig

    return AppConfig()


# ---------- 设置窗口 ----------


def test_settings_window_smoke(app, tmp_path):
    """设置窗口能构造、控件齐备、默认值来自传入的配置。"""
    from ui.settings_window import SettingsWindow

    win = SettingsWindow(_cfg(), tmp_path / "cfg.json", tmp_path)
    try:
        assert win.windowTitle() == "设置"
        assert win.save_btn.property("primary") == "true"
        assert win.reset_btn.property("ghost") == "true"
        # 恢复默认是危险动作，须带 danger 标
        assert win.reset_btn.property("danger") == "true"
        assert win.depth_spin.value() == 5
        assert win.total_spin.value() == 50
        assert win.sniff_check.isChecked() is True
        # 删原件默认不开
        assert win.delete_orig_check.isChecked() is False
    finally:
        win.close()
    app.processEvents()


def test_settings_collect_roundtrip(app, tmp_path):
    """控件 → 配置 的收集应完整映射所有暴露的字段。"""
    from core.appconfig import AUTORUN_CONFIRM, OUTPUT_WORKDIR
    from ui.settings_window import SettingsWindow

    win = SettingsWindow(_cfg(), tmp_path / "cfg.json", tmp_path)
    try:
        win.output_combo.setCurrentIndex(win.output_combo.findData(OUTPUT_WORKDIR))
        win.autorun_combo.setCurrentIndex(win.autorun_combo.findData(AUTORUN_CONFIRM))
        win.depth_spin.setValue(8)
        win.total_spin.setValue(120)
        win.ratio_spin.setValue(500)
        win.sniff_check.setChecked(False)
        win.keep_mid_check.setChecked(True)
        win.delete_orig_check.setChecked(True)

        cfg = win._collect()
        assert cfg.output_mode == OUTPUT_WORKDIR
        assert cfg.autorun_mode == AUTORUN_CONFIRM
        assert cfg.max_depth == 8
        assert cfg.max_total_gb == 120
        assert cfg.max_ratio == 500
        assert cfg.sniff_archives is False
        assert cfg.delete_intermediate is False
        assert cfg.keep_original is False
    finally:
        win.close()
    app.processEvents()


def test_settings_collect_normalizes_over_range(app, tmp_path):
    """越界输入在收集时被规范化，而不是拒绝保存整份配置。"""
    from ui.settings_window import SettingsWindow

    win = SettingsWindow(_cfg(), tmp_path / "cfg.json", tmp_path)
    try:
        win.depth_spin.setValue(10)          # 控件上限
        win.subdir_edit.setText("  ")        # 空白 → 回落默认
        cfg = win._collect()
        assert cfg.max_depth == 10
        assert cfg.subdir_name == "_解压开镜"
    finally:
        win.close()
    app.processEvents()


def test_settings_save_writes_file_and_emits(app, tmp_path):
    """保存：落盘 + emit saved + 自动关闭。"""
    from core.appconfig import OUTPUT_WORKDIR, AppConfig
    from ui.settings_window import SettingsWindow

    path = tmp_path / "cfg.json"
    win = SettingsWindow(_cfg(), path, tmp_path)
    got: list = []
    win.saved.connect(got.append)
    try:
        win.output_combo.setCurrentIndex(win.output_combo.findData(OUTPUT_WORKDIR))
        win.depth_spin.setValue(7)
        win._on_save()
        app.processEvents()

        assert got, "保存后未发出 saved 信号"
        assert got[0].output_mode == OUTPUT_WORKDIR
        assert path.is_file()
        assert AppConfig.load(path).max_depth == 7
    finally:
        win.close()
    app.processEvents()


def test_settings_reset_restores_defaults_in_ui(app, tmp_path, monkeypatch):
    """恢复默认：不弹模态框（打桩 Yes）后，控件回到出厂值。"""
    from PySide6.QtWidgets import QMessageBox

    from core.appconfig import AppConfig
    from ui.settings_window import SettingsWindow

    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    win = SettingsWindow(AppConfig(max_depth=9, sniff_archives=False),
                         tmp_path / "cfg.json", tmp_path)
    try:
        assert win.depth_spin.value() == 9
        win._on_reset()
        app.processEvents()
        assert win.depth_spin.value() == AppConfig().max_depth
        assert win.sniff_check.isChecked() is True
    finally:
        win.close()
    app.processEvents()


def test_settings_clear_history(app, tmp_path, monkeypatch):
    """清空历史：确认后清空并刷新提示文案。"""
    from PySide6.QtWidgets import QMessageBox

    from core.appconfig import AppConfig
    from ui.settings_window import SettingsWindow

    monkeypatch.setattr(QMessageBox, "question",
                        staticmethod(lambda *a, **k: QMessageBox.Yes))
    cfg = AppConfig(recent_inputs=["D:/a.zip", "D:/b.zip"])
    win = SettingsWindow(cfg, tmp_path / "cfg.json", tmp_path)
    try:
        assert "已记录 2 条" in win.history_label.text()
        win._clear_history()
        app.processEvents()
        assert cfg.recent_inputs == []
        assert "暂无记录" in win.history_label.text()
    finally:
        win.close()
    app.processEvents()


def test_settings_hint_switches_with_mode(app, tmp_path):
    """切换下拉时提示文案同步变化（否则用户看不懂选项含义）。"""
    from core.appconfig import OUTPUT_SAMEDIR, OUTPUT_WORKDIR
    from ui.settings_window import SettingsWindow

    win = SettingsWindow(_cfg(), tmp_path / "cfg.json", tmp_path)
    try:
        win.output_combo.setCurrentIndex(win.output_combo.findData(OUTPUT_WORKDIR))
        app.processEvents()
        assert "隔离工作目录" in win.output_hint.text()
        win.output_combo.setCurrentIndex(win.output_combo.findData(OUTPUT_SAMEDIR))
        app.processEvents()
        assert "所在目录" in win.output_hint.text()
    finally:
        win.close()
    app.processEvents()


# ---------- 主窗口偏好联动 ----------


def test_main_window_reads_config(app, tmp_path, monkeypatch):
    """主窗口启动时读配置：初始化开关状态与偏好一致。"""
    from core.appconfig import AUTORUN_OFF, OPEN_NONE, AppConfig
    from ui import main_window
    from ui.main_window import MainWindow

    # conftest 已重定向 CONFIG_PATH；这里预写一份"全关"配置
    AppConfig(autorun_mode=AUTORUN_OFF, open_after=OPEN_NONE).save(main_window.CONFIG_PATH)

    win = MainWindow()
    try:
        assert win.auto_run.isChecked() is False
        assert win.auto_open.isChecked() is False
        assert win._cfg.autorun_mode == AUTORUN_OFF
    finally:
        win.close()
    app.processEvents()


def test_toolbar_toggle_writes_config(app, tmp_path):
    """工具条开关 = 快捷档位：勾/取消会即时写回配置文件。"""
    from core.appconfig import AUTORUN_DIRECT, AUTORUN_OFF, OPEN_NONE, OPEN_PATHS, AppConfig
    from ui import main_window
    from ui.main_window import MainWindow

    AppConfig(autorun_mode=AUTORUN_OFF, open_after=OPEN_NONE).save(main_window.CONFIG_PATH)
    win = MainWindow()
    try:
        win.auto_run.setChecked(True)
        app.processEvents()
        assert win._cfg.autorun_mode == AUTORUN_DIRECT
        assert AppConfig.load(main_window.CONFIG_PATH).autorun_mode == AUTORUN_DIRECT

        win.auto_open.setChecked(True)
        app.processEvents()
        assert win._cfg.open_after == OPEN_PATHS
        assert AppConfig.load(main_window.CONFIG_PATH).open_after == OPEN_PATHS

        win.auto_run.setChecked(False)
        app.processEvents()
        assert AppConfig.load(main_window.CONFIG_PATH).autorun_mode == AUTORUN_OFF
    finally:
        win.close()
    app.processEvents()


def test_autorun_timer_deferred_not_immediate(app, tmp_path):
    """拖入即开始必须是「延迟合并」而不是立即启动：计时器处于活动态，线程未起。"""
    from core.appconfig import AUTORUN_DIRECT, AppConfig
    from ui import main_window
    from ui.main_window import MainWindow

    AppConfig(autorun_mode=AUTORUN_DIRECT, autorun_delay_ms=5000).save(main_window.CONFIG_PATH)
    win = MainWindow()
    try:
        real = tmp_path / "a.zip"
        real.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
        win.input_list.add_paths([str(real)])
        win._on_dropped([str(real)])

        assert win._autorun_timer.isActive(), "拖入后未启动合并计时器"
        assert win._thread is None, "不应在拖入瞬间就启动解压"
    finally:
        win.close()
    app.processEvents()


def test_autorun_off_does_not_arm_timer(app, tmp_path):
    """关闭自动时拖入不应启动任何计时器。"""
    from core.appconfig import AUTORUN_OFF, AppConfig
    from ui import main_window
    from ui.main_window import MainWindow

    AppConfig(autorun_mode=AUTORUN_OFF).save(main_window.CONFIG_PATH)
    win = MainWindow()
    try:
        real = tmp_path / "a.zip"
        real.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
        win.input_list.add_paths([str(real)])
        win._on_dropped([str(real)])
        assert win._autorun_timer.isActive() is False
    finally:
        win.close()
    app.processEvents()


def test_dropped_paths_are_remembered(app, tmp_path):
    """拖入的路径进入历史并落盘（供「最近输入」）。"""
    from core.appconfig import AUTORUN_OFF, AppConfig
    from ui import main_window
    from ui.main_window import MainWindow

    AppConfig(autorun_mode=AUTORUN_OFF).save(main_window.CONFIG_PATH)
    win = MainWindow()
    try:
        real = tmp_path / "comic.zip"
        real.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
        win.input_list.add_paths([str(real)])
        win._on_dropped([str(real)])
        app.processEvents()

        saved = AppConfig.load(main_window.CONFIG_PATH)
        assert str(real) in saved.recent_inputs
    finally:
        win.close()
    app.processEvents()


def test_restore_last_inputs_respects_switch(app, tmp_path, monkeypatch):
    """启动恢复输入：默认关（不复原）；显式开启后存在路径才会回到输入区。"""
    from core.appconfig import AUTORUN_OFF, AppConfig
    from ui import main_window
    from ui.main_window import MainWindow

    real = tmp_path / "old.zip"
    real.write_bytes(b"PK\x03\x04" + b"\x00" * 100)

    AppConfig(autorun_mode=AUTORUN_OFF, restore_last_inputs=False,
              recent_inputs=[str(real)]).save(main_window.CONFIG_PATH)
    win = MainWindow()
    try:
        assert win.input_list.real_paths() == [], "默认不应恢复上次输入"
    finally:
        win.close()
    app.processEvents()

    AppConfig(autorun_mode=AUTORUN_OFF, restore_last_inputs=True,
              recent_inputs=[str(real)]).save(main_window.CONFIG_PATH)
    win2 = MainWindow()
    try:
        assert str(real) in win2.input_list.real_paths()
    finally:
        win2.close()
    app.processEvents()


def test_restore_skips_deleted_paths(app, tmp_path):
    """历史里的路径已被删除时不得塞进输入区（否则解压会报"不存在"）。"""
    from core.appconfig import AUTORUN_OFF, AppConfig
    from ui import main_window
    from ui.main_window import MainWindow

    AppConfig(autorun_mode=AUTORUN_OFF, restore_last_inputs=True,
              recent_inputs=[str(tmp_path / "gone.zip")]).save(main_window.CONFIG_PATH)
    win = MainWindow()
    try:
        assert win.input_list.real_paths() == []
    finally:
        win.close()
    app.processEvents()


def test_open_results_caps_window_count(app, tmp_path, monkeypatch):
    """结果目录超过 5 个时只开前 5 个，避免刷屏。"""
    from core.appconfig import AUTORUN_OFF, AppConfig
    from ui import main_window
    from ui.main_window import MainWindow

    AppConfig(autorun_mode=AUTORUN_OFF).save(main_window.CONFIG_PATH)
    opened: list[str] = []
    monkeypatch.setattr(MainWindow, "_open_path",
                        lambda self, p, t="无法打开": opened.append(str(p)))

    win = MainWindow()
    try:
        dirs = []
        for i in range(8):
            d = tmp_path / f"out{i}"
            d.mkdir()
            dirs.append(str(d))
        win._open_results(dirs)
        assert len(opened) == 5
    finally:
        win.close()
    app.processEvents()


def test_open_results_dedupes_and_skips_missing(app, tmp_path, monkeypatch):
    """重复目录只开一次；不存在的目录被跳过。"""
    from core.appconfig import AUTORUN_OFF, AppConfig
    from ui import main_window
    from ui.main_window import MainWindow

    AppConfig(autorun_mode=AUTORUN_OFF).save(main_window.CONFIG_PATH)
    opened: list[str] = []
    monkeypatch.setattr(MainWindow, "_open_path",
                        lambda self, p, t="无法打开": opened.append(str(p)))

    win = MainWindow()
    try:
        d = tmp_path / "out"
        d.mkdir()
        win._open_results([str(d), str(d), str(tmp_path / "nope")])
        assert opened == [str(d)]
    finally:
        win.close()
    app.processEvents()


def test_settings_window_is_singleton_and_reopenable(app, tmp_path):
    """设置窗口单例复用；关闭后再打开必须重建（连接已失效）。"""
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        win._open_settings_window()
        first = win._settings_window
        assert first is not None
        win._open_settings_window()
        assert win._settings_window is first, "设置窗口应为单例"

        first.close()
        app.processEvents()
        assert win._settings_window is None, "关闭后引用应被清空"

        win._open_settings_window()
        assert win._settings_window is not None
        assert win._settings_window is not first, "应重建而非复用已关闭窗口"
    finally:
        win.close()
    app.processEvents()


def test_make_cfg_uses_preferences(app, tmp_path):
    """_make_cfg 把偏好映射为本次运行参数（GB→字节、开关透传）。"""
    from core.appconfig import AUTORUN_OFF, OUTPUT_SAMEDIR, AppConfig
    from ui import main_window
    from ui.main_window import MainWindow

    AppConfig(autorun_mode=AUTORUN_OFF, output_mode=OUTPUT_SAMEDIR,
              max_total_gb=3, subdir_name="_解压开镜").save(main_window.CONFIG_PATH)
    win = MainWindow()
    try:
        cfg = win._make_cfg()
        assert cfg.max_total_uncompressed == 3 * (1024 ** 3)
        # 折叠区控件覆盖偏好
        win.total_spin.setValue(9)
        assert win._make_cfg().max_total_uncompressed == 9 * (1024 ** 3)
    finally:
        win.close()
    app.processEvents()
