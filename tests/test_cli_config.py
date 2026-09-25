"""CLI 配置命令与输出模式的端到端测试。

关键约定：CLI 会读写 <项目根>/config.json。测试必须先把 cli.CONFIG_PATH
重定向到 tmp_path，否则会污染开发者真实配置。
"""
import json
import subprocess
from pathlib import Path

import pytest

import cli
from core.appconfig import OUTPUT_SAMEDIR, OUTPUT_WORKDIR, AppConfig
from core.config import detect_sevenzip


@pytest.fixture
def isolated_cli(tmp_path, monkeypatch):
    """把 CLI 的 CONFIG_PATH / DEFAULT_DB 指到临时目录。"""
    monkeypatch.setattr(cli, "CONFIG_PATH", tmp_path / "config.json", raising=False)
    monkeypatch.setattr(cli, "DEFAULT_DB", tmp_path / "cli.db", raising=False)
    monkeypatch.setattr(cli, "PROJECT_ROOT", tmp_path, raising=False)
    return tmp_path


def test_config_prints_defaults(isolated_cli, capsys):
    """config 子命令：无参数时打印当前配置（首次运行会落盘默认值）。"""
    rc = cli.main(["config"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "配置文件" in out
    data = json.loads(out.split("\n", 1)[1])
    assert data["output_mode"] == OUTPUT_SAMEDIR
    assert (isolated_cli / "config.json").is_file()


def test_config_set_single_value(isolated_cli, capsys):
    """--set 修改单项并落盘。"""
    rc = cli.main(["config", "--set", "max_depth=7"])
    capsys.readouterr()
    assert rc == 0
    assert AppConfig.load(isolated_cli / "config.json").max_depth == 7


def test_config_set_multiple_values(isolated_cli, capsys):
    """--set 可重复，一次改多项。"""
    rc = cli.main(["config",
                   "--set", "max_depth=6",
                   "--set", "output_mode=workdir",
                   "--set", "subdir_name=unzipped"])
    capsys.readouterr()
    assert rc == 0
    cfg = AppConfig.load(isolated_cli / "config.json")
    assert cfg.max_depth == 6
    assert cfg.output_mode == OUTPUT_WORKDIR
    assert cfg.subdir_name == "unzipped"


def test_config_set_boolean_variants(isolated_cli, capsys):
    """布尔项接受 true/false/1/0/yes/no/on/off。"""
    for raw, expect in (("false", False), ("0", False), ("no", False),
                        ("true", True), ("1", True), ("yes", True)):
        rc = cli.main(["config", "--set", f"sniff_archives={raw}"])
        capsys.readouterr()
        assert rc == 0
        assert AppConfig.load(isolated_cli / "config.json").sniff_archives is expect


def test_config_set_out_of_range_clamped(isolated_cli, capsys):
    """越界值被收敛而非拒绝（与 GUI 行为一致）。"""
    rc = cli.main(["config", "--set", "max_depth=999"])
    capsys.readouterr()
    assert rc == 0
    assert AppConfig.load(isolated_cli / "config.json").max_depth == 10


def test_config_set_unknown_key_rejected(isolated_cli, capsys):
    """未知配置项：报错、列出可用项、返回码 2，且不写入该值。"""
    rc = cli.main(["config", "--set", "no_such_key=1"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "未知配置项" in out
    assert "max_depth" in out
    # ensure() 已落盘默认值（预期），关键是没写进那个非法键
    data = json.loads((isolated_cli / "config.json").read_text(encoding="utf-8"))
    assert "no_such_key" not in data
    assert data["max_depth"] == AppConfig().max_depth


def test_config_set_bad_type_rejected(isolated_cli, capsys):
    """类型不符：报错且不写入。"""
    rc = cli.main(["config", "--set", "max_depth=不是数字"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "类型不正确" in out
    data = json.loads((isolated_cli / "config.json").read_text(encoding="utf-8"))
    assert data["max_depth"] == AppConfig().max_depth

    rc2 = cli.main(["config", "--set", "sniff_archives=maybe"])
    capsys.readouterr()
    assert rc2 == 2


def test_config_set_missing_equals_rejected(isolated_cli, capsys):
    """缺等号：报错提示格式。"""
    rc = cli.main(["config", "--set", "max_depth"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "key=value" in out


def _sevenzip_or_skip() -> Path:
    try:
        return detect_sevenzip()
    except FileNotFoundError:
        pytest.skip("未找到 7z.exe，跳过集成测试")


def test_extract_respects_samedir_flag(isolated_cli, tmp_path, capsys):
    """--samedir：产物落到压缩包旁。"""
    exe = _sevenzip_or_skip()
    src = tmp_path / "dl"
    src.mkdir()
    (src / "a.txt").write_text("cli payload", encoding="utf-8")
    subprocess.run([str(exe), "a", "p.zip", "a.txt"], cwd=str(src), capture_output=True)
    (src / "a.txt").unlink()

    rc = cli.main(["extract", str(src), "--samedir",
                   "--workdir", str(tmp_path / "wd"), "--no-config"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert (src / "_解压开镜" / "p" / "a.txt").is_file()


def test_extract_workdir_only_does_not_copy_back(isolated_cli, tmp_path, capsys):
    """--workdir-only：纯隔离，源目录完全不动（不建容器目录、不复制）。"""
    exe = _sevenzip_or_skip()
    src = tmp_path / "dl"
    src.mkdir()
    (src / "a.txt").write_text("cli payload", encoding="utf-8")
    subprocess.run([str(exe), "a", "p.zip", "a.txt"], cwd=str(src), capture_output=True)
    (src / "a.txt").unlink()

    wd = tmp_path / "wd"
    rc = cli.main(["extract", str(src), "--workdir-only",
                   "--workdir", str(wd), "--no-config"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert list(wd.rglob("a.txt"))
    assert not (src / "_解压开镜").exists()
    # 源目录里只剩原来的压缩包
    assert sorted(p.name for p in src.iterdir()) == ["p.zip"]


def test_extract_workdir_mode_copies_back_by_default(isolated_cli, tmp_path, capsys):
    """不带 --samedir/--workdir-only 时遵循配置；workdir+copy_back 应复制回源目录。"""
    exe = _sevenzip_or_skip()
    cli.main(["config", "--set", "output_mode=workdir",
              "--set", "copy_back_to_source=true"])
    capsys.readouterr()

    src = tmp_path / "dl"
    src.mkdir()
    (src / "a.txt").write_text("payload", encoding="utf-8")
    subprocess.run([str(exe), "a", "p.zip", "a.txt"], cwd=str(src), capture_output=True)
    (src / "a.txt").unlink()

    rc = cli.main(["extract", str(src), "--workdir", str(tmp_path / "wd")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert (src / "_解压开镜" / "p" / "a.txt").is_file()


def test_extract_reads_output_mode_from_config(isolated_cli, tmp_path, capsys):
    """不带模式开关时，遵循 config.json 里的 output_mode。"""
    exe = _sevenzip_or_skip()
    cli.main(["config", "--set", "output_mode=samedir"])
    capsys.readouterr()

    src = tmp_path / "dl"
    src.mkdir()
    (src / "a.txt").write_text("payload", encoding="utf-8")
    subprocess.run([str(exe), "a", "p.zip", "a.txt"], cwd=str(src), capture_output=True)
    (src / "a.txt").unlink()

    # 注意：这里故意不加 --no-config，让 CLI 读 config.json
    rc = cli.main(["extract", str(src), "--workdir", str(tmp_path / "wd")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert (src / "_解压开镜" / "p" / "a.txt").is_file()


def test_extract_no_config_ignores_file(isolated_cli, tmp_path, capsys):
    """--no-config：config.json 里的 workdir 模式被忽略，回到内置默认。"""
    exe = _sevenzip_or_skip()
    cli.main(["config", "--set", "output_mode=workdir"])
    capsys.readouterr()

    src = tmp_path / "dl"
    src.mkdir()
    (src / "a.txt").write_text("payload", encoding="utf-8")
    subprocess.run([str(exe), "a", "p.zip", "a.txt"], cwd=str(src), capture_output=True)
    (src / "a.txt").unlink()

    rc = cli.main(["extract", str(src), "--no-config",
                   "--workdir", str(tmp_path / "wd")])
    out = capsys.readouterr().out
    assert rc == 0, out
    # 内置默认是 samedir
    assert (src / "_解压开镜" / "p" / "a.txt").is_file()


def test_extract_skips_done_on_rerun_and_force_overrides(isolated_cli, tmp_path, capsys):
    """CLI 端到端：重跑默认跳过已有产物，--force 则重新解压。

    只断言「--force 被 argparse 接受」是没用的——那只证明参数存在，没证明它
    接进了配置。这里用产物目录的实际变化来验证整条链路。
    """
    exe = _sevenzip_or_skip()
    src = tmp_path / "dl"
    src.mkdir()
    (src / "a.txt").write_text("cli payload", encoding="utf-8")
    subprocess.run([str(exe), "a", "p.zip", "a.txt"], cwd=str(src), capture_output=True)
    (src / "a.txt").unlink()

    args = ["extract", str(src), "--samedir", "--no-config",
            "--workdir", str(tmp_path / "wd"), "--sevenzip", str(exe)]

    rc1 = cli.main(args)
    out1 = capsys.readouterr().out
    assert rc1 == 0, out1
    assert (src / "_解压开镜" / "p" / "a.txt").is_file()

    rc2 = cli.main(args)
    out2 = capsys.readouterr().out
    assert rc2 == 0, out2
    assert "跳过" in out2, f"重跑未报告跳过：{out2}"
    assert not (src / "_解压开镜" / "p (2)").exists(), "重跑堆出了重复产物目录"

    rc3 = cli.main([*args, "--force"])
    out3 = capsys.readouterr().out
    assert rc3 == 0, out3
    assert (src / "_解压开镜" / "p (2)" / "a.txt").is_file(), f"--force 未重新解压：{out3}"


def test_extract_rejects_non_positive_timeout(isolated_cli, tmp_path, capsys):
    """--timeout 必须为正数；报错要走我们自己的校验文案。

    断言文案而不只是返回码：参数未接上时 argparse 也返回 2，只断 2 分不清
    「参数存在但值非法」与「参数根本不存在」。
    """
    rc = cli.main(["extract", str(tmp_path), "--timeout", "0",
                   "--workdir", str(tmp_path / "wd"), "--no-config"])
    out = capsys.readouterr().out
    assert rc == 2
    assert "必须为正数" in out, f"未命中自校验分支（参数可能没接上）：{out}"


def test_extract_timeout_reaches_config(isolated_cli, tmp_path, capsys):
    """--timeout 一路走到 Config 并反映在结果摘要里。"""
    exe = _sevenzip_or_skip()
    src = tmp_path / "dl"
    src.mkdir()
    (src / "a.txt").write_text("payload", encoding="utf-8")
    subprocess.run([str(exe), "a", "p.zip", "a.txt"], cwd=str(src), capture_output=True)
    (src / "a.txt").unlink()

    rc = cli.main(["extract", str(src), "--samedir", "--no-config",
                   "--workdir", str(tmp_path / "wd"), "--sevenzip", str(exe),
                   "--timeout", "600"])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "解压超时: 600s/包" in out, f"--timeout 未生效：{out}"


def test_config_set_extract_timeout_clamped(isolated_cli, capsys):
    """extract_timeout 可持久化，且被区间收敛（下限 30 秒）。"""
    rc = cli.main(["config", "--set", "extract_timeout=5"])
    capsys.readouterr()
    assert rc == 0
    assert AppConfig.load(isolated_cli / "config.json").extract_timeout == 30
