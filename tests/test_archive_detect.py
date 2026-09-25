"""archive_detect 单元测试：magic bytes 嗅探与复合文档排除。"""
from pathlib import Path

import pytest

from core.archive_detect import COMPOUND_ZIP_EXTS, looks_like_archive, volume_info
from core.config import Config
from core.pipeline import Pipeline
from core.store import TaskStore
from core.vault import PasswordVault


def _make_pipeline(tmp_path: Path, **overrides) -> Pipeline:
    """用假 7z 文件构造 Pipeline（_looks_like_archive 不真正调用 7z）。"""
    fake_exe = tmp_path / "fake7z.exe"
    fake_exe.write_bytes(b"MZ")
    cfg = Config.create(sevenzip=str(fake_exe), workdir=tmp_path / "wd", **overrides)
    return Pipeline(cfg, PasswordVault(tmp_path / "db.sqlite"),
                    TaskStore(tmp_path / "db.sqlite"))


# ---------- looks_like_archive: magic 命中 ----------

def test_zip_magic_with_disguised_ext(tmp_path):
    p = tmp_path / "image.dat"
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 32)
    assert looks_like_archive(p) is True


def test_7z_magic(tmp_path):
    p = tmp_path / "blob.bin"
    p.write_bytes(b"7z\xbc\xaf\x27\x1c" + b"\x00" * 16)
    assert looks_like_archive(p) is True


def test_rar_magic(tmp_path):
    p = tmp_path / "noext"
    p.write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 16)
    assert looks_like_archive(p) is True


def test_gzip_magic(tmp_path):
    p = tmp_path / "payload"
    p.write_bytes(b"\x1f\x8b\x08\x00" + b"\x00" * 16)
    assert looks_like_archive(p) is True


def test_tar_magic_at_offset_257(tmp_path):
    data = bytearray(b"\x00" * 512)
    data[257:262] = b"ustar"
    p = tmp_path / "weird"
    p.write_bytes(bytes(data))
    assert looks_like_archive(p) is True


def test_iso_magic_at_0x8001(tmp_path):
    data = bytearray(b"\x00" * (0x8001 + 5))
    data[0x8001:0x8006] = b"CD001"
    p = tmp_path / "disc.img"
    p.write_bytes(bytes(data))
    assert looks_like_archive(p) is True


# ---------- looks_like_archive: 边界与否定 ----------

def test_tiny_file_rejected(tmp_path):
    p = tmp_path / "tiny.bin"
    p.write_bytes(b"PK\x03")  # 只有 3 字节
    assert looks_like_archive(p) is False


def test_empty_file_rejected(tmp_path):
    p = tmp_path / "empty"
    p.write_bytes(b"")
    assert looks_like_archive(p) is False


def test_random_text_rejected(tmp_path):
    p = tmp_path / "readme.txt"
    p.write_bytes(b"just a plain text file, nothing to see here" * 4)
    assert looks_like_archive(p) is False


def test_missing_file_rejected(tmp_path):
    assert looks_like_archive(tmp_path / "nope.bin") is False


# ---------- Pipeline._looks_like_archive: 扩展名优先 + 嗅探兜底 ----------

def test_ext_hit_without_sniff(tmp_path):
    pipe = _make_pipeline(tmp_path)
    p = tmp_path / "a.zip"
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 16)
    assert pipe._looks_like_archive(p) is True


def test_sniff_catches_disguised_ext(tmp_path):
    """扩展名 .dat 不在列表，但 PK 头命中 → 嗅探兜底识别。"""
    pipe = _make_pipeline(tmp_path)
    p = tmp_path / "image.dat"
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 32)
    assert pipe._looks_like_archive(p) is True


def test_sniff_disabled(tmp_path):
    """sniff_archives=False → 扩展名未命中一律不收。"""
    pipe = _make_pipeline(tmp_path, sniff_archives=False)
    p = tmp_path / "image.dat"
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 32)
    assert pipe._looks_like_archive(p) is False


def test_compound_docx_excluded_even_with_pk_header(tmp_path):
    """docx 本质是 zip，但属于应用文档 → 始终排除。"""
    pipe = _make_pipeline(tmp_path)
    p = tmp_path / "report.docx"
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 64)
    assert pipe._looks_like_archive(p) is False


def test_compound_exts_cover_common_formats():
    for ext in (".docx", ".xlsx", ".pptx", ".epub", ".jar", ".apk", ".whl"):
        assert ext in COMPOUND_ZIP_EXTS


# ---------- P0-3 分卷识别：partNN 与 .NNN 两种命名 ----------

def test_volume_info_part_style():
    assert volume_info(Path("a.part1.rar")) == ("a.rar", 1)
    assert volume_info(Path("a.part2.rar")) == ("a.rar", 2)
    assert volume_info(Path("x.PART10.RAR")) == ("x.rar", 10)


def test_volume_info_split_style():
    assert volume_info(Path("v.7z.001")) == ("v.7z", 1)
    assert volume_info(Path("v.7z.031")) == ("v.7z", 31)
    assert volume_info(Path("d.zip.002")) == ("d.zip", 2)


def test_volume_info_non_volume():
    assert volume_info(Path("plain.rar")) is None
    assert volume_info(Path("a.part.rar")) is None      # 没有序号
    assert volume_info(Path("a.part1.txt")) is None     # 非压缩扩展名
    assert volume_info(Path("a.7z.001.txt")) is None


def test_part_volumes_collapse_to_single_task(tmp_path):
    """P0-3 回归：part1/part2/part3.rar 后缀都是 .rar，旧实现会入队 3 个任务。

    只有首卷能被 7z 正确解压，其余单独解必失败——用户会看到莫名的 FAILED。
    """
    pipe = _make_pipeline(tmp_path)
    inputs = tmp_path / "in"
    inputs.mkdir()
    for name in ("a.part1.rar", "a.part2.rar", "a.part3.rar"):
        (inputs / name).write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 32)

    report = pipe.run([inputs])

    top = [t for t in pipe.store.list_all() if t.depth == 0]
    assert len(top) == 1, f"分卷被拆成了 {len(top)} 个任务"
    assert top[0].archive_path.endswith("a.part1.rar")
    # 首卷齐全时应当静默收敛，不必打扰用户
    assert not any("首卷" in w for w in report.warnings)


def test_missing_first_volume_warned_and_skipped(tmp_path):
    """只拖入非首卷时应明确告警，而不是入队一个注定失败的任务。"""
    pipe = _make_pipeline(tmp_path)
    inputs = tmp_path / "in"
    inputs.mkdir()
    (inputs / "a.part2.rar").write_bytes(b"Rar!\x1a\x07\x01\x00" + b"\x00" * 32)

    report = pipe.run([inputs])

    assert pipe.store.list_all() == [], "缺少首卷的分卷不应入队"
    assert any("首卷" in w for w in report.warnings)


# ---------- P1-1 嗅探短路：已知非压缩扩展名不读文件头 ----------

def test_known_media_ext_skips_sniffing(tmp_path, monkeypatch):
    """P1-1 回归：已知非压缩扩展名必须直接短路，不读文件头。

    实测背景：解压出的 3 万个小文件逐个 open+read(4096) 使扫描从 2.7s 涨到 7.8s。
    对 .jpg 这类约定俗成的非压缩类型，读文件头没有意义。
    """
    p = tmp_path / "photo.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 64)  # JPEG 头

    opens: list[Path] = []
    real_open = Path.open

    def spy(self, *args, **kwargs):
        opens.append(Path(self))
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", spy)

    assert looks_like_archive(p) is False
    assert opens == [], f"对 {p.name} 触发了文件读取，嗅探未被短路"


def test_unknown_ext_still_sniffed(tmp_path):
    """优化不得退化成"只认白名单"：伪装成未知扩展名的包仍要识别出来。

    这是 test_known_media_ext_skips_sniffing 的配重——一个降开销，一个守能力。
    """
    p = tmp_path / "blob.dat"
    p.write_bytes(b"PK\x03\x04" + b"\x00" * 32)
    assert looks_like_archive(p) is True
