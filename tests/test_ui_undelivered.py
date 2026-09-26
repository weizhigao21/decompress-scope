"""界面层守卫：未交付任务的呈现、交付失败弹窗、工作目录残留入口。

为什么这些必须在**界面层**再钉一遍：core 把 delivery_error 落库了不等于用户看得见。
旧实现的问题是"库里 done、界面完成、用户什么都没有"——只要呈现层不接，
core 修得再对也还是同一个坑。所以这里钉的是**呈现**与**入口**：

- 任务行必须显示「未交付」+ 警示色，而不是跟着 done 显示绿色「完成」；
- 真实状态与呈现态要分开算：统计卡的「完成」数不能因为改了显示文案就少一个；
- 交付失败必须弹窗（状态栏一闪而过的提示会被忽略）；
- 设置里要有工作目录残留入口——工作目录里的东西全是「成功」状态，
  不主动给入口，用户永远不知道自己磁盘被吃了多少；
- 清理这类**跨 Qt 边界的字节数**不许被 32 位 int 截断：`Signal(int)` 上限 2 GiB，
  越界时 emit 不抛异常、只静默丢信号（详见下面"跨 Qt 边界的数值不许溢出"一节）。
"""
import re
import shutil
import warnings
from pathlib import Path

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过未交付呈现守卫")

from PySide6.QtCore import QEvent, Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from ui import theme  # noqa: E402

(_COL_NAME, _COL_TYPE, _COL_SIZE, _COL_STATUS,
 _COL_PWD, _COL_PROG, _COL_INFO) = range(7)


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    inst = QApplication.instance() or QApplication([])
    inst.setStyleSheet(theme.build_stylesheet())
    return inst


def _status_event(task_id=1, status="done", delivery_error="", **kw):
    ev = {"kind": "status", "task_id": task_id, "status": status,
          "password_used": None, "error": "", "extracted_dir": "", **kw}
    if delivery_error:
        ev["delivery_error"] = delivery_error
    return ev


def _task_event(task_id=1, path="D:/dl/pack.zip", parent_id=None, depth=0):
    return {"kind": "task", "task_id": task_id, "parent_id": parent_id,
            "depth": depth, "path": path}


# ---------- 任务行呈现 ----------


def test_done_row_shows_undelivered_when_delivery_error(app):
    """done + delivery_error → 行上必须是「未交付」+ 警示色，不是绿色的「完成」。"""
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        win._on_event(_task_event())
        win._on_event(_status_event(delivery_error="交付到源目录失败：磁盘空间不足"))
        item = win._task_items[1]

        assert item.text(_COL_STATUS) == "未交付"
        assert item.foreground(_COL_STATUS).color() == QColor(theme.UNDELIVERED)
        assert item.foreground(_COL_STATUS).color() != QColor(theme.STATUS_COLORS["done"]), \
            "未交付不能沿用 done 的绿色"
        assert "磁盘空间不足" in item.text(_COL_INFO), "信息列要写出原因"
        assert "磁盘空间不足" in item.toolTip(_COL_INFO)
        assert "磁盘空间不足" in item.toolTip(_COL_STATUS)
        assert item.text(_COL_PROG) == "—", "没交付就不该显示 100%"
    finally:
        win.close()
    app.processEvents()


def test_plain_done_row_has_no_undelivered_mark(app):
    """没交付问题的任务不受影响，仍是绿色的「完成」。"""
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        win._on_event(_task_event())
        win._on_event(_status_event())
        item = win._task_items[1]

        assert item.text(_COL_STATUS) == "完成"
        assert item.foreground(_COL_STATUS).color() == QColor(theme.STATUS_COLORS["done"])
        assert item.text(_COL_PROG) == "100%"
    finally:
        win.close()
    app.processEvents()


def test_undelivered_does_not_change_done_stat(app):
    """统计卡按**真实状态**算：未交付仍算一个 done，不能凭空少一个。

    呈现态（done_undelivered）与状态（done）混在同一个 role 里是最容易犯的错——
    结果就是四张卡的总和对不上任务树的行数。
    """
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        win._on_event(_task_event())
        win._on_event(_status_event(delivery_error="交付失败"))
        assert win._stats["done"] == 1, "未交付把 done 计数吃掉了"
        assert win._stats["failed"] == 0
        assert win.card_done.value.text() == "1"
    finally:
        win.close()
    app.processEvents()


def test_undelivered_arrives_after_done_event(app):
    """交付发生在收尾阶段，晚于任务变 done：后到的失败信息必须能覆盖绿色完成态。"""
    from ui.main_window import MainWindow

    win = MainWindow()
    try:
        win._on_event(_task_event())
        win._on_event(_status_event(status="done"))
        assert win._task_items[1].text(_COL_STATUS) == "完成"
        # _cleanup 阶段才发出带 delivery_error 的 status 事件
        win._on_event(_status_event(status="done", delivery_error="内层产物落到外层失败"))
        assert win._task_items[1].text(_COL_STATUS) == "未交付"
        assert win._stats["done"] == 1, "第二次事件不该把统计算成两次 done"
    finally:
        win.close()
    app.processEvents()


def test_finished_popup_warns_about_delivery_failure(app, monkeypatch):
    """交付失败必须弹窗——状态栏提示会被当成噪音略过。"""
    from PySide6.QtWidgets import QMessageBox

    from core.models import Task, TaskStatus
    from core.pipeline import RunReport
    from ui.main_window import MainWindow

    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda parent, title, text, *a, **kw: shown.append((title, text)))

    win = MainWindow()
    try:
        t = Task(id=83, archive_path="D:/dl/inner.zip", depth=1,
                 status=TaskStatus.DONE,
                 delivery_error="交付到源目录失败（产物仍在工作目录）：磁盘空间不足")
        report = RunReport(done=1, delivery_failed=1, delivery_failed_tasks=[t])
        win._on_finished(report)

        titles = [t0 for t0, _ in shown]
        assert any("没能交付" in t0 for t0 in titles), shown
        text = dict(shown)["有产物没能交付"]
        assert "inner.zip" in text
        assert "磁盘空间不足" in text
        assert "未交付" in text, "要告诉用户到任务列表哪里去找"
    finally:
        win.close()
    app.processEvents()


def test_finished_without_delivery_failure_does_not_popup(app, monkeypatch):
    """交付正常时不许弹这个窗——误报会让人以后再也不看警告。"""
    from PySide6.QtWidgets import QMessageBox

    from core.pipeline import RunReport
    from ui.main_window import MainWindow

    shown: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda parent, title, text, *a, **kw: shown.append(title))

    win = MainWindow()
    try:
        win._on_finished(RunReport(done=3))
        assert "有产物没能交付" not in shown
    finally:
        win.close()
    app.processEvents()


# ---------- 工作目录残留入口（已从主界面移入设置窗口） ----------
#
# 原先这一行常驻主界面底部。现在随其余低频设置一起收进「设置 → 输出位置」，
# 主界面只留「添加输入 → 开始解压」一条主线。**入口本身必须有**：
# 工作目录里的东西全是「成功」状态，不给入口就等于让磁盘悄悄被吃掉。


def _settings(app, tmp_path, **cfg_kw):
    """按真实启动路径装配设置窗口：窗口显示出来才能做像素断言。"""
    from core.appconfig import AppConfig
    from ui.settings_window import SettingsWindow

    win = SettingsWindow(AppConfig(**cfg_kw), tmp_path / "cfg.json", tmp_path,
                         db_path=tmp_path / "t.db")
    win.resize(780, 620)
    win.show()
    app.processEvents()
    # offscreen 光标常驻 (10,10)，不清掉悬停态会把悬停色当成常态读
    QApplication.sendEvent(win, QEvent(QEvent.Type.Leave))
    app.processEvents()
    return win


def _warm_pixels(img) -> int:
    """统计明显偏暖的像素。

    WARN(#D29922) 满足 r>150 且 r-b>60；TEXT_FAINT(#5E6773) 的 r 只有 94，
    一个都命中不了——判据天然分得开这两档，不需要断言"样式表里有某条规则"。
    """
    n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.red() > 150 and c.red() > c.blue() + 60:
                n += 1
    return n


def test_residue_entry_lives_in_settings(app, tmp_path):
    """残留入口在设置窗口里，且**不再**留在主界面（搬干净，不是复制一份）。"""
    from ui.main_window import MainWindow

    win = _settings(app, tmp_path)
    try:
        assert win.residue_label.text() in {"无残留", "0 项残留"}
        assert win.residue_open_btn.property("ghost") == "true"
        assert not win.residue_open_btn.property("primary"), "不许再加第二个 primary"
    finally:
        win.close()
    app.processEvents()

    m = MainWindow()
    try:
        assert not hasattr(m, "workdir_open_btn"), "主界面还留着残留入口"
        assert not hasattr(m, "workdir_label"), "主界面还留着残留计数"
        # 密码库那行仍在（不是替换关系）
        assert hasattr(m, "vault_open_btn")
    finally:
        m.close()
    app.processEvents()


def test_residue_count_reflects_leftover_dirs(app, tmp_path):
    """残留计数要真的反映工作目录里的 task_<id> 目录数，并有警示色。

    计数与颜色一起断言：只测数字会漏掉"有残留却和「无残留」长得一样"，
    只测颜色会漏掉"颜色对但数错了"。
    """
    ws = tmp_path / ".workspace"
    (ws / "task_1").mkdir(parents=True)
    (ws / "task_2").mkdir()
    (ws / "不是我的目录").mkdir()

    win = _settings(app, tmp_path)
    try:
        assert win.residue_label.text() == "2 项残留", win.residue_label.text()
        img = win.residue_label.grab().toImage()
        assert img.width() > 8 and img.height() > 8, "没抓到画面，本用例会假绿"
        assert _warm_pixels(img) >= 20, "有残留时要用警示色，不能和「无残留」一个样"

        shutil.rmtree(ws)
        win._refresh_residue()
        app.processEvents()
        assert win.residue_label.text() == "无残留"
        assert _warm_pixels(win.residue_label.grab().toImage()) == 0, \
            "清空后还挂着警示色 = 常驻噪音，会把真危险稀释掉"
    finally:
        win.close()
    app.processEvents()


def test_residue_follows_workdir_field(app, tmp_path):
    """计数跟着工作目录输入框走：改完路径还没保存，也该显示那个目录的真实情况。"""
    other = tmp_path / "elsewhere"
    (other / "task_7").mkdir(parents=True)
    (tmp_path / ".workspace").mkdir()

    win = _settings(app, tmp_path)
    try:
        assert win.residue_label.text() == "无残留"
        win.workdir_edit.setText(str(other))
        app.processEvents()
        assert win.residue_label.text() == "1 项残留", win.residue_label.text()
    finally:
        win.close()
    app.processEvents()


def test_workdir_window_is_singleton(app, tmp_path):
    """残留窗口单例复用（沿用密码库窗口的做法）。"""
    win = _settings(app, tmp_path)
    try:
        win._open_workdir_window()
        first = win._workdir_window
        assert first is not None
        win._open_workdir_window()
        assert win._workdir_window is first
    finally:
        win.close()
    app.processEvents()


def test_workdir_window_checks_only_safe_rows(app, tmp_path):
    """默认只勾「可清理」项：默认勾上再让用户取消是最容易误删的设计。"""
    from core.models import Task, TaskStatus
    from core.store import TaskStore
    from ui.workdir_window import _COL_CHECK, _COL_TASK, _COL_VERDICT, WorkdirWindow

    wd = tmp_path / "wd"
    outside = tmp_path / "downloads" / "pack"
    outside.mkdir(parents=True)
    db = tmp_path / "t.db"
    store = TaskStore(db)
    delivered = Task(archive_path=str(tmp_path / "a.zip"), depth=0,
                     status=TaskStatus.DONE, extracted_dir=str(outside))
    delivered.id = store.create(delivered)
    store.update(delivered)
    kept = Task(archive_path=str(tmp_path / "b.zip"), depth=1,
                status=TaskStatus.DONE, extracted_dir=str(wd / "task_x"))
    kept.id = store.create(kept)
    store.update(kept)
    store.close()

    for tid in (delivered.id, kept.id):
        d = wd / f"task_{tid}" / "out" / "pack"
        d.mkdir(parents=True)
        (d / "x.bin").write_bytes(b"x" * 10)
    (wd / "task_9999").mkdir()   # 库里没有 → 孤儿

    win = WorkdirWindow(wd, db)
    try:
        states = {}
        for i in range(win.tree.topLevelItemCount()):
            it = win.tree.topLevelItem(i)
            states[it.text(_COL_TASK)] = it.checkState(_COL_CHECK)
        assert states[f"task_{delivered.id}"] == Qt.Checked, "已交付的副本应默认勾选"
        assert states[f"task_{kept.id}"] == Qt.Unchecked, "产物可能没搬出去，不能默认勾"
        assert states["task_9999"] == Qt.Unchecked
        assert win.tree.topLevelItem(0).text(_COL_VERDICT) in {"可清理", "请保留", "待确认"}
    finally:
        win.close()
    app.processEvents()


def test_workdir_window_prune_removes_only_checked_and_confirms(app, tmp_path, monkeypatch):
    """清理必须二次确认，且只删勾选项；未勾的（危险项）原样留在磁盘上。"""
    from PySide6.QtWidgets import QMessageBox

    from core.models import Task, TaskStatus
    from core.store import TaskStore
    from ui.workdir_window import WorkdirWindow

    wd = tmp_path / "wd"
    outside = tmp_path / "downloads" / "pack"
    outside.mkdir(parents=True)
    db = tmp_path / "t.db"
    store = TaskStore(db)
    delivered = Task(archive_path=str(tmp_path / "a.zip"), depth=0,
                     status=TaskStatus.DONE, extracted_dir=str(outside))
    delivered.id = store.create(delivered)
    store.update(delivered)
    kept = Task(archive_path=str(tmp_path / "b.zip"), depth=1,
                status=TaskStatus.NEEDS_PASSWORD)
    kept.id = store.create(kept)
    store.update(kept)
    store.close()

    for tid in (delivered.id, kept.id):
        d = wd / f"task_{tid}" / "out" / "pack"
        d.mkdir(parents=True)
        (d / "x.bin").write_bytes(b"x" * 100)
    store = TaskStore(db)
    kept_task = store.get(kept.id)
    kept_task.extracted_dir = str(wd / f"task_{kept.id}" / "out" / "pack")
    store.update(kept_task)
    store.close()

    asked: list[str] = []
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda parent, title, text, *a, **kw: (
                            asked.append(text), QMessageBox.Yes)[1])

    win = WorkdirWindow(wd, db)
    try:
        win._on_prune()

        assert asked, "清理前没有二次确认"
        assert "无法恢复" in asked[0], "确认文案要说明不可恢复"
        assert not (wd / f"task_{delivered.id}").exists(), "勾选的副本没被清掉"
        assert (wd / f"task_{kept.id}").is_dir(), "未勾选的目录被删了"
    finally:
        win.close()
    app.processEvents()


def test_workdir_window_cancel_keeps_everything(app, tmp_path, monkeypatch):
    """在确认框点「否」时一个文件都不许删。"""
    from PySide6.QtWidgets import QMessageBox

    from core.models import Task, TaskStatus
    from core.store import TaskStore
    from ui.workdir_window import WorkdirWindow

    wd = tmp_path / "wd"
    outside = tmp_path / "downloads" / "pack"
    outside.mkdir(parents=True)
    db = tmp_path / "t.db"
    store = TaskStore(db)
    t = Task(archive_path=str(tmp_path / "a.zip"), depth=0,
             status=TaskStatus.DONE, extracted_dir=str(outside))
    t.id = store.create(t)
    store.update(t)
    store.close()
    d = wd / f"task_{t.id}" / "out" / "pack"
    d.mkdir(parents=True)
    (d / "x.bin").write_bytes(b"x" * 100)

    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **kw: QMessageBox.No)

    win = WorkdirWindow(wd, db)
    try:
        win._on_prune()
        assert (d / "x.bin").is_file(), "点了「否」还是把文件删了"
    finally:
        win.close()
    app.processEvents()


def test_workdir_window_empty_state(app, tmp_path):
    """没有残留时列表隐藏、空态可见、操作按钮禁用。"""
    from core.store import TaskStore
    from ui.workdir_window import WorkdirWindow

    wd = tmp_path / "wd"
    wd.mkdir()
    db = tmp_path / "t.db"
    TaskStore(db).close()

    win = WorkdirWindow(wd, db)
    try:
        win.show()
        app.processEvents()
        assert win.tree.isVisible() is False
        assert win.empty_label.isVisible() is True
        assert win.prune_btn.isEnabled() is False
    finally:
        win.close()
    app.processEvents()


def test_checked_indicator_is_visible(app, tmp_path):
    """勾选态必须在**像素上**真的画出来。

    不能只断言"QSS 里有这条规则"——真实的失败模式是规则没覆盖到 item view
    的指示器，于是落到平台默认样式：Fusion 在暗色底上只画一个近同色的方框，
    勾没勾几乎分不清。所以这里直接扫像素，确认勾选框是强调色底 + 对勾。
    """
    from core.models import Task, TaskStatus
    from core.store import TaskStore
    from ui.workdir_window import _COL_CHECK, WorkdirWindow

    wd = tmp_path / "wd"
    outside = tmp_path / "downloads" / "pack"
    outside.mkdir(parents=True)
    db = tmp_path / "t.db"
    store = TaskStore(db)
    t = Task(archive_path=str(tmp_path / "a.zip"), depth=0,
             status=TaskStatus.DONE, extracted_dir=str(outside))
    t.id = store.create(t)
    store.update(t)
    store.close()
    d = wd / f"task_{t.id}" / "out" / "pack"
    d.mkdir(parents=True)
    (d / "x.bin").write_bytes(b"x" * 100)

    win = WorkdirWindow(wd, db)
    try:
        win.resize(900, 400)
        win.show()
        app.processEvents()
        item = win.tree.topLevelItem(0)
        assert item.checkState(_COL_CHECK) == Qt.Checked
        rect = win.tree.visualItemRect(item)
        assert rect.isValid() and rect.width() > 40, f"行区域异常: {rect}"

        img = win.tree.viewport().grab().toImage()
        accent = QColor(theme.ACCENT)
        hits = 0
        for y in range(max(0, rect.top()), min(img.height(), rect.bottom())):
            for x in range(max(0, rect.left()), min(img.width(), rect.left() + 40)):
                c = img.pixelColor(x, y)
                if (abs(c.red() - accent.red()) < 12
                        and abs(c.green() - accent.green()) < 12
                        and abs(c.blue() - accent.blue()) < 14):
                    hits += 1
        assert hits > 30, \
            f"勾选框没画出强调色底（命中 {hits} 像素），说明落到了平台默认样式"
    finally:
        win.close()
    app.processEvents()


def test_theme_undelivered_color_is_separate_from_status_table():
    """呈现态取色走独立入口，STATUS_COLORS 仍只装真实状态。

    那张表的键必须与 core.models.TaskStatus 一一对应（test_ui_smoke 有守卫），
    混入 done_undelivered 会让它不再是"状态表"。
    """
    from core.models import TaskStatus

    assert set(theme.STATUS_COLORS) == {s.value for s in TaskStatus}
    assert theme.status_color("done_undelivered") == theme.UNDELIVERED
    assert theme.status_color("done") == theme.STATUS_COLORS["done"]
    assert theme.status_color("no_such_state") == ""


# ---------- 跨 Qt 边界的数值不许溢出 ----------

# 一份真实残留的大小：task_83 实测就是这个数（5.38 GB）。
_OVER_2GIB = 5_782_098_582

_UI_DIR = Path(__file__).resolve().parent.parent / "ui"


def test_no_bare_int_signal_in_ui():
    """ui/ 下禁止裸 `Signal(int)`：它是 C++ 的 32 位 int，装不下字节数。

    PySide6 把 `Signal(int)` 映射成 32 位有符号 int（上限 2,147,483,647 ≈ 2 GiB）。
    越界时**既不抛异常也不报错**，只在 stderr 打一行 shiboken Overflow 警告，
    外加一句「AttributeError: Slot 'xxx(int)' not found.」——信号被静默丢掉。
    真实事故：清完 5.38 GB 残留，主界面残留计数和"已释放 xx"提示一个都没刷新。

    静态扫描而不是逐个测运行时：这类宽度问题是**声明处**就定了的，扫一遍源码
    能连还没写 emit 的新信号一起挡住。要传数值就显式写位宽（"qint64"），
    Python 对象用 object。
    """
    offenders: list[str] = []
    for src in sorted(_UI_DIR.glob("*.py")):
        for i, line in enumerate(src.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if re.search(r"Signal\(\s*int\s*\)", stripped):
                offenders.append(f"{src.name}:{i}: {stripped}")
    assert not offenders, (
        "裸 Signal(int) 上限 2 GiB，字节数会溢出且被静默丢弃；请改 Signal(\"qint64\")：\n"
        + "\n".join(offenders))


def test_workdir_cleaned_signal_carries_multi_gigabyte_value(app, tmp_path, monkeypatch):
    """清理掉 2 GiB 以上时，释放量必须原值送达主窗口（不许截断、不许丢）。

    这是那个真实事故的最小复现：`WorkdirWindow.cleaned` 收的是字节数，
    task_83 一份残留就有 5.38 GB。旧签名 `Signal(int)` 下 emit 不抛异常、
    只是信没送到——所以断言必须落在"槽收到的值"上，而不是"没报错"。
    """
    from PySide6.QtWidgets import QMessageBox

    from core.models import Task, TaskStatus
    from core.store import TaskStore
    from ui.workdir_window import WorkdirWindow

    wd = tmp_path / "wd"
    outside = tmp_path / "downloads" / "pack"
    outside.mkdir(parents=True)
    db = tmp_path / "t.db"
    store = TaskStore(db)
    t = Task(archive_path=str(tmp_path / "big.zip"), depth=0,
             status=TaskStatus.DONE, extracted_dir=str(outside))
    t.id = store.create(t)
    store.update(t)
    store.close()
    d = wd / f"task_{t.id}" / "out" / "pack"
    d.mkdir(parents=True)
    (d / "x.bin").write_bytes(b"x" * 100)

    # 真删磁盘上的东西（走真 prune），但把释放量放大到 > 2 GiB——
    # 造 5 GB 真文件太慢，而溢出的触发条件只跟"返回的值"有关。
    import core.workdir_cleanup as wc

    real_prune = wc.prune
    seen: dict[str, int] = {}

    def _inflated_prune(targets, workdir, allow_unsafe=False):
        freed, errors = real_prune(targets, workdir, allow_unsafe)
        seen["freed"] = freed
        return freed + _OVER_2GIB, errors

    monkeypatch.setattr("ui.workdir_window.prune", _inflated_prune)
    monkeypatch.setattr(QMessageBox, "warning",
                        lambda *a, **kw: QMessageBox.Yes)

    win = WorkdirWindow(wd, db)
    got: list[int] = []
    win.cleaned.connect(got.append)
    try:
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            win._on_prune()

        assert not (wd / f"task_{t.id}").exists(), "前置条件失败：根本没删到东西"
        expected = seen["freed"] + _OVER_2GIB
        overflow = [str(w.message) for w in caught if "Overflow" in str(w.message)]
        assert not overflow, f"字节数跨 Qt 边界溢出被静默降级：{overflow}"
        assert got == [expected], \
            f"释放量没送达（{got} != [{expected}]），主界面残留计数与提示都不会刷新"
    finally:
        win.close()
    app.processEvents()


def test_residue_count_refreshes_after_big_cleanup(app, tmp_path, monkeypatch):
    """端到端：清理 > 2 GiB 之后，设置里的残留计数必须真的归零。

    这才是用户看到的现象——点了清理、文件确实没了、计数却还写着「1 项残留」。
    上面的单测钉信号宽度，这条钉"接上了没有"。
    """
    from PySide6.QtWidgets import QMessageBox

    from core.models import Task, TaskStatus
    from core.store import TaskStore

    db = tmp_path / "t.db"
    ws = tmp_path / ".workspace"
    store = TaskStore(db)
    outside = tmp_path / "downloads" / "pack"
    outside.mkdir(parents=True)
    t = Task(archive_path=str(tmp_path / "big.zip"), depth=0,
             status=TaskStatus.DONE, extracted_dir=str(outside))
    t.id = store.create(t)
    store.update(t)
    store.close()
    d = ws / f"task_{t.id}" / "out" / "pack"
    d.mkdir(parents=True)
    (d / "x.bin").write_bytes(b"x" * 100)

    import core.workdir_cleanup as wc

    real_prune = wc.prune
    monkeypatch.setattr(
        "ui.workdir_window.prune",
        lambda targets, workdir, allow_unsafe=False: (
            real_prune(targets, workdir, allow_unsafe)[0] + _OVER_2GIB, []))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **kw: QMessageBox.Yes)

    win = _settings(app, tmp_path, workdir=str(ws))
    try:
        assert win.residue_label.text() == "1 项残留", win.residue_label.text()
        win._open_workdir_window()
        win._workdir_window._on_prune()

        assert not (ws / f"task_{t.id}").exists(), "前置条件失败：没删到东西"
        assert win.residue_label.text() == "无残留", \
            f"清理后残留计数没刷新：{win.residue_label.text()!r}"
    finally:
        win.close()
    app.processEvents()
