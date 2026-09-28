"""源码运行与冻结 exe 运行共用的可写数据目录。"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _windows_local_appdata() -> Path | None:
    """通过系统查询用户目录，不依赖 Explorer 传入的环境变量。"""
    if os.name != "nt":
        return None
    import ctypes

    try:
        get_folder_path = ctypes.WinDLL("shell32").SHGetFolderPathW
        get_folder_path.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_wchar_p,
        ]
        get_folder_path.restype = ctypes.c_long
        buffer = ctypes.create_unicode_buffer(260)  # MAX_PATH
        # CSIDL_LOCAL_APPDATA，读取当前用户的实际目录（包括重定向）。
        if get_folder_path(None, 0x001C, None, 0, buffer) == 0 and buffer.value:
            return Path(buffer.value)
    except (AttributeError, OSError):
        pass
    return None


def runtime_data_root(source_root: Path, app_name: str = "解压开镜") -> Path:
    """返回应用可写根目录。

    源码模式沿用项目根目录，方便开发时查看 config.json、data/ 和 .workspace；
    PyInstaller 冻结后则使用 ``%LOCALAPPDATA%``，缺失时查询 Windows；
    避免把用户数据写进临时解包目录或安装目录（后者通常没有写权限）。
    """
    if getattr(sys, "frozen", False):
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            return Path(local_appdata) / app_name
        base = _windows_local_appdata()
        if base is None:
            try:
                base = Path.home() / "AppData" / "Local"
            except RuntimeError as exc:
                raise RuntimeError("无法确定用户数据目录，请设置 LOCALAPPDATA 环境变量。") from exc
        return base / app_name
    return Path(source_root).resolve()
