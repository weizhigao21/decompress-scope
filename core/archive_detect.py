"""压缩包文件头嗅探：通过 magic bytes 识别伪装/无扩展名的压缩包。"""
from __future__ import annotations

import re
from pathlib import Path

from .embedded_zip import find_embedded_zip

# (偏移, magic, 类型名) — 覆盖 zip / rar4+rar5 / 7z / gzip / bzip2 / xz / cab / wim / tar
#
# 类型名刻意与 7z `l -slt` 报出的 `Type` 取值对齐（zip/7z/tar/gzip/bzip2/xz/wim），
# 免得同一个包走"7z 报告"和"magic 兜底"两条路径时显示出两种叫法。
_MAGIC_HEADERS: tuple[tuple[int, bytes, str], ...] = (
    (0, b"PK\x03\x04", "zip"), (0, b"PK\x05\x06", "zip"), (0, b"PK\x07\x08", "zip"),
    (0, b"Rar!\x1a\x07", "rar"),           # RAR4 与 RAR5 共同前缀
    (0, b"7z\xbc\xaf\x27\x1c", "7z"),
    (0, b"\x1f\x8b", "gzip"),
    (0, b"BZh", "bzip2"),
    (0, b"\xfd7zXZ\x00", "xz"),
    (0, b"MSCF", "cab"),
    (0, b"MSWIM\x00\x00\x00", "wim"),
    (0, b"WLPWM\x00\x00\x00", "wim"),
    (257, b"ustar", "tar"),                # tar 的 magic 在 257 偏移
)
_ISO_OFFSET = 0x8001  # ISO9660 卷描述符位置
_ISO_MAGIC = b"CD001"
_ISO_NAME = "iso"

# 本质是 zip 但属于应用文档/安装包，不应作为压缩包展开
COMPOUND_ZIP_EXTS = frozenset({
    ".docx", ".docm", ".xlsx", ".xlsm", ".xltx", ".xltm",
    ".pptx", ".pptm", ".potx", ".potm",
    ".odt", ".ods", ".odp", ".epub",
    ".jar", ".apk", ".ipa", ".xapk", ".vsix", ".whl",
    ".appx", ".msix",
})

_READ_SIZE = 4096

VIDEO_EXTS = frozenset({
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg",
})

# 已知"绝不可能是压缩包"的常见扩展名：命中即直接判否，**不读文件头**。
#
# 为什么需要：解压出的素材包里图片/视频动辄几万个，而嗅探对每个扩展名未命中
# 的文件都要 open + read(4096)。实测 3 万个小文件目录，扫描耗时从 2.74s
# 涨到 7.78s（+184%），其中绝大部分花在注定不是压缩包的文件上。
#
# 为什么这是安全的：这里只收"约定俗成不可能是压缩包"的类型。刻意**不收**
# .dat / .bin / .img / .exe 和视频后缀这类可能的伪装载体。
_NOT_ARCHIVE_EXTS = frozenset({
    # 图片
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff",
    ".ico", ".svg", ".avif", ".heic", ".psd",
    # 音频
    ".mp3", ".flac", ".wav", ".aac", ".ogg", ".m4a", ".wma", ".ape",
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


def volume_group_total(path: Path) -> tuple[int, int]:
    """整组分卷的 (总字节数, 卷数)；非分卷返回 (该文件自身大小, 1)。

    为什么不能用 `path.stat().st_size`：分卷包的"压缩包大小"指的是**整组**，
    而任务里存的 `archive_path` 只是首卷。实测一个 4 卷 7z：
    首卷 32 KB、整组 124 KB —— 界面把 3 MB 的解压后内容配上 32 KB 的包体，
    而 `compression_ratio` 的分母正是这个数，于是**压缩比被高估 3.8 倍**
    （真值 25:1 报成 96:1）。这不只是显示不准：默认 1000:1 的阈值下，
    一个分卷数够多的正常包会被**误判成 zip bomb 并被拒解**。

    一次目录遍历同时得出"总量"与"卷数"，两个事实来自同一份列表，
    界面就不可能出现"4 个分卷"配着 3 卷的大小。

    同名不同组不会混：分组标识含扩展名，`a.part01.rar` 与 `a.rar.001` 是两组。
    读不到的项跳过；一个都没统计到时退回自身大小——宁可少报，不要报 0。
    """
    try:
        own = path.stat().st_size
    except OSError:
        own = 0
    vi = volume_info(path)
    if vi is None:
        return own, 1
    base = vi[0]
    total = 0
    count = 0
    try:
        entries = list(path.parent.iterdir())
    except OSError:
        return own, 1
    for p in entries:
        other = volume_info(p)
        if other is None or other[0] != base:
            continue
        try:
            if not p.is_file():
                continue
            total += p.stat().st_size
            count += 1
        except OSError:
            continue
    if count == 0:
        return own, 1
    return total, count


def _sniff(path: Path) -> tuple[int, str]:
    """读文件头识别压缩包，返回 (文件大小, 类型名)；识别不出时类型名为 ""。

    统一入口：looks_like_archive 只关心"是不是"，sniff_format 只关心"是什么"，
    两者共用同一份读取与匹配逻辑，就不会出现判定漂移。文件读不到/太小一律
    (大小, "")，绝不抛异常——探测兜底跑的正是「已经出错」的分支。
    """
    try:
        size = path.stat().st_size
    except OSError:
        return 0, ""
    if size < 8:
        return size, ""
    try:
        with path.open("rb") as f:
            head = f.read(_READ_SIZE)
    except OSError:
        return size, ""
    for offset, magic, name in _MAGIC_HEADERS:
        end = offset + len(magic)
        if len(head) >= end and head[offset:end] == magic:
            return size, name
    if size >= _ISO_OFFSET + 5:
        try:
            with path.open("rb") as f:
                f.seek(_ISO_OFFSET)
                if f.read(5) == _ISO_MAGIC:
                    return size, _ISO_NAME
        except OSError:
            pass
    return size, ""


def sniff_format(path: Path) -> str:
    """读文件头给出压缩包类型名（zip/7z/rar/gzip/...）；识别不出返回 ""。

    存在意义：头部加密的包（`-mhe=on` 的 7z、加密文件名的 rar）用空密码连目录
    都列不出来，`7z l -slt` 里根本没有 `Type = ` 一行，`ArchiveInfo.format_name`
    就是空的。而界面此刻恰恰要回答"这是什么包"——读 4KB 文件头就能补上。

    与 looks_like_archive 共用 magic 表与扩展名短路规则，理由见 _sniff。
    """
    if path.suffix.lower() in VIDEO_EXTS:
        return "zip" if find_embedded_zip(path) else _sniff(path)[1]
    if path.suffix.lower() in _NOT_ARCHIVE_EXTS:
        return ""
    return _sniff(path)[1] or ("zip" if find_embedded_zip(path) else "")


def looks_like_archive(path: Path) -> bool:
    """读取文件头 magic bytes 判断是否压缩包；无法读取/太小一律按否处理。"""
    if path.suffix.lower() in VIDEO_EXTS:
        return find_embedded_zip(path) is not None or bool(_sniff(path)[1])
    if path.suffix.lower() in _NOT_ARCHIVE_EXTS:
        # 已知非压缩类型：不碰磁盘。见 _NOT_ARCHIVE_EXTS 的说明。
        return False
    return _sniff(path)[1] != "" or find_embedded_zip(path) is not None
