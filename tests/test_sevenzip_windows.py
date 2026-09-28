"""Windows GUI 启动 7-Zip 时不能闪出控制台窗口。"""
import os
import subprocess
import zipfile

import pytest

from core.config import detect_sevenzip
from core.sevenzip import SevenZip


@pytest.mark.skipif(os.name != "nt", reason="仅 Windows 有 CREATE_NO_WINDOW")
def test_probe_and_extract_hide_7zip_console(tmp_path, monkeypatch):
    try:
        exe = detect_sevenzip()
    except FileNotFoundError:
        pytest.skip("未找到 7z.exe")

    archive = tmp_path / "pack.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("hello.txt", "hello")

    captured_flags = []
    real_popen = subprocess.Popen

    def record_popen(*args, **kwargs):
        captured_flags.append(kwargs.get("creationflags", 0))
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", record_popen)
    sz = SevenZip(exe)
    code, _, _ = sz.list_raw(archive)
    assert code == 0
    code, _, _ = sz.extract(archive, tmp_path / "out")
    assert code == 0
    assert (tmp_path / "out" / "hello.txt").read_text() == "hello"
    assert len(captured_flags) >= 2
    assert all(flags & subprocess.CREATE_NO_WINDOW for flags in captured_flags)
