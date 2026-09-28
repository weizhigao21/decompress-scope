"""解压开镜 CLI。

用法:
    python cli.py extract <目录或文件...> [--source 域名] [--max-depth 5]
                          [--samedir | --workdir] [--subdir 名字]
                          [--no-delete-intermediate] [--delete-original]
                          [--force] [--timeout 秒]
    python cli.py pass-add <密码> [--source 域名]
    python cli.py pass-list
    python cli.py clean-workdir [--path 工作目录] [--yes] [--all]
    python cli.py config                # 打印当前配置
    python cli.py config --set key=val  # 改配置（可多次）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from core.appconfig import OUTPUT_SAMEDIR, OUTPUT_WORKDIR, AppConfig
from core.config import Config
from core.formatting import human_size
from core.pipeline import Pipeline, RunReport
from core.runtime_paths import runtime_data_root
from core.store import TaskStore
from core.vault import PasswordVault
from core.workdir_cleanup import prune, scan_workdir

PROJECT_ROOT = runtime_data_root(Path(__file__).resolve().parent)
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
    line = f"成功 {report.done} | 失败 {report.failed} | 待密码 {report.needs_password}"
    if report.skipped:
        line += f" | 已跳过 {report.skipped}"
    if report.delivery_failed:
        line += f" | 未交付 {report.delivery_failed}"
    print(line)
    if report.delivery_failed_tasks:
        # 单独成段而不是混进「警告」：解压成功但产物没到手上，是用户最容易
        # 误判为"已经好了"的情况，必须给出明确的下一步。
        print("-- 解压成功但产物未交付（仍在工作目录）--")
        for t in report.delivery_failed_tasks:
            print(f"  #{t.id} {t.archive_path}")
            print(f"      {t.delivery_error}")
        print(f"  产物仍在: {cfg.workdir}")
        print("  处理：修好目标位置后重跑，或用 clean-workdir 查看/清理")
    if report.skipped_tasks:
        print("-- 已跳过（此前已成功解压）--")
        for t in report.skipped_tasks:
            print(f"  {t.archive_path}  ->  {t.extracted_dir}")
        print("  如需重新解压，加 --force")
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
    print(f"解压超时: {cfg.extract_timeout:.0f}s/包")


def _cmd_clean_workdir(args) -> int:
    """盘点 / 清理隔离工作目录残留。默认只盘点，删除必须显式 --yes。

    为什么要单独一条命令：工作目录里的东西全是"解压成功"的状态，库里 done、
    界面显示「完成」——用户没有任何入口知道磁盘被吃了多少。这里把每一份残留
    的定性（可清理 / 请保留）和理由都摊开，删不删由用户决定。
    """
    pref = AppConfig.ensure(CONFIG_PATH)
    # 走 resolve_workdir 而不是直接用 pref.workdir：后者可能是空串或相对路径
    # （"空 = 项目/.workspace"），直接 Path() 出来会得到当前目录 `.` ——
    # 于是盘点的是调用者的 cwd，报"没有残留"，而 5GB 就在项目目录里躺着。
    workdir = (Path(args.path).expanduser() if args.path
               else pref.resolve_workdir(PROJECT_ROOT))
    if not workdir.is_dir():
        print(f"[错误] 工作目录不存在: {workdir}")
        return 2

    store = TaskStore(DEFAULT_DB)
    try:
        leftovers = scan_workdir(workdir, store)
    finally:
        store.close()

    print(f"工作目录: {workdir}")
    if not leftovers:
        print("没有残留。")
        return 0

    total = sum(lo.size for lo in leftovers)
    safe_items = [lo for lo in leftovers if lo.safe]
    print(f"共 {len(leftovers)} 项，合计 {human_size(total)}"
          f"（其中可安全清理 {len(safe_items)} 项 / {human_size(sum(x.size for x in safe_items))}）")
    print()
    for lo in leftovers:
        mark = "✔" if lo.safe else "·"
        print(f"  {mark} {lo.path.name}  [{lo.label}]  {human_size(lo.size)}  {lo.files} 文件")
        print(f"      {lo.reason}")

    if not args.yes:
        print()
        print("以上仅为盘点，未删除任何文件。")
        print(f"清理已交付的副本：      python cli.py clean-workdir --yes")
        print(f"连「请保留/待确认」也清：python cli.py clean-workdir --yes --all"
              f"   ← 有丢失唯一副本的风险")
        return 0

    targets = leftovers if args.all else safe_items
    if not targets:
        print()
        print("没有可安全清理的项；要强清请加 --all（可能删掉唯一副本）。")
        return 0
    if args.all:
        unsafe = [lo for lo in targets if not lo.safe]
        if unsafe:
            print()
            print(f"【警告】--all 将删除 {len(unsafe)} 项「请保留 / 待确认」的目录，"
                  f"它们可能是这些产物的唯一副本。")

    freed, errors = prune(targets, workdir, allow_unsafe=args.all)
    print()
    print(f"已清理 {len(targets) - len(errors)} 项，释放 {human_size(freed)}")
    for e in errors:
        print(f"  {e}")
    return 1 if errors else 0


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


def _cmd_shell_install() -> int:
    try:
        from core.shell_menu import install_context_menu, menu_icon, quick_command

        install_context_menu(quick_command(PROJECT_ROOT), menu_icon(PROJECT_ROOT))
    except OSError as exc:
        print(f"[错误] 安装右键菜单失败: {exc}")
        return 2
    print("已安装右键菜单：在压缩包或文件夹上右键可见「解压开镜」。")
    return 0


def _cmd_shell_uninstall() -> int:
    try:
        from core.shell_menu import uninstall_context_menu

        uninstall_context_menu()
    except OSError as exc:
        print(f"[错误] 移除右键菜单失败: {exc}")
        return 2
    print("已移除「解压开镜」右键菜单。")
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
                       help="产物外的容器目录名；默认不建容器，产物直接落在压缩包所在目录")
    p_ext.add_argument("--no-config", action="store_true",
                       help="忽略 config.json，全部使用内置默认值")
    p_ext.add_argument("--force", action="store_true",
                       help="已成功解压过的包也重新解压（关闭幂等跳过）")
    p_ext.add_argument("--timeout", type=float, default=None,
                       help="单个压缩包的解压超时秒数(默认 3600，超大包可调大)")

    p_add = sub.add_parser("pass-add", help="手工添加密码")
    p_add.add_argument("password")
    p_add.add_argument("--source", default="", help="关联来源(域名)")

    sub.add_parser("pass-list", help="查看密码库")

    p_cfg = sub.add_parser("config", help="查看/修改配置(config.json)")
    p_cfg.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                       help="修改配置项，可重复：--set max_depth=6 --set output_mode=samedir")

    p_clean = sub.add_parser("clean-workdir", help="盘点/清理隔离工作目录里的残留产物")
    p_clean.add_argument("--path", default=None, help="工作目录(默认取 config.json)")
    p_clean.add_argument("--yes", action="store_true", help="真的删除（不加则只盘点）")
    p_clean.add_argument("--all", action="store_true",
                         help="连「请保留/待确认」的目录一起删 —— 可能删掉唯一副本")

    sub.add_parser("gui", help="启动图形界面(需要 PySide6)")

    p_quick = sub.add_parser("quick", help=argparse.SUPPRESS)
    p_quick.add_argument("path", help="Explorer 右键菜单传入的文件或目录")
    sub.add_parser("shell-install", help="安装当前用户的「解压开镜」右键菜单")
    sub.add_parser("shell-uninstall", help="移除当前用户的「解压开镜」右键菜单")

    args = parser.parse_args(argv)

    if args.cmd == "gui":
        from ui.app import launch

        return launch()

    if args.cmd == "quick":
        from ui.app import launch_quick

        return launch_quick([args.path])

    if args.cmd == "shell-install":
        return _cmd_shell_install()

    if args.cmd == "shell-uninstall":
        return _cmd_shell_uninstall()

    if args.cmd == "clean-workdir":
        return _cmd_clean_workdir(args)

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
    if getattr(args, "force", False):
        overrides["skip_done"] = False
    if getattr(args, "timeout", None) is not None:
        if args.timeout <= 0:
            print("[错误] --timeout 必须为正数")
            return 2
        overrides["extract_timeout"] = float(args.timeout)

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
    # 交付失败也算「这一轮没完全成功」：产物确实解出来了，但用户手上没拿到。
    # 退 0 会让脚本编排以为一切正常，正是"库里 done、界面完成、什么都没有"的脚本版。
    if report.failed or report.needs_password or report.delivery_failed:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
