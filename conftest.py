"""测试全局配置。

关键：MainWindow / SettingsWindow 会读写项目根目录的 config.json。
若不隔离，跑一次测试就会改写开发者本机的真实偏好（我们已被咬过一次——
UI 冒烟测试把 recent_inputs 清空了）。这里用 autouse fixture 把 CONFIG_PATH
指到临时目录，保证测试永不触碰真实配置文件。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """把所有 UI 模块里的 CONFIG_PATH / PROJECT_ROOT 重定向到临时目录。"""
    try:
        from ui import main_window
    except ImportError:
        return  # 未安装 PySide6 时不干预

    monkeypatch.setattr(main_window, "CONFIG_PATH", tmp_path / "config.json", raising=False)
    monkeypatch.setattr(main_window, "PROJECT_ROOT", tmp_path, raising=False)
    yield


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """主窗口的 DEFAULT_DB 同样指向临时库，避免测试写入真实密码库。"""
    try:
        from ui import main_window
    except ImportError:
        return

    monkeypatch.setattr(main_window, "DEFAULT_DB", tmp_path / "test.db", raising=False)
    yield
