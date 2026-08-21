from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QRect, QThread, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox

from douk_manager.background import ClosePolicy, TaskSpec
from douk_manager.gui import MainWindow, SmartSkipChoice, SmartSkipPreviewDialog
from douk_manager.startup import StartupState
from douk_manager.ui_state import WindowGeometryState, WindowStateStore


class CountingStore:
    def __init__(self, delegate: WindowStateStore) -> None:
        self.delegate = delegate
        self.save_calls = 0

    def load(self) -> WindowGeometryState | None:
        return self.delegate.load()

    def save(self, state: WindowGeometryState) -> bool:
        self.save_calls += 1
        return self.delegate.save(state)


def drain_until(app: QApplication, condition, *, timeout_ms: int = 5000) -> None:
    elapsed = 0
    while not condition() and elapsed < timeout_ms:
        app.processEvents()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
        timer = QTimer()
        timer.setSingleShot(True)
        timer.start(1)
        while timer.isActive():
            app.processEvents()
        elapsed += 1
    if not condition():
        raise AssertionError(f"bounded Qt drain timed out after {timeout_ms} ms")


def close_logger(window: MainWindow) -> None:
    for handler in list(window.controller.logger.handlers):
        window.controller.logger.removeHandler(handler)
        handler.close()


def run_probe() -> dict[str, object]:
    app = QApplication.instance() or QApplication([])
    evidence: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="douk-stabilization-qt-") as directory:
        root = Path(directory)
        manager_home = root / "manager"
        ui_path = root / "acceptance-ui-state.ini"
        with patch.dict(
            os.environ,
            {
                "DOUK_MANAGER_HOME": os.fspath(manager_home),
                "DOUK_MANAGER_UI_STATE_PATH": os.fspath(ui_path),
            },
        ):
            dialog_text = "\n".join(
                (
                    "输入账号：A1-A1370（共 1370 个，有效URL 1370 个）",
                    "近期明确非私密（纳入）："
                    + "、".join(f"A{number}" for number in range(1, 1371)),
                    "创建时可选择：按预览跳过、强制包含全部或取消创建。",
                )
            )
            dialog = SmartSkipPreviewDialog(dialog_text, can_skip=True)
            QTimer.singleShot(0, dialog.reject)
            dialog.exec()
            evidence["dialog_choice"] = dialog.choice
            evidence["dialog_complete"] = dialog.details.toPlainText() == dialog_text
            evidence["dialog_scrollable"] = (
                dialog.details.verticalScrollBar().maximum() > 0
            )
            evidence["dialog_widget_count"] = len(dialog.findChildren(object))
            dialog.deleteLater()
            app.processEvents()
            if evidence["dialog_choice"] != SmartSkipChoice.CANCEL:
                raise AssertionError("Escape/reject did not map to cancel")
            if not evidence["dialog_complete"] or not evidence["dialog_scrollable"]:
                raise AssertionError("large preview was incomplete or not scrollable")

            base_store = WindowStateStore.default()
            initial = WindowGeometryState(QRect(16, 16, 768, 700), False)
            if not base_store.save(initial):
                raise AssertionError("failed to seed isolated UI state")
            store = CountingStore(base_store)
            window = MainWindow(window_state_store=store)
            window.poll_timer.stop()
            window.controller.startup_state = StartupState.READY
            window.setGeometry(16, 60, 768, 700)
            window.show()
            app.processEvents()

            entered = threading.Event()
            release = threading.Event()

            def blocking_action(token) -> str:
                entered.set()
                if not release.wait(5.0):
                    raise RuntimeError("blocking action release timed out")
                token.raise_if_cancelled()
                return "unexpected success"

            spec = TaskSpec(
                task_type="stabilization_close_probe",
                display_name="稳定化关闭探针",
                resource_keys=frozenset({"stabilization_probe"}),
                deduplicate_key="stabilization_close_probe",
                cancellable=True,
                close_policy=ClosePolicy.CANCEL,
            )
            task_id = window._submit_background(spec, blocking_action)
            if task_id is None:
                raise AssertionError("failed to submit blocking lifecycle task")
            drain_until(app, entered.is_set)
            record = window.coordinator._records[task_id]
            thread = record.thread
            worker = record.worker
            if thread is None or worker is None:
                raise AssertionError("real coordinator did not construct QThread/Worker")
            settled: list[str] = []
            removed: list[str] = []
            finished: list[bool] = []
            thread_destroyed: list[bool] = []
            worker_destroyed: list[bool] = []
            window.coordinator.task_settled.connect(
                lambda current, *_args: settled.append(current)
            )
            window.coordinator.task_removed.connect(removed.append)
            thread.finished.connect(lambda: finished.append(True))
            thread.destroyed.connect(lambda *_: thread_destroyed.append(True))
            worker.destroyed.connect(lambda *_: worker_destroyed.append(True))

            with patch.object(QMessageBox, "information"):
                window.close()
            app.processEvents()
            evidence["rejected_close_saved"] = store.save_calls
            evidence["rejected_close_visible"] = window.isVisible()
            evidence["rejected_close_state_unchanged"] = base_store.load() == initial
            release.set()
            drain_until(
                app,
                lambda: not window.isVisible()
                and not window.coordinator.has_active_tasks()
                and bool(finished),
            )
            drain_until(
                app,
                lambda: bool(thread_destroyed) and bool(worker_destroyed),
            )
            evidence["accepted_close_saved"] = store.save_calls
            evidence["settled"] = settled
            evidence["removed"] = removed
            evidence["thread_finished"] = len(finished)
            evidence["thread_destroyed"] = len(thread_destroyed)
            evidence["worker_destroyed"] = len(worker_destroyed)
            evidence["active_after_accept"] = window.coordinator.has_active_tasks()
            evidence["qthreads_after_accept"] = len(
                window.coordinator.findChildren(QThread)
            )
            if not (
                evidence["rejected_close_saved"] == 0
                and evidence["rejected_close_visible"]
                and evidence["rejected_close_state_unchanged"]
                and evidence["accepted_close_saved"] == 1
                and settled == [task_id]
                and removed == [task_id]
                and finished == [True]
                and thread_destroyed == [True]
                and worker_destroyed == [True]
                and not evidence["active_after_accept"]
                and evidence["qthreads_after_accept"] == 0
            ):
                raise AssertionError(f"close lifecycle contract failed: {evidence}")
            close_logger(window)
            window.deleteLater()
            app.processEvents()

            failure_store = CountingStore(WindowStateStore.default())
            failed = MainWindow(window_state_store=failure_store)
            failed.poll_timer.stop()
            failed.controller.startup_state = StartupState.READY
            failed.controller.collector.process = object()
            failed.show()
            failure_entered = threading.Event()
            failure_release = threading.Event()

            def fail_stop() -> None:
                failure_entered.set()
                if not failure_release.wait(5.0):
                    raise RuntimeError("collector failure release timed out")
                raise RuntimeError("synthetic collector stop failure")

            failed.controller.stop_collector = fail_stop
            with patch.object(QMessageBox, "critical"):
                failed.close()
                drain_until(app, failure_entered.is_set)
                failure_record = next(iter(failed.coordinator._records.values()))
                failure_thread = failure_record.thread
                failure_worker = failure_record.worker
                if failure_thread is None or failure_worker is None:
                    raise AssertionError("collector failure did not use real QThread/Worker")
                failure_finished: list[bool] = []
                failure_thread_destroyed: list[bool] = []
                failure_worker_destroyed: list[bool] = []
                failure_thread.finished.connect(lambda: failure_finished.append(True))
                failure_thread.destroyed.connect(
                    lambda *_: failure_thread_destroyed.append(True)
                )
                failure_worker.destroyed.connect(
                    lambda *_: failure_worker_destroyed.append(True)
                )
                failure_release.set()
                drain_until(
                    app,
                    lambda: not failed.coordinator.has_active_tasks()
                    and bool(failure_finished),
                )
                drain_until(
                    app,
                    lambda: bool(failure_thread_destroyed)
                    and bool(failure_worker_destroyed),
                )
            evidence["failure_close_visible"] = failed.isVisible()
            evidence["failure_close_saved"] = failure_store.save_calls
            evidence["failure_thread_finished"] = len(failure_finished)
            evidence["failure_thread_destroyed"] = len(failure_thread_destroyed)
            evidence["failure_worker_destroyed"] = len(failure_worker_destroyed)
            evidence["failure_qthreads_after"] = len(
                failed.coordinator.findChildren(QThread)
            )
            if not (
                evidence["failure_close_visible"]
                and evidence["failure_close_saved"] == 0
                and failure_finished == [True]
                and failure_thread_destroyed == [True]
                and failure_worker_destroyed == [True]
                and evidence["failure_qthreads_after"] == 0
            ):
                raise AssertionError(f"collector failure close contract failed: {evidence}")
            failed.hide()
            close_logger(failed)
            failed.deleteLater()
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            app.processEvents()

            evidence["ui_state_path"] = os.fspath(ui_path)
            evidence["status"] = "passed"
    return evidence


def main() -> int:
    evidence = run_probe()
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    print("stabilization Qt lifecycle probe: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
