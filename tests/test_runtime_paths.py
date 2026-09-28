"""冻结 exe 时用户数据必须离开只读安装目录。"""
from pathlib import Path

from core import runtime_paths


def test_source_mode_uses_source_root(monkeypatch, tmp_path):
    monkeypatch.delattr(runtime_paths.sys, "frozen", raising=False)
    assert runtime_paths.runtime_data_root(tmp_path) == tmp_path.resolve()


def test_frozen_mode_uses_localappdata(monkeypatch, tmp_path):
    monkeypatch.setattr(runtime_paths.sys, "frozen", True, raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    assert runtime_paths.runtime_data_root(Path("C:/ignored")) == tmp_path / "local" / "解压开镜"
