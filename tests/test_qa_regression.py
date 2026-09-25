"""QA 回归测试 — 针对本轮独立验证发现的两个源码缺陷。

⚠️ 这两个测试在缺陷修复前会 **FAIL**（这是预期：它们是"失败的证据"）。
工程师修复后应转绿，之后可并入 test_vault_admin.py / test_ui_smoke.py 或保留于此。

缺陷 1（core）：query() 的 LIKE 通配符未转义
    文件: core/vault.py:159-163
    现象: keyword 含 '%' 或 '_' 时被当作 SQL LIKE 通配符，搜 '%' 命中全部。
    期望: keyword 一律按字面包含匹配（转义 % 和 _，或用 ESCAPE 子句）。

缺陷 2（ui）：MainWindow 单例窗口关闭后复用已失效的 DB 连接
    文件: ui/main_window.py:695-708（_open_vault_window）与
          ui/vault_window.py:470-475（closeEvent 里 self._vault.close()）
    现象: 用户关掉密码库窗口再点「打开密码库」，复用同一对象但其
          PasswordVault 连接已 close → 读静默失败(显示陈旧数据)、
          写抛 sqlite3.ProgrammingError: Cannot operate on a closed database。
    期望: 关闭后引用置 None（或窗口重开时重建连接），复用时功能正常。
"""
from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.vault import PasswordVault


# ---------- 缺陷 1：LIKE 通配符转义 ----------

def test_query_percent_is_literal_not_wildcard(tmp_path):
    """搜 '%' 应只命中密码/来源中**字面**含 % 的记录，而非全部。

    复现: 库内 3 条均不含 '%'，query(keyword='%') 期望 []，实际返回 3 条。
    """
    v = PasswordVault(tmp_path / "v.db")
    v.add_manual("abc", "a.com")
    v.add_manual("def", "b.com")
    v.add_manual("ghi", "c.com")

    assert v.query(keyword="%") == [], (
        "keyword='%' 被当成 LIKE 通配符匹配了全部记录；应转义为字面匹配")
    v.close()


def test_query_underscore_is_literal_not_wildcard(tmp_path):
    """搜 '_' 应只命中字面含下划线的记录，而非任意单字符。"""
    v = PasswordVault(tmp_path / "v.db")
    v.add_manual("abc", "a.com")     # 不含 _
    v.add_manual("a_b", "b.com")     # 含 _

    got = {e.password for e in v.query(keyword="_")}
    assert got == {"a_b"}, (
        f"keyword='_' 被当成 LIKE 通配符；期望 {{'a_b'}}，实际 {got}")
    v.close()


def test_query_percent_only_matches_literal_percent(tmp_path):
    """正向：库里真有含 '%' 的记录时，搜 '%' 应精确命中它。"""
    v = PasswordVault(tmp_path / "v.db")
    v.add_manual("100%off", "s.com")
    v.add_manual("plain", "s.com")

    got = {e.password for e in v.query(keyword="%")}
    assert got == {"100%off"}, f"期望 {{'100%off'}}，实际 {got}"
    v.close()


# ---------- 缺陷 2：单例窗口关闭后复用 ----------

@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    from ui import theme

    inst = QApplication.instance() or QApplication([])
    inst.setStyleSheet(theme.build_stylesheet())
    return inst


def test_reopen_vault_window_after_close_works(app, tmp_path, monkeypatch):
    """关掉密码库窗口后重新打开，读写仍应正常（连接不能是死的）。"""
    from core.vault import PasswordVault as PV
    import ui.main_window as mw
    from ui.main_window import MainWindow

    db = tmp_path / "v.db"
    PV(db).add_manual("seed", "a.com")  # 建库 + 一条数据

    monkeypatch.setattr(mw, "DEFAULT_DB", db)

    win = MainWindow()
    try:
        win._open_vault_window()
        win._vault_window.close()          # 模拟用户点窗口的 X
        app.processEvents()
        app.sendPostedEvents(None, 0)
        app.processEvents()

        win._open_vault_window()           # 再点「打开密码库」
        reopened = win._vault_window
        assert reopened is not None
        # 关键：复用窗口的连接必须是活的
        reopened.refresh()
        assert reopened.table.rowCount() == 1, "重开后应能看到原有数据"
        # 写操作不能抛 ProgrammingError
        reopened._vault.add_manual("after-reopen", "b.com")
        assert reopened._vault.count() == 2
    finally:
        win.close()
    app.processEvents()
