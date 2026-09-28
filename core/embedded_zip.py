"""定位视频/SFX 中的 ZIP，去掉前缀和干扰尾部后交给正常解压流程。"""
from __future__ import annotations

import io
import struct
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .sevenzip import SevenZipCancelled

_TAIL_SIZE = 1024 * 1024
_EOCD = b"PK\x05\x06"


@dataclass(frozen=True)
class EmbeddedZip:
    start: int
    end: int
    members: tuple[str, ...] = ()


class _BoundedReader:
    """让 zipfile 看到真实 ZIP 结尾，而不是后面伪造的 RAR 数据。"""

    def __init__(self, stream, end: int):
        self.stream = stream
        self.end = end

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_END:
            offset += self.end
            whence = io.SEEK_SET
        return self.stream.seek(offset, whence)

    def tell(self) -> int:
        return self.stream.tell()

    def read(self, size: int = -1) -> bytes:
        available = max(0, self.end - self.tell())
        return self.stream.read(available if size < 0 else min(size, available))

    def seekable(self) -> bool:
        return True


def find_embedded_zip(path: Path) -> EmbeddedZip | None:
    """只读尾部和目录表验证 ZIP 边界，普通视频不会被全文件扫描。"""
    try:
        size = path.stat().st_size
        if size < 22:
            return None
        with path.open("rb") as stream:
            head = stream.read(8)
            if head.startswith((b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07", b"\x1f\x8b",
                                b"BZh", b"\xfd7zXZ\x00", b"MSCF", b"MSWIM")):
                return None  # 正常其他格式内部即使包含 ZIP，也不能当视频外壳剥离
            tail_start = max(0, size - _TAIL_SIZE)
            stream.seek(tail_start)
            tail = stream.read()
            search_end = len(tail)
            # 限制假签名重试，避免把随机视频数据当成目录表。
            for _ in range(64):
                pos = tail.rfind(_EOCD, 0, search_end)
                if pos < 0:
                    break
                search_end = pos
                if pos + 22 > len(tail):
                    continue
                _, disk, cd_disk, count_disk, count, cd_size, cd_offset, comment = (
                    struct.unpack_from("<4s4H2LH", tail, pos)
                )
                end = tail_start + pos + 22 + comment
                if disk or cd_disk or count_disk != count or end > size:
                    continue
                try:
                    with zipfile.ZipFile(_BoundedReader(stream, end)) as archive:
                        entries = archive.infolist()
                        if cd_offset == 0xFFFFFFFF:  # ZIP64: zipfile 已换算绝对位置
                            start = min((entry.header_offset for entry in entries),
                                        default=archive.start_dir)
                        else:
                            start = archive.start_dir - cd_offset
                        if start < 0 or start >= end:
                            continue
                        stream.seek(start)
                        magic = stream.read(4)
                        if magic not in (b"PK\x03\x04", _EOCD):
                            continue
                        if start or end < size:
                            return EmbeddedZip(start, end, tuple(e.filename for e in entries))
                        return None  # 普通 ZIP 不需要复制
                except (zipfile.BadZipFile, ValueError, EOFError, struct.error):
                    continue
    except OSError:
        return None
    return None


@contextmanager
def prepare_embedded_zip(path: Path, workdir: Path, *, should_cancel=None, on_progress=None):
    bounds = find_embedded_zip(path)
    if bounds is None:
        yield path
        return
    workdir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="embedded_zip_", dir=workdir) as folder:
        recovered = Path(folder) / "payload.zip"
        total = bounds.end - bounds.start
        copied = 0
        if on_progress:
            on_progress(0)
        with path.open("rb") as source, recovered.open("wb") as output:
            source.seek(bounds.start)
            while copied < total:
                if should_cancel and should_cancel():
                    raise SevenZipCancelled("用户取消恢复内嵌压缩包")
                block = source.read(min(8 * 1024 * 1024, total - copied))
                if not block:
                    raise OSError("读取内嵌 ZIP 时文件提前结束，源文件可能正在被修改。")
                output.write(block)
                copied += len(block)
                if on_progress:
                    on_progress(copied * 100 // total)
        yield recovered
