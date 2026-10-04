"""密码库管理窗口：独立非模态窗口，提供完整的增/删/改/查能力。

设计要点（见 docs/vault-window-design.md）：
- 三段式布局：顶部工具条 → 中部表格 → 页脚操作条。
- 长连接：窗口存活期间持有一个 PasswordVault，closeEvent 中 close()。
- 依赖方向单向 ui → core，本文件不写 SQL，只调 core 方法。
- 颜色只从 ui.theme 取，或走 QSS 属性（primary/ghost/panel/section），无字面色值。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.vault import PasswordVault, VaultEntry
from ui import theme
from ui.glass import GlassWindowMixin

# 表格列索引
_COL_PASSWORD = 0
_COL_SOURCE = 1
_COL_HITS = 2
_COL_LAST = 3
_COL_CREATED = 4

_EMPTY_SOURCE_LABEL = "无来源"
_EMPTY_HINT = "密码库还是空的 · 解压命中密码会自动入库，也可以点右上角「新增」手动添加"

_NO_SOURCE_KEY = "\x00__NO_SOURCE__\x00"  # 下拉里代表"仅无来源"的哨兵值


def _mask(pwd: str) -> str:
    """密码打码：长度 ≤ 2 全 `*`；≤ 4 保留首尾各 1 位；更长保留首尾各 2 位 + 固定 4 颗星。

    星号数量固定为 4（不随密码长度变化），避免通过星号个数泄露真实密码长度。
    纯展示逻辑，放在 UI 层（不进 core）。
    """
    n = len(pwd)
    if n <= 2:
        return "*" * n
    if n <= 4:
        return pwd[0] + "*" * (n - 2) + pwd[-1]
    return pwd[:2] + "****" + pwd[-2:]


def _short_ts(ts: str | None, *, empty: str = "从未") -> str:
    """ISO 8601 秒精度 → 截断到分钟展示；None 用占位文案。"""
    if not ts:
        return empty
    return ts[:16].replace("T", " ")


class VaultWindow(GlassWindowMixin, QMainWindow):
    """密码库管理窗口。任何写操作成功后 emit vault_changed。

    继承 GlassWindowMixin 拿到无边框 + 毛玻璃（与主窗口一致的外观）。
    """

    vault_changed = Signal()
    # close() 不会触发 QObject.destroyed（后者只在析构/deleteLater 时发出），
    # 故额外提供 closed 信号，便于 MainWindow 在用户点 X 关闭后正确清理引用。
    closed = Signal()

    COLUMNS = ("密码", "来源", "命中", "最近命中", "添加时间")

    def __init__(self, db_path: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("密码库")
        self.resize(880, 560)
        self._vault = PasswordVault(db_path)

        self._keyword = ""
        self._source_filter: str | None = None   # None=全部；""=仅无来源；"x"=指定来源

        # 搜索防抖定时器（单次触发）
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._on_search_debounced)

        self._rows: list[VaultEntry] = []

        self._build_ui()
        self.refresh()
        self.init_glass(self.centralWidget())

    # ---------- UI 构建 ----------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        # 标题栏（窗口名 + 关闭）：无边框窗口的唯一窗口控制入口
        root.addWidget(self.build_title_bar("密码库"))
        root.addWidget(self._build_toolbar())

        # 表格与空态共用一个容器，靠可见性切换
        self._table_wrap = QWidget()
        wrap_box = QVBoxLayout(self._table_wrap)
        wrap_box.setContentsMargins(0, 0, 0, 0)
        wrap_box.setSpacing(0)

        self.table = self._build_table()
        wrap_box.addWidget(self.table)

        self.empty_label = QLabel(_EMPTY_HINT)
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setWordWrap(True)
        self.empty_label.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; background: transparent; padding: 40px 20px;")
        wrap_box.addWidget(self.empty_label)

        root.addWidget(self._table_wrap, stretch=1)
        root.addWidget(self._build_footer())

        self.statusBar().showMessage("就绪")

    def _build_toolbar(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("搜索密码或来源")
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setMinimumWidth(200)
        self.search_edit.textChanged.connect(self._on_search_changed)
        row.addWidget(self.search_edit, stretch=1)

        self.source_combo = QComboBox()
        self.source_combo.setMinimumWidth(160)
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        row.addWidget(self.source_combo)

        self.show_plain = QCheckBox("显示明文")
        self.show_plain.toggled.connect(self._on_plain_toggled)
        row.addWidget(self.show_plain)

        row.addStretch(1)

        self.add_btn = QPushButton("新增")
        self.add_btn.setProperty("primary", "true")
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.clicked.connect(self._on_add)
        row.addWidget(self.add_btn)
        return bar

    def _build_table(self) -> QTableWidget:
        table = QTableWidget(0, len(self.COLUMNS))
        table.setHorizontalHeaderLabels(list(self.COLUMNS))
        table.setSelectionBehavior(QAbstractItemView.SelectRows)
        table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        table.setAlternatingRowColors(False)
        table.verticalHeader().setVisible(False)
        table.verticalHeader().setDefaultSectionSize(30)
        table.setShowGrid(False)
        table.setContextMenuPolicy(Qt.CustomContextMenu)
        table.customContextMenuRequested.connect(self._on_context_menu)
        table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        table.itemSelectionChanged.connect(self._update_actions_state)

        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        # 密码列是唯一可伸缩列（Stretch），窗口拉宽时吸收剩余空间，避免右侧留白；
        # 其余 4 列为固定初始宽度的 Interactive（用户可手动调整）。
        table.setColumnWidth(_COL_PASSWORD, 260)
        header.setSectionResizeMode(_COL_PASSWORD, QHeaderView.Stretch)
        table.setColumnWidth(_COL_SOURCE, 180)
        header.setSectionResizeMode(_COL_SOURCE, QHeaderView.Interactive)
        table.setColumnWidth(_COL_HITS, 70)
        header.setSectionResizeMode(_COL_HITS, QHeaderView.Interactive)
        table.setColumnWidth(_COL_LAST, 150)
        header.setSectionResizeMode(_COL_LAST, QHeaderView.Interactive)
        table.setColumnWidth(_COL_CREATED, 150)
        header.setSectionResizeMode(_COL_CREATED, QHeaderView.Interactive)
        return table

    def _build_footer(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.sel_label = QLabel("已选 0 项")
        self.sel_label.setStyleSheet(f"color: {theme.TEXT_FAINT}; background: transparent;")
        row.addWidget(self.sel_label)

        self.delete_btn = QPushButton("删除选中")
        self.delete_btn.setProperty("ghost", "true")
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self._on_delete_selected)
        row.addWidget(self.delete_btn)

        row.addStretch(1)

        self.clear_btn = QPushButton("清空全部")
        self.clear_btn.setProperty("ghost", "true")
        self.clear_btn.setProperty("danger", "true")
        self.clear_btn.clicked.connect(self._on_clear_all)
        row.addWidget(self.clear_btn)
        return bar

    # ---------- 数据加载 ----------

    def refresh(self) -> None:
        """按当前筛选/排序从 core 拉数据并渲染。"""
        self._reload_sources()
        try:
            rows = self._vault.query(
                keyword=self._keyword,
                source=self._source_filter,
                order="source",
                descending=False,
                limit=0,
            )
        except Exception as exc:  # pragma: no cover - 只读失败兜底
            self.statusBar().showMessage(f"加载失败：{exc}", 6000)
            return
        self._rows = rows
        self._render_rows(rows)
        self._update_actions_state()
        self.statusBar().showMessage(f"共 {len(rows)} 条")

    def _reload_sources(self) -> None:
        """重建来源下拉，尽量保留当前选择。"""
        prev = self._current_source_key()
        self.source_combo.blockSignals(True)
        self.source_combo.clear()
        self.source_combo.addItem("全部来源", None)
        try:
            stats = self._vault.sources()
        except Exception:  # pragma: no cover
            stats = []
        for st in stats:
            if st.source == "":
                self.source_combo.addItem(f"{_EMPTY_SOURCE_LABEL} ({st.count})",
                                          _NO_SOURCE_KEY)
            else:
                self.source_combo.addItem(f"{st.source} ({st.count})", st.source)
        # 还原选择
        idx = self.source_combo.findData(prev)
        self.source_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.source_combo.blockSignals(False)

    def _render_rows(self, rows: list[VaultEntry]) -> None:
        plain = self.show_plain.isChecked()
        self.table.setRowCount(0)
        self.table.setRowCount(len(rows))
        for r, entry in enumerate(rows):
            # 列 0 密码
            pwd_item = QTableWidgetItem(entry.password if plain else _mask(entry.password))
            pwd_item.setData(Qt.UserRole, entry.id)
            pwd_item.setToolTip(entry.password)  # tooltip 恒为原文
            self.table.setItem(r, _COL_PASSWORD, pwd_item)

            # 列 1 来源
            if entry.source == "":
                src_item = QTableWidgetItem(_EMPTY_SOURCE_LABEL)
                src_item.setForeground(QColor(theme.TEXT_FAINT))
            else:
                src_item = QTableWidgetItem(entry.source)
            self.table.setItem(r, _COL_SOURCE, src_item)

            # 列 2 命中
            hits_text = "—" if entry.hit_count == 0 else str(entry.hit_count)
            hits_item = QTableWidgetItem(hits_text)
            hits_item.setTextAlignment(Qt.AlignCenter)
            self.table.setItem(r, _COL_HITS, hits_item)

            # 列 3 最近命中
            self.table.setItem(r, _COL_LAST, QTableWidgetItem(_short_ts(entry.last_hit_at)))

            # 列 4 添加时间
            self.table.setItem(r, _COL_CREATED, QTableWidgetItem(_short_ts(entry.created_at, empty="—")))

        self._show_empty_state(len(rows) == 0)

    def _show_empty_state(self, on: bool) -> None:
        """空态：隐藏表格、显示居中引导。区分"空库"与"筛选无结果"。"""
        self.table.setVisible(not on)
        self.empty_label.setVisible(on)
        if on:
            if self._keyword or self._source_filter is not None:
                self.empty_label.setText(f"没有匹配「{self._keyword}」的密码")
            else:
                self.empty_label.setText(_EMPTY_HINT)

    # ---------- 交互 ----------

    def _on_search_changed(self, text: str) -> None:
        self._keyword = text
        self._search_timer.start()  # 防抖 200ms

    def _on_search_debounced(self) -> None:
        self.refresh()

    def _on_source_changed(self, _index: int) -> None:
        self._source_filter = self._current_source_key()
        self.refresh()

    def _current_source_key(self) -> str | None:
        data = self.source_combo.currentData()
        if data == _NO_SOURCE_KEY:
            return ""
        return data  # None 或 source 字符串

    def _on_plain_toggled(self, _on: bool) -> None:
        self._render_rows(self._rows)

    def _selected_ids(self) -> list[int]:
        ids: list[int] = []
        for index in self.table.selectionModel().selectedRows():
            item = self.table.item(index.row(), _COL_PASSWORD)
            if item is not None:
                ids.append(int(item.data(Qt.UserRole)))
        return ids

    def _update_actions_state(self) -> None:
        n = len(self.table.selectionModel().selectedRows())
        self.sel_label.setText(f"已选 {n} 项")
        self.delete_btn.setEnabled(n > 0)

    # ---------- 右键菜单 / 双击 ----------

    def _row_entry(self, row: int) -> VaultEntry | None:
        item = self.table.item(row, _COL_PASSWORD)
        if item is None:
            return None
        entry_id = int(item.data(Qt.UserRole))
        for entry in self._rows:
            if entry.id == entry_id:
                return entry
        return self._vault.get(entry_id)

    def _on_context_menu(self, pos) -> None:
        row = self.table.rowAt(pos.y())
        if row < 0:
            return
        entry = self._row_entry(row)
        if entry is None:
            return
        menu = QMenu(self)
        act_copy = QAction("复制", self)
        act_edit = QAction("编辑", self)
        act_delete = QAction("删除", self)
        act_copy.triggered.connect(lambda: self._on_copy(entry))
        act_edit.triggered.connect(lambda: self._on_edit(entry))
        act_delete.triggered.connect(lambda: self._on_delete_entry(entry))
        menu.addAction(act_copy)
        menu.addAction(act_edit)
        menu.addSeparator()
        menu.addAction(act_delete)
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def _on_cell_double_clicked(self, row: int, _col: int) -> None:
        entry = self._row_entry(row)
        if entry is not None:
            self._on_edit(entry)

    # ---------- 写操作 ----------

    def _on_copy(self, entry: VaultEntry) -> None:
        from PySide6.QtWidgets import QApplication

        QApplication.clipboard().setText(entry.password)
        self.statusBar().showMessage("已复制到剪贴板", 3000)

    def _on_add(self) -> None:
        pwd, ok = QInputDialog.getText(self, "新增密码", "密码：")
        if not ok or not pwd.strip():
            return
        source, ok2 = QInputDialog.getText(
            self, "新增密码", "来源域名（可空）：", text="")
        if not ok2:
            return
        try:
            self._vault.add_manual(pwd.strip(), source.strip())
        except Exception as exc:
            QMessageBox.critical(self, "新增失败", str(exc))
            return
        self.vault_changed.emit()
        self.refresh()

    def _on_edit(self, entry: VaultEntry) -> None:
        new_pwd, ok = QInputDialog.getText(
            self, "编辑密码", "密码：", text=entry.password)
        if not ok:
            return
        new_src, ok2 = QInputDialog.getText(
            self, "编辑密码", "来源域名（留空 = 无来源）：", text=entry.source)
        if not ok2:
            return
        try:
            self._vault.update(entry.id, password=new_pwd, source=new_src)
        except Exception as exc:
            QMessageBox.critical(self, "编辑失败", str(exc))
            return
        self.vault_changed.emit()
        self.refresh()

    def _on_delete_entry(self, entry: VaultEntry) -> None:
        ret = QMessageBox.question(
            self, "删除密码",
            f"确定删除这条密码吗？\n\n[{entry.source or _EMPTY_SOURCE_LABEL}] {entry.password}",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        try:
            self._vault.delete(entry.id)
        except Exception as exc:
            QMessageBox.critical(self, "删除失败", str(exc))
            return
        self.vault_changed.emit()
        self.refresh()

    def _on_delete_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            return
        ret = QMessageBox.question(
            self, "删除选中",
            f"确定删除选中的 {len(ids)} 条密码吗？此操作不可撤销。",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        try:
            self._vault.delete_many(ids)
        except Exception as exc:
            QMessageBox.critical(self, "删除失败", str(exc))
            return
        self.vault_changed.emit()
        self.refresh()

    def _on_clear_all(self) -> None:
        total = self._vault.count()
        if total == 0:
            QMessageBox.information(self, "清空密码库", "密码库已经是空的。")
            return
        ret = QMessageBox.warning(
            self, "清空密码库",
            f"将删除全部 {total} 条密码，此操作不可撤销。\n\n确定继续吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        try:
            self._vault.clear_all()
        except Exception as exc:
            QMessageBox.critical(self, "清空失败", str(exc))
            return
        self.vault_changed.emit()
        self.refresh()

    # ---------- 生命周期 ----------

    def showEvent(self, event) -> None:
        super().showEvent(event)
        # 毛玻璃必须等 show 之后：winId 到这时才存在
        self.enable_glass()

    def closeEvent(self, event) -> None:
        try:
            self._vault.close()
        except Exception:  # pragma: no cover - 关闭失败不阻塞退出
            pass
        self.closed.emit()  # 通知持有者清理引用，避免复用一个已关闭的连接
        super().closeEvent(event)
