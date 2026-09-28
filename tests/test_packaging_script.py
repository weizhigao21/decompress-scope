"""打包脚本必须可被 Windows CMD 稳定解析。"""
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_packaging_script_is_ascii_and_targets_gui_entry():
    """CMD 在括号块内预读 UTF-8 中文时会误解析；脚本保持 ASCII。"""
    text = (PROJECT_ROOT / "打包.bat").read_text(encoding="utf-8")
    assert text.isascii()
    assert "main.py" in text
    assert "--windowed" in text
    assert "--noupx" in text
    assert "--onefile" in text
    assert "--onedir" not in text
    assert 'rmdir /s /q "build\\%APP_NAME%"' in text
    assert "--icon \"ui\\assets\\app.ico\"" in text
    assert "--hidden-import ui.quick_extract_window" in text
    assert "--add-data \"ui\\assets;ui\\assets\"" in text


def test_packaging_script_has_no_python_tuple_comparison_inside_cmd_blocks():
    """`(3, 10)` 和 `>=` 会分别破坏 CMD 的块边界与重定向解析。"""
    text = (PROJECT_ROOT / "打包.bat").read_text(encoding="utf-8")
    assert "version_info >=" not in text
    assert "version_info >= (" not in text
