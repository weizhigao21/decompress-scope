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
from .archive_detect import COMPOUND_ZIP_EXTS, VIDEO_EXTS, looks_like_archive, volume_info
from .cloud_unlock import (
    STATUS_ERROR,
    STATUS_OK,
    CloudQueryJob,
    CloudResult,
    build_unlocker,
)
from .embedded_zip import find_embedded_zip, prepare_embedded_zip
from .config import Config
from .models import ArchiveInfo, AttemptOutcome, Task, TaskStatus
from .output_plan import (
    OutputPlan,
    archive_dir_name,
    plan_output,
    same_volume,
    unique_path,
)
from .password_finder import build_candidates, extract_source
from .name_repair import repair_tree
from .zip_password_filter import make_zip_password_filter
from .probe import ProbeError, check_bomb, classify_extract, probe, probe_with_password
from .sevenzip import SevenZip, SevenZipCancelled
from .store import TaskStore
from .vault import PasswordVault

# 云端查询在本地候选跑完之后最多再等多久（秒）。
#
# 云端从第一秒就在后台跑（见 CloudQueryJob），所以正常情况下候选还没跑完它
# 就回来了。只有候选极少（比如文件名里直接写着密码但猜错了）时才会走到"等它"：
# 这时候它已经是最后一根稻草，值得等 —— 但必须封顶，否则一个连不上的网络会让
# 每个加密包都卡满 socket 超时。真正的时间上限由 CloudUnlocker.timeout 决定，
# 这里额外留一点余量给算 MD5。
_CLOUD_WAIT_GRACE = 5.0

# 交付（跨卷复制）每次读写多少字节。8 MiB 兼顾两件事：块越大系统调用越少，
# 但块太大时"取消"与"报进度"的粒度也跟着变粗 —— 20GB 的包体文件按 8 MiB
# 分块是 2500 个检查点，足够及时。
_COPY_CHUNK = 8 * 1024 * 1024


class DeliveryCancelled(Exception):
    """用户取消时打断了产物交付（跨卷复制中途停下）。

    单独一个异常类型：交付里"用户不干了"和"磁盘满/没权限"必须分开处理——
    前者不是故障，产物完整留在工作目录里，措辞也不该是"失败"。
    """


def _copy_file_chunked(src: Path, dst: Path, on_chunk, should_cancel) -> None:
    """按块复制一个文件，顺带保留时间戳。

    单独成函数是为了有个明确的注入点：交付失败/取消的守卫要能在"复制到一半"
    这一层抛错，而 `shutil` 的接口既报不了进度也插不进取消检查。
    """
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        while True:
            if should_cancel():
                raise DeliveryCancelled("用户取消交付")
            chunk = fin.read(_COPY_CHUNK)
            if not chunk:
                break
            fout.write(chunk)
            on_chunk(len(chunk))
    shutil.copystat(src, dst)


@dataclass
class RunReport:
    done: int = 0
    failed: int = 0
    needs_password: int = 0
    skipped: int = 0
    # 解压成功但产物没能交付回源目录的任务数。绝不该被算进 failed——产物确实解出来了，
    # 只是还在工作目录里；但也绝不该被藏起来，否则就是"库里 done、界面完成、
    # 用户手上什么都没有"。
    delivery_failed: int = 0
    needs_password_tasks: list[Task] = field(default_factory=list)
    failed_tasks: list[Task] = field(default_factory=list)
    skipped_tasks: list[Task] = field(default_factory=list)
    delivery_failed_tasks: list[Task] = field(default_factory=list)
    output_dirs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # 既不是失败也不是警告的补充说明（例如"已修正 N 个乱码文件名"）。
    # 单独立一个字段：混进 warnings 会让「结果里有警告」失去信息量。
    notes: list[str] = field(default_factory=list)


class Pipeline:
    def __init__(self, cfg: Config, vault: PasswordVault, store: TaskStore):
        self.cfg = cfg
        self.sz = SevenZip(cfg.sevenzip_path, cfg.list_timeout, cfg.extract_timeout)
        self.vault = vault
        self.store = store
        self._on_event = None  # 可选事件回调: on_event(dict)，供 UI 桥接
        self._should_cancel = None  # 可选取消探测: should_cancel() -> bool
        self._ask_password = None  # 可选问询回调: ask_password({"path": ...}) -> str | None
        # 可选先手密码探针: password_ready() -> str | None（UI 上已填好、还没等
        # pipeline 开口问的那个密码）。与 _ask_password 的分工见 _early_user_password。
        self._password_ready = None
        # 输出落地策略（由 UI/CLI 注入；默认沿用旧的隔离工作目录行为）
        self.output_mode = OUTPUT_WORKDIR
        # 容器目录名：留空 = 产物直接落在压缩包所在目录，不再套一层容器
        self.subdir_name = ""
        self.copy_back = True  # workdir 模式下是否复制一份回源目录
        self.skip_done = getattr(cfg, "skip_done", True)  # 重跑时跳过已成功解压的包
        # 云端密码库（云解密）：默认由 cfg 决定（关闭时为 None，绝不出网）。
        # 测试直接把它换成一个假实现即可，不必碰网络。
        self.cloud_unlock = build_unlocker(cfg)
        # 同一轮里同一个文件只查一次云端：同一个包被拖两次、或内层重复时，
        # 每一次查询都要烧对方一次配额（App 侧显示"剩余 N 次"）。
        self._cloud_cache: dict[str, CloudResult] = {}
        self._cloud_note = ""          # 最近一次云端结论，用于密码问询里交代一句
        self._cloud_error_reported = False
        self._plans: dict[int, OutputPlan] = {}
        self._input_aliases: dict[str, str] = {}
        self._joined_volume_paths: dict[int, set[Path]] = {}
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

    def _early_user_password(self) -> str:
        """用户**先手**输入的密码（UI 上已经填好、pipeline 还没开口问）。无则空串。

        和 `_ask_password` 的分工：那个是"候选全失败后现场问、阻塞等回答"；
        这个只做**探测**（不消费、不回答），由候选循环每轮问一次 —— 从右键进来的
        用户常常手里就攥着密码，让他等着 20 个候选挨个跑完（7z-AES 每个约 0.3 s）
        纯属浪费。一探到输入，就把它插到最前面试。

        探针本身不许把 pipeline 搞崩：UI 侧的任何异常都按"没有先手密码"处理。
        """
        cb = self._password_ready
        if cb is None:
            return ""
        try:
            return (cb() or "").strip()
        except Exception:
            return ""

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
        if (
            prev is None
            or not prev.extracted_dir
            or not prev.archive_fingerprint
            or prev.archive_fingerprint != self._archive_fingerprint(Path(archive_path))
        ):
            return None
        try:
            if Path(prev.extracted_dir).is_dir():
                return prev
        except OSError:
            return None
        return None

    @staticmethod
    def _archive_fingerprint(archive: Path) -> str:
        """生成用于幂等跳过的轻量文件指纹。

        分卷包必须包含整组卷的大小和修改时间：只记录首卷的话，后续分卷被
        替换仍会错判成旧任务。读取元数据即可完成，不为跳过判定额外读取大文件。
        文件在扫描后被删掉或无法 stat 时返回空串，调用方自然会重新处理。
        """
        try:
            vi = volume_info(archive)
            if vi is None:
                stat = archive.stat()
                return f"file:{stat.st_size}:{stat.st_mtime_ns}"
            base, _ = vi
            parts: list[tuple[int, str, int, int]] = []
            for candidate in archive.parent.iterdir():
                if not candidate.is_file():
                    continue
                member = volume_info(candidate)
                if member is None or member[0].lower() != base.lower():
                    continue
                stat = candidate.stat()
                parts.append((member[1], candidate.name.lower(), stat.st_size, stat.st_mtime_ns))
            parts.sort()
            return "volumes:" + "|".join(
                f"{index}:{name}:{size}:{mtime}" for index, name, size, mtime in parts
            )
        except OSError:
            return ""

    # ---------- 主流程 ----------

    def _associated_embedded_inputs(self, path: Path) -> list[Path]:
        """选中一个视频分卷载体时，只补入装着同组分卷的同目录载体。"""
        bounds = find_embedded_zip(path)
        groups = {vi[0] for name in (bounds.members if bounds else ())
                  if (vi := volume_info(Path(name))) is not None}
        if not groups:
            return [path]
        result = [path]
        for sibling in sorted(path.parent.iterdir()):
            if sibling == path or not sibling.is_file():
                continue
            if sibling.suffix.lower() not in (set(self.cfg.archive_exts) | VIDEO_EXTS):
                continue
            other = find_embedded_zip(sibling)
            if other and any((vi := volume_info(Path(name))) and vi[0] in groups
                             for name in other.members):
                result.append(sibling)
        return result

    def run(
        self,
        inputs: list[Path],
        source: str = "",
        on_event=None,
        should_cancel=None,
        ask_password=None,
        password_ready=None,
    ) -> RunReport:
        """可选回调（均为普通可调用对象，core 不依赖任何 UI 框架）:

        - on_event(dict): 事件流，
          kind ∈ {'task','status','progress','info','warning'}
          （info = 包体信息，见 _emit_archive_info）
        - should_cancel(): 返回 True 时在当前任务完成后停止处理剩余任务
        - ask_password({"path": ...}) -> str | None:
          密码候选全部失败时现场询问（如 UI 弹窗），返回密码则立即重试，
          返回 None/空 则该包进入 needs_password。
        - password_ready() -> str | None: 用户**先手**填好的密码（不等 pipeline
          开口问）。候选循环每轮探一次，一有输入就插到最前面试 —— 密码是从 UI
          输入框直接拿的，天然比"文件名/域名/密码库"推导出来的候选更可靠。

        输出落点由 self.output_mode / self.subdir_name 决定（见 core.output_plan）。
        """
        self._on_event = on_event
        self._should_cancel = should_cancel
        self._ask_password = ask_password
        self._password_ready = password_ready
        self._plans.clear()
        self._input_aliases.clear()
        self._joined_volume_paths.clear()
        self._cloud_cache.clear()
        self._cloud_note = ""
        self._cloud_error_reported = False
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
                files = self._collapse_volumes(self._associated_embedded_inputs(p), report)
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
                task = Task(
                    archive_path=key,
                    archive_fingerprint=self._archive_fingerprint(f),
                    depth=0,
                    source=source or extract_source(f.name),
                )
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

    def _emit_archive_info(self, task: Task, info: ArchiveInfo) -> None:
        """把探测到的包体信息推给界面：类型 / 压缩包大小 / 解压后大小 / 文件数 / 是否加密。

        单独成一种事件而不是挂在 status 上：status 是状态机迁移（一个任务要发好几
        次），而包体信息是"探到了新数据"才发——头部加密的包在拿到密码复探之后
        就是第二次，界面据此把解压后大小从空补齐。
        """
        self._emit({
            "kind": "info",
            "task_id": task.id,
            "format": info.format_name,
            "archive_size": info.archive_size,
            "volumes": info.volume_count,
            "uncompressed": info.total_uncompressed,
            "files": info.file_count,
            # 两种加密形态都要算：zipcrypto 只加密内容（条目带 Encrypted=+），
            # 7z 的 -mhe=on 连目录一起加密（空密码列不出目录）。
            "encrypted": bool(info.flag_encrypted or info.needs_password_for_listing),
        })

    def _process(self, task: Task, queue: deque[Task], run_tasks: list[Task], report: RunReport) -> None:
        self._join_sibling_volumes(task, run_tasks)
        original = Path(task.archive_path)
        with prepare_embedded_zip(
            original, self.cfg.workdir,
            should_cancel=self._cancel_requested,
            on_progress=lambda pct: self._emit({
                "kind": "phase", "task_id": task.id, "percent": pct,
                "path": str(original),
                "message": f"正在恢复视频内嵌 ZIP… {pct}%",
            }),
        ) as archive:
            self._input_aliases[str(archive)] = str(original)
            try:
                self._process_archive(task, queue, run_tasks, report, archive)
            finally:
                self._input_aliases.pop(str(archive), None)

    def _join_sibling_volumes(self, task: Task, run_tasks: list[Task]) -> None:
        """BFS 已解开外层载体后，把同组内层分卷放到首卷旁边供 7z 读取。"""
        archive = Path(task.archive_path)
        vi = volume_info(archive)
        if task.parent_id is None or vi is None or vi[1] != 1:
            return
        by_id = {t.id: t for t in run_tasks}

        def source_folder(t: Task) -> Path:
            while t.parent_id in by_id:
                t = by_id[t.parent_id]
            return Path(t.archive_path).parent

        candidates: dict[int, set[Path]] = {}
        for parent in run_tasks:
            plan = self._plans.get(parent.id)
            if (parent.status != TaskStatus.DONE or parent.depth != task.depth - 1
                    or plan is None or source_folder(parent) != source_folder(task)):
                continue
            for member in plan.out_dir.rglob("*"):
                other = volume_info(member)
                if member.is_file() and other and other[0] == vi[0]:
                    candidates.setdefault(other[1], set()).add(member)
        consumed = {archive}
        for index, paths in candidates.items():
            if index == 1 or len(paths) != 1:
                continue  # 同名组有歧义时不混用
            source = next(iter(paths))
            target = archive.parent / source.name
            if source != target:
                if target.exists():
                    continue
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copy2(source, target)
            consumed.update((source, target))
        self._joined_volume_paths[task.id] = consumed

    def _process_archive(self, task: Task, queue: deque[Task], run_tasks: list[Task],
                         report: RunReport, archive: Path) -> None:
        self._set(task, TaskStatus.PROBING)
        try:
            info = probe(self.sz, archive)
        except ProbeError as exc:
            task.error = f"探测失败: {exc}"
            self._set(task, TaskStatus.FAILED)
            return
        # 先报包体信息，再判 bomb：被判拒的包恰恰最需要让用户看清"是什么、多大"，
        # 顺序反了就会留下一行只有失败原因的空白任务。
        self._emit_archive_info(task, info)
        bomb_reason = check_bomb(info, self.cfg)
        if bomb_reason:
            task.error = bomb_reason
            self._set(task, TaskStatus.FAILED)
            return

        # 库的取用配额**必须**取自 max_password_attempts，不能在这里写死。
        # 写死 20 会让设置项说谎：用户把它从 20 调到 200，库里能进来的仍然只有
        # 那 20 条，调整完全不生效。候选的最终截断在下一行的 [:...] 完成。
        vault_candidates = self.vault.candidates_for(
            task.source, limit=self.cfg.max_password_attempts
        )
        candidates = build_candidates(
            Path(task.archive_path).name, task.source, vault_candidates
        )[: self.cfg.max_password_attempts]

        plan = self._plan_for(task)
        out_dir = plan.out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        self._set(task, TaskStatus.EXTRACTING)

        # 云端从第一秒就并行开跑（见 _attempt_extract），所以这里只决定
        # "这个包值不值得查"：
        # ① 必须真的加密（明文包第一轮空密码就成功了，根本走不到云端那一步，
        #    这个条件只是兜底，防止哪天候选顺序变了就先烧掉一次查询）；
        # ② 分卷包跳过：云端按**整包** MD5 索引，首卷的 MD5 与它对不上，
        #    查了必然未收录，只会白烧一次配额（报告 §4.3 的 md5 是整包）。
        cloud_ok = bool(info.flag_encrypted or info.needs_password_for_listing) \
            and info.volume_count <= 1
        outcome = self._attempt_extract(
            archive, out_dir, candidates, task.id,
            cloud_ok=cloud_ok, report=report,
        )
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
                # 复探拿到了真实条目，补发一次包体信息：界面上"解压后大小/文件数"
                # 从空变成有值，用户能看到这个包到底有多大。
                self._emit_archive_info(task, real)
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

        # ZIP 里不带 UTF-8 标志的文件名由 7z 按系统代码页（中文 Windows 上是 936）
        # 解释，Shift-JIS 的日文包落盘就成了「擔杮岅偺僼傽僀儖柤」这种乱码。
        # 必须趁产物还没被交付、没被当成内层包探测之前改回来 —— 一旦按乱码名
        # 交付出去，用户拿到的就是一堆认不出的文件（见 core/name_repair.py）。
        repair = repair_tree(plan.out_dir, archive=archive)
        if repair.renamed:
            report.notes.append(f"{archive.name}: {repair.describe()}")
            # 界面列只放得下一句话，例子走 detail（悬停能看到）
            self._emit({"kind": "notice", "task_id": task.id,
                        "message": repair.summary(),
                        "detail": repair.describe()})

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

    def _record_delivery_failure(self, task: Task, msg: str, report: RunReport) -> None:
        """产物没能落到该去的地方：落库 + 双通道告警（report 给 CLI，事件给 UI）。

        为什么不能只往 report.warnings 里塞一句就 continue（旧实现就是这么干的）：
        那条警告只活在本次运行的内存里。运行结束，库里那条记录仍是 DONE、界面
        仍是「完成」，而用户的目标目录里什么都没有——产物还压在隔离工作目录里
        吃磁盘，且下一次重跑会因为「产物仍在」被幂等跳过。库里没有任何一行能
        回答"我上次那 5 GB 去哪了"。

        状态刻意不动：解压确实成功了，把它改成 FAILED 会让用户以为包是坏的、
        跑去重新解压（然后被同名避让堆出一份新的）。真正缺的信息用
        delivery_error 单独表达。
        """
        task.delivery_error = msg
        try:
            self.store.update(task)
        except Exception:
            pass  # 落库失败不该让整轮运行崩掉，事件与 report 通道仍然有记录
        report.warnings.append(msg)
        self._emit({"kind": "warning", "message": msg})
        self._emit({
            "kind": "status", "task_id": task.id, "status": task.status.value,
            "password_used": task.password_used, "error": task.error,
            "extracted_dir": task.extracted_dir, "delivery_error": msg,
        })

    def _deliver_hierarchy(self, run_tasks: list[Task], report: RunReport) -> None:
        """把嵌套任务的产物按「包名」逐层落进父任务的交付目录。

        内层压缩包一律在自己的隔离 out_dir 里展开（避免中间产物污染用户目录），
        结束时把它**整目录搬到它在父产物中的原位置**，目录名换成自己的包名：

            outer.zip → data/inner.zip → deep.txt
            ⇒ <交付>/outer/data/inner/deep.txt

        按深度降序上传，所以搬某一层时它下面的层已经就位、随之一起搬走。

        为什么不能像旧实现那样把内层内容「拍平」进外层目录：拍平后内层包名彻底
        消失，用户拿到的东西看不出是从哪个包来的；一个外层套多个内层包时，各包
        内容更是全糊在同一层里分不开。保留层级则包名与层级都在产物里。
        """
        by_id = {t.id: t for t in run_tasks}
        plans = self._plans
        for t in sorted(run_tasks, key=lambda x: x.depth, reverse=True):
            if t.status != TaskStatus.DONE or t.parent_id is None:
                continue
            parent = by_id.get(t.parent_id)
            parent_plan = plans.get(t.parent_id)
            child_plan = plans.get(t.id)
            if parent is None or parent_plan is None or child_plan is None:
                continue
            # 父任务失败时它的 out_dir 已被收掉，此时把子产物搬进去等于凭空复活
            # 一个不该存在的目录（samedir 下还会直接出现在用户源目录里）。
            if parent.status != TaskStatus.DONE:
                continue
            if not child_plan.out_dir.is_dir():
                continue

            anchor = parent_plan.out_dir
            try:
                rel = Path(t.archive_path).resolve().relative_to(anchor.resolve())
            except (ValueError, OSError):
                # 内层包不在父产物里（被用户搬走等）：无从判断该落在哪一级。
                # 但**不能静默 continue**——这条分支此前一声不响地放过，产物就
                # 留在工作目录里，而库里是 DONE、界面显示「完成」。用户既看不到
                # 提示，也没有任何线索指向那堆残留。
                self._record_delivery_failure(
                    t,
                    f"内层产物无法定位归属（内层包已不在父产物内），仍留在工作目录："
                    f"{child_plan.out_dir}",
                    report,
                )
                continue
            target = anchor / rel.parent / archive_dir_name(rel)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    target = unique_path(target)
                shutil.move(str(child_plan.out_dir), str(target))
            except (OSError, shutil.Error) as exc:
                self._record_delivery_failure(
                    t,
                    f"内层产物落到外层失败（产物仍在工作目录）："
                    f"{child_plan.out_dir} → {target} — {exc}",
                    report,
                )
                continue

            # 已消化的内层压缩包不留在交付里。
            #
            # 删除点特意放在本函数而不是 _prune_consumed_archives：那里靠「相对
            # T.out_dir 的路径」定位，而深度 ≥2 时中间层的 out_dir 已被本函数搬走，
            # 锚点失效 → 那些包永远清不掉。此刻 rel 就是它的确切位置，最可靠。
            # 关掉 delete_intermediate 时不删，用户就能同时拿到包与解出的目录。
            if self.cfg.delete_intermediate:
                try:
                    (anchor / rel).unlink(missing_ok=True)
                except OSError:
                    pass
                for volume in self._joined_volume_paths.get(t.id, set()):
                    try:
                        volume.unlink(missing_ok=True)
                    except OSError:
                        pass

            # out_dir 整个搬走后只剩空壳，顺手把 workdir 里的 task_<id>/out/ 两层
            # 空目录收掉，别攒垃圾（绝不越过 workdir 根，也绝不删非空目录）
            try:
                root = Path(self.cfg.workdir).resolve()
                shell = child_plan.out_dir.parent
                for _ in range(2):
                    if not shell.is_dir() or shell.resolve() == root:
                        break
                    if any(shell.iterdir()):
                        break
                    shell.rmdir()
                    shell = shell.parent
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

        samedir 模式会连父级容器目录一起收掉——若本轮所有任务都没成功，
        <源目录>/<容器名>/ 不该剩下（用户会以为解压成功了）。
        只有当目录为空时才删，绝不碰用户已经放进去的文件。
        """
        # 覆盖模式的输出先在 workdir staging 区生成。失败时只能清这块
        # 自己创建的目录，绝不能检查或删除 final_dir——它可能是用户原有目录。
        if plan.out_dir != plan.final_dir:
            shutil.rmtree(plan.out_dir, ignore_errors=True)
            return
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

        # 往上收空壳父目录：只在**确实有容器层**时（subdir_name 非空）收，且只收
        # 名为它的那一层，绝不越过用户源目录。留空时 parent 就是压缩包所在目录
        # 本身——那是用户的下载目录，无论如何都不能碰。
        if self.subdir_name:
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

    @staticmethod
    def _cloud_cache_key(archive: Path) -> str:
        """同一轮内的云端查询去重键。取不到元数据时返回空串（不去重）。"""
        try:
            stat = archive.stat()
            resolved = archive.resolve()
        except OSError:
            return ""
        return f"{resolved}:{stat.st_size}:{stat.st_mtime_ns}"

    def _cloud_start(self, archive: Path, task_id: int
                     ) -> tuple[CloudQueryJob | None, CloudResult | None]:
        """开一个后台云端查询。返回 (任务, 已就绪的结论)。

        两个返回值互斥：
        - 本轮缓存里已经有这个文件的结论 → 直接给 (None, 结论)，不再发包（配额很贵）；
        - 否则 (job, None)，查询在后台线程跑，主流程该试什么继续试。

        云端**排在第一位**不是"先等它"，而是"它从第一秒就开始跑"：大包算 MD5
        要几秒到几十秒，放在主流程里会把本地候选全堵在后面；反过来，本地候选
        可能是两千条、7z-AES 每条 0.3 s，全跑完要十分钟。两条线并行，谁先出
        结果用谁，命中即插队。

        为什么另开线程的是云端、不是本地候选：候选每试一个都要跑一次 `7z.exe`
        往**同一个** out_dir 里解，两条并行会互相踩产物；而 pipeline 本身在
        GUI 里已经跑在工作线程上（右键窗口与主窗口都是 QThread），CLI 里也没有
        别人在等。真正需要"另开线程"的只有云端这条纯 I/O（哈希 + 网络）的线。
        """
        unlocker = self.cloud_unlock
        if unlocker is None:
            return None, None
        key = self._cloud_cache_key(archive)
        cached = self._cloud_cache.get(key) if key else None
        if cached is not None:
            self._cloud_note = cached.message
            return None, cached
        self._emit({"kind": "phase", "task_id": task_id, "percent": 0,
                    "message": "正在向云端密码库查询（同时继续试本地密码）"})
        job = CloudQueryJob(unlocker, archive, should_cancel=self._cancel_requested)
        job.start()
        return job, None

    def _cloud_finish(self, result: CloudResult, archive: Path,
                      report: RunReport | None) -> None:
        """记住结论（本轮缓存 + 给问询用的一句话），失败时整轮只警告一条。"""
        key = self._cloud_cache_key(archive)
        if key:
            self._cloud_cache[key] = result
        self._cloud_note = result.message
        if result.status == STATUS_ERROR and not self._cloud_error_reported:
            self._cloud_error_reported = True
            msg = (f"{archive.name}: 云端密码库查询失败 —— {result.message}"
                   f"（本轮不再重试云端）")
            if report is not None:
                report.warnings.append(msg)
            self._emit({"kind": "warning", "message": msg})

    def _try_password(self, task_id: int, archive: Path, out_dir: Path, pwd: str,
                      message: str = "正在解压") -> tuple[AttemptOutcome | None, str]:
        """用这个密码解压一次。

        返回 (结论, 错误消息)：结论非空 = 这一轮得有结果（成功 / 硬失败 / 取消）；
        结论为空 = 密码不对（或可继续的情况），调用方接着试下一个，并把错误
        消息留给最后的"密码均不匹配"。

        抽出来是因为现在有三条入口都要走同一段"解压 → 判类 → 清现场"：用户先手
        密码、云端密码、候选密码。三份复制粘贴的话，任何一处漏掉 rmtree 都会让
        下一次尝试撞上残留产物。
        """
        self._emit({"kind": "phase", "task_id": task_id, "percent": 0,
                    "message": message})
        result = self._run_extract(task_id, archive, out_dir, pwd)
        if result is None:
            return AttemptOutcome(message="用户取消", password_issue=False), ""
        code, out, err = result
        ok, wrong_pw, msg = classify_extract(code, out, err)
        if ok:
            return AttemptOutcome(password=pwd), ""
        if not wrong_pw:
            return AttemptOutcome(message=msg, password_issue=False), msg
        shutil.rmtree(out_dir, ignore_errors=True)
        return None, msg

    def _attempt_extract(
        self, archive: Path, out_dir: Path, candidates: list[str], task_id: int,
        *, cloud_ok: bool = False, report: RunReport | None = None,
    ) -> AttemptOutcome:
        """空密码先试，再按候选顺序尝试；ZIP 先快速排除明显错误的候选。

        三条线并行，谁先出结果用谁：
        1. **用户先手密码** —— 每轮候选之前探一次 `password_ready()`，一有输入就
           插到最前面试。试错也不扔：记进 seen 后继续按顺序试候选，最后仍会重问。
        2. **云端密码库** —— `_cloud_start` 一进本函数就把查询丢到后台线程
           （算 MD5 + 发包），主流程继续试本地候选；每轮候选前 poll 一次，
           云端一命中就**立刻插队**用它解压，剩下的候选不再跑。
        3. **本地候选** —— 文件名提示 / 来源派生 / 密码库 / 常用字典。

        为什么云端要"排第一"却不"先等它"：大包算 MD5 要几秒到几十秒，等它就是
        把本地候选全堵在后面；而本地候选可能两千条、7z-AES 每条 0.29 s，全跑完
        是十分钟级。两条线本来就是独立的，并行才是"谁都不等谁"。

        候选都试完、云端还没回来时才阻塞等它（封顶 `_CLOUD_WAIT_GRACE` +
        查询超时）；它也没有才现场询问密码，最多 3 次；用户放弃则进入
        needs_password。`cloud_ok` 由调用方判定"这个包值不值得查"
        （见 _process_archive）。
        """
        attempts = [""]
        seen: set[str] = {""}
        self._cloud_note = ""
        for cand in candidates:
            if cand and cand not in seen:
                seen.add(cand)
                attempts.append(cand)
        last_msg = ""
        password_filter = None
        last_percent = -1
        total = len(attempts) - 1

        cloud_job: CloudQueryJob | None = None
        cloud_result: CloudResult | None = None
        cloud_done = False          # 云端结论已经用过了，不再重复取
        hash_bucket = {"pct": -10}

        if cloud_ok:
            cloud_job, cloud_result = self._cloud_start(archive, task_id)

        def take_cloud(wait: bool = False) -> AttemptOutcome | None:
            """取云端结论并试着解压。返回 None = 没有可用结论，继续本地流程。"""
            nonlocal cloud_result, cloud_done
            if cloud_done or (cloud_job is None and cloud_result is None):
                return None
            if cloud_result is None and cloud_job is not None:
                if not wait:
                    # 顺手把校验值进度报上去（在自己的线程里读、在自己的线程里发）
                    pct = cloud_job.percent
                    if pct - hash_bucket["pct"] >= 10:
                        hash_bucket["pct"] = pct
                        self._emit({
                            "kind": "phase", "task_id": task_id, "percent": pct,
                            "message": f"正在计算校验值… {pct}%（本地密码同时在试）",
                        })
                    cloud_result = cloud_job.poll()
                    if cloud_result is None:
                        return None
                else:
                    self._emit({"kind": "phase", "task_id": task_id, "percent": 0,
                                "message": "本地密码都试过了，正在等云端密码库"})
                    limit = float(getattr(self.cfg, "cloud_unlock_timeout", 10.0) or 10.0)
                    cloud_result = cloud_job.wait(limit + _CLOUD_WAIT_GRACE)
                    if cloud_result is None:
                        cloud_result = CloudResult(
                            None, "云端查询超时（网络太慢或对方无响应）", STATUS_ERROR)
            cloud_done = True
            self._cloud_finish(cloud_result, archive, report)
            pwd = cloud_result.password
            if not pwd:
                return None
            if pwd in seen:
                # 云端给的正是本地刚试过的那个：不必再花一次解压时间
                self._cloud_note = "云端返回的密码与本地候选相同（已试过）"
                return None
            seen.add(pwd)
            self._emit({"kind": "phase", "task_id": task_id, "percent": 0,
                        "message": f"云端密码库命中：{pwd}"})
            outcome, msg = self._try_password(
                task_id, archive, out_dir, pwd, message="正在用云端密码解压")
            if outcome is not None:
                return outcome
            # 云端给了密码却解不开：库里这条记录已过期，或这个包被人替换过。
            # 不能就此判失败——语义上仍是"密码不对"，继续走本地候选并问用户。
            self._cloud_note = "云端返回的密码不正确（库里的记录可能已过期）"
            return None

        for index, pwd in enumerate(attempts):
            if self._cancel_requested():
                return AttemptOutcome(message="用户取消", password_issue=False)
            early = self._early_user_password()
            if early and early not in seen:
                # 用户先手填好了密码：插到最前面试。注意这里**不** break ——
                # 试错就回到候选顺序继续（seen 已记下，不会把这个密码再试一遍）。
                seen.add(early)
                outcome, msg = self._try_password(
                    task_id, archive, out_dir, early, message="正在用你输入的密码解压")
                if outcome is not None:
                    return outcome
                last_msg = msg or last_msg
                continue
            # 云端插队点放在先手密码之后、本地候选之前：用户手里攥着密码时先试
            # 他的；否则云端一有结论就立刻插队，不等候选跑完。
            due = take_cloud()
            if due is not None:
                return due
            if pwd:
                percent = (index - 1) * 100 // total
                if percent != last_percent:
                    last_percent = percent
                    self._emit({
                        "kind": "phase", "task_id": task_id, "percent": percent,
                        "message": f"正在查找密码（{index}/{total}）",
                    })
                if password_filter is not None and not password_filter.may_match(pwd):
                    last_msg = "密码均不匹配"
                    continue
            outcome, msg = self._try_password(task_id, archive, out_dir, pwd)
            if outcome is not None:
                return outcome
            last_msg = msg or last_msg
            if not pwd:
                # 空密码这次失败之后才建 ZIP 快速预筛（建它要读一次目录表）
                password_filter = make_zip_password_filter(archive)

        # 候选跑完了，云端可能还在算 MD5 / 等响应：它是最后一根稻草，等它。
        due = take_cloud(wait=True)
        if due is not None:
            return due

        if self._ask_password is not None:
            for _ in range(3):
                manual = ""
                try:
                    manual = self._ask_password({
                        "path": self._input_aliases.get(str(archive), str(archive)),
                        # 让弹窗能交代"云端也查过了"，用户才知道下一步该找谁
                        "cloud": self._cloud_note,
                    }) or ""
                except Exception:
                    break
                manual = manual.strip()
                if not manual:
                    break
                if manual in seen:
                    continue
                seen.add(manual)
                outcome, msg = self._try_password(task_id, archive, out_dir, manual)
                if outcome is not None:
                    return outcome
                last_msg = msg or last_msg
        return AttemptOutcome(message=last_msg or "密码均不匹配", password_issue=True)

    def _enqueue_children(
        self, task: Task, out_dir: Path, queue: deque[Task], run_tasks: list[Task], report: RunReport
    ) -> None:
        inner = sorted(
            p for p in out_dir.rglob("*")
            if p.is_file() and self._looks_like_archive(p)
        )
        inner = self._collapse_volumes(inner, report)
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
                archive_fingerprint=self._archive_fingerprint(p),
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
        尽力而为：目标已存在则避让到「包名~2」，空间不足/无权限记一条警告并把
        失败**落库**（delivery_error），任务状态仍算成功——解压本身没出错。
        警告同时进 report 与事件流：CLI 只读 report，UI 只读事件流，两边都不能瞎。
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

            # 目标已存在则避让到 "包名~2"，与前一份产物并存（绝不覆盖）。
            # 覆盖只对已存在的同名**目录**成立（合并进它）；同名位置是文件时
            # 无法合并——无扩展名的包，其目录路径正是包文件本身，往那里写
            # 只会得到 WinError 183。这条判断同时兜住 plan 算完后的竞态。
            if plan.overwrite and dst.is_dir():
                target = dst
            else:
                target = dst if not dst.exists() else unique_path(dst)
            created = not target.exists()
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                self._place_tree(
                    src, target,
                    on_progress=lambda pct, _t=t: self._emit({
                        "kind": "phase", "task_id": _t.id, "percent": pct,
                        # 百分比交给进度条，话里不再重复：界面那句后面还会
                        # 拼上「（已用时 m:ss）」，塞个数字进去读着别扭
                        "message": "正在交付到源目录",
                    }),
                    should_cancel=self._cancel_requested,
                )
            except DeliveryCancelled:
                # 用户不干了：把**自己刚建的**半个目标目录收掉，产物原样留在
                # 工作目录。不删的话源目录会留下一棵看似完整、其实缺文件的树 ——
                # 那种"半成品"比没有更糟，用户会以为是这个包本身有问题。
                if created:
                    shutil.rmtree(target, ignore_errors=True)
                self._record_delivery_failure(
                    t,
                    f"交付被取消（产物完整保留在工作目录 {src}）：{target}",
                    report,
                )
                continue
            except (OSError, shutil.Error) as exc:
                if created:
                    shutil.rmtree(target, ignore_errors=True)
                self._record_delivery_failure(
                    t,
                    f"交付到源目录失败（产物仍在工作目录 {src}）：{target} — {exc}",
                    report,
                )
                continue

            # 交付成功（无论是搬到目标还是复制到目标）：真实落点变了，必须同步
            # 回任务与库。跨卷时 _place_tree 走 copytree，src 会作为兜底副本留在
            # 工作目录里——此时 extracted_dir 若仍指着工作目录，界面「打开目录」
            # 就会开到隔离区，用户明明拿到了文件却以为没解出来；工作目录里那份
            # 也会被残留扫描误判成"还没交付"。
            t.extracted_dir = str(target)
            self.store.update(t)
            if target != dst:
                msg = f"目标目录已存在，产物另存为：{target}"
                plan.warnings.append(msg)
                report.warnings.append(msg)
                self._emit({"kind": "warning", "message": msg})

    def _place_tree(self, src: Path, dst: Path, on_progress=None,
                    should_cancel=None) -> None:
        """把 src 交付到 dst。

        同卷优先 rename：单个系统调用、原子、不占额外磁盘，因此不需要
        "留在工作目录"的兜底副本。跨卷时 rename 会在系统层退化成逐文件
        拷贝且中途失败会留下半成品，所以仍走逐文件复制，把工作目录那份
        留作安全网。rename 撞上竞态（目标被并发创建）时同样回落复制。
        """
        if same_volume(src, dst.parent):
            try:
                os.rename(src, dst)
                return
            except OSError:
                pass  # 回落复制：可能跨设备，或目标刚被并发创建
        self._copy_tree(src, dst, on_progress, should_cancel)

    def _copy_tree(self, src: Path, dst: Path, on_progress=None,
                   should_cancel=None) -> None:
        """跨卷交付：逐文件复制，带字节进度与取消点。

        不用 `shutil.copytree` 的两条硬理由（都是实测踩到的）：
        ① 它报不了进度 —— 28GB 从 C 盘到 F 盘要几分钟，界面只能停在
           「解压 100%」一动不动，用户以为程序卡死；
        ② 它不能被打断 —— 用户关窗之后复制仍在后台跑，跑完才算完，中途被杀
           就留下一棵缺文件的半成品树（调用方负责按 `created` 收掉）。

        大文件按块复制：一个 20GB 的包体文件本身就要搬几十秒，只在"文件之间"
        报进度等于一个文件一条进度，照样是卡死的样子；取消点也必须进到块循环
        里，否则点取消要等当前文件搬完（可能几分钟）。
        """
        entries: list[tuple[Path, int | None]] = []
        total = 0
        for root, dirs, names in os.walk(src):
            rootp = Path(root)
            entries.extend((rootp / d, None) for d in dirs)
            for name in names:
                path = rootp / name
                try:
                    size = path.stat().st_size
                except OSError:
                    size = 0
                entries.append((path, size))
                total += size

        dst.mkdir(parents=True, exist_ok=True)
        state = {"done": 0, "last": -1}

        def cancelled() -> bool:
            return should_cancel is not None and bool(should_cancel())

        def advance(nbytes: int) -> None:
            state["done"] += nbytes
            pct = int(state["done"] * 100 / total) if total else 100
            if pct != state["last"]:
                state["last"] = pct
                if on_progress is not None:
                    on_progress(pct)

        # 立刻报一次 0%：界面要在开始搬的第一秒就从「正在解压 100%」切到
        # 「正在交付 0%」。等到搬完第一个文件才说话，一个 20GB 的包体文件
        # 就能让这句话晚几十秒才出现 —— 那正是用户以为卡死的那一刻。
        advance(0)

        for path, size in entries:
            if cancelled():
                raise DeliveryCancelled("用户取消交付")
            target = dst / path.relative_to(src)
            if size is None:
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            _copy_file_chunked(path, target, advance, cancelled)
        if total == 0 and on_progress is not None:
            on_progress(100)


    def _fill_report(self, report: RunReport, run_tasks: list[Task]) -> None:
        for t in run_tasks:
            if t.status == TaskStatus.DONE:
                report.done += 1
                # 交付失败单独计数：它既不能被算作 failed（产物确实解出来了），
                # 也不能只躺在 warnings 里——CLI 的退出码与 UI 的横幅都靠它。
                if t.delivery_error:
                    report.delivery_failed += 1
                    report.delivery_failed_tasks.append(t)
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
                report.failed_tasks.append(t)
            elif t.status == TaskStatus.NEEDS_PASSWORD:
                report.needs_password += 1
                report.needs_password_tasks.append(t)

    def _set(self, task: Task, status: TaskStatus) -> None:
        task.status = status
        self.store.update(task)
        self._emit({"kind": "status", "task_id": task.id, "status": task.status.value,
                    "password_used": task.password_used, "error": task.error,
                    "extracted_dir": task.extracted_dir,
                    "delivery_error": task.delivery_error})
