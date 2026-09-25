"""设置窗口：把 core.appconfig.AppConfig 的全部用户偏好可视化编辑。

设计要点：
- 三段式：输出 / 启动 / 解压 三个分组，每组用 QGroupBox 分隔，语义清晰。
- 底部固定操作条：恢复默认（ghost+danger 左）· 取消 · 保存（primary 右）。
- 依赖方向单向 ui → core：本文件不写 JSON，只改 AppConfig 再交给 core 落盘。
- 保存前先 cfg.normalize()，再把规范化后的值回填到控件，用户能立刻看到被纠正的结果。
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QDoubleSpinBox,
    QVBoxLayout,
    QWidget,
    QMainWindow,
)

from core.appconfig import (
    AUTORUN_CONFIRM,
    AUTORUN_DIRECT,
    AUTORUN_OFF,
    OPEN_ITEMS,
    OPEN_LABELS,
    OPEN_NONE,
    OPEN_PATHS,
    OPEN_WORKDIR,
    OUTPUT_MODES,
    OUTPUT_SAMEDIR,
    OUTPUT_WORKDIR,
    AppConfig,
)
from ui import theme

_AUTORUN_LABELS = {
    AUTORUN_OFF: "关闭（手动点开始）",
    AUTORUN_DIRECT: "直接开始",
    AUTORUN_CONFIRM: "先弹确认",
}
_AUTORUN_HINTS = {
    AUTORUN_OFF: "添加文件后不自动解压，需要手动点「开始解压」。",
    AUTORUN_DIRECT: "拖入文件后自动开始解压。连续拖入会合并成一次任务。",
    AUTORUN_CONFIRM: "拖入后弹窗确认真实数量，避免误拖整个磁盘。",
}
_OUTPUT_HINTS = {
    OUTPUT_SAMEDIR: "每个压缩包解到它附近：<所在目录>/<_解压开镜>/<包名>/",
    OUTPUT_WORKDIR: "统一解到隔离工作目录，成功后复制一份回压缩包所在目录。",
}


class SettingsWindow(QMainWindow):
    """偏好设置窗口。保存成功后 emit saved(cfg)。"""

    saved = Signal(object)     # AppConfig
    closed = Signal()

    def __init__(self, cfg: AppConfig, config_path: Path, project_root: Path, parent=None):
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(620, 660)
        self._cfg = cfg
        self._config_path = Path(config_path)
        self._project_root = Path(project_root)
        self._build_ui()
        self._load_from_cfg(cfg)

    # ---------- UI ----------

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        root.addWidget(self._build_output_group())
        root.addWidget(self._build_start_group())
        root.addWidget(self._build_extract_group())
        root.addWidget(self._build_history_group())
        root.addStretch(1)
        root.addWidget(self._build_actions())

        self.statusBar().showMessage(f"配置文件：{self._config_path}")

    def _build_output_group(self) -> QGroupBox:
        box = QGroupBox("输出位置")
        form = QFormLayout(box)
        form.setSpacing(10)
        form.setContentsMargins(12, 14, 12, 12)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)

        self.output_combo = QComboBox()
        self.output_combo.addItem("解压到压缩包所在目录", OUTPUT_SAMEDIR)
        self.output_combo.addItem("解压到隔离工作目录", OUTPUT_WORKDIR)
        self.output_combo.currentIndexChanged.connect(self._on_output_changed)
        form.addRow("默认落点", self.output_combo)

        # 提示不单独占一行（会让分组显得碎），改为跟随控件的 tooltip + 一行弱化说明
        self.output_hint = QLabel()
        self.output_hint.setWordWrap(True)
        self.output_hint.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; background: transparent; font-size: 12px;")
        form.addRow("", self.output_hint)

        self.subdir_edit = QLineEdit()
        self.subdir_edit.setPlaceholderText("_解压开镜")
        self.subdir_edit.setToolTip("在压缩包所在目录下创建的容器目录名，避免产物散落一地")
        form.addRow("容器目录名", self.subdir_edit)

        work_row = QWidget()
        work_box = QHBoxLayout(work_row)
        work_box.setContentsMargins(0, 0, 0, 0)
        work_box.setSpacing(6)
        self.workdir_edit = QLineEdit()
        self.workdir_edit.setPlaceholderText("留空 = 项目目录下的 .workspace")
        work_box.addWidget(self.workdir_edit, stretch=1)
        btn_pick = QPushButton("选择…")
        btn_pick.setProperty("ghost", "true")
        btn_pick.clicked.connect(self._pick_workdir)
        work_box.addWidget(btn_pick)
        form.addRow("工作目录", work_row)

        self.copy_back_check = QCheckBox("隔离解压后复制一份回压缩包所在目录")
        self.copy_back_check.setToolTip(
            "仅「隔离工作目录」模式有意义。关闭 = 源目录完全不被动过")
        form.addRow("", self._left(self.copy_back_check))

        self.overwrite_check = QCheckBox("同名输出目录直接覆盖")
        self.overwrite_check.setToolTip(
            "关闭时（推荐）遇到同名目录自动改名为「xxx (2)」，绝不覆盖既有产物")
        form.addRow("", self._left(self.overwrite_check))
        return box

    @staticmethod
    def _left(widget: QWidget) -> QWidget:
        """把控件包一层并右补弹簧，使其贴左而不是在 QFormLayout 里被拉伸。"""
        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(widget)
        row.addStretch(1)
        return wrap

    def _build_start_group(self) -> QGroupBox:
        box = QGroupBox("启动方式")
        form = QFormLayout(box)
        form.setSpacing(9)
        form.setContentsMargins(12, 14, 12, 12)

        self.autorun_combo = QComboBox()
        for mode in (AUTORUN_DIRECT, AUTORUN_CONFIRM, AUTORUN_OFF):
            self.autorun_combo.addItem(_AUTORUN_LABELS[mode], mode)
        self.autorun_combo.currentIndexChanged.connect(self._on_autorun_changed)
        form.addRow("拖入即开始", self.autorun_combo)

        self.autorun_hint = QLabel()
        self.autorun_hint.setWordWrap(True)
        self.autorun_hint.setStyleSheet(
            f"color: {theme.TEXT_FAINT}; background: transparent; font-size: 12px;")
        form.addRow("", self.autorun_hint)

        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(0, 10000)
        self.delay_spin.setSingleStep(200)
        self.delay_spin.setSuffix(" ms")
        self.delay_spin.setToolTip("连续拖入多个文件的合并窗口，避免拖 5 个启动 5 次")
        form.addRow("启动延迟", self.delay_spin)

        self.remember_check = QCheckBox("记住最近使用的输入路径")
        form.addRow("", self.remember_check)

        self.restore_check = QCheckBox("启动时把上次的输入放回输入区")
        self.restore_check.setToolTip(
            "默认关闭：程序一开就自动解压上次的内容通常不是你想要的")
        form.addRow("", self.restore_check)

        self.recent_spin = QSpinBox()
        self.recent_spin.setRange(0, 200)
        self.recent_spin.setToolTip("0 = 不记录任何历史")
        form.addRow("历史条数上限", self.recent_spin)
        return box

    def _build_extract_group(self) -> QGroupBox:
        box = QGroupBox("解压参数")
        form = QFormLayout(box)
        form.setSpacing(9)
        form.setContentsMargins(12, 14, 12, 12)

        self.depth_spin = QSpinBox()
        self.depth_spin.setRange(1, 10)
        form.addRow("最大嵌套深度", self.depth_spin)

        self.total_spin = QSpinBox()
        self.total_spin.setRange(1, 2000)
        self.total_spin.setSuffix(" GB")
        self.total_spin.setToolTip("解压后总大小超过此值将拒绝，防 zip 炸弹")
        form.addRow("大小上限", self.total_spin)

        self.ratio_spin = QDoubleSpinBox()
        self.ratio_spin.setRange(1.0, 100000.0)
        self.ratio_spin.setDecimals(0)
        self.ratio_spin.setSuffix(" : 1")
        form.addRow("压缩比上限", self.ratio_spin)

        self.attempts_spin = QSpinBox()
        self.attempts_spin.setRange(1, 200)
        form.addRow("密码尝试上限", self.attempts_spin)

        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(30, 86400)
        self.timeout_spin.setSingleStep(300)
        self.timeout_spin.setSuffix(" 秒")
        self.timeout_spin.setToolTip(
            "单个压缩包的解压超时。超大包（几十 GB）在中低速磁盘上可能超过默认的 1 小时，"
            "被中途杀掉时会报解压失败，酌情调大")
        form.addRow("单包解压超时", self.timeout_spin)

        self.skip_done_check = QCheckBox("重跑时跳过已成功解压过的包")
        self.skip_done_check.setToolTip(
            "开启后，之前已成功解压且产物仍在的包不再重复解压，避免同名目录越解越多。"
            "产物被手动删除时会自动重新解压。需要强制重解时用命令行 --force")
        form.addRow("", self.skip_done_check)

        self.sniff_check = QCheckBox("文件头嗅探伪装压缩包")
        self.sniff_check.setToolTip("扩展名不认识时读 magic bytes 判断是否压缩包")
        form.addRow("", self.sniff_check)

        self.keep_mid_check = QCheckBox("保留中间层压缩包")
        form.addRow("", self.keep_mid_check)

        self.delete_orig_check = QCheckBox("完成后删除原始压缩包")
        self.delete_orig_check.setToolTip("危险：原件将被删除，仅在你确认产物完好后再开启")
        self.delete_orig_check.setProperty("danger", "true")
        form.addRow("", self.delete_orig_check)

        self.keep_days_spin = QSpinBox()
        self.keep_days_spin.setRange(1, 365)
        self.keep_days_spin.setSuffix(" 天")
        form.addRow("任务记录保留", self.keep_days_spin)
        return box

    def _build_history_group(self) -> QGroupBox:
        box = QGroupBox("最近输入")
        outer = QVBoxLayout(box)
        outer.setContentsMargins(12, 14, 12, 12)
        outer.setSpacing(8)

        self.history_label = QLabel()
        self.history_label.setWordWrap(True)
        self.history_label.setStyleSheet(
            f"color: {theme.TEXT_MUTED}; background: transparent; font-size: 12px;")
        outer.addWidget(self.history_label)

        row = QHBoxLayout()
        row.addStretch(1)
        self.clear_history_btn = QPushButton("清空历史")
        self.clear_history_btn.setProperty("ghost", "true")
        self.clear_history_btn.setProperty("danger", "true")
        self.clear_history_btn.clicked.connect(self._clear_history)
        row.addWidget(self.clear_history_btn)
        outer.addLayout(row)
        return box

    def _build_actions(self) -> QWidget:
        bar = QWidget()
        row = QHBoxLayout(bar)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)

        self.reset_btn = QPushButton("恢复默认")
        self.reset_btn.setProperty("ghost", "true")
        self.reset_btn.setProperty("danger", "true")
        self.reset_btn.clicked.connect(self._on_reset)
        row.addWidget(self.reset_btn)

        row.addStretch(1)

        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setProperty("ghost", "true")
        self.cancel_btn.clicked.connect(self.close)
        row.addWidget(self.cancel_btn)

        self.save_btn = QPushButton("保存")
        self.save_btn.setProperty("primary", "true")
        self.save_btn.setMinimumHeight(34)
        self.save_btn.setCursor(Qt.PointingHandCursor)
        self.save_btn.clicked.connect(self._on_save)
        row.addWidget(self.save_btn)
        return bar

    # ---------- 数据绑定 ----------

    def _load_from_cfg(self, cfg: AppConfig) -> None:
        """控件 ← 配置。先 blockSignals，避免程序化赋值触发联动与"已修改"态。"""
        self.output_combo.blockSignals(True)
        self.autorun_combo.blockSignals(True)

        idx = self.output_combo.findData(cfg.output_mode)
        self.output_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.output_hint.setText(_OUTPUT_HINTS.get(cfg.output_mode, ""))

        idx = self.autorun_combo.findData(cfg.autorun_mode)
        self.autorun_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.autorun_hint.setText(_AUTORUN_HINTS.get(cfg.autorun_mode, ""))

        self.output_combo.blockSignals(False)
        self.autorun_combo.blockSignals(False)

        self.subdir_edit.setText(cfg.subdir_name)
        self.workdir_edit.setText(cfg.workdir)
        self.overwrite_check.setChecked(cfg.overwrite_existing)
        self.copy_back_check.setChecked(cfg.copy_back_to_source)

        self.delay_spin.setValue(cfg.autorun_delay_ms)
        self.remember_check.setChecked(cfg.remember_inputs)
        self.restore_check.setChecked(cfg.restore_last_inputs)
        self.recent_spin.setValue(cfg.recent_inputs_limit)

        self.depth_spin.setValue(cfg.max_depth)
        self.total_spin.setValue(cfg.max_total_gb)
        self.ratio_spin.setValue(cfg.max_ratio)
        self.attempts_spin.setValue(cfg.max_password_attempts)
        self.timeout_spin.setValue(cfg.extract_timeout)
        self.skip_done_check.setChecked(cfg.skip_done)
        self.sniff_check.setChecked(cfg.sniff_archives)
        self.keep_mid_check.setChecked(cfg.delete_intermediate is False)
        self.delete_orig_check.setChecked(cfg.keep_original is False)
        self.keep_days_spin.setValue(cfg.session_days)

        self._refresh_history_label(cfg)

    def _collect(self) -> AppConfig:
        """控件 → 配置（基于原 cfg 派生，保留本窗口未暴露的字段）。"""
        import dataclasses

        return dataclasses.replace(
            self._cfg,
            output_mode=self.output_combo.currentData() or OUTPUT_SAMEDIR,
            subdir_name=self.subdir_edit.text().strip() or "_解压开镜",
            workdir=self.workdir_edit.text().strip(),
            overwrite_existing=self.overwrite_check.isChecked(),
            copy_back_to_source=self.copy_back_check.isChecked(),
            autorun_mode=self.autorun_combo.currentData() or AUTORUN_DIRECT,
            autorun_delay_ms=self.delay_spin.value(),
            remember_inputs=self.remember_check.isChecked(),
            restore_last_inputs=self.restore_check.isChecked(),
            recent_inputs_limit=self.recent_spin.value(),
            max_depth=self.depth_spin.value(),
            max_total_gb=self.total_spin.value(),
            max_ratio=self.ratio_spin.value(),
            max_password_attempts=self.attempts_spin.value(),
            extract_timeout=self.timeout_spin.value(),
            skip_done=self.skip_done_check.isChecked(),
            sniff_archives=self.sniff_check.isChecked(),
            delete_intermediate=not self.keep_mid_check.isChecked(),
            keep_original=not self.delete_orig_check.isChecked(),
            session_days=self.keep_days_spin.value(),
        ).normalize()

    def _refresh_history_label(self, cfg: AppConfig) -> None:
        n = len(cfg.recent_inputs or [])
        if not cfg.remember_inputs:
            self.history_label.setText("已关闭记录。程序不会保存任何输入路径。")
        elif n == 0:
            self.history_label.setText("暂无记录。")
        else:
            preview = "、".join(Path(p).name for p in cfg.recent_inputs[:3])
            more = f" 等 {n} 条" if n > 3 else ""
            self.history_label.setText(f"已记录 {n} 条：{preview}{more}")

    # ---------- 交互 ----------

    def _on_output_changed(self, _i: int) -> None:
        self.output_hint.setText(_OUTPUT_HINTS.get(self.output_combo.currentData(), ""))

    def _on_autorun_changed(self, _i: int) -> None:
        self.autorun_hint.setText(_AUTORUN_HINTS.get(self.autorun_combo.currentData(), ""))

    def _pick_workdir(self) -> None:
        start = self.workdir_edit.text().strip() or str(self._project_root)
        d = QFileDialog.getExistingDirectory(self, "选择工作目录", start)
        if d:
            self.workdir_edit.setText(d)

    def _clear_history(self) -> None:
        if not self._cfg.recent_inputs:
            QMessageBox.information(self, "清空历史", "当前没有历史记录。")
            return
        ret = QMessageBox.question(
            self, "清空历史",
            f"将删除 {len(self._cfg.recent_inputs)} 条输入路径记录，确定吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        self._cfg.recent_inputs = []
        self._refresh_history_label(self._cfg)

    def _on_reset(self) -> None:
        ret = QMessageBox.question(
            self, "恢复默认设置",
            "所有设置将恢复为出厂默认值（不影响密码库与已解压的文件）。\n\n确定吗？",
            QMessageBox.Yes | QMessageBox.No,
        )
        if ret != QMessageBox.Yes:
            return
        fresh = AppConfig()
        self._load_from_cfg(fresh)
        self.statusBar().showMessage("已恢复默认值，点「保存」生效", 6000)

    def _on_save(self) -> None:
        cfg = self._collect()
        # 工作目录若填了值，先确认可写，避免保存后每次解压都失败
        if cfg.workdir:
            wd = Path(cfg.workdir).expanduser()
            try:
                wd.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                QMessageBox.critical(
                    self, "工作目录不可用",
                    f"无法创建或写入：{wd}\n\n{exc}")
                return
        try:
            cfg.save(self._config_path)
        except OSError as exc:
            QMessageBox.critical(self, "保存失败", f"写入配置失败：{exc}")
            return
        self._cfg = cfg
        # 回填规范化后的值，让用户立刻看到被纠正的结果（如深度被截到 10）
        self._load_from_cfg(cfg)
        self.statusBar().showMessage("已保存", 4000)
        self.saved.emit(cfg)
        self.close()

    # ---------- 生命周期 ----------

    def closeEvent(self, event) -> None:
        self.closed.emit()
        super().closeEvent(event)
