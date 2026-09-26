"""设置窗口「左导航 + 右内容」布局守卫。

背景：改版前 4 个分组纵向堆成一列，实测窗口被 minimumSizeHint 顶到 **1220px**
（代码里的 resize(620,660) 形同废纸），底部的「保存」在 1080p 屏上根本看不见；
9 个开关全都以 `addRow("", cbox)` 塞在字段列里，紧贴上一行输入框，读起来像是那个
输入框的附属说明（实测复选框 x 与字段列 x 完全相同：100 / 112 / 112）。

本文件把这些结构性缺陷钉成可执行断言。三条纪律：
1. **布局不在 QObject 父子链里** —— `findChildren(QFormLayout)` 一个都找不到、
   沿 `widget.parent()` 也永远走不到布局；凡"找布局对象"的写法都会恒判通过（实测两次）。
   所以一律按**渲染坐标**判定。
2. **必须在同一页内找对照物** —— QStackedWidget 的隐藏页占着同一套坐标，
   全窗口搜会把别的页的标签错配过来（实测把「密码尝试」配给了输出位置那页）。
3. 每个用例先断言"样本数量够"，否则删光控件就能让守卫静默通过。
"""
import os

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过设置窗口布局守卫")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QAbstractScrollArea,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QWidget,
)

from ui import theme  # noqa: E402
from ui.settings_window import SettingsWindow  # noqa: E402

# 模块清单：改这里就等于改导航栏。顺序即显示顺序。
MODULES = ["输出位置", "启动与拖入", "解压阈值", "解压行为", "记录与历史"]

# 输入类控件：这些必须全部住在导航页里，漏在页外的就是没归位的设置项。
INPUT_TYPES = (QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox)


@pytest.fixture(scope="module")
def app():
    """按真实启动路径装配：Fusion + 暗色调色板 + 主题 QSS。"""
    from ui.app import _apply_dark_palette

    inst = QApplication.instance() or QApplication([])
    inst.setStyle("Fusion")
    _apply_dark_palette(inst)
    inst.setStyleSheet(theme.build_stylesheet())
    return inst


@pytest.fixture
def win(app, tmp_path):
    from core.appconfig import AppConfig

    w = SettingsWindow(AppConfig(recent_inputs=["D:/a.zip", "D:/b.zip"]),
                       tmp_path / "cfg.json", tmp_path)
    w.resize(780, 620)
    w.show()
    app.processEvents()
    # offscreen 光标常驻 (10,10)，不清掉悬停态会把悬停色当成常态读
    QApplication.sendEvent(w, QEvent(QEvent.Type.Leave))
    app.processEvents()
    yield w
    w.close()
    app.processEvents()


# ---------- 导航骨架 ----------


def test_nav_lists_every_module(win):
    """导航栏逐项列出所有模块，且与页面栈一一对应。"""
    titles = [win.nav.item(i).text() for i in range(win.nav.count())]
    assert titles == MODULES, f"导航项与模块清单不一致：{titles}"
    assert win.stack.count() == win.nav.count(), "导航项数与页面数不等，会点出空白页"


def test_selecting_nav_switches_page(win):
    """点导航必须换页——否则右半边永远是同一页，导航就是个装饰。"""
    for i in range(win.nav.count()):
        win.nav.setCurrentRow(i)
        QApplication.instance().processEvents()
        assert win.stack.currentIndex() == i, f"选中第 {i} 项没切到第 {i} 页"


def test_every_module_page_is_scrollable(win):
    """每页都要包在滚动区里：短屏上内容要能滚，而不是把窗口顶高。"""
    for i in range(win.stack.count()):
        page = win.stack.widget(i)
        assert isinstance(page, QScrollArea), f"第 {i} 页不是滚动区（{type(page).__name__}）"
        assert page.widget() is not None, f"第 {i} 页没有内容控件"


def test_module_page_has_title_and_description(win):
    """每页要有自己的标题与一行说明：光靠导航高亮，换个截图就看不出这是哪一页。"""
    for i in range(win.stack.count()):
        page = win.stack.widget(i).widget()
        title = [lb for lb in page.findChildren(QLabel) if lb.property("role") == "pageTitle"]
        desc = [lb for lb in page.findChildren(QLabel) if lb.property("role") == "pageDesc"]
        assert len(title) == 1, f"第 {i} 页标题数 = {len(title)}"
        assert title[0].text() == MODULES[i]
        assert desc and desc[0].text().strip(), f"第 {i} 页缺说明文字"


# ---------- 窗口尺寸：改版前的核心缺陷 ----------


def test_window_can_shrink_to_the_requested_size(win):
    """窗口高度必须服从 resize —— 改版前 minimumSizeHint 是 1220，resize 完全被顶掉。"""
    assert win.height() == 620, f"请求 620 高，实际 {win.height()}（又被内容顶高了？）"
    assert win.minimumSizeHint().height() < 700, (
        f"最小高度 {win.minimumSizeHint().height()}px —— 1080p 屏上底部的保存按钮会看不见")


def test_primary_action_is_always_on_screen(win):
    """「保存」必须在窗口可视范围内，且**不在滚动区内**（否则滚到顶就够不着）。"""
    assert win.save_btn.property("primary") == "true"

    anc = win.save_btn.parent()
    while anc is not None:
        assert not isinstance(anc, QAbstractScrollArea), "保存按钮被放进了滚动区，滚动时会跑掉"
        anc = anc.parent()

    bottom = win.save_btn.mapTo(win, win.save_btn.rect().bottomLeft()).y()
    assert bottom <= win.height(), f"保存按钮底边 y={bottom} 超出窗口高 {win.height()}"


# ---------- 开关不再当"孤儿" ----------


def _row_label_for(win, page, widget) -> str | None:
    """同一页内、垂直居中对齐且位于控件左侧的字段标签；找不到返回 None。

    两处刻意的写法，都是踩坑换来的（见模块 docstring）：
    - 按渲染坐标判定，不去布局树里找 QFormLayout；
    - 只在**本页**范围内找标签，避免隐藏页的同坐标控件被错配过来。
    """
    def vmid(wd) -> float:
        top = wd.mapTo(win, wd.rect().topLeft()).y()
        bottom = wd.mapTo(win, wd.rect().bottomLeft()).y()
        return (top + bottom) / 2

    mid = vmid(widget)
    for lab in page.findChildren(QLabel):
        if lab.property("role") != "fieldLabel" or not lab.isVisibleTo(page):
            continue
        if abs(vmid(lab) - mid) > 8:
            continue
        if lab.mapTo(win, lab.rect().topLeft()).x() < widget.mapTo(win, widget.rect().topLeft()).x():
            return lab.text()
    return None


def test_no_orphan_switch(win):
    """每个开关都要有同行标签。

    改版前 9 个开关全是 `addRow("", cbox)`：字段列里孤零零一个复选框，紧贴在上一行
    输入框下面，读起来像那个输入框的附属说明。现在每行都有「复制回源」「原始包」
    这样的标签，归属明确。
    """
    app = QApplication.instance()
    checked = 0
    for i in range(win.stack.count()):
        win.nav.setCurrentRow(i)
        app.processEvents()
        page = win.stack.currentWidget()
        for cb in page.findChildren(QCheckBox):
            label = _row_label_for(win, page, cb)
            assert label is not None, (
                f"[{MODULES[i]}] 复选框「{cb.text()[:20]}」同行找不到左侧标签——又是孤儿行")
            assert label.strip(), (
                f"[{MODULES[i]}] 复选框「{cb.text()[:20]}」所在行的标签是空的")
            checked += 1
    assert checked >= 8, f"只查到 {checked} 个开关，覆盖不全的守卫等于没有"


def test_every_switch_reads_as_an_action(win):
    """复选框文字本身要说得清"打开会怎样"。"""
    boxes = win.findChildren(QCheckBox)
    assert len(boxes) >= 8, f"只找到 {len(boxes)} 个复选框，样本太少"
    for cb in boxes:
        assert cb.text().strip(), "有复选框没有说明文字"


def test_field_labels_share_one_column(win):
    """各页的标签列必须落在同一条竖线上（相同 x、相同宽）。

    否则翻页时会看到行首一跳一跳——和任务树那次"东倒西歪"是同一类问题。
    """
    app = QApplication.instance()
    seen: set[tuple[int, int]] = set()
    total = 0
    for i in range(win.stack.count()):
        win.nav.setCurrentRow(i)
        app.processEvents()
        page = win.stack.widget(i).widget()
        for lab in page.findChildren(QLabel):
            if lab.property("role") != "fieldLabel":
                continue
            seen.add((lab.x(), lab.width()))
            total += 1
    assert total >= 18, f"只找到 {total} 个字段标签，样本太少"
    assert len(seen) == 1, f"标签列不统一，出现 {sorted(seen)} 种 (x, 宽)"


# ---------- 危险开关的警示 ----------


def _red_pixels(img: QImage) -> int:
    """统计明显偏红的像素。ERR(#F85149) 满足；TEXT_MUTED(#8B949E) 与 ACCENT(#3B9EFF)
    的 g 都大于 r，一个都命中不了——判据天然分得开这三种颜色。

    这里刻意**不断言 QSS 字符串**（"样式表里有 QCheckBox[danger]:checked 规则吗"）：
    那是写死实现的断言，换个写法（比如只染指示框）就会假红，而删光规则又可能漏判。
    上面两条反向的像素用例已经双向覆盖了行为，字符串断言纯属多余。
    """
    n = 0
    for y in range(img.height()):
        for x in range(img.width()):
            c = img.pixelColor(x, y)
            if c.red() > c.green() + 40 and c.red() > c.blue() + 40:
                n += 1
    return n


def test_danger_switch_turns_red_when_on(win):
    """打开危险开关时必须变红——这是唯一的警示时机。"""
    win.delete_orig_check.setChecked(True)
    QApplication.instance().processEvents()

    img = win.delete_orig_check.grab().toImage()
    assert img.width() > 8 and img.height() > 8, "没抓到画面，本用例会假绿"
    assert _red_pixels(img) >= 20, (
        f"勾选后只有 {_red_pixels(img)} 个红色像素——危险开关没有警示")


def test_danger_switch_is_quiet_when_off(win):
    """反向用例：没打开时不得挂红。

    常驻红字会让真正的危险被稀释成"又一个彩色控件"。
    """
    win.delete_orig_check.setChecked(False)
    QApplication.instance().processEvents()

    img = win.delete_orig_check.grab().toImage()
    assert img.width() > 8 and img.height() > 8, "没抓到画面，本用例会假绿"
    assert _red_pixels(img) == 0, (
        f"未勾选态出现 {_red_pixels(img)} 个红色像素——警示变成了常驻噪音")


# ---------- 归位与联动 ----------


def test_every_input_control_lives_in_a_module_page(win):
    """所有输入控件都得住在导航页里。

    放在页面之外的控件既不属于任何模块、也不会被翻页带走，用户永远找不到它。
    """
    pages = [win.stack.widget(i) for i in range(win.stack.count())]

    def in_a_page(widget) -> bool:
        anc = widget.parent()
        while anc is not None:
            if anc in pages:
                return True
            anc = anc.parent()
        return False

    # findChildren 不接受类型元组（只收单个 type），所以先取全部 QWidget 再 isinstance 过滤。
    controls = [w for w in win.findChildren(QWidget) if isinstance(w, INPUT_TYPES)]
    assert len(controls) >= 18, f"只找到 {len(controls)} 个输入控件，样本太少"
    stray = [f"{type(w).__name__}({getattr(w, 'text', lambda: '')()})"
             for w in controls if not in_a_page(w)]
    assert not stray, f"这些控件不在任何模块页里：{stray}"


def test_copy_back_is_disabled_outside_isolated_mode(win):
    """「复制回源」只在隔离模式下成立，其它模式必须禁用。

    pipeline 只在 output_mode == workdir 时看这个开关（见 core/output_plan.py），
    samedir 下留着可勾选就是界面在说谎。
    """
    from core.appconfig import OUTPUT_SAMEDIR, OUTPUT_WORKDIR

    win.output_combo.setCurrentIndex(win.output_combo.findData(OUTPUT_SAMEDIR))
    QApplication.instance().processEvents()
    assert win.copy_back_check.isEnabled() is False, "samedir 模式下「复制回源」该禁用"

    win.output_combo.setCurrentIndex(win.output_combo.findData(OUTPUT_WORKDIR))
    QApplication.instance().processEvents()
    assert win.copy_back_check.isEnabled() is True, "隔离模式下「复制回源」该可用"


def test_nav_selection_is_visually_distinct(win):
    """选中项必须与未选中项在像素上分得开——只靠文字变亮不够，扫一眼要能定位。"""
    win.nav.setCurrentRow(0)
    QApplication.instance().processEvents()

    img = win.grab().toImage()
    sel = win.nav.visualItemRect(win.nav.item(0))
    other = win.nav.visualItemRect(win.nav.item(2))

    def color_at(rect):
        # 取**项右端的空白处**：项内 padding 只有 12px，在左边取样会打到文字笔画，
        # 于是"选中项更亮"其实是被选中的白字撑出来的假象。
        # 用 viewport 换算：item rect 是视口坐标，而列表还压着 QSS 画的边框。
        pt = win.nav.viewport().mapTo(win, rect.topLeft())
        c = img.pixelColor(pt.x() + rect.width() - 8, pt.y() + rect.height() // 2)
        return (c.red(), c.green(), c.blue())

    a, b = color_at(sel), color_at(other)
    assert sum(a) - sum(b) >= 18, (
        f"选中项 {a} 与未选中项 {b} 的底色谱太近，看不出当前在哪一页")
