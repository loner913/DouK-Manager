import os
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication
from douk_manager.core.watchlist_reminders import reminder_kind, reminder_summary
from douk_manager.core.watchlist import WatchlistSnapshot
from douk_manager.gui import WatchlistTableModel
from douk_manager.gui import MainWindow
from douk_manager.startup import StartupState
from douk_manager.ui.shell.navigation import NavigationItem, NavigationSidebar
from douk_manager.ui.shell.main_window import ModernMainWindow


class ReminderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def row(self, w_id, due, state="watching"):
        return dict(w_id=w_id, next_review_at=due, state=state,
                    created_at="2026-09-01T00:00:00Z")

    def test_local_midnight_not_24_hours(self):
        tz = timezone(timedelta(hours=8))
        row = self.row(1, "2026-09-14T15:59:00Z")
        self.assertEqual(reminder_kind(row, datetime(2026, 9, 14, 15, 58, tzinfo=timezone.utc), tz), "upcoming")
        self.assertEqual(reminder_kind(row, datetime(2026, 9, 14, 15, 59, tzinfo=timezone.utc), tz), "due")
        self.assertEqual(reminder_kind(row, datetime(2026, 9, 14, 16, 0, tzinfo=timezone.utc), tz), "overdue")

    def test_summary_excludes_inactive_and_does_not_mutate(self):
        rows = [self.row(1, "2026-09-13T00:00:00Z"),
                self.row(2, "2026-09-14T00:00:00Z"),
                self.row(3, "2099-01-01T00:00:00Z"),
                self.row(4, "2020-01-01T00:00:00Z", "archived"),
                self.row(5, "2020-01-01T00:00:00Z", "promoted")]
        before = repr(rows)
        result = reminder_summary(rows, datetime(2026, 9, 14, 8, tzinfo=timezone.utc), timezone.utc)
        self.assertEqual((result["due"], result["overdue"]), (2, 1))
        self.assertEqual(repr(rows), before)

    def test_overdue_filter_is_due_subset_and_refresh_keeps_rows(self):
        model = WatchlistTableModel()
        model.set_snapshot(WatchlistSnapshot(1, 4, (
            self.row(1, "2020-01-01T00:00:00Z"),
            self.row(2, "2099-01-01T00:00:00Z"),
            self.row(3, None, "archived"))))
        model.set_filters("watching", "overdue", "")
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.reminder_summary()["due"], 1)
        self.assertTrue(model.data(model.index(0, 5), Qt.ItemDataRole.FontRole).bold())
        resets = []
        model.modelReset.connect(lambda: resets.append(True))
        model.refresh_reminders()
        self.assertEqual(resets, [])
        model.set_filters("watching", "due", "")
        self.assertEqual(model.record_at(0)["w_id"], 1)

    def test_collapsed_navigation_retains_badge(self):
        nav = NavigationSidebar((NavigationItem("watch", "观察名单"),))
        nav.set_badge("观察名单", 2, True)
        self.assertEqual(nav._buttons["watch"].text(), "观察名单 (2)")
        nav.set_collapsed(True)
        self.assertEqual(nav._buttons["watch"].text(), "2")
        nav.set_collapsed(False)
        self.assertEqual(nav._buttons["watch"].text(), "观察名单 (2)")
        nav.set_badge("观察名单", 0)
        self.assertEqual(nav._buttons["watch"].text(), "观察名单")

    def test_invalid_dates_do_not_become_due(self):
        self.assertEqual(reminder_kind(self.row(1, None)), "missing")
        self.assertEqual(reminder_kind(self.row(1, "bad")), "invalid")

    def test_timer_refreshes_on_clock_jump_without_changing_system_clock(self):
        now = datetime.now(timezone.utc)
        host = SimpleNamespace(controller=SimpleNamespace(startup_state=StartupState.READY),
                               _reminder_tick=10, _reminder_refresh_tick=10,
                               _reminder_wall=now-timedelta(hours=1),
                               _reminder_offset=now.astimezone().utcoffset(),
                               _refresh_watchlist_reminders=Mock(), refresh_watchlist=Mock())
        with patch("douk_manager.gui.time.monotonic", return_value=11):
            MainWindow._tick_watchlist_reminders(host)
        host._refresh_watchlist_reminders.assert_called_once()
        host.refresh_watchlist.assert_called_once_with(preserve_output=True, quiet=True)

    def test_overview_navigation_filter_and_quiet_refresh_agree(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"DOUK_MANAGER_HOME": directory}):
                window = ModernMainWindow()
                try:
                    self.assertIsNone(window._modern_shell_install_error)
                    snapshot = WatchlistSnapshot(1, 4, (
                        self.row(1, "2020-01-01T00:00:00Z"),
                        self.row(2, "2099-01-01T00:00:00Z"),
                        self.row(3, None, "archived")))
                    window._apply_watchlist_snapshot(snapshot)
                    window.modern_overview.refresh_watchlist_reminder()
                    window._sync_modern_status()
                    self.assertIn("待处理 1 · 超期 1", window.modern_overview.watchlist_reminder_text.text())
                    window.modern_overview.watchlist_reminder.click()
                    self.assertEqual(window.tabs.currentIndex(), window.watchlist_tab_index)
                    self.assertEqual(window.watchlist_model.rowCount(), 1)
                    window.watchlist_table.selectRow(0)
                    window.watchlist_search_edit.setText("1")
                    window.watchlist_table.selectRow(0)
                    resets = []
                    window.watchlist_model.modelReset.connect(lambda: resets.append(True))
                    window._apply_watchlist_snapshot(snapshot, preserve_output=True, quiet=True)
                    self.assertEqual(resets, [])
                    self.assertEqual(window.watchlist_search_edit.text(), "1")
                    self.assertEqual(window._selected_watchlist_record()["w_id"], 1)
                    route = window._route_for_index(window.watchlist_tab_index)
                    self.assertIn("1", window.modern_sidebar._buttons[route].text())
                finally:
                    window.watchlist_reminder_timer.stop()
                    window.poll_timer.stop()
                    window._modern_status_timer.stop()
                    window.modern_overview._timer.stop()
                    window.modern_overview._clock_timer.stop()
                    window.controller.begin_closing()
                    window.hide()
                    window.deleteLater()
                    self.app.processEvents()
                    for handler in list(window.controller.logger.handlers):
                        window.controller.logger.removeHandler(handler)
                        handler.close()


if __name__ == "__main__":
    unittest.main()
