"""应用配置持久化：JSON 存盘，UI 与 CLI 共用。

分层约定：
- core 层零 Qt 依赖，本模块只认 Python 原生类型。
- 只负责「读写 + 校验」，不负责 GUI 展示（展示在 ui/settings_window.py）。
- 配置项是「用户偏好」，与 core.config.Config 的「单次运行参数」区分：
    运行参数（max_depth / workdir ...）每次 run 从偏好派生 → 见 as_overrides()。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

# 解压产物落地位置
OUTPUT_WORKDIR = "workdir"      # 隔离工作目录（默认，项目/.workspace）
OUTPUT_SAMEDIR = "samedir"      # 每个压缩包解到它自己所在目录下的子目录

OUTPUT_MODES: tuple[str, ...] = (OUTPUT_WORKDIR, OUTPUT_SAMEDIR)

# 拖入即开始的行为档位
AUTORUN_OFF = "off"             # 不自动，必须点「开始解压」
AUTORUN_DIRECT = "direct"       # 拖入即开始（用户要的默认行为）
AUTORUN_CONFIRM = "confirm"     # 拖入先弹确认（包多/包大时防误拖）

AUTORUN_MODES: tuple[str, ...] = (AUTORUN_OFF, AUTORUN_DIRECT, AUTORUN_CONFIRM)

# 完成后打开目录的目标
OPEN_NONE = "none"              # 不打开
OPEN_WORKDIR = "workdir"        # 打开隔离工作目录（旧行为）
OPEN_PATHS = "paths"            # 打开每个输出所在目录（推荐，配合 samedir 尤其自然）
OPEN_ITEMS: tuple[str, ...] = (OPEN_NONE, OPEN_WORKDIR, OPEN_PATHS)

OPEN_LABELS: dict[str, str] = {
    OPEN_NONE: "不打开",
    OPEN_WORKDIR: "隔离工作目录",
    OPEN_PATHS: "解压结果所在目录",
}

_CLAMP = {
    "max_depth": (1, 10),
    "max_total_gb": (1, 2000),
    "max_ratio": (1.0, 100000.0),
    # 上限 2000 而不是 200：这个值同时是"密码库能贡献多少条候选"的配额，
    # 而 vault.candidates_for 的单次扫描上限也是 2000（见 core/vault.py 的 _SCAN_CAP）
    # —— 两边对齐，才不会出现"配额允许 3000 条、库却只吐 2000 条"的隐形天花板。
    # 代价是线性的：每个候选都要让 7z 重新派生一次密钥，实测约 20 ms。
    # 2000 意味着单个需密码的包最坏约 40 秒（设置窗口会在控件下方实时显示这个估算）。
    "max_password_attempts": (1, 2000),
    "recent_inputs_limit": (0, 200),
    "session_days": (1, 365),
    "extract_timeout": (30, 86400),
}


def clamp_bounds(name: str) -> tuple[float, float] | None:
    """字段的合法区间 (下限, 上限)；未登记则返回 None。

    给 UI 层设控件范围用的。**范围只能有这一份定义**——两边各写一份的话，迟早
    出现"控件明明能调到 500、一保存却被夹回 200"的鬼打墙：用户的输入被静默吞掉，
    而且从界面上完全看不出是谁夹的（`normalize()` 在加载和保存前都会跑一次，
    连手改 config.json 都会被打回）。
    """
    return _CLAMP.get(name)


def sanitize_component(name: str) -> str:
    """把任意字符串压成单层目录名，防路径逃逸。

    顺序有讲究，三类危险必须都堵死：
    1. 分隔符先换掉（否则 "a/b" 会变成两层）；
    2. `:` 换掉——Windows 上 "C:foo" 会被 pathlib 认成盘符相对路径
       （anchor="C:"），拼进 Path 后产物会跳到源目录之外，是本函数最隐蔽的逃逸口；
    3. ".." 收成 "_"（否则 "../../etc" 替换后仍是 "_.._.._etc"），最后剥首尾点与空白。

    放在 appconfig 是因为它是本包最低层的配置模块，output_plan 与 normalize
    都从这里取用，避免两处各写一份消毒逻辑而漏项（曾因此漏掉盘符）。
    """
    cleaned = (name or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    while ".." in cleaned:
        cleaned = cleaned.replace("..", "_")
    cleaned = cleaned.strip().strip(".").strip()
    return cleaned or "output"


_SAVE_LOCK = threading.Lock()


def _replace_with_retry(src: str, dst: Path, attempts: int = 5) -> None:
    """os.replace 带短重试：Windows 上目标被并发 replace 短暂占用会抛 WinError 5。"""
    delay = 0.01
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 3


def _clamp(name: str, value) -> object:
    """把数值收敛到合法区间；非数值原样返回（由调用方类型校验兜底）。"""
    lo, hi = _CLAMP.get(name, (None, None))
    if lo is None:
        return value
    try:
        v = float(value)
    except (TypeError, ValueError):
        return value
    if name == "max_ratio":
        return min(max(v, lo), hi)
    return int(min(max(v, lo), hi))


@dataclass
class AppConfig:
    """全部用户偏好。字段增减需同步 _TYPE_HINTS，否则 from_dict 会丢弃。"""

    # --- 输出 ---
    # 落点与「是否复制回源目录」是两个独立维度：
    #   output_mode=samedir        → 直接解到源目录旁，无需复制
    #   output_mode=workdir + 复制 → 隔离解压，成功后复制一份回源目录（推荐默认）
    #   output_mode=workdir + 不复制 → 纯隔离，源目录一尘不染
    output_mode: str = OUTPUT_SAMEDIR
    copy_back_to_source: bool = True      # 仅 workdir 模式有意义
    workdir: str = ""                     # 空 = 默认 项目/.workspace
    subdir_name: str = "_解压开镜"          # samedir 模式的子目录名，避免污染原目录
    overwrite_existing: bool = False      # False = 同名目录自动改名，绝不覆盖既有产物

    # --- 启动 ---
    autorun_mode: str = AUTORUN_DIRECT    # 拖入即开始
    autorun_delay_ms: int = 1200          # 连续拖入合并窗口，拖完 N ms 才真正启动

    # --- 完成后 ---
    open_after: str = OPEN_PATHS

    # --- 解压参数 ---
    max_depth: int = 5
    max_total_gb: int = 50
    max_ratio: float = 1000.0
    max_password_attempts: int = 20
    # 密码来源（通常是站点域名）：用于派生候选密码（域名、www.域名）。
    # 留空 = 每个包各自从自己的文件名里识别来源。
    # 它不进 Config，而是每次 run 时作为 Pipeline.run(source=...) 传入——
    # 所以不进 as_overrides()，由调用方（ui/main_window.py）直接取用。
    password_source: str = ""
    sniff_archives: bool = True
    delete_intermediate: bool = True
    keep_original: bool = True
    # 已成功解压过（产物仍在）的包，重跑时跳过，避免 samedir 下堆出 "包名 (2)/(3)"
    skip_done: bool = True
    # 单个压缩包的解压超时（秒）。默认 3600 对超大包偏紧，被误杀时调大
    extract_timeout: int = 3600

    # --- 输入历史 ---
    remember_inputs: bool = True
    recent_inputs_limit: int = 30
    recent_inputs: list[str] = None      # type: ignore[assignment]
    restore_last_inputs: bool = False    # 启动时把上次的输入放回输入区（默认关，避免误触）

    # --- 其他 ---
    session_days: int = 30               # 任务记录保留天数（0 = 永久）

    def __post_init__(self) -> None:
        if self.recent_inputs is None:
            self.recent_inputs = []

    # ---------- 序列化 ----------

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "AppConfig":
        """宽容构造：未知键忽略、类型不符者退回默认值、枚举值越界则纠正。"""
        cfg = cls()
        if not isinstance(data, dict):
            return cfg

        by_name = {f.name: f for f in fields(cls)}
        for key, raw in data.items():
            f = by_name.get(key)
            if f is None or key == "recent_inputs":
                continue
            default = getattr(cfg, key)
            try:
                if isinstance(default, bool):
                    if not isinstance(raw, bool):
                        if isinstance(raw, str):
                            low = raw.strip().lower()
                            if low in ("true", "1", "yes", "on"):
                                raw = True
                            elif low in ("false", "0", "no", "off"):
                                raw = False
                            else:
                                continue
                        elif isinstance(raw, (int, float)):
                            raw = bool(raw)
                        else:
                            continue
                elif isinstance(default, int):
                    raw = int(raw)
                elif isinstance(default, float):
                    raw = float(raw)
                elif isinstance(default, str):
                    # None / 非字符串一律保留默认：否则 str(None) 会变成字面 "None"，
                    # 让 subdir_name 在源目录建出一个叫 None 的目录、workdir 变成数字串。
                    if not isinstance(raw, str):
                        continue
                    raw = raw
                else:
                    continue
            except (TypeError, ValueError):
                continue
            setattr(cfg, key, _clamp(key, raw))

        if isinstance(data.get("recent_inputs"), list):
            cfg.recent_inputs = [
                str(p) for p in data["recent_inputs"] if isinstance(p, (str, os.PathLike))
            ]

        cfg.normalize()
        return cfg

    def normalize(self) -> "AppConfig":
        """逐字段自我纠正（枚举值越界、路径空白、列表清洗）。原地生效并返回 self。"""
        if self.output_mode not in OUTPUT_MODES:
            self.output_mode = OUTPUT_SAMEDIR
        if self.autorun_mode not in AUTORUN_MODES:
            self.autorun_mode = AUTORUN_DIRECT
        if self.open_after not in OPEN_ITEMS:
            self.open_after = OPEN_PATHS

        self.workdir = (self.workdir or "").strip()
        self.password_source = (self.password_source or "").strip()
        # 子目录名消毒复用 output_plan 的同一份实现，避免两处各写一份而漏掉盘符。
        # 原值为空/纯非法字符时 sanitize 会给出兜底 "output"，这里换回用户可读的默认名。
        raw_sub = (self.subdir_name or "").strip()
        cleaned_sub = sanitize_component(raw_sub)
        self.subdir_name = "_解压开镜" if cleaned_sub == "output" else cleaned_sub

        self.autorun_delay_ms = int(max(0, min(10000, self.autorun_delay_ms)))
        for name in _CLAMP:
            setattr(self, name, _clamp(name, getattr(self, name)))

        seen: set[str] = set()
        clean: list[str] = []
        for p in self.recent_inputs or []:
            p = str(p).strip()
            if not p or p in seen:
                continue
            seen.add(p)
            clean.append(p)
        limit = self.recent_inputs_limit
        self.recent_inputs = clean[:limit] if limit else []
        return self

    # ---------- 派生 ----------

    def resolve_workdir(self, project_root: Path) -> Path:
        """隔离工作目录的绝对路径（workdir 为空则用 项目/.workspace）。"""
        if self.workdir:
            return Path(self.workdir).expanduser().resolve()
        return (Path(project_root) / ".workspace").resolve()

    def as_overrides(self, project_root: Path, workdir_override: Path | None = None) -> dict:
        """转成 core.config.Config.create(**overrides) 可用的字典。"""
        return {
            "workdir": Path(workdir_override) if workdir_override else self.resolve_workdir(project_root),
            "max_depth": self.max_depth,
            "max_total_uncompressed": self.max_total_gb * (1024 ** 3),
            "max_compression_ratio": self.max_ratio,
            "max_password_attempts": self.max_password_attempts,
            "sniff_archives": self.sniff_archives,
            "delete_intermediate": self.delete_intermediate,
            "keep_original": self.keep_original,
            "overwrite_existing": self.overwrite_existing,
            "skip_done": self.skip_done,
            "extract_timeout": float(self.extract_timeout),
        }

    def note_inputs(self, paths: list[str]) -> None:
        """把本次输入并入历史（去重、新的在前、按上限截断）。"""
        if not self.remember_inputs:
            return
        seen: set[str] = set()
        merged: list[str] = []
        for p in list(paths) + list(self.recent_inputs):
            p = str(p).strip()
            if not p or p in seen:
                continue
            seen.add(p)
            merged.append(p)
        limit = self.recent_inputs_limit
        self.recent_inputs = merged[:limit] if limit else []

    def with_changes(self, **kwargs) -> "AppConfig":
        """返回改过若干字段的新实例（不改自身），并重新 normalize。"""
        return replace(self, **kwargs).normalize()

    # ---------- 磁盘 IO ----------

    def save(self, path: Path) -> None:
        """原子落盘：先写同目录临时文件再 os.replace，断电也不会留下半截 JSON。

        并发安全：os.replace 在 Windows 上非幂等抗并发，目标被另一线程的 replace
        短暂占用时会抛 PermissionError(WinError 5)。故用模块级锁把「写临时文件 +
        replace」整段串行化，再叠加短重试兜底外部进程占用（如编辑器正在保存同名文件）。
        """
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True)
        with _SAVE_LOCK:
            fd, tmp = tempfile.mkstemp(
                prefix=path.name + ".", suffix=".tmp", dir=str(path.parent)
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                    f.write(payload + "\n")
                    f.flush()
                    os.fsync(f.fileno())
                _replace_with_retry(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        """读盘；文件不存在/损坏/为空一律退回默认配置，绝不抛给调用方。"""
        path = Path(path)
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return cls()
        try:
            data = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return cls()
        return cls.from_dict(data)

    @classmethod
    def ensure(cls, path: Path) -> "AppConfig":
        """load()，且首次运行时把默认配置写盘（让用户有文件可手改）。"""
        path = Path(path)
        if not path.exists():
            cfg = cls()
            try:
                cfg.save(path)
            except OSError:
                pass
            return cfg
        return cls.load(path)
