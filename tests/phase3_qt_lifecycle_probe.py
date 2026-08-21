from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
import threading
import traceback
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QTimer, qInstallMessageHandler
from PySide6.QtWidgets import QApplication, QMessageBox

from douk_manager.background import ClosePolicy, TaskSpec, TaskState
from douk_manager.gui import MainWindow
from douk_manager.startup import StartupState
from tests.test_result_dashboard_gui import _write_task_log


FORBIDDEN_DIAGNOSTICS = (
    "QThread: Destroyed while thread is still running",
    "Internal C++ object already deleted",
)


class _CloseEvent:
    def __init__(self) -> None:
        self.accepted = 0
        self.ignored = 0

    def accept(self) -> None:
        self.accepted += 1

    def ignore(self) -> None:
        self.ignored += 1


def _run_until(condition, *, timeout_ms: int = 4000) -> None:
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
    if timed_out or not condition():
        raise RuntimeError("bounded Phase 3 Qt lifecycle probe timed out")


def _active_task_id(window: MainWindow, generation_key: str) -> str:
    for task_id, binding in window._background_bindings.items():
        if binding.generation_key == generation_key:
            return task_id
    raise RuntimeError(f"no active task for generation key {generation_key}")


def _observe_blocked_record(window: MainWindow, task_id: str, evidence: dict) -> None:
    record = window.coordinator._records[task_id]
    if record.thread is None or record.worker is None:
        raise RuntimeError(f"task {task_id} has no live Qt record")
    record.thread.finished.connect(
        lambda task_id=task_id: evidence["thread_finished"].append(task_id)
    )
    record.worker.destroyed.connect(
        lambda *_args, task_id=task_id: evidence["worker_destroyed"].append(task_id)
    )


def _assert_unique_lifecycle(evidence: dict) -> None:
    settled_ids = [item[0] for item in evidence["settled"]]
    removed_ids = evidence["removed"]
    if len(settled_ids) != len(set(settled_ids)):
        raise RuntimeError(f"duplicate settled signals: {settled_ids!r}")
    if len(removed_ids) != len(set(removed_ids)):
        raise RuntimeError(f"duplicate removed signals: {removed_ids!r}")
    if set(settled_ids) != set(removed_ids):
        raise RuntimeError(
            f"settled/removed task mismatch: settled={settled_ids!r}, removed={removed_ids!r}"
        )
    observed = set(evidence["observed_blocked_tasks"])
    if set(evidence["thread_finished"]) != observed:
        raise RuntimeError("not every observed blocked QThread emitted finished exactly once")
    if set(evidence["worker_destroyed"]) != observed:
        raise RuntimeError("not every observed blocked worker was destroyed exactly once")


def run_probe() -> dict:
    evidence: dict = {
        "status": "failed",
        "scenarios": [],
        "settled": [],
        "removed": [],
        "thread_finished": [],
        "worker_destroyed": [],
        "observed_blocked_tasks": [],
        "exception": None,
    }
    app = QApplication.instance() or QApplication([])
    window: MainWindow | None = None
    home_patch = None
    release_events: list[threading.Event] = []
    try:
        temporary = tempfile.TemporaryDirectory()
        evidence["synthetic_root"] = temporary.name
        home_patch = patch.dict(os.environ, {"DOUK_MANAGER_HOME": temporary.name})
        home_patch.start()
        window = MainWindow()
        window.poll_timer.stop()
        window.controller.startup_state = StartupState.READY
        window._apply_action_gate()
        window.show()
        app.processEvents()
        window.coordinator.task_settled.connect(
            lambda task_id, generation, outcome, _payload: evidence["settled"].append(
                (task_id, generation, getattr(outcome, "value", str(outcome)))
            )
        )
        window.coordinator.task_removed.connect(evidence["removed"].append)

        root = Path(temporary.name)
        logs = window.controller.paths.download_task_logs
        native = root / "synthetic-native.log"
        native.write_text("synthetic lifecycle evidence", encoding="utf-8")
        tasks: list[Path] = []
        for name, hour in (("A", 8), ("B", 9), ("C", 10)):
            tasks.append(
                _write_task_log(
                    logs,
                    f"DownloadTask_{name}.log",
                    started=f"2026-08-21 {hour:02d}:00:00",
                    ended=f"2026-08-21 {hour:02d}:01:00",
                    template=f"{name}.json",
                    native_log=native,
                )
            )

        window.refresh_result_dashboard()
        _run_until(
            lambda: window._dashboard_snapshot is not None
            and window._dashboard_snapshot.task_log == tasks[2]
            and not window.coordinator.has_active_tasks()
        )
        evidence["scenarios"].append("default_latest_loaded")

        writer_entered = threading.Event()
        writer_release = threading.Event()
        release_events.append(writer_release)

        def hold_result_logs(context):
            writer_entered.set()
            while not writer_release.wait(0.005):
                context.raise_if_cancelled()
            return "released"

        writer_spec = TaskSpec(
            task_type="phase3_probe_summary_writer",
            display_name="Phase 3 synthetic summary writer",
            resource_keys=frozenset({"result_logs"}),
            deduplicate_key="phase3_probe_summary_writer",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )
        writer_id = window.coordinator.start(writer_spec, 1, hold_result_logs)
        _run_until(writer_entered.is_set)
        evidence["observed_blocked_tasks"].append(writer_id)
        _observe_blocked_record(window, writer_id, evidence)
        generation_before = window._background_generations.get("result_dashboard_index")
        window.refresh_result_dashboard()
        if window._background_generations.get("result_dashboard_index") != generation_before:
            raise RuntimeError("resource rejection replaced dashboard generation")
        writer_release.set()
        _run_until(lambda: writer_id not in window.coordinator._records)
        window.refresh_result_dashboard()
        _run_until(lambda: not window.coordinator.has_active_tasks())
        evidence["scenarios"].append("result_logs_conflict_rejected_then_recovered")

        rapid_entered = threading.Event()
        rapid_release = threading.Event()
        release_events.append(rapid_release)
        rapid_calls: list[str] = []
        original_query = window.controller.result_dashboard_snapshot

        def rapid_query(task_log: Path, **kwargs):
            rapid_calls.append(task_log.name)
            if task_log == tasks[0]:
                rapid_entered.set()
                context = kwargs["context"]
                while not rapid_release.wait(0.005):
                    context.raise_if_cancelled()
            return original_query(task_log, **kwargs)

        window.controller.result_dashboard_snapshot = rapid_query
        window.dashboard_task_selector.setCurrentIndex(
            window.dashboard_task_selector.findData(window._dashboard_key(tasks[0]))
        )
        _run_until(rapid_entered.is_set)
        rapid_id = _active_task_id(window, "result_dashboard_task")
        evidence["observed_blocked_tasks"].append(rapid_id)
        _observe_blocked_record(window, rapid_id, evidence)
        window.dashboard_task_selector.setCurrentIndex(
            window.dashboard_task_selector.findData(window._dashboard_key(tasks[1]))
        )
        window.dashboard_task_selector.setCurrentIndex(
            window.dashboard_task_selector.findData(window._dashboard_key(tasks[2]))
        )
        _run_until(
            lambda: window._dashboard_snapshot.task_log == tasks[2]
            and not window.coordinator.has_active_tasks()
        )
        if rapid_calls != [tasks[0].name, tasks[2].name]:
            raise RuntimeError(f"rapid selection executed unexpected tasks: {rapid_calls!r}")
        evidence["scenarios"].append("rapid_a_b_c_only_c_rendered")

        close_entered = threading.Event()
        close_release = threading.Event()
        release_events.append(close_release)
        stable = window._dashboard_snapshot

        def close_query(task_log: Path, **kwargs):
            close_entered.set()
            context = kwargs["context"]
            while not close_release.wait(0.005):
                context.raise_if_cancelled()
            return original_query(task_log, **kwargs)

        window.controller.result_dashboard_snapshot = close_query
        window._load_dashboard_selection(force_refresh=True)
        _run_until(close_entered.is_set)
        close_task_id = _active_task_id(window, "result_dashboard_task")
        evidence["observed_blocked_tasks"].append(close_task_id)
        _observe_blocked_record(window, close_task_id, evidence)
        close_event = _CloseEvent()
        with patch.object(QMessageBox, "information"):
            window.closeEvent(close_event)
        if close_event.ignored != 1 or close_event.accepted:
            raise RuntimeError("active dashboard close did not enter cooperative cancellation")
        _run_until(lambda: not window.coordinator.has_active_tasks())
        if window.controller.startup_state is not StartupState.CLOSING:
            raise RuntimeError("window did not retain CLOSING state")
        if window._background_pending:
            raise RuntimeError("coalesced dashboard request survived closing")
        if window._render_dashboard_snapshot_if_current(
            stable, stable.task_log, stable.fingerprint
        ):
            raise RuntimeError("late dashboard snapshot rendered after CLOSING")
        evidence["scenarios"].append("close_cancelled_and_late_result_dropped")

        _assert_unique_lifecycle(evidence)
        if window.coordinator._records:
            raise RuntimeError("coordinator retained task records after lifecycle probe")
        evidence["status"] = "passed"
    except Exception as exc:
        evidence["exception"] = {
            "type": type(exc).__name__,
            "message": str(exc),
            "traceback": traceback.format_exc(),
        }
    finally:
        for event in release_events:
            event.set()
        if window is not None:
            window.poll_timer.stop()
            if window.coordinator.has_active_tasks():
                window.coordinator.begin_closing()
                try:
                    _run_until(lambda: not window.coordinator.has_active_tasks())
                except Exception:
                    evidence["status"] = "failed"
            window.hide()
            window.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()
            for handler in list(window.controller.logger.handlers):
                window.controller.logger.removeHandler(handler)
                handler.close()
        if home_patch is not None:
            home_patch.stop()
        if "temporary" in locals():
            temporary.cleanup()
    return evidence


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 3 real Qt lifecycle probe")
    parser.add_argument("--evidence-jsonl", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    diagnostics: list[str] = []
    previous_handler = qInstallMessageHandler(
        lambda _mode, _context, message: diagnostics.append(message)
    )
    try:
        evidence = run_probe()
    finally:
        qInstallMessageHandler(previous_handler)
    evidence["qt_diagnostics"] = diagnostics
    forbidden = [
        message
        for message in diagnostics
        if any(
            fragment.casefold() in message.casefold()
            for fragment in FORBIDDEN_DIAGNOSTICS
        )
    ]
    evidence["forbidden_qt_diagnostics"] = forbidden
    if forbidden:
        evidence["status"] = "failed"
        if evidence["exception"] is None:
            evidence["exception"] = {
                "type": "ForbiddenQtDiagnostic",
                "message": repr(forbidden),
                "traceback": "",
            }
    line = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
    print(line, flush=True)
    if args.evidence_jsonl is not None:
        args.evidence_jsonl.parent.mkdir(parents=True, exist_ok=True)
        with args.evidence_jsonl.open("x", encoding="utf-8") as handle:
            handle.write(line + "\n")
    if evidence["status"] != "passed":
        print("Phase 3 Qt lifecycle probe failed", file=sys.stderr)
        return 1
    print("Phase 3 Qt lifecycle probe: deterministic round passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
