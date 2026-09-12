from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer, Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

from douk_manager.core.account_audit import (
    AccountAuditEntry,
    AccountEvidence,
    Disposition,
    IdentityState,
    PrivacyState,
    ReachabilityState,
    Suggestion,
)
from douk_manager.core.download_summary import AccountStatus
from douk_manager.core.profile_url import (
    FormalAccountRef,
    ProfileUrlResolution,
    ProfileUrlStatus,
)
from douk_manager.core.result_dashboard import DashboardAccountRow
from douk_manager.core.result_history import AccountHistoryRow, ResultPageSnapshot
from douk_manager.gui import MainWindow
from douk_manager.startup import StartupState


class HomepageGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, root: Path) -> tuple[MainWindow, object]:
        home_patch = patch.dict(os.environ, {"DOUK_MANAGER_HOME": str(root)})
        home_patch.start()
        window = MainWindow()
        window.poll_timer.stop()
        window.controller.startup_state = StartupState.READY
        window._apply_action_gate()
        window.show()
        self.app.processEvents()
        return window, home_patch

    def _dispose(self, window: MainWindow, home_patch: object) -> None:
        window.poll_timer.stop()
        if window.coordinator.has_active_tasks():
            window.coordinator.begin_closing()
            self._run_until(lambda: not window.coordinator.has_active_tasks())
        window.controller.begin_closing()
        window.hide()
        window.deleteLater()
        self.app.processEvents()
        for handler in list(window.controller.logger.handlers):
            window.controller.logger.removeHandler(handler)
            handler.close()
        home_patch.stop()

    def _run_until(self, condition, *, timeout_ms: int = 4000) -> None:
        if condition():
            return
        loop = QEventLoop()
        timed_out: list[bool] = []
        poll = QTimer()
        poll.setInterval(2)

        def check() -> None:
            if condition():
                poll.stop()
                loop.quit()

        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: (timed_out.append(True), loop.quit()))
        poll.timeout.connect(check)
        poll.start()
        timer.start(timeout_ms)
        loop.exec()
        poll.stop()
        timer.stop()
        self.assertFalse(timed_out, "bounded homepage GUI event loop timed out")
        self.assertTrue(condition())

    def _render_results(
        self,
        window: MainWindow,
        root: Path,
        *a_numbers: int,
    ) -> None:
        rows = tuple(
            AccountHistoryRow(
                task_log=root / f"DownloadTask_A{a_number}.log",
                task_template="synthetic.json",
                ended_at=datetime(2026, 9, 13, 12, 0, 0),
                a_number=a_number,
                status=AccountStatus.DOWNLOADED,
            )
            for a_number in a_numbers
        )
        window._render_result_snapshot(ResultPageSnapshot((), rows, 0))
        self._run_until(
            lambda: window._result_render_cursor == len(rows)
            and not window.result_render_timer.isActive()
        )

    def _wait_profile_task(self, window: MainWindow) -> None:
        self._run_until(
            lambda: not window.coordinator.has_active_tasks()
            and window._profile_open_task_id is None
        )

    def test_result_homepage_uses_selected_a_after_sort_and_keeps_log_double_click(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                log = root / "synthetic-result.log"
                log.write_text("synthetic result log", encoding="utf-8")
                self._render_results(window, root, 20, 3)
                window.result_table.item(0, 5).setText(str(log))
                window.result_table.selectRow(0)
                window.result_table.sortItems(1, Qt.SortOrder.AscendingOrder)
                self.assertEqual(window._selected_result_a_number(), 20)
                self.assertTrue(window.result_open_home_button.isEnabled())

                resolver = Mock(
                    return_value=ProfileUrlResolution(
                        FormalAccountRef(20),
                        ProfileUrlStatus.FOUND,
                        "https://www.douyin.com/user/synthetic-20",
                    )
                )
                window.controller.resolve_profile_url = resolver
                with patch.object(
                    QDesktopServices, "openUrl", return_value=True
                ) as open_url:
                    window.result_open_home_button.click()
                    self._wait_profile_task(window)
                resolver.assert_called_once()
                self.assertEqual(resolver.call_args.args[0], 20)
                self.assertEqual(
                    open_url.call_args.args[0].toString(),
                    "https://www.douyin.com/user/synthetic-20",
                )

                with patch.object(
                    QDesktopServices, "openUrl", return_value=True
                ) as open_log:
                    log_row = next(
                        row
                        for row in range(window.result_table.rowCount())
                        if window.result_table.item(row, 1).text() == "A20"
                    )
                    window._open_result_log(log_row, 5)
                self.assertTrue(open_log.call_args.args[0].isLocalFile())
                self.assertTrue(
                    Path(open_log.call_args.args[0].toLocalFile()).samefile(log)
                )
            finally:
                self._dispose(window, home_patch)

    def test_dashboard_homepage_uses_model_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            try:
                window._dashboard_account_model.set_rows(
                    (
                        DashboardAccountRow(
                            91, AccountStatus.DOWNLOADED, False
                        ),
                    ),
                    evidence_source="synthetic",
                )
                window.dashboard_account_scope.setCurrentIndex(
                    window.dashboard_account_scope.findData("all")
                )
                window.dashboard_account_table.selectRow(0)
                self.assertTrue(window.dashboard_open_home_button.isEnabled())
                resolver = Mock(
                    return_value=ProfileUrlResolution(
                        FormalAccountRef(91),
                        ProfileUrlStatus.FOUND,
                        "https://www.douyin.com/user/synthetic-91",
                    )
                )
                window.controller.resolve_profile_url = resolver
                with patch.object(
                    QDesktopServices, "openUrl", return_value=True
                ) as open_url:
                    window.dashboard_open_home_button.click()
                    self._wait_profile_task(window)
                resolver.assert_called_once()
                self.assertEqual(resolver.call_args.args[0], 91)
                self.assertEqual(
                    open_url.call_args.args[0].toString(),
                    "https://www.douyin.com/user/synthetic-91",
                )
            finally:
                self._dispose(window, home_patch)

    def test_audit_homepage_uses_single_selected_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            try:
                entry = AccountAuditEntry(
                    a_number=17,
                    identity=IdentityState.CONFIRMED,
                    reachability=ReachabilityState.REACHABLE,
                    privacy=PrivacyState.PUBLIC,
                    disposition=Disposition.ENABLED,
                    master_enable=True,
                    suggestion=Suggestion.KEEP,
                    suggestion_reason="synthetic evidence",
                    evidence=AccountEvidence(
                        a_number=17,
                        runs_seen=1,
                        last_run_stamp="synthetic-run",
                        status_counts={AccountStatus.DOWNLOADED: 1},
                        consecutive_error_runs=0,
                        consecutive_private_runs=0,
                        consecutive_no_eligible_runs=0,
                        last_downloaded_run="synthetic-run",
                        consecutive_error_run_ids=(),
                        last_classified_status=AccountStatus.DOWNLOADED,
                    ),
                )
                window._account_audit_model.set_report(
                    SimpleNamespace(entries=(entry,))
                )
                window._apply_account_audit_filter()
                window.account_audit_table.selectRow(0)
                self.assertTrue(window.account_audit_open_home_button.isEnabled())
                resolver = Mock(
                    return_value=ProfileUrlResolution(
                        FormalAccountRef(17),
                        ProfileUrlStatus.FOUND,
                        "https://www.douyin.com/user/synthetic-17",
                    )
                )
                window.controller.resolve_profile_url = resolver
                with patch.object(
                    QDesktopServices, "openUrl", return_value=True
                ) as open_url:
                    window.account_audit_open_home_button.click()
                    self._wait_profile_task(window)
                resolver.assert_called_once()
                self.assertEqual(resolver.call_args.args[0], 17)
                self.assertEqual(
                    open_url.call_args.args[0].toString(),
                    "https://www.douyin.com/user/synthetic-17",
                )
            finally:
                self._dispose(window, home_patch)

    def test_invalid_or_late_profile_resolution_never_opens_or_leaks_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                self._render_results(window, root, 1, 2)
                window.result_table.selectRow(0)
                invalid = ProfileUrlResolution(
                    FormalAccountRef(1), ProfileUrlStatus.INVALID
                )
                window.controller.resolve_profile_url = Mock(return_value=invalid)
                with patch.object(
                    QMessageBox, "information"
                ) as information, patch.object(
                    QDesktopServices, "openUrl", return_value=True
                ) as open_url:
                    window.result_open_home_button.click()
                    self._wait_profile_task(window)
                open_url.assert_not_called()
                self.assertEqual(information.call_count, 1)
                text = " ".join(str(arg) for arg in information.call_args.args)
                self.assertIn("A1", text)
                self.assertNotIn("https://", text)

                window.result_table.selectRow(0)
                window.result_table.selectRow(1)
                with patch.object(
                    QDesktopServices, "openUrl", return_value=True
                ) as open_url:
                    window._apply_profile_resolution(
                        ProfileUrlResolution(
                            FormalAccountRef(1),
                            ProfileUrlStatus.FOUND,
                            "https://www.douyin.com/user/late-1",
                        ),
                        "result",
                        1,
                    )
                open_url.assert_not_called()
            finally:
                self._dispose(window, home_patch)


if __name__ == "__main__":
    unittest.main()
