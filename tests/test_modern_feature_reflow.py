from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QFrame, QSizePolicy

from douk_manager.ui.pages.legacy_page import ModernLegacyPage
from douk_manager.ui.shell.main_window import ModernMainWindow


class ModernFeatureReflowTests(unittest.TestCase):
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
        overview = getattr(window, "modern_overview", None)
        if overview is not None:
            overview._timer.stop()
            overview._clock_timer.stop()
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

    def test_every_feature_page_gets_its_reflow_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            for label, wrapper in window.modern_feature_pages.items():
                self.assertIsInstance(wrapper, ModernLegacyPage)
                self.assertIsNone(
                    wrapper.layout_reflow_error,
                    f"{label} reflow failed: {wrapper.layout_reflow_error}",
                )
                self.assertEqual(wrapper.layout_profile, label)
                self.assertTrue(wrapper.legacy_page.property("legacyReflowed"))
                cards = [
                    frame
                    for frame in wrapper.legacy_page.findChildren(QFrame)
                    if frame.property("legacySectionCard")
                ]
                self.assertGreater(len(cards), 0, f"{label} has no modern section cards")
                self.assertTrue(
                    all(card.property("legacyGeometryPolished") for card in cards),
                    f"{label} has a section card without Windows geometry polish",
                )
            self._dispose_window(window)

    def test_reflow_keeps_original_v016_controls_inside_original_pages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            controls = {
                "账号任务": window.task_expression,
                "账号审计": window.account_audit_table,
                "批次生成": window.batch_start,
                "下载队列": window.task_list,
                "账号采集": window.collector_output,
                "截图与索引": window.post_output,
                "下载结果": window.result_table,
                "结果看板": window.dashboard_account_table,
                "设置": window.engine_edit,
            }
            for label, control in controls.items():
                wrapper = window.modern_feature_pages[label]
                self.assertTrue(
                    wrapper.legacy_page.isAncestorOf(control),
                    f"{label} control was recreated or detached from the original page",
                )
            self._dispose_window(window)

    def test_compact_forms_do_not_absorb_tall_window_whitespace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            task_group = window.task_expression.parentWidget()
            self.assertIsNotNone(task_group)
            self.assertEqual(
                task_group.sizePolicy().verticalPolicy(),
                QSizePolicy.Policy.Maximum,
            )
            self.assertEqual(
                window.task_output.sizePolicy().verticalPolicy(),
                QSizePolicy.Policy.Expanding,
            )
            self.assertEqual(
                window.result_table.sizePolicy().verticalPolicy(),
                QSizePolicy.Policy.Expanding,
            )
            self._dispose_window(window)

    def test_reflowed_pages_remain_visible_when_navigated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            for index in range(1, window.tabs.count()):
                window.tabs.setCurrentIndex(index)
                self.app.processEvents()
                wrapper = window.tabs.widget(index)
                self.assertIsInstance(wrapper, ModernLegacyPage)
                self.assertTrue(wrapper.isVisible())
                self.assertTrue(wrapper.legacy_page.isVisibleTo(wrapper.surface))
            self._dispose_window(window)


if __name__ == "__main__":
    unittest.main()
