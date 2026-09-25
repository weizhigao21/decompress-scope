"""探测引擎：解析 7z l -slt 元数据，识别加密状态与 zip bomb 风险。"""
from __future__ import annotations

import re
from pathlib import Path

from .config import Config
from .models import ArchiveEntry, ArchiveInfo
from .sevenzip import SevenZip

_SEP = re.compile(r"^-{5,}\s*$")
_TYPE = re.compile(r"^Type = (.+)$", re.M)

_WRONG_PW_MARKERS = ("wrong password", "错误的密码", "密码错误", "密码不正确")


class ProbeError(RuntimeError):
    pass


def _to_int(text: str | None) -> int:
    try:
        return int(text) if text else 0
    except ValueError:
        return 0


def _consume_entry(info: ArchiveInfo, entry: dict[str, str]) -> None:
    is_dir = entry.get("Folder", "") == "+"
    size = _to_int(entry.get("Size"))
    if is_dir:
        info.dir_count += 1
    else:
        info.file_count += 1
        info.total_uncompressed += size
    if entry.get("Encrypted", "").strip().lower() in ("+", "true", "yes"):
        info.flag_encrypted = True
    info.entries.append(ArchiveEntry(path=entry.get("Path", ""), size=size, is_dir=is_dir))


def parse_slt(text: str, archive_path: Path) -> ArchiveInfo:
    """解析 `7z l -slt` 的输出，只统计分隔线之后的条目块。"""
    info = ArchiveInfo(path=archive_path)
    m = _TYPE.search(text)
    if m:
        info.format_name = m.group(1).strip()
    try:
        info.archive_size = archive_path.stat().st_size
    except OSError:
        pass

    lines = text.splitlines()
    start = 0
    for i, line in enumerate(lines):
        if _SEP.match(line.strip()):
            start = i + 1
            break

    entry: dict[str, str] | None = None

    def flush() -> None:
        nonlocal entry
        if entry is not None:
            _consume_entry(info, entry)
            entry = None

    for line in lines[start:]:
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key == "Path":
            flush()
            entry = {"Path": value.strip()}
        elif entry is not None:
            entry[key] = value.strip()
    flush()
    return info


def probe(sz: SevenZip, archive: Path) -> ArchiveInfo:
    """空密码尝试列目录：成功→解析元数据；报密码错误→头加密；其他→坏包。"""
    code, out, err = sz.list_raw(archive, password="")
    if code == 0:
        return parse_slt(out, archive)
    text = out + err
    low = text.lower()
    if any(marker in low for marker in _WRONG_PW_MARKERS):
        info = ArchiveInfo(path=archive, needs_password_for_listing=True)
        try:
            info.archive_size = archive.stat().st_size
        except OSError:
            pass
        return info
    raise ProbeError(text.strip()[-400:] or f"7z 退出码 {code}")


def probe_with_password(sz: SevenZip, archive: Path, password: str) -> ArchiveInfo | None:
    """用已知密码复探加密包，取真实的解压后总大小（供 zip bomb 校验）。

    为什么需要：头部加密的包（`-mhe=on` 的 7z、加密文件名的 rar）在空密码下
    连目录都列不出来，`probe()` 只能返回 `needs_password_for_listing=True`，
    而 `total_uncompressed` 停在 0 —— 于是 `check_bomb` 的两个条件全都不触发，
    唯一的安全防线对加密包形同虚设。拿到正确密码后再探一次即可补齐这个盲区。

    实测：7z 头部加密包用**正确**密码执行 `l` 返回 exit=0 并列出完整条目，
    因此能拿到真实大小。（注意 zipcrypto 的 zip 本来就能空密码列目录，
    它的 `total_uncompressed` 由首次 probe 就正确，不走这条路径。）

    返回 None 表示即使给了密码也列不出目录（少见）；调用方自行决定是否放行，
    不要因为复探失败就一律拒绝，否则会把"能解但探不到"的包误杀。
    """
    code, out, err = sz.list_raw(archive, password=password)
    if code != 0:
        return None
    return parse_slt(out, archive)


def check_bomb(info: ArchiveInfo, cfg: Config) -> str | None:
    """zip bomb 检查，返回拒绝原因；None 表示通过。"""
    gib = 1024 ** 3
    if info.total_uncompressed > cfg.max_total_uncompressed:
        return (
            f"疑似 zip bomb：解压后总大小 {info.total_uncompressed / gib:.1f} GiB"
            f"超过上限 {cfg.max_total_uncompressed / gib:.1f} GiB（可调大小上限后重试）"
        )
    if (
        info.archive_size > 0
        and info.total_uncompressed > cfg.ratio_floor_bytes
        and info.compression_ratio > cfg.max_compression_ratio
    ):
        return (
            f"疑似 zip bomb：压缩比 {info.compression_ratio:.0f}:1"
            f"超过上限 {cfg.max_compression_ratio:.0f}:1（可调大压缩比上限后重试）"
        )
    return None


def classify_extract(exit_code: int, stdout: str, stderr: str) -> tuple[bool, bool, str]:
    """解压结果分类。返回 (成功?, 是否密码问题, 消息)。"""
    text = (stdout or "") + (stderr or "")
    if exit_code == 0:
        return True, False, ""
    low = text.lower()
    wrong_pw = any(marker in low for marker in _WRONG_PW_MARKERS)
    msg = text.strip()[-400:] or f"7z 退出码 {exit_code}"
    return False, wrong_pw, msg
