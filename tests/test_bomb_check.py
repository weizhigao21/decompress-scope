"""zip bomb 阈值单元测试（不依赖 7z）。核心场景：大而正常的包不能误判。"""
from pathlib import Path

from core.config import Config
from core.models import ArchiveInfo
from core.probe import check_bomb

GIB = 1024 ** 3


def _cfg() -> Config:
    return Config(sevenzip_path=Path("7z.exe"), workdir=Path("."))


def _info(total: int, archive_size: int) -> ArchiveInfo:
    return ArchiveInfo(path=Path("x.zip"), archive_size=archive_size,
                       total_uncompressed=total)


def test_large_but_legit_archive_passes():
    """5.4 GB 压缩包（压缩比约 1:1）不应被误判为 bomb。"""
    info = _info(total=int(5.4 * GIB), archive_size=int(5.3 * GIB))
    assert check_bomb(info, _cfg()) is None


def test_over_total_limit_rejected():
    info = _info(total=60 * GIB, archive_size=59 * GIB)
    reason = check_bomb(info, _cfg())
    assert reason is not None and "总大小" in reason


def test_bomb_ratio_rejected():
    """总量未超 50GiB 但压缩比 2000:1 → 按压缩比拒绝。"""
    info = _info(total=40 * GIB, archive_size=20 * 1024 * 1024)
    reason = check_bomb(info, _cfg())
    assert reason is not None and "压缩比" in reason


def test_small_high_ratio_not_flagged():
    """小文件高压缩比（总量未超过 1GiB 下限）不误报，如高度可压缩的文本。"""
    info = _info(total=500 * 1024 * 1024, archive_size=100 * 1024)
    assert check_bomb(info, _cfg()) is None


def test_custom_limit_respected():
    cfg = Config(sevenzip_path=Path("7z.exe"), workdir=Path("."),
                 max_total_uncompressed=4 * GIB)
    info = _info(total=int(5.4 * GIB), archive_size=int(5.3 * GIB))
    assert check_bomb(info, cfg) is not None
