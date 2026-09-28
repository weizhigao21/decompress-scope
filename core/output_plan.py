"""每任务输出目录决策：把「解压到哪」从 Pipeline 里抽成纯函数，便于单测。

两种模式（与 core.appconfig.OUTPUT_* 对应）：
- OUTPUT_WORKDIR：所有产物落到隔离工作目录 workdir/task_<id>/out（默认旧行为）。
  成功且无嵌套的内层任务，额外把结果同名复制回源压缩包所在目录（尽力而为，
  失败只记警告；源目录不可写、已存在同名、空间不足都不应让解压任务失败）。
- OUTPUT_SAMEDIR：每个压缩包解到它自己旁边的 <包名>/ 目录。`subdir_name` 留空
  （默认）时基准就是压缩包所在目录，即 `<源目录>/<包名>/`；填了容器名才多一层
  `<源目录>/<容器名>/<包名>/`。省掉跨盘复制，也符合「解压到压缩包目录」的直觉。

嵌套的内层压缩包一律留在 workdir 内展开（它们是中间产物，不属于最终交付），
最终落点由最外层任务决定；它们的**包名会保留成一级目录名**，见
Pipeline._deliver_hierarchy。
"""
from __future__ import annotations

import os
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
    overwrite: bool = False                    # 交付时允许合并覆盖 final_dir 中的同名项
    warnings: list[str] = field(default_factory=list)


def _safe_component(name: str) -> str:
    """兼容旧名：samedir 的目录名消毒，实际实现见 appconfig.sanitize_component。"""
    return sanitize_component(name)


def archive_dir_name(archive) -> str:
    """压缩包路径 → 单层目录名（已消毒）。

    内层包与最外层包都用它命名产物目录，所以必须是同一个函数——两处各写一份
    迟早会漂移出两种叫法。

    绝不能直接把 stem 当目录名：`...zip` 的 stem 正好是 `".."`，拼进源目录后
    路径 normalize 回来就是**源目录本身**，产物会被平铺进用户的下载目录。
    与盘符逃逸同族——都是把外部字符串直接当路径成分。
    """
    return sanitize_component(Path(archive).stem)


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


def same_volume(a: Path, b: Path) -> bool:
    """两个路径是否位于同一卷——同卷才能用 rename 代替逐文件复制。

    Windows 上 `os.stat().st_dev` 是卷序列号，跨盘可可靠区分。stat 失败
    （路径不存在、无权限、网络盘抖动）一律按「不同卷」处理：退回复制只是
    慢一点，而误判成同卷去 rename 会直接抛错、任务失败。
    """
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def plan_output(    task,
    cfg,
    mode: str,
    subdir_name: str = "",
    force_new: bool = False,
    copy_back: bool = True,
) -> OutputPlan:
    """按模式给出该任务的输出方案。纯函数：只看参数与目标目录是否存在。

    不变式：**`plan.out_dir.name` 恒等于该包的目录名**（见 archive_dir_name）。
    每个任务的产物都先落在以自己包名命名的目录里，交付时整目录搬到父产物的
    对应位置。内层包的包名因此在最终产物里留下一级目录，而不是被拍平丢掉。

    - 嵌套任务（depth > 0）恒在 workdir 内展开：它们是中间产物，不属于最终交付。
    - samedir 模式 + 最外层：直接解到 <源目录>/<包名>/。
    - workdir 模式 + 最外层 + copy_back：解到 workdir/task_<id>/out/<包名>，
      成功后复制回 <源目录>/<包名>/（复制失败只警告，任务仍算成功）。
    - workdir 模式 + 最外层 + 不 copy_back：纯隔离，源目录不动；工作目录里的
      产物同样带着包名，用户去翻工作目录时也能认出哪个目录是哪个包。

    `subdir_name` 留空（默认）= **不要容器层**，产物直接落在压缩包所在目录；填了
    才多一层 `<源目录>/<容器名>/`。空值必须在这里短路，不能让它落到
    sanitize_component 的 "output" 兜底上——那会在用户的下载目录里凭空建一个
    output 目录。`appconfig.normalize()` 对空值的处理与这里保持同一套语义。
    """
    task_dir = Path(cfg.workdir) / f"task_{task.id}"
    archive = Path(task.archive_path)
    out_dir = task_dir / "out" / archive_dir_name(archive)
    # 交付侧的基准目录：有容器名才多一层，留空则就是压缩包所在目录
    container = sanitize_component(subdir_name) if (subdir_name or "").strip() else ""
    anchor = archive.parent / container if container else archive.parent

    if mode != OUTPUT_SAMEDIR or task.depth > 0:
        target = None
        if task.depth == 0 and mode == OUTPUT_WORKDIR and copy_back:
            target = anchor / archive_dir_name(archive)
        return OutputPlan(out_dir=out_dir, final_dir=target, copy_back=target is not None)

    # 覆盖模式也必须先解到工作目录。直接往用户已有目录写入后，密码错误、取消
    # 或 7z 报错时无法区分“本次写入”与用户原有文件，清理现场会误删原文件。
    # 成功后才把 staging 树交付到目标目录，保留覆盖同名项的既有语义。
    base = anchor / archive_dir_name(archive)
    if force_new:
        final = unique_path(base)
        return OutputPlan(out_dir=final, final_dir=final)
    if cfg.overwrite_existing:
        return OutputPlan(
            out_dir=out_dir,
            final_dir=base,
            copy_back=True,
            overwrite=True,
        )
    if not cfg.overwrite_existing:
        final = unique_path(base)
    else:
        final = base
    return OutputPlan(out_dir=final, final_dir=final)
