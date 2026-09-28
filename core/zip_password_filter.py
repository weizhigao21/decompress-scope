"""ZIP 密码候选的快速排除器；通过预筛仍须由 7-Zip 完整解压验证。

格式依据：PKWARE APPNOTE 6.1 与 WinZip AES specification。
只缓存一个加密条目的校验头，不读取或解压正文；非 ASCII 密码、未知加密、
分卷或不一致的头部交回 7-Zip，避免编码与格式差异导致误排除。
"""
from __future__ import annotations

import hashlib
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path


def _crc_table() -> tuple[int, ...]:
    table = []
    for value in range(256):
        for _ in range(8):
            value = (value >> 1) ^ (0xEDB88320 if value & 1 else 0)
        table.append(value)
    return tuple(table)


_CRC = _crc_table()
_LOCAL = struct.Struct("<4s5H3I2H")


def _zipcrypto_last_byte(password: bytes, header: bytes) -> int:
    key0, key1, key2 = 0x12345678, 0x23456789, 0x34567890
    for char in password:
        key0 = (key0 >> 8) ^ _CRC[(key0 ^ char) & 255]
        key1 = ((key1 + (key0 & 255)) * 134775813 + 1) & 0xFFFFFFFF
        key2 = (key2 >> 8) ^ _CRC[(key2 ^ (key1 >> 24)) & 255]
    plain = 0
    for char in header:
        temp = key2 | 2
        plain = char ^ ((temp * (temp ^ 1) >> 8) & 255)
        key0 = (key0 >> 8) ^ _CRC[(key0 ^ plain) & 255]
        key1 = ((key1 + (key0 & 255)) * 134775813 + 1) & 0xFFFFFFFF
        key2 = (key2 >> 8) ^ _CRC[(key2 ^ (key1 >> 24)) & 255]
    return plain


@dataclass(frozen=True)
class ZipPasswordFilter:
    data: bytes             # ZipCrypto 的 12 字节头 / AES 的盐
    verifier: bytes
    key_size: int = 0        # 0 = ZipCrypto；其余是 AES 密钥字节数

    def may_match(self, password: str) -> bool:
        """False = 可排除；True = 需要真实解压，绝不是成功判据。"""
        if not password.isascii():
            return True
        encoded = password.encode("ascii")
        if not self.key_size:
            return _zipcrypto_last_byte(encoded, self.data) == self.verifier[0]
        try:
            derived = hashlib.pbkdf2_hmac(
                "sha1", encoded, self.data, 1000, dklen=2 * self.key_size + 2,
            )
        except (ValueError, AttributeError):
            return True  # 当前运行环境不提供该算法时仍交给 7-Zip
        return derived[-2:] == self.verifier


def _extra_fields(extra: bytes) -> dict[int, bytes]:
    fields = {}
    pos = 0
    while pos < len(extra):
        tag, size = struct.unpack_from("<HH", extra, pos)
        pos += 4
        if pos + size > len(extra) or tag in fields:
            raise ValueError("不一致的 ZIP 附加字段")
        fields[tag] = extra[pos:pos + size]
        pos += size
    return fields


def make_zip_password_filter(path: Path) -> ZipPasswordFilter | None:
    """只在可完整确认、未分卷的 ZIP 上使用预筛；不支持时返回 None。"""
    try:
        with path.open("rb") as stream:
            if stream.read(4) != b"PK\x03\x04":
                return None
            with zipfile.ZipFile(stream) as archive:
                entry = next((e for e in archive.infolist()
                              if e.flag_bits & 1 and not e.is_dir()), None)
                if entry is None or entry.volume or entry.flag_bits & (0x40 | 0x2000):
                    return None
                stream.seek(entry.header_offset)
                raw = stream.read(_LOCAL.size)
                sig, version, flags, method, dos_time, date, crc, packed, size, nlen, xlen = (
                    _LOCAL.unpack(raw)
                )
                if (sig != b"PK\x03\x04" or flags != entry.flag_bits
                        or method != entry.compress_type):
                    return None
                name = stream.read(nlen)
                extra = stream.read(xlen)
                if len(name) != nlen or len(extra) != xlen:
                    return None
                local_fields = _extra_fields(extra)
                central_fields = _extra_fields(entry.extra)
                if 0x0017 in local_fields or 0x0017 in central_fields:
                    return None  # PKWARE Strong Encryption
                if method == 99:
                    aes = local_fields.get(0x9901, b"")
                    if len(aes) < 7 or aes != central_fields.get(0x9901):
                        return None
                    aes_version, vendor, strength, compression = struct.unpack_from("<H2sBH", aes)
                    if aes_version not in (1, 2) or vendor != b"AE" or strength not in (1, 2, 3):
                        return None
                    key_size = (16, 24, 32)[strength - 1]
                    salt_size = key_size // 2
                    if entry.compress_size < salt_size + 2 + 10:
                        return None
                    data = stream.read(salt_size + 2)
                    if len(data) != salt_size + 2:
                        return None
                    return ZipPasswordFilter(data[:-2], data[-2:], key_size)
                if 0x9901 in local_fields or 0x9901 in central_fields or entry.compress_size < 12:
                    return None
                # 流式写入（bit 3）用 DOS 时间高字节；普通条目用 CRC 高字节。
                if flags & 8:
                    hour, minute, second = entry.date_time[3:]
                    central_time = (hour << 11) | (minute << 5) | (second // 2)
                    if dos_time != central_time:
                        return None
                    check = dos_time >> 8
                else:
                    if crc != entry.CRC:
                        return None
                    check = crc >> 24
                data = stream.read(12)
                if len(data) == 12:
                    return ZipPasswordFilter(data, bytes([check]))
    except (OSError, zipfile.BadZipFile, ValueError, EOFError, struct.error, NotImplementedError):
        pass
    return None
