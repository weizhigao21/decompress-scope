"""解压开镜 core 引擎。

架构约束：本包及其子模块禁止导入任何 Qt 相关模块（PyQt*/PySide*），
由 tests/test_core_no_qt.py 静态守卫保证。
"""

__version__ = "0.3.1"
