"""BFS 多层解压编排引擎。

流程：扫描入队 → 探测(加密/bomb) → 逐密码尝试解压 → 内层扫描再入队 →
完成回写密码库 → 按输出模式把产物落到目标位置 → 清理工作目录内的中间层压缩包。
"""
from __future__ import annotations

import os
import shutil
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from .appconfig import OUTPUT_SAMEDIR, OUTPUT_WORKDIR
from .archive_detect import COMPOUND_ZIP_EXTS, looks_like_archive, volume_info
from .config import Config
from .models import AttemptOutcome, Task, TaskStatus
from .output_plan import OutputPlan, plan_output, same_volume, unique_path
from .password_finder import build_candidates, extract_source
from .probe import ProbeError, check_bomb, classify_extract, probe, probe_with_password
from .sevenzip import SevenZip, SevenZipCancelled
from .store import TaskStore
from .vault import PasswordVault


@dataclass
class RunReport:
    done: int = 0
    failed: int = 0
    needs_password: int = 0
    skipped: int = 0
    needs_password_tasks: list[Task] = field(default_factory=list)
    skipped_tasks: list[Task] = field(default_factory=list)
    output_dirs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, cfg: Config, vault: PasswordVault, store: TaskStore):
        self.cfg = cfg
        self.sz = SevenZip(cfg.sevenzip_path, cfg.list_timeout, cfg.extract_timeout)
        self.vault = vault
        self.store = store
        self._on_event = None  # 可选事件回调: on_event(dict)，供 UI 桥接
        self._should_cancel = None  # 可选取消探测: should_cancel() -> bool
        self._ask_password = None  # 可选问询回调: ask_password({"path": ...}) -> str | None
        # 输出落地策略（由 UI/CLI 注入；默认沿用旧的隔离工作目录行为）
        self.output_mode = OUTPUT_WORKDIR
        self.subdir_name = "_解压开镜"
        self.copy_back = True  # workdir 模式下是否复制一份回源目录
        self.skip_done = getattr(cfg, "skip_done", True)  # 重跑时跳过已成功解压的包
        self._plans: dict[int, OutputPlan] = {}
        cfg.workdir.mkdir(parents=True, exist_ok=True)


    def _emit(self, event: dict) -> None:
        cb = self._on_event
        if cb is None:
            return
        try:
            cb(event)
        except Exception:
            pass

    def _cancel_requested(self) -> bool:
        cb = self._should_cancel
        return bool(cb()) if cb else False

    def _looks_like_archive(self, path: Path) -> bool:
        """扩展名优先判定；未命中且开启嗅探时读文件头 magic 兜底。

        复合文档（docx/xlsx/apk 等本质是 zip 的应用文件）始终排除，
        即使文件头是 PK 也不展开。
        """
        suffix = path.suffix.lower()
        if suffix in COMPOUND_ZIP_EXTS:
            return False
        if suffix in self.cfg.archive_exts:
            return True
        if self.cfg.sniff_archives:
            return looks_like_archive(path)
        return False

    def _collapse_volumes(self, files: list[Path], report: RunReport) -> list[Path]:
        """把分卷序列收敛成"只解首卷"，并对缺首卷的组给出明确告警。

        为什么必须做：`a.part1.rar` 与 `a.part2.rar` 的后缀都是 `.rar`，
        扩展名判定会让它们各成一个任务；而非首卷单独解压必然失败 —— 用户会
        看到一条无法理解的 FAILED，还白白多跑一轮探测与解压尝试。

        只认序号 1 的首卷。缺首卷时整组跳过而不是硬着头皮解：与其入队一个
        注定失败的任务，不如直接告诉用户少了什么。7-Zip 风格的 `.001/.002`
        本来就只识别得到 `.001`（其余扩展名不命中且无 magic），因此天然安全，
        本函数对其只是无害通过。
        """
        groups: dict[tuple[str, str], list[tuple[int, Path]]] = {}
        for f in files:
            vi = volume_info(f)
            if vi is not None:
                groups.setdefault((str(f.parent), vi[0]), []).append((vi[1], f))
        if not groups:
            return files

        skip: set[Path] = set()
        for (parent, base), members in groups.items():
            members.sort(key=lambda m: m[0])
            first_index = members[0][0]
            if first_index != 1:
                msg = (
                    f"检测到分卷但缺少首卷，已跳过：{Path(parent) / base}"
                    f"（现有最小分卷序号 {first_index}，请补齐序号 1 的首卷）"
                )
                report.warnings.append(msg)
                self._emit({"kind": "warning", "message": msg})
                skip.update(p for _, p in members)
            else:
                # 只留首卷，其余分卷由 7z 自行按同目录顺序读取
                skip.update(p for _, p in members[1:])
        return [f for f in files if f not in skip]

    def _already_done(self, archive_path: str) -> Task | None:
        """该路径此前是否已成功解压**且产物仍在**——两个条件缺一不可。

        只查数据库记录是不够的：DONE 只是历史结论，不代表产物还躺在原地。
        用户手动删掉产物目录再重跑是常见操作（想重新来一遍），此时若仍判
        "已跳过"，程序会一边宣称跳过一边什么都不产出——静默失效，比重复解压
        糟糕得多。所以产物目录的存在性是判据的一部分。
        """
        prev = self.store.find_done(archive_path)
        if prev is None or not prev.extracted_dir:
            return None
        try:
            if Path(prev.extracted_dir).is_dir():
                return prev
        except OSError:
            return None
        return None

    # ---------- 主流程 ----------

    def run(
        self,
        inputs: list[Path],
        source: str = "",
        on_event=None,
        should_cancel=None,
        ask_password=None,
    ) -> RunReport:
        """可选回调（均为普通可调用对象，core 不依赖任何 UI 框架）:

        - on_event(dict): 事件流，kind ∈ {'task','status','progress','warning'}
        - should_cancel(): 返回 True 时在当前任务完成后停止处理剩余任务
        - ask_password({"path": ...}) -> str | None:
          密码候选全部失败时现场询问（如 UI 弹窗），返回密码则立即重试，
          返回 None/空 则该包进入 needs_password。

        输出落点由 self.output_mode / self.subdir_name 决定（见 core.output_plan）。
        """
        self._on_event = on_event
        self._should_cancel = should_cancel
        self._ask_password = ask_password
        self._plans.clear()
        report = RunReport()
        run_tasks: list[Task] = []
        queue: deque[Task] = deque()
        seen: set[str] = set()
        for raw in inputs:
            p = Path(raw)
            if p.is_dir():
                files = sorted(
                    f for f in p.rglob("*")
                    if f.is_file() and self._looks_like_archive(f)
                )
                files = self._collapse_volumes(files, report)
            elif p.is_file():
                files = self._collapse_volumes([p], report)
            else:
                msg = f"路径不存在: {p}"
                report.warnings.append(msg)
                self._emit({"kind": "warning", "message": msg})
                continue
            for f in files:
                key = str(f.resolve())
                if key in seen:
                    continue
                seen.add(key)
                if self.skip_done:
                    prev = self._already_done(key)
                    if prev is not None:
                        report.skipped += 1
                        report.skipped_tasks.append(prev)
                        msg = (
                            f"跳过已处理：{f.name}（产物仍在 {prev.extracted_dir}；"
                            f"如需重新解压请用 --force）"
                        )
                        report.warnings.append(msg)
                        self._emit({"kind": "warning", "message": msg})
                        continue
                task = Task(archive_path=key, depth=0, source=source or extract_source(f.name))
                task.id = self.store.create(task)
                run_tasks.append(task)
                queue.append(task)
                self._emit({"kind": "task", "task_id": task.id, "parent_id": None,
                            "depth": 0, "path": key})
        while queue:
            if self._cancel_requested():
                msg = "用户取消，剩余任务未处理"
                report.warnings.append(msg)
                self._emit({"kind": "warning", "message": msg})
                break
            task = queue.popleft()
            try:
                self._process(task, queue, run_tasks, report)
            except Exception as exc:  # noqa: BLE001 单任务异常不拖垮整轮
                task.error = f"处理异常: {exc.__class__.__name__}: {exc}"
                try:
                    self._set(task, TaskStatus.FAILED)
                except Exception:
                    pass
        self._cleanup(run_tasks, report)
        self._fill_report(report, run_tasks)
        return report

    # ---------- 单任务处理 ----------

    def _process(self, task: Task, queue: deque[Task], run_tasks: list[Task], report: RunReport) -> None:
        archive = Path(task.archive_path)
        self._set(task, TaskStatus.PROBING)
        try:
            info = probe(self.sz, archive)
        except ProbeError as exc:
            task.error = f"探测失败: {exc}"
            self._set(task, TaskStatus.FAILED)
            return
        bomb_reason = check_bomb(info, self.cfg)
        if bomb_reason:
            task.error = bomb_reason
            self._set(task, TaskStatus.FAILED)
            return

        vault_candidates = self.vault.candidates_for(task.source, limit=20)
        candidates = build_candidates(
            archive.name, task.source, vault_candidates
        )[: self.cfg.max_password_attempts]

        plan = self._plan_for(task)
        out_dir = plan.out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        self._set(task, TaskStatus.EXTRACTING)

        outcome = self._attempt_extract(archive, out_dir, candidates, task.id)
        if outcome.password is None:
            # 失败/待密码时清掉空壳目录，避免 samedir 模式在用户目录留下痕迹
            self._discard_plan(plan)
            if outcome.password_issue:
                task.error = outcome.message or "需要密码"
                self._set(task, TaskStatus.NEEDS_PASSWORD)
            else:
                task.error = f"解压失败: {outcome.message}"
                self._set(task, TaskStatus.FAILED)
            return

        # 加密包的 bomb 补检：头部加密的包在空密码下 probe 拿不到条目，
        # total_uncompressed 停在 0，前面的 check_bomb 等于没跑。现在密码已确定，
        # 用它对目录再探一次拿真实大小。必须在 record_success 之前——被判定为
        # bomb 的包不该把密码记为"成功命中"。
        if info.needs_password_for_listing and outcome.password:
            real = probe_with_password(self.sz, archive, outcome.password)
            if real is not None:
                bomb_reason = check_bomb(real, self.cfg)
                if bomb_reason:
                    shutil.rmtree(plan.out_dir, ignore_errors=True)
                    self._discard_plan(plan)
                    task.error = bomb_reason
                    msg = f"{archive.name}: {bomb_reason}"
                    report.warnings.append(msg)
                    self._emit({"kind": "warning", "message": msg})
                    self._set(task, TaskStatus.FAILED)
                    return

        task.password_used = outcome.password or None
        if outcome.password:
            self.vault.record_success(outcome.password, task.source)
        self._sweep_empty_dirs(plan)
        task.extracted_dir = str(plan.out_dir)
        self._enqueue_children(task, plan.out_dir, queue, run_tasks, report)
        self._set(task, TaskStatus.DONE)

    # ---------- 输出落地 ----------

    def _plan_for(self, task: Task) -> OutputPlan:
        """计算并缓存该任务的输出方案（同一任务只算一次，避免重试时换目录）。"""
        plan = self._plans.get(task.id)
        if plan is None:
            plan = plan_output(task, self.cfg, self.output_mode, self.subdir_name,
                               copy_back=self.copy_back)
            self._plans[task.id] = plan
        return plan

    def _sweep_empty_dirs(self, plan: OutputPlan) -> None:
        """7z -spd 遇到不带目录项的包会扁平落盘；若产生了顶层单目录，把内容提上来。"""
        out = plan.out_dir
        try:
            items = list(out.iterdir())
        except OSError:
            return
        if len(items) != 1 or not items[0].is_dir():
            return
        # 仅当外层不存在同名文件冲突时才上提（保持"解出来就是内容"的直觉）
        try:
            for child in items[0].iterdir():
                target = out / child.name
                if target.exists():
                    return
            for child in list(items[0].iterdir()):
                child.rename(out / child.name)
            items[0].rmdir()
        except OSError:
            pass

    def _deliver_hierarchy(self, run_tasks: list[Task], report: RunReport) -> None:
        """把嵌套任务的产物归并到最外层的交付目录。

        为什么需要：内层压缩包一律在 workdir 里展开（避免中间产物污染用户目录），
        所以最外层的交付目录里只会看到内层压缩包本身——而它随后会被
        delete_intermediate 删掉。用户最终拿到的目录就成了"少了几层内容"的残缺品。
        这里自底向上，把每个已成功任务 out_dir 里的非压缩包内容，合并进它父任务的
        out_dir，逐层上传，直到最外层。
        """
        by_id = {t.id: t for t in run_tasks}
        plans = self._plans
        # 按深度降序处理：先收最内层，再往上传，避免路径在处理途中被搬走
        for t in sorted(run_tasks, key=lambda x: x.depth, reverse=True):
            if t.status != TaskStatus.DONE or t.parent_id is None:
                continue
            child_plan = plans.get(t.id)
            parent_plan = plans.get(t.parent_id)
            if child_plan is None or parent_plan is None:
                continue
            if not child_plan.out_dir.is_dir():
                continue
            src_root = child_plan.out_dir.resolve()
            dst_root = parent_plan.out_dir
            try:
                dst_root.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                report.warnings.append(f"归并内层产物失败：{dst_root} — {exc}")
                continue

            for item in list(src_root.iterdir()):
                # 内层压缩包本身是中间产物，不搬走也不留给用户——直接删。
                # 它的内容已经由它自己的 out_dir 归并上来（本函数的自底向上遍历保证）。
                try:
                    if item.is_file() and self._looks_like_archive(item):
                        item.unlink(missing_ok=True)
                        continue
                except OSError:
                    continue
                target = dst_root / item.name
                try:
                    if target.exists():
                        target = unique_path(target)
                    shutil.move(str(item), str(target))
                except (OSError, shutil.Error) as exc:
                    msg = f"内层产物覆盖到外层失败：{item} → {target} — {exc}"
                    report.warnings.append(msg)
                    self._emit({"kind": "warning", "message": msg})

            # 内层产物搬空后，把工作目录里的壳收掉，别攒垃圾
            try:
                if src_root.is_dir() and not any(src_root.iterdir()):
                    src_root.rmdir()
                    inner_parent = src_root.parent
                    if inner_parent.is_dir() and not any(inner_parent.iterdir()):
                        inner_parent.rmdir()
            except OSError:
                pass

    def _prune_consumed_archives(self, run_tasks: list[Task], report: RunReport) -> None:
        """交付目录里删掉「已消化」的中间压缩包。

        场景：外层包解出 inner.zip，inner.zip 又被展开并把内容归并回外层交付目录。
        此时交付目录里那个 inner.zip 是已经消化完的中间产物，留着只会让用户
        再解一遍。

        匹配规则（不能用绝对路径相比——交付目录里的那份是复制出来的，路径不同）：
        某任务 T 的**子任务 C** 实际处理的压缩包，其相对 T 的 out_dir 的**相对路径**
        记为 `rel`；若 T 的 out_dir / final_dir 下**同一相对位置** `root/rel` 存在
        且确实是压缩包，那它就是 C 消化掉的那份，删之。

        为什么必须带相对路径、不能只用文件名：内层包可能藏在 T 产物的子目录里
        （`outer.zip` → `data/inner.zip`）。若按裸文件名 rglob 全目录匹配，
        用户自己放在 `data/keep/inner.zip` 的同名无关文件会被一起删掉——静默丢数据。
        """
        if not self.cfg.delete_intermediate:
            return
        for t in run_tasks:
            if t.status != TaskStatus.DONE:
                continue
            plan = self._plans.get(t.id)
            if plan is None:
                continue

            # 收集 T 名下被消化子包的「相对 T.out_dir 的路径」
            #
            # 锚点必须用 plan.out_dir，不能用 task.extracted_dir：后者是面向
            # 用户的"产物落点"，workdir 模式下交付后会改成源目录侧的路径，
            # 而子任务的 archive_path 记的是**工作目录**里的位置，两者一减
            # relative_to 直接抛 ValueError → rels 为空 → 中间包永远清不掉。
            anchor = plan.out_dir
            rels: set[str] = set()
            for child in run_tasks:
                if child.parent_id != t.id or child.status != TaskStatus.DONE:
                    continue
                try:
                    rel = Path(child.archive_path).resolve().relative_to(anchor.resolve())
                except (ValueError, OSError):
                    continue
                rels.add(str(rel).lower())
            if not rels:
                continue

            roots: list[Path] = []
            for cand in (plan.out_dir, plan.final_dir):
                if cand is not None and cand.is_dir() and cand not in roots:
                    roots.append(cand)
            for root in roots:
                for rel in rels:
                    p = root / rel
                    try:
                        if p.is_file():
                            p.unlink(missing_ok=True)
                    except OSError:
                        continue

    def _discard_plan(self, plan: OutputPlan) -> None:
        """任务未成功：清掉空壳目录，别在用户源目录留下痕迹。

        samedir 模式会连父级子目录一起收掉——若本轮所有任务都没成功，
        <源目录>/_解压开镜/ 不该剩下（用户会以为解压成功了）。
        只有当目录为空时才删，绝不碰用户已经放进去的文件。
        """
        if plan.final_dir is None:
            return
        try:
            if plan.final_dir.is_dir() and not any(plan.final_dir.iterdir()):
                plan.final_dir.rmdir()
            elif plan.out_dir.is_dir() and plan.out_dir != plan.final_dir:
                # workdir 模式下的 out_dir 也可以收掉
                if not any(plan.out_dir.iterdir()):
                    pass
        except OSError:
            return

        # 往上收空壳父目录：只收名为 subdir_name 的那一层，绝不越过用户源目录
        parent = plan.final_dir.parent
        try:
            if (parent.is_dir() and parent.name == self.subdir_name
                    and not any(parent.iterdir())):
                parent.rmdir()
        except OSError:
            pass


    def _run_extract(self, task_id: int, archive: Path, out_dir: Path,
                     pwd: str) -> tuple[int, str, str] | None:
        """执行一次解压；发进度事件；用户取消时清理现场并返回 None。"""
        try:
            return self.sz.extract(
                archive, out_dir, pwd,
                on_progress=lambda pct: self._emit(
                    {"kind": "progress", "task_id": task_id, "percent": pct}),
                should_cancel=self._cancel_requested,
            )
        except SevenZipCancelled:
            shutil.rmtree(out_dir, ignore_errors=True)
            return None

    def _attempt_extract(
        self, archive: Path, out_dir: Path, candidates: list[str], task_id: int
    ) -> AttemptOutcome:
        """空密码先试，再按候选顺序尝试；密码错误换下一个，其他失败保留现场。

        候选全部失败后，若注册了 ask_password 回调（UI 弹窗等），现场询问密码
        并立即重试，最多 3 次；用户放弃则进入 needs_password。
        """
        attempts = [""]
        seen: set[str] = {""}
        for cand in candidates:
            if cand and cand.lower() not in seen:
                seen.add(cand.lower())
                attempts.append(cand)
        last_msg = ""
        for pwd in attempts:
            result = self._run_extract(task_id, archive, out_dir, pwd)
            if result is None:
                return AttemptOutcome(message="用户取消", password_issue=False)
            code, out, err = result
            ok, wrong_pw, msg = classify_extract(code, out, err)
            if ok:
                return AttemptOutcome(password=pwd)
            last_msg = msg
            if not wrong_pw:
                return AttemptOutcome(message=last_msg, password_issue=False)
            shutil.rmtree(out_dir, ignore_errors=True)

        if self._ask_password is not None:
            for _ in range(3):
                manual = ""
                try:
                    manual = self._ask_password({"path": str(archive)}) or ""
                except Exception:
                    break
                manual = manual.strip()
                if not manual:
                    break
                if manual.lower() in seen:
                    continue
                seen.add(manual.lower())
                result = self._run_extract(task_id, archive, out_dir, manual)
                if result is None:
                    return AttemptOutcome(message="用户取消", password_issue=False)
                code, out, err = result
                ok, wrong_pw, msg = classify_extract(code, out, err)
                if ok:
                    return AttemptOutcome(password=manual)
                last_msg = msg
                if not wrong_pw:
                    return AttemptOutcome(message=last_msg, password_issue=False)
                shutil.rmtree(out_dir, ignore_errors=True)
        return AttemptOutcome(message=last_msg or "密码均不匹配", password_issue=True)

    def _enqueue_children(
        self, task: Task, out_dir: Path, queue: deque[Task], run_tasks: list[Task], report: RunReport
    ) -> None:
        inner = sorted(
            p for p in out_dir.rglob("*")
            if p.is_file() and self._looks_like_archive(p)
        )
        if not inner:
            return
        if task.depth >= self.cfg.max_depth:
            msg = f"任务 #{task.id} 已达最大深度 {self.cfg.max_depth}，{len(inner)} 个内层压缩包不再展开"
            report.warnings.append(msg)
            self._emit({"kind": "warning", "message": msg})
            return
        for p in inner:
            child = Task(
                archive_path=str(p),
                parent_id=task.id,
                depth=task.depth + 1,
                source=task.source or extract_source(p.name),
            )
            child.id = self.store.create(child)
            run_tasks.append(child)
            queue.append(child)
            self._emit({"kind": "task", "task_id": child.id, "parent_id": child.parent_id,
                        "depth": child.depth, "path": child.archive_path})

    # ---------- 收尾 ----------

    def _cleanup(self, run_tasks: list[Task], report: RunReport) -> None:
        """收尾：先按输出方案落地产物，再清理工作目录内的中间层压缩包。

        注意顺序——复制必须在删原件之前，否则 --delete-original 会把源目录复制
        的出发地删掉。
        """
        self._deliver_hierarchy(run_tasks, report)
        self._deliver(run_tasks, report)
        self._prune_consumed_archives(run_tasks, report)
        workdir = self.cfg.workdir.resolve()
        for t in run_tasks:
            if t.status != TaskStatus.DONE:
                continue
            archive = Path(t.archive_path)
            try:
                in_workdir = archive.resolve().is_relative_to(workdir)
            except OSError:
                in_workdir = False
            if in_workdir and self.cfg.delete_intermediate:
                archive.unlink(missing_ok=True)
            elif t.depth == 0 and not self.cfg.keep_original:
                archive.unlink(missing_ok=True)

    def _deliver(self, run_tasks: list[Task], report: RunReport) -> None:
        """workdir 模式下把最外层任务的产物交付回源压缩包所在目录。

        同卷走 rename（单个原子系统调用，省一半写入与磁盘），跨卷才逐文件复制。
        尽力而为：目标已存在则避让到「包名 (2)」，空间不足/无权限只记一条警告，
        不影响任务的成功状态。警告同时进 report 与事件流——CLI 只读 report，
        UI 只读事件流，两边都不能瞎。
        """
        for t in run_tasks:
            if t.status != TaskStatus.DONE or t.depth != 0:
                continue
            plan = self._plans.get(t.id)
            if plan is None or not plan.copy_back or plan.final_dir is None:
                continue
            src, dst = plan.out_dir, plan.final_dir
            if not src.is_dir():
                continue

            def _warn(msg: str) -> None:
                plan.warnings.append(msg)
                report.warnings.append(msg)
                self._emit({"kind": "warning", "message": msg})

            # 目标已存在则避让到 "包名 (2)"，与前一份产物并存（绝不覆盖）
            target = dst if not dst.exists() else unique_path(dst)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                moved = self._place_tree(src, target)
            except (OSError, shutil.Error) as exc:
                _warn(f"交付到源目录失败（产物仍在工作目录）：{target} — {exc}")
                continue

            if moved:
                # 产物被搬走后真实落点变了，必须同步回任务与库——否则
                # report.output_dirs 会指向已经消失的工作目录（UI「打开目录」失效），
                # 库里的 extracted_dir 也会失效，令下次重跑的幂等跳过永远
                # 判不出"产物仍在"，跳过功能在 workdir 模式下等于没做。
                t.extracted_dir = str(target)
                self.store.update(t)
            if target != dst:
                _warn(f"目标目录已存在，产物另存为：{target}")

    def _place_tree(self, src: Path, dst: Path) -> bool:
        """把 src 交付到 dst，返回是否发生了搬移（src 已不在原地）。

        同卷优先 rename：单个系统调用、原子、不占额外磁盘，因此不需要
        "留在工作目录"的兜底副本。跨卷时 rename 会在系统层退化成逐文件
        拷贝且中途失败会留下半成品，所以仍走 copytree，把工作目录那份
        留作安全网。rename 撞上竞态（目标被并发创建）时同样回落复制。
        """
        if same_volume(src, dst.parent):
            try:
                os.rename(src, dst)
                return True
            except OSError:
                pass  # 回落 copytree：可能跨设备，或目标刚被并发创建
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return False


    def _fill_report(self, report: RunReport, run_tasks: list[Task]) -> None:
        for t in run_tasks:
            if t.status == TaskStatus.DONE:
                report.done += 1
                # 只报最外层任务的落点：内层产物会被 _deliver_hierarchy 归并到最
                # 外层目录，内层自己的 out_dir 随后就被收掉了，报出来是条死路径。
                #
                # 旧实现按"自己没有子任务"筛（`t.id not in parent_ids`），条件正好
                # 写反：报的恰恰是被搬空的内层目录，外层交付目录反而被排除。UI 的
                # _open_results 会静默跳过不存在的目录，所以这个错一直没暴露。
                if t.parent_id is None and t.extracted_dir:
                    report.output_dirs.append(t.extracted_dir)
            elif t.status == TaskStatus.FAILED:
                report.failed += 1
            elif t.status == TaskStatus.NEEDS_PASSWORD:
                report.needs_password += 1
                report.needs_password_tasks.append(t)

    def _set(self, task: Task, status: TaskStatus) -> None:
        task.status = status
        self.store.update(task)
        self._emit({"kind": "status", "task_id": task.id, "status": task.status.value,
                    "password_used": task.password_used, "error": task.error,
                    "extracted_dir": task.extracted_dir})
