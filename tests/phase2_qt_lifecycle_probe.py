from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QThread, QTimer, qInstallMessageHandler
from PySide6.QtWidgets import QApplication

from douk_manager.background import BackgroundTaskCoordinator, ClosePolicy, TaskSpec, TaskState


ROUNDS = 20
FORBIDDEN_DIAGNOSTICS = (
    "QThread: Destroyed while thread is still running",
    "Internal C++ object already deleted",
)


def run_until(condition, *, timeout_ms: int = 3000) -> None:
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

    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(lambda: (timed_out.append(True), loop.quit()))
    poll.timeout.connect(check_condition)
    poll.start()
    timeout.start(timeout_ms)
    loop.exec()
    poll.stop()
    timeout.stop()
    if timed_out or not condition():
        raise RuntimeError("bounded Phase 2 Qt lifecycle probe timed out")


def run_round(round_number: int) -> None:
    coordinator = BackgroundTaskCoordinator()
    entered = threading.Event()
    proceed = threading.Event()
    critical_entered = threading.Event()
    settlements: list[tuple[object, ...]] = []
    removals: list[str] = []
    worker_destructions: list[bool] = []
    thread_finished_events: list[bool] = []
    internal_errors: list[str] = []

    cancel_before_critical = round_number % 2 == 1

    def action(context) -> dict[str, int | str]:
        entered.set()
        if not proceed.wait(2.0):
            raise RuntimeError("probe proceed event timed out")
        context.enter_critical_phase()
        critical_entered.set()
        return {"round": round_number, "winner": "critical"}

    spec = TaskSpec(
        task_type="phase2_probe",
        display_name=f"Phase 2 Probe {round_number}",
        resource_keys=frozenset({"phase2_probe"}),
        deduplicate_key="phase2_probe",
        cancellable=True,
        close_policy=ClosePolicy.CANCEL,
        dynamic_cancellation=True,
    )
    coordinator.task_settled.connect(lambda *args: settlements.append(args))
    coordinator.task_removed.connect(removals.append)
    coordinator.internal_error.connect(internal_errors.append)
    task_id = coordinator.start(spec, round_number, action)
    record = coordinator._records[task_id]
    assert record.worker is not None
    assert record.thread is not None
    record.worker.destroyed.connect(lambda *_: worker_destructions.append(True))
    record.thread.finished.connect(lambda: thread_finished_events.append(True))

    run_until(entered.is_set)
    if cancel_before_critical:
        if not coordinator.request_cancel(task_id):
            raise RuntimeError(f"round {round_number}: pre-critical cancel was rejected")
        proceed.set()
        expected_outcome = TaskState.CANCELLED
        expected_payload: object = "background operation was cancelled"
    else:
        proceed.set()
        run_until(critical_entered.is_set)
        if coordinator.request_cancel(task_id):
            raise RuntimeError(f"round {round_number}: late critical cancel was accepted")
        expected_outcome = TaskState.SUCCEEDED
        expected_payload = {"round": round_number, "winner": "critical"}

    run_until(
        lambda: len(removals) == 1
        and len(worker_destructions) == 1
        and len(thread_finished_events) == 1
    )
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    expected = (task_id, round_number, expected_outcome, expected_payload)
    if settlements != [expected]:
        raise RuntimeError(f"round {round_number}: unexpected settlements {settlements!r}")
    if removals != [task_id]:
        raise RuntimeError(f"round {round_number}: unexpected removals {removals!r}")
    if worker_destructions != [True] or thread_finished_events != [True]:
        raise RuntimeError(f"round {round_number}: incomplete QObject/QThread teardown")
    if internal_errors:
        raise RuntimeError(f"round {round_number}: internal errors {internal_errors!r}")
    if coordinator.has_active_tasks() or coordinator.findChildren(QThread):
        raise RuntimeError(f"round {round_number}: retained task or QThread")


def main() -> int:
    QApplication.instance() or QApplication([])
    diagnostics: list[str] = []
    previous_handler = qInstallMessageHandler(
        lambda _mode, _context, message: diagnostics.append(message)
    )
    try:
        for round_number in range(1, ROUNDS + 1):
            run_round(round_number)
    except Exception as exc:
        print(f"phase2 Qt lifecycle probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        qInstallMessageHandler(previous_handler)

    forbidden = [
        message
        for message in diagnostics
        if any(fragment.casefold() in message.casefold() for fragment in FORBIDDEN_DIAGNOSTICS)
    ]
    if forbidden:
        print(f"phase2 Qt lifecycle probe forbidden diagnostics: {forbidden!r}", file=sys.stderr)
        return 1
    print(f"phase2 Qt lifecycle probe: {ROUNDS}/{ROUNDS} rounds passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
