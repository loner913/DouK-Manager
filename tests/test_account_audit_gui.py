from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from datetime import timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QThread, QTimer, Qt
from PySide6.QtWidgets import (
    QApplication,
    QListWidgetItem,
    QMessageBox,
    QTableView,
    QWidget,
)

from douk_manager.background import ClosePolicy, TaskSpec
from douk_manager.controller import ControllerError, ManagerController
from douk_manager.core.account_audit import (
    AccountAuditEntry,
    AccountAuditReport,
    AccountEvidence,
    AuditApplyPreview,
    AuditApplyResult,
    AuditDecision,
    Disposition,
    IdentityState,
    PrivacyState,
    ReachabilityState,
    Suggestion,
    UNCERTAIN_EARLY_HISTORY_WARNING,
    AccountAuditService,
)
from douk_manager.core.backup import BackupService
from douk_manager.core.download_summary import AccountStatus
from douk_manager.core.json_store import read_json, write_json_atomic
from douk_manager.core.result_history import (
    AccountHistoryRow,
    DownloadTaskHistory,
    ResultHistoryService,
)
from douk_manager.core.settings_tasks import ActivatedTask
from douk_manager.gui import AccountAuditTableModel, MainWindow
from douk_manager.operation import OperationContext, OperationProgress
from douk_manager.startup import StartupState
from tests.helpers import make_test_paths


def _entry(
    number: int,
    *,
    enabled: bool = True,
    identity: IdentityState = IdentityState.CONFIRMED,
    suggestion: Suggestion = Suggestion.KEEP,
) -> AccountAuditEntry:
    status = AccountStatus.ERROR if suggestion is Suggestion.SUGGEST_DISABLE else AccountStatus.DOWNLOADED
    run_ids = (
        tuple(f"DownloadTask_synthetic_{index}" for index in range(1, 6))
        if suggestion is Suggestion.SUGGEST_DISABLE
        else ()
    )
    evidence = AccountEvidence(
        a_number=number,
        runs_seen=5,
        last_run_stamp="DownloadTask_synthetic_5",
        status_counts={status: 5},
        consecutive_error_runs=len(run_ids),
        consecutive_private_runs=0,
        consecutive_no_eligible_runs=0,
        last_downloaded_run=None if run_ids else "DownloadTask_synthetic_5",
        consecutive_error_run_ids=run_ids,
        last_classified_status=status,
    )
    return AccountAuditEntry(
        a_number=number,
        identity=identity,
        reachability=(
            ReachabilityState.REQUEST_FAILED
            if suggestion is Suggestion.SUGGEST_DISABLE
            else ReachabilityState.REACHABLE
        ),
        privacy=PrivacyState.PUBLIC,
        disposition=(
            Disposition.ENABLED if enabled else Disposition.PERMANENTLY_DISABLED
        ),
        master_enable=enabled,
        suggestion=suggestion,
        suggestion_reason=f"A{number} synthetic audit reason",
        evidence=evidence,
        duplicate_group=1 if identity is IdentityState.DUPLICATE else None,
    )


def _report(total: int = 4, *, generated_minute: int = 0) -> AccountAuditReport:
    entries = [_entry(number) for number in range(1, total + 1)]
    if total >= 1:
        entries[0] = _entry(1, suggestion=Suggestion.SUGGEST_DISABLE)
    if total >= 2:
        entries[1] = _entry(2, enabled=False, suggestion=Suggestion.SUGGEST_REENABLE)
    if total >= 3:
        entries[2] = _entry(
            3, identity=IdentityState.DUPLICATE, suggestion=Suggestion.REVIEW
        )
    return AccountAuditReport(
        generated_at=datetime(2026, 9, 8, 12, generated_minute),
        master_sha256=f"synthetic-master-{generated_minute}",
        total_accounts=total,
        entries=tuple(entries),
        runs_scanned=42,
        oldest_run="2026-08-01 12:00:00",
        newest_run="2026-09-08 12:00:00",
        duplicate_groups=1 if total >= 3 else 0,
        warnings=(UNCERTAIN_EARLY_HISTORY_WARNING,),
    )


class AccountAuditControllerIntegrationTests(unittest.TestCase):
    def _controller(self) -> ManagerController:
        controller = ManagerController.__new__(ManagerController)
        controller.startup_state = StartupState.READY
        controller._download_lifecycle_active = False
        controller.account_audit = Mock()
        controller.engine = SimpleNamespace(external_running=Mock(return_value=False))
        controller.collector = SimpleNamespace(running=False, health=Mock(return_value=False))
        controller.logger = Mock()
        return controller

    def test_controller_owns_scan_apply_and_rechecks_process_guards(self) -> None:
        controller = self._controller()
        report = _report()
        decisions = (AuditDecision(1, Disposition.PERMANENTLY_DISABLED),)
        applied = AuditApplyResult(
            AuditApplyPreview((1,), (), (), 3, 3, 4, 4),
            Path("synthetic-backup"),
            Path("synthetic-audit-state"),
            "synthetic-after",
        )
        controller.account_audit.build_current_report.return_value = report
        controller.account_audit.apply_decisions.return_value = applied
        context = OperationContext()

        self.assertIs(
            controller.audit_accounts(
                error_threshold=6,
                minimum_evidence_runs=4,
                context=context,
            ),
            report,
        )
        controller.account_audit.build_current_report.assert_called_once_with(
            error_threshold=6,
            minimum_evidence_runs=4,
            context=context,
        )
        self.assertIs(
            controller.apply_audit_decisions(report, decisions, context=context),
            applied,
        )
        controller.account_audit.apply_decisions.assert_called_once_with(
            report, decisions, context=context
        )

        controller.collector.running = True
        with self.assertRaisesRegex(ControllerError, "采集服务"):
            controller.apply_audit_decisions(report, decisions, context=context)
        controller.startup_state = StartupState.CLOSING
        with self.assertRaisesRegex(ControllerError, "关闭"):
            controller.audit_accounts(context=context)


class AccountAuditScanIntegrationTests(unittest.TestCase):
    def test_1392_by_42_scan_reports_progress_reuses_cache_and_detects_duplicates(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 1392)
            master = read_json(paths.master_settings)
            master["accounts_urls"][1]["url"] = master["accounts_urls"][0]["url"]
            write_json_atomic(paths.master_settings, master)
            history = ResultHistoryService(paths.download_task_logs)
            base = datetime(2026, 7, 1, 12, 0, 0)
            runs = tuple(
                DownloadTaskHistory(
                    task_log=paths.download_task_logs
                    / f"DownloadTask_synthetic_{run_number:02d}.log",
                    task_template="synthetic-task.json",
                    ended_at=base + timedelta(days=run_number),
                    exit_code=0,
                    complete=True,
                    reliable=True,
                    account_rows=tuple(
                        AccountHistoryRow(
                            task_log=paths.download_task_logs
                            / f"DownloadTask_synthetic_{run_number:02d}.log",
                            task_template="synthetic-task.json",
                            ended_at=base + timedelta(days=run_number),
                            a_number=number,
                            status=AccountStatus.DOWNLOADED,
                        )
                        for number in range(1, 1393)
                    ),
                    details_complete=True,
                )
                for run_number in range(42)
            )
            history.list_runs = Mock(return_value=runs)
            service = AccountAuditService(
                history, paths=paths, backup=BackupService(paths)
            )
            progress: list[OperationProgress] = []
            context = OperationContext(
                progress_callback=progress.append,
                progress_interval_seconds=0,
            )

            first = service.build_current_report(context=context)
            second = service.build_current_report(context=context)

            self.assertIs(first, second)
            self.assertEqual(history.list_runs.call_count, 1)
            self.assertEqual(first.total_accounts, 1392)
            self.assertEqual(first.runs_scanned, 42)
            self.assertEqual(first.entries[0].identity, IdentityState.DUPLICATE)
            self.assertEqual(first.entries[1].identity, IdentityState.DUPLICATE)
            self.assertEqual(progress[-1].phase, "account_audit_scan")
            self.assertEqual((progress[-1].current, progress[-1].total), (42, 42))


class AccountAuditTableModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_1392_rows_use_one_model_and_filter_without_row_widgets(self) -> None:
        model = AccountAuditTableModel()
        model.set_report(_report(1392))

        self.assertEqual(model.rowCount(), 1392)
        self.assertEqual(model.children(), [])
        model.set_filter("suggest_disable", "")
        self.assertEqual(model.rowCount(), 1)
        self.assertEqual(model.index(0, 0).data(), "A1")
        model.set_filter("disabled", "")
        self.assertEqual(model.index(0, 0).data(), "A2")
        model.set_filter("duplicate", "A3")
        self.assertEqual(model.rowCount(), 1)
        model.set_filter("all", "not-a-number")
        self.assertEqual(model.rowCount(), 0)


class AccountAuditGuiTests(unittest.TestCase):
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

    def _run_until(self, condition, *, timeout_ms: int = 5000) -> None:
        if condition():
            return
        loop = QEventLoop()
        timed_out: list[bool] = []
        poll = QTimer()
        poll.setInterval(2)
        poll.timeout.connect(lambda: loop.quit() if condition() else None)
        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(lambda: (timed_out.append(True), loop.quit()))
        poll.start()
        timeout.start(timeout_ms)
        loop.exec()
        poll.stop()
        timeout.stop()
        self.assertFalse(timed_out, "bounded account-audit GUI event loop timed out")
        self.assertTrue(condition())

    def _dispose(self, window: MainWindow, home_patch: object) -> None:
        window.poll_timer.stop()
        if window.coordinator.has_active_tasks():
            window.coordinator.begin_closing()
            self._run_until(lambda: not window.coordinator.has_active_tasks())
        window.hide()
        window.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        self.app.processEvents()
        for handler in list(window.controller.logger.handlers):
            window.controller.logger.removeHandler(handler)
            handler.close()
        home_patch.stop()

    def test_page_is_independent_safe_and_uses_a_table_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            try:
                self.assertEqual(window.tabs.tabText(window.audit_tab_index), "账号审计")
                self.assertIsInstance(window.account_audit_table, QTableView)
                self.assertIs(window.account_audit_table.model(), window._account_audit_model)
                self.assertFalse(window.account_audit_native_logs.isChecked())
                self.assertIn("建议不会自动生效", window.account_audit_notice.text())
                self.assertLess(len(window.account_audit_page.findChildren(QWidget)), 80)
                window._apply_account_audit_report(_report())
                window.account_audit_table.selectRow(0)
                window._set_account_audit_disposition(Disposition.ENABLED)
                self.assertEqual(window._account_audit_decisions, {})
                window.account_audit_table.selectRow(0)
                window._set_account_audit_disposition(
                    Disposition.PERMANENTLY_DISABLED
                )
                self.assertEqual(
                    window._account_audit_decisions,
                    {1: Disposition.PERMANENTLY_DISABLED},
                )
                self.assertIn("停用 1", window.account_audit_pending.text())
            finally:
                self._dispose(window, home_patch)

    def test_scan_uses_coalesced_generation_resources_and_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            try:
                report = _report()
                window.controller.audit_accounts = Mock(return_value=report)
                with patch.object(
                    window, "_submit_coalesced_background", return_value="audit-task"
                ) as submit:
                    window._start_account_audit_scan()

                spec, action = submit.call_args.args[:2]
                self.assertIsInstance(spec, TaskSpec)
                self.assertEqual(spec.task_type, "account_audit_scan")
                self.assertEqual(spec.resource_keys, frozenset({"settings", "task_logs"}))
                self.assertEqual(spec.close_policy, ClosePolicy.CANCEL)
                self.assertTrue(spec.cancellable)
                self.assertTrue(spec.dynamic_cancellation)
                context = OperationContext()
                self.assertIs(action(context), report)
                window.controller.audit_accounts.assert_called_once_with(
                    error_threshold=5,
                    minimum_evidence_runs=3,
                    context=context,
                )
                progress = OperationProgress("account_audit_scan", "已扫描 2 / 42 轮", 2, 42)
                submit.call_args.kwargs["on_progress"](progress)
                self.assertIn("2 / 42", window.account_audit_progress.text())
            finally:
                self._dispose(window, home_patch)

    def test_preview_cancel_never_submits_and_confirm_reuses_controller_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            try:
                report = _report()
                window._apply_account_audit_report(report)
                window._account_audit_decisions = {
                    1: Disposition.PERMANENTLY_DISABLED,
                    2: Disposition.ENABLED,
                    3: Disposition.PENDING_REVIEW,
                }
                preview = AuditApplyPreview((1,), (2,), (3,), 1, 3, 4, 4)
                window.controller.account_audit.preview_decisions = Mock(
                    return_value=preview
                )
                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.No,
                ) as question, patch.object(window, "_submit_background") as submit:
                    window._preview_and_apply_account_audit()
                submit.assert_not_called()
                confirmation = question.call_args.args[2]
                self.assertIn("A1", confirmation)
                self.assertIn("A2", confirmation)
                self.assertIn("A3", confirmation)
                self.assertIn("数组长度保持 4 项不变", confirmation)
                self.assertIn(UNCERTAIN_EARLY_HISTORY_WARNING, confirmation)

                result = AuditApplyResult(
                    preview,
                    Path("synthetic-backup"),
                    Path("synthetic-audit-state"),
                    "synthetic-after",
                )
                window.controller.apply_audit_decisions = Mock(return_value=result)
                with patch.object(
                    QMessageBox,
                    "question",
                    return_value=QMessageBox.StandardButton.Yes,
                ), patch.object(
                    window, "_submit_background", return_value="apply-task"
                ) as submit:
                    window._preview_and_apply_account_audit()
                spec, action = submit.call_args.args[:2]
                self.assertEqual(spec.task_type, "account_audit_apply")
                self.assertEqual(
                    spec.resource_keys,
                    frozenset({"settings", "volume", "collector_process"}),
                )
                context = OperationContext()
                self.assertIs(action(context), result)
                window.controller.apply_audit_decisions.assert_called_once()
            finally:
                self._dispose(window, home_patch)

    def test_repeated_scan_keeps_latest_result_and_releases_qthreads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            entered = threading.Event()
            calls = {"value": 0}
            older = _report(generated_minute=1)
            newer = _report(generated_minute=2)

            def scan(*, context: OperationContext, **_kwargs):
                calls["value"] += 1
                if calls["value"] == 1:
                    entered.set()
                    while True:
                        context.raise_if_cancelled()
                        threading.Event().wait(0.002)
                return newer

            try:
                window.controller.audit_accounts = scan
                window._start_account_audit_scan()
                self._run_until(entered.is_set)
                window._start_account_audit_scan()
                self._run_until(lambda: not window.coordinator.has_active_tasks())
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()

                self.assertEqual(calls["value"], 2)
                self.assertIs(window._account_audit_report, newer)
                self.assertEqual(window.coordinator.findChildren(QThread), [])
                self.assertFalse(window.coordinator.has_active_tasks())
            finally:
                self._dispose(window, home_patch)

    def test_close_cancels_scan_blocks_late_result_and_releases_qthreads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            entered = threading.Event()
            late = _report(generated_minute=3)

            def scan(*, context: OperationContext, **_kwargs):
                entered.set()
                while not context.cancel_requested:
                    threading.Event().wait(0.002)
                return late

            try:
                window.controller.audit_accounts = scan
                window._start_account_audit_scan()
                self._run_until(entered.is_set)
                with patch.object(QMessageBox, "information"):
                    window.close()
                self._run_until(lambda: not window.coordinator.has_active_tasks())
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()

                self.assertEqual(window.controller.startup_state, StartupState.CLOSING)
                self.assertIsNone(window._account_audit_report)
                self.assertEqual(window.coordinator.findChildren(QThread), [])
            finally:
                self._dispose(window, home_patch)

    def test_activation_feedback_lists_only_vetoed_a_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            try:
                task_path = Path(directory) / "synthetic-task.json"
                item = QListWidgetItem("synthetic-task.json")
                item.setData(Qt.ItemDataRole.UserRole, str(task_path))
                item.setCheckState(Qt.CheckState.Checked)
                window.task_list.addItem(item)
                result = ActivatedTask(
                    Path(directory) / "settings.json",
                    (2, 4),
                    Path(directory) / "synthetic-backup",
                )
                window.controller.activate_task = Mock(return_value=result)
                with patch.object(window, "refresh_all"):
                    window._activate_selected_task()

                output = window.queue_output.toPlainText()
                self.assertIn("否决 2 个账号", output)
                self.assertIn("A2,A4", output)
                self.assertNotIn("synthetic-backup", output)
            finally:
                self._dispose(window, home_patch)
