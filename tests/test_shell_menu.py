"""Explorer 右键菜单的纯函数与 CLI 入口测试，不写真实注册表。"""
import cli
from core.shell_menu import (
    ARCHIVE_EXTENSIONS, MENU_NAME, _legacy_menu_key_paths,
    menu_icon, menu_key_paths, quick_command,
)


def test_menu_keys_cover_all_files_and_directories():
    keys = menu_key_paths()
    assert keys == (
        f"Software\\Classes\\*\\shell\\{MENU_NAME}",
        f"Software\\Classes\\Directory\\shell\\{MENU_NAME}",
    )


def test_legacy_extension_entries_remain_known_for_cleanup():
    """升级后可清除旧版留下的按扩展名菜单，避免右键重复显示。"""
    keys = _legacy_menu_key_paths()
    assert len(keys) == len(ARCHIVE_EXTENSIONS)
    assert all("SystemFileAssociations" in key for key in keys)


def test_quick_command_quotes_script_and_explorer_argument(monkeypatch, tmp_path):
    monkeypatch.setattr("core.shell_menu.sys.executable", str(tmp_path / "python.exe"))
    command = quick_command(tmp_path / "project root")
    assert '"' + str(tmp_path / "project root" / "cli.py") + '"' in command
    assert 'quick "%1"' in command


def test_menu_icon_uses_source_ico_or_frozen_exe(monkeypatch, tmp_path):
    monkeypatch.setattr("core.shell_menu.sys.frozen", False, raising=False)
    assert menu_icon(tmp_path) == f'"{tmp_path / "ui" / "assets" / "app.ico"}"'
    monkeypatch.setattr("core.shell_menu.sys.frozen", True)
    monkeypatch.setattr("core.shell_menu.sys.executable", str(tmp_path / "app.exe"))
    assert menu_icon(tmp_path) == f'"{tmp_path / "app.exe"}",0'


def test_shell_install_uses_quick_launcher(monkeypatch, capsys):
    captured = []
    monkeypatch.setattr("core.shell_menu.install_context_menu", lambda *args: captured.append(args))
    monkeypatch.setattr("core.shell_menu.quick_command", lambda root: '"launcher" quick "%1"')
    monkeypatch.setattr("core.shell_menu.menu_icon", lambda root: '"icon.ico"')

    assert cli.main(["shell-install"]) == 0
    assert captured == [('"launcher" quick "%1"', '"icon.ico"')]
    assert "已安装右键菜单" in capsys.readouterr().out


def test_quick_cli_forwards_selected_path(monkeypatch, tmp_path):
    received = []
    monkeypatch.setattr("ui.app.launch_quick", lambda paths: received.append(paths) or 0)

    assert cli.main(["quick", str(tmp_path / "包.zip")]) == 0
    assert received == [[str(tmp_path / "包.zip")]]
