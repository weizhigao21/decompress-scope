"""Windows Explorer「解压开镜」右键菜单的每用户注册。

只写 HKCU\\Software\\Classes，不要求管理员权限，也不会修改文件关联。Explorer
会把选中的文件路径作为 ``%1`` 传给 quick 启动模式；目录同样支持，方便一次处理
下载目录里的多个压缩包。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

MENU_NAME = "JieyaKaijing"
MENU_LABEL = "解压开镜"
ARCHIVE_EXTENSIONS: tuple[str, ...] = (
    ".zip", ".rar", ".7z", ".001", ".tar", ".gz", ".bz2", ".xz", ".cab", ".iso",
)


def menu_key_paths() -> tuple[str, ...]:
    """当前版本拥有的注册表项：所有文件与文件夹各一项。"""
    return (
        rf"Software\Classes\*\shell\{MENU_NAME}",
        rf"Software\Classes\Directory\shell\{MENU_NAME}",
    )


def _legacy_menu_key_paths() -> tuple[str, ...]:
    """旧版只按扩展名注册的项；仅用于升级和卸载时清理。"""
    return tuple(
        rf"Software\Classes\SystemFileAssociations\{ext}\shell\{MENU_NAME}"
        for ext in ARCHIVE_EXTENSIONS
    )


def _winreg():
    try:
        import winreg
    except ImportError as exc:  # pragma: no cover - 仅 Windows 才会实际调用
        raise OSError("右键菜单仅支持 Windows") from exc
    return winreg


def _notify_shell_changed() -> None:
    """通知 Explorer 刷新右键菜单和图标缓存。"""
    if os.name == "nt":
        import ctypes

        ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)


def quick_command(project_root: Path) -> str:
    """生成 Explorer 命令；源码与 PyInstaller 打包后的 exe 都可用。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" quick "%1"'
    interpreter = Path(sys.executable)
    pythonw = interpreter.with_name("pythonw.exe")
    launcher = pythonw if os.name == "nt" and pythonw.is_file() else interpreter
    return f'"{launcher}" "{Path(project_root) / "cli.py"}" quick "%1"'


def menu_icon(project_root: Path) -> str:
    """右键菜单图标：打包后取 exe 内嵌图标，源码运行取项目 ico。"""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}",0'
    return f'"{Path(project_root) / "ui" / "assets" / "app.ico"}"'


def install_context_menu(command: str, icon: str) -> None:
    """安装所有文件/目录的右键菜单。``command`` 必须已包含 ``%1``。"""
    reg = _winreg()
    for path in menu_key_paths():
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, path, 0, reg.KEY_WRITE) as key:
            reg.SetValueEx(key, "MUIVerb", 0, reg.REG_SZ, MENU_LABEL)
            reg.SetValueEx(key, "Icon", 0, reg.REG_SZ, icon)
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, path + r"\command", 0, reg.KEY_WRITE) as key:
            reg.SetValueEx(key, "", 0, reg.REG_SZ, command)
    # 更新已有用户：不留下旧版那组“仅已知扩展名”的重复菜单。
    for path in _legacy_menu_key_paths():
        _delete_tree(reg, reg.HKEY_CURRENT_USER, path)
    _notify_shell_changed()


def _delete_tree(reg, root, path: str) -> None:
    """删除明确属于本功能的一棵注册表树。"""
    try:
        with reg.OpenKey(root, path, 0, reg.KEY_READ | reg.KEY_WRITE) as key:
            while True:
                try:
                    child = reg.EnumKey(key, 0)
                except OSError:
                    break
                _delete_tree(reg, root, path + "\\" + child)
        reg.DeleteKey(root, path)
    except FileNotFoundError:
        return


def uninstall_context_menu() -> None:
    """仅移除本模块创建的菜单项；其他程序的右键项不受影响。"""
    reg = _winreg()
    for path in (*menu_key_paths(), *_legacy_menu_key_paths()):
        _delete_tree(reg, reg.HKEY_CURRENT_USER, path)
    _notify_shell_changed()


def is_context_menu_installed() -> bool:
    """所有文件和文件夹均已注册时才返回 True。"""
    try:
        reg = _winreg()
        for path in menu_key_paths():
            with reg.OpenKey(reg.HKEY_CURRENT_USER, path + r"\command", 0, reg.KEY_READ):
                pass
    except OSError:
        return False
    return True
