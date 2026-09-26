"""人类可读的数值格式化（纯函数，零 Qt）。

放进 core 而不是 ui：CLI 也要用它，而 CLI 顶层不允许导入 ui（会连带拉进
PySide6，让命令行工具凭空多一个重依赖）。
"""
from __future__ import annotations

import math

_STEP = 1024
_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def human_size(num_bytes) -> str:
    """把字节数格式化成像 "1.5 MB" / "345 MB" / "1.2 GB" 的短文本。

    三个刻意的取舍：

    - **1024 进制**：与 Windows 资源管理器一致。用 1000 的话用户拿显示值去
      核对文件属性会对不上，而我们服务的正是 Windows 用户。
    - **精度随量级递减**：不足 10 个单位保留 1 位小数（1.5 MB 有意义），
      超过 10 个就取整（"345.7 MB" 的小数位是噪声）。
    - **四舍五入撞顶要进位**：1023.97 MB 直接取整会显示成 "1024 MB"，
      看着像数值溢出。此时上提一级显示 "1.0 GB" 更符合直觉。

    非法输入（None/字符串/负数/NaN）统一给 "0 B"：这个函数的结果会直接
    渲染到界面上，"−1 B" 或抛异常都不该出现在用户眼前。
    """
    try:
        n = float(num_bytes)
    except (TypeError, ValueError):
        return "0 B"
    if not math.isfinite(n) or n <= 0:
        return "0 B"

    unit = 0
    while n >= _STEP and unit < len(_UNITS) - 1:
        n /= _STEP
        unit += 1

    digits = 1 if n < 10 else 0
    # 取整后可能刚好顶到 1024（1023.97 MB → "1024 MB"），再上提一级
    if round(n, digits) >= _STEP and unit < len(_UNITS) - 1:
        n /= _STEP
        unit += 1
        digits = 1 if n < 10 else 0

    if unit == 0:
        return f"{int(n)} {_UNITS[0]}"
    return f"{n:.1f} {_UNITS[unit]}" if digits else f"{n:.0f} {_UNITS[unit]}"


def human_count(n) -> str:
    """整数千分位（"1,234"）；非法输入给 "0"。"""
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return "0"
