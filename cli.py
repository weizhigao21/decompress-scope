"""解压开镜 CLI。

用法:
    python cli.py extract <目录或文件...> [--source 域名] [--max-depth 5]
                          [--samedir | --workdir] [--subdir 名字]
                          [--no-delete-intermediate] [--delete-original]
    python cli.py pass-add <密码> [--source 域名]
    python cli.py pass-list
    python cli.py config                # 打印当前配置
    python cli.py config --set key=val  # 改配置（可多次）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.appconfig import OUTPUT_SAMEDIR, OUTPUT_WORKDIR, AppConfig
from core.config import Config
from core.pipeline import Pipeline, RunReport
from core.store import TaskStore
from core.vault import PasswordVault

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = PROJECT_ROOT / "data" / "jieya.db"
CONFIG_PATH = PROJECT_ROOT / "config.json"



def _init_streams() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass


def _print_report(report: RunReport, cfg: Config) -> None:
    print()
    print("========== 结果 ==========")
    print(f"成功 {report.done} | 失败 {report.failed} | 待密码 {report.needs_password}")
    if report.output_dirs:
        print("-- 解压结果目录 --")
        for d in report.output_dirs:
            print(f"  {d}")
    if report.needs_password_tasks:
        print("-- 待补密码（pass-add 后重新 extract 即可）--")
        for t in report.needs_password_tasks:
            print(f"  #{t.id} {t.archive_path}")
    if report.warnings:
        print("-- 警告 --")
        for w in report.warnings:
            print(f"  {w}")
    print(f"工作目录: {cfg.workdir}")
    print(f"密码库: {DEFAULT_DB}")


def _cmd_config(args) -> int:
    """查看/修改配置。--set 支持 key=value，key 必须是 AppConfig 已有字段。"""
    cfg = AppConfig.ensure(CONFIG_PATH)
    if not args.set:
        import json

        print(f"配置文件: {CONFIG_PATH}")
        print(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    from dataclasses import fields

    valid = {f.name for f in fields(AppConfig)}
    for pair in args.set:
        if "=" not in pair:
            print(f"[错误] 需写成 key=value: {pair}")
            return 2
        key, _, raw = pair.partition("=")
        key = key.strip()
        if key not in valid:
            print(f"[错误] 未知配置项: {key}")
            print(f"       可用项: {', '.join(sorted(valid))}")
            return 2
        data = cfg.to_dict()
        default = getattr(AppConfig(), key)
        try:
            if isinstance(default, bool):
                low = raw.strip().lower()
                if low not in ("true", "false", "1", "0", "yes", "no", "on", "off"):
                    print(f"[错误] {key} 需为布尔值，收到: {raw}")
                    return 2
                data[key] = low in ("true", "1", "yes", "on")
            elif isinstance(default, int):
                data[key] = int(raw)
            elif isinstance(default, float):
                data[key] = float(raw)
            else:
                data[key] = raw
        except ValueError:
            print(f"[错误] {key} 的类型不正确: {raw}")
            return 2
        cfg = AppConfig.from_dict(data)

    try:
        cfg.save(CONFIG_PATH)
    except OSError as exc:
        print(f"[错误] 写入配置失败: {exc}")
        return 2
    print(f"已更新: {CONFIG_PATH}")
    for pair in args.set:
        k = pair.partition("=")[0].strip()
        print(f"  {k} = {getattr(cfg, k)!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    _init_streams()
    parser = argparse.ArgumentParser(prog="jieya", description="解压开镜 — 多层压缩包自动处理")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_ext = sub.add_parser("extract", help="递归解压多层压缩包")
    p_ext.add_argument("paths", nargs="+", help="压缩包文件或目录")
    p_ext.add_argument("--source", default="", help="手动指定密码来源(通常为站点域名)")
    p_ext.add_argument("--workdir", default=None, help="隔离工作目录(默认 项目/.workspace)")
    p_ext.add_argument("--sevenzip", default=None, help="7z.exe 路径(默认自动探测)")
    p_ext.add_argument("--max-depth", type=int, default=None, help="递归深度上限(默认 5)")
    p_ext.add_argument("--max-total-gb", type=float, default=None,
                       help="解压后总大小上限(GB，默认 50)")
    p_ext.add_argument("--max-ratio", type=float, default=None,
                       help="压缩比上限(默认 1000:1)")
    p_ext.add_argument("--no-delete-intermediate", action="store_true", help="保留中间层压缩包")
    p_ext.add_argument("--delete-original", action="store_true", help="成功后删除原始输入(默认保留)")
    p_ext.add_argument("--no-sniff", action="store_true",
                       help="关闭文件头嗅探(仅按扩展名识别压缩包)")
    p_ext.add_argument("--samedir", action="store_true",
                       help="解压到压缩包所在目录的子目录(默认遵循 config.json)")
    p_ext.add_argument("--workdir-only", action="store_true",
                       help="仅解到隔离工作目录，不复制回源目录")
    p_ext.add_argument("--subdir", default=None,
                       help="samedir 模式下的容器目录名(默认 _解压开镜)")
    p_ext.add_argument("--no-config", action="store_true",
                       help="忽略 config.json，全部使用内置默认值")

    p_add = sub.add_parser("pass-add", help="手工添加密码")
    p_add.add_argument("password")
    p_add.add_argument("--source", default="", help="关联来源(域名)")

    sub.add_parser("pass-list", help="查看密码库")

    p_cfg = sub.add_parser("config", help="查看/修改配置(config.json)")
    p_cfg.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                       help="修改配置项，可重复：--set max_depth=6 --set output_mode=samedir")

    sub.add_parser("gui", help="启动图形界面(需要 PySide6)")

    args = parser.parse_args(argv)

    if args.cmd == "gui":
        from ui.app import launch

        return launch()

    if args.cmd == "config":
        return _cmd_config(args)

    if args.cmd in ("pass-add", "pass-list"):
        vault = PasswordVault(DEFAULT_DB)
        if args.cmd == "pass-add":
            vault.add_manual(args.password, args.source)
            print(f"已添加: {args.password} (来源: {args.source or '无'})")
        else:
            rows = vault.list_all()
            if not rows:
                print("密码库为空")
            else:
                print(f"共 {len(rows)} 条:")
                for pwd, src, hits, last in rows:
                    print(f"  [{src or '无来源'}] {pwd}  命中 {hits}  最近 {last or '-'}")
        vault.close()
        return 0

    # 配置来源：默认读 config.json，--no-config 时用内置默认值
    pref = AppConfig() if args.no_config else AppConfig.ensure(CONFIG_PATH)

    overrides: dict = pref.as_overrides(PROJECT_ROOT)
    if args.workdir is not None:
        overrides["workdir"] = Path(args.workdir).resolve()
    if args.max_depth is not None:
        overrides["max_depth"] = args.max_depth
    if getattr(args, "max_total_gb", None) is not None:
        if args.max_total_gb <= 0:
            print("[错误] --max-total-gb 必须为正数")
            return 2
        overrides["max_total_uncompressed"] = int(args.max_total_gb * (1024 ** 3))
    if getattr(args, "max_ratio", None) is not None:
        if args.max_ratio <= 0:
            print("[错误] --max-ratio 必须为正数")
            return 2
        overrides["max_compression_ratio"] = args.max_ratio
    if args.no_delete_intermediate:
        overrides["delete_intermediate"] = False
    if args.delete_original:
        overrides["keep_original"] = False
    if getattr(args, "no_sniff", False):
        overrides["sniff_archives"] = False

    # 输出模式：命令行开关 > 配置
    mode = pref.output_mode
    if args.samedir:
        mode = OUTPUT_SAMEDIR
    if args.workdir_only:
        mode = OUTPUT_WORKDIR
    subdir = args.subdir if args.subdir is not None else pref.subdir_name
    # --workdir-only 的语义是"纯隔离，源目录一尘不染"，所以要关掉复制回源
    copy_back = pref.copy_back_to_source and not args.workdir_only

    try:
        cfg = Config.create(sevenzip=args.sevenzip, **overrides)
    except FileNotFoundError as exc:
        print(f"[错误] {exc}")
        return 2

    vault = PasswordVault(DEFAULT_DB)
    store = TaskStore(DEFAULT_DB)
    pipe = Pipeline(cfg, vault, store)
    pipe.output_mode = mode
    pipe.subdir_name = subdir
    pipe.copy_back = copy_back
    report = pipe.run([Path(p) for p in args.paths], source=args.source)
    _print_report(report, cfg)
    return 0 if report.failed == 0 and report.needs_password == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
