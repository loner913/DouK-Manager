from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from douk_manager.startup import StartupState
from douk_manager.ui.shell.main_window import ModernMainWindow


class ModernOverviewTests(unittest.TestCase):
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
        for timer_name in ("_modern_status_timer", "poll_timer"):
            timer = getattr(window, timer_name, None)
            if timer is not None:
                timer.stop()
        overview = getattr(window, "modern_overview", None)
        if overview is not None:
            overview._timer.stop()
            overview._clock_timer.stop()
        window.hide()
        window.deleteLater()
        self.app.processEvents()
        logger = getattr(window.controller, "logger", None)
        if logger is not None:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
        self._home_patch.stop()

    def test_overview_wraps_original_v016_controls_instead_of_recreating_them(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))

            self.assertIs(window.tabs.widget(0), window.modern_overview)
            self.assertTrue(
                window._legacy_overview_page.isAncestorOf(window.startup_recheck_button)
            )
            self.assertTrue(
                window._legacy_overview_page.isAncestorOf(window.startup_log_button)
            )
            self.assertTrue(
                window._legacy_overview_page.isAncestorOf(window.startup_details)
            )
            self._dispose_window(window)

    def test_metric_cards_render_only_values_from_real_dashboard_snapshot_contract(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            snapshot = SimpleNamespace(
                fingerprint=SimpleNamespace(
                    normalized_path="task.log",
                    size=123,
                    mtime_ns=456,
                ),
                planned_count=12,
                started_count=10,
                completed_with_anomaly_count=2,
                not_started_count=2,
                complete=False,
                reliable=False,
                reliability_reasons=("任务被中断",),
                main_status_counts=(),
                pre_start_error_count=0,
                task_log=Path("DownloadTask_demo.log"),
                ended_at=None,
                duration_seconds=45,
            )

            window.modern_overview._refresh_snapshot(snapshot)

            self.assertEqual(
                window.modern_overview.metrics["planned"].value_label.text(), "12"
            )
            self.assertEqual(
                window.modern_overview.metrics["started"].value_label.text(), "10"
            )
            self.assertEqual(
                window.modern_overview.metrics["complete"].value_label.text(), "未完整"
            )
            self.assertEqual(
                window.modern_overview.metrics["reliable"].value_label.text(), "需复核"
            )
            self.assertEqual(
                window.modern_overview.metrics["anomaly"].value_label.text(), "2"
            )
            self.assertEqual(
                window.modern_overview.metrics["not_started"].value_label.text(), "2"
            )
            self._dispose_window(window)

    def test_overview_requests_existing_dashboard_pipeline_after_startup_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            window.controller.startup_state = StartupState.READY
            window.refresh_result_dashboard = Mock()
            window._dashboard_snapshot = None
            window.modern_overview._dashboard_refresh_requested = False

            window.modern_overview.request_real_dashboard_refresh()

            window.refresh_result_dashboard.assert_called_once_with(auto_refresh=True)
            self._dispose_window(window)

    def test_read_only_protection_is_presented_as_safe_mode_without_auto_expanding_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            window.controller.startup_state = StartupState.DEGRADED_READ_ONLY
            window.modern_overview.set_diagnostics_expanded(False)

            window.modern_overview._refresh_startup(window)

            self.assertEqual(window.modern_overview.startup_badge.text(), "只读保护")
            self.assertFalse(window.modern_overview._diagnostics_expanded)
            self.assertFalse(window._legacy_overview_page.isVisible())
            self.assertIn("安全运行模式", window.modern_overview.startup_badge.toolTip())
            self._dispose_window(window)


if __name__ == "__main__":
    unittest.main()
