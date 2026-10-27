"""修复解压出来的乱码文件名（ZIP 条目里的非 UTF-8 文件名）。

## 问题从哪来

ZIP 规范里条目文件名默认使用「创建者所在系统的本地代码页」，只有 general
purpose bit 11（UTF-8 标志）置位时才保证是 UTF-8。日文资源包大量由 Shift-JIS
环境的工具生成，其中相当一部分既不置这个标志位、又用 Shift-JIS 写名字。
7z.exe 在中文 Windows 上对这类条目按**系统代码页 936（GBK）**解释字节，落盘
就成了：

    日本語のファイル名.mp3  →  擔杮岅偺僼傽僀儖柤.mp3

实测（7-Zip 26.04，`chcp` = 936）：同一个包只要带 UTF-8 标志就完全正常。所以
用户看到的是「有概率乱码」—— 取决于那个包是谁用什么工具打的。

7z 没有给 `x` 模式提供指定源码页的开关：`-mcp=932` 在 `x` 上直接以非零码退出
（它只对创建压缩包有效），系统代码页也没法只给一个子进程改。所以只能在解压
之后按名改回来。

## 怎么反推

乱码本身就是「原始字节被 GBK 解码」的结果，反推就是把它编码回去再按原编码解：

    raw  = 乱码.encode("gbk")                      # 拿回原始字节
    text = raw.decode("utf-8" / "cp932" / "euc-jp")  # 按原编码严格解一次

严格模式很关键：utf-8 的校验极严，GBK 双字节几乎不可能凑成合法 UTF-8 序列，
所以 utf-8 这一档几乎不会误判。

## 为什么判定必须保守

把正常中文名改成日文乱码，比不修更糟 —— 用户不但要面对乱码，还失去了「按
原名去找原包」的线索。所以这里有两道硬闸门，宁可漏修不可错修：

1. **只在名字「看起来不像正常文本」时才动手**：含汉字，且落在 GB2312 常用字
   区之外的汉字占比很高；同时假名占比要低（正常日文名有假名兜着，不会触发）。
   实测覆盖率：正常中文名 100%；Shift-JIS 乱码 0%；UTF-8 乱码 0–10%；
   正常日文名「日本語のファイル名」的汉字部分 75%。
2. **修复结果不许比原样更差**：垃圾分必须严格下降，且不许新出现半角片假名。
   中文名被误当日文解码时，产物必然是 `ｲ簗ﾔﾎﾄｼ` 这种半角假名与汉字混排，
   这一道专门挡它。

另外两条「无法挽回就别碰」的短路：名字里出现 U+FFFD（替换字符）或半角 `?`
（Windows 文件名不允许 `?`，出现必是解码失败的替换产物）说明字节已经丢了，
怎么解都是猜 —— 直接放弃。
"""
from __future__ import annotations

import os
import zipfile
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# 判断顺序即优先级：utf-8 最严格、误判率最低；cp932 是日文 Windows 的实际编码
# （比 shift_jis 多 NEC/IBM 选字，覆盖面更广）；euc-jp 只用于少数 Unix 侧工具。
_TARGET_CODECS: tuple[tuple[str, str], ...] = (
    ("utf-8", "UTF-8"),
    ("cp932", "Shift-JIS"),
    ("euc-jp", "EUC-JP"),
)

# Windows 文件名里不允许出现的字符。反推出的名字若含这些字符，说明解错了
# （cp932 的 0x5C 解出来就是反斜杠，直接拿去 rename 会被当成路径分隔符）。
_ILLEGAL = set('\\/:*?"<>|')


@lru_cache(maxsize=16384)
def _in_gb2312(ch: str) -> bool:
    """这个字符是否落在 GB2312 常用字/符号区（覆盖 6763 个汉字 + 常用标点）。"""
    try:
        ch.encode("gb2312")
    except (UnicodeEncodeError, UnicodeError):
        return False
    return True


def _garbage_score(text: str) -> int:
    """越大越不像正常文本。只用来做「修复前后谁更差」的相对比较，不是绝对标准。"""
    score = 0
    for ch in text:
        cp = ord(ch)
        if cp == 0xFFFD:                      # 替换字符：字节已丢
            score += 30
        elif 0xFF61 <= cp <= 0xFF9F:          # 半角片假名：日文乱码的典型指纹
            score += 6
        elif 0xE000 <= cp <= 0xF8FF:          # 私用区
            score += 10
        elif 0xF900 <= cp <= 0xFAFF:          # 兼容汉字
            score += 4
        elif 0x3400 <= cp <= 0x4DBF:          # 扩展 A
            score += 2
        elif cp >= 0x20000:                   # 扩展 B 及以后
            score += 3
        elif 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:   # 谚文
            score += 3
        elif 0x4E00 <= cp <= 0x9FFF and not _in_gb2312(ch):
            # 生僻字（落在 GB2312 常用字区之外）。Shift-JIS / UTF-8 的字节被按
            # GBK 解时，落点绝大多数在这里 —— 它才是乱码最本质的特征，光看
            # 「码位在不在扩展区」是抓不到的（生僻字仍在基本区）。
            score += 2
    return score


def _counts(text: str) -> tuple[int, int, int, int]:
    """返回 (汉字数, GB2312 内汉字数, 全角假名数, 半角假名数)。"""
    han = common = kana = half = 0
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF:
            han += 1
            if _in_gb2312(ch):
                common += 1
        elif 0x3040 <= cp <= 0x30FF:
            kana += 1
        elif 0xFF61 <= cp <= 0xFF9F:
            half += 1
    return han, common, kana, half


def looks_like_mojibake(name: str) -> bool:
    """这个文件名是否可能是「非本地区码的字节被按 GBK 解」的产物。

    保守到近乎吝啬：返回 False 只是「不值得冒险去改」，不代表名字一定正常。
    """
    if not name or name.isascii():
        return False
    if "\ufffd" in name or "?" in name:
        # 字节已经丢了（解码失败的替换产物），怎么解都是猜
        return False
    han, common, kana, half = _counts(name)
    if half:
        # 半角假名夹在文件名里：要么本来就是日文全角名被解坏，要么是中文名被
        # 当日文解过一道。两种情况都值得往下试 —— 能不能修由 _plausible 决定。
        return True
    if not han:
        return False
    # 汉字为主（假名少于汉字的 1/3）且生僻字过半 → 不像人写的名字
    if kana * 3 < han and common * 2 < han:
        return True
    return False


def _plausible(candidate: str, original: str) -> bool:
    """修复结果是否比原样可信。任何一条不满足就放弃这一档编码。"""
    if not candidate or candidate == original:
        return False
    if "\ufffd" in candidate:
        return False
    if any(ch in _ILLEGAL for ch in candidate):
        return False
    if any(ord(ch) < 0x20 for ch in candidate):
        return False
    if candidate.endswith(" ") or candidate.endswith("."):
        return False
    _, _, _, new_half = _counts(candidate)
    _, _, _, old_half = _counts(original)
    if new_half > old_half:
        # 不许把「看着奇怪」修成「全是半角片假名」——那正是误判的指纹
        return False
    if _garbage_score(candidate) >= _garbage_score(original):
        return False
    han, common, kana, _ = _counts(candidate)
    if han and not kana and common * 2 < han:
        # 解出来还是生僻字堆，说明这一档编码不对
        return False
    return True


@lru_cache(maxsize=8192)
def _repair_cached(name: str) -> tuple[str, str]:
    if not looks_like_mojibake(name):
        return name, ""
    for source in ("gbk", "gb18030"):
        try:
            raw = name.encode(source)
        except (UnicodeEncodeError, UnicodeError):
            continue
        for codec, label in _TARGET_CODECS:
            try:
                candidate = raw.decode(codec)
            except (UnicodeDecodeError, UnicodeError):
                continue
            if _plausible(candidate, name):
                return candidate, label
    return name, ""


def repair_name(name: str) -> str:
    """修一个文件名（含扩展名整段处理）。修不动就原样返回。"""
    return _repair_cached(name)[0]


def repair_encoding(name: str) -> str:
    """修这个名字用到了哪种编码（用于向用户交代）；修不动时为空串。"""
    return _repair_cached(name)[1]


@dataclass
class NameRepair:
    """一次树修复的结果，供界面/CLI 汇报。"""

    renamed: int = 0
    encoding: str = ""
    samples: list[tuple[str, str]] = field(default_factory=list)

    def summary(self) -> str:
        """给界面列用的最短一句：只讲"修了几个"。

        任务树信息列在默认 1000px 窗口下只有约 190px，带上「（Shift-JIS）」
        要 203px，整句会被 elide 成省略号——连"修了几个"都看不全（用户反馈过
        主界面这里看不见）。所以编码与例子一律留给 describe() 走 tooltip。
        """
        if not self.renamed:
            return ""
        return f"已修正 {self.renamed} 个乱码文件名"

    def describe(self) -> str:
        """带编码与例子的一句，给 CLI 报告 / tooltip 这种有横向空间的地方。"""
        head = self.summary()
        if not head:
            return ""
        if self.encoding:
            head += f"（{self.encoding}）"
        if self.samples:
            old, new = self.samples[0]
            head += f"，例如 {old} → {new}"
        return head


def _unique_target(parent: Path, name: str) -> Path | None:
    """目标名已被占用时挪一个后缀，绝不允许覆盖已有产物。"""
    target = parent / name
    if not target.exists() and not target.is_symlink():
        return target
    stem, suffix = os.path.splitext(name)
    for index in range(2, 1000):
        candidate = parent / f"{stem} ({index}){suffix}"
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    return None


def _repair_bytes(raw: bytes, shown: str) -> tuple[str, str]:
    """用**原始字节**解码出正确名字。shown 是按 cp936 解出来的（也就是磁盘上的样子）。

    比 `_repair_cached` 强的地方：不依赖「把乱码再 encode 回字节」。当 7z 解码
    时插过替换字符（`?` / U+FFFD）时，磁盘名里的字节已经不可逆地错了，而这里
    的 raw 直接来自 ZIP 中央目录，一个字节都没丢。
    """
    if not looks_like_mojibake(shown):
        return shown, ""
    for codec, label in _TARGET_CODECS:
        try:
            candidate = raw.decode(codec)
        except (UnicodeDecodeError, UnicodeError):
            continue
        if _plausible(candidate, shown):
            return candidate, label
    return shown, ""


def _bump(result: NameRepair, encodings: dict[str, int],
          old: str, new: str, label: str) -> None:
    result.renamed += 1
    if label:
        encodings[label] = encodings.get(label, 0) + 1
    if len(result.samples) < 5:
        result.samples.append((old, new))


def _plan_from_zip(archive: Path, root: Path) -> list[tuple[int, Path, str, str, str]]:
    """从 ZIP 中央目录算出改名计划：(深度, 现有路径, 新名, 旧名, 编码)。

    中央目录是明文，连 AES-256 加密的包也能读 —— 加密只作用于文件数据，文件名
    不在其中。名字在 flag bit 11（UTF-8 标志）未置位时由 `zipfile` 按 cp437 解码，
    cp437 是逐字节全映射，`orig_filename.encode("cp437")` 能**无损**拿回原始字节。

    对齐校验：`raw.decode("cp936")` 必须恰好等于磁盘上的名字。对不上说明 7z 走的
    不是 CP936（插了替换字符、或把非法字符改写掉了），这条就跳过 —— 猜错名字去
    rename 是危险的，宁可退回启发式。
    """
    try:
        with zipfile.ZipFile(archive) as zf:
            infos = zf.infolist()
    except Exception:
        # 不是 zip（7z/rar）、分卷首卷、包损坏：这条路不适用
        return []

    planned: list[tuple[int, Path, str, str, str]] = []
    seen: set[str] = set()
    for info in infos:
        if info.flag_bits & 0x800:
            continue                       # 带 UTF-8 标志：7z 本来就解对了
        try:
            raw = info.orig_filename.encode("cp437")
        except UnicodeEncodeError:
            continue
        segments = [seg for seg in raw.split(b"/") if seg]
        if not segments or any(b"\\" in seg for seg in segments):
            # 空名，或名字里自带反斜杠（7z 会当分隔符，与 zip 语义不一致）
            continue
        old_parts: list[str] = []
        new_parts: list[str] = []
        labels: list[str] = []
        usable = True
        for segment in segments:
            try:
                shown = segment.decode("cp936")
            except UnicodeDecodeError:
                usable = False
                break
            old_parts.append(shown)
            fixed, label = _repair_bytes(segment, shown)
            new_parts.append(fixed)
            labels.append(label)
        if not usable or old_parts == new_parts:
            continue
        old_rel = os.path.join(*old_parts)
        if old_rel in seen:
            continue
        seen.add(old_rel)
        path = root / old_rel
        if not (path.exists() or path.is_symlink()):
            continue                       # 7z 跳过了这条（越界路径等）
        # 只改**当前这一级**的名字，父级留给更晚（深度更小）的那一轮 —— 父目录
        # 一改名，子项会自动跟过去，所以这里绝不能拼出「新父名/新子名」的路径。
        planned.append((len(segments), path, new_parts[-1], old_parts[-1], labels[-1]))
    return planned


def _from_zip(archive: Path, root: Path, result: NameRepair,
              encodings: dict[str, int]) -> None:
    planned = _plan_from_zip(archive, root)
    # 深度降序：先把深层改完，再改它所在目录的名字（子项随之移动）
    planned.sort(key=lambda item: -item[0])
    for _, path, new_name, old_name, label in planned:
        if new_name == old_name or not new_name:
            continue
        if not path.parent.is_dir():
            continue
        if not (path.exists() or path.is_symlink()):
            continue
        target = _unique_target(path.parent, new_name)
        if target is None:
            continue
        try:
            path.rename(target)
        except OSError:
            continue
        _bump(result, encodings, old_name, new_name, label)


def _from_heuristic(root: Path, result: NameRepair,
                    encodings: dict[str, int], limit: int) -> None:
    """兜底：不依赖压缩包，直接从磁盘上的乱码名反推（7z/rar/分卷/中央目录对不上）。

    为什么要自己收集再按深度排序，而不是直接 `os.walk(topdown=False)` 边遍历
    边改名：改了深层目录的名字之后，父级那一轮拿到的 `dirnames` 还是**旧名**，
    接着对旧路径 rename 就会失败（异常被吞掉就是「有些目录没修」）。先收集成
    静态列表、按深度降序处理，父目录一定在子项都处理完之后才被改名。
    """
    entries: list[tuple[int, Path]] = []
    for dirpath, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(dirpath)
        depth = len(base.relative_to(root).parts)
        for name in filenames:
            entries.append((depth, base / name))
        for name in dirnames:
            entries.append((depth, base / name))
        if len(entries) > limit:
            break
    entries.sort(key=lambda item: -item[0])

    for _, path in entries:
        parent = path.parent
        name = path.name
        new_name, label = _repair_cached(name)
        if new_name == name or not new_name:
            continue
        if not parent.is_dir():
            continue
        if not (path.exists() or path.is_symlink()):
            continue
        target = _unique_target(parent, new_name)
        if target is None:
            continue
        try:
            path.rename(target)
        except OSError:
            continue
        _bump(result, encodings, name, new_name, label)


def repair_tree(root: Path, *, archive: Path | None = None,
                limit: int = 200_000) -> NameRepair:
    """把 root 之下所有乱码名改回来。

    两条路，先确定后启发：

    1. **ZIP 中央目录**（`archive` 给定时）：原始文件名字节一个都没丢，能精确
       还原，连 7z 已经插过替换字符的那种也救得回来。
    2. **启发式反推**：处理 7z/rar、分卷首卷、中央目录与磁盘对不上的情况；也用来
       收拾 ZIP 那条路没覆盖到的残余（比如包内自带的乱码目录项）。

    两条路都要求「改完比原来更像正常文本」，宁可漏修也绝不把好名字改坏。
    """
    root = Path(root)
    result = NameRepair()
    if not root.is_dir():
        return result
    encodings: dict[str, int] = {}
    if archive is not None:
        _from_zip(Path(archive), root, result, encodings)
    _from_heuristic(root, result, encodings, limit)
    if encodings:
        result.encoding = max(encodings.items(), key=lambda kv: kv[1])[0]
    return result
