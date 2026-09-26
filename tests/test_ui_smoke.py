"""GUI 冒烟测试：未安装 PySide6 时自动跳过；offscreen 模式无需显示环境。"""
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


def test_main_window_smoke(app):
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        assert "解压开镜" in win.windowTitle()
        # 7 列 = 压缩包/类型/大小/状态/密码/进度/信息
        # （早先把 7 列精简为 5 列：ID/层 并入信息列；后来又加回类型/大小两列，
        #   那是用户明确要看的包体信息，与当初被砍掉的 ID/层 不是一回事。
        #   列名与顺序的守卫见 tests/test_ui_info_columns.py）
        assert win.tree.columnCount() == 7
        assert win.input_list.real_paths() == []
        assert win.start_btn.isEnabled()
    finally:
        win.close()
    app.processEvents()


def test_empty_state_placeholder(app):
    """空态不是死区：列表里有引导文案，且不会被当作真实输入。"""
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        assert win.input_list.count() == 1, "空态应有一条引导项"
        assert win.input_list.real_paths() == [], "引导项不得计入真实输入"
        assert "拖入" in win.input_list.item(0).text()
    finally:
        win.close()
    app.processEvents()


def test_advanced_params_live_in_settings_not_main_window(app):
    """极简取向：高级参数与工作目录残留**整块**搬进了设置窗口。

    这两块原先在主界面上（一个默认折叠的「高级参数」面板 + 一行残留入口）。
    主界面只留「添加输入 → 开始解压」一条主线；深度/上限/密码来源/各类开关
    以及残留盘点入口都住在 ui.settings_window 里。

    属性名一并钉住：同名控件留在主窗口就等于"搬了但没搬干净"，而两处都能改
    的话迟早出现"显示 50、实际 3"。设置窗口侧的存在性由 test_ui_settings 守。
    """
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        for gone in ("params_body", "params_toggle", "source_edit", "depth_spin",
                     "total_spin", "sniff", "keep_intermediate", "delete_original",
                     "workdir_label", "workdir_open_btn"):
            assert not hasattr(win, gone), f"主界面仍残留 {gone}，应已移入设置窗口"
        # 密码库折叠体已移除，改为独立窗口入口按钮（ghost）
        assert not hasattr(win, "vault_body")
        assert hasattr(win, "vault_open_btn")
        assert win.vault_open_btn.property("ghost") == "true"
    finally:
        win.close()
    app.processEvents()


def test_single_primary_action(app):
    """主操作唯一：只有「开始解压」使用 primary 样式。"""
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        # QSS 属性经 Qt 动态属性返回字符串 "true"
        assert win.start_btn.property("primary") == "true"
        assert win.open_dir_btn.property("ghost") == "true"
        assert not win.open_dir_btn.property("primary")
        # 「打开密码库」是 ghost，不是 primary
        assert win.vault_open_btn.property("ghost") == "true"
        assert not win.vault_open_btn.property("primary")
    finally:
        win.close()
    app.processEvents()


def test_open_vault_window_is_singleton(app):
    """点击「打开密码库」单例复用：两次调用指向同一对象。"""
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        win._open_vault_window()
        first = win._vault_window
        assert first is not None
        win._open_vault_window()
        assert win._vault_window is first, "密码库窗口应为单例复用"
    finally:
        win.close()
    app.processEvents()


def test_vault_window_smoke(app, tmp_path):
    """VaultWindow 能独立构造/渲染/关闭，空库显示空态。"""
    from core.vault import PasswordVault
    from ui.vault_window import VaultWindow

    db = tmp_path / "v.db"
    v = PasswordVault(db)
    v.add_manual("alpha", "a.com")
    v.add_manual("beta", "")
    v.close()

    win = VaultWindow(db)
    try:
        win.refresh()
        assert win.table.rowCount() == 2
        assert win.table.columnCount() == 5
        # 密码默认打码，tooltip 恒为原文
        assert win.table.item(0, 0).text() != "alpha"
        assert win.table.item(0, 0).toolTip() in {"alpha", "beta"}
    finally:
        win.close()
    app.processEvents()


def test_vault_window_empty_state_switching(app, tmp_path):
    """空库→表格隐藏/空态可见；有数据→反转；筛选无结果→「无匹配」文案。"""
    from core.vault import PasswordVault
    from ui.vault_window import VaultWindow

    db = tmp_path / "empty.db"
    win = VaultWindow(db)
    try:
        win.show()
        app.processEvents()
        assert win.table.isVisible() is False
        assert win.empty_label.isVisible() is True

        v = PasswordVault(db)
        v.add_manual("x", "a.com")
        v.close()
        win.refresh()
        app.processEvents()
        assert win.table.isVisible() is True
        assert win.empty_label.isVisible() is False

        win.search_edit.setText("nope")
        win._on_search_debounced()
        app.processEvents()
        assert win.table.rowCount() == 0
        assert win.empty_label.isVisible() is True
        assert "nope" in win.empty_label.text()
    finally:
        win.close()
    app.processEvents()


def test_vault_mask():
    """打码规则：≤2 全星；更长保留首尾；星号数固定为 4（不泄露长度）。"""
    from ui.vault_window import _mask

    assert _mask("") == ""
    assert _mask("a") == "*"
    assert _mask("ab") == "**"
    assert _mask("abcd") == "a**d"
    # 长密码：固定 4 颗星，不随长度递增
    assert _mask("abcdefgh") == "ab****gh"
    assert _mask("a" * 50) == "aa****aa"
    # 星号数量恒定，不泄露长度
    assert _mask("abcdefgh").count("*") == 4
    assert _mask("a" * 50).count("*") == 4


def test_theme_tokens_defined():
    """设计令牌齐备，QSS 能正常生成且不含未替换占位。"""
    from ui import theme

    qss = theme.build_stylesheet()
    assert "{" in qss and "QPushButton" in qss
    assert "#3B9EFF" in qss  # 唯一强调色
    for name in ("CANVAS", "SURFACE", "RAISED", "LINE", "TEXT", "ACCENT"):
        assert getattr(theme, name)
    assert set(theme.STATUS_COLORS) == {
        "pending", "probing", "extracting", "needs_password", "done", "failed"}
