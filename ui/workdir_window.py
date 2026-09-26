"""工作目录残留窗口：盘点隔离工作目录里堆积的产物，确认后清理。

为什么需要一个专门的入口：工作目录里的东西**全都**是"解压成功"状态——库里 done、
界面显示「完成」——所以用户既没有线索知道磁盘被吃了多少，也没有入口清掉它。
实测 `task_83` 一次就留下 5.38 GB。这个窗口把每一份残留的定性
（可清理 / 请保留 / 待确认）与理由摊开，删不删由用户按行勾选决定。

设计要点（沿用 VaultWindow 的骨架）：
- 三段式：顶部汇总 → 中部列表 → 页脚操作条。
- 单例非模态；close() 不触发 destroyed，故自带 closed 信号供主窗口清理引用。
- 依赖方向单向 ui → core：本文件不删文件、不写 SQL，只调 core.workdir_cleanup。
- 判定与删除都在 core（有单测），UI 只负责展示与确认。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.formatting import human_count, human_size
from core.store import TaskStore
from core.workdir_cleanup import CLEANABLE, ORPHAN, prune, scan_workdir
from ui import theme

(_COL_CHECK, _COL_TASK, _COL_VERDICT, _COL_SIZE, _COL_REASON) = range(5)

_KIND_COLORS = {
    CLEANABLE: theme.OK,
    ORPHAN: theme.TEXT_MUTED,
}

_EMPTY_HINT = "工作目录很干净，没有残留。"


class WorkdirWindow(QMainWindow):
    """工作目录残留盘点与清理。清理成功后 emit cleaned(释放字节数)。"""

    # 必须显式写 "qint64"，不能用裸 int：PySide6 把 `Signal(int)` 映射到 C++ 的
    # 32 位 int，而这里传的是**释放的字节数**——上限 2,147,483,647 只够 2 GiB，
    # 一次清理 5.38 GB 就溢出。而且它不抛异常、不报错，只往 stderr 打一行
    # 「libshiboken: Overflow: Value ... exceeds limits of type [signed] "int"」
    # 外加一句「AttributeError: Slot 'MainWindow::_on_workdir_cleaned(int)' not found.」
    # ——信号被静默丢掉，主界面的残留计数和"已释放 xx"提示都不刷新，用户看到的是
    # 清完了但数字还是原样。（qint64 = 9.2 EB，离溢出远得很。）
    cleaned = Signal("qint64")
    closed = Signal()

    def __init__(self, workdir: Path, db_path: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("工作目录残留")
        self.resize(900, 540)
        self._workdir = Path(workdir)
        self._db_path = Path(db_path)
        self._rows: list = []
        self._build_ui()
        self.refresh()

    # ---------- UI 构建 ----------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(14, 12, 14, 12)
        root.setSpacing(10)

        root.addWidget(self._build_toolbar())

        self._table_wrap = QWidget()
        wrap_box = QVBoxLayout(self._table_wrap)
        wrap_box.setContentsMargins(0, 0, 0, 0)
        wrap_box.setSpacing(0)

        self.tree = self._build_tree()
        wrap_box.addWidget(self.tree)

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

        self.path_label = QLabel(str(self._workdir))
        self.path_label.setProperty("role", "hint")
        self.path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.path_label.setToolTip("隔离工作目录（可在设置 → 输出位置里修改）")
        row.addWidget(self.path_label, stretch=1)

        self.refresh_btn = QPushButton("刷新")
        self.refresh_btn.setProperty("ghost", "true")
        self.refresh_btn.clicked.connect(self.refresh)
        row.addWidget(self.refresh_btn)
        return bar

    def _build_tree(self) -> QTreeWidget:
        tree = QTreeWidget()
        tree.setColumnCount(5)
        tree.setHeaderLabels(("", "任务", "判定", "占用", "说明"))
        tree.setRootIsDecorated(False)
        tree.setUniformRowHeights(True)
        tree.setSelectionMode(QTreeWidget.ExtendedSelection)
        header = tree.header()
        header.setSectionResizeMode(_COL_CHECK, QHeaderView.Fixed)
        header.setSectionResizeMode(_COL_TASK, QHeaderView.Fixed)
        header.setSectionResizeMode(_COL_VERDICT, QHeaderView.Fixed)
        header.setSectionResizeMode(_COL_SIZE, QHeaderView.Fixed)
        header.setSectionResizeMode(_COL_REASON, QHeaderView.Stretch)
        tree.setColumnWidth(_COL_CHECK, 34)
        tree.setColumnWidth(_COL_TASK, 110)
        tree.setColumnWidth(_COL_VERDICT, 84)
        tree.setColumnWidth(_COL_SIZE, 96)
        tree.itemChanged.connect(self._on_item_changed)
        return tree

    def _build_footer(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.summary_label = QLabel("")
        self.summary_label.setProperty("role", "hint")
        row.addWidget(self.summary_label, stretch=1)

        self.select_safe_btn = QPushButton("只选可清理项")
        self.select_safe_btn.setProperty("ghost", "true")
        self.select_safe_btn.clicked.connect(self._select_safe)
        row.addWidget(self.select_safe_btn)

        self.prune_btn = QPushButton("清理选中")
        self.prune_btn.setProperty("ghost", "true")
        self.prune_btn.setProperty("danger", "true")
        self.prune_btn.clicked.connect(self._on_prune)
        row.addWidget(self.prune_btn)
        return bar

    # ---------- 数据 ----------

    def refresh(self) -> None:
        store = TaskStore(self._db_path)
        try:
            self._rows = scan_workdir(self._workdir, store)
        finally:
            store.close()
        self._render()

    def _render(self) -> None:
        self.tree.blockSignals(True)
        self.tree.clear()
        for lo in self._rows:
            item = QTreeWidgetItem()
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            # 默认只勾「可清理」项：它们已被确认交付过，这份纯属冗余。
            # 「请保留 / 待确认」默认不勾，理由写在同一行的说明里——
            # 默认勾上再让用户去取消，是最容易造成误删的设计。
            item.setCheckState(_COL_CHECK,
                               Qt.Checked if lo.safe else Qt.Unchecked)
            item.setData(_COL_TASK, Qt.UserRole, lo)
            item.setText(_COL_TASK, lo.path.name)
            item.setText(_COL_VERDICT, lo.label)
            item.setText(_COL_SIZE, human_size(lo.size))
            item.setText(_COL_REASON, lo.reason)
            item.setToolTip(_COL_REASON, lo.reason)
            item.setToolTip(_COL_SIZE, f"{lo.files} 个文件　{human_size(lo.size)}")
            color = QColor(_KIND_COLORS.get(lo.kind, theme.WARN))
            item.setForeground(_COL_VERDICT, color)
            self.tree.addTopLevelItem(item)
        self.tree.blockSignals(False)

        has_rows = bool(self._rows)
        self.tree.setVisible(has_rows)
        self.empty_label.setVisible(not has_rows)
        self.select_safe_btn.setEnabled(has_rows)
        self.prune_btn.setEnabled(has_rows)
        self._update_summary()

    def _update_summary(self) -> None:
        total = sum(lo.size for lo in self._rows)
        files = sum(lo.files for lo in self._rows)
        if not self._rows:
            self.summary_label.setText("没有残留")
        else:
            self.summary_label.setText(
                f"共 {len(self._rows)} 项 · {human_count(files)} 个文件 · "
                f"{human_size(total)}　（勾选后可清理）")

    def _checked(self) -> list:
        out = []
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if item.checkState(_COL_CHECK) == Qt.Checked:
                lo = item.data(_COL_TASK, Qt.UserRole)
                if lo is not None:
                    out.append(lo)
        return out

    # ---------- 交互 ----------

    def _on_item_changed(self, item: QTreeWidgetItem, column: int) -> None:
        if column != _COL_CHECK:
            return
        self._update_summary()

    def _select_safe(self) -> None:
        self.tree.blockSignals(True)
        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            lo = item.data(_COL_TASK, Qt.UserRole)
            item.setCheckState(_COL_CHECK,
                               Qt.Checked if lo is not None and lo.safe else Qt.Unchecked)
        self.tree.blockSignals(False)
        self._update_summary()

    def _on_prune(self) -> None:
        targets = self._checked()
        if not targets:
            QMessageBox.information(self, "清理工作目录", "没有勾选任何项。")
            return
        unsafe = [lo for lo in targets if not lo.safe]
        size = sum(lo.size for lo in targets)
        lines = "\n".join(f"· {lo.path.name}　{lo.label}　{human_size(lo.size)}"
                          for lo in targets[:12])
        more = f"\n…… 另 {len(targets) - 12} 项" if len(targets) > 12 else ""
        text = f"将永久删除以下 {len(targets)} 个目录（合计 {human_size(size)}）：\n\n{lines}{more}"
        # 「不可恢复」对所有删除都成立，必须无条件说——不能只在勾了危险项时才提。
        text += "\n\n删除后无法恢复，也不是移入回收站。"
        if unsafe:
            text += ("\n\n【请注意】其中 "
                     f"{len(unsafe)} 项被标为「请保留 / 待确认」——"
                     "它们的产物可能还没交付出去，这些目录可能是唯一副本。")
        ret = QMessageBox.warning(
            self, "确认清理", text, QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No)
        if ret != QMessageBox.Yes:
            return

        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            freed, errors = prune(targets, self._workdir,
                                  allow_unsafe=bool(unsafe))
        finally:
            QApplication.restoreOverrideCursor()

        self.refresh()
        self.statusBar().showMessage(
            f"已释放 {human_size(freed)}" + (f"　{len(errors)} 项未处理" if errors else ""),
            8000)
        if errors:
            QMessageBox.warning(self, "部分未处理", "\n".join(errors[:20]))
        if freed:
            self.cleaned.emit(freed)

    # ---------- 生命周期 ----------

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)
