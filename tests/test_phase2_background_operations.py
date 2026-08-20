from __future__ import annotations

import inspect
import os
import tempfile
import threading
import unittest
import weakref
from dataclasses import FrozenInstanceError, is_dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QEventLoop,
    QObject,
    QThread,
    QTimer,
    Qt,
    Slot,
)
from PySide6.QtWidgets import (
    QApplication,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTextEdit,
)

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
from douk_manager.integrations.collector import MigrationResult
from douk_manager.integrations.indexer import IndexResult
from douk_manager.startup import StartupState
from tests.helpers import make_test_paths


_QT_APPLICATION = QApplication.instance() or QApplication([])


class _ThreadFinishedObserver(QObject):
    def __init__(self, callback: Any, parent: QObject) -> None:
        super().__init__(parent)
        self._callback = callback

    @Slot()
    def observe(self) -> None:
        self._callback()

try:
    from douk_manager.operation import OperationContext, OperationProgress
except ModuleNotFoundError as exc:  # RED until Step 2 adds the protocol module.
    OperationContext: Any = None
    OperationProgress: Any = None
    OPERATION_IMPORT_ERROR = exc
else:
    OPERATION_IMPORT_ERROR = None


def dispose_test_coordinator(
    app: QCoreApplication,
    coordinator: BackgroundTaskCoordinator,
) -> None:
    if coordinator.has_active_tasks():
        raise AssertionError("cannot dispose a coordinator with active records")
    if coordinator.findChildren(QThread):
        raise AssertionError("cannot dispose a coordinator with live QThreads")
    coordinator.task_settled.disconnect()
    coordinator.task_removed.disconnect()
    destroyed: list[bool] = []
    coordinator.destroyed.connect(lambda *_args: destroyed.append(True))
    coordinator.deleteLater()
    QCoreApplication.sendPostedEvents(coordinator, QEvent.Type.DeferredDelete)
    app.processEvents()
    if destroyed != [True]:
        raise AssertionError("coordinator DeferredDelete was not processed")


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

    def test_settled_parent_admits_eight_resource_runtime_refresh_before_removal(
        self,
    ) -> None:
        app = QCoreApplication.instance() or QCoreApplication([])
        coordinator = BackgroundTaskCoordinator()
        runtime_resources = frozenset(
            {
                "engine_process",
                "collector_process",
                "engine_files",
                "volume",
                "settings",
                "video_tree",
                "index",
                "collector_data",
            }
        )
        window = SimpleNamespace(
            coordinator=coordinator,
            controller=SimpleNamespace(
                startup_state=StartupState.READY,
                logger=Mock(),
            ),
            _background_bindings={},
            _background_generations={},
            _background_pending={},
            _cancel_shutdown_for_new_work=Mock(),
            _append_info=Mock(),
            _apply_action_gate=Mock(),
            statusBar=Mock(return_value=SimpleNamespace(showMessage=Mock())),
        )
        window._background_binding_is_current = lambda binding, generation: (
            MainWindow._background_binding_is_current(window, binding, generation)
        )
        window._refresh_background_targets = lambda targets: (
            MainWindow._refresh_background_targets(window, targets)
        )

        parent_task_id: list[str] = []
        diagnostic_task_id: list[str] = []
        admission_state: list[tuple[bool, bool, bool]] = []
        diagnostic_success = Mock()
        diagnostic_started = threading.Event()
        diagnostic_admitted_before_parent_removal = threading.Event()
        release_diagnostic = threading.Event()
        settlements: list[tuple[object, ...]] = []
        removals: list[str] = []

        diagnostic_spec = TaskSpec(
            task_type="runtime_status_snapshot",
            display_name="refresh runtime status",
            resource_keys=runtime_resources,
            deduplicate_key="runtime_status_snapshot",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            refresh_targets=(),
            dynamic_cancellation=True,
        )

        def refresh_status() -> None:
            parent_record = coordinator._records[parent_task_id[0]]
            admission_state.append(
                (
                    parent_record.terminal_seen,
                    parent_record.thread_finished_seen,
                    parent_task_id[0] in coordinator._records,
                )
            )

            def diagnose(_context: object) -> dict[str, bool]:
                diagnostic_started.set()
                if not release_diagnostic.wait(timeout=3):
                    raise AssertionError("test did not release runtime diagnostic")
                return {"safe": True}

            task_id = MainWindow._submit_background(
                window,
                diagnostic_spec,
                diagnose,
                on_success=diagnostic_success,
                generation_key="runtime_status_snapshot",
            )
            if task_id is not None:
                diagnostic_task_id.append(task_id)

        window._refresh_status = refresh_status
        coordinator.task_settled.connect(
            lambda *args: MainWindow._on_background_task_settled(window, *args)
        )
        coordinator.task_removed.connect(
            lambda task_id: MainWindow._on_background_task_removed(window, task_id)
        )

        def observe_settled(*args: object) -> None:
            settlements.append(args)
            if not parent_task_id or args[0] != parent_task_id[0]:
                return
            parent_record = coordinator._records[parent_task_id[0]]
            self.assertTrue(parent_record.terminal_seen)
            self.assertFalse(parent_record.thread_finished_seen)
            self.assertEqual(len(diagnostic_task_id), 1)
            child_id = diagnostic_task_id[0]
            child_record = coordinator._records[child_id]
            child_binding = window._background_bindings[child_id]
            self.assertFalse(child_record.terminal_seen)
            self.assertEqual(child_binding.generation, child_record.generation)
            self.assertEqual(
                window._background_generations[child_binding.generation_key],
                child_record.generation,
            )
            diagnostic_admitted_before_parent_removal.set()
            release_diagnostic.set()

        coordinator.task_settled.connect(observe_settled)
        coordinator.task_removed.connect(removals.append)

        parent_spec = TaskSpec(
            task_type="parent_write",
            display_name="parent write",
            resource_keys=runtime_resources,
            deduplicate_key="parent_write",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            refresh_targets=("runtime_status",),
            dynamic_cancellation=True,
        )
        submitted_parent = MainWindow._submit_background(
            window,
            parent_spec,
            lambda _context: {"parent": "settled"},
            generation_key="parent_write",
        )
        self.assertIsNotNone(submitted_parent)
        parent_task_id.append(submitted_parent)

        for _ in range(2000):
            app.processEvents()
            if not coordinator.has_active_tasks():
                break
        for _ in range(2000):
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            if not coordinator.findChildren(QThread):
                break

        self.assertEqual(admission_state, [(True, False, True)])
        self.assertEqual(len(diagnostic_task_id), 1)
        self.assertTrue(diagnostic_started.is_set())
        self.assertTrue(diagnostic_admitted_before_parent_removal.is_set())
        diagnostic_success.assert_called_once_with({"safe": True})
        self.assertEqual(
            [entry[0] for entry in settlements],
            [parent_task_id[0], diagnostic_task_id[0]],
        )
        self.assertEqual(set(removals), {parent_task_id[0], diagnostic_task_id[0]})
        self.assertFalse(coordinator.has_active_tasks())
        self.assertEqual(window._background_bindings, {})
        self.assertEqual(window._background_pending, {})
        self.assertFalse(coordinator.findChildren(QThread))
        dispose_test_coordinator(app, coordinator)
        window.coordinator = None

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


class ReadOnlyEntryCoordinatorContractTests(unittest.TestCase):
    RUNTIME_RESOURCES = frozenset(
        {
            "engine_process",
            "collector_process",
            "engine_files",
            "volume",
            "settings",
            "video_tree",
            "index",
            "collector_data",
        }
    )

    @staticmethod
    def _window(state: StartupState = StartupState.READY) -> SimpleNamespace:
        controller = SimpleNamespace(
            startup_state=state,
            logger=Mock(),
            result_snapshot=Mock(return_value="result-payload"),
            list_tasks=Mock(return_value=(Path("Task_A1.json"),)),
            preview_private_skip=Mock(return_value="private-payload"),
            screenshot_preview=Mock(
                return_value=SimpleNamespace(
                    recognized_folders=1,
                    recognized_images=2,
                    movable=2,
                    missing_account_folder=0,
                    already_existing=0,
                    unmatched_folders=0,
                )
            ),
            runtime_status_snapshot=Mock(return_value={"engine_running": False}),
        )
        window = SimpleNamespace(
            coordinator=_BindingCoordinator(),
            controller=controller,
            _background_bindings={},
            _background_generations={"unrelated": 9},
            _background_pending={"unrelated": object()},
            _cancel_shutdown_for_new_work=Mock(),
            _append_info=Mock(),
            _apply_action_gate=Mock(),
            _refresh_background_targets=Mock(),
            statusBar=Mock(return_value=SimpleNamespace(showMessage=Mock())),
            refresh_all=lambda **_kwargs: None,
            result_table=object(),
            result_refresh_button=SimpleNamespace(setEnabled=Mock()),
            task_list=object(),
            task_refresh_button=SimpleNamespace(setEnabled=Mock()),
            task_smart_private=SimpleNamespace(isChecked=Mock(return_value=True)),
            task_expression=SimpleNamespace(text=Mock(return_value="A1-A3")),
            task_private_days=SimpleNamespace(value=Mock(return_value=30)),
            task_output=object(),
            task_preview_button=SimpleNamespace(setEnabled=Mock()),
            post_output=object(),
            screenshot_preview_button=SimpleNamespace(setEnabled=Mock()),
            refresh_status_button=SimpleNamespace(setEnabled=Mock()),
            _render_result_snapshot=Mock(),
            _render_task_paths=Mock(),
            _render_private_preview_if_current=Mock(return_value=True),
            _render_health_snapshot=Mock(),
            _replace_info=Mock(),
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
        return window

    @staticmethod
    def _invoke(window: SimpleNamespace, entry: str) -> None:
        entries = {
            "result": MainWindow.refresh_results,
            "task": MainWindow.refresh_tasks,
            "private": MainWindow._preview_task,
            "screenshot": MainWindow._preview_screenshots,
            "runtime": MainWindow._refresh_status,
        }
        entries[entry](window)

    def test_ready_real_entries_submit_exact_specs_context_and_one_direct_render(
        self,
    ) -> None:
        cases = (
            (
                "result",
                "result_page_snapshot",
                "刷新下载结果",
                {"result_logs"},
                "result_page_snapshot",
            ),
            (
                "task",
                "task_list_snapshot",
                "刷新任务列表",
                {"task_templates", "settings"},
                "task_list_snapshot",
            ),
            (
                "private",
                "private_preview",
                "智能私密预览",
                {"result_logs", "settings"},
                "private_preview",
            ),
            (
                "screenshot",
                "screenshot_preview",
                "预览截图归档",
                {"screenshots", "video_tree"},
                "screenshot_preview",
            ),
        )
        for entry, task_type, display_name, resources, deduplicate_key in cases:
            with self.subTest(entry=entry):
                window = self._window()
                self._invoke(window, entry)

                self.assertEqual(len(window.coordinator.calls), 1)
                spec, generation, action = window.coordinator.calls[0]
                self.assertEqual(spec.task_type, task_type)
                self.assertEqual(spec.display_name, display_name)
                self.assertEqual(spec.resource_keys, frozenset(resources))
                self.assertEqual(spec.deduplicate_key, deduplicate_key)
                self.assertTrue(spec.cancellable)
                self.assertIs(spec.close_policy, ClosePolicy.CANCEL)
                self.assertEqual(spec.refresh_targets, ())
                self.assertTrue(spec.dynamic_cancellation)
                self.assertFalse(spec.critical_write_started)
                self.assertFalse(spec.allow_during_closing)
                self.assertEqual(generation, 1)

                context = Mock(name=f"{entry}_context")
                payload = action(context)
                task_id = next(iter(window._background_bindings))
                self.assertEqual(
                    window._background_bindings[task_id].generation_key,
                    deduplicate_key,
                )
                MainWindow._on_background_task_settled(
                    window, task_id, generation, TaskState.SUCCEEDED, payload
                )
                if entry == "result":
                    window.controller.result_snapshot.assert_called_once_with(
                        limit=500, context=context
                    )
                    window._render_result_snapshot.assert_called_once_with(payload)
                elif entry == "task":
                    window.controller.list_tasks.assert_called_once_with(context=context)
                    window._render_task_paths.assert_called_once_with(tuple(payload))
                elif entry == "private":
                    window.controller.preview_private_skip.assert_called_once_with(
                        "A1-A3", 30, context=context
                    )
                    window._render_private_preview_if_current.assert_called_once_with(
                        payload, "A1-A3", 30
                    )
                else:
                    window.controller.screenshot_preview.assert_called_once_with(
                        context=context
                    )
                    window._replace_info.assert_called_once()
                window._refresh_background_targets.assert_called_once_with(())

    def test_non_ready_read_only_entries_leave_all_admission_and_ui_state_unchanged(
        self,
    ) -> None:
        blocked_states = (
            StartupState.DEGRADED_READ_ONLY,
            StartupState.BOOTSTRAPPING,
            StartupState.SAFETY_CHECKING,
            StartupState.CLOSING,
        )
        for state in blocked_states:
            for entry in ("result", "task", "private", "screenshot"):
                with self.subTest(state=state, entry=entry):
                    window = self._window(state)
                    generations = dict(window._background_generations)
                    pending = dict(window._background_pending)
                    bindings = dict(window._background_bindings)

                    self._invoke(window, entry)

                    self.assertEqual(window.coordinator.calls, [])
                    self.assertEqual(window._background_generations, generations)
                    self.assertEqual(window._background_pending, pending)
                    self.assertEqual(window._background_bindings, bindings)
                    window._append_info.assert_not_called()
                    window._replace_info.assert_not_called()
                    window._render_result_snapshot.assert_not_called()
                    window._render_task_paths.assert_not_called()
                    window._render_private_preview_if_current.assert_not_called()
                    for button in (
                        window.result_refresh_button,
                        window.task_refresh_button,
                        window.task_preview_button,
                        window.screenshot_preview_button,
                    ):
                        button.setEnabled.assert_not_called()

    def test_runtime_diagnostic_state_gate_and_exact_eight_resource_conflicts(
        self,
    ) -> None:
        for state in (StartupState.READY, StartupState.DEGRADED_READ_ONLY):
            with self.subTest(allowed=state):
                window = self._window(state)
                self._invoke(window, "runtime")
                self.assertEqual(len(window.coordinator.calls), 1)

        for state in (
            StartupState.BOOTSTRAPPING,
            StartupState.SAFETY_CHECKING,
            StartupState.CLOSING,
        ):
            with self.subTest(blocked=state):
                window = self._window(state)
                generations = dict(window._background_generations)
                pending = dict(window._background_pending)
                self._invoke(window, "runtime")
                self.assertEqual(window.coordinator.calls, [])
                self.assertEqual(window._background_generations, generations)
                self.assertEqual(window._background_pending, pending)
                window._render_health_snapshot.assert_not_called()
                window.refresh_status_button.setEnabled.assert_not_called()

        window = self._window()
        self._invoke(window, "runtime")
        spec, _generation, action = window.coordinator.calls[0]
        self.assertEqual(spec.task_type, "runtime_status_snapshot")
        self.assertEqual(spec.display_name, "刷新运行状态")
        self.assertEqual(spec.deduplicate_key, "runtime_status_snapshot")
        self.assertEqual(spec.resource_keys, self.RUNTIME_RESOURCES)
        self.assertNotIn("runtime_status", spec.resource_keys)
        self.assertNotIn("static_paths", spec.resource_keys)
        self.assertEqual(spec.refresh_targets, ())
        self.assertTrue(spec.cancellable)
        self.assertIs(spec.close_policy, ClosePolicy.CANCEL)
        self.assertTrue(spec.dynamic_cancellation)
        runtime_task_id = next(iter(window._background_bindings))
        self.assertEqual(
            window._background_bindings[runtime_task_id].generation_key,
            "runtime_status_snapshot",
        )
        context = Mock(name="runtime_context")
        self.assertEqual(action(context), {"engine_running": False})
        window.controller.runtime_status_snapshot.assert_called_once_with(context=context)

        for resource in sorted(self.RUNTIME_RESOURCES):
            with self.subTest(conflict=resource):
                coordinator = BackgroundTaskCoordinator()
                coordinator._records["writer"] = TaskRecord(
                    task_id="writer",
                    spec=TaskSpec(
                        task_type=f"{resource}_writer",
                        display_name=f"{resource} writer",
                        resource_keys=frozenset({resource}),
                        deduplicate_key=f"{resource}_writer",
                    ),
                    generation=1,
                    token=CancellationToken(),
                    thread=None,
                    worker=None,
                    state=TaskState.RUNNING,
                )
                with self.assertRaisesRegex(TaskRejectedError, resource):
                    coordinator._validate_start(spec)

        coordinator = BackgroundTaskCoordinator()
        coordinator._records["result-reader"] = TaskRecord(
            task_id="result-reader",
            spec=TaskSpec(
                task_type="result-reader",
                display_name="result reader",
                resource_keys=frozenset({"result_logs"}),
                deduplicate_key="result-reader",
            ),
            generation=1,
            token=CancellationToken(),
            thread=None,
            worker=None,
            state=TaskState.RUNNING,
        )
        coordinator._validate_start(spec)

    def test_result_private_and_screenshot_resource_conflicts_are_symmetric(
        self,
    ) -> None:
        specs: dict[str, TaskSpec] = {}
        for entry in ("result", "task", "private", "screenshot"):
            window = self._window()
            self._invoke(window, entry)
            specs[entry] = window.coordinator.calls[0][0]

        summary = TaskSpec(
            task_type="download_summary",
            display_name="summary",
            resource_keys=frozenset({"task_logs", "result_logs"}),
            deduplicate_key="summary:run",
        )
        settings_write = TaskSpec(
            task_type="settings_write",
            display_name="settings write",
            resource_keys=frozenset({"settings"}),
            deduplicate_key="settings_write",
        )
        screenshot_archive = TaskSpec(
            task_type="screenshot_archive",
            display_name="archive screenshots",
            resource_keys=frozenset({"screenshots", "video_tree"}),
            deduplicate_key="screenshot_archive",
        )
        index_refresh = TaskSpec(
            task_type="index_refresh",
            display_name="refresh index",
            resource_keys=frozenset({"video_tree", "index"}),
            deduplicate_key="index_refresh",
        )

        for left, right, resource in (
            (specs["result"], summary, "result_logs"),
            (specs["task"], settings_write, "settings"),
            (specs["private"], summary, "result_logs"),
            (specs["private"], settings_write, "settings"),
            (specs["screenshot"], screenshot_archive, "screenshots"),
            (specs["screenshot"], index_refresh, "video_tree"),
        ):
            for active, candidate in ((left, right), (right, left)):
                coordinator = BackgroundTaskCoordinator()
                coordinator._records["active"] = TaskRecord(
                    task_id="active",
                    spec=active,
                    generation=1,
                    token=CancellationToken(),
                    thread=None,
                    worker=None,
                    state=TaskState.RUNNING,
                )
                with self.assertRaisesRegex(TaskRejectedError, resource):
                    coordinator._validate_start(candidate)

        coordinator = BackgroundTaskCoordinator()
        coordinator._records["result-reader"] = TaskRecord(
            task_id="result-reader",
            spec=TaskSpec(
                task_type="result-reader",
                display_name="result reader",
                resource_keys=frozenset({"result_logs"}),
                deduplicate_key="result-reader",
            ),
            generation=1,
            token=CancellationToken(),
            thread=None,
            worker=None,
            state=TaskState.RUNNING,
        )
        coordinator._validate_start(specs["task"])

    def test_rejected_resource_admission_does_not_replace_generation_or_binding(
        self,
    ) -> None:
        for entry in ("result", "private", "screenshot"):
            with self.subTest(entry=entry):
                window = self._window()
                sentinel = SimpleNamespace(generation_key="settings_write")
                window._background_bindings["existing"] = sentinel
                window.coordinator.error = TaskRejectedError("synthetic resource conflict")
                generations = dict(window._background_generations)
                pending = dict(window._background_pending)
                bindings = dict(window._background_bindings)

                self._invoke(window, entry)

                self.assertEqual(window._background_generations, generations)
                self.assertEqual(window._background_pending, pending)
                self.assertEqual(window._background_bindings, bindings)
                window._render_result_snapshot.assert_not_called()
                window._render_private_preview_if_current.assert_not_called()
                window._replace_info.assert_not_called()

    def test_stale_or_closing_callbacks_only_remove_read_only_bindings(self) -> None:
        for entry in ("result", "task", "private", "screenshot", "runtime"):
            for terminal_mode in ("stale", "closing"):
                with self.subTest(entry=entry, terminal_mode=terminal_mode):
                    window = self._window()
                    self._invoke(window, entry)
                    task_id = next(iter(window._background_bindings))
                    binding = window._background_bindings[task_id]
                    if terminal_mode == "stale":
                        window._background_generations[binding.generation_key] = 2
                    else:
                        window.controller.startup_state = StartupState.CLOSING

                    MainWindow._on_background_task_settled(
                        window,
                        task_id,
                        binding.generation,
                        TaskState.SUCCEEDED,
                        SimpleNamespace(
                            recognized_folders=1,
                            recognized_images=1,
                            movable=1,
                            missing_account_folder=0,
                            already_existing=0,
                            unmatched_folders=0,
                        ),
                    )
                    MainWindow._on_background_task_removed(window, task_id)

                    self.assertNotIn(task_id, window._background_bindings)
                    self.assertEqual(len(window.coordinator.calls), 1)
                    window._render_result_snapshot.assert_not_called()
                    window._render_task_paths.assert_not_called()
                    window._render_private_preview_if_current.assert_not_called()
                    window._render_health_snapshot.assert_not_called()
                    window._replace_info.assert_not_called()
                    window._refresh_background_targets.assert_not_called()

    def test_result_and_private_rapid_requests_render_only_latest_generation(
        self,
    ) -> None:
        for entry in ("result", "private"):
            with self.subTest(entry=entry):
                window = self._window()
                self._invoke(window, entry)
                first_task_id = next(iter(window._background_bindings))
                self._invoke(window, entry)
                self._invoke(window, entry)
                key = (
                    "result_page_snapshot" if entry == "result" else "private_preview"
                )
                self.assertEqual(window._background_generations[key], 3)
                self.assertEqual(window._background_pending[key].generation, 3)
                self.assertEqual(
                    window.coordinator.cancelled,
                    [first_task_id, first_task_id],
                )

                MainWindow._on_background_task_settled(
                    window, first_task_id, 1, TaskState.SUCCEEDED, "old"
                )
                MainWindow._on_background_task_removed(window, first_task_id)
                latest_task_id = next(
                    task_id
                    for task_id in window._background_bindings
                    if task_id != "existing"
                )
                MainWindow._on_background_task_settled(
                    window, latest_task_id, 3, TaskState.SUCCEEDED, "latest"
                )

                if entry == "result":
                    window._render_result_snapshot.assert_called_once_with("latest")
                else:
                    window._render_private_preview_if_current.assert_called_once_with(
                        "latest", "A1-A3", 30
                    )
                window._refresh_background_targets.assert_called_once_with(())

    def test_cancelled_screenshot_and_runtime_scans_never_write_old_ui(self) -> None:
        for entry in ("screenshot", "runtime"):
            with self.subTest(entry=entry):
                window = self._window()
                self._invoke(window, entry)
                task_id = next(iter(window._background_bindings))
                binding = window._background_bindings[task_id]

                MainWindow._on_background_task_settled(
                    window,
                    task_id,
                    binding.generation,
                    TaskState.CANCELLED,
                    "cancelled at deterministic scan checkpoint",
                )

                window._append_info.assert_not_called()
                window._replace_info.assert_not_called()
                window._render_health_snapshot.assert_not_called()
                window._refresh_background_targets.assert_not_called()


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
        thread = None
        dispose_test_coordinator(app, coordinator)
        window.coordinator = None

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
        destroyed: list[str] = []
        expected_destroyed = 0

        def retain_current_thread(label: str) -> str:
            nonlocal expected_destroyed
            self.assertEqual(len(coordinator._records), 1)
            record = next(iter(coordinator._records.values()))
            self.assertIsNotNone(record.thread)
            record.thread.destroyed.connect(lambda *_: destroyed.append(label))
            expected_destroyed += 1
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
                if len(destroyed) == expected_destroyed:
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
                if len(destroyed) == expected_destroyed:
                    break

        self.assertEqual(reader_calls, [1, 2])
        self.assertEqual(window._render_result_snapshot.call_count, 2)
        expected_ids = [first_reader_id, summary_id, second_reader_id]
        self.assertEqual([event[0] for event in settlements], expected_ids)
        self.assertEqual(removals, expected_ids)
        self.assertEqual(destroyed, ["reader-1", "summary", "reader-2"])
        self.assertFalse(coordinator.findChildren(QThread))
        dispose_test_coordinator(app, coordinator)
        window.coordinator = None

    def test_summary_and_result_readers_share_result_logs_resource(self) -> None:
        window, run = self._summary_window(Path("Data/DownloadTask_20260820.log"))
        summary_spec = self._start_and_capture(window, run).args[0]

        result_window = SimpleNamespace(
            result_table=object(),
            result_refresh_button=None,
            controller=SimpleNamespace(
                startup_state=StartupState.READY,
                result_snapshot=Mock(),
            ),
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
            controller=SimpleNamespace(
                startup_state=StartupState.READY,
                preview_private_skip=Mock(),
            ),
            _submit_coalesced_background=Mock(),
        )
        MainWindow._preview_task(private_window)
        private_spec = private_window._submit_coalesced_background.call_args.args[0]

        self.assertEqual(result_spec.resource_keys, frozenset({"result_logs"}))
        self.assertEqual(
            private_spec.resource_keys,
            frozenset({"result_logs", "settings"}),
        )

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
                thread = None
                dispose_test_coordinator(app, coordinator)
                window.coordinator = None


class EngineUpdateCoordinatorCorrectionTests(unittest.TestCase):
    @staticmethod
    def _preview_payload(archive: Path) -> SimpleNamespace:
        return SimpleNamespace(
            archive=archive,
            archive_sha256="a" * 64,
            package_prefix="package",
            file_count=2,
            uncompressed_bytes=2048,
            main_exe_bytes=1024,
            contains_packaged_volume=False,
        )

    @staticmethod
    def _apply_payload(archive: Path) -> SimpleNamespace:
        return SimpleNamespace(
            archive=archive,
            backup_path=Path("synthetic-backup"),
            rollback_path=Path("synthetic-rollback"),
            old_main_sha256="b" * 64,
            new_main_sha256="c" * 64,
        )

    @classmethod
    def _window(
        cls,
        archive: Path,
        state: StartupState = StartupState.READY,
    ) -> SimpleNamespace:
        controller = SimpleNamespace(
            startup_state=state,
            logger=Mock(),
            config=SimpleNamespace(collector_port=18765),
            preview_engine_update=Mock(return_value=cls._preview_payload(archive)),
            apply_engine_update=Mock(return_value=cls._apply_payload(archive)),
            start_collector=Mock(return_value=Path("synthetic-collector.log")),
        )
        window = SimpleNamespace(
            coordinator=_BindingCoordinator(),
            controller=controller,
            _background_bindings={},
            _background_generations={},
            _background_pending={},
            _cancel_shutdown_for_new_work=Mock(),
            _append_info=Mock(),
            _replace_info=Mock(),
            _apply_action_gate=Mock(),
            _refresh_background_targets=Mock(),
            statusBar=Mock(return_value=SimpleNamespace(showMessage=Mock())),
            engine_update_zip=SimpleNamespace(text=Mock(return_value=os.fspath(archive))),
            settings_output=object(),
            collector_output=object(),
            engine_update_preview_button=SimpleNamespace(setEnabled=Mock()),
            engine_update_apply_button=SimpleNamespace(setEnabled=Mock()),
            collector_start_button=SimpleNamespace(setEnabled=Mock()),
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
        window._submit_engine_update_apply = lambda selected: (
            MainWindow._submit_engine_update_apply(window, selected)
        )
        return window

    @staticmethod
    def _engine_preview_key(archive: Path) -> str:
        canonical = os.path.normcase(
            os.path.normpath(os.path.abspath(os.fspath(archive)))
        )
        return f"engine_update_preview:{canonical}"

    @staticmethod
    def _record(spec: TaskSpec) -> TaskRecord:
        return TaskRecord(
            task_id="active",
            spec=spec,
            generation=1,
            token=CancellationToken(),
            thread=None,
            worker=None,
            state=TaskState.RUNNING,
        )

    def test_engine_preview_gui_ready_gate_precedes_all_admission(self) -> None:
        for entry in (
            MainWindow._preview_engine_update,
            MainWindow._apply_engine_update,
        ):
            for state in (
                StartupState.BOOTSTRAPPING,
                StartupState.SAFETY_CHECKING,
                StartupState.DEGRADED_READ_ONLY,
                StartupState.CLOSING,
            ):
                with self.subTest(entry=entry.__name__, state=state):
                    window = self._window(Path("synthetic-engine.zip"), state)
                    generations = dict(window._background_generations)
                    pending = dict(window._background_pending)

                    entry(window)

                    self.assertEqual(window.coordinator.calls, [])
                    self.assertEqual(window._background_bindings, {})
                    self.assertEqual(window._background_generations, generations)
                    self.assertEqual(window._background_pending, pending)
                    window.controller.preview_engine_update.assert_not_called()
                    window._replace_info.assert_not_called()
                    window._refresh_background_targets.assert_not_called()
                    window.engine_update_preview_button.setEnabled.assert_not_called()
                    window.engine_update_apply_button.setEnabled.assert_not_called()

    def test_engine_preview_uses_canonical_per_zip_dedup_and_global_generation(
        self,
    ) -> None:
        relative = Path("synthetic-engine-updates") / "Engine.ZIP"
        archive = Path.cwd() / relative
        equivalent_paths = (
            relative,
            archive,
            archive.parent / "nested" / ".." / archive.name,
            Path(os.fspath(archive).swapcase()),
        )
        keys: list[str | None] = []
        generation_keys: list[str] = []
        for candidate in equivalent_paths:
            window = self._window(candidate)
            MainWindow._preview_engine_update(window)
            spec, _generation, _action = window.coordinator.calls[0]
            keys.append(spec.deduplicate_key)
            binding = next(iter(window._background_bindings.values()))
            generation_keys.append(binding.generation_key)

        expected = self._engine_preview_key(archive)
        self.assertEqual(keys, [expected] * len(equivalent_paths))
        self.assertEqual(
            generation_keys,
            ["engine_update_preview"] * len(equivalent_paths),
        )

        other = archive.parent / "other.zip"
        other_window = self._window(other)
        MainWindow._preview_engine_update(other_window)
        other_spec = other_window.coordinator.calls[0][0]
        self.assertEqual(other_spec.deduplicate_key, self._engine_preview_key(other))
        self.assertNotEqual(other_spec.deduplicate_key, expected)

        apply_window = self._window(archive)
        MainWindow._apply_engine_update(apply_window)
        apply_preview_spec = apply_window.coordinator.calls[0][0]
        apply_binding = next(iter(apply_window._background_bindings.values()))
        self.assertEqual(apply_preview_spec.deduplicate_key, expected)
        self.assertEqual(apply_binding.generation_key, "engine_update_preview")

    def test_both_preview_actions_forward_the_worker_operation_context(self) -> None:
        archive = Path("synthetic-engine.zip")
        for entry in (
            MainWindow._preview_engine_update,
            MainWindow._apply_engine_update,
        ):
            with self.subTest(entry=entry.__name__):
                window = self._window(archive)
                entry(window)
                spec, generation, action = window.coordinator.calls[0]
                context = Mock(name=f"{entry.__name__}_context")

                result = action(context)

                self.assertEqual(spec.resource_keys, frozenset({"engine_files"}))
                self.assertTrue(spec.dynamic_cancellation)
                self.assertEqual(generation, 1)
                self.assertIs(result, window.controller.preview_engine_update.return_value)
                window.controller.preview_engine_update.assert_called_once_with(
                    archive, context=context
                )

    def test_rapid_a_b_c_preview_only_c_renders_and_stale_removals_do_not_enable(
        self,
    ) -> None:
        archives = tuple(Path(f"engine-{name}.zip") for name in ("A", "B", "C"))
        window = self._window(archives[0])
        button = window.engine_update_preview_button
        rendered = threading.Event()
        window._replace_info.side_effect = lambda *_args, **_kwargs: rendered.set()

        MainWindow._preview_engine_update(window)
        first_task_id = next(iter(window._background_bindings))
        window.engine_update_zip.text.return_value = os.fspath(archives[1])
        MainWindow._preview_engine_update(window)
        MainWindow._on_background_task_settled(
            window,
            first_task_id,
            1,
            TaskState.SUCCEEDED,
            self._preview_payload(archives[0]),
        )
        MainWindow._on_background_task_removed(window, first_task_id)
        self.assertFalse(rendered.is_set())

        second_task_id = next(iter(window._background_bindings))
        window.engine_update_zip.text.return_value = os.fspath(archives[2])
        MainWindow._preview_engine_update(window)
        MainWindow._on_background_task_settled(
            window,
            second_task_id,
            2,
            TaskState.SUCCEEDED,
            self._preview_payload(archives[1]),
        )
        MainWindow._on_background_task_removed(window, second_task_id)
        self.assertFalse(rendered.is_set())

        third_task_id = next(iter(window._background_bindings))
        MainWindow._on_background_task_settled(
            window,
            third_task_id,
            3,
            TaskState.SUCCEEDED,
            self._preview_payload(archives[2]),
        )
        self.assertTrue(rendered.is_set())

        self.assertEqual(window._background_generations["engine_update_preview"], 3)
        self.assertEqual(window.coordinator.cancelled, [first_task_id, second_task_id])
        self.assertEqual(len(window.coordinator.calls), 3)
        window._replace_info.assert_called_once()
        rendered_messages = window._replace_info.call_args.args[1:]
        self.assertIn(f"ZIP：{archives[2]}", rendered_messages)
        self.assertNotIn(f"ZIP：{archives[0]}", rendered_messages)
        self.assertNotIn(f"ZIP：{archives[1]}", rendered_messages)
        self.assertNotIn(True, [call.args[0] for call in button.setEnabled.call_args_list])

    def test_engine_resources_conflict_while_result_logs_remain_parallel(self) -> None:
        preview_a_window = self._window(Path("engine-A.zip"))
        preview_b_window = self._window(Path("engine-B.zip"))
        apply_window = self._window(Path("engine-A.zip"))
        collector_window = self._window(Path("engine-A.zip"))
        MainWindow._preview_engine_update(preview_a_window)
        MainWindow._preview_engine_update(preview_b_window)
        MainWindow._submit_engine_update_apply(apply_window, Path("engine-A.zip"))
        MainWindow._start_collector(collector_window)
        preview_a = preview_a_window.coordinator.calls[0][0]
        preview_b = preview_b_window.coordinator.calls[0][0]
        apply_spec = apply_window.coordinator.calls[0][0]
        collector_start = collector_window.coordinator.calls[0][0]

        self.assertEqual(
            apply_spec.resource_keys,
            frozenset(
                {
                    "engine_process",
                    "collector_process",
                    "engine_files",
                    "volume",
                    "settings",
                }
            ),
        )
        for active, candidate, resource in (
            (preview_a, preview_b, "engine_files"),
            (preview_a, apply_spec, "engine_files"),
            (apply_spec, preview_a, "engine_files"),
            (apply_spec, collector_start, "collector_process"),
        ):
            with self.subTest(
                active=active.task_type,
                candidate=candidate.task_type,
            ):
                coordinator = BackgroundTaskCoordinator()
                coordinator._records["active"] = self._record(active)
                with self.assertRaisesRegex(TaskRejectedError, resource):
                    coordinator._validate_start(candidate)

        coordinator = BackgroundTaskCoordinator()
        coordinator._records["active"] = self._record(preview_a)
        coordinator._validate_start(
            TaskSpec(
                task_type="result_reader",
                display_name="result reader",
                resource_keys=frozenset({"result_logs"}),
                deduplicate_key="result_reader",
            )
        )

    def test_apply_success_refreshes_only_runtime_status_and_closing_drops_ui(
        self,
    ) -> None:
        archive = Path("synthetic-engine.zip")
        window = self._window(archive)
        MainWindow._submit_engine_update_apply(window, archive)
        task_id = next(iter(window._background_bindings))
        binding = window._background_bindings[task_id]
        self.assertEqual(binding.spec.refresh_targets, ("runtime_status",))
        self.assertNotIn("static_paths", binding.spec.refresh_targets)

        payload = self._apply_payload(archive)
        MainWindow._on_background_task_settled(
            window, task_id, binding.generation, TaskState.SUCCEEDED, payload
        )
        window._replace_info.assert_called_once()
        window._refresh_background_targets.assert_called_once_with(("runtime_status",))

        closing = self._window(archive)
        MainWindow._submit_engine_update_apply(closing, archive)
        closing_task_id = next(iter(closing._background_bindings))
        closing_binding = closing._background_bindings[closing_task_id]
        closing.controller.startup_state = StartupState.CLOSING
        MainWindow._on_background_task_settled(
            closing,
            closing_task_id,
            closing_binding.generation,
            TaskState.SUCCEEDED,
            payload,
        )
        closing._replace_info.assert_not_called()
        closing._refresh_background_targets.assert_not_called()


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


class RealGuiEvidenceEntryTests(unittest.TestCase):
    _ENTRIES = ("stop", "migration", "backup", "self_test")

    @classmethod
    def _run_until(cls, condition: Any, *, timeout_ms: int = 4000) -> None:
        if condition():
            return
        loop = QEventLoop()
        timed_out: list[bool] = []
        poll = QTimer()
        poll.setInterval(1)
        timeout = QTimer()
        timeout.setSingleShot(True)

        def check() -> None:
            if condition():
                poll.stop()
                loop.quit()

        timeout.timeout.connect(lambda: (timed_out.append(True), loop.quit()))
        poll.timeout.connect(check)
        poll.start()
        timeout.start(timeout_ms)
        loop.exec()
        poll.stop()
        timeout.stop()
        if timed_out or not condition():
            raise AssertionError("bounded real GUI evidence event loop timed out")

    @staticmethod
    def _expected_spec(entry: str) -> dict[str, object]:
        common = {
            "critical_write_started": False,
            "allow_during_closing": False,
        }
        expected = {
            "stop": {
                "task_type": "collector_stop",
                "display_name": "停止账号采集服务",
                "resource_keys": frozenset({"collector_process"}),
                "deduplicate_key": "collector_stop",
                "cancellable": False,
                "close_policy": ClosePolicy.WAIT,
                "refresh_targets": ("runtime_status",),
                "dynamic_cancellation": False,
            },
            "migration": {
                "task_type": "collector_migration",
                "display_name": "迁移旧采集器数据",
                "resource_keys": frozenset(
                    {"collector_process", "collector_data", "settings", "screenshots"}
                ),
                "deduplicate_key": "collector_migration",
                "cancellable": False,
                "close_policy": ClosePolicy.WAIT,
                "refresh_targets": ("runtime_status",),
                "dynamic_cancellation": False,
            },
            "backup": {
                "task_type": "full_volume_backup",
                "display_name": "完整 Volume 备份",
                "resource_keys": frozenset({"volume", "settings"}),
                "deduplicate_key": "full_volume_backup",
                "cancellable": True,
                "close_policy": ClosePolicy.CANCEL,
                "refresh_targets": ("runtime_status",),
                "dynamic_cancellation": True,
            },
            "self_test": {
                "task_type": "index_cleanup_self_test",
                "display_name": "自检索引清理",
                "resource_keys": frozenset({"index"}),
                "deduplicate_key": "index_operation",
                "cancellable": True,
                "close_policy": ClosePolicy.CANCEL,
                "refresh_targets": (),
                "dynamic_cancellation": True,
            },
        }[entry]
        return {**expected, **common}

    @staticmethod
    def _make_controller(
        base: Path,
        entry: str,
        entered: threading.Event,
        release: threading.Event,
        calls: dict[str, list[object]],
    ) -> tuple[ManagerController, object]:
        paths = make_test_paths(base)
        migration = MigrationResult((), (), 0, 0, 1, 1, 0, 0)
        backup_path = paths.backups / "synthetic-full-backup"
        self_test = IndexResult(0, "synthetic cleanup self-test")
        payloads: dict[str, object] = {
            "stop": None,
            "migration": migration,
            "backup": backup_path,
            "self_test": self_test,
        }
        controller = ManagerController.__new__(ManagerController)
        controller.paths = paths
        controller.config = SimpleNamespace(
            collector_port=18765,
            old_screenshot_dir=os.fspath(base / "old-collector-screenshots"),
        )
        controller.logger = Mock()
        controller.startup_state = StartupState.READY
        controller.read_only_reason = ""
        controller.startup_backup = paths.backups / "startup-backup"
        controller._download_lifecycle_active = False
        controller._last_collector_running = entry == "stop"
        controller._last_engine_running = False
        controller.engine = SimpleNamespace(
            current=None,
            external_running=Mock(return_value=False),
        )

        collector = SimpleNamespace(
            process=object() if entry == "stop" else None,
            running=False,
            health=Mock(return_value=False),
        )

        def controlled_service(
            result: object,
            *,
            enter_critical: bool = False,
            clear_collector: bool = False,
        ) -> Any:
            def run(*args: object, **kwargs: object) -> object:
                calls["service_args"].append((args, kwargs))
                context = kwargs.get("context")
                if enter_critical:
                    assert context is not None
                    context.enter_critical_phase()
                calls["thread_names"].append(QThread.currentThread().objectName())
                entered.set()
                if not release.wait(timeout=3):
                    raise AssertionError("real GUI evidence service was not released")
                if clear_collector:
                    collector.process = None
                return result

            return run

        collector.stop = controlled_service(None, clear_collector=True)
        collector.migrate_old_data = controlled_service(migration)
        controller.collector = collector
        controller.backup = SimpleNamespace(
            create_full_snapshot=controlled_service(backup_path)
        )
        controller.indexer = SimpleNamespace(
            cleanup_self_test=controlled_service(self_test, enter_critical=True)
        )

        if entry == "stop":
            original = controller.require_managed_runtime_control

            def observe_runtime_gate(operation: str, *, managed: bool) -> None:
                calls["gate"].append(("runtime", operation, managed))
                original(operation, managed=managed)

            controller.require_managed_runtime_control = observe_runtime_gate
        elif entry in ("migration", "backup"):
            original = controller.require_safe_write

            def observe_safe_write_gate() -> None:
                calls["gate"].append(("safe_write",))
                original()

            controller.require_safe_write = observe_safe_write_gate
        else:
            original = controller.require_operational_ready

            def observe_ready_gate(operation: str) -> None:
                calls["gate"].append(("ready", operation))
                original(operation)

            controller.require_operational_ready = observe_ready_gate
        return controller, payloads[entry]

    @staticmethod
    def _make_window(controller: ManagerController) -> MainWindow:
        window = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(window)
        window.controller = controller
        window.coordinator = BackgroundTaskCoordinator(window)
        window._background_bindings = {}
        window._background_generations = {}
        window._background_pending = {}
        window.shutdown_timer = None
        window.queue_output = QTextEdit(window)
        window.collector_output = QTextEdit(window)
        window.overview_output = QTextEdit(window)
        window.post_output = QTextEdit(window)
        window.collector_stop_button = QPushButton(window)
        window.collector_migrate_button = QPushButton(window)
        window.manual_backup_button = QPushButton(window)
        window.refresh_index_button = QPushButton(window)
        window.cleanup_index_button = QPushButton(window)
        window.cleanup_test_button = QPushButton(window)
        window._append_info = Mock()
        window._replace_info = Mock()
        window._refresh_status = Mock()
        window.coordinator.task_settled.connect(window._on_background_task_settled)
        window.coordinator.task_progress.connect(window._on_background_task_progress)
        window.coordinator.task_removed.connect(window._on_background_task_removed)
        buttons = {
            "stop": [window.collector_stop_button],
            "migration": [window.collector_migrate_button],
            "backup": [window.manual_backup_button],
            "self_test": [
                window.refresh_index_button,
                window.cleanup_index_button,
                window.cleanup_test_button,
            ],
        }
        window._evidence_buttons = buttons
        window._safe_widgets = []
        window._path_widgets = []
        window._diagnostic_widgets = []
        window._dangerous_widgets = [
            button for entry_buttons in buttons.values() for button in entry_buttons
        ]
        return window

    @staticmethod
    def _invoke(window: MainWindow, entry: str) -> None:
        if entry == "backup":
            with patch(
                "douk_manager.gui.QMessageBox.question",
                return_value=QMessageBox.Yes,
            ):
                window._manual_backup()
            return
        {
            "stop": window._stop_collector,
            "migration": window._migrate_collector,
            "self_test": window._cleanup_index_self_test,
        }[entry]()

    def _exercise_entry(
        self,
        entry: str,
        *,
        closing: bool,
        finished_observer: str,
    ) -> None:
        entered = threading.Event()
        release = threading.Event()
        calls: dict[str, list[object]] = {
            "gate": [],
            "service_args": [],
            "thread_names": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            controller, success_payload = self._make_controller(
                Path(directory), entry, entered, release, calls
            )
            window = self._make_window(controller)
            coordinator = window.coordinator
            settlements: list[tuple[object, ...]] = []
            removals: list[str] = []
            observations: list[tuple[object, ...]] = []
            worker_destroyed: list[bool] = []
            thread_destroyed: list[bool] = []
            finished_seen: list[bool] = []
            record: TaskRecord | None = None
            worker: TaskWorker | None = None
            thread: QThread | None = None
            observer: _ThreadFinishedObserver | None = None
            coordinator_ref = weakref.ref(coordinator)

            task_id_holder: list[str] = []

            def observe_settled(*args: object) -> None:
                settlements.append(args)
                observations.append(("terminal",))

            def observe_removed(task_id: str) -> None:
                removals.append(task_id)
                observations.append(("removed",))

            coordinator.task_settled.connect(observe_settled)
            coordinator.task_removed.connect(observe_removed)
            try:
                if finished_observer == "direct":
                    def observe_preconnected_finished() -> None:
                        task_id = task_id_holder[0]
                        current_coordinator = coordinator_ref()
                        current_record = current_coordinator._records[task_id]
                        observations.append(
                            (
                                "finished-before-coordinator",
                                current_record.terminal_seen,
                                current_record.thread_finished_seen,
                                task_id in coordinator._records,
                            )
                        )
                        finished_seen.append(True)

                    observer = _ThreadFinishedObserver(
                        observe_preconnected_finished, window
                    )

                    def create_observed_thread(parent: QObject) -> QThread:
                        observed_thread = QThread(parent)
                        observed_thread.finished.connect(
                            observer.observe, Qt.ConnectionType.DirectConnection
                        )
                        return observed_thread

                    with patch(
                        "douk_manager.background.QThread",
                        side_effect=create_observed_thread,
                    ):
                        self._invoke(window, entry)
                else:
                    self._invoke(window, entry)
                self.assertEqual(len(coordinator._records), 1)
                record = next(iter(coordinator._records.values()))
                worker = record.worker
                thread = record.thread
                self.assertIsNotNone(worker)
                self.assertIsNotNone(thread)
                assert worker is not None
                assert thread is not None
                task_id = record.task_id
                task_id_holder.append(task_id)
                expected_spec = self._expected_spec(entry)
                self.assertEqual(
                    set(expected_spec), set(TaskSpec.__dataclass_fields__)
                )
                for field_name, value in expected_spec.items():
                    self.assertEqual(getattr(record.spec, field_name), value)
                self.assertEqual(record.generation, 1)
                self.assertEqual(
                    window._background_generations[record.spec.deduplicate_key], 1
                )
                binding = window._background_bindings[task_id]
                self.assertIs(binding.spec, record.spec)
                self.assertEqual(binding.generation, record.generation)
                buttons = window._evidence_buttons[entry]
                self.assertTrue(all(not button.isEnabled() for button in buttons))
                self.assertTrue(entered.wait(timeout=3))
                self.assertEqual(
                    calls["thread_names"],
                    [f"douk-{record.spec.task_type}-{task_id[:8]}"],
                )
                expected_gate = {
                    "stop": [("runtime", "停止账号采集服务", True)],
                    "migration": [("safe_write",)],
                    "backup": [("safe_write",)],
                    "self_test": [("ready", "执行索引清理自检")],
                }[entry]
                self.assertEqual(calls["gate"], expected_gate)
                self.assertEqual(len(calls["service_args"]), 1)
                if record.spec.dynamic_cancellation:
                    self.assertIsNotNone(record.operation_context)
                    service_kwargs = calls["service_args"][0][1]
                    self.assertIs(service_kwargs["context"], record.operation_context)
                else:
                    self.assertIsNone(record.operation_context)
                    self.assertNotIn("context", calls["service_args"][0][1])

                worker.destroyed.connect(lambda *_args: worker_destroyed.append(True))
                thread.destroyed.connect(lambda *_args: thread_destroyed.append(True))
                record_ref = weakref.ref(record)

                if finished_observer != "direct":
                    def observe_finished() -> None:
                        current_record = record_ref()
                        current_coordinator = coordinator_ref()
                        observations.append(
                            (
                                "finished-queued",
                                current_record.thread_finished_seen,
                                task_id in current_coordinator._records,
                            )
                        )
                        finished_seen.append(True)

                    observer = _ThreadFinishedObserver(observe_finished, window)
                    thread.finished.connect(
                        observer.observe, Qt.ConnectionType.QueuedConnection
                    )

                if closing:
                    self.assertFalse(coordinator.begin_closing())
                    self.assertTrue(controller.begin_closing())
                    window._apply_action_gate()
                release.set()
                self._run_until(
                    lambda: len(removals) == 1
                    and worker_destroyed == [True]
                    and thread_destroyed == [True]
                    and finished_seen == [True]
                    and not coordinator.has_active_tasks()
                    and not coordinator.findChildren(QThread)
                )
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                _QT_APPLICATION.processEvents()

                expected_outcome = (
                    TaskState.CANCELLED
                    if closing and entry == "backup"
                    else TaskState.SUCCEEDED
                )
                expected_payload = (
                    "background operation was cancelled"
                    if expected_outcome is TaskState.CANCELLED
                    else success_payload
                )
                self.assertEqual(
                    settlements,
                    [(task_id, 1, expected_outcome, expected_payload)],
                )
                self.assertEqual(removals, [task_id])
                self.assertEqual(worker_destroyed, [True])
                self.assertEqual(thread_destroyed, [True])
                if finished_observer == "direct":
                    self.assertEqual(
                        observations,
                        [
                            ("terminal",),
                            ("finished-before-coordinator", True, False, True),
                            ("removed",),
                        ],
                    )
                else:
                    self.assertEqual(
                        observations,
                        [
                            ("terminal",),
                            ("removed",),
                            ("finished-queued", True, False),
                        ],
                    )
                self.assertEqual(coordinator._records, {})
                self.assertEqual(window._background_bindings, {})
                self.assertEqual(window._background_pending, {})
                self.assertFalse(coordinator.findChildren(QThread))
                self.assertTrue(
                    all(button.isEnabled() is not closing for button in buttons)
                )
                if entry in ("migration", "backup"):
                    self.assertTrue(controller.paths.lock_file.is_file())

                if closing:
                    window._append_info.assert_not_called()
                    window._replace_info.assert_not_called()
                    window._refresh_status.assert_not_called()
                    generations = dict(window._background_generations)
                    self._invoke(window, entry)
                    self.assertEqual(coordinator._records, {})
                    self.assertEqual(window._background_bindings, {})
                    self.assertEqual(window._background_pending, {})
                    self.assertEqual(window._background_generations, generations)
                    self.assertEqual(len(calls["gate"]), 1)
                    self.assertEqual(len(calls["service_args"]), 1)
                    window._refresh_status.assert_not_called()
                    window._append_info.assert_called_once()
                    self.assertTrue(
                        window._append_info.call_args.args[-1].startswith("【未启动】")
                    )
                    window._replace_info.assert_not_called()
                else:
                    if entry in ("stop", "backup"):
                        window._append_info.assert_called_once()
                        window._replace_info.assert_not_called()
                    else:
                        window._replace_info.assert_called_once()
                        window._append_info.assert_not_called()
                    if record.spec.refresh_targets:
                        window._refresh_status.assert_called_once_with()
                    else:
                        window._refresh_status.assert_not_called()
            finally:
                release.set()
                if coordinator.has_active_tasks() or coordinator.findChildren(QThread):
                    self._run_until(
                        lambda: not coordinator.has_active_tasks()
                        and not coordinator.findChildren(QThread)
                    )
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                _QT_APPLICATION.processEvents()
                if record is not None:
                    record.worker = None
                    record.thread = None
                worker = None
                thread = None
                observer = None
                dispose_test_coordinator(_QT_APPLICATION, coordinator)
                window.coordinator = None
                window_destroyed: list[bool] = []
                window.destroyed.connect(lambda *_args: window_destroyed.append(True))
                window.deleteLater()
                QCoreApplication.sendPostedEvents(window, QEvent.Type.DeferredDelete)
                _QT_APPLICATION.processEvents()
                self.assertEqual(window_destroyed, [True])

    def test_four_real_entries_use_exact_specs_controller_gates_and_qthreads(self) -> None:
        for index, entry in enumerate(self._ENTRIES):
            with self.subTest(entry=entry):
                self._exercise_entry(
                    entry,
                    closing=False,
                    finished_observer="direct" if index % 2 == 0 else "queued",
                )

    def test_four_real_entries_drop_late_ui_and_reject_new_work_while_closing(self) -> None:
        for index, entry in enumerate(self._ENTRIES):
            with self.subTest(entry=entry):
                self._exercise_entry(
                    entry,
                    closing=True,
                    finished_observer="queued" if index % 2 == 0 else "direct",
                )


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


class Phase2QtProbeExecutionContractTests(unittest.TestCase):
    def test_one_round_uses_one_real_window_and_real_qthreads_for_all_entries(
        self,
    ) -> None:
        from tests import phase2_qt_lifecycle_probe as probe

        evidence = probe.run_round(1)

        self.assertEqual(evidence["status"], "passed")
        self.assertEqual(evidence["window_type"], "douk_manager.gui.MainWindow")
        self.assertEqual(
            evidence["coordinator_type"],
            "douk_manager.background.BackgroundTaskCoordinator",
        )
        self.assertEqual(evidence["window_instances"], 1)
        self.assertEqual(
            evidence["required_entry_calls"],
            [
                "collector_stop",
                "collector_migration",
                "manual_backup",
                "index_self_test",
                "task_scan",
                "engine_preview",
            ],
        )
        self.assertEqual(
            evidence["summary_post_sequence"],
            [
                "summary_started",
                "summary_settled",
                "summary_removed",
                "post_started_after_summary_removal",
                "post_settled",
                "post_removed",
            ],
        )
        required_task_types = {
            "download_summary",
            "download_post_actions",
            "collector_stop",
            "collector_migration",
            "full_volume_backup",
            "index_cleanup_self_test",
            "task_list_snapshot",
            "engine_update_preview",
        }
        tasks = evidence["tasks"]
        self.assertTrue(required_task_types.issubset({task["task_type"] for task in tasks}))
        self.assertEqual(len({task["task_id"] for task in tasks}), len(tasks))
        for task in tasks:
            self.assertTrue(task["thread_started"])
            self.assertTrue(task["thread_running_after_start"])
            self.assertEqual(task["worker_type"], "douk_manager.background.TaskWorker")
            self.assertEqual(task["thread_type"], "PySide6.QtCore.QThread")
            self.assertEqual(task["action_thread_name"], task["thread_name"])
            self.assertEqual(task["settled_count"], 1)
            self.assertEqual(task["removed_count"], 1)
            self.assertEqual(task["thread_finished_count"], 1)
            self.assertEqual(task["thread_destroyed_count"], 1)
            self.assertEqual(task["worker_destroyed_count"], 1)
        self.assertEqual(evidence["active_records_after"], 0)
        self.assertEqual(evidence["bindings_after"], 0)
        self.assertEqual(evidence["pending_after"], 0)
        self.assertEqual(evidence["qthreads_after"], 0)
        self.assertEqual(set(evidence["validated_real_specs"]), required_task_types)
        self.assertEqual(evidence["generic_protocol"]["mode"], "cancel_before_critical")
        self.assertTrue(evidence["generic_protocol"]["cancellation_accepted"])
        self.assertEqual(evidence["generic_protocol"]["actual_outcome"], "CANCELLED")
        self.assertEqual(
            evidence["generic_protocol"]["finished_observation"],
            [
                {
                    "observer": "direct_before_coordinator",
                    "record_present": True,
                    "terminal_seen": True,
                    "thread_finished_seen": False,
                }
            ],
        )
        self.assertEqual(evidence["closing_protocol"]["mode"], "cancel_before_critical")
        self.assertEqual(evidence["closing_protocol"]["actual_outcome"], "CANCELLED")
        self.assertTrue(evidence["closing_protocol"]["ui_unchanged"])
        self.assertTrue(evidence["closing_protocol"]["late_refresh_suppressed"])
        self.assertTrue(evidence["closing_protocol"]["closing_entry_rejected"])
        self.assertEqual(evidence["round_cleanup"]["active_records_after_cleanup"], 0)
        self.assertEqual(evidence["round_cleanup"]["qthreads_after_cleanup"], 0)
        self.assertEqual(evidence["round_cleanup"]["errors"], [])
        self.assertEqual(evidence["window_cleanup"]["window_destroyed_count"], 1)

    def test_adjacent_round_proves_critical_wins_and_closing_suppresses_ui(self) -> None:
        from tests import phase2_qt_lifecycle_probe as probe

        evidence = probe.run_round(2)

        self.assertEqual(evidence["status"], "passed")
        self.assertEqual(evidence["window_instances"], 1)
        self.assertEqual(evidence["generic_protocol"]["mode"], "critical_before_cancel")
        self.assertFalse(evidence["generic_protocol"]["cancellation_accepted"])
        self.assertEqual(evidence["generic_protocol"]["actual_outcome"], "SUCCEEDED")
        self.assertEqual(evidence["closing_protocol"]["mode"], "critical_before_cancel")
        self.assertEqual(
            evidence["closing_protocol"]["arbitration"],
            {"cancel_requested": False, "critical_to_completion": True},
        )
        self.assertEqual(evidence["closing_protocol"]["actual_outcome"], "SUCCEEDED")
        self.assertTrue(evidence["closing_protocol"]["ui_unchanged"])
        self.assertTrue(evidence["closing_protocol"]["late_refresh_suppressed"])
        self.assertTrue(evidence["closing_protocol"]["closing_entry_rejected"])
        self.assertEqual(evidence["round_cleanup"]["errors"], [])
        self.assertEqual(evidence["window_cleanup"]["window_destroyed_count"], 1)


if __name__ == "__main__":
    unittest.main()
