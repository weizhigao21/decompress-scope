"""human_size 的边界与进位守卫。

体积数字是用户判断「这个包要不要解」的第一手依据，单位错了比不显示更糟。
这里把最容易出错的三个点钉死：单位切换阈值、小数位策略、四舍五入撞顶。
"""
from core.formatting import human_size


def test_bytes_keep_integer_no_unit():
    assert human_size(0) == "0 B"
    assert human_size(1) == "1 B"
    assert human_size(1023) == "1023 B"


def test_unit_switch_at_1024():
    """1024 进制（与资源管理器一致），不是 1000。"""
    assert human_size(1024) == "1.0 KB"
    assert human_size(1024 ** 2) == "1.0 MB"
    assert human_size(1024 ** 3) == "1.0 GB"


def test_small_values_keep_one_decimal():
    assert human_size(9500) == "9.3 KB"


def test_large_values_drop_decimal():
    """超过 10 个单位后小数位没有信息量，整数更清爽。"""
    assert human_size(345 * 1024 ** 2) == "345 MB"
    assert human_size(50 * 1024 ** 3) == "50 GB"


def test_rounding_up_does_not_overflow_unit():
    """1023.97 MB 四舍五入成 "1024 MB" 看着像溢出，必须进位成 1.0 GB。

    真实触发路径：一个 1.07 GB 的包算出 1023.97 MB。用户看到 "1024 MB" 会怀疑
    程序算错了——单位换算的经典陷阱。
    """
    assert human_size(int(1023.97 * 1024 ** 2)) == "1.0 GB"
    assert human_size(int(1023.6 * 1024)) == "1.0 MB"


def test_negative_and_garbage_fall_back_to_zero():
    """大小理论上不为负；真出现异常值也不能把 "−1 B" 这种鬼东西摆到界面上。"""
    assert human_size(-5) == "0 B"
    assert human_size(None) == "0 B"
    assert human_size("nonsense") == "0 B"


def test_terabyte_scale():
    assert human_size(2 * 1024 ** 4) == "2.0 TB"
