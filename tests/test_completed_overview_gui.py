import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PySide6.QtCore import QCoreApplication, QEvent, Qt
from PySide6.QtWidgets import QApplication, QLabel
from douk_manager.background import TaskSpec
from douk_manager.config import AppConfig
from douk_manager.startup import StartupState
from douk_manager.ui.shell.main_window import ModernMainWindow
from test_completed_overview_data import write_task


class CompletedOverviewGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def settle(self, window):
        for i in range(500):
            self.app.processEvents()
            time.sleep(.005)
            if i > 5 and not window._background_bindings:
                return
        self.fail("background did not settle")

    def test_log_updates_empty_state_and_bound_detail(self):
        runtime = Path(__file__).resolve().parents[2] / "ui-modernization-preview-runtime"
        with tempfile.TemporaryDirectory(dir=runtime) as directory:
            home = Path(directory)
            logs = home / "Logs" / "DownloadTasks"
            end = datetime.now().replace(microsecond=0)
            task = write_task(logs, "one", end)
            config = AppConfig(engine_exe=str(home/'Engine/main.exe'), video_root=str(home/'Video'),
                               index_root=str(home/'Index'), old_screenshot_dir=str(home/'Screenshots'))
            env = {
                "DOUK_MANAGER_HOME": str(home),
                "DOUK_MANAGER_UI_STATE_PATH": str(home / "state.ini"),
                "DOUK_MANAGER_PREVIEW_MOCK_DATA": "0",
                "DOUK_MANAGER_PREVIEW_LOG_DATA": "0",
            }
            with patch.dict(os.environ, env), patch.object(AppConfig, "load", return_value=config):
                window = ModernMainWindow()
                window.show()
                try:
                    window.controller.startup_state = StartupState.READY
                    window.modern_overview.refresh_from_host()
                    self.settle(window)
                    page = window.modern_overview
                    self.assertIsNone(window._modern_shell_install_error)
                    self.assertEqual(
                        page.queue_card.findChild(QLabel, "modernSectionTitle").text(),
                        "上次任务摘要",
                    )
                    self.assertEqual(
                        page.attention_card.findChild(QLabel, "modernSectionTitle").text(),
                        "上次任务异常",
                    )
                    self.assertTrue(
                        page.composition_legend.alignment()
                        & Qt.AlignmentFlag.AlignTop
                    )
                    self.assertLessEqual(
                        page.composition_legend.geometry().top(),
                        page.donut_chart.geometry().top(),
                    )
                    self.assertEqual(page.throughput_value.text(), "8.0 账号/分钟")
                    self.assertIn("98.1%", page.composition_legend.text())
                    for color in page.donut_chart._COLOURS[:6]:
                        self.assertIn(color, page.composition_legend.text())
                    for theme in range(2):
                        for width, height in ((1680, 1050), (2560, 1400), (1100, 800)):
                            window.resize(width, height)
                            page.attention_filter.setCurrentIndex(0)
                            self.settle(window)
                            positions = (page.attention_note.pos(), page.attention_filter.pos(),
                                         page.dashboard_button.pos())
                            for key in ("interrupted", "pre_start_error"):
                                page.attention_filter.setCurrentIndex(page.attention_filter.findData(key))
                                self.settle(window)
                                self.assertEqual(page.attention_page_label.text(), "0 / 0")
                                self.assertTrue(page.attention_value.isVisible())
                                self.assertIs(page.attention_value.parentWidget(), page.attention_body)
                                self.assertTrue(page.attention_body.rect().contains(page.attention_value.geometry()))
                                self.assertEqual(positions, (page.attention_note.pos(),
                                    page.attention_filter.pos(), page.dashboard_button.pos()))
                            page.attention_filter.setCurrentIndex(0)
                            self.settle(window)
                            self.assertFalse(page.attention_value.isVisible())
                        window._modern_theme.toggle()
                    visited = set()
                    while True:
                        visited.update(label.text() for label in page.attention_card.findChildren(
                            type(page.attention_value), "modernAttentionTitle") if label.isVisible())
                        if not page.attention_next.isEnabled():
                            break
                        page.attention_next.click()
                        self.app.processEvents()
                    self.assertEqual(len(visited), 31)
                    self.assertIn("31", page.attention_page_label.text())
                    page.attention_filter.setCurrentIndex(page.attention_filter.findData("error"))
                    self.assertEqual(page.attention_page_label.text(), "1–5 / 23")
                    page.attention_filter.setCurrentIndex(page.attention_filter.findData("anomaly"))
                    self.assertEqual(page.attention_page_label.text(), "1–5 / 8")
                    page.attention_next.click()
                    page.refresh_button.click()
                    self.settle(window)
                    self.assertEqual(page.attention_page_label.text(), "6–8 / 8")
                    page.attention_filter.setCurrentIndex(0)
                    page.queue_button.click()
                    self.settle(window)
                    self.assertEqual(window._dashboard_snapshot.task_log, task)
                    # A synthetic summary terminal event exercises the real coordinator hook.
                    window._submit_background(TaskSpec(task_type="download_summary", display_name="synthetic"),
                        lambda _token: str(write_task(logs, "one", end, count=600)))
                    self.settle(window)
                    self.assertEqual(page.throughput_value.text(), "4.0 账号/分钟")
                    with patch.object(window.controller.result_dashboard, "task_index", side_effect=OSError("fixture")):
                        page.refresh_button.click()
                        self.settle(window)
                    self.assertEqual(page._completed.started_count, 600)
                    self.assertIn("刷新失败", page.subtitle.text())
                    write_task(logs, "one", end, count=10, anomalies=0,
                               distribution=(2, 1, 1, 1, 2, 3), exit_code=1,
                               pre_start_errors=(11, 12))
                    page.refresh_button.click()
                    self.settle(window)
                    window.navigate_to_page_label("总览")
                    for key, accounts in (("interrupted", {"A8", "A9", "A10"}),
                                          ("pre_start_error", {"A11", "A12"})):
                        page.attention_filter.setCurrentIndex(page.attention_filter.findData(key))
                        self.settle(window)
                        self.assertEqual(page.attention_page_label.text(), f"1–{len(accounts)} / {len(accounts)}")
                        self.assertEqual({label.text() for label in page.attention_card.findChildren(
                            type(page.attention_value), "modernAttentionTitle") if label.isVisible()}, accounts)
                    self.assertIn("30.0%", page.composition_legend.text())
                    task.unlink()
                    page.refresh_button.click()
                    self.settle(window)
                    self.assertIsNone(page._completed)
                    self.assertEqual(page.throughput_value.text(), "—")
                    self.assertFalse(page.queue_button.isEnabled())
                finally:
                    self.settle(window)
                    window.close()
                    logger = window.controller.logger
                    for handler in list(logger.handlers):
                        logger.removeHandler(handler)
                        handler.close()
                    window.deleteLater()
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    self.app.processEvents()
