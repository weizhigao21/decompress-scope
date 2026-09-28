"""密码现场提取与候选生成。

候选优先级：文件名/提示正则 → 来源域名派生 → 密码库(按命中排序) → 内置字典。
顺序即优先级，见 build_candidates 的说明——来源派生必须排在库之前，
否则库满配额时高命中率的来源密码会被截断切掉。
"""
from __future__ import annotations

import re

# 文件名中的密码提示: [密码:xxx] （密码：xxx） password:xxx 等
_BRACKET_PATTERNS = (
    re.compile(
        r"[\[【（(]\s*(?:密码|密碼|password|passwd|pw|pass)\s*[:：=]?\s*([^\]】）)]{1,64})[\]】）)]",
        re.I,
    ),
    re.compile(r"(?:密码|密碼|password|passwd)\s*[:：=]\s*([A-Za-z0-9@#$_&+\-.~^]{3,64})", re.I),
)
# 站点来源(域名) TLD 集合
_DOMAIN_TLDS = (
    r"(?:com|net|org|cc|me|xyz|top|vip|club|info|io|cn|tv|us|co|la|moe|fun|site|shop|link|icu|biz|asia)"
)
# 完整校验: 一串标签 + 已知 TLD, 如 www.abc.com / moe123.net
_STRICT_DOMAIN_RE = re.compile(
    rf"^[a-z0-9][a-z0-9\-]*(?:\.[a-z0-9][a-z0-9\-]*)*\.{_DOMAIN_TLDS}$",
    re.I,
)
# 宽松兜底: 单标签 + TLD
_LOOSE_DOMAIN_RE = re.compile(rf"[a-z0-9][a-z0-9\-]{{1,30}}\.{_DOMAIN_TLDS}", re.I)
# 括号内容: [xxx] 【xxx】 （xxx） (xxx)
_BRACKET_RE = re.compile(r"[\[【（(]\s*([^\]】）)]{1,80})\s*[\]】）)]")

BUILTIN_DICT: tuple[str, ...] = (
    "123456", "1234", "12345678", "1234567890", "password", "admin", "88888888", "66666666",
)


def extract_passwords_from_name(name: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for pattern in _BRACKET_PATTERNS:
        for m in pattern.finditer(name):
            pwd = m.group(1).strip().strip("。.,，、 ")
            if pwd and pwd not in seen:
                seen.add(pwd)
                found.append(pwd)
    return found


def extract_source(name: str) -> str:
    """从文件名提取来源域名，优先取括号内完整域名，兜底宽松匹配。"""
    for m in _BRACKET_RE.finditer(name):
        content = m.group(1).strip()
        if _STRICT_DOMAIN_RE.match(content):
            return content.lower()
    m = _LOOSE_DOMAIN_RE.search(name)
    return m.group(0).lower() if m else ""


def derived_from_source(source: str) -> list[str]:
    """很多站点直接拿域名当密码，派生两个常见变体。"""
    if not source:
        return []
    return [source, f"www.{source}"]


def build_candidates(archive_name: str, source: str, vault_candidates: list[str]) -> list[str]:
    """按优先级合并所有密码候选，大小写去重。

    顺序即优先级，且**必须在调用方截断之前**就排好：
        文件名/提示 → 来源域名派生 → 密码库命中 → 内置字典

    来源派生之所以要排在密码库之前：调用方会做 `[:max_password_attempts]` 截断，
    而库候选的配额同样取自这个上限——库大时它会占满整个列表。若库排在前，
    「文件名提示」与「来源派生」这两类**这个包特有的线索**就会被通用猜测挤出，
    而它们恰恰是命中率最高的一档。
    库内条目已按「同来源 → 无来源 → 其他来源」分档、档内按 hit_count 排序
    （见 `PasswordVault.candidates_for`），同类命中仍然优先于内置字典。
    """
    candidates: list[str] = []
    seen: set[str] = set()

    def add(pwd: str) -> None:
        key = pwd
        if pwd and key not in seen:
            seen.add(key)
            candidates.append(pwd)

    for pwd in extract_passwords_from_name(archive_name):
        add(pwd)
    for pwd in derived_from_source(source):
        add(pwd)
    for pwd in vault_candidates:
        add(pwd)
    for pwd in BUILTIN_DICT:
        add(pwd)
    return candidates
