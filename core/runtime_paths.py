"""源码运行与冻结 exe 运行共用的可写数据目录。"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def runtime_data_root(source_root: Path, app_name: str = "解压开镜") -> Path:
    """返回应用可写根目录。

    源码模式沿用项目根目录，方便开发时查看 config.json、data/ 和 .workspace；
    PyInstaller 冻结后则使用 ``%LOCALAPPDATA%``，避免把用户数据写进临时
    解包目录或安装目录（后者通常没有写权限）。
    """
    if getattr(sys, "frozen", False):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / app_name
    return Path(source_root).resolve()
