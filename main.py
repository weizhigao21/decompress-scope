"""Windows GUI 可执行文件入口。

直接双击时启动主界面；Explorer 右键菜单传入 ``quick <路径>`` 时继续交给 CLI
分派到紧凑自动解压窗口。
"""
from __future__ import annotations

import sys

from cli import main


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:] or ["gui"]))
