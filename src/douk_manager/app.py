from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if "--collector-worker" in arguments:
        from douk_manager.vendor.collector_server import main as collector_main

        sys.argv = [sys.argv[0], *[item for item in arguments if item != "--collector-worker"]]
        return collector_main()

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from douk_manager.gui import MainWindow

    app = QApplication([sys.argv[0], *arguments])
    app.setApplicationName("DouK全流程一体化管理器")
    app.setOrganizationName("loner913")
    window = MainWindow()
    window.show()
    QTimer.singleShot(0, window.begin_startup_check)
    return app.exec()
