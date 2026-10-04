# -*- coding: utf-8 -*-
"""屏幕捕获的 mss 兼容层。

mss 10 起把工厂函数 mss.mss 标记为弃用，改为类 mss.MSS；
9.x 只有前者。这里统一封装，避免各调用点各写一份兼容判断，
也避免把弃用警告散落到多处。

注意：本模块是唯一允许引用旧工厂名的地方（经 getattr 兜底），
tests/test_screen.py 有静态守卫防止它重新扩散到其它模块。
"""
from __future__ import annotations


def open_screen():
    """返回可直接用作上下文管理器的屏幕捕获实例。

    优先 `mss.MSS`（10+），回退 `mss.mss`（9.x）。
    """
    import mss

    factory = getattr(mss, "MSS", None)
    if factory is None:
        factory = getattr(mss, "mss", None)
    if factory is None:
        raise RuntimeError("当前 mss 版本既没有 MSS 也没有 mss，无法创建截屏实例")
    return factory()
