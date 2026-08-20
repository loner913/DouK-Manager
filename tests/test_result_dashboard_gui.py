from __future__ import annotations

import os
import inspect
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox

from douk_manager.background import TaskSpec
from douk_manager.core.download_summary import (
    AccountOutcome,
    AccountStatus,
    DownloadSummary,
    LocatedNativeLogs,
    NativeLogSegment,
    format_summary_for_task_log,
)
from douk_manager.gui import BackgroundTaskBinding, MainWindow
from douk_manager.startup import StartupState


def _write_task_log(
    directory: Path,
    name: str,
    *,
    started: str,
    ended: str,
    template: str,
    native_log: Path,
) -> Path:
    summary = DownloadSummary(
        planned_count=4,
        started_outcomes=(
            AccountOutcome(1, 1, AccountStatus.DOWNLOADED, True),
            AccountOutcome(2, 2, AccountStatus.ALL_SKIPPED),
            AccountOutcome(3, 3, AccountStatus.PRIVATE),
        ),
        pre_start_errors=(4,),
        not_started=(),
        primary_status_counts={
            AccountStatus.DOWNLOADED: 1,
            AccountStatus.ALL_SKIPPED: 1,
            AccountStatus.NO_ELIGIBLE_WORKS: 0,
            AccountStatus.PRIVATE: 1,
            AccountStatus.ERROR: 0,
            AccountStatus.INTERRUPTED: 0,
        },
        completed_with_anomaly=(1,),
        complete=True,
        reliable=True,
        reasons=(),
        located=LocatedNativeLogs(
            (NativeLogSegment(native_log, 0, 100),), "size-delta", True
        ),
        exit_code=0,
    )
    block = format_summary_for_task_log(
        summary, datetime.strptime(ended, "%Y-%m-%d %H:%M:%S")
    )
    path = directory / name
    path.write_text(
        f"[{started}] Started；Task template: {template}\n\n{block}",
        encoding="utf-8",
    )
    return path


class ResultDashboardGuiTests(unittest.TestCase):
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
        self.assertFalse(timed_out, "bounded dashboard GUI event loop timed out")
        self.assertTrue(condition())

    def _load(self, window: MainWindow) -> None:
        window.refresh_result_dashboard()
        self._run_until(
            lambda: window._dashboard_snapshot is not None
            and not window.coordinator.has_active_tasks()
        )

    def test_dashboard_is_independent_and_old_result_page_contract_is_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            try:
                self.assertNotEqual(window.dashboard_tab_index, window.result_tab_index)
                self.assertEqual(window.tabs.tabText(window.result_tab_index), "下载结果")
                self.assertEqual(window.tabs.tabText(window.dashboard_tab_index), "结果看板")
                old_headers = tuple(
                    window.result_table.horizontalHeaderItem(index).text()
                    for index in range(window.result_table.columnCount())
                )
                self.assertEqual(
                    old_headers,
                    ("结束时间", "账号", "状态", "异常附加", "任务模板", "来源日志"),
                )
                self.assertEqual(window.dashboard_account_table.height(), 230)
                self.assertEqual(window.dashboard_distribution.rowCount(), 6)
            finally:
                self._dispose(window, home_patch)

    def test_default_load_render_force_refresh_and_log_actions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                native = root / "synthetic-native.log"
                native.write_text("synthetic only", encoding="utf-8")
                task = _write_task_log(
                    window.controller.paths.download_task_logs,
                    "DownloadTask_latest.log",
                    started="2026-08-21 10:00:00",
                    ended="2026-08-21 10:01:30",
                    template="A1-A4.json",
                    native_log=native,
                )
                selected_task = window.controller.result_dashboard.selected_task
                window.controller.result_dashboard.selected_task = Mock(
                    wraps=selected_task
                )

                self._load(window)

                self.assertEqual(window._dashboard_snapshot.task_log, task)
                self.assertEqual(window.dashboard_metric_values["planned"].text(), "4")
                self.assertEqual(window.dashboard_metric_values["started"].text(), "3")
                self.assertEqual(window.dashboard_metric_values["reliable"].text(), "可靠")
                self.assertEqual(window.dashboard_account_table.rowCount(), 4)
                self.assertEqual(window.dashboard_distribution.item(0, 2).text(), "33.3%")

                window.refresh_result_dashboard(force_refresh=True)
                self._run_until(lambda: not window.coordinator.has_active_tasks())
                self.assertTrue(
                    window.controller.result_dashboard.selected_task.call_args.kwargs[
                        "force_refresh"
                    ]
                )

                with patch.object(
                    QDesktopServices, "openUrl", return_value=True
                ) as open_url:
                    window._open_dashboard_task_log()
                    window._open_dashboard_native_log()
                opened = [Path(call.args[0].toLocalFile()) for call in open_url.call_args_list]
                self.assertEqual(opened, [task, native])

                native.unlink()
                with patch.object(QMessageBox, "information") as information, patch.object(
                    QDesktopServices, "openUrl", return_value=True
                ) as open_url:
                    window._open_dashboard_native_log()
                information.assert_called_once()
                open_url.assert_not_called()
            finally:
                self._dispose(window, home_patch)

    def test_manual_selection_survives_automatic_index_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                logs = window.controller.paths.download_task_logs
                native = root / "native.log"
                older = _write_task_log(
                    logs,
                    "DownloadTask_A.log",
                    started="2026-08-21 08:00:00",
                    ended="2026-08-21 08:01:00",
                    template="A.json",
                    native_log=native,
                )
                newer = _write_task_log(
                    logs,
                    "DownloadTask_B.log",
                    started="2026-08-21 09:00:00",
                    ended="2026-08-21 09:01:00",
                    template="B.json",
                    native_log=native,
                )
                self._load(window)
                self.assertEqual(window._dashboard_snapshot.task_log, newer)

                older_key = window._dashboard_key(older)
                window.dashboard_task_selector.setCurrentIndex(
                    window.dashboard_task_selector.findData(older_key)
                )
                self._run_until(
                    lambda: window._dashboard_snapshot.task_log == older
                    and not window.coordinator.has_active_tasks()
                )

                _write_task_log(
                    logs,
                    "DownloadTask_C.log",
                    started="2026-08-21 10:00:00",
                    ended="2026-08-21 10:01:00",
                    template="C.json",
                    native_log=native,
                )
                window.refresh_result_dashboard(auto_refresh=True)
                self._run_until(lambda: not window.coordinator.has_active_tasks())

                self.assertEqual(window.dashboard_task_selector.currentData(), older_key)
                self.assertEqual(window._dashboard_snapshot.task_log, older)
            finally:
                self._dispose(window, home_patch)

    def test_rapid_a_b_c_selection_only_renders_c(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            release = threading.Event()
            try:
                logs = window.controller.paths.download_task_logs
                native = root / "native.log"
                tasks = []
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
                self._load(window)
                original = window.controller.result_dashboard_snapshot
                entered = threading.Event()
                calls: list[str] = []

                def controlled(task_log: Path, **kwargs):
                    calls.append(task_log.name)
                    if task_log == tasks[0]:
                        entered.set()
                        context = kwargs["context"]
                        while not release.wait(0.005):
                            context.raise_if_cancelled()
                    return original(task_log, **kwargs)

                window.controller.result_dashboard_snapshot = controlled
                window.dashboard_task_selector.setCurrentIndex(
                    window.dashboard_task_selector.findData(
                        window._dashboard_key(tasks[0])
                    )
                )
                self._run_until(entered.is_set)
                window.dashboard_task_selector.setCurrentIndex(
                    window.dashboard_task_selector.findData(
                        window._dashboard_key(tasks[1])
                    )
                )
                window.dashboard_task_selector.setCurrentIndex(
                    window.dashboard_task_selector.findData(
                        window._dashboard_key(tasks[2])
                    )
                )
                self._run_until(
                    lambda: window._dashboard_snapshot.task_log == tasks[2]
                    and not window.coordinator.has_active_tasks()
                )

                self.assertEqual(
                    calls,
                    [tasks[0].name, tasks[2].name],
                )
                self.assertEqual(window._dashboard_snapshot.task_template, "C.json")
            finally:
                release.set()
                self._dispose(window, home_patch)

    def test_close_cancels_dashboard_worker_and_rejects_late_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            release = threading.Event()
            try:
                native = root / "native.log"
                task = _write_task_log(
                    window.controller.paths.download_task_logs,
                    "DownloadTask_close.log",
                    started="2026-08-21 10:00:00",
                    ended="2026-08-21 10:01:00",
                    template="close.json",
                    native_log=native,
                )
                self._load(window)
                stable = window._dashboard_snapshot
                entered = threading.Event()
                original = window.controller.result_dashboard_snapshot

                def controlled(task_log: Path, **kwargs):
                    entered.set()
                    context = kwargs["context"]
                    while not release.wait(0.005):
                        context.raise_if_cancelled()
                    return original(task_log, **kwargs)

                window.controller.result_dashboard_snapshot = controlled
                window._load_dashboard_selection(force_refresh=True)
                self._run_until(entered.is_set)
                close_event = Mock()
                with patch.object(QMessageBox, "information"):
                    window.closeEvent(close_event)
                close_event.ignore.assert_called_once()
                self._run_until(lambda: not window.coordinator.has_active_tasks())

                self.assertIs(window.controller.startup_state, StartupState.CLOSING)
                self.assertEqual(window._background_pending, {})
                self.assertFalse(
                    window._render_dashboard_snapshot_if_current(
                        stable, task, stable.fingerprint
                    )
                )
            finally:
                release.set()
                self._dispose(window, home_patch)

    def test_summary_auto_refresh_waits_for_old_result_reader_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            try:
                old_reader = BackgroundTaskBinding(
                    generation_key="result_page_snapshot",
                    generation=1,
                    spec=TaskSpec(
                        task_type="result_page_snapshot",
                        display_name="刷新下载结果",
                    ),
                )
                window._background_bindings["old-reader"] = old_reader
                with patch.object(MainWindow, "refresh_result_dashboard") as refresh:
                    window._defer_dashboard_refresh_until_results_idle()

                    refresh.assert_not_called()
                    self.assertIsNotNone(old_reader.on_removed)
                    window._background_bindings.pop("old-reader")
                    old_reader.on_removed()
                    refresh.assert_called_once_with(window, auto_refresh=True)
                source = inspect.getsource(MainWindow._remove_download_summary)
                self.assertIn(
                    '_refresh_background_targets(("download_results", "runtime_status"))',
                    source,
                )
                self.assertIn("_defer_dashboard_refresh_until_results_idle", source)
            finally:
                self._dispose(window, home_patch)


if __name__ == "__main__":
    unittest.main()
