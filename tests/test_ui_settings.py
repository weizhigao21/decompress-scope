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
        # 幂等跳过默认开；单包超时默认 3600s
        assert win.skip_done_check.isChecked() is True
        assert win.timeout_spin.value() == 3600
        # 删原件默认不开
        assert win.delete_orig_check.isChecked() is False
        # 密码来源默认空（= 每个包从文件名识别）；主界面搬来的两项在这里
        assert win.source_edit.text() == ""
        assert win.attempts_spin.value() == 20
        # 工作目录残留入口也在设置里
        assert win.residue_label.text() in {"无残留", "0 项残留"}
        assert win.residue_open_btn.property("ghost") == "true"
        assert not win.residue_open_btn.property("primary"), "不许有第二个 primary"
        assert win.context_menu_check.text().startswith("在资源管理器右键菜单")
        # 右键解压打开结果目录后自动关窗：默认开，且默认可点（默认的 open_after 是开的）
        assert win.close_after_open_check.isChecked() is True
        assert win.close_after_open_check.isEnabled() is True
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
        win.skip_done_check.setChecked(False)
        win.timeout_spin.setValue(7200)
        win.source_edit.setText("  example.com  ")
        win.close_after_open_check.setChecked(False)

        cfg = win._collect()
        assert cfg.output_mode == OUTPUT_WORKDIR
        assert cfg.autorun_mode == AUTORUN_CONFIRM
        assert cfg.max_depth == 8
        assert cfg.max_total_gb == 120
        assert cfg.max_ratio == 500
        assert cfg.sniff_archives is False
        assert cfg.delete_intermediate is False
        assert cfg.keep_original is False
        assert cfg.skip_done is False
        assert cfg.extract_timeout == 7200
        assert cfg.password_source == "example.com", "来源两端空白应被规范化掉"
        # 右键窗口的退场开关也要真的收进配置，而不只是停在界面上
        assert cfg.close_quick_after_open is False, \
            "「结果目录打开后关闭右键解压窗口」没进配置，界面就是个装饰"
    finally:
        win.close()
    app.processEvents()


def test_context_menu_checkbox_applies_immediately(app, tmp_path, monkeypatch):
    """右键菜单是系统操作：勾选/取消立刻写注册表，不必另点保存。"""
    from ui import settings_window
    from ui.settings_window import SettingsWindow

    calls = []
    monkeypatch.setattr(settings_window, "is_context_menu_installed", lambda: False)
    monkeypatch.setattr(settings_window, "quick_command", lambda root: '"app" quick "%1"')
    monkeypatch.setattr(settings_window, "menu_icon", lambda root: '"app.exe",0')
    monkeypatch.setattr(settings_window, "install_context_menu", lambda *args: calls.append(args))
    monkeypatch.setattr(settings_window, "uninstall_context_menu", lambda: calls.append("remove"))
    win = SettingsWindow(_cfg(), tmp_path / "cfg.json", tmp_path)
    try:
        assert win.context_menu_check.isChecked() is False
        win.context_menu_check.setChecked(True)
        win.context_menu_check.setChecked(False)
        assert calls == [('"app" quick "%1"', '"app.exe",0'), "remove"]
    finally:
        win.close()
    app.processEvents()


def test_settings_collect_normalizes_over_range(app, tmp_path):
    """越界输入在收集时被规范化，而不是拒绝保存整份配置。"""
    from ui.settings_window import SettingsWindow

    win = SettingsWindow(_cfg(), tmp_path / "cfg.json", tmp_path)
    try:
        win.depth_spin.setValue(10)          # 控件上限
        win.subdir_edit.setText("  ")        # 空白 → 不要容器层
        cfg = win._collect()
        assert cfg.max_depth == 10
        assert cfg.subdir_name == ""
    finally:
        win.close()
    app.processEvents()


def test_close_after_open_follows_auto_open_switch(app, tmp_path):
    """「关窗」依赖「自动打开」：后者关掉时前者必须就地禁用并说明原因。

    留着可勾选就是界面在说谎 —— 用户会以为开了就生效，实际一次都不会发生
    （右键窗口只在真的打开了目录之后才退场）。
    """
    from core.appconfig import AppConfig
    from ui.settings_window import SettingsWindow

    cfg_path = tmp_path / "cfg.json"
    AppConfig().save(cfg_path)
    win = SettingsWindow(AppConfig.ensure(cfg_path), cfg_path, tmp_path,
                         db_path=tmp_path / "t.db")
    try:
        assert win.open_after_check.isChecked() is True
        assert win.close_after_open_check.isEnabled() is True

        win.open_after_check.setChecked(False)
        app.processEvents()
        assert win.close_after_open_check.isEnabled() is False, \
            "不自动打开目录时，「关窗」勾了也不会触发，不该还让人勾"
        assert "自动打开" in win.close_after_open_check.toolTip(), \
            "禁用却不说原因，用户只会以为这个选项坏了"

        win.open_after_check.setChecked(True)
        app.processEvents()
        assert win.close_after_open_check.isEnabled() is True
    finally:
        win.close()
    app.processEvents()


def test_close_after_open_survives_save_and_reload(app, tmp_path):
    """关掉之后必须真的存住：右键窗口每次都是重新读配置文件的。"""
    from core.appconfig import AppConfig
    from ui.settings_window import SettingsWindow

    cfg_path = tmp_path / "cfg.json"
    AppConfig().save(cfg_path)
    win = SettingsWindow(AppConfig.ensure(cfg_path), cfg_path, tmp_path,
                         db_path=tmp_path / "t.db")
    try:
        win.close_after_open_check.setChecked(False)
        win._on_save()
        app.processEvents()
    finally:
        win.close()
    app.processEvents()

    assert AppConfig.ensure(cfg_path).close_quick_after_open is False, \
        "存盘后被 normalize 打回了默认值"
    # 再打开设置窗口时勾选状态要跟着配置文件走
    win2 = SettingsWindow(AppConfig.ensure(cfg_path), cfg_path, tmp_path,
                          db_path=tmp_path / "t.db")
    try:
        assert win2.close_after_open_check.isChecked() is False
    finally:
        win2.close()
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


def test_numeric_control_ranges_come_from_core_bounds(app, tmp_path):
    """数字框的范围只能来自 core/appconfig 的边界，不许在 UI 里另写一份。

    症状级理由：范围被写死成两份时，其中一份迟早改不到。用户会遇到"控件明明
    能调上去、一保存却被夹回来"的鬼打墙——输入被静默吞掉，而且从界面上完全
    看不出是谁夹的（`AppConfig.normalize()` 在加载和保存前都会跑一次，
    连手改 config.json 都会被打回）。
    """
    from core.appconfig import clamp_bounds
    from ui.settings_window import SettingsWindow

    win = SettingsWindow(_cfg(), tmp_path / "cfg.json", tmp_path)
    try:
        pairs = (
            ("max_depth", win.depth_spin),
            ("max_total_gb", win.total_spin),
            ("max_ratio", win.ratio_spin),
            ("max_password_attempts", win.attempts_spin),
            ("recent_inputs_limit", win.recent_spin),
            ("session_days", win.keep_days_spin),
            ("extract_timeout", win.timeout_spin),
        )
        for name, widget in pairs:
            lo, hi = clamp_bounds(name)
            assert (widget.minimum(), widget.maximum()) == (lo, hi), (
                f"{name} 的控件范围 {widget.minimum()}–{widget.maximum()} 与 core 的 "
                f"{lo}–{hi} 不一致。范围只能有一份定义，否则用户会被夹在中间")
    finally:
        win.close()
    app.processEvents()


def test_attempts_can_go_past_200_and_survives_save(app, tmp_path):
    """症状级回归：「密码尝试」调到 200 以上必须真的存得住。

    旧实现把上限写死成 200（UI 的 setRange 与 core 的 _CLAMP 各一份），用户想把
    整个密码库都试一遍时，输入会被无声夹回 200 —— 表现就是"这个数字改不动"。
    这里钉的是用户真正在做的动作：调大、保存、再读回来。
    """
    from core.appconfig import AppConfig
    from ui.settings_window import SettingsWindow

    cfg_path = tmp_path / "cfg.json"
    AppConfig().save(cfg_path)
    win = SettingsWindow(AppConfig.ensure(cfg_path), cfg_path, tmp_path,
                         db_path=tmp_path / "t.db")
    try:
        win.attempts_spin.setValue(500)
        assert win.attempts_spin.value() == 500, "控件把大于 200 的输入夹回去了"
        # 必须能穿到 pipeline 真正消费的那份 Config，而不只是停在界面上
        assert win._collect().as_overrides(tmp_path)["max_password_attempts"] == 500
        win._on_save()
    finally:
        win.close()
    app.processEvents()

    assert AppConfig.ensure(cfg_path).max_password_attempts == 500, \
        "存盘时被 normalize() 夹回去了"


def test_attempts_hint_shows_worst_case_cost(app, tmp_path):
    """控件下方必须实时显示"这个数字意味着等多久"。

    上限从 200 放开到 2000 之后，没有这行提示，用户会毫无察觉地把单个包的等待
    时间设成几十秒，然后以为程序卡死了。断言跟着 `_MS_PER_CANDIDATE` 算，
    不写死秒数——否则这条测试会随实测值一起过期。
    """
    from ui.settings_window import _MS_PER_CANDIDATE, SettingsWindow

    win = SettingsWindow(_cfg(), tmp_path / "cfg.json", tmp_path)
    try:
        n = 1000
        win.attempts_spin.setValue(n)
        app.processEvents()
        expected = f"{n * _MS_PER_CANDIDATE / 1000:.1f} 秒"
        assert expected in win.attempts_hint.text(), (
            f"提示没跟上控件值：期望含 {expected!r}，实际 {win.attempts_hint.text()!r}")
        # 改回小值也要跟着变（不是一次性渲染）
        win.attempts_spin.setValue(20)
        app.processEvents()
        assert "0.4 秒" in win.attempts_hint.text()
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
    """_make_cfg 把偏好映射为本次运行参数（GB→字节、开关透传）。

    主界面**没有**任何"本次覆盖"了：想这一次跑得不一样就去设置里改。所以这里
    断言的是"偏好是什么，本次运行参数就是什么"——多加一条本地覆盖会让设置
    窗口的显示与实际生效值分家。
    """
    from core.appconfig import AUTORUN_OFF, OUTPUT_SAMEDIR, AppConfig
    from ui import main_window
    from ui.main_window import MainWindow

    AppConfig(autorun_mode=AUTORUN_OFF, output_mode=OUTPUT_SAMEDIR,
              max_total_gb=3, subdir_name="",
              extract_timeout=1800, skip_done=False).save(main_window.CONFIG_PATH)
    win = MainWindow()
    try:
        cfg = win._make_cfg()
        assert cfg.max_total_uncompressed == 3 * (1024 ** 3)
        # 这几项也必须真的流进本次运行参数，否则设置窗口就是个装饰
        assert cfg.extract_timeout == 1800
        assert cfg.skip_done is False
        # 主界面不再持有可覆盖偏好的控件
        assert not hasattr(win, "total_spin"), "主界面不该再有大小上限的本地覆盖"
    finally:
        win.close()
    app.processEvents()


def test_start_passes_password_source_to_worker(app, tmp_path, monkeypatch):
    """密码来源必须真的流到工作线程。

    只断言"设置窗口收集到了这个字段"证明不了接线：字段可以存进配置、然后被
    启动路径忽略。用假线程 + 假 worker 拦下构造参数，断言真正传下去的值。
    """
    from PySide6.QtCore import QObject, QThread
    from PySide6.QtCore import Signal as QSignal

    from core.appconfig import AUTORUN_OFF, AppConfig
    from ui import main_window as mw

    AppConfig(autorun_mode=AUTORUN_OFF, password_source="mydomain.com").save(mw.CONFIG_PATH)
    seen: dict = {}

    class FakeWorker(QObject):
        task_event = QSignal(dict)
        need_password = QSignal(dict)
        finished_ok = QSignal(object)
        failed = QSignal(str)

        def __init__(self, cfg, db, inputs, **kw):
            super().__init__()
            seen.update(kw)
            seen["inputs"] = list(inputs)

        def run(self) -> None:      # 不真跑解压
            pass

    class FakeThread(QThread):
        """真 QThread 的子类（moveToThread 会做类型检查），只是不起线程。"""

        def start(self) -> None:
            pass

    monkeypatch.setattr(mw, "ExtractWorker", FakeWorker)
    monkeypatch.setattr(mw, "QThread", FakeThread)

    win = mw.MainWindow()
    try:
        f = tmp_path / "a.zip"
        f.write_bytes(b"PK\x03\x04" + b"\x00" * 100)
        win.input_list.add_paths([str(f)])
        win._start()
        assert seen.get("source") == "mydomain.com", \
            f"密码来源没传到工作线程：{seen.get('source')!r}"
    finally:
        win.close()
    app.processEvents()
