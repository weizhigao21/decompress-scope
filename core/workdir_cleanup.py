"""隔离工作目录的残留盘点与安全清理。

为什么需要这个模块：工作目录此前**没有任何清理机制**。三类东西会在那里无限堆积——

1. 交付失败留下的整份副本（交付失败过去只往 warnings 里塞一句话就 continue）；
2. 跨卷交付时 copytree 之后刻意留着当安全网的副本（同卷走 rename 是搬走，跨卷是复制）；
3. 运行中途被杀 / 收尾没跑完留下的整层产物。

它们全都是"解压成功"的状态，所以库里 done、界面显示「完成」，用户没有任何入口
知道磁盘被吃了多少，也没有入口清掉。实测 `task_83` 一次就留下 5.38 GB。

判定原则：**只把"已经确认交付过"的目录算作可清理**。产物还留在工作目录、
任务没结束、库里查不到记录的，一律标成保留并写明原因——那可能是用户唯一的副本。
清理永远由调用方显式触发，本模块不做任何自动删除。
"""
from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path

from .models import TaskStatus

# 只认自己建出来的目录名。工作目录理论上是我们独占的，但用户完全可能把
# 压缩包直接拖到这儿；用白名单认领比"假设这个目录下都是我的"安全得多。
_TASK_DIR_RE = re.compile(r"^task_(\d+)$")

# 可清理：产物已确认交付到工作目录之外，这里这份是副本
CLEANABLE = "cleanable"
# 没交付：产物就这一个副本，删了就真没了
NOT_DELIVERED = "not_delivered"
# 没结束：任务不是 DONE（待密码/失败/迁移中）
UNFINISHED = "unfinished"
# 孤儿：库里有不认识的目录（换过库、库被清空过）
ORPHAN = "orphan"

_KIND_LABELS = {
    CLEANABLE: "可清理",
    NOT_DELIVERED: "请保留",
    UNFINISHED: "请保留",
    ORPHAN: "待确认",
}


@dataclass(frozen=True)
class Leftover:
    """工作目录里的一个 `task_<id>` 残留目录。"""

    path: Path
    task_id: int | None
    kind: str
    reason: str
    size: int
    files: int
    mtime: float

    @property
    def safe(self) -> bool:
        """是否属于「产物已另有一份、这份纯属冗余」——可以默认勾选清理。"""
        return self.kind == CLEANABLE

    @property
    def label(self) -> str:
        return _KIND_LABELS.get(self.kind, self.kind)


def _task_id_of(path: Path) -> int:
    m = _TASK_DIR_RE.match(path.name)
    return int(m.group(1)) if m else -1


def is_link_like(path: Path) -> bool:
    """符号链接 / junction / 其它重解析点——凡是"指向别处"的目录都算。

    **必须查 reparse 标志位，不能只用 os.path.islink**：Windows 上 junction
    被 islink 判为 False（实测），但 junction 同样指向另一个目录，rmtree 的
    后果与符号链接一样是越界删除。查不出来时按 True 处理——删不动只是麻烦，
    删错了是灾难。
    """
    try:
        if os.path.islink(path):
            return True
        st = os.lstat(path)
    except OSError:
        return True
    attrs = getattr(st, "st_file_attributes", 0)
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def iter_task_dirs(workdir) -> list[Path]:
    """工作目录下的直属 `task_<id>` 目录（廉价：只读一层，不递归）。

    主界面要常驻显示残留数量，这个调用每次刷新都会跑，所以绝不能在这里递归
    统计大小——一个几十万文件的目录能把界面卡住好几秒。
    """
    root = Path(workdir)
    try:
        entries = list(root.iterdir())
    except OSError:
        return []
    out: list[Path] = []
    for p in entries:
        try:
            if p.is_dir() and _TASK_DIR_RE.match(p.name):
                out.append(p)
        except OSError:
            continue
    # 按任务号数值排序，而不是按名字字符串——字符串序会把 task_12 排在 task_7
    # 前面，列表看起来像是乱的。
    return sorted(out, key=_task_id_of)


def _dir_size(path: Path, count_limit: int = 500_000) -> tuple[int, int]:
    """递归统计 (字节数, 文件数)。跳过链接/junction，避免跟着它们数到别的卷上去。"""
    total = 0
    files = 0
    for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
        for name in filenames:
            fp = os.path.join(dirpath, name)
            try:
                total += os.path.getsize(fp)
            except OSError:
                continue
            files += 1
            if files >= count_limit:
                return total, files
        dirnames[:] = [d for d in dirnames
                       if not is_link_like(Path(dirpath) / d)]
    return total, files


def _inside(path: Path, root: Path) -> bool:
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def classify(path: Path, task, workdir: Path) -> tuple[str, str]:
    """给一个残留目录定性，返回 (kind, 给人看的理由)。

    顺序即优先级：越是"删了就找不回来"的判定越靠前。
    """
    if task is None:
        return ORPHAN, "数据库里没有这条任务记录，无法判断产物是否已交付"
    if task.status != TaskStatus.DONE:
        return UNFINISHED, f"任务状态为「{task.status.value}」，没有成功结束"
    if task.delivery_error:
        return NOT_DELIVERED, f"交付失败：{task.delivery_error}"
    if task.extracted_dir and _inside(Path(task.extracted_dir), workdir):
        return NOT_DELIVERED, "库中记录的产物位置仍指向工作目录，可能还没搬出去"
    if task.extracted_dir:
        return CLEANABLE, f"产物已交付到 {task.extracted_dir}，这份是工作目录里的副本"
    return ORPHAN, "任务未记录产物位置，无法判断是否已交付"


def scan_workdir(workdir, store, count_limit: int = 500_000) -> list[Leftover]:
    """盘点工作目录残留。store 可为 None（则所有目录都按孤儿处理）。"""
    root = Path(workdir)
    out: list[Leftover] = []
    for d in iter_task_dirs(root):
        m = _TASK_DIR_RE.match(d.name)
        task_id = int(m.group(1)) if m else None
        task = None
        if store is not None and task_id is not None:
            try:
                task = store.get(task_id)
            except Exception:
                task = None
        kind, reason = classify(d, task, root)
        size, files = _dir_size(d, count_limit)
        try:
            mtime = d.stat().st_mtime
        except OSError:
            mtime = 0.0
        out.append(Leftover(path=d, task_id=task_id, kind=kind, reason=reason,
                            size=size, files=files, mtime=mtime))
    return out


def _force_remove(func, path, _exc_info) -> None:
    """rmtree 的错误回调：只读文件在 Windows 上会让 unlink 直接 WinError 5。"""
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        pass


def prune(leftovers, workdir, allow_unsafe: bool = False) -> tuple[int, list[str]]:
    """删除给定的残留目录，返回 (释放的字节数, 失败原因列表)。

    三道闸，任何一道不过就跳过该目录（不删、报原因）：

    1. 必须是 `workdir` 的**直属** `task_<数字>` 子目录——`resolve()` 后比对父目录，
       挡掉 `..`、绝对路径、以及指向别处的链接；名字不匹配的一律不碰，用户自己
       放进工作目录的东西不在我们的删除范围内。
    2. 目录本身不能是符号链接/junction —— rmtree 会顺着它删到目标目录去，
       那是彻底的越界删除（且 Windows 上 junction 逃得过 os.path.islink）。
    3. 默认只删 `safe` 的项。要删「请保留 / 待确认」的必须显式 `allow_unsafe=True`
       （CLI 走 --all，界面走二次确认），因为那可能是唯一副本。
    """
    root = Path(workdir).resolve()
    freed = 0
    errors: list[str] = []
    for lo in leftovers:
        path = Path(lo.path)
        if not allow_unsafe and not lo.safe:
            errors.append(f"跳过（{lo.label}）：{path}")
            continue
        # 先判存在性再判链接：路径已经没了的时候 is_link_like 也会返回 True
        # （lstat 失败按"不敢删"处理），先报"已不存在"才是准确的诊断。
        if not path.is_dir():
            errors.append(f"跳过（已不存在）：{path}")
            continue
        if is_link_like(path):
            errors.append(f"跳过（是链接/junction，删除会越界）：{path}")
            continue
        try:
            if path.resolve().parent != root:
                errors.append(f"跳过（不是工作目录的直属子目录）：{path}")
                continue
        except OSError as exc:
            errors.append(f"跳过（路径无法解析）：{path} — {exc}")
            continue
        if not _TASK_DIR_RE.match(path.name):
            errors.append(f"跳过（目录名不是 task_<id>）：{path}")
            continue
        size = lo.size if lo.size else _dir_size(path)[0]
        try:
            shutil.rmtree(path, onerror=_force_remove)
        except OSError as exc:
            errors.append(f"删除失败：{path} — {exc}")
            continue
        freed += size
    return freed, errors
