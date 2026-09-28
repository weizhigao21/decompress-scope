"""GUI 启动入口。"""
from __future__ import annotations

import sys
from pathlib import Path


def _application_icon_path() -> Path:
    """返回源码和 PyInstaller 包内共用的应用图标路径。"""
    return Path(__file__).resolve().parent / "assets" / "app.ico"


def _apply_dark_palette(app) -> None:
    """原生控件（弹窗、菜单、输入框内部）也跟随暗色，避免白底刺眼。"""
    from PySide6.QtGui import QColor, QPalette

    from ui import theme

    pal = QPalette()
    pal.setColor(QPalette.Window, QColor(theme.CANVAS))
    pal.setColor(QPalette.WindowText, QColor(theme.TEXT))
    pal.setColor(QPalette.Base, QColor(theme.SURFACE))
    pal.setColor(QPalette.AlternateBase, QColor(theme.RAISED))
    pal.setColor(QPalette.Text, QColor(theme.TEXT))
    pal.setColor(QPalette.PlaceholderText, QColor(theme.TEXT_FAINT))
    pal.setColor(QPalette.Button, QColor(theme.RAISED))
    pal.setColor(QPalette.ButtonText, QColor(theme.TEXT))
    pal.setColor(QPalette.Highlight, QColor(theme.ACCENT))
    pal.setColor(QPalette.HighlightedText, QColor(theme.ACCENT_TEXT))
    pal.setColor(QPalette.ToolTipBase, QColor(theme.RAISED))
    pal.setColor(QPalette.ToolTipText, QColor(theme.TEXT))
    pal.setColor(QPalette.Link, QColor(theme.ACCENT))
    app.setPalette(pal)


def _create_application():
    try:
        from PySide6.QtGui import QIcon
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("PySide6 未安装: 请先执行  pip install PySide6")
        return 2

    from ui import theme

    app = QApplication(sys.argv)
    icon_path = _application_icon_path()
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    app.setStyle("Fusion")  # 保证 QSS 在 Windows 上完全生效
    _apply_dark_palette(app)
    app.setStyleSheet(theme.build_stylesheet())

    return app


def launch() -> int:
    from ui.main_window import MainWindow

    app = _create_application()
    if isinstance(app, int):
        return app
    win = MainWindow()
    win.show()
    return app.exec()


def launch_quick(paths: list[str]) -> int:
    """启动 Explorer 右键菜单调用的紧凑解压窗口。"""
    from ui.quick_extract_window import QuickExtractWindow

    app = _create_application()
    if isinstance(app, int):
        return app
    win = QuickExtractWindow(paths)
    win.show()
    return app.exec()
