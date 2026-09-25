"""静态守卫：core/ 禁止导入任何 Qt 模块（PyQt*/PySide*）。"""
import re
from pathlib import Path

CORE_DIR = Path(__file__).resolve().parent.parent / "core"
_BANNED = re.compile(r"^\s*(?:from|import)\s+(?:PyQt\d?|PySide\d?)\S*", re.M)


def test_core_must_not_import_qt():
    offenders = []
    for py in sorted(CORE_DIR.rglob("*.py")):
        if _BANNED.search(py.read_text(encoding="utf-8")):
            offenders.append(py.name)
    assert not offenders, f"core/ 禁止导入 Qt 模块，违规文件: {offenders}"
