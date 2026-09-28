"""视频前缀 + ZIP + 假 RAR 尾部的识别与恢复。"""
import io
import os
import subprocess
import zipfile

import pytest

from core.archive_detect import looks_like_archive
from core.embedded_zip import find_embedded_zip, prepare_embedded_zip
from core.sevenzip import SevenZipCancelled


def make_carrier(path, content=b"payload", name="note.txt"):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, content)
    prefix = b"\x00\x00\x00\x20ftypisom" + b"video data" * 100
    trailer = b"Rar!\x1a\x07\x01\x00" + b"fake RAR" * 50
    data = prefix + buffer.getvalue() + trailer
    path.write_bytes(data)
    return data, len(prefix), len(prefix) + len(buffer.getvalue())


def test_detects_video_zip_and_removes_fake_rar_trailer(tmp_path):
    path = tmp_path / "movie.mp4"
    original, start, end = make_carrier(path)
    bounds = find_embedded_zip(path)
    assert (bounds.start, bounds.end) == (start, end)
    assert looks_like_archive(path)
    with prepare_embedded_zip(path, tmp_path / "work") as recovered:
        assert recovered.read_bytes() == original[start:end]
        with zipfile.ZipFile(recovered) as archive:
            assert archive.read("note.txt") == b"payload"
    assert path.read_bytes() == original
    assert not list((tmp_path / "work").glob("embedded_zip_*"))


def test_plain_video_and_plain_zip_do_not_need_preparation(tmp_path):
    video = tmp_path / "normal.mp4"
    video.write_bytes(b"\x00\x00\x00\x20ftypisom" + b"video" * 100)
    assert not looks_like_archive(video)
    assert find_embedded_zip(video) is None
    archive = tmp_path / "normal.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("note.txt", "normal")
    with prepare_embedded_zip(archive, tmp_path / "work") as recovered:
        assert recovered == archive


def test_cancel_preparation_cleans_temporary_copy(tmp_path):
    path = tmp_path / "movie.zip"
    original, _, _ = make_carrier(path)
    assert looks_like_archive(path)
    with pytest.raises(SevenZipCancelled):
        with prepare_embedded_zip(path, tmp_path / "work", should_cancel=lambda: True):
            pytest.fail("不应继续解压")
    assert path.read_bytes() == original
    assert not list((tmp_path / "work").glob("embedded_zip_*"))


def _pipeline(tmp_path):
    from core.config import Config, detect_sevenzip
    from core.pipeline import Pipeline
    from core.store import TaskStore
    from core.vault import PasswordVault

    try:
        sevenzip = detect_sevenzip()
    except FileNotFoundError:
        pytest.skip("未找到 7z.exe")
    cfg = Config.create(sevenzip=str(sevenzip), workdir=tmp_path / "work")
    vault = PasswordVault(tmp_path / "app.db")
    store = TaskStore(tmp_path / "app.db")
    pipe = Pipeline(cfg, vault, store)
    pipe.copy_back = False
    return sevenzip, pipe, vault, store


def test_encrypted_carrier_keeps_original_password_prompt_path(tmp_path):
    sevenzip, pipe, vault, store = _pipeline(tmp_path)
    note = tmp_path / "note.txt"
    note.write_text("encrypted content")
    zip_path = tmp_path / "source.zip"
    result = subprocess.run([str(sevenzip), "a", "-psecret9", str(zip_path), note.name],
                            cwd=tmp_path, capture_output=True)
    assert result.returncode == 0
    carrier = tmp_path / "video.mp4"
    carrier.write_bytes(b"video prefix" * 100 + zip_path.read_bytes() + b"Rar!\x1a\x07\x01\x00fake")
    asked = []
    try:
        report = pipe.run([carrier], ask_password=lambda info: asked.append(info["path"]) or "secret9")
        assert report.done == 1 and report.failed == 0
        assert asked == [str(carrier)]
        from pathlib import Path
        assert (Path(report.output_dirs[0]) / "note.txt").read_text() == "encrypted content"
    finally:
        vault.close()
        store.close()


def test_selected_second_carrier_finds_first_and_joins_inner_volumes(tmp_path):
    """用户只右键第二个视频，仍能找到配套载体并完整展开内层分卷。"""
    sevenzip, pipe, vault, store = _pipeline(tmp_path)
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "data.bin").write_bytes(os.urandom(3500))
    result = subprocess.run([str(sevenzip), "a", "-v1k", "inner.7z", "data.bin"],
                            cwd=stage, capture_output=True)
    assert result.returncode == 0
    parts = sorted(stage.glob("inner.7z.*"))
    assert len(parts) > 1
    inputs = tmp_path / "inputs"
    inputs.mkdir()
    first = inputs / "episode1.zip"
    second = inputs / "episode2.mp4"
    make_carrier(first, parts[0].read_bytes(), parts[0].name)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as bundle:
        for part in parts[1:]:
            bundle.writestr(part.name, part.read_bytes())
    second.write_bytes(b"video" * 100 + buffer.getvalue() + b"Rar!\x1a\x07\x01\x00fake")
    try:
        report = pipe.run([second])
        assert report.failed == 0 and report.done == 3
        extracted = list((tmp_path / "work").rglob("data.bin"))
        assert len(extracted) == 1
        assert extracted[0].parent.name == "inner"
        assert extracted[0].read_bytes() == (stage / "data.bin").read_bytes()
        assert not list((tmp_path / "work").rglob("inner.7z.*"))
        assert first.exists() and second.exists()
    finally:
        vault.close()
        store.close()
