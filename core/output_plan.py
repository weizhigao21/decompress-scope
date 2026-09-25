"""每任务输出目录决策：把「解压到哪」从 Pipeline 里抽成纯函数，便于单测。

两种模式（与 core.appconfig.OUTPUT_* 对应）：
- OUTPUT_WORKDIR：所有产物落到隔离工作目录 workdir/task_<id>/out（默认旧行为）。
  成功且无嵌套的内层任务，额外把结果同名复制回源压缩包所在目录（尽力而为，
  失败只记警告；源目录不可写、已存在同名、空间不足都不应让解压任务失败）。
- OUTPUT_SAMEDIR：每个压缩包解到它自己旁边的子目录 <父目录>/<subdir>/<包名>/，
  省掉跨盘复制，也符合「解压到压缩包目录」的直觉。

嵌套的内层压缩包一律留在 workdir 内展开（它们是中间产物，不属于最终交付），
最终落点由最外层任务决定。
"""
from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from .appconfig import OUTPUT_SAMEDIR, OUTPUT_WORKDIR, sanitize_component


@dataclass
class OutputPlan:
    """一个任务解压后的落地决策。"""

    out_dir: Path                              # 7z 实际写入的目录
    final_dir: Path | None = None              # 最终交付目录；None = 仅 out_dir
    copy_back: bool = False                    # out_dir 完成后同名复制到 final_dir
    warnings: list[str] = field(default_factory=list)


def _safe_component(name: str) -> str:
    """兼容旧名：samedir 的目录名消毒，实际实现见 appconfig.sanitize_component。"""
    return sanitize_component(name)


def unique_path(base: Path) -> Path:
    """找不冲突的路径：已存在时插入序号。

    - 目录：`pack` → `pack (2)`
    - 文件：`data.txt` → `data (2).txt`（序号插在主名与扩展名之间）

    第二种是关键：若照搬目录的命名法会得到 `data.txt (2)`，扩展名不再是
    最后一个后缀，很多程序按后缀识别类型时会认不出。多段后缀（`.tar.gz`）
    只保留最后一个作为扩展名，因为多段后缀无法与"主名里的点"区分开。
    """
    if not base.exists():
        return base
    suffix = base.suffix
    stem = base.name[: -len(suffix)] if suffix else base.name
    for i in range(2, 1000):
        cand = base.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    # 极端兜底：用纳秒时间戳，避免无限循环
    import time

    stamp = time.time_ns()
    return base.with_name(f"{stem} ({stamp}){suffix}")


def plan_output(
    task,
    cfg,
    mode: str,
    subdir_name: str = "_解压开镜",
    force_new: bool = False,
    copy_back: bool = True,
) -> OutputPlan:
    """按模式给出该任务的输出方案。纯函数：只看参数与目标目录是否存在。

    - 嵌套任务（depth > 0）恒在 workdir 内展开：它们是中间产物，不属于最终交付。
    - samedir 模式 + 最外层：直接解到 <源目录>/<subdir>/<包名>/。
    - workdir 模式 + 最外层 + copy_back：解到 workdir/task_<id>/out，成功后
      复制回 <源目录>/<subdir>/<包名>/（复制失败只警告，任务仍算成功）。
    - workdir 模式 + 最外层 + 不 copy_back：纯隔离，源目录不动。
    """
    task_dir = Path(cfg.workdir) / f"task_{task.id}"
    out_dir = task_dir / "out"
    archive = Path(task.archive_path)

    if mode != OUTPUT_SAMEDIR or task.depth > 0:
        target = None
        if task.depth == 0 and mode == OUTPUT_WORKDIR and copy_back:
            target = archive.parent / sanitize_component(subdir_name) / archive.stem
        return OutputPlan(out_dir=out_dir, final_dir=target, copy_back=target is not None)

    # samedir 模式：直接在源目录旁解压
    parent = archive.parent / sanitize_component(subdir_name)
    base = parent / archive.stem
    if force_new or not cfg.overwrite_existing:
        final = unique_path(base)
    else:
        final = base
    return OutputPlan(out_dir=final, final_dir=final)
