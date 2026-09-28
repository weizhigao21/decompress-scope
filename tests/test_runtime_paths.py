"""冻结 exe 时用户数据必须离开只读安装目录。"""
from pathlib import Path

import pytest

from core import runtime_paths


def _missing_home():
    raise RuntimeError("Could not determine home directory.")


def test_source_mode_uses_source_root(monkeypatch, tmp_path):
    monkeypatch.delattr(runtime_paths.sys, "frozen", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(_missing_home))
    assert runtime_paths.runtime_data_root(tmp_path) == tmp_path.resolve()


def test_frozen_mode_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert runtime_paths.runtime_data_root(Path("C:/ignored")) == tmp_path / "local" / "解压开镜"


def test_frozen_mode_with_localappdata_does_not_require_home(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setattr(Path, "home", staticmethod(_missing_home))
    assert runtime_paths.runtime_data_root(Path("C:/ignored")) == tmp_path / "local" / "解压开镜"


@pytest.mark.parametrize("local_appdata", [None, ""])
def test_frozen_mode_uses_windows_directory_without_home(monkeypatch, tmp_path, local_appdata):
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    if local_appdata is None:
        monkeypatch.delenv("LOCALAPPDATA", raising=False)
    else:
        monkeypatch.setenv("LOCALAPPDATA", local_appdata)
    monkeypatch.setattr(Path, "home", staticmethod(_missing_home))
    monkeypatch.setattr(runtime_paths, "_windows_local_appdata", lambda: tmp_path / "local")
    assert runtime_paths.runtime_data_root(Path("C:/ignored")) == tmp_path / "local" / "解压开镜"


def test_frozen_mode_falls_back_to_home_if_system_query_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(runtime_paths, "_windows_local_appdata", lambda: None)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))
    assert runtime_paths.runtime_data_root(Path("C:/ignored")) == tmp_path / "AppData" / "Local" / "解压开镜"


def test_missing_data_directory_explains_environment_fix(monkeypatch):
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.setattr(runtime_paths, "_windows_local_appdata", lambda: None)
    monkeypatch.setattr(Path, "home", staticmethod(_missing_home))
    with pytest.raises(RuntimeError, match="LOCALAPPDATA"):
        runtime_paths.runtime_data_root(Path("C:/ignored"))


@pytest.mark.skipif(runtime_paths.os.name != "nt", reason="需要 Windows 系统目录 API")
def test_real_windows_directory_query_without_environment_or_home(monkeypatch):
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("USERPROFILE", raising=False)
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)
    monkeypatch.setattr(Path, "home", staticmethod(_missing_home))
    root = runtime_paths.runtime_data_root(Path("C:/ignored"))
    assert root.name == "解压开镜"
    assert root.parent.is_absolute()
    assert root.parent.is_dir()
