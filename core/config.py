"""配置项与 7z.exe 自动探测。"""
from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

DEFAULT_ARCHIVE_EXTS: tuple[str, ...] = (
    ".zip", ".rar", ".7z", ".cbz", ".cbr",
    ".tar", ".gz", ".bz2", ".xz", ".cab", ".iso", ".001",
)

_GIB = 1024 ** 3


def detect_sevenzip(explicit: str | None = None) -> Path:
    """定位 7z.exe：显式参数 > SEVENZIP_PATH 环境变量 > 常见安装路径 > PATH。"""
    if explicit:
        p = Path(explicit)
        if p.is_file():
            return p
        raise FileNotFoundError(f"指定的 7z 不存在: {p}")
    env = os.environ.get("SEVENZIP_PATH", "")
    if env and Path(env).is_file():
        return Path(env)
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for base in (pf, pf86):
        p = Path(base) / "7-Zip" / "7z.exe"
        if p.is_file():
            return p
    found = shutil.which("7z")
    if found:
        return Path(found)
    raise FileNotFoundError(
        "未找到 7z.exe：请安装 7-Zip，或设置环境变量 SEVENZIP_PATH，或传 --sevenzip"
    )


@dataclass
class Config:
    sevenzip_path: Path
    workdir: Path
    max_depth: int = 5
    max_total_uncompressed: int = 50 * _GIB  # 现实中大包很多, 默认放宽到 50 GiB
    max_compression_ratio: float = 1000.0
    ratio_floor_bytes: int = 1 * _GIB
    max_password_attempts: int = 20
    delete_intermediate: bool = True
    keep_original: bool = True
    archive_exts: tuple[str, ...] = DEFAULT_ARCHIVE_EXTS
    sniff_archives: bool = True  # 扩展名未命中时读文件头 magic 识别伪装压缩包
    # False 时同名输出目录自动避让为 "xxx (2)"，绝不覆盖既有产物
    overwrite_existing: bool = False
    list_timeout: float = 120.0
    extract_timeout: float = 3600.0

    @classmethod
    def create(cls, sevenzip: str | None = None, workdir: str | None = None, **overrides) -> "Config":
        default_workdir = Path(__file__).resolve().parent.parent / ".workspace"
        cfg = cls(
            sevenzip_path=detect_sevenzip(sevenzip),
            workdir=Path(workdir).resolve() if workdir else default_workdir,
        )
        for key, value in overrides.items():
            if not hasattr(cfg, key):
                raise KeyError(f"未知配置项: {key}")
            setattr(cfg, key, value)
        return cfg
