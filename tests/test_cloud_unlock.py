"""云解密客户端（core/cloud_unlock.py）的纯逻辑守卫。

这里**一个真请求都不发**：所有网络路径都走注入的 `transport`，出事的是解析
与参数拼装 —— 那才是最容易悄悄坏掉、又最难在真机上复现的部分。
"""
from __future__ import annotations

import gzip
import hashlib
import json
import re
import time

import pytest

from core import cloud_unlock as cu
from core.cloud_unlock import (
    STATUS_ERROR,
    STATUS_NOT_FOUND,
    STATUS_OK,
    CloudCancelled,
    CloudQueryJob,
    CloudResult,
    CloudUnlocker,
    build_body,
    build_query,
    build_unlocker,
    default_device_id,
    file_md5,
    parse_answer,
)


# ---------- 校验值 ----------

def test_file_md5_is_uppercase_and_matches_hashlib(tmp_path):
    """接口要求大写 hex：小写会直接查不到，而且不会报错，只会"永远未收录"。"""
    path = tmp_path / "a.zip"
    payload = b"hello cloud" * 1000
    path.write_bytes(payload)

    digest = file_md5(path)

    assert digest == hashlib.md5(payload).hexdigest().upper()
    assert digest.isupper()


def test_file_md5_reports_progress_up_to_100(tmp_path):
    path = tmp_path / "a.zip"
    path.write_bytes(b"x" * (9 * 1024 * 1024))     # 跨过 4 MiB 的分块边界
    seen: list[int] = []

    file_md5(path, on_progress=seen.append)

    assert seen and seen[-1] == 100
    assert seen == sorted(seen), "进度倒退了"


def test_file_md5_can_be_cancelled(tmp_path):
    path = tmp_path / "a.zip"
    path.write_bytes(b"x" * (5 * 1024 * 1024))

    with pytest.raises(CloudCancelled):
        file_md5(path, should_cancel=lambda: True)


def test_file_md5_returns_empty_for_unreadable_path(tmp_path):
    assert file_md5(tmp_path / "nope.zip") == ""


# ---------- 请求参数 ----------

def test_build_query_carries_every_required_parameter():
    query = build_query("ABC123", udid="d" * 32, tm=1793026229, boot_ts=1793020000)
    params = dict(item.split("=", 1) for item in query.split("&"))

    assert params["md5"] == "ABC123"
    assert params["newudid"] == "d" * 32
    assert params["ycs"] == "1"
    assert params["isvip"] == "false"
    assert params["tm"] == "1793026229"
    # his = 启动时间戳.当前时间戳，后半段必须与 tm 完全一致
    assert params["his"] == "1793020000.1793026229"
    assert params["ver"] == cu.APP_VER and params["vercode"] == cu.APP_VERCODE
    assert params["pack"] == cu.APP_PACK and params["chanel"] == cu.APP_CHANNEL
    # key 服务端不校验，但必须是 32 位 hex —— 填个空串虽然也能过，
    # 但一旦对面哪天加了校验，就是"整条链路静默失效"
    assert re.fullmatch(r"[0-9a-f]{32}", params["key"])


def test_build_query_uppercases_md5():
    """调用方传小写也必须被纠正 —— 这是本模块唯一的硬性格式要求。"""
    assert "md5=ABC123" in build_query("abc123", udid="d" * 32)


def test_build_body_urlencodes_name_and_size():
    body = build_body(215, "测试 包.zip").decode("utf-8")

    assert "size=215" in body
    assert "name=%E6%B5%8B%E8%AF%95+%E5%8C%85.zip" in body


def test_device_id_is_stable_32_hex():
    first, second = default_device_id(), default_device_id()

    assert first == second, "同一台机器必须派生同一个设备标识"
    assert re.fullmatch(r"[0-9a-f]{32}", first)


# ---------- 响应解析 ----------

@pytest.mark.parametrize("payload", [
    {"code": 1, "msg": "成功", "data": "mypass"},
    {"code": "1", "data": {"password": "mypass"}},
    {"code": 200, "data": {"pwd": "mypass"}},
    {"data": {"密码": "mypass"}},
    {"code": 1, "data": ["mypass"]},
])
def test_parse_answer_finds_password_in_known_shapes(payload):
    result = parse_answer(json.dumps(payload, ensure_ascii=False).encode())

    assert result.status == STATUS_OK
    assert result.password == "mypass"
    assert result.found is True


def test_parse_answer_reports_not_found_for_201():
    result = parse_answer(b'{"code":201,"message":"\\u6ca1\\u6709\\u627e\\u5230\\u5bc6\\u7801\\uff01"}')

    assert result.status == STATUS_NOT_FOUND
    assert result.password is None
    assert "未收录" in result.message


def test_parse_answer_flags_success_without_password_field():
    """code=1 但没密码 = 结构变了，不能当成"未收录"糊过去。"""
    result = parse_answer(b'{"code":1,"msg":"ok"}')

    assert result.status == STATUS_ERROR
    assert "没有密码字段" in result.message


@pytest.mark.parametrize("raw", [b"", b"<html>502</html>", b"[]", b'"   "'])
def test_parse_answer_never_raises_on_garbage(raw):
    result = parse_answer(raw)

    assert isinstance(result, CloudResult)
    assert result.status in (STATUS_ERROR, STATUS_NOT_FOUND)


def test_parse_answer_strips_whitespace_and_quotes():
    result = parse_answer(b'{"code":1,"data":"  \\"abc123\\"  "}')

    assert result.password == "abc123"


# ---------- 传输层 ----------

class _FakeTransport:
    """记录请求，按预设脚本回包。"""

    def __init__(self, status=200, body=b"", encoding="", error=None):
        self.status = status
        self.body = body
        self.encoding = encoding
        self.error = error
        self.calls: list[dict] = []

    def __call__(self, url, body, headers, timeout):
        self.calls.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        if self.error:
            raise self.error
        return self.status, self.body, self.encoding


def _ok_transport(password: str = "cloudpass") -> _FakeTransport:
    return _FakeTransport(body=json.dumps(
        {"code": 1, "data": password}).encode("utf-8"))


def test_query_posts_md5_and_returns_password(tmp_path):
    path = tmp_path / "包[密码：x].zip"
    payload = b"payload-bytes"
    path.write_bytes(payload)
    transport = _ok_transport("cloudpass")
    unlocker = CloudUnlocker(udid="e" * 32, transport=transport)

    result = unlocker.query(path)

    assert result.status == STATUS_OK and result.password == "cloudpass"
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert cu.YUN_PATH in call["url"]
    # 大写 MD5 —— 大小写错了对面只会回"未收录"
    assert f"md5={hashlib.md5(payload).hexdigest().upper()}" in call["url"]
    assert f"newudid={'e' * 32}" in call["url"]
    assert call["headers"]["Content-Type"] == "application/x-www-form-urlencoded"
    # size 与 name 在 body 里，不在 query 里
    assert f"size={len(payload)}".encode() in call["body"]
    assert "name=" in call["body"].decode("utf-8")


def test_query_decodes_gzip_response(tmp_path):
    path = tmp_path / "a.zip"
    path.write_bytes(b"x")
    raw = gzip.compress(json.dumps({"code": 1, "data": "gzpass"}).encode())
    transport = _FakeTransport(body=raw, encoding="gzip")

    result = CloudUnlocker(transport=transport).query(path)

    assert result.password == "gzpass", "gzip 响应没解压，密码就永远拿不到"


def test_query_swallows_network_errors(tmp_path):
    path = tmp_path / "a.zip"
    path.write_bytes(b"x")
    transport = _FakeTransport(error=OSError("connection reset"))
    events: list[str] = []

    result = CloudUnlocker(transport=transport).query(
        path, on_progress=events.append)

    assert result.status == STATUS_ERROR
    assert "connection reset" in result.message
    assert events[-1] == 100          # 校验值算完了才会发请求


def test_query_reports_http_failure(tmp_path):
    path = tmp_path / "a.zip"
    path.write_bytes(b"x")
    transport = _FakeTransport(status=503, body=b"busy")

    result = CloudUnlocker(transport=transport).query(path)

    assert result.status == STATUS_ERROR
    assert "503" in result.message


def test_query_returns_skipped_when_cancelled(tmp_path):
    path = tmp_path / "a.zip"
    path.write_bytes(b"x" * (5 * 1024 * 1024))
    transport = _ok_transport()

    result = CloudUnlocker(transport=transport).query(path, should_cancel=lambda: True)

    assert result.status == cu.STATUS_SKIPPED
    assert transport.calls == [], "取消之后还是发了请求"


def test_query_does_not_send_when_file_is_empty(tmp_path):
    path = tmp_path / "empty.zip"
    path.write_bytes(b"")
    transport = _ok_transport()

    result = CloudUnlocker(transport=transport).query(path)

    assert result.status == STATUS_ERROR
    assert transport.calls == []


# ---------- 由配置构造 ----------

class _Cfg:
    def __init__(self, **kw):
        self.cloud_unlock = kw.get("cloud_unlock", False)
        self.cloud_unlock_timeout = kw.get("cloud_unlock_timeout", 10.0)
        self.cloud_unlock_udid = kw.get("cloud_unlock_udid", "")


def test_build_unlocker_respects_switch():
    assert build_unlocker(_Cfg(cloud_unlock=False)) is None


def test_build_unlocker_passes_timeout_and_udid():
    unlocker = build_unlocker(_Cfg(cloud_unlock=True, cloud_unlock_timeout=3.5,
                                   cloud_unlock_udid="a" * 32))

    assert isinstance(unlocker, CloudUnlocker)
    assert unlocker.timeout == 3.5
    assert unlocker.udid == "a" * 32


# ---------- 后台任务（与本地候选并行的那条线） ----------

def _slow_unlocker(seconds: float = 0.3, result: CloudResult | None = None):
    class Slow:
        def __init__(self):
            self.calls = 0
            self.progress = []

        def query(self, archive, *, name="", size=None, on_progress=None,
                  should_cancel=None):
            self.calls += 1
            for pct in (10, 50, 100):
                if on_progress is not None:
                    on_progress(pct)
                time.sleep(seconds / 3)
            if result is not None:
                return result
            return CloudResult("threadpw", "命中", STATUS_OK)

    return Slow()


def test_job_runs_in_background_and_does_not_block(tmp_path):
    """`start()` 必须立刻返回：算 MD5 是几秒到几十秒的事，不能堵住候选循环。"""
    path = tmp_path / "a.zip"
    path.write_bytes(b"x")
    unlocker = _slow_unlocker(0.4)
    job = CloudQueryJob(unlocker, path)

    started = time.monotonic()
    job.start()
    elapsed = time.monotonic() - started

    assert elapsed < 0.2, f"start() 阻塞了调用方（{elapsed:.2f}s）"
    assert job.running is True
    assert job.poll() is None, "线程还没跑完就给出了结论"
    result = job.wait(5)
    assert result is not None and result.password == "threadpw"
    assert job.running is False
    assert unlocker.calls == 1


def test_job_exposes_hash_progress_for_the_caller_to_report(tmp_path):
    """进度由调用方在自己的线程里读 —— 任务的进度回调绝不反向调用调用方。"""
    path = tmp_path / "a.zip"
    path.write_bytes(b"x")
    job = CloudQueryJob(_slow_unlocker(0.3), path).start()

    job.wait(5)

    assert job.percent == 100


def test_job_turns_exceptions_into_a_result(tmp_path):
    """线程里的异常不能抛给调用方（那边没有 try 包着）。"""
    path = tmp_path / "a.zip"
    path.write_bytes(b"x")

    class Boom:
        def query(self, *a, **kw):
            raise RuntimeError("socket closed")

    result = CloudQueryJob(Boom(), path).start().wait(5)

    assert result is not None
    assert result.status == STATUS_ERROR
    assert "socket closed" in result.message


def test_job_passes_cancellation_into_the_hash_loop(tmp_path):
    """取消要能中断算 MD5（大包算到一半被取消不该继续读几十 GB）。"""
    seen: list[bool] = []

    class Cancellable:
        def query(self, archive, *, name="", size=None, on_progress=None,
                  should_cancel=None):
            seen.append(bool(should_cancel and should_cancel()))
            return CloudResult(None, "已取消", cu.STATUS_SKIPPED)

    path = tmp_path / "a.zip"
    path.write_bytes(b"x")
    CloudQueryJob(Cancellable(), path, should_cancel=lambda: True).start().wait(5)

    assert seen == [True], "should_cancel 没有传给云端客户端"

