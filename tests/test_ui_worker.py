"""工作线程密码协议的守卫：先手输入与现场回答必须走同一条队。

背景：右键窗口的密码输入区是常驻的，用户可能在 pipeline 还在试候选时就先把密码
交了（先手输入），也可能等 pipeline 开口问才交。两种时序如果各走一条路，就会出现
"先手提交被清掉"或"上一轮的回答被下一轮误用"——所以回答一律进 `_pwd_inbox`
队列，事件只当唤醒用。这里钉的就是这条队列的语义。

`_ask_password` 是会**阻塞**的，所以每个用例都通过 `_ask` 在线程里调用并设超时：
守卫自己不许挂住（挂住的用例比失败的用例危险得多——整套测试会被它拖死）。
"""
import os
import threading
import time

import pytest

pytest.importorskip("PySide6", reason="未安装 PySide6，跳过工作线程测试")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_ASK_TIMEOUT = 5.0


def _worker(tmp_path):
    from core.config import Config
    from ui.worker import ExtractWorker

    # 只测协议，不跑解压：7z 路径随便给一个，Config 直接构造（不触发探测）
    cfg = Config(sevenzip_path=tmp_path / "7z.exe", workdir=tmp_path)
    return ExtractWorker(cfg, tmp_path / "app.db", [str(tmp_path / "a.zip")])


def _recorder(worker):
    """同步记录 need_password（直连，不依赖 Qt 事件循环）。"""
    from PySide6.QtCore import Qt

    asked: list[dict] = []
    worker.need_password.connect(asked.append, Qt.ConnectionType.DirectConnection)
    return asked


def _ask(worker, info=None):
    """在线程里调 `_ask_password` 并等待；超时返回哨兵 `None` 之外的含义由用例判。

    返回 (是否按时返回, 答案)。挂住时 is_done 为 False —— 用例应当直接失败，
    而不是把整个测试进程陪葬。
    """
    box: dict = {}

    def run():
        box["pwd"] = worker._ask_password(info or {"path": "a.zip"})

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(_ASK_TIMEOUT)
    return not thread.is_alive(), box.get("pwd")


def test_typed_ahead_password_is_delivered_without_asking(tmp_path):
    """pipeline 还没开口问，用户就把密码交了 → 直接取走，不再麻烦 UI。"""
    worker = _worker(tmp_path)
    asked = _recorder(worker)

    worker.submit_password("  secret9  ")     # 两端空白要被剥掉

    assert worker._password_ready() == "secret9"
    done, pwd = _ask(worker)
    assert done, "_ask_password 挂住了：先手答案已经在队列里，不该还等"
    assert pwd == "secret9"
    assert asked == [], "答案已经在队列里，程序还弹了一次问询"


def test_answers_are_delivered_in_order(tmp_path):
    """连交两次答案，按提交顺序取 —— 后交的不许覆盖先交的。"""
    worker = _worker(tmp_path)
    worker.submit_password("first")
    worker.submit_password("second")

    done1, first = _ask(worker)
    done2, second = _ask(worker)
    assert done1 and done2, "_ask_password 挂住了"
    assert (first, second) == ("first", "second")


def test_skip_answer_means_give_up(tmp_path):
    """None/空 = 放弃该包（「跳过此包」与取消放行走这条路）。"""
    worker = _worker(tmp_path)
    worker.submit_password(None)
    done, pwd = _ask(worker)
    assert done and pwd is None

    worker.submit_password("   ")
    done2, pwd2 = _ask(worker)
    assert done2 and pwd2 is None


def test_password_ready_is_empty_until_submitted(tmp_path):
    """队列空时探针必须返回 None —— 否则候选循环会被一个不存在的密码带偏。"""
    worker = _worker(tmp_path)
    assert worker._password_ready() is None

    worker.submit_password("secret9")
    assert worker._password_ready() == "secret9"
    done, _ = _ask(worker)
    assert done, "_ask_password 挂住了"
    assert worker._password_ready() is None, "取走的答案不该留在队列里"


def test_ask_blocks_until_ui_submits(tmp_path):
    """队列空时才发 need_password 并阻塞；UI 一提交就带着答案回来。"""
    worker = _worker(tmp_path)
    asked = _recorder(worker)
    box: dict = {}

    def run():
        box["pwd"] = worker._ask_password({"path": "a.zip"})

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 3
    while not asked and time.monotonic() < deadline:
        time.sleep(0.01)
    assert asked, "need_password 没有发出来"
    assert "pwd" not in box, "还没提交答案，_ask_password 不该返回"

    worker.submit_password("secret9")
    thread.join(_ASK_TIMEOUT)
    assert not thread.is_alive(), "_ask_password 没有在提交后返回"
    assert box["pwd"] == "secret9"
