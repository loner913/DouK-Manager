from __future__ import annotations

import os
import sqlite3
import sys
import threading
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QObject, QThread, QTimer
from PySide6.QtWidgets import QApplication

from douk_manager.background import (
    BackgroundTaskCoordinator,
    CancellationToken,
    ClosePolicy,
    TaskFailure,
    TaskRecord,
    TaskRejectedError,
    TaskSpec,
    TaskState,
    TaskWorker,
)


def make_spec(
    *,
    deduplicate_key: str = "startup_safety",
    resource_keys: frozenset[str] = frozenset({"startup_safety"}),
    cancellable: bool = True,
    close_policy: ClosePolicy = ClosePolicy.CANCEL,
) -> TaskSpec:
    return TaskSpec(
        task_type="startup_safety",
        display_name="Startup safety",
        resource_keys=resource_keys,
        deduplicate_key=deduplicate_key,
        cancellable=cancellable,
        close_policy=close_policy,
        refresh_targets=("runtime_status",),
    )


def make_record(
    task_id: str = "task-1",
    *,
    spec: TaskSpec | None = None,
    generation: int = 7,
    state: TaskState = TaskState.RUNNING,
) -> TaskRecord:
    return TaskRecord(
        task_id=task_id,
        spec=spec or make_spec(),
        generation=generation,
        token=CancellationToken(),
        thread=None,
        worker=None,
        state=state,
    )


class CoordinatorStateTests(unittest.TestCase):
    def test_record_accepts_only_first_terminal_event_and_preserves_generation(self) -> None:
        record = make_record(generation=23)
        original_payload = {"status": "ready"}

        self.assertTrue(record.accept_terminal(TaskState.SUCCEEDED, original_payload))
        self.assertFalse(
            record.accept_terminal(
                TaskState.FAILED,
                TaskFailure("LateFailure", "late", "late traceback"),
            )
        )

        self.assertEqual(record.generation, 23)
        self.assertEqual(record.state, TaskState.SUCCEEDED)
        self.assertIs(record.payload, original_payload)
        self.assertTrue(record.terminal_seen)
        self.assertFalse(record.ready_for_removal)
        self.assertTrue(record.observe_thread_finished())
        self.assertFalse(record.observe_thread_finished())
        self.assertTrue(record.ready_for_removal)

    def test_nonterminal_settlement_becomes_protocol_failure(self) -> None:
        coordinator = BackgroundTaskCoordinator()
        record = make_record()
        coordinator._records[record.task_id] = record
        internal_errors: list[str] = []
        settlements: list[tuple[object, ...]] = []
        coordinator.internal_error.connect(internal_errors.append)
        coordinator.task_settled.connect(lambda *args: settlements.append(args))

        coordinator._on_worker_settled(
            record.task_id,
            record.generation,
            TaskState.RUNNING,
            {"invalid": True},
        )

        self.assertEqual(record.state, TaskState.FAILED)
        self.assertTrue(record.terminal_seen)
        self.assertEqual(len(internal_errors), 1)
        self.assertEqual(len(settlements), 1)
        self.assertEqual(settlements[0][0:3], (record.task_id, 7, TaskState.FAILED))
        self.assertIsInstance(settlements[0][3], TaskFailure)

    def test_finished_without_settled_is_internal_only_and_removes_once(self) -> None:
        coordinator = BackgroundTaskCoordinator()
        record = make_record()
        thread_marker = object()
        record.thread = thread_marker  # type: ignore[assignment]
        coordinator._records[record.task_id] = record
        internal_errors: list[str] = []
        settlements: list[tuple[object, ...]] = []
        removals: list[str] = []
        idle_events: list[bool] = []
        coordinator.internal_error.connect(internal_errors.append)
        coordinator.task_settled.connect(lambda *args: settlements.append(args))
        coordinator.task_removed.connect(removals.append)
        coordinator.idle.connect(lambda: idle_events.append(True))
        coordinator._idle_emitted = False

        coordinator._observe_thread_finished(record.task_id, thread_marker)
        coordinator._observe_thread_finished(record.task_id, thread_marker)

        self.assertEqual(record.state, TaskState.FAILED)
        self.assertTrue(record.ready_for_removal)
        self.assertEqual(len(internal_errors), 1)
        self.assertEqual(settlements, [])
        self.assertEqual(removals, [record.task_id])
        self.assertEqual(idle_events, [True])
        self.assertFalse(coordinator.has_active_tasks())

    def test_worker_rejects_unsafe_payloads_as_ordinary_failure(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            try:
                raise RuntimeError("traceback payload")
            except RuntimeError:
                live_traceback = sys.exc_info()[2]
            unsafe_payloads = (QObject(), connection, live_traceback)

            for index, unsafe_payload in enumerate(unsafe_payloads):
                with self.subTest(payload_type=type(unsafe_payload).__name__):
                    settlements: list[tuple[object, ...]] = []
                    worker = TaskWorker(
                        f"unsafe-{index}",
                        9,
                        lambda _token, value=unsafe_payload: value,
                        CancellationToken(),
                    )
                    worker.settled.connect(lambda *args: settlements.append(args))

                    worker.run()

                    self.assertEqual(len(settlements), 1)
                    self.assertEqual(settlements[0][2], TaskState.FAILED)
                    self.assertIsInstance(settlements[0][3], TaskFailure)
        finally:
            connection.close()

    def test_task_failure_bounds_traceback_and_requires_strings(self) -> None:
        failure = TaskFailure("ExampleError", "failure", "x" * 5000)

        self.assertEqual(len(failure.traceback_text), 4000)
        with self.assertRaises(TypeError):
            TaskFailure("ExampleError", "failure", QObject())  # type: ignore[arg-type]

    def test_resource_conflict_and_deduplicate_key_are_rejected_separately(self) -> None:
        coordinator = BackgroundTaskCoordinator()
        coordinator._records["active"] = make_record()

        with self.assertRaisesRegex(TaskRejectedError, "duplicate"):
            coordinator._validate_start(make_spec())

        with self.assertRaisesRegex(TaskRejectedError, "resource"):
            coordinator._validate_start(
                make_spec(
                    deduplicate_key="different-task",
                    resource_keys=frozenset({"startup_safety", "settings"}),
                )
            )

    def test_terminal_record_waiting_for_thread_finish_does_not_block_next_generation(
        self,
    ) -> None:
        coordinator = BackgroundTaskCoordinator()
        record = make_record()
        self.assertTrue(record.accept_terminal(TaskState.SUCCEEDED, {"ready": True}))
        self.assertFalse(record.thread_finished_seen)
        coordinator._records[record.task_id] = record

        rejection: Exception | None = None
        try:
            coordinator._validate_start(make_spec())
        except Exception as exc:  # pragma: no cover - converted into an assertion below
            rejection = exc

        self.assertIsNone(rejection)
        self.assertIn(record.task_id, coordinator._records)

    def test_finished_observer_does_not_call_qthread_methods(self) -> None:
        property_calls: list[str] = []

        class GuardedThread(QThread):
            def property(self, name: str) -> object:  # noqa: A003
                property_calls.append(name)
                raise AssertionError("QThread method called after finished")

        thread = GuardedThread()

        class SenderCoordinator(BackgroundTaskCoordinator):
            def sender(self) -> QObject:
                return thread

        coordinator = SenderCoordinator()
        record = make_record()
        record.thread = thread
        self.assertTrue(record.accept_terminal(TaskState.SUCCEEDED, {"ready": True}))
        coordinator._records[record.task_id] = record

        observer_error: Exception | None = None
        try:
            coordinator._on_thread_finished()
        except Exception as exc:  # pragma: no cover - converted into an assertion below
            observer_error = exc

        self.assertIsNone(observer_error)
        self.assertEqual(property_calls, [])
        self.assertFalse(coordinator.has_active_tasks())
        thread.deleteLater()

    def test_cancellation_state_is_cooperative_and_idempotent(self) -> None:
        coordinator = BackgroundTaskCoordinator()
        record = make_record()
        coordinator._records[record.task_id] = record

        self.assertTrue(coordinator.request_cancel(record.task_id))
        self.assertEqual(record.state, TaskState.CANCELLING)
        self.assertTrue(record.token.is_cancelled())
        self.assertTrue(coordinator.request_cancel(record.task_id))
        self.assertEqual(record.state, TaskState.CANCELLING)

    def test_non_cancellable_or_critical_write_task_rejects_cancellation(self) -> None:
        coordinator = BackgroundTaskCoordinator()
        non_cancellable = make_record(spec=make_spec(cancellable=False))
        critical = make_record(
            "task-2",
            spec=TaskSpec(
                task_type="critical",
                display_name="Critical write",
                resource_keys=frozenset({"settings"}),
                deduplicate_key="critical",
                cancellable=True,
                close_policy=ClosePolicy.WAIT,
                refresh_targets=(),
                critical_write_started=True,
            ),
        )
        coordinator._records = {
            non_cancellable.task_id: non_cancellable,
            critical.task_id: critical,
        }

        self.assertFalse(coordinator.request_cancel(non_cancellable.task_id))
        self.assertFalse(coordinator.request_cancel(critical.task_id))
        self.assertFalse(non_cancellable.token.is_cancelled())
        self.assertFalse(critical.token.is_cancelled())

    def test_accepted_closing_is_sticky_and_cancels_cancel_policy_tasks(self) -> None:
        coordinator = BackgroundTaskCoordinator()
        record = make_record()
        coordinator._records[record.task_id] = record

        self.assertFalse(coordinator.begin_closing())
        self.assertTrue(coordinator.is_closing)
        self.assertTrue(record.token.is_cancelled())
        self.assertFalse(coordinator.begin_closing())

        with self.assertRaisesRegex(TaskRejectedError, "closing"):
            coordinator._validate_start(make_spec(deduplicate_key="new"))

    def test_reject_close_policy_does_not_enter_sticky_closing(self) -> None:
        coordinator = BackgroundTaskCoordinator()
        record = make_record(spec=make_spec(close_policy=ClosePolicy.REJECT))
        coordinator._records[record.task_id] = record

        self.assertFalse(coordinator.begin_closing())
        self.assertFalse(coordinator.is_closing)
        self.assertFalse(record.token.is_cancelled())


class CoordinatorQtLifecycleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def _run_until(self, condition, *, timeout_ms: int = 3000) -> None:
        if condition():
            return
        loop = QEventLoop()
        timed_out: list[bool] = []
        poll = QTimer()
        poll.setInterval(1)

        def check_condition() -> None:
            if condition():
                poll.stop()
                loop.quit()

        def on_timeout() -> None:
            timed_out.append(True)
            loop.quit()

        timeout = QTimer()
        timeout.setSingleShot(True)
        timeout.timeout.connect(on_timeout)
        poll.timeout.connect(check_condition)
        poll.start()
        timeout.start(timeout_ms)
        loop.exec()
        poll.stop()
        timeout.stop()
        self.assertFalse(timed_out, "bounded Qt event loop timed out")
        self.assertTrue(condition())

    def test_real_qthread_settles_removes_and_destroys_worker_once(self) -> None:
        coordinator = BackgroundTaskCoordinator()
        entered = threading.Event()
        release = threading.Event()
        settlements: list[tuple[object, ...]] = []
        removals: list[str] = []
        worker_destructions: list[bool] = []
        thread_finished_events: list[bool] = []

        def action(token: CancellationToken) -> dict[str, bool]:
            entered.set()
            if not release.wait(2.0):
                raise RuntimeError("test release event timed out")
            token.raise_if_cancelled()
            return {"ready": True}

        coordinator.task_settled.connect(lambda *args: settlements.append(args))
        coordinator.task_removed.connect(removals.append)
        task_id = coordinator.start(make_spec(), 17, action)
        record = coordinator._records[task_id]
        assert record.worker is not None
        assert record.thread is not None
        record.worker.destroyed.connect(lambda *_: worker_destructions.append(True))
        record.thread.finished.connect(lambda: thread_finished_events.append(True))

        release_timer = QTimer()
        release_timer.setInterval(1)

        def release_when_entered() -> None:
            if entered.is_set():
                release_timer.stop()
                release.set()

        release_timer.timeout.connect(release_when_entered)
        release_timer.start()
        self._run_until(
            lambda: len(removals) == 1
            and len(worker_destructions) == 1
            and len(thread_finished_events) == 1
        )
        release_timer.stop()

        self.assertEqual(len(settlements), 1)
        self.assertEqual(settlements[0], (task_id, 17, TaskState.SUCCEEDED, {"ready": True}))
        self.assertEqual(removals, [task_id])
        self.assertEqual(worker_destructions, [True])
        self.assertEqual(thread_finished_events, [True])
        self.assertFalse(coordinator.has_active_tasks())

    def test_completion_close_race_emits_idle_callback_once_without_gui_wait(self) -> None:
        coordinator = BackgroundTaskCoordinator()
        entered = threading.Event()
        release = threading.Event()
        settlements: list[tuple[object, ...]] = []
        close_callbacks: list[bool] = []
        worker_destructions: list[bool] = []

        def action(_token: CancellationToken) -> str:
            entered.set()
            if not release.wait(2.0):
                raise RuntimeError("test release event timed out")
            return "finished during close"

        coordinator.task_settled.connect(lambda *args: settlements.append(args))
        coordinator.idle.connect(lambda: close_callbacks.append(True))
        task_id = coordinator.start(make_spec(), 31, action)
        record = coordinator._records[task_id]
        assert record.worker is not None
        record.worker.destroyed.connect(lambda *_: worker_destructions.append(True))

        close_timer = QTimer()
        close_timer.setInterval(1)

        def close_when_entered() -> None:
            if entered.is_set():
                close_timer.stop()
                coordinator.begin_closing()
                release.set()

        close_timer.timeout.connect(close_when_entered)
        close_timer.start()
        self._run_until(
            lambda: len(close_callbacks) == 1 and len(worker_destructions) == 1
        )
        close_timer.stop()

        self.assertTrue(coordinator.is_closing)
        self.assertTrue(record.token.is_cancelled())
        self.assertEqual(len(settlements), 1)
        self.assertEqual(settlements[0][0:3], (task_id, 31, TaskState.CANCELLED))
        self.assertEqual(close_callbacks, [True])
        self.assertFalse(coordinator.has_active_tasks())


if __name__ == "__main__":
    unittest.main()
