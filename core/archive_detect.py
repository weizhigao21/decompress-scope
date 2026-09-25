"""压缩包文件头嗅探：通过 magic bytes 识别伪装/无扩展名的压缩包。"""
from __future__ import annotations

import re
from pathlib import Path

# (偏移, magic) — 覆盖 zip / rar4+rar5 / 7z / gzip / bzip2 / xz / cab / wim / tar
_MAGIC_HEADERS: tuple[tuple[int, bytes], ...] = (
    (0, b"PK\x03\x04"), (0, b"PK\x05\x06"), (0, b"PK\x07\x08"),
    (0, b"Rar!\x1a\x07"),           # RAR4 与 RAR5 共同前缀
    (0, b"7z\xbc\xaf\x27\x1c"),
    (0, b"\x1f\x8b"),               # gzip
    (0, b"BZh"),                    # bzip2
    (0, b"\xfd7zXZ\x00"),           # xz
    (0, b"MSCF"),                   # cab
    (0, b"MSWIM\x00\x00\x00"),      # wim
    (0, b"WLPWM\x00\x00\x00"),      # wim
    (257, b"ustar"),                # tar
)
_ISO_OFFSET = 0x8001  # ISO9660 卷描述符位置

# 本质是 zip 但属于应用文档/安装包，不应作为压缩包展开
COMPOUND_ZIP_EXTS = frozenset({
    ".docx", ".docm", ".xlsx", ".xlsm", ".xltx", ".xltm",
    ".pptx", ".pptm", ".potx", ".potm",
    ".odt", ".ods", ".odp", ".epub",
    ".jar", ".apk", ".ipa", ".xapk", ".vsix", ".whl",
    ".appx", ".msix",
})

_READ_SIZE = 4096

# 已知"绝不可能是压缩包"的常见扩展名：命中即直接判否，**不读文件头**。
#
# 为什么需要：解压出的素材包里图片/视频动辄几万个，而嗅探对每个扩展名未命中
# 的文件都要 open + read(4096)。实测 3 万个小文件目录，扫描耗时从 2.74s
# 涨到 7.78s（+184%），其中绝大部分花在注定不是压缩包的文件上。
#
# 为什么这是安全的：这里只收"约定俗成不可能是压缩包"的类型。刻意**不收**
# .dat / .bin / .img 这类常见伪装载体，也不收 .exe —— 它们仍走原有的 magic
# 嗅探，因此"伪装成 .dat 的 zip"等能力不受影响。
_NOT_ARCHIVE_EXTS = frozenset({
    # 图片
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".ico", ".svg", ".avif", ".heic", ".psd",
    # 音视频
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a", ".wma", ".ape",
    # 文本与文档
    ".txt", ".md", ".nfo", ".log", ".json", ".xml", ".csv", ".tsv",
    ".ini", ".cfg", ".yaml", ".yml", ".toml", ".pdf", ".rtf",
    ".srt", ".ass", ".vtt",
    # 字体
    ".ttf", ".otf", ".woff", ".woff2",
})

# 分卷命名两种流派（大小写不敏感）：
#   WinRAR 风格： a.part01.rar / a.part1.rar
#   7-Zip 风格：  a.7z.001 / a.zip.002
_PART_VOLUME_RE = re.compile(r"^(?P<base>.+)\.part(?P<num>\d+)\.(?P<ext>rar|zip|7z)$", re.I)
_SPLIT_VOLUME_RE = re.compile(r"^(?P<base>.+)\.(?P<ext>rar|zip|7z)\.(?P<num>\d{3,4})$", re.I)


def volume_info(path: Path) -> tuple[str, int] | None:
    """识别分卷文件，返回 (同组标识, 分卷序号)；非分卷返回 None。

    WinRAR 风格的 `a.part1.rar` / `a.part2.rar` 后缀都是 `.rar`，扩展名判定
    会让它们各成一个任务——但只有首卷能被完整解压，其余单独解必然失败。
    这里给出分组依据，由调用方收敛成"只解首卷"。

    同组标识**不含目录**，调用方需自行拼接父目录：不同目录下的同名分卷
    互不相干，不能合并成一组。
    """
    name = path.name
    for pattern in (_PART_VOLUME_RE, _SPLIT_VOLUME_RE):
        m = pattern.match(name)
        if m:
            return f"{m.group('base')}.{m.group('ext')}".lower(), int(m.group("num"))
    return None


def looks_like_archive(path: Path) -> bool:
    """读取文件头 magic bytes 判断是否压缩包；无法读取/太小一律按否处理。"""
    if path.suffix.lower() in _NOT_ARCHIVE_EXTS:
        # 已知非压缩类型：不碰磁盘。见 _NOT_ARCHIVE_EXTS 的说明。
        return False
    try:
        size = path.stat().st_size
        if size < 8:
            return False
        with path.open("rb") as f:
            head = f.read(_READ_SIZE)
        for offset, magic in _MAGIC_HEADERS:
            end = offset + len(magic)
            if len(head) >= end and head[offset:end] == magic:
                return True
        if size >= _ISO_OFFSET + 5:
            with path.open("rb") as f:
                f.seek(_ISO_OFFSET)
                if f.read(5) == b"CD001":
                    return True
    except OSError:
        return False
    return False
