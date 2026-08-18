from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QEventLoop, QTimer, qInstallMessageHandler
from PySide6.QtWidgets import QApplication

from douk_manager.background import (
    BackgroundTaskCoordinator,
    CancellationToken,
    ClosePolicy,
    TaskSpec,
    TaskState,
)


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
    if timed_out or not condition():
        raise RuntimeError("bounded Qt lifecycle probe timed out")


def run_round(round_number: int) -> None:
    coordinator = BackgroundTaskCoordinator()
    entered = threading.Event()
    release = threading.Event()
    settlements: list[tuple[object, ...]] = []
    removals: list[str] = []
    worker_destructions: list[bool] = []
    thread_finished_events: list[bool] = []

    def action(token: CancellationToken) -> dict[str, int]:
        entered.set()
        if not release.wait(2.0):
            raise RuntimeError("probe release event timed out")
        token.raise_if_cancelled()
        return {"round": round_number}

    spec = TaskSpec(
        task_type="probe",
        display_name=f"Probe {round_number}",
        resource_keys=frozenset({"probe"}),
        deduplicate_key="probe",
        cancellable=True,
        close_policy=ClosePolicy.CANCEL,
        refresh_targets=(),
    )
    coordinator.task_settled.connect(lambda *args: settlements.append(args))
    coordinator.task_removed.connect(removals.append)
    task_id = coordinator.start(spec, round_number, action)
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
    run_until(
        lambda: len(removals) == 1
        and len(worker_destructions) == 1
        and len(thread_finished_events) == 1
    )
    release_timer.stop()

    expected = (task_id, round_number, TaskState.SUCCEEDED, {"round": round_number})
    if settlements != [expected]:
        raise RuntimeError(f"round {round_number}: unexpected settlements {settlements!r}")
    if removals != [task_id]:
        raise RuntimeError(f"round {round_number}: unexpected removals {removals!r}")
    if worker_destructions != [True]:
        raise RuntimeError(
            f"round {round_number}: worker destruction count {len(worker_destructions)}"
        )
    if thread_finished_events != [True]:
        raise RuntimeError(
            f"round {round_number}: thread finished count {len(thread_finished_events)}"
        )
    if coordinator.has_active_tasks():
        raise RuntimeError(f"round {round_number}: coordinator retained active task")


def main() -> int:
    QApplication.instance() or QApplication([])
    diagnostics: list[str] = []

    def capture_message(_mode, _context, message: str) -> None:
        diagnostics.append(message)

    previous_handler = qInstallMessageHandler(capture_message)
    try:
        for round_number in range(1, ROUNDS + 1):
            run_round(round_number)
    except Exception as exc:
        print(f"qt lifecycle probe failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        qInstallMessageHandler(previous_handler)

    forbidden = [
        message
        for message in diagnostics
        if any(fragment.casefold() in message.casefold() for fragment in FORBIDDEN_DIAGNOSTICS)
    ]
    if forbidden:
        print(f"qt lifecycle probe forbidden diagnostics: {forbidden!r}", file=sys.stderr)
        return 1
    print(f"qt lifecycle probe: {ROUNDS}/{ROUNDS} rounds passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
