"""真实 7-Zip 验证快速预筛：错误候选不启动进程，通过校验也不能直接算成功。"""
import os
import struct
import subprocess
import zipfile

import pytest

from core.config import Config, detect_sevenzip
from core.pipeline import Pipeline
from core.store import TaskStore
from core.vault import PasswordVault
from core.zip_password_filter import make_zip_password_filter


@pytest.fixture
def env(tmp_path):
    try:
        exe = detect_sevenzip()
    except FileNotFoundError:
        pytest.skip("未找到 7-Zip")
    vault = PasswordVault(tmp_path / "app.db")
    store = TaskStore(tmp_path / "app.db")
    pipe = Pipeline(Config(exe, tmp_path / "work"), vault, store)
    yield exe, pipe
    vault.close()
    store.close()


def make_encrypted(exe, root, method="ZipCrypto", password="MiXeD-secret9", stream=False):
    payload = b"password filter payload" * 20
    archive = root / "archive.weird"
    args = [str(exe), "a", "-tzip", f"-mem={method}", f"-p{password}", str(archive)]
    if stream:
        args += ["-sinote.txt"]
    else:
        (root / "note.txt").write_bytes(payload)
        args += ["note.txt"]
    result = subprocess.run(
        args, input=payload if stream else None, cwd=root, capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return archive, payload


def count_extracts(pipe, monkeypatch):
    calls = []
    extract = pipe.sz.extract

    def counted(archive, dest, password="", **kwargs):
        calls.append(password)
        return extract(archive, dest, password, **kwargs)

    monkeypatch.setattr(pipe.sz, "extract", counted)
    return calls


@pytest.mark.parametrize("method", ["ZipCrypto", "AES128", "AES192", "AES256"])
def test_wrong_candidates_do_not_launch_7z_but_correct_one_extracts(env, tmp_path, monkeypatch, method):
    exe, pipe = env
    archive, payload = make_encrypted(exe, tmp_path, method)
    check = make_zip_password_filter(archive)
    assert check is not None and check.may_match("MiXeD-secret9")
    # 排除偶然命中的短校验值，精确验证已被筛掉的候选不调用解压。
    wrong = [f"wrong-{i}" for i in range(150) if not check.may_match(f"wrong-{i}")]
    assert len(wrong) > 100
    calls = count_extracts(pipe, monkeypatch)
    result = pipe._attempt_extract(archive, tmp_path / "out", wrong + ["MiXeD-secret9"], 1)
    assert result.password == "MiXeD-secret9"
    assert calls == ["", "MiXeD-secret9"]
    assert (tmp_path / "out" / "note.txt").read_bytes() == payload


def test_streaming_zip_uses_time_check_and_still_extracts(env, tmp_path, monkeypatch):
    exe, pipe = env
    archive, payload = make_encrypted(exe, tmp_path, stream=True)
    with zipfile.ZipFile(archive) as bundle:
        assert bundle.infolist()[0].flag_bits & 8
    check = make_zip_password_filter(archive)
    assert check is not None and check.may_match("MiXeD-secret9")
    calls = count_extracts(pipe, monkeypatch)
    result = pipe._attempt_extract(archive, tmp_path / "out", ["MiXeD-secret9"], 1)
    assert result.password == "MiXeD-secret9"
    assert calls == ["", "MiXeD-secret9"]
    assert (tmp_path / "out" / "note.txt").read_bytes() == payload


def test_real_zipcrypto_header_collision_requires_full_validation(env, tmp_path, monkeypatch):
    exe, pipe = env
    archive, payload = make_encrypted(exe, tmp_path)
    check = make_zip_password_filter(archive)
    collision = next((f"wrong-{i}" for i in range(10000)
                      if check.may_match(f"wrong-{i}")), None)
    assert collision is not None
    calls = count_extracts(pipe, monkeypatch)
    result = pipe._attempt_extract(archive, tmp_path / "out", [collision, "MiXeD-secret9"], 1)
    assert calls == ["", collision, "MiXeD-secret9"]
    assert result.password == "MiXeD-secret9"
    assert (tmp_path / "out" / "note.txt").read_bytes() == payload


def test_non_ascii_password_falls_back_to_real_7z(env, tmp_path, monkeypatch):
    exe, pipe = env
    password = "MiXeD-secret9"
    archive, payload = make_encrypted(exe, tmp_path, "AES256", password)
    check = make_zip_password_filter(archive)
    assert check.may_match("错误密码") and check.may_match("正确密码9")
    calls = count_extracts(pipe, monkeypatch)
    result = pipe._attempt_extract(archive, tmp_path / "out", ["错误密码", password], 1)
    assert result.password == password
    assert calls == ["", "错误密码", password]
    assert (tmp_path / "out" / "note.txt").read_bytes() == payload


def test_fast_candidate_loop_can_be_cancelled(env, tmp_path, monkeypatch):
    exe, pipe = env
    archive, _ = make_encrypted(exe, tmp_path)
    check = make_zip_password_filter(archive)
    wrong = [f"wrong-{i}" for i in range(100) if not check.may_match(f"wrong-{i}")]
    cancelled = False

    def on_event(event):
        nonlocal cancelled
        if event.get("message", "").startswith("正在查找密码"):
            cancelled = True

    pipe._on_event = on_event
    pipe._should_cancel = lambda: cancelled
    calls = count_extracts(pipe, monkeypatch)
    result = pipe._attempt_extract(archive, tmp_path / "out", wrong, 1)
    assert result.password is None and not result.password_issue
    assert result.message == "用户取消"
    assert calls == [""]


def test_plain_non_zip_and_broken_files_have_no_filter(tmp_path):
    plain = tmp_path / "plain.zip"
    with zipfile.ZipFile(plain, "w") as bundle:
        bundle.writestr("note.txt", "plain")
    assert make_zip_password_filter(plain) is None
    assert make_zip_password_filter(tmp_path / "missing") is None
    plain.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"normal 7z" * 100)
    assert make_zip_password_filter(plain) is None
    plain.write_bytes(b"PK\x03\x04" + b"broken data" * 100)
    assert make_zip_password_filter(plain) is None


@pytest.mark.parametrize("change", ["strong", "local_flags", "local_crc", "truncated_extra"])
def test_ambiguous_or_unsupported_headers_fall_back(env, tmp_path, change):
    exe, _ = env
    archive, _ = make_encrypted(exe, tmp_path)
    data = bytearray(archive.read_bytes())
    local = data.index(b"PK\x03\x04")
    central = data.index(b"PK\x01\x02")
    if change == "strong":
        flags = struct.unpack_from("<H", data, central + 8)[0]
        struct.pack_into("<H", data, central + 8, flags | 0x40)
    elif change == "local_flags":
        flags = struct.unpack_from("<H", data, local + 6)[0]
        struct.pack_into("<H", data, local + 6, flags ^ 8)
    elif change == "local_crc":
        data[local + 14] ^= 1
    else:
        struct.pack_into("<H", data, local + 28, 65535)
    archive.write_bytes(data)
    assert make_zip_password_filter(archive) is None
