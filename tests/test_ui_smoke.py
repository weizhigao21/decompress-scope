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
        # 7 列精简为 5 列：ID/层 已并入信息列（设计决策）
        assert win.tree.columnCount() == 5
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


def test_fold_panels_collapsed_by_default(app):
    """极简取向：高级参数默认折叠；密码库已降级为「打开密码库」按钮。"""
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        # 窗口未 show 时 isVisible() 恒 False，须用 isVisibleTo / 初始 check 态判断
        assert win.params_body.isVisibleTo(win) is False
        assert win.params_toggle.isChecked() is False
        # 密码库折叠体已移除，改为独立窗口入口按钮（ghost）
        assert not hasattr(win, "vault_body")
        assert hasattr(win, "vault_open_btn")
        assert win.vault_open_btn.property("ghost") == "true"
        win.params_toggle.setChecked(True)
        app.processEvents()
        assert win.params_body.isVisibleTo(win) is True
        assert "高级参数" in win.params_toggle.text()
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
