"""云解密接入 pipeline 的行为守卫（真实 7z 构造加密包，假云端实现，绝不出网）。

要守住的不是"能查到密码"，而是这几条：
- 云端从**第一秒**就在后台线程里跑，本地候选同时继续试，谁先出结果用谁；
- 云端一命中就立刻插队 —— 剩下的候选不再跑（那是十分钟级的等待）；
- 本地候选跑完云端还没回来时才阻塞等它；
- 明文包、分卷包不查（前者第一轮就解开，后者的首卷 MD5 与整包索引对不上）；
- 断网时整轮只警告一次，不能把每个包都刷一条。
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from core.cloud_unlock import (
    STATUS_ERROR,
    STATUS_NOT_FOUND,
    STATUS_OK,
    CloudResult,
)
from core.config import Config, detect_sevenzip
from core.pipeline import Pipeline
from core.store import TaskStore
from core.vault import PasswordVault


def _sevenzip_or_skip() -> Path:
    try:
        return detect_sevenzip()
    except FileNotFoundError:
        pytest.skip("未找到 7z.exe，跳过集成测试")


class FakeCloud:
    """假云端客户端：记录被查过的包，按预设结论回话（不联网、瞬时返回）。"""

    def __init__(self, password: str | None = None, status: str = STATUS_OK,
                 message: str = "", error: Exception | None = None):
        self.password = password
        self.status = status
        self.message = message
        self.error = error
        self.calls: list[Path] = []

    def query(self, archive, *, name="", size=None, on_progress=None,
              should_cancel=None):
        self.calls.append(Path(archive))
        if on_progress is not None:
            on_progress(100)
        if self.error is not None:
            raise self.error
        if self.password:
            return CloudResult(self.password, f"云端密码库命中：{self.password}", STATUS_OK)
        return CloudResult(None, self.message or "云端密码库未收录该文件", self.status)


class FakeJob:
    """假的云端后台任务：按脚本回答 poll()，用来精确控制"第几轮才出结果"。"""

    def __init__(self, result: CloudResult | None = None, polls_before_ready: int = 0):
        self._result = result or CloudResult("cloudpw7788", "命中", STATUS_OK)
        self._ready_after = polls_before_ready
        self.polls = 0
        self.waited = False

    @property
    def percent(self) -> int:
        return 100 if self._ready() else 40

    @property
    def running(self) -> bool:
        return not self._ready()

    def _ready(self) -> bool:
        return self.polls > self._ready_after

    def poll(self):
        self.polls += 1
        return self._result if self._ready() else None

    def wait(self, timeout=None):
        self.waited = True
        return self._result


def _make_env(tmp_path: Path):
    exe = _sevenzip_or_skip()
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    db = tmp_path / "app.db"
    cfg = Config.create(sevenzip=str(exe), workdir=tmp_path / "wd")
    vault = PasswordVault(db)
    store = TaskStore(db)
    return exe, inputs, vault, Pipeline(cfg, vault, store)


def _make_encrypted_zip(exe: Path, folder: Path, name: str, password: str) -> Path:
    (folder / "note.txt").write_text("cloud payload", encoding="utf-8")
    proc = subprocess.run(
        [str(exe), "a", f"-p{password}", name, "note.txt"],
        cwd=str(folder), capture_output=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    (folder / "note.txt").unlink()
    return folder / name


# ---------- 端到端：真实 7z + 真线程的云端查询 ----------

def test_cloud_password_is_used_when_local_candidates_fail(tmp_path):
    """文件名不含任何密码线索 → 本地全失败 → 云端命中 → 解压成功并回写密码库。"""
    exe, inputs, vault, pipe = _make_env(tmp_path)
    archive = _make_encrypted_zip(exe, inputs, "pack.zip", "cloudonly7788")
    cloud = FakeCloud("cloudonly7788")
    pipe.cloud_unlock = cloud

    events: list[dict] = []
    report = pipe.run([inputs], on_event=events.append)

    assert report.done == 1, report.warnings
    assert report.needs_password == 0 and report.failed == 0
    assert cloud.calls == [archive], "云端应该正好被查一次"
    # 云端给的密码必须写回密码库：下一次这个包在本地就能命中，不必再烧配额
    assert "cloudonly7788" in vault.candidates_for("")
    messages = [e.get("message", "") for e in events if e["kind"] == "phase"]
    assert any("云端密码库命中" in m for m in messages), messages


def test_cloud_is_not_queried_for_plain_archive(tmp_path):
    exe, inputs, vault, pipe = _make_env(tmp_path)
    (inputs / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run([str(exe), "a", "plain.zip", "a.txt"], cwd=str(inputs),
                   capture_output=True, check=True)
    cloud = FakeCloud("nope")
    pipe.cloud_unlock = cloud

    report = pipe.run([inputs])

    assert report.done == 1
    assert cloud.calls == [], "明文包不该触发云端查询"


def test_cloud_is_skipped_for_multi_volume(tmp_path):
    """分卷包跳过：云端按整包 MD5 索引，首卷的校验值对不上。"""
    exe, inputs, vault, pipe = _make_env(tmp_path)
    # 必须是不可压缩数据：全零/重复字节会被压到一两个分卷里，测不到分卷行为
    (inputs / "big.bin").write_bytes(os.urandom(6000))
    subprocess.run(
        [str(exe), "a", "-pvolpw7788", "-v1k", "pack.zip", "big.bin"],
        cwd=str(inputs), capture_output=True, check=True,
    )
    (inputs / "big.bin").unlink()
    assert (inputs / "pack.zip.001").is_file() and (inputs / "pack.zip.002").is_file()
    cloud = FakeCloud("volpw7788")
    pipe.cloud_unlock = cloud

    report = pipe.run([inputs])

    assert report.needs_password == 1, "分卷包没解出来，应该走待密码"
    assert cloud.calls == [], "分卷包不该去查云端（首卷 MD5 与整包对不上）"


def test_wrong_cloud_password_falls_through_to_manual_prompt(tmp_path):
    """云端给了密码却解不开：继续问用户，并在问询里交代云端已查过。"""
    exe, inputs, vault, pipe = _make_env(tmp_path)
    _make_encrypted_zip(exe, inputs, "pack.zip", "realpw7788")
    cloud = FakeCloud("stale-password")
    pipe.cloud_unlock = cloud
    asked: list[dict] = []

    def ask(info: dict) -> str:
        asked.append(info)
        return "realpw7788"

    report = pipe.run([inputs], ask_password=ask)

    assert len(cloud.calls) == 1
    assert asked and asked[0]["cloud"], "密码问询里没有交代云端结论"
    assert "不正确" in asked[0]["cloud"]
    assert report.done == 1


def test_cloud_network_failure_warns_only_once_per_run(tmp_path):
    """断网时一批包只发一条警告：几十条同样的提示会把界面刷满。"""
    exe, inputs, vault, pipe = _make_env(tmp_path)
    _make_encrypted_zip(exe, inputs, "one.zip", "pw-one-7788")
    _make_encrypted_zip(exe, inputs, "two.zip", "pw-two-7788")
    cloud = FakeCloud(status=STATUS_ERROR, message="连不上云端密码库：URLError")
    pipe.cloud_unlock = cloud

    report = pipe.run([inputs])

    assert report.needs_password == 2
    assert len(cloud.calls) == 2, "两个包都应该试过一次云端"
    cloud_warnings = [w for w in report.warnings if "云端密码库查询失败" in w]
    assert len(cloud_warnings) == 1, f"警告没有去重：{cloud_warnings}"


def test_cloud_not_found_does_not_warn(tmp_path):
    """未收录是常态，不是错误：不该污染警告列表。"""
    exe, inputs, vault, pipe = _make_env(tmp_path)
    _make_encrypted_zip(exe, inputs, "pack.zip", "somepw7788")
    cloud = FakeCloud(status=STATUS_NOT_FOUND, message="云端密码库未收录该文件（失败！）")
    pipe.cloud_unlock = cloud
    asked: list[dict] = []

    def ask(info: dict) -> str:
        asked.append(info)
        return ""

    report = pipe.run([inputs], ask_password=ask)

    assert report.needs_password == 1
    assert not [w for w in report.warnings if "云端" in w]
    assert "未收录" in asked[0]["cloud"]


def test_cloud_result_is_cached_within_one_run(tmp_path):
    """同一个包被重复处理时不再发第二次请求（每次都烧对方一次配额）。"""
    exe, inputs, vault, pipe = _make_env(tmp_path)
    archive = _make_encrypted_zip(exe, inputs, "pack.zip", "cachepw7788")
    cloud = FakeCloud("cachepw7788")
    pipe.cloud_unlock = cloud

    job, cached = pipe._cloud_start(archive, 1)
    assert cached is None and job is not None
    result = job.wait(5)
    assert result is not None, "后台查询没有在 5 秒内返回"
    pipe._cloud_finish(result, archive, None)

    job2, cached2 = pipe._cloud_start(archive, 2)

    assert job2 is None, "缓存命中却又起了一个后台查询"
    assert cached2 is not None and cached2.password == "cachepw7788"
    assert len(cloud.calls) == 1


def test_pipeline_has_no_cloud_client_by_default(tmp_path):
    """Config 默认关：不显式开启就不可能联网。"""
    exe = _sevenzip_or_skip()
    cfg = Config.create(sevenzip=str(exe), workdir=tmp_path / "wd")
    db = tmp_path / "app.db"
    vault, store = PasswordVault(db), TaskStore(db)
    try:
        pipe = Pipeline(cfg, vault, store)
        assert cfg.cloud_unlock is False
        assert pipe.cloud_unlock is None
        job, cached = pipe._cloud_start(tmp_path / "x.zip", 1)
        assert job is None and cached is None
    finally:
        vault.close()
        store.close()


def test_cloud_enabled_through_config_builds_a_client(tmp_path):
    """偏好打开时 Pipeline 自己备好客户端，UI/CLI 不必各写一遍构造逻辑。"""
    exe = _sevenzip_or_skip()
    cfg = Config.create(sevenzip=str(exe), workdir=tmp_path / "wd",
                        cloud_unlock=True, cloud_unlock_timeout=4.0,
                        cloud_unlock_udid="f" * 32)
    db = tmp_path / "app.db"
    vault, store = PasswordVault(db), TaskStore(db)
    try:
        pipe = Pipeline(cfg, vault, store)
        assert pipe.cloud_unlock is not None
        assert pipe.cloud_unlock.timeout == 4.0
        assert pipe.cloud_unlock.udid == "f" * 32
    finally:
        vault.close()
        store.close()


# ---------- 并行与插队的时机（不碰 7z：解压与云端结论都可编排） ----------

def _scripted_pipeline(tmp_path: Path, monkeypatch):
    """造一个不碰 7z 的 pipeline：解压由脚本代替，云端结论由 FakeJob 控制。"""
    exe = _sevenzip_or_skip()
    cfg = Config.create(sevenzip=str(exe), workdir=tmp_path / "wd")
    db = tmp_path / "app.db"
    vault, store = PasswordVault(db), TaskStore(db)
    pipe = Pipeline(cfg, vault, store)
    order: list[tuple[str, str]] = []

    def fake_extract(task_id, archive, out_dir, pwd):
        order.append(("try", pwd))
        if pwd == "cloudpw7788":
            return 0, "", ""
        return 2, "ERROR: Wrong password?", ""

    monkeypatch.setattr(pipe, "_run_extract", fake_extract)
    return pipe, order, (vault, store)


def _inject_job(pipe, job):
    pipe._cloud_start = lambda archive, task_id: (job, None)
    return job


def test_cloud_result_is_used_before_any_candidate(tmp_path, monkeypatch):
    """云端已经出结论（比如缓存命中）：第一轮就用它，本地候选一条都不跑。"""
    pipe, order, handles = _scripted_pipeline(tmp_path, monkeypatch)
    _inject_job(pipe, FakeJob(polls_before_ready=0))
    archive = tmp_path / "pack.zip"
    archive.write_bytes(b"x")
    try:
        outcome = pipe._attempt_extract(archive, tmp_path / "out",
                                       [f"g{i}" for i in range(50)], 1, cloud_ok=True)
    finally:
        for h in handles:
            h.close()

    assert outcome.password == "cloudpw7788"
    assert [pwd for kind, pwd in order] == ["cloudpw7788"], (
        f"云端已有结论，却还去试了本地候选：{order}")


def test_local_candidates_keep_running_while_cloud_is_querying(tmp_path, monkeypatch):
    """云端还在跑（算 MD5）时本地候选不能停 —— 两条线并行，谁先出结果用谁。"""
    pipe, order, handles = _scripted_pipeline(tmp_path, monkeypatch)
    _inject_job(pipe, FakeJob(polls_before_ready=3))
    archive = tmp_path / "pack.zip"
    archive.write_bytes(b"x")
    try:
        outcome = pipe._attempt_extract(archive, tmp_path / "out",
                                       [f"g{i}" for i in range(50)], 1, cloud_ok=True)
    finally:
        for h in handles:
            h.close()

    tried = [pwd for kind, pwd in order]
    assert outcome.password == "cloudpw7788"
    # 前 3 次 poll 还没结论：空密码 + 2 个候选照常跑，第 4 次 poll 才插队
    assert tried == ["", "g0", "g1", "cloudpw7788"], tried


def test_cloud_hit_skips_the_remaining_candidates(tmp_path, monkeypatch):
    """云端一旦命中就收手：两百条候选不该为了"试完"再跑下去。"""
    pipe, order, handles = _scripted_pipeline(tmp_path, monkeypatch)
    _inject_job(pipe, FakeJob(polls_before_ready=2))
    archive = tmp_path / "pack.zip"
    archive.write_bytes(b"x")
    try:
        outcome = pipe._attempt_extract(archive, tmp_path / "out",
                                       [f"g{i}" for i in range(200)], 1, cloud_ok=True)
    finally:
        for h in handles:
            h.close()

    tried = [pwd for kind, pwd in order]
    assert outcome.password == "cloudpw7788"
    assert len(tried) < 10, f"云端命中后还在继续跑候选：{tried}"
    assert tried[-1] == "cloudpw7788"


def test_cloud_is_awaited_when_candidates_are_exhausted(tmp_path, monkeypatch):
    """候选跑完云端还没回来：阻塞等它（它是最后一根稻草），最多等超时。"""
    pipe, order, handles = _scripted_pipeline(tmp_path, monkeypatch)
    job = _inject_job(pipe, FakeJob(
        result=CloudResult("cloudpw7788", "命中", STATUS_OK),
        polls_before_ready=10 ** 6))
    archive = tmp_path / "pack.zip"
    archive.write_bytes(b"x")
    candidates = [f"g{i}" for i in range(5)]
    try:
        outcome = pipe._attempt_extract(archive, tmp_path / "out", candidates, 1,
                                        cloud_ok=True)
    finally:
        for h in handles:
            h.close()

    assert job.waited is True, "候选跑完后没有等云端"
    assert outcome.password == "cloudpw7788"
    assert [pwd for kind, pwd in order] == [""] + candidates + ["cloudpw7788"]


def test_cloud_is_asked_at_most_once_per_attempt(tmp_path, monkeypatch):
    """云端密码试错之后不许再问第二次：每一问都花对方一次配额。"""
    pipe, order, handles = _scripted_pipeline(tmp_path, monkeypatch)
    job = _inject_job(pipe, FakeJob(
        result=CloudResult("stale-pw", "命中", STATUS_OK), polls_before_ready=0))
    archive = tmp_path / "pack.zip"
    archive.write_bytes(b"x")
    try:
        outcome = pipe._attempt_extract(archive, tmp_path / "out",
                                        [f"g{i}" for i in range(20)], 1, cloud_ok=True)
    finally:
        for h in handles:
            h.close()

    assert outcome.password_issue is True
    assert job.polls <= 2, f"云端被反复 poll（{job.polls} 次）"
    assert "不正确" in pipe._cloud_note


def test_cloud_query_does_not_block_the_caller(tmp_path):
    """真线程版：`_cloud_start` 立刻返回，查询在后台跑（不阻塞候选循环）。"""

    class SlowCloud:
        def __init__(self):
            self.calls = 0

        def query(self, archive, *, name="", size=None, on_progress=None,
                  should_cancel=None):
            self.calls += 1
            time.sleep(0.3)                       # 假装在算 MD5 + 等响应
            return CloudResult("slowpw", "命中", STATUS_OK)

    exe = _sevenzip_or_skip()
    cfg = Config.create(sevenzip=str(exe), workdir=tmp_path / "wd")
    db = tmp_path / "app.db"
    vault, store = PasswordVault(db), TaskStore(db)
    try:
        pipe = Pipeline(cfg, vault, store)
        pipe.cloud_unlock = SlowCloud()
        archive = tmp_path / "a.zip"
        archive.write_bytes(b"x")

        started = time.monotonic()
        job, cached = pipe._cloud_start(archive, 1)
        elapsed = time.monotonic() - started

        assert job is not None and cached is None
        assert elapsed < 0.2, f"_cloud_start 阻塞了调用方（{elapsed:.2f}s）"
        assert job.running is True and job.poll() is None
        result = job.wait(5)
        assert result is not None and result.password == "slowpw"
    finally:
        vault.close()
        store.close()
