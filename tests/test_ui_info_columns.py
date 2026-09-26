"""任务树「压缩包类型 / 大小」两列的守卫。

需求：解压时在界面上显示这个包是什么格式、多大。实现上做了两件事——
任务树插入两列 + core 新增 info 事件。

这组测试盯住三个真实风险：
1. 列索引平移：新列插在中间，状态/密码/进度/信息的下标全部后移，
   只要漏改一处就会出现"状态跑到类型列里"的错位。所有断言都按列名解析；
2. info 事件与其它事件互相覆盖：失败原因写「信息」列时不能顺手把大小抹掉；
3. 加密包的格式名来自 magic 兜底，界面要能体现出"这是加密包"。
"""
import os

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过任务树列测试")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    from PySide6.QtWidgets import QApplication

    from ui import theme

    inst = QApplication.instance() or QApplication([])
    inst.setStyleSheet(theme.build_stylesheet())
    return inst


@pytest.fixture
def win(app):
    from ui.main_window import MainWindow

    w = MainWindow()
    w.resize(1000, 680)
    yield w
    w.close()
    app.processEvents()


def _col(win, name: str) -> int:
    """按表头名解析列号——测试不认死下标，才能真的守住"错位"这个风险。"""
    header = win.tree.headerItem()
    for i in range(win.tree.columnCount()):
        if header.text(i) == name:
            return i
    raise AssertionError(
        f"任务树没有「{name}」列，现有："
        f"{[header.text(i) for i in range(win.tree.columnCount())]}")


def _feed(win, *events) -> None:
    for e in events:
        win._on_event(dict(e))


def _task_event(task_id=1, parent_id=None, depth=0, path=r"E:\dl\pack.zip") -> dict:
    return {"kind": "task", "task_id": task_id, "parent_id": parent_id,
            "depth": depth, "path": path}


def _info_event(task_id=1, fmt="zip", size=1288490188, uncompressed=0,
                files=0, encrypted=False, volumes=1) -> dict:
    return {"kind": "info", "task_id": task_id, "format": fmt,
            "archive_size": size, "volumes": volumes, "uncompressed": uncompressed,
            "files": files, "encrypted": encrypted}


# ---------- 表头 ----------

def test_header_has_type_and_size_columns(win):
    """类型紧挨文件名、大小紧随其后：读一行的顺序就是「是什么 → 多大 → 怎么样」。"""
    names = [win.tree.headerItem().text(i) for i in range(win.tree.columnCount())]
    assert names == ["压缩包", "类型", "大小", "状态", "密码", "进度", "信息"]


# ---------- info 事件写入 ----------

def test_info_event_fills_type_and_size(win):
    _feed(win, _task_event(), _info_event(fmt="7z", size=1572864))
    item = win.tree.topLevelItem(0)

    assert item.text(_col(win, "类型")) == "7z"
    assert item.text(_col(win, "大小")) == "1.5 MB"


def test_unprobed_task_shows_unknown_placeholder(win):
    """探测还没回来（或直接探测失败）时用 "—" 占位，而不是留空。

    留空的单元格分不清"还没探到"和"这行没有这两项信息"。
    """
    from ui import theme

    _feed(win, _task_event())
    item = win.tree.topLevelItem(0)

    assert item.text(_col(win, "类型")) == "—"
    assert item.text(_col(win, "大小")) == "—"
    color = item.foreground(_col(win, "类型")).color().name().lower()
    assert color == theme.TEXT_FAINT.lower(), "占位符应当比真实值更弱"


def test_info_overwrites_placeholder_with_real_value(win):
    from ui import theme

    _feed(win, _task_event(), _info_event(fmt="zip", size=1024 ** 2))
    item = win.tree.topLevelItem(0)

    assert item.text(_col(win, "类型")) == "zip"
    color = item.foreground(_col(win, "类型")).color().name().lower()
    assert color == theme.TEXT_MUTED.lower(), "占位符的弱色没有换回真实值的颜色"


def test_size_tooltip_reveals_uncompressed_and_count(win):
    """表格里只放压缩包大小；解压后多大、几个文件放 tooltip。"""
    _feed(win, _task_event(),
          _info_event(fmt="zip", size=1572864, uncompressed=10 * 1024 ** 3, files=1234))
    item = win.tree.topLevelItem(0)

    tip = item.toolTip(_col(win, "大小"))
    assert "1.5 MB" in tip
    assert "10 GB" in tip
    assert "1,234" in tip


def test_tooltip_hides_unknown_uncompressed(win):
    """bzip2/xz 这类流式格式 7z 报不出解压后大小（0），不能显示成 "0 B"。"""
    _feed(win, _task_event(), _info_event(fmt="xz", size=2048, uncompressed=0, files=0))
    item = win.tree.topLevelItem(0)

    tip = item.toolTip(_col(win, "大小"))
    assert "0 B" not in tip
    assert "解压后" not in tip


def test_size_tooltip_names_volume_count(win):
    """分卷包：大小是整组之和，tooltip 必须写明几卷。

    大小列本身比用户拖进来的那个 `.001` 大得多——不解释清楚，用户会以为
    这次又算错了（他反馈的就是"分卷包大小不对"）。
    """
    _feed(win, _task_event(), _info_event(fmt="7z", size=3_145_876, volumes=4))
    item = win.tree.topLevelItem(0)

    assert item.text(_col(win, "大小")) == "3.0 MB", "大小列应是整组之和"
    tip = item.toolTip(_col(win, "大小"))
    assert "3.0 MB" in tip
    assert "4 个分卷" in tip


def test_single_volume_tooltip_has_no_volume_mention(win):
    """配重：单卷包不能挂上"1 个分卷"这种废话，否则这句话失去指示意义。"""
    _feed(win, _task_event(), _info_event(fmt="zip", size=1_572_864, volumes=1))
    item = win.tree.topLevelItem(0)

    assert "分卷" not in item.toolTip(_col(win, "大小"))


def test_size_tooltip_defaults_to_single_volume_when_absent(win):
    """老事件里没有 volumes 字段时按单卷处理，不能显示成"0 个分卷"。"""
    ev = _info_event(fmt="zip", size=1_572_864)
    ev.pop("volumes")
    _feed(win, _task_event(), ev)

    tip = win.tree.topLevelItem(0).toolTip(_col(win, "大小"))
    assert "0 个分卷" not in tip
    assert "1.5 MB" in tip


def test_encrypted_archive_marked_with_warn_color(win):
    """加密包：类型列转警示色并注明，用户一眼看出「这个要密码」。"""
    from ui import theme

    _feed(win, _task_event(), _info_event(fmt="7z", encrypted=True))
    item = win.tree.topLevelItem(0)

    color = item.foreground(_col(win, "类型")).color().name().lower()
    assert color == theme.WARN.lower(), "加密包的类型列没有用警示色标出"
    assert "加密" in item.toolTip(_col(win, "类型"))


def test_plain_archive_not_warn_colored(win):
    """配重：非加密包不能被染成警示色，否则警示色失去意义。"""
    from ui import theme

    _feed(win, _task_event(), _info_event(fmt="zip", encrypted=False))
    item = win.tree.topLevelItem(0)

    color = item.foreground(_col(win, "类型")).color().name().lower()
    assert color != theme.WARN.lower()


# ---------- 列索引平移的回归 ----------

def test_other_events_land_in_renamed_columns(win):
    """新列插入后，原有四类内容必须仍落在自己的列里。

    这是防「错位」的主守卫：状态/密码/进度/嵌套说明各断言一次，
    任何一处下标漏改都会被同一条用例抓出来。
    """
    _feed(
        win,
        _task_event(),
        _info_event(size=1024 ** 2),
        {"kind": "status", "task_id": 1, "status": "extracting",
         "password_used": "pw123", "error": "", "extracted_dir": ""},
    )
    item = win.tree.topLevelItem(0)

    assert item.text(_col(win, "状态")) == "解压中"
    assert item.text(_col(win, "密码")) == "pw123"
    assert item.text(_col(win, "进度")) == "0%"
    assert item.text(_col(win, "类型")) == "zip", "类型列被其它事件覆盖了"


def test_size_survives_error_message(win):
    """失败原因写进「信息」列时，类型与大小必须原样保留。

    失败行恰恰是最需要知道"这是什么包、多大"的行——不能因为报了错就变空。
    """
    _feed(
        win,
        _task_event(),
        _info_event(fmt="rar", size=524288000),
        {"kind": "status", "task_id": 1, "status": "failed",
         "password_used": None, "error": "探测失败: 数据错误", "extracted_dir": ""},
    )
    item = win.tree.topLevelItem(0)

    assert item.text(_col(win, "信息")) == "探测失败: 数据错误"
    assert item.text(_col(win, "类型")) == "rar"
    assert item.text(_col(win, "大小")) == "500 MB"


def test_nested_task_depiction_not_shifted(win):
    """嵌套说明仍写「信息」列，且子项挂在父项下。"""
    _feed(win, _task_event(task_id=1), _task_event(task_id=2, parent_id=1, depth=1))
    parent = win.tree.topLevelItem(0)
    child = parent.child(0)

    assert child.text(_col(win, "信息")) == "嵌套第 1 层"
    assert parent.text(_col(win, "信息")) == ""


def test_progress_event_updates_progress_column(win):
    _feed(win, _task_event(), {"kind": "progress", "task_id": 1, "percent": 42})
    item = win.tree.topLevelItem(0)

    assert item.text(_col(win, "进度")) == "42%"


def test_info_for_unknown_task_ignored(app):
    """任务已不在树上（如新一轮已清空）时来了 info，不能抛异常。"""
    from ui.main_window import MainWindow

    w = MainWindow()
    try:
        w._on_event(_info_event(task_id=999))
    finally:
        w.close()
    app.processEvents()


def _first_ink(img, y: int, x0: int, x1: int):
    """该行 [x0, x1) 区间里第一个文字像素的 x；没有文字则返回 None。

    阈值 60：正文色远高于它，而底 surface(#161B22, 最大通道 34)、悬停
    raised(#1F2630, 48)、表头分隔线 line(#2D3844, 68→ 但分隔线不在文字行上)
    都在其下，所以只会命中笔画。
    """
    for x in range(x0, x1):
        c = img.pixelColor(x, y)
        if max(c.red(), c.green(), c.blue()) > 60:
            return x
    return None


def test_header_and_cell_share_one_left_edge(win):
    """表头文字与它那一列的内容，必须从同一个左起点开始。

    这是"表格整不整齐"的本质断言——比逐列写死对齐方式更耐改：
    谁把某一列改成右对齐/居中却忘了同步表头，这里立刻会红。

    历史：给「大小」单开右对齐（想按位数扫）后，实测它的表头距列左 59px、
    而左右邻居都只有 10px，表头行里「大小」跟「类型」隔 101px、跟「状态」
    只隔 22px，整条表头被拉得参差不齐；同为数字的「进度」又是左对齐。
    现在整表统一左对齐，这条断言就是那次回归的守卫。

    第 0 列不参与：树形缩进让内容比表头多缩一层，表头追不上（控件固有行为）。
    """
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QApplication

    # 事件要盖全每一处会写单元格的分支：status 分支和 progress 分支各自都会
    # 往「进度」列写值，少发一个 progress，那段代码就不在这条用例的覆盖里，
    # 有人只给 progress 分支加对齐也测不出来（实测过，确实漏）。
    _feed(win, _task_event(),
          _info_event(fmt="zip", size=1500000000),
          {"kind": "status", "task_id": 1, "status": "extracting",
           "password_used": "pw123", "error": "", "extracted_dir": ""},
          {"kind": "progress", "task_id": 1, "percent": 43})
    win.show()
    QApplication.processEvents()

    tree = win.tree
    hdr = tree.header()
    vp = tree.viewport().mapTo(win, QPoint(0, 0))
    img = win.grab().toImage()
    assert img.width() > 100, f"没抓到画面（{img.width()}x{img.height()}），本用例会假绿"

    item = tree.topLevelItem(0)
    row_y = tree.visualItemRect(item).center().y() + vp.y()
    hdr_y = vp.y() - hdr.height() + hdr.height() // 2

    checked, offenders = 0, []
    for c in range(1, tree.columnCount()):
        x0 = vp.x() + hdr.sectionViewportPosition(c)
        x1 = x0 + hdr.sectionSize(c)
        h = _first_ink(img, hdr_y, x0, x1)
        cell = _first_ink(img, row_y, x0, x1)
        if h is None or cell is None:
            continue  # 该列这一行为空（如失败原因），无从比对
        checked += 1
        if abs(cell - h) > 3:
            offenders.append(
                f"「{tree.headerItem().text(c)}」表头@{h} 内容@{cell}（错开 {cell - h}px）")

    win.close()
    assert checked >= 4, f"只比对了 {checked} 列，覆盖不足，守卫形同虚设"
    assert not offenders, "表头与内容不在同一条左起线上：" + "；".join(offenders)


def test_real_extraction_fills_columns_end_to_end(tmp_path, app):
    """真 7z 跑完整链路：树上真的出现了这个包的类型与大小。

    前面的用例都是手工喂 dict 事件，只证明"写值逻辑对"。这条用真包跑一遍
    探测→info 事件→工作线程信号→任务树，证明整条链路是接通的——否则列做好
    了却永远没数据，测试全绿而功能不存在。
    """
    from core.config import detect_sevenzip

    try:
        exe = detect_sevenzip()
    except FileNotFoundError:
        pytest.skip("未找到 7z.exe，跳过列填充端到端测试")

    import subprocess
    import time

    from PySide6.QtTest import QTest

    from core.appconfig import OPEN_NONE, OUTPUT_SAMEDIR
    from core.formatting import human_size
    from ui.main_window import MainWindow

    src = tmp_path / "dl"
    src.mkdir()
    (src / "note.txt").write_text("包体信息", encoding="utf-8")
    subprocess.run([str(exe), "a", "-tzip", "pack.zip", "note.txt"],
                   cwd=str(src), capture_output=True)
    (src / "note.txt").unlink()  # 只剩包，树上的行才对应真实产物
    size = (src / "pack.zip").stat().st_size
    assert size > 0

    win = MainWindow()
    try:
        win._cfg = win._cfg.with_changes(
            output_mode=OUTPUT_SAMEDIR, open_after=OPEN_NONE)
        win.input_list.add_paths([str(src / "pack.zip")])
        win._start()

        deadline = time.monotonic() + 40
        while time.monotonic() < deadline:
            QTest.qWait(50)
            if win._thread is None:
                break
        assert win._thread is None, "解压线程未在超时内结束"

        item = win.tree.topLevelItem(0)
        assert item.text(_col(win, "状态")) == "完成"
        assert item.text(_col(win, "类型")) == "zip", "类型列没拿到真实探测结果"
        assert item.text(_col(win, "大小")) == human_size(size)
        assert "note.txt" in item.toolTip(_col(win, "大小")) or \
            "解压后" in item.toolTip(_col(win, "大小"))
    finally:
        win.close()
    app.processEvents()


def test_real_split_volume_e2e_shows_group_size(tmp_path, app):
    """真分卷包跑完整链路：列里显示的是整组大小，tooltip 写明卷数。

    补上一条手工喂事件测不到的东西：`volumes` 这个新字段要能穿过
    工作线程的 dict 信号活着到达任务树。字段在 core 里加对了、界面读法也写对了，
    中间那一段丢掉它，用户看到的仍是一个没有解释的数字。
    """
    import os
    import subprocess
    import time

    from PySide6.QtTest import QTest

    from core.appconfig import OPEN_NONE, OUTPUT_SAMEDIR
    from core.config import detect_sevenzip
    from core.formatting import human_size
    from ui.main_window import MainWindow

    try:
        exe = detect_sevenzip()
    except FileNotFoundError:
        pytest.skip("未找到 7z.exe，跳过列填充端到端测试")

    src = tmp_path / "dl"
    src.mkdir()
    (src / "big.bin").write_bytes(os.urandom(3 * 1024 * 1024))  # 不可压缩才会切开
    subprocess.run([str(exe), "a", "-tzip", "-v1m", "pack.zip", "big.bin"],
                   cwd=str(src), capture_output=True)
    vols = sorted(p for p in src.iterdir() if p.name.startswith("pack.zip."))
    assert len(vols) >= 2, f"没切成多卷（{[v.name for v in vols]}）"
    total = sum(v.stat().st_size for v in vols)
    first = vols[0].stat().st_size
    (src / "big.bin").unlink()   # 只剩分卷，树上的行才对应真实产物

    win = MainWindow()
    try:
        win._cfg = win._cfg.with_changes(
            output_mode=OUTPUT_SAMEDIR, open_after=OPEN_NONE)
        win.input_list.add_paths([str(src / "pack.zip.001")])
        win._start()

        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            QTest.qWait(50)
            if win._thread is None:
                break
        assert win._thread is None, "解压线程未在超时内结束"

        item = win.tree.topLevelItem(0)
        assert item.text(_col(win, "状态")) == "完成"
        assert item.text(_col(win, "大小")) == human_size(total), \
            f"列里显示的是 {item.text(_col(win, '大小'))}，整组应为 {human_size(total)}"
        assert item.text(_col(win, "大小")) != human_size(first)
        assert item.text(_col(win, "类型")) == "zip", \
            f"类型列显示的是 {item.text(_col(win, '类型'))!r}，应为里层的 zip"
        assert f"{len(vols)} 个分卷" in item.toolTip(_col(win, "大小")), \
            "tooltip 没把卷数带过来，用户看不懂这个数是怎么来的"
    finally:
        win.close()
    app.processEvents()
