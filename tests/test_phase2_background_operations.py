from __future__ import annotations

import inspect
import os
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError, is_dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QThread

from douk_manager.background import (
    BackgroundTaskCoordinator,
    CancellationToken,
    ClosePolicy,
    TaskCancelled,
    TaskFailure,
    TaskRecord,
    TaskRejectedError,
    TaskSpec,
    TaskState,
    TaskWorker,
)
from douk_manager.gui import MainWindow
from douk_manager.controller import ManagerController
from douk_manager.core.result_history import (
    AccountHistoryRow,
    DownloadTaskHistory,
    ResultHistoryService,
    ResultPageSnapshot,
)
from douk_manager.core.download_summary import AccountStatus
from douk_manager.core.backup import BackupService, BackupError
from douk_manager.core.engine_update import EngineUpdateService
from douk_manager.startup import StartupState

try:
    from douk_manager.operation import OperationContext, OperationProgress
except ModuleNotFoundError as exc:  # RED until Step 2 adds the protocol module.
    OperationContext: Any = None
    OperationProgress: Any = None
    OPERATION_IMPORT_ERROR = exc
else:
    OPERATION_IMPORT_ERROR = None


class OperationContractTests(unittest.TestCase):
    def require_context(self) -> tuple[Any, Any]:
        self.assertIsNone(
            OPERATION_IMPORT_ERROR,
            f"Phase 2 operation protocol is missing: {OPERATION_IMPORT_ERROR}",
        )
        self.assertIsNotNone(OperationContext)
        self.assertIsNotNone(OperationProgress)
        return OperationContext, OperationProgress

    def test_operation_protocol_is_pure_python_and_progress_is_immutable(self) -> None:
        context_type, progress_type = self.require_context()
        module_source = inspect.getsource(inspect.getmodule(context_type))
        self.assertNotIn("PySide6", module_source)
        self.assertTrue(is_dataclass(progress_type))
        progress = progress_type("scan", "reading", 1, 3, False)
        with self.assertRaises(FrozenInstanceError):
            progress.phase = "other"

    def test_context_starts_cancellable_and_cancel_wins_before_critical(self) -> None:
        context_type, _ = self.require_context()
        context = context_type()

        self.assertTrue(context.request_cancel())
        self.assertFalse(context.request_cancel())
        self.assertTrue(context.cancel_requested)
        with self.assertRaises(TaskCancelled):
            context.raise_if_cancelled()
        with self.assertRaises(TaskCancelled):
            context.enter_critical_phase()
        self.assertFalse(context.critical_to_completion)

    def test_critical_phase_wins_and_late_cancel_is_rejected(self) -> None:
        context_type, _ = self.require_context()
        context = context_type()

        context.enter_critical_phase()
        self.assertTrue(context.critical_to_completion)
        self.assertFalse(context.request_cancel())
        context.raise_if_cancelled()
        context.seal_terminal()
        self.assertTrue(context.terminal_sealed)
        self.assertFalse(context.request_cancel())

    def test_cancel_and_critical_phase_race_has_one_atomic_winner(self) -> None:
        context_type, _ = self.require_context()
        for _ in range(20):
            context = context_type()
            barrier = threading.Barrier(3)
            results: dict[str, bool] = {}

            def cancel() -> None:
                barrier.wait()
                results["cancel"] = context.request_cancel()

            def enter_critical() -> None:
                barrier.wait()
                try:
                    context.enter_critical_phase()
                except TaskCancelled:
                    results["critical"] = False
                else:
                    results["critical"] = True

            threads = [
                threading.Thread(target=cancel),
                threading.Thread(target=enter_critical),
            ]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(2.0)
                self.assertFalse(thread.is_alive())

            self.assertNotEqual(results["cancel"], results["critical"])
            self.assertEqual(
                context.cancel_requested,
                results["cancel"],
            )
            self.assertEqual(
                context.critical_to_completion,
                results["critical"],
            )

    def test_success_commit_and_cancel_race_has_one_atomic_winner(self) -> None:
        context_type, _ = self.require_context()
        for _ in range(20):
            context = context_type()
            barrier = threading.Barrier(3)
            results: dict[str, bool] = {}

            def cancel() -> None:
                barrier.wait()
                results["cancel"] = context.request_cancel()

            def seal_success() -> None:
                barrier.wait()
                try:
                    context.seal_terminal()
                except TaskCancelled:
                    results["success"] = False
                else:
                    results["success"] = True

            threads = [threading.Thread(target=cancel), threading.Thread(target=seal_success)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join(2.0)
                self.assertFalse(thread.is_alive())

            self.assertNotEqual(results["cancel"], results["success"])
            self.assertEqual(context.cancel_requested, results["cancel"])
            self.assertEqual(context.terminal_sealed, results["success"])

    def test_critical_failure_keeps_failure_after_close_request(self) -> None:
        context_type, _ = self.require_context()
        context = context_type()
        context.enter_critical_phase()

        self.assertFalse(context.request_cancel(), "close must not interrupt critical failure")
        failure = RuntimeError("critical operation failed")
        with self.assertRaisesRegex(RuntimeError, "critical operation failed"):
            raise failure
        self.assertTrue(context.critical_to_completion)
        self.assertFalse(context.cancel_requested)

    def test_progress_is_throttled_before_cross_thread_callback(self) -> None:
        context_type, progress_type = self.require_context()
        now = [100.0]
        emitted: list[Any] = []
        context = context_type(
            progress_callback=emitted.append,
            clock=lambda: now[0],
            progress_interval_seconds=0.15,
        )

        first = progress_type("scan", "reading", 1, 10, False)
        same_phase = progress_type("scan", "reading", 2, 10, False)
        phase_change = progress_type("hash", "hashing", 1, 10, False)

        self.assertTrue(context.report_progress(first))
        now[0] += 0.05
        self.assertFalse(context.report_progress(same_phase))
        self.assertEqual(emitted, [first])
        now[0] += 0.11
        self.assertTrue(context.report_progress(same_phase))
        self.assertEqual(emitted, [first, same_phase])
        self.assertTrue(context.report_progress(phase_change))
        self.assertEqual(emitted, [first, same_phase, phase_change])

    def test_critical_progress_is_immediate_and_terminal_progress_is_dropped(self) -> None:
        context_type, progress_type = self.require_context()
        now = [20.0]
        emitted: list[Any] = []
        context = context_type(
            progress_callback=emitted.append,
            clock=lambda: now[0],
            progress_interval_seconds=0.15,
        )
        context.report_progress(progress_type("write", "preparing", 1, 2, False))
        now[0] += 0.01
        critical = progress_type("write", "committing", 2, 2, True)
        self.assertTrue(context.report_progress(critical))
        context.seal_terminal()
        now[0] += 1.0
        self.assertFalse(context.report_progress(progress_type("write", "done", 2, 2, False)))
        self.assertEqual(emitted, [emitted[0], critical])


class CoordinatorProtocolExtensionTests(unittest.TestCase):
    def test_phase_one_task_spec_defaults_to_static_cancellation(self) -> None:
        spec = TaskSpec(task_type="legacy", display_name="Legacy")
        self.assertIn("dynamic_cancellation", TaskSpec.__dataclass_fields__)
        self.assertFalse(spec.dynamic_cancellation)

    def test_dynamic_task_record_can_hold_operation_context(self) -> None:
        context_type = OperationContext
        self.assertIsNotNone(
            context_type,
            f"Phase 2 operation protocol is missing: {OPERATION_IMPORT_ERROR}",
        )
        context = context_type()
        spec = TaskSpec(
            task_type="phase2",
            display_name="Phase 2 operation",
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )
        record = TaskRecord(
            task_id="phase2-1",
            spec=spec,
            generation=1,
            token=CancellationToken(),
            thread=None,
            worker=None,
            operation_context=context,
        )
        self.assertIs(record.operation_context, context)

    def test_coordinator_close_respects_context_critical_phase(self) -> None:
        context_type = OperationContext
        self.assertIsNotNone(
            context_type,
            f"Phase 2 operation protocol is missing: {OPERATION_IMPORT_ERROR}",
        )
        context = context_type()
        context.enter_critical_phase()
        spec = TaskSpec(
            task_type="phase2",
            display_name="Phase 2 operation",
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )
        record = TaskRecord(
            task_id="phase2-close",
            spec=spec,
            generation=1,
            token=CancellationToken(),
            thread=None,
            worker=None,
            operation_context=context,
        )
        coordinator = BackgroundTaskCoordinator()
        coordinator._records[record.task_id] = record

        self.assertFalse(coordinator.begin_closing())
        self.assertTrue(coordinator.is_closing)
        self.assertFalse(context.cancel_requested)


class _BindingCoordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.error: Exception | None = None
        self.cancelled: list[str] = []

    def start(self, spec: TaskSpec, generation: int, action: Any) -> str:
        if self.error is not None:
            raise self.error
        task_id = f"task-{len(self.calls) + 1}"
        self.calls.append((spec, generation, action))
        return task_id

    def request_cancel(self, task_id: str) -> bool:
        self.cancelled.append(task_id)
        return True


class MainWindowBackgroundBindingTests(unittest.TestCase):
    @staticmethod
    def _window() -> SimpleNamespace:
        status_bar = SimpleNamespace(showMessage=Mock())
        window = SimpleNamespace(
            coordinator=_BindingCoordinator(),
            controller=SimpleNamespace(
                startup_state=StartupState.READY,
                logger=Mock(),
            ),
            _background_bindings={},
            _background_generations={},
            _cancel_shutdown_for_new_work=Mock(),
            _append_info=Mock(),
            _apply_action_gate=Mock(),
            _refresh_background_targets=Mock(),
            statusBar=Mock(return_value=status_bar),
        )
        window._background_binding_is_current = lambda binding, generation: (
            MainWindow._background_binding_is_current(window, binding, generation)
        )
        return window

    @staticmethod
    def _spec(name: str = "synthetic", *, refresh: tuple[str, ...] = ()) -> TaskSpec:
        return TaskSpec(
            task_type=name,
            display_name=name,
            deduplicate_key=name,
            refresh_targets=refresh,
            dynamic_cancellation=True,
        )

    def test_submit_success_progress_refresh_and_remove_share_one_binding(self) -> None:
        window = self._window()
        button = SimpleNamespace(setEnabled=Mock())
        succeeded = Mock()
        progressed = Mock()
        removed = Mock()
        spec = self._spec("success", refresh=("task_list", "runtime_status"))
        action = lambda _context: {"ok": True}

        task_id = MainWindow._submit_background(
            window,
            spec,
            action,
            buttons=(button,),
            on_success=succeeded,
            on_progress=progressed,
            on_removed=removed,
        )

        self.assertEqual(task_id, "task-1")
        self.assertEqual(window.coordinator.calls, [(spec, 1, action)])
        button.setEnabled.assert_called_once_with(False)
        progress = OperationProgress("scan", "reading", 1, 2)
        MainWindow._on_background_task_progress(window, task_id, 1, progress)
        progressed.assert_called_once_with(progress)

        payload = {"ok": True}
        MainWindow._on_background_task_settled(
            window, task_id, 1, TaskState.SUCCEEDED, payload
        )
        succeeded.assert_called_once_with(payload)
        window._refresh_background_targets.assert_called_once_with(spec.refresh_targets)

        MainWindow._on_background_task_removed(window, task_id)
        self.assertEqual(window._background_bindings, {})
        self.assertEqual(button.setEnabled.call_args_list[-1].args, (True,))
        removed.assert_called_once_with()
        window._apply_action_gate.assert_called_once_with()

    def test_failure_cancel_and_request_cancel_are_dispatched(self) -> None:
        window = self._window()
        failed = Mock()
        cancelled = Mock()
        first = MainWindow._submit_background(
            window,
            self._spec("failure"),
            lambda _context: None,
            on_failure=failed,
        )
        second = MainWindow._submit_background(
            window,
            self._spec("cancel"),
            lambda _context: None,
            on_cancelled=cancelled,
        )
        failure = TaskFailure("RuntimeError", "expected", "trace")

        MainWindow._on_background_task_settled(
            window, first, 1, TaskState.FAILED, failure
        )
        MainWindow._on_background_task_settled(
            window, second, 1, TaskState.CANCELLED, "cancelled"
        )

        failed.assert_called_once_with(failure)
        cancelled.assert_called_once_with("cancelled")
        self.assertTrue(MainWindow._cancel_background(window, second))
        self.assertEqual(window.coordinator.cancelled, [second])

    def test_rejection_does_not_disable_or_replace_current_generation(self) -> None:
        window = self._window()
        window.coordinator.error = TaskRejectedError("synthetic conflict")
        button = SimpleNamespace(setEnabled=Mock())

        task_id = MainWindow._submit_background(
            window,
            self._spec("conflict"),
            lambda _context: None,
            output=SimpleNamespace(),
            buttons=(button,),
        )

        self.assertIsNone(task_id)
        self.assertEqual(window._background_bindings, {})
        self.assertEqual(window._background_generations, {})
        button.setEnabled.assert_not_called()
        window._append_info.assert_called_once()

    def test_stale_or_closing_progress_and_terminal_callbacks_are_dropped(self) -> None:
        window = self._window()
        succeeded = Mock()
        progressed = Mock()
        spec = self._spec("stale", refresh=("task_list",))
        task_id = MainWindow._submit_background(
            window,
            spec,
            lambda _context: None,
            on_success=succeeded,
            on_progress=progressed,
        )
        window._background_generations["stale"] = 2

        MainWindow._on_background_task_progress(
            window, task_id, 1, OperationProgress("scan", "old")
        )
        MainWindow._on_background_task_settled(
            window, task_id, 1, TaskState.SUCCEEDED, "old"
        )
        self.assertFalse(progressed.called)
        self.assertFalse(succeeded.called)
        self.assertFalse(window._refresh_background_targets.called)

        window.controller.startup_state = StartupState.CLOSING
        window._background_generations["stale"] = 1
        MainWindow._on_background_task_progress(
            window, task_id, 1, OperationProgress("scan", "closing")
        )
        self.assertFalse(progressed.called)


class DownloadSummaryCoordinatorCorrectionTests(unittest.TestCase):
    @staticmethod
    def _summary_window(task_log: Path) -> tuple[SimpleNamespace, SimpleNamespace]:
        run = SimpleNamespace(
            task_log=task_log,
            running=False,
            completion_marker=None,
            pause_after_exit=False,
        )
        window = SimpleNamespace(
            controller=SimpleNamespace(
                startup_state=StartupState.READY,
                summarize_download=Mock(return_value="summary-payload"),
                logger=Mock(),
            ),
            _background_bindings={},
            _background_generations={},
            _download_summary_binding=None,
            _submit_background=Mock(return_value="summary-task"),
            _append_info=Mock(),
            _refresh_background_targets=Mock(),
            _finish_run_after_summary=Mock(),
            _close_result_wrapper=Mock(return_value=True),
            queue_output=object(),
            queue_cancel_requested=False,
            queue_pending=[Path("next.json")],
            queue_current=run,
            queue_active=True,
            _release_download_lifecycle=Mock(),
            _record_task_elapsed=Mock(),
            _record_queue_elapsed=Mock(),
            _cleanup_completion_marker=Mock(),
        )
        return window, run

    @staticmethod
    def _start_and_capture(window: SimpleNamespace, run: SimpleNamespace):
        with patch(
            "douk_manager.gui.QThread",
            side_effect=AssertionError("legacy dedicated summary QThread was used"),
        ):
            MainWindow._start_download_summary(
                window,
                run,
                0,
                SimpleNamespace(normal_exit=True),
            )
        binding = window._download_summary_binding
        if binding is not None:
            window._background_generations[binding.deduplicate_key] = binding.generation
        return window._submit_background.call_args

    def test_summary_uses_exact_coordinator_contract_and_removed_only_continuation(
        self,
    ) -> None:
        window, run = self._summary_window(Path("Data/DownloadTask_20260820.log"))

        submitted = self._start_and_capture(window, run)
        binding = window._download_summary_binding

        spec, action = submitted.args
        self.assertEqual(spec.task_type, "download_summary")
        self.assertEqual(spec.resource_keys, frozenset({"task_logs", "result_logs"}))
        self.assertTrue(spec.deduplicate_key.startswith("download_summary:"))
        self.assertFalse(spec.cancellable)
        self.assertIs(spec.close_policy, ClosePolicy.WAIT)
        self.assertEqual(spec.refresh_targets, ())
        self.assertEqual(action(None), "summary-payload")
        window.controller.summarize_download.assert_called_once()

        summary = SimpleNamespace(complete=True, reliable=True)
        with patch(
            "douk_manager.gui.format_summary_for_ui", return_value=("summary complete",)
        ):
            submitted.kwargs["on_success"](summary)
            submitted.kwargs["on_success"](summary)

        self.assertIs(run._summary_result, summary)
        window._finish_run_after_summary.assert_not_called()
        window._refresh_background_targets.assert_not_called()

        submitted.kwargs["on_removed"]()
        submitted.kwargs["on_removed"]()

        window._refresh_background_targets.assert_called_once_with(
            ("download_results", "runtime_status")
        )
        window._finish_run_after_summary.assert_called_once()
        self.assertEqual(binding.task_id, "summary-task")
        self.assertEqual(binding.generation, 1)
        self.assertTrue(binding.terminal_consumed)
        self.assertTrue(binding.removed_consumed)
        self.assertIsNone(window._download_summary_binding)

    def test_real_coordinator_record_blocks_close_until_summary_is_removed(
        self,
    ) -> None:
        app = QCoreApplication.instance() or QCoreApplication([])
        entered = threading.Event()
        release = threading.Event()
        action_done = threading.Event()
        coordinator = BackgroundTaskCoordinator()
        window, run = self._summary_window(Path("Data/DownloadTask_real_record.log"))
        window.coordinator = coordinator
        window._background_pending = {}
        window._cancel_shutdown_for_new_work = Mock()
        window._apply_action_gate = Mock()
        window.statusBar = Mock(
            return_value=SimpleNamespace(showMessage=Mock())
        )
        window._background_binding_is_current = lambda binding, generation: (
            MainWindow._background_binding_is_current(window, binding, generation)
        )

        def summarize(*_args: object) -> dict[str, bool]:
            entered.set()
            try:
                if not release.wait(2.0):
                    raise RuntimeError("summary release event timed out")
                return {"complete": True, "reliable": True}
            finally:
                action_done.set()

        window.controller.summarize_download = summarize
        window._submit_background = lambda *args, **kwargs: MainWindow._submit_background(
            window, *args, **kwargs
        )
        coordinator.task_settled.connect(
            lambda *args: MainWindow._on_background_task_settled(window, *args)
        )
        coordinator.task_removed.connect(
            lambda task_id: MainWindow._on_background_task_removed(window, task_id)
        )

        with patch("douk_manager.gui.format_summary_for_ui", return_value=("done",)):
            MainWindow._start_download_summary(
                window,
                run,
                0,
                SimpleNamespace(normal_exit=True),
            )
            self.assertTrue(entered.wait(2.0))
            self.assertEqual(len(coordinator._records), 1)
            record = next(iter(coordinator._records.values()))
            thread = record.thread
            self.assertIsNotNone(thread)
            thread_destroyed: list[bool] = []
            thread.destroyed.connect(lambda *_: thread_destroyed.append(True))
            binding = window._download_summary_binding
            self.assertEqual(binding.task_id, record.task_id)
            self.assertEqual(binding.generation, record.generation)
            self.assertEqual(record.spec.task_type, "download_summary")
            self.assertEqual(
                record.spec.resource_keys,
                frozenset({"task_logs", "result_logs"}),
            )

            MainWindow._start_download_summary(
                window,
                run,
                0,
                SimpleNamespace(normal_exit=True),
            )
            self.assertEqual(len(coordinator._records), 1)

            close_event = SimpleNamespace(ignore=Mock(), accept=Mock())
            window.queue_current = None
            with patch("douk_manager.gui.QMessageBox.information"):
                MainWindow.closeEvent(window, close_event)
            window.queue_current = run
            close_event.ignore.assert_called_once_with()
            close_event.accept.assert_not_called()
            self.assertIs(window.controller.startup_state, StartupState.READY)
            self.assertFalse(coordinator.is_closing)

            release.set()
            self.assertTrue(action_done.wait(2.0))
            for _ in range(1000):
                app.processEvents()
                if not coordinator.has_active_tasks() and window._download_summary_binding is None:
                    break
            for _ in range(1000):
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                app.processEvents()
                if thread_destroyed:
                    break

        self.assertFalse(coordinator.has_active_tasks())
        self.assertIsNone(window._download_summary_binding)
        self.assertEqual(thread_destroyed, [True])
        self.assertFalse(coordinator.findChildren(QThread))
        window._finish_run_after_summary.assert_called_once_with(
            run, binding.assessment
        )

    def test_real_gui_result_reader_and_summary_conflicts_recover_both_ways(
        self,
    ) -> None:
        app = QCoreApplication.instance() or QCoreApplication([])
        coordinator = BackgroundTaskCoordinator()
        window, run = self._summary_window(Path("Data/DownloadTask_conflict.log"))
        window.coordinator = coordinator
        window._background_pending = {}
        window._cancel_shutdown_for_new_work = Mock()
        window._apply_action_gate = Mock()
        window.statusBar = Mock(
            return_value=SimpleNamespace(showMessage=Mock())
        )
        window._background_binding_is_current = lambda binding, generation: (
            MainWindow._background_binding_is_current(window, binding, generation)
        )
        window._submit_background = lambda *args, **kwargs: MainWindow._submit_background(
            window, *args, **kwargs
        )
        window._submit_coalesced_background = (
            lambda *args, **kwargs: MainWindow._submit_coalesced_background(
                window, *args, **kwargs
            )
        )
        window.result_table = object()
        window.result_refresh_button = None
        window._render_result_snapshot = Mock()
        window.task_smart_private = SimpleNamespace(isChecked=Mock(return_value=True))
        window.task_expression = SimpleNamespace(text=Mock(return_value="A1"))
        window.task_private_days = SimpleNamespace(value=Mock(return_value=7))
        window.task_output = object()
        window.task_preview_button = object()

        reader_entered = threading.Event()
        reader_release = threading.Event()
        reader_done = threading.Event()
        second_reader_done = threading.Event()
        reader_calls: list[int] = []

        def result_snapshot(*, limit: int, context: object) -> dict[str, int]:
            self.assertEqual(limit, 500)
            self.assertIsNotNone(context)
            reader_calls.append(len(reader_calls) + 1)
            if len(reader_calls) == 1:
                reader_entered.set()
                try:
                    if not reader_release.wait(2.0):
                        raise RuntimeError("reader release event timed out")
                finally:
                    reader_done.set()
            else:
                second_reader_done.set()
            return {"reader": len(reader_calls)}

        summary_entered = threading.Event()
        summary_release = threading.Event()
        summary_done = threading.Event()
        summary_calls: list[Path] = []

        def summarize(target_run: object, *_args: object) -> dict[str, bool]:
            summary_calls.append(target_run.task_log)
            summary_entered.set()
            try:
                if not summary_release.wait(2.0):
                    raise RuntimeError("summary release event timed out")
                return {"complete": True, "reliable": True}
            finally:
                summary_done.set()

        window.controller.result_snapshot = result_snapshot
        window.controller.preview_private_skip = Mock()
        window.controller.summarize_download = summarize
        settlements: list[tuple[object, ...]] = []
        removals: list[str] = []
        coordinator.task_settled.connect(lambda *args: settlements.append(args))
        coordinator.task_removed.connect(removals.append)
        coordinator.task_settled.connect(
            lambda *args: MainWindow._on_background_task_settled(window, *args)
        )
        coordinator.task_removed.connect(
            lambda task_id: MainWindow._on_background_task_removed(window, task_id)
        )
        retained_threads: list[QThread] = []
        destroyed: list[str] = []

        def retain_current_thread(label: str) -> str:
            self.assertEqual(len(coordinator._records), 1)
            record = next(iter(coordinator._records.values()))
            self.assertIsNotNone(record.thread)
            retained_threads.append(record.thread)
            record.thread.destroyed.connect(lambda *_: destroyed.append(label))
            return record.task_id

        def drain_current_task(done: threading.Event) -> None:
            self.assertTrue(done.wait(2.0))
            for _ in range(1000):
                app.processEvents()
                if not coordinator.has_active_tasks():
                    break
            self.assertFalse(coordinator.has_active_tasks())
            for _ in range(1000):
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                app.processEvents()
                if len(destroyed) == len(retained_threads):
                    break

        try:
            MainWindow.refresh_results(window)
            self.assertTrue(reader_entered.wait(2.0))
            first_reader_id = retain_current_thread("reader-1")

            MainWindow._start_download_summary(
                window,
                run,
                0,
                SimpleNamespace(normal_exit=True),
            )
            self.assertIsNone(window._download_summary_binding)
            self.assertEqual(summary_calls, [])
            self.assertTrue(window.queue_active)
            self.assertIs(window.queue_current, run)
            window._finish_run_after_summary.assert_not_called()

            reader_release.set()
            drain_current_task(reader_done)

            with patch("douk_manager.gui.format_summary_for_ui", return_value=("done",)):
                MainWindow._start_download_summary(
                    window,
                    run,
                    0,
                    SimpleNamespace(normal_exit=True),
                )
                self.assertTrue(summary_entered.wait(2.0))
                summary_id = retain_current_thread("summary")

                MainWindow.refresh_results(window)
                MainWindow._preview_task(window)
                self.assertEqual(reader_calls, [1])
                window.controller.preview_private_skip.assert_not_called()
                self.assertEqual(len(coordinator._records), 1)

                summary_release.set()
                drain_current_task(summary_done)

            self.assertIsNone(window._download_summary_binding)
            self.assertEqual(summary_calls, [run.task_log])
            window._finish_run_after_summary.assert_called_once()

            MainWindow.refresh_results(window)
            self.assertTrue(second_reader_done.wait(2.0))
            second_reader_id = retain_current_thread("reader-2")
            drain_current_task(second_reader_done)
        finally:
            reader_release.set()
            summary_release.set()
            for _ in range(1000):
                app.processEvents()
                if not coordinator.has_active_tasks():
                    break
            for _ in range(1000):
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                app.processEvents()
                if len(destroyed) == len(retained_threads):
                    break

        self.assertEqual(reader_calls, [1, 2])
        self.assertEqual(window._render_result_snapshot.call_count, 2)
        expected_ids = [first_reader_id, summary_id, second_reader_id]
        self.assertEqual([event[0] for event in settlements], expected_ids)
        self.assertEqual(removals, expected_ids)
        self.assertEqual(destroyed, ["reader-1", "summary", "reader-2"])
        self.assertFalse(coordinator.findChildren(QThread))

    def test_summary_and_result_readers_share_result_logs_resource(self) -> None:
        window, run = self._summary_window(Path("Data/DownloadTask_20260820.log"))
        summary_spec = self._start_and_capture(window, run).args[0]

        result_window = SimpleNamespace(
            result_table=object(),
            result_refresh_button=None,
            controller=SimpleNamespace(result_snapshot=Mock()),
            _submit_coalesced_background=Mock(),
        )
        MainWindow.refresh_results(result_window)
        result_spec = result_window._submit_coalesced_background.call_args.args[0]

        private_window = SimpleNamespace(
            task_smart_private=SimpleNamespace(isChecked=Mock(return_value=True)),
            task_expression=SimpleNamespace(text=Mock(return_value="A1")),
            task_private_days=SimpleNamespace(value=Mock(return_value=7)),
            task_output=object(),
            task_preview_button=object(),
            controller=SimpleNamespace(preview_private_skip=Mock()),
            _submit_coalesced_background=Mock(),
        )
        MainWindow._preview_task(private_window)
        private_spec = private_window._submit_coalesced_background.call_args.args[0]

        self.assertEqual(result_spec.resource_keys, frozenset({"result_logs"}))
        self.assertEqual(private_spec.resource_keys, frozenset({"result_logs"}))

        coordinator = BackgroundTaskCoordinator()
        coordinator._records["reader"] = TaskRecord(
            task_id="reader",
            spec=result_spec,
            generation=1,
            token=CancellationToken(),
            thread=None,
            worker=None,
            state=TaskState.RUNNING,
        )
        with self.assertRaisesRegex(TaskRejectedError, "result_logs"):
            coordinator._validate_start(summary_spec)
        coordinator._validate_start(
            TaskSpec(
                task_type="collector_probe",
                display_name="collector probe",
                resource_keys=frozenset({"collector_process"}),
                deduplicate_key="collector_probe",
            )
        )

        coordinator._records.clear()
        coordinator._records["summary"] = TaskRecord(
            task_id="summary",
            spec=summary_spec,
            generation=1,
            token=CancellationToken(),
            thread=None,
            worker=None,
            state=TaskState.RUNNING,
        )
        for reader_spec in (result_spec, private_spec):
            with self.subTest(reader=reader_spec.task_type):
                with self.assertRaisesRegex(TaskRejectedError, "result_logs"):
                    coordinator._validate_start(reader_spec)
        coordinator._records.clear()
        coordinator._validate_start(result_spec)
        coordinator._validate_start(private_spec)

    def test_removed_without_terminal_fails_closed_and_ignores_late_terminal(
        self,
    ) -> None:
        window, run = self._summary_window(Path("Data/DownloadTask_protocol.log"))
        submitted = self._start_and_capture(window, run)
        summary = SimpleNamespace(complete=True, reliable=True)

        submitted.kwargs["on_removed"]()

        self.assertFalse(window.queue_active)
        self.assertIsNone(window.queue_current)
        self.assertEqual(window.queue_pending, [])
        window._release_download_lifecycle.assert_called_once_with()
        window._finish_run_after_summary.assert_not_called()
        window._refresh_background_targets.assert_called_once_with(
            ("download_results", "runtime_status")
        )
        display_count = window._append_info.call_count

        submitted.kwargs["on_success"](summary)
        submitted.kwargs["on_removed"]()

        self.assertEqual(window._append_info.call_count, display_count)
        window._release_download_lifecycle.assert_called_once_with()
        window._finish_run_after_summary.assert_not_called()
        self.assertIsNone(window._download_summary_binding)

    def test_closing_late_summary_callbacks_only_clean_up(self) -> None:
        window, run = self._summary_window(Path("Data/DownloadTask_closing_late.log"))
        submitted = self._start_and_capture(window, run)
        window.controller.startup_state = StartupState.CLOSING

        submitted.kwargs["on_success"](
            SimpleNamespace(complete=True, reliable=True)
        )
        submitted.kwargs["on_removed"]()
        submitted.kwargs["on_removed"]()

        window._append_info.assert_not_called()
        window._refresh_background_targets.assert_not_called()
        window._finish_run_after_summary.assert_not_called()
        window._release_download_lifecycle.assert_called_once_with()
        self.assertFalse(window.queue_active)
        self.assertIsNone(window.queue_current)
        self.assertEqual(window.queue_pending, [])
        self.assertIsNone(window._download_summary_binding)

    def test_closing_during_removed_refresh_prevents_post_and_cleans_up(self) -> None:
        window, run = self._summary_window(Path("Data/DownloadTask_refresh_race.log"))
        submitted = self._start_and_capture(window, run)
        with patch(
            "douk_manager.gui.format_summary_for_ui", return_value=("complete",)
        ):
            submitted.kwargs["on_success"](
                SimpleNamespace(complete=True, reliable=True)
            )
        window._refresh_background_targets.side_effect = lambda _targets: setattr(
            window.controller, "startup_state", StartupState.CLOSING
        )

        submitted.kwargs["on_removed"]()

        window._finish_run_after_summary.assert_not_called()
        window._release_download_lifecycle.assert_called_once_with()
        self.assertFalse(window.queue_active)
        self.assertIsNone(window.queue_current)
        self.assertEqual(window.queue_pending, [])
        self.assertIsNone(window._download_summary_binding)

    def test_stale_summary_generation_callbacks_are_ignored(self) -> None:
        window, run = self._summary_window(Path("Data/DownloadTask_stale.log"))
        submitted = self._start_and_capture(window, run)
        binding = window._download_summary_binding
        window._background_generations[binding.deduplicate_key] = binding.generation + 1

        submitted.kwargs["on_success"](
            SimpleNamespace(complete=True, reliable=True)
        )
        submitted.kwargs["on_removed"]()

        self.assertFalse(binding.terminal_consumed)
        self.assertFalse(binding.removed_consumed)
        window._append_info.assert_not_called()
        window._refresh_background_targets.assert_not_called()
        window._finish_run_after_summary.assert_not_called()

        window._background_generations[binding.deduplicate_key] = binding.generation
        submitted.kwargs["on_failure"](
            TaskFailure("RuntimeError", "current failure", "trace")
        )
        submitted.kwargs["on_removed"]()

        self.assertTrue(binding.terminal_consumed)
        self.assertTrue(binding.removed_consumed)
        window._release_download_lifecycle.assert_called_once_with()
        self.assertIsNone(window._download_summary_binding)

    def test_per_run_dedup_is_canonical_and_closing_never_submits(self) -> None:
        first_window, first_run = self._summary_window(
            Path("Data/../Data/DownloadTask_20260820.log")
        )
        first_spec = self._start_and_capture(first_window, first_run).args[0]

        equivalent_window, equivalent_run = self._summary_window(
            Path("data/downloadtask_20260820.LOG")
        )
        equivalent_spec = self._start_and_capture(
            equivalent_window, equivalent_run
        ).args[0]

        other_window, other_run = self._summary_window(
            Path("Data/DownloadTask_20260821.log")
        )
        other_spec = self._start_and_capture(other_window, other_run).args[0]

        self.assertEqual(first_spec.deduplicate_key, equivalent_spec.deduplicate_key)
        self.assertNotEqual(first_spec.deduplicate_key, other_spec.deduplicate_key)

        closing_window, closing_run = self._summary_window(
            Path("Data/DownloadTask_closing.log")
        )
        closing_window.controller.startup_state = StartupState.CLOSING
        MainWindow._start_download_summary(
            closing_window,
            closing_run,
            0,
            SimpleNamespace(normal_exit=True),
        )
        closing_window._submit_background.assert_not_called()


class DownloadPostActionsCoordinatorCorrectionTests(unittest.TestCase):
    @staticmethod
    def _post_window(
        *,
        screenshot_mode: str = "none",
        index_mode: str = "none",
        cleanup: bool = False,
        lifecycle_identity: str = "download_lifecycle:c:/data/downloadtask_first.log",
    ) -> tuple[SimpleNamespace, SimpleNamespace]:
        run = SimpleNamespace(
            task_log=Path("Data/DownloadTask_current.log"),
            running=False,
            result_review_waiting=False,
            process=SimpleNamespace(returncode=0),
        )
        controller = SimpleNamespace(
            startup_state=StartupState.READY,
            config=SimpleNamespace(
                screenshot_post_mode=screenshot_mode,
                index_post_mode=index_mode,
                cleanup_after_index=cleanup,
            ),
            _download_lifecycle_active=True,
            run_post_actions=Mock(return_value=[]),
            release_download_lifecycle=Mock(),
            logger=Mock(),
        )
        window = SimpleNamespace(
            controller=controller,
            coordinator=SimpleNamespace(
                has_active_tasks=Mock(return_value=False),
                begin_closing=Mock(return_value=True),
            ),
            _background_bindings={},
            _background_generations={},
            _background_pending={},
            _download_summary_binding=None,
            _download_lifecycle_identity=lifecycle_identity,
            _download_post_bindings={},
            _download_post_completed_keys=set(),
            _submit_background=Mock(return_value="post-task"),
            _append_info=Mock(),
            _refresh_background_targets=Mock(),
            _start_next_queue_item=Mock(),
            _record_queue_elapsed=Mock(),
            _release_download_lifecycle=Mock(),
            _begin_shutdown_countdown_if_requested=Mock(),
            _apply_action_gate=Mock(),
            _close_pending=False,
            queue_output=object(),
            queue_pause_button=SimpleNamespace(setEnabled=Mock()),
            queue_cancel_button=SimpleNamespace(setEnabled=Mock()),
            queue_pending=[Path("next.json")],
            queue_current=run,
            queue_active=True,
            queue_summaries_complete=True,
            queue_summaries_reliable=True,
            queue_shutdown_requested=False,
            background_thread=None,
        )
        return window, run

    @staticmethod
    def _capture_post_submission(
        window: SimpleNamespace,
        timing: str,
        run: object | None,
    ):
        MainWindow._run_post_actions_background(window, timing, run)
        return window._submit_background.call_args

    def test_post_task_specs_freeze_exact_resource_matrix_and_cancel_contract(
        self,
    ) -> None:
        cases = (
            ("batch", "none", "none", False, frozenset()),
            ("queue", "none", "none", True, frozenset()),
            (
                "batch",
                "batch",
                "none",
                False,
                frozenset({"screenshots", "video_tree"}),
            ),
            (
                "queue",
                "queue",
                "none",
                False,
                frozenset({"screenshots", "video_tree"}),
            ),
            (
                "batch",
                "none",
                "batch",
                False,
                frozenset({"video_tree", "index"}),
            ),
            (
                "queue",
                "none",
                "queue",
                True,
                frozenset({"video_tree", "index"}),
            ),
            (
                "batch",
                "batch",
                "batch",
                True,
                frozenset({"screenshots", "video_tree", "index"}),
            ),
            (
                "queue",
                "queue",
                "batch",
                True,
                frozenset({"screenshots", "video_tree"}),
            ),
            (
                "queue",
                "queue",
                "queue",
                True,
                frozenset({"screenshots", "video_tree", "index"}),
            ),
        )
        for timing, screenshot_mode, index_mode, cleanup, expected in cases:
            with self.subTest(
                timing=timing,
                screenshot=screenshot_mode,
                index=index_mode,
                cleanup=cleanup,
            ):
                window, run = self._post_window(
                    screenshot_mode=screenshot_mode,
                    index_mode=index_mode,
                    cleanup=cleanup,
                )
                submitted = self._capture_post_submission(
                    window, timing, run if timing == "batch" else None
                )
                spec = submitted.args[0]
                self.assertEqual(spec.task_type, "download_post_actions")
                self.assertEqual(spec.resource_keys, expected)
                self.assertTrue(spec.cancellable)
                self.assertTrue(spec.dynamic_cancellation)
                self.assertIs(spec.close_policy, ClosePolicy.CANCEL)
                self.assertEqual(
                    spec.refresh_targets,
                    ("runtime_status", "download_results"),
                )

    def test_post_action_key_is_canonical_and_scoped_to_lifecycle_run_and_timing(
        self,
    ) -> None:
        run_a = SimpleNamespace(task_log=Path("Data/../Data/DownloadTask_A.log"))
        run_a_equivalent = SimpleNamespace(task_log=Path("data/downloadtask_a.log"))
        run_b = SimpleNamespace(task_log=Path("Data/DownloadTask_B.log"))

        first = MainWindow._download_post_action_key(
            "download_lifecycle:first", "batch", run_a
        )
        equivalent = MainWindow._download_post_action_key(
            "download_lifecycle:first", "batch", run_a_equivalent
        )
        second_run = MainWindow._download_post_action_key(
            "download_lifecycle:first", "batch", run_b
        )
        queue_post = MainWindow._download_post_action_key(
            "download_lifecycle:first", "queue", None
        )
        second_lifecycle = MainWindow._download_post_action_key(
            "download_lifecycle:second", "batch", run_a
        )

        self.assertEqual(first, equivalent)
        self.assertEqual(len({first, second_run, queue_post, second_lifecycle}), 4)

    def test_none_resources_still_use_lifecycle_dedup_and_ready_state_gate(
        self,
    ) -> None:
        window, run = self._post_window()

        submitted = self._capture_post_submission(window, "batch", run)
        MainWindow._run_post_actions_background(window, "batch", run)

        spec = submitted.args[0]
        self.assertEqual(spec.resource_keys, frozenset())
        self.assertEqual(window._submit_background.call_count, 1)
        self.assertEqual(tuple(window._download_post_bindings), (spec.deduplicate_key,))
        self.assertEqual(
            window._background_generations,
            {spec.deduplicate_key: 1},
        )

        for state in (
            StartupState.BOOTSTRAPPING,
            StartupState.SAFETY_CHECKING,
            StartupState.DEGRADED_READ_ONLY,
            StartupState.CLOSING,
        ):
            with self.subTest(state=state):
                blocked, blocked_run = self._post_window()
                blocked.controller.startup_state = state
                identity = blocked._download_lifecycle_identity

                MainWindow._run_post_actions_background(blocked, "batch", blocked_run)

                blocked._submit_background.assert_not_called()
                self.assertEqual(blocked._download_post_bindings, {})
                self.assertEqual(blocked._background_generations, {})
                self.assertEqual(blocked._download_lifecycle_identity, identity)
                self.assertTrue(blocked.controller._download_lifecycle_active)
                blocked._release_download_lifecycle.assert_not_called()
                blocked._append_info.assert_not_called()

    def test_post_resource_matrix_creates_only_real_coordinator_conflicts(
        self,
    ) -> None:
        screenshot_window, screenshot_run = self._post_window(
            screenshot_mode="batch",
            lifecycle_identity="download_lifecycle:screenshots",
        )
        screenshot_spec = self._capture_post_submission(
            screenshot_window, "batch", screenshot_run
        ).args[0]
        index_window, index_run = self._post_window(
            index_mode="batch",
            lifecycle_identity="download_lifecycle:index",
        )
        index_spec = self._capture_post_submission(
            index_window, "batch", index_run
        ).args[0]
        empty_window, empty_run = self._post_window(
            lifecycle_identity="download_lifecycle:empty"
        )
        empty_spec = self._capture_post_submission(
            empty_window, "batch", empty_run
        ).args[0]
        unrelated = TaskSpec(
            task_type="unrelated_reader",
            display_name="unrelated reader",
            resource_keys=frozenset({"result_logs", "collector_process"}),
            deduplicate_key="unrelated_reader",
        )

        coordinator = BackgroundTaskCoordinator()
        coordinator._records["screenshots"] = TaskRecord(
            task_id="screenshots",
            spec=screenshot_spec,
            generation=1,
            token=CancellationToken(),
            thread=None,
            worker=None,
            state=TaskState.RUNNING,
        )
        with self.assertRaisesRegex(TaskRejectedError, "video_tree"):
            coordinator._validate_start(index_spec)
        coordinator._validate_start(unrelated)

        coordinator._records.clear()
        coordinator._records["index"] = TaskRecord(
            task_id="index",
            spec=index_spec,
            generation=1,
            token=CancellationToken(),
            thread=None,
            worker=None,
            state=TaskState.RUNNING,
        )
        with self.assertRaisesRegex(TaskRejectedError, "(index|video_tree)"):
            coordinator._validate_start(screenshot_spec)
        coordinator._validate_start(unrelated)

        coordinator._records.clear()
        coordinator._records["unrelated"] = TaskRecord(
            task_id="unrelated",
            spec=unrelated,
            generation=1,
            token=CancellationToken(),
            thread=None,
            worker=None,
            state=TaskState.RUNNING,
        )
        coordinator._validate_start(empty_spec)

    def test_post_action_uses_same_context_and_duplicate_callbacks_decide_once(
        self,
    ) -> None:
        window, run = self._post_window(
            screenshot_mode="batch",
            index_mode="batch",
            cleanup=True,
        )

        submitted = self._capture_post_submission(window, "batch", run)
        MainWindow._run_post_actions_background(window, "batch", run)

        self.assertEqual(window._submit_background.call_count, 1)
        context = OperationContext()
        submitted.args[1](context)
        window.controller.run_post_actions.assert_called_once_with(
            "batch", context=context
        )

        submitted.kwargs["on_success"]([])
        submitted.kwargs["on_success"]([])
        submitted.kwargs["on_removed"]()
        submitted.kwargs["on_removed"]()

        window._start_next_queue_item.assert_called_once_with()
        self.assertEqual(len(window._download_post_completed_keys), 1)
        self.assertEqual(window._download_post_bindings, {})

    def test_success_removal_and_late_duplicate_leave_no_lifecycle_state(self) -> None:
        window, _run = self._post_window()
        window.queue_pending = []

        def release_controller_lifecycle() -> None:
            window.controller._download_lifecycle_active = False

        window.controller.release_download_lifecycle.side_effect = (
            release_controller_lifecycle
        )
        window._release_download_lifecycle = lambda: (
            MainWindow._release_download_lifecycle(window)
        )

        submitted = self._capture_post_submission(window, "queue", None)
        spec = submitted.args[0]
        self.assertEqual(window._background_generations[spec.deduplicate_key], 1)

        submitted.kwargs["on_success"]([])
        submitted.kwargs["on_removed"]()
        MainWindow._run_post_actions_background(window, "queue", None)

        self.assertEqual(window._submit_background.call_count, 1)
        self.assertEqual(window._download_post_bindings, {})
        self.assertEqual(window._download_post_completed_keys, set())
        self.assertEqual(window._background_generations, {})
        self.assertIsNone(window._download_lifecycle_identity)
        self.assertFalse(window.controller._download_lifecycle_active)
        self.assertFalse(window.queue_active)
        self.assertIsNone(window.queue_current)
        self.assertEqual(window.queue_pending, [])
        window.controller.release_download_lifecycle.assert_called_once_with()

    def test_post_admission_failure_releases_lifecycle_without_dangling_state(
        self,
    ) -> None:
        window, run = self._post_window()
        window._submit_background.return_value = None

        def release_controller_lifecycle() -> None:
            window.controller._download_lifecycle_active = False

        window.controller.release_download_lifecycle.side_effect = (
            release_controller_lifecycle
        )
        window._release_download_lifecycle = lambda: (
            MainWindow._release_download_lifecycle(window)
        )

        MainWindow._run_post_actions_background(window, "batch", run)

        window._submit_background.assert_called_once()
        self.assertEqual(window._download_post_bindings, {})
        self.assertEqual(window._download_post_completed_keys, set())
        self.assertEqual(window._background_generations, {})
        self.assertIsNone(window._download_lifecycle_identity)
        self.assertFalse(window.controller._download_lifecycle_active)
        self.assertFalse(window.queue_active)
        self.assertIsNone(window.queue_current)
        self.assertEqual(window.queue_pending, [])
        window.controller.release_download_lifecycle.assert_called_once_with()

    def test_cancel_failure_and_closing_each_stop_once_without_advancing(self) -> None:
        for terminal in ("on_cancelled", "on_failure"):
            with self.subTest(terminal=terminal):
                window, run = self._post_window()
                submitted = self._capture_post_submission(window, "batch", run)

                submitted.kwargs[terminal](object())
                submitted.kwargs[terminal](object())
                submitted.kwargs["on_removed"]()
                submitted.kwargs["on_removed"]()

                self.assertFalse(window.queue_active)
                self.assertEqual(window.queue_pending, [])
                self.assertIsNone(window.queue_current)
                window._start_next_queue_item.assert_not_called()
                window._release_download_lifecycle.assert_called_once_with()
                window._refresh_background_targets.assert_not_called()
                window.controller._download_lifecycle_active = False
                window._download_lifecycle_identity = None
                window._download_post_completed_keys.clear()
                MainWindow._run_post_actions_background(window, "batch", run)
                self.assertEqual(window._submit_background.call_count, 1)
                window._release_download_lifecycle.assert_called_once_with()

        window, run = self._post_window()
        submitted = self._capture_post_submission(window, "batch", run)
        window.controller.startup_state = StartupState.CLOSING

        submitted.kwargs["on_success"]([])
        submitted.kwargs["on_removed"]()
        submitted.kwargs["on_removed"]()

        window._append_info.assert_not_called()
        window._start_next_queue_item.assert_not_called()
        window._begin_shutdown_countdown_if_requested.assert_not_called()
        window._refresh_background_targets.assert_not_called()
        window._release_download_lifecycle.assert_called_once_with()

    def test_batch_post_close_enters_closing_but_running_download_still_rejects(
        self,
    ) -> None:
        window, run = self._post_window()
        self._capture_post_submission(window, "batch", run)
        order: list[str] = []
        window.coordinator.has_active_tasks.return_value = True
        window.coordinator.begin_closing.side_effect = lambda: order.append(
            "coordinator"
        ) or False

        def begin_controller_closing() -> None:
            order.append("controller")
            window.controller.startup_state = StartupState.CLOSING

        window.controller.begin_closing = Mock(side_effect=begin_controller_closing)
        window.controller.engine = SimpleNamespace(current=None)
        window.controller.collector = SimpleNamespace(process=None)
        window.controller.stop_collector = Mock()
        event = SimpleNamespace(ignore=Mock(), accept=Mock())

        with patch("douk_manager.gui.QMessageBox.information"):
            MainWindow.closeEvent(window, event)

        self.assertEqual(order, ["coordinator", "controller"])
        event.ignore.assert_called_once_with()
        event.accept.assert_not_called()
        self.assertTrue(window._close_pending)

        blocked, blocked_run = self._post_window()
        blocked_run.running = True
        blocked.controller.begin_closing = Mock()
        blocked.controller.engine = SimpleNamespace(current=blocked_run)
        blocked.controller.collector = SimpleNamespace(process=None)
        blocked.controller.stop_collector = Mock()
        blocked_event = SimpleNamespace(ignore=Mock(), accept=Mock())

        with patch("douk_manager.gui.QMessageBox.information"):
            MainWindow.closeEvent(blocked, blocked_event)

        blocked_event.ignore.assert_called_once_with()
        blocked.controller.begin_closing.assert_not_called()
        blocked.coordinator.begin_closing.assert_not_called()

        different, stale_run = self._post_window()
        self._capture_post_submission(different, "batch", stale_run)
        different.queue_current = SimpleNamespace(
            task_log=Path("Data/DownloadTask_next.log"),
            running=False,
            result_review_waiting=False,
            process=SimpleNamespace(returncode=0),
        )
        different.controller.begin_closing = Mock()
        different.controller.engine = SimpleNamespace(current=None)
        different.controller.collector = SimpleNamespace(process=None)
        different.controller.stop_collector = Mock()
        different_event = SimpleNamespace(ignore=Mock(), accept=Mock())

        with patch("douk_manager.gui.QMessageBox.information"):
            MainWindow.closeEvent(different, different_event)

        different_event.ignore.assert_called_once_with()
        different.controller.begin_closing.assert_not_called()
        different.coordinator.begin_closing.assert_not_called()

    def test_real_post_close_cancel_and_critical_wait_have_one_terminal(self) -> None:
        app = QCoreApplication.instance() or QCoreApplication([])
        for first_actor, expected_outcome in (
            ("close", TaskState.CANCELLED),
            ("critical", TaskState.SUCCEEDED),
        ):
            with self.subTest(first_actor=first_actor):
                coordinator = BackgroundTaskCoordinator()
                window, run = self._post_window()
                arbitration_barrier = threading.Barrier(2)
                allow_critical = threading.Event()
                critical_attempted = threading.Event()
                critical_entered = threading.Event()
                release_action = threading.Event()
                action_done = threading.Event()
                window.coordinator = coordinator
                window._cancel_shutdown_for_new_work = Mock()
                window.statusBar = Mock(
                    return_value=SimpleNamespace(showMessage=Mock())
                )
                window._background_binding_is_current = (
                    lambda binding, generation: MainWindow._background_binding_is_current(
                        window, binding, generation
                    )
                )
                window._submit_background = (
                    lambda *args, **kwargs: MainWindow._submit_background(
                        window, *args, **kwargs
                    )
                )
                window._release_download_lifecycle = (
                    lambda: MainWindow._release_download_lifecycle(window)
                )
                window.controller.engine = SimpleNamespace(current=None)
                window.controller.collector = SimpleNamespace(process=None)
                window.controller.stop_collector = Mock()

                def run_post_actions(
                    timing: str, *, context: OperationContext
                ) -> list[str]:
                    self.assertEqual(timing, "batch")
                    try:
                        arbitration_barrier.wait(2.0)
                        if not allow_critical.wait(2.0):
                            raise RuntimeError("critical arbitration event timed out")
                        context.enter_critical_phase()
                        critical_entered.set()
                        if not release_action.wait(2.0):
                            raise RuntimeError("post action release event timed out")
                        context.raise_if_cancelled()
                        return []
                    finally:
                        critical_attempted.set()
                        action_done.set()

                window.controller.run_post_actions = run_post_actions
                close_order: list[str] = []
                original_begin_closing = coordinator.begin_closing

                def begin_coordinator_closing() -> bool:
                    close_order.append("coordinator")
                    return original_begin_closing()

                coordinator.begin_closing = begin_coordinator_closing

                def begin_controller_closing() -> None:
                    close_order.append("controller")
                    window.controller.startup_state = StartupState.CLOSING

                window.controller.begin_closing = begin_controller_closing
                settlements: list[tuple[object, ...]] = []
                removals: list[str] = []
                coordinator.task_settled.connect(
                    lambda *args: settlements.append(args)
                )
                coordinator.task_removed.connect(removals.append)
                coordinator.task_settled.connect(
                    lambda *args: MainWindow._on_background_task_settled(
                        window, *args
                    )
                )
                coordinator.task_removed.connect(
                    lambda task_id: MainWindow._on_background_task_removed(
                        window, task_id
                    )
                )

                MainWindow._run_post_actions_background(window, "batch", run)
                self.assertEqual(len(coordinator._records), 1)
                record = next(iter(coordinator._records.values()))
                thread = record.thread
                self.assertIsNotNone(thread)
                destroyed: list[bool] = []
                thread.destroyed.connect(lambda *_: destroyed.append(True))
                close_event = SimpleNamespace(ignore=Mock(), accept=Mock())

                arbitration_barrier.wait(2.0)
                if first_actor == "critical":
                    allow_critical.set()
                    self.assertTrue(critical_entered.wait(2.0))
                    self.assertTrue(record.operation_context.critical_to_completion)
                    self.assertFalse(record.operation_context.cancel_requested)
                    with patch("douk_manager.gui.QMessageBox.information"):
                        MainWindow.closeEvent(window, close_event)
                else:
                    with patch("douk_manager.gui.QMessageBox.information"):
                        MainWindow.closeEvent(window, close_event)
                    self.assertTrue(record.operation_context.cancel_requested)
                    self.assertFalse(record.operation_context.critical_to_completion)
                    allow_critical.set()
                    self.assertTrue(critical_attempted.wait(2.0))

                self.assertEqual(close_order, ["coordinator", "controller"])
                self.assertIs(window.controller.startup_state, StartupState.CLOSING)
                close_event.ignore.assert_called_once_with()
                release_action.set()
                self.assertTrue(action_done.wait(2.0))
                for _ in range(1000):
                    app.processEvents()
                    if not coordinator.has_active_tasks():
                        break
                for _ in range(1000):
                    QCoreApplication.sendPostedEvents(
                        None, QEvent.Type.DeferredDelete
                    )
                    app.processEvents()
                    if destroyed:
                        break

                self.assertFalse(coordinator.has_active_tasks())
                self.assertEqual(len(settlements), 1)
                self.assertIs(settlements[0][2], expected_outcome)
                self.assertEqual(removals, [record.task_id])
                self.assertEqual(destroyed, [True])
                self.assertFalse(coordinator.findChildren(QThread))
                self.assertEqual(window._download_post_bindings, {})
                window._start_next_queue_item.assert_not_called()
                window._refresh_background_targets.assert_not_called()
                window._begin_shutdown_countdown_if_requested.assert_not_called()
                window.controller.release_download_lifecycle.assert_called_once_with()


class PhaseTwoReadOnlyTaskTests(unittest.TestCase):
    @staticmethod
    def _history_run(path: str, *, complete: bool = True) -> DownloadTaskHistory:
        ended_at = datetime(2026, 8, 19, 1, 2, 3)
        row = AccountHistoryRow(
            ended_at=ended_at,
            a_number=7,
            status=AccountStatus.PRIVATE,
            completed_with_anomaly=False,
            task_template="Task_A7.json",
            task_log=Path(path),
        )
        return DownloadTaskHistory(
            ended_at=ended_at,
            task_template="Task_A7.json",
            exit_code=0,
            complete=complete,
            reliable=True,
            details_complete=complete,
            account_rows=(row,),
            task_log=Path(path),
        )

    def test_result_page_snapshot_derives_rows_and_integrity_from_one_scan(self) -> None:
        service = ResultHistoryService(Path("unused"))
        runs = (
            self._history_run("new.log"),
            self._history_run("old.log", complete=False),
        )
        service.list_runs = Mock(return_value=runs)
        context = Mock()

        snapshot = service.page_snapshot(limit=500, context=context)

        self.assertIsInstance(snapshot, ResultPageSnapshot)
        self.assertEqual(snapshot.runs, runs)
        self.assertEqual(snapshot.rows, runs[0].account_rows + runs[1].account_rows)
        self.assertEqual(snapshot.incomplete_old_runs, 1)
        service.list_runs.assert_called_once_with(limit=500, context=context)

    def test_controller_result_snapshot_delegates_without_second_scan(self) -> None:
        controller = ManagerController.__new__(ManagerController)
        controller.results = SimpleNamespace(page_snapshot=Mock(return_value="snapshot"))
        context = Mock()

        result = controller.result_snapshot(limit=123, context=context)

        self.assertEqual(result, "snapshot")
        controller.results.page_snapshot.assert_called_once_with(
            limit=123, context=context
        )

    def test_coalescing_cancels_current_and_starts_only_latest_after_removal(self) -> None:
        window = MainWindowBackgroundBindingTests._window()
        window._background_pending = {}
        first_action = lambda _context: "first"
        middle_action = lambda _context: "middle"
        latest_action = lambda _context: "latest"
        spec = MainWindowBackgroundBindingTests._spec("snapshot")

        first = MainWindow._submit_coalesced_background(window, spec, first_action)
        middle = MainWindow._submit_coalesced_background(window, spec, middle_action)
        latest = MainWindow._submit_coalesced_background(window, spec, latest_action)

        self.assertEqual(first, "task-1")
        self.assertEqual(middle, "task-1")
        self.assertEqual(latest, "task-1")
        self.assertEqual(window.coordinator.cancelled, ["task-1", "task-1"])
        self.assertEqual(len(window.coordinator.calls), 1)
        self.assertEqual(window._background_generations["snapshot"], 3)

        MainWindow._on_background_task_removed(window, "task-1")

        self.assertEqual(len(window.coordinator.calls), 2)
        self.assertIs(window.coordinator.calls[1][2], latest_action)
        self.assertEqual(window.coordinator.calls[1][1], 3)
        self.assertEqual(window._background_pending, {})

    def test_private_preview_result_is_rejected_after_inputs_change(self) -> None:
        window = SimpleNamespace(
            task_expression=SimpleNamespace(text=Mock(return_value="A8")),
            task_private_days=SimpleNamespace(value=Mock(return_value=3)),
            task_smart_private=SimpleNamespace(isChecked=Mock(return_value=True)),
            _replace_info=Mock(),
            task_output=object(),
            _smart_preview_lines=Mock(return_value=("preview",)),
        )

        applied = MainWindow._render_private_preview_if_current(
            window, "result", "A7", 3
        )

        self.assertFalse(applied)
        window._replace_info.assert_not_called()

    def test_full_backup_cancel_before_sqlite_cleans_temporary_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            from tests.helpers import make_test_paths

            paths = make_test_paths(Path(directory))
            context = OperationContext()
            service = BackupService(paths)
            context.request_cancel()
            with self.assertRaises(TaskCancelled):
                service.create_full_snapshot("Phase2", context=context)
            category = paths.backups / "Phase2"
            self.assertFalse(
                any(path.name.startswith(".") for path in category.iterdir())
                if category.is_dir()
                else False
            )

    def test_engine_update_cancel_after_backup_never_moves_formal_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            from tests.helpers import make_test_paths
            from tests.test_engine_update import write_update_zip

            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "new-engine.zip"
            write_update_zip(archive)
            context = OperationContext()
            backup = BackupService(paths)
            original = backup.create_full_snapshot

            def backup_then_cancel(*args, **kwargs):
                result = original(*args, **kwargs)
                context.request_cancel()
                return result

            backup.create_full_snapshot = backup_then_cancel
            service = EngineUpdateService(paths, backup)
            with patch("douk_manager.core.engine_update.shutil.move") as move:
                with self.assertRaises(TaskCancelled):
                    service.apply(archive, context=context)
            move.assert_not_called()


class PhaseOneFreezeTests(unittest.TestCase):
    def test_phase_one_worker_still_emits_one_settled_from_finally(self) -> None:
        source = inspect.getsource(TaskWorker.run)
        self.assertEqual(source.count("self.settled.emit"), 1)
        self.assertIn("finally:", source)

        settlements: list[tuple[Any, ...]] = []
        worker = TaskWorker("phase1-freeze", 1, lambda _token: {"ok": True}, CancellationToken())
        worker.settled.connect(lambda *args: settlements.append(args))
        worker.run()
        self.assertEqual(len(settlements), 1)
        self.assertEqual(settlements[0][2], TaskState.SUCCEEDED)


if __name__ == "__main__":
    unittest.main()
