from __future__ import annotations

import inspect
import os
import threading
import unittest
from dataclasses import FrozenInstanceError, is_dataclass
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from douk_manager.background import (
    BackgroundTaskCoordinator,
    CancellationToken,
    ClosePolicy,
    TaskCancelled,
    TaskRecord,
    TaskSpec,
    TaskState,
    TaskWorker,
)

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
        now[0] += 0.10
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
