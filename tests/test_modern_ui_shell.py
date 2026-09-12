from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from douk_manager.gui import MainWindow as LegacyMainWindow
from douk_manager.ui.shell.main_window import ModernMainWindow


class ModernUiShellTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, root: Path) -> ModernMainWindow:
        self._home_patch = patch.dict(os.environ, {"DOUK_MANAGER_HOME": str(root)})
        self._home_patch.start()
        window = ModernMainWindow()
        window.show()
        self.app.processEvents()
        return window

    def _dispose_window(self, window: ModernMainWindow) -> None:
        timer = getattr(window, "_modern_status_timer", None)
        if timer is not None:
            timer.stop()
        window.poll_timer.stop()
        window.hide()
        window.deleteLater()
        self.app.processEvents()
        logger = getattr(window.controller, "logger", None)
        if logger is not None:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
        self._home_patch.stop()

    def test_shell_wraps_the_existing_v016_pages_without_recreating_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            self.assertIsInstance(window, LegacyMainWindow)
            self.assertIsNone(window._modern_shell_install_error)
            self.assertEqual(window.tabs.count(), 10)
            self.assertIs(window.tabs.widget(window.audit_tab_index), window.account_audit_page)
            self.assertIs(window.tabs.widget(window.result_tab_index), window.result_page)
            self.assertIs(window.tabs.widget(window.dashboard_tab_index), window.dashboard_page)
            self.assertFalse(window.tabs.tabBar().isVisible())
            self.assertIs(window.centralWidget(), window._modern_root)
            self._dispose_window(window)

    def test_sidebar_routes_through_the_original_qtabwidget(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            target = window.result_tab_index
            window.modern_sidebar.route_requested.emit(window._route_for_index(target))
            self.app.processEvents()
            self.assertEqual(window.tabs.currentIndex(), target)

            window.tabs.setCurrentIndex(window.dashboard_tab_index)
            self.app.processEvents()
            button = window.modern_sidebar._buttons[
                window._route_for_index(window.dashboard_tab_index)
            ]
            self.assertTrue(button.isChecked())
            self._dispose_window(window)

    def test_new_header_only_mirrors_existing_status_widgets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            window.global_collector_status.setText("运行中")
            window.global_engine_status.setText("后台监听")
            window.status_labels["database"].setText("正常")
            window.status_labels["volume"].setText("正常")
            window._sync_modern_status()

            self.assertEqual(window.modern_header.collector_status.value.text(), "运行中")
            self.assertEqual(window.modern_header.download_status.value.text(), "后台监听")
            self.assertEqual(window.modern_header.database_status.value.text(), "正常")
            self.assertEqual(window.modern_header.volume_status.value.text(), "正常")
            self._dispose_window(window)

    def test_startup_and_business_methods_remain_inherited_from_v016(self) -> None:
        self.assertIs(ModernMainWindow.begin_startup_check, LegacyMainWindow.begin_startup_check)
        self.assertIs(ModernMainWindow.apply_startup_result, LegacyMainWindow.apply_startup_result)
        self.assertIs(ModernMainWindow.closeEvent, LegacyMainWindow.closeEvent)


if __name__ == "__main__":
    unittest.main()
