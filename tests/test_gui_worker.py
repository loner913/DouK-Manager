from __future__ import annotations

import inspect
import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6.QtWidgets  # noqa: F401
except (ImportError, ModuleNotFoundError):
    ActionWorker = None  # type: ignore[assignment,misc]
    MainWindow = None  # type: ignore[assignment,misc]
else:
    import douk_manager.gui as gui_module
    from douk_manager.background import (
        BackgroundTaskCoordinator,
        CancellationToken,
        TaskRecord,
        TaskSpec,
        TaskState,
    )
    from douk_manager.controller import ManagerController
    from douk_manager.core.download_summary import SummaryWriteError
    from douk_manager.core.engine import assess_process_exit
    from douk_manager.gui import ActionWorker, MainWindow, TaskTemplateList
    from douk_manager.startup import StartupSafetyResult, StartupStage, StartupState
    from PySide6.QtCore import QPoint, Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QListWidgetItem, QMainWindow


class QueueInteractionSourceTests(unittest.TestCase):
    @staticmethod
    def _gui_source() -> str:
        return (
            Path(__file__).parents[1] / "src" / "douk_manager" / "gui.py"
        ).read_text(encoding="utf-8")

    def test_drag_drop_is_removed_even_without_gui_dependencies(self) -> None:
        source = (
            Path(__file__).parents[1] / "src" / "douk_manager" / "gui.py"
        ).read_text(encoding="utf-8")

        self.assertNotIn("class ReorderableTaskList", source)
        self.assertNotIn("DragDropMode.InternalMove", source)
        self.assertNotIn("def dropEvent", source)
        self.assertNotIn("def startDrag", source)
        self.assertIn("DragDropMode.NoDragDrop", source)
        self.assertIn("setDragEnabled(False)", source)
        self.assertIn("setAcceptDrops(False)", source)
        self.assertIn('QKeySequence("Alt+Up")', source)
        self.assertIn('QKeySequence("Alt+Down")', source)

    def test_download_summary_lifecycle_exists_without_gui_dependencies(self) -> None:
        source = self._gui_source()

        self.assertIn("def _start_download_summary", source)
        self.assertIn("def _finish_download_summary", source)
        self.assertIn("正在汇总账号结果", source)
        self.assertIn("self.download_summary_thread: QThread | None = None", source)
        self.assertIn("self.download_summary_worker: ActionWorker | None = None", source)

    def test_process_exit_waits_for_summary_service_before_queue_advance(self) -> None:
        source = self._gui_source()
        process_exit_branch = source[
            source.index("    def _poll_processes") : source.index(
                "    def _manual_backup"
            )
        ]

        self.assertIn("self._start_download_summary", process_exit_branch)
        self.assertNotIn("self._start_next_queue_item()\n", process_exit_branch)
        self.assertNotIn("task_log.open", process_exit_branch)
        self.assertNotIn("Download result: unverified", process_exit_branch)

    def test_window_close_waits_for_running_download_summary(self) -> None:
        source = self._gui_source()
        close_branch = source[
            source.index("    def closeEvent") : source.index(
                "    def _apply_style"
            )
        ]

        self.assertIn("download_summary_thread", close_branch)
        self.assertIn("账号结果汇总", close_branch)

    def test_all_formal_activation_and_start_entries_share_queue_gate(self) -> None:
        source = self._gui_source()

        self.assertIn("if activate and self._queue_state_locked():", source)
        self.assertIn(
            "def _activate_selected_task(self) -> None:\n"
            "        if self._queue_state_locked():",
            source,
        )

    def test_queue_and_result_ui_expose_confirmed_v014_repairs(self) -> None:
        source = self._gui_source()
        self.assertIn("class TaskTemplateList", source)
        self.assertIn("self._drag_target_state", source)
        self.assertIn('QPushButton("全选模板")', source)
        self.assertIn('QPushButton("取消全选")', source)
        self.assertIn('QPushButton("删除已选模板")', source)
        self.assertIn('QPushButton("应用为正式 setting")', source)
        self.assertIn('QPushButton("运行当前 setting")', source)
        self.assertIn('QPushButton("按顺序运行已选")', source)
        self.assertIn('QPushButton("取消全部下载任务")', source)
        self.assertIn("cellDoubleClicked.connect(self._open_result_log)", source)
        self.assertIn("_schedule_result_refresh", source)
        self.assertIn("采集服务：", source)
        self.assertIn("下载进程：", source)
        self.assertIn(
            "def _start_current(self) -> None:\n"
            "        if self._queue_state_locked():",
            source,
        )
        self.assertIn(
            "def _start_queue(self) -> None:\n"
            "        if self._queue_state_locked():",
            source,
        )

    def test_v014_result_refresh_interrupt_and_elapsed_ui_hooks_exist(self) -> None:
        source = self._gui_source()
        self.assertIn('QPushButton("立即刷新结果")', source)
        self.assertIn("tabs.currentChanged.connect(self._tab_changed)", source)
        self.assertIn("def _tab_changed", source)
        self.assertIn("def _update_elapsed_labels", source)
        self.assertIn("本次队列耗时", source)
        self.assertIn("当前任务耗时", source)
        self.assertIn("_record_task_elapsed", source)
        self.assertIn("_record_queue_elapsed", source)
        self.assertIn("self.refresh_results()", source)
        self.assertIn("detect_interruption", source)

    def test_drag_check_gesture_owns_mouse_events_instead_of_default_selection(self) -> None:
        source = self._gui_source()
        drag_source = source[
            source.index("class TaskTemplateList") : source.index("class MainWindow")
        ]

        press = drag_source[
            drag_source.index("    def mousePressEvent") : drag_source.index(
                "    def mouseMoveEvent"
            )
        ]
        move = drag_source[
            drag_source.index("    def mouseMoveEvent") : drag_source.index(
                "    def mouseReleaseEvent"
            )
        ]
        release = drag_source[
            drag_source.index("    def mouseReleaseEvent") : drag_source.index(
                "    def _apply_drag_state"
            )
        ]
        self.assertLess(press.index("event.accept()"), press.rindex("super().mousePressEvent"))
        self.assertIn("event.accept()", move)
        self.assertIn("event.accept()", release)

    def test_result_review_is_run_state_and_can_change_during_download(self) -> None:
        source = self._gui_source()
        self.assertIn("def _result_view_option_changed", source)
        self.assertIn("self.controller.set_result_review(run, checked)", source)
        self.assertIn("self.task_pause_console.stateChanged.connect", source)
        self.assertIn("self.queue_pause_console.stateChanged.connect", source)
        self.assertIn("if getattr(run, \"pause_after_exit\", False):", source)
        self.assertNotIn("if self._result_view_enabled():", source)

    def test_cancel_all_downloads_clears_queue_and_skips_post_actions(self) -> None:
        source = self._gui_source()
        cancel_source = source[
            source.index("    def _cancel_all_downloads") : source.index(
                "    def _begin_shutdown_countdown_if_requested"
            )
        ]
        finish_source = source[
            source.index("    def _finish_run_after_summary") : source.index(
                "    def _shutdown_option_changed"
            )
        ]
        self.assertIn("queue_cancel_requested", cancel_source)
        self.assertIn("queue_pending.clear()", cancel_source)
        self.assertIn("cancel_current_download", cancel_source)
        self.assertIn("if self.queue_cancel_requested:", finish_source)

    def test_new_gui_work_cancels_an_active_shutdown_countdown(self) -> None:
        source = self._gui_source()
        run_source = source[source.index("    def _run(") : source.index("    def _run_index_background")]
        background_source = source[
            source.index("    def _run_index_background") : source.index("    def refresh_all")
        ]

        self.assertIn("_cancel_shutdown_for_new_work", run_source)
        self.assertIn("_cancel_shutdown_for_new_work", background_source)

    def test_final_shutdown_tick_rechecks_all_activity_and_none_is_success(self) -> None:
        source = self._gui_source()
        tick_source = source[
            source.index("    def _shutdown_tick") : source.index("    def _cancel_shutdown")
        ]

        for token in (
            "collector",
            "engine_running",
            "monitor_running",
            "queue_active",
            "download_summary_thread",
            "background_thread",
        ):
            self.assertIn(token, tick_source)
        self.assertNotIn("if result is None", tick_source)


class _FakeSignal:
    def __init__(self) -> None:
        self.callbacks: list[object] = []

    def connect(self, callback: object) -> None:
        self.callbacks.append(callback)


class _FakeThread:
    def __init__(self, parent: object) -> None:
        self.parent = parent
        self.started = _FakeSignal()
        self.finished = _FakeSignal()
        self.start_count = 0
        self.quit = Mock()
        self.deleteLater = Mock()

    def start(self) -> None:
        self.start_count += 1


class _FakeWorker:
    def __init__(self, action: object) -> None:
        self.action = action
        self.done = _FakeSignal()
        self.result = None
        self.error = None
        self.thread = None
        self.run = Mock()
        self.deleteLater = Mock()

    def moveToThread(self, thread: object) -> None:
        self.thread = thread


@unittest.skipIf(ActionWorker is None, "PySide6 is installed by the Windows build workflow")
class ActionWorkerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_success_result_is_preserved(self) -> None:
        worker = ActionWorker(lambda: 42)

        worker.run()

        self.assertEqual(worker.result, 42)
        self.assertIsNone(worker.error)

    def test_failure_is_preserved_for_main_thread(self) -> None:
        def fail() -> object:
            raise RuntimeError("expected failure")

        worker = ActionWorker(fail)

        worker.run()

        self.assertIsNone(worker.result)
        self.assertIsInstance(worker.error, RuntimeError)
        self.assertEqual(str(worker.error), "expected failure")

    def test_queue_tab_disables_drag_and_exposes_safe_move_controls(self) -> None:
        queue_source = inspect.getsource(MainWindow._queue_tab)
        move_source = inspect.getsource(MainWindow._move_highlighted_task)
        move_action_source = inspect.getsource(
            MainWindow._move_highlighted_task_by_action
        )
        refresh_source = inspect.getsource(MainWindow.refresh_tasks)
        restore_source = inspect.getsource(MainWindow._restore_task_order)

        self.assertIn("DragDropMode.NoDragDrop", queue_source)
        self.assertNotIn("DragDropMode.InternalMove", queue_source)
        self.assertIn("setDragEnabled(False)", queue_source)
        self.assertIn("setAcceptDrops(False)", queue_source)
        self.assertIn("setDropIndicatorShown(False)", queue_source)
        self.assertIn('QKeySequence("Alt+Up")', queue_source)
        self.assertIn('QKeySequence("Alt+Down")', queue_source)
        self.assertIn('QPushButton("移动高亮任务")', queue_source)
        self.assertIn('QPushButton("恢复按 A 编号排序")', queue_source)
        self.assertIn('QLabel("指定第几位")', queue_source)
        self.assertIn("_move_highlighted_task_by_action", move_source)
        self.assertIn("save_task_order", move_action_source)
        self.assertIn("refresh_tasks()", move_action_source)
        self.assertIn("~Qt.ItemIsDragEnabled", refresh_source)
        self.assertIn("~Qt.ItemIsDropEnabled", refresh_source)
        self.assertIn("restore_task_order", restore_source)
        self.assertIn("refresh_tasks()", restore_source)

    @staticmethod
    def _window_harness(*, exit_code: int = 0) -> MainWindow:
        run = SimpleNamespace(
            running=False,
            process=SimpleNamespace(returncode=exit_code, pid=1234),
            task_template="A1-A2.json",
            selected_accounts=2,
        )
        window = MainWindow.__new__(MainWindow)
        QMainWindow.__init__(window)
        window.controller = SimpleNamespace(
            logger=Mock(),
            engine=SimpleNamespace(
                current=None,
                detect_interruption=Mock(return_value=False),
            ),
            summarize_download=Mock(return_value=object()),
            run_post_actions=Mock(return_value=[]),
            create_task=Mock(),
            activate_task=Mock(),
            activate_and_start=Mock(),
            release_download_lifecycle=Mock(),
            dismiss_result_review=Mock(),
            set_result_review=Mock(
                side_effect=lambda run, enabled: setattr(
                    run, "pause_after_exit", bool(enabled)
                )
            ),
            cancel_current_download=Mock(return_value=Path("cancel.log")),
        )
        window.queue_output = Mock()
        window.queue_active = True
        window.queue_current = run
        window.queue_pending = [Path("A3.json")]
        window.queue_shutdown_requested = False
        window.queue_summaries_complete = True
        window.queue_summaries_reliable = True
        window.queue_cancel_requested = False
        window.queue_paused = False
        window.queue_run_source = "queue"
        window.queue_started_at = None
        window.current_task_started_at = None
        window.queue_elapsed_label = SimpleNamespace(setText=Mock())
        window.task_elapsed_label = SimpleNamespace(setText=Mock())
        window.task_smart_private = SimpleNamespace(isChecked=lambda: False)
        window.queue_shutdown = SimpleNamespace(
            isChecked=lambda: False,
            setChecked=Mock(),
        )
        window.queue_pause_button = SimpleNamespace(setText=Mock())
        window.shutdown_timer = None
        window.download_summary_thread = None
        window.download_summary_worker = None
        window.download_summary_run = None
        window.download_summary_exit_code = None
        window.download_summary_assessment = None
        window.coordinator = BackgroundTaskCoordinator(window)
        window._close_pending = False
        window._append_info = Mock()
        window._start_next_queue_item = Mock()
        window._run = lambda action, _output=None: action()
        window.refresh_all = Mock()
        window.refresh_results = Mock()
        return window

    def test_mouse_drag_checks_and_second_drag_unchecks_the_same_rows(self) -> None:
        widget = TaskTemplateList()
        widget.resize(360, 180)
        for number in range(1, 5):
            item = QListWidgetItem(f"A{number}.json")
            item.setCheckState(Qt.CheckState.Unchecked)
            item.setData(Qt.ItemDataRole.UserRole, str(Path(f"A{number}.json")))
            widget.addItem(item)
        widget.show()
        self.app.processEvents()

        def row_center(row: int) -> QPoint:
            return widget.visualItemRect(widget.item(row)).center()

        QTest.mousePress(
            widget.viewport(), Qt.MouseButton.LeftButton, pos=row_center(0)
        )
        QTest.mouseMove(widget.viewport(), row_center(2), delay=10)
        QTest.mouseRelease(
            widget.viewport(), Qt.MouseButton.LeftButton, pos=row_center(2)
        )
        self.assertEqual(
            [widget.item(row).checkState() for row in range(4)],
            [
                Qt.CheckState.Checked,
                Qt.CheckState.Checked,
                Qt.CheckState.Checked,
                Qt.CheckState.Unchecked,
            ],
        )

        QTest.mousePress(
            widget.viewport(), Qt.MouseButton.LeftButton, pos=row_center(0)
        )
        QTest.mouseMove(widget.viewport(), row_center(2), delay=10)
        QTest.mouseRelease(
            widget.viewport(), Qt.MouseButton.LeftButton, pos=row_center(2)
        )
        self.assertEqual(
            [widget.item(row).checkState() for row in range(4)],
            [Qt.CheckState.Unchecked] * 4,
        )
        widget.close()

    def test_unchecking_result_view_updates_current_run_without_resuming_queue(self) -> None:
        window = self._window_harness()
        window.queue_current.pause_after_exit = True
        window.queue_current.result_review_waiting = False
        window.queue_paused = True

        MainWindow._result_view_option_changed(
            window, "queue", Qt.CheckState.Unchecked.value
        )

        self.assertFalse(window.queue_current.pause_after_exit)
        self.assertTrue(window.queue_paused)
        window.controller.set_result_review.assert_called_once_with(
            window.queue_current, False
        )
        window.controller.dismiss_result_review.assert_not_called()
        window._start_next_queue_item.assert_not_called()

    def test_unchecked_result_view_closes_wrapper_but_paused_queue_stays_paused(
        self,
    ) -> None:
        window = self._window_harness()
        run = window.queue_current
        run.completion_marker = Path("download.exit")
        run.running = True
        run.pause_after_exit = False
        window.queue_paused = True
        window.download_summary_worker = SimpleNamespace(
            error=None,
            result=SimpleNamespace(complete=True, reliable=True),
        )
        window.download_summary_thread = object()
        window.download_summary_run = run
        window.download_summary_exit_code = 0
        window.download_summary_assessment = assess_process_exit(0)
        window.controller.dismiss_result_review.side_effect = lambda _run: setattr(
            _run, "running", False
        )
        window._start_next_queue_item.side_effect = lambda: MainWindow._start_next_queue_item(
            window
        )

        with patch.object(gui_module, "format_summary_for_ui", return_value=("汇总完成",)):
            MainWindow._finish_download_summary(window)

        window.controller.dismiss_result_review.assert_called_once_with(run)
        self.assertTrue(window.queue_paused)
        self.assertIsNone(window.queue_current)
        self.assertEqual(window.queue_pending, [Path("A3.json")])
        window.controller.activate_and_start.assert_not_called()

    def test_one_poll_starts_one_summary_worker_without_advancing_queue(self) -> None:
        window = self._window_harness()
        current = window.queue_current
        pending = list(window.queue_pending)

        with (
            patch.object(gui_module, "QThread", _FakeThread),
            patch.object(gui_module, "ActionWorker", _FakeWorker),
        ):
            MainWindow._poll_processes(window)
            MainWindow._poll_processes(window)

        self.assertIs(window.queue_current, current)
        self.assertEqual(window.queue_pending, pending)
        self.assertTrue(window.queue_active)
        self.assertIsInstance(window.download_summary_thread, _FakeThread)
        self.assertIsInstance(window.download_summary_worker, _FakeWorker)
        self.assertEqual(window.download_summary_thread.start_count, 1)
        window.download_summary_worker.action()
        window.controller.summarize_download.assert_called_once()
        called_run, called_code, called_ended_at = (
            window.controller.summarize_download.call_args.args
        )
        self.assertIs(called_run, current)
        self.assertEqual(called_code, 0)
        self.assertIsNotNone(called_ended_at)
        window._start_next_queue_item.assert_not_called()
        window.controller.run_post_actions.assert_not_called()

    def test_normal_summary_completion_runs_post_actions_then_advances(self) -> None:
        window = self._window_harness()
        summary = SimpleNamespace(complete=True, reliable=True)
        window.download_summary_worker = SimpleNamespace(error=None, result=summary)
        window.download_summary_thread = object()
        window.download_summary_run = window.queue_current
        window.download_summary_exit_code = 0
        window.download_summary_assessment = assess_process_exit(0)
        events: list[object] = []
        window.controller.run_post_actions.side_effect = lambda timing: (
            events.append(("post", timing)) or ["截图归档完成"]
        )
        window._start_next_queue_item.side_effect = lambda: events.append(
            ("next", window.queue_current)
        )
        window._append_info.side_effect = lambda _output, *messages, **_kwargs: (
            events.append(("display", messages))
        )

        with patch.object(
            gui_module,
            "format_summary_for_ui",
            return_value=("下载进程：正常退出（退出码 0）", "账号汇总：完整"),
        ):
            MainWindow._finish_download_summary(window)

        self.assertEqual(
            events,
            [
                (
                    "display",
                    ("下载进程：正常退出（退出码 0）", "账号汇总：完整"),
                ),
                ("post", "batch"),
                ("display", ("截图归档完成",)),
                ("next", None),
            ],
        )
        self.assertTrue(window.queue_active)
        self.assertIsNone(window.queue_current)
        self.assertEqual(window.queue_pending, [Path("A3.json")])
        window.refresh_all.assert_called_once_with()

    def test_incomplete_summary_is_folded_into_queue_completion_state(self) -> None:
        window = self._window_harness()
        window.download_summary_worker = SimpleNamespace(
            error=None,
            result=SimpleNamespace(complete=False, reliable=True),
        )
        window.download_summary_thread = object()
        window.download_summary_run = window.queue_current
        window.download_summary_exit_code = 0
        window.download_summary_assessment = assess_process_exit(0)
        window.controller.run_post_actions.return_value = []

        with patch.object(gui_module, "format_summary_for_ui", return_value=("部分汇总",)):
            MainWindow._finish_download_summary(window)

        self.assertFalse(window.queue_summaries_complete)
        self.assertTrue(window.queue_summaries_reliable)

    def test_background_queue_post_action_success_finalizes_without_resubmitting(self) -> None:
        window = self._window_harness()
        window.queue_current = None
        window.queue_pending = []
        window.queue_cancel_button = SimpleNamespace(setEnabled=Mock())
        window._background_bindings = {}
        window._background_generations = {}
        window._background_pending = {}
        window._submit_background = Mock()

        MainWindow._run_post_actions_background(window, "queue", None)

        binding = window._submit_background.call_args.kwargs
        binding["on_success"](["索引完成"])

        self.assertFalse(window.queue_active)
        self.assertIsNone(window.queue_current)
        window._start_next_queue_item.assert_not_called()
        window.controller.release_download_lifecycle.assert_called_once_with()

    def test_abnormal_summary_completion_displays_partial_result_and_stops(self) -> None:
        window = self._window_harness(exit_code=7)
        window.download_summary_worker = SimpleNamespace(
            error=None,
            result=SimpleNamespace(complete=False, reliable=True),
        )
        window.download_summary_thread = object()
        window.download_summary_run = window.queue_current
        window.download_summary_exit_code = 7
        window.download_summary_assessment = assess_process_exit(7)

        with patch.object(
            gui_module,
            "format_summary_for_ui",
            return_value=("下载进程：异常退出（退出码 7）", "账号汇总：结果不完整"),
        ):
            MainWindow._finish_download_summary(window)

        window._append_info.assert_called_once_with(
            window.queue_output,
            "下载进程：异常退出（退出码 7）",
            "账号汇总：结果不完整",
        )
        self.assertFalse(window.queue_active)
        self.assertIsNone(window.queue_current)
        self.assertEqual(window.queue_pending, [])
        window.controller.run_post_actions.assert_not_called()
        window._start_next_queue_item.assert_not_called()
        window.controller.release_download_lifecycle.assert_called_once_with()

    def test_summary_error_stops_queue_without_advancing(self) -> None:
        window = self._window_harness()
        window.download_summary_worker = SimpleNamespace(
            error=SummaryWriteError("无法将账号汇总写入现有任务日志。"), result=None
        )
        window.download_summary_thread = object()
        window.download_summary_run = window.queue_current
        window.download_summary_exit_code = 0
        window.download_summary_assessment = assess_process_exit(0)

        MainWindow._finish_download_summary(window)

        displayed = "\n".join(
            str(value)
            for call in window._append_info.call_args_list
            for value in call.args[1:]
        )
        self.assertIn("账号结果汇总失败", displayed)
        self.assertFalse(window.queue_active)
        self.assertIsNone(window.queue_current)
        self.assertEqual(window.queue_pending, [])
        window.controller.run_post_actions.assert_not_called()
        window._start_next_queue_item.assert_not_called()
        window.controller.logger.exception.assert_called_once()
        window.controller.release_download_lifecycle.assert_called_once_with()

    def test_completion_always_releases_dedicated_summary_state(self) -> None:
        window = self._window_harness(exit_code=2)
        window.download_summary_worker = SimpleNamespace(
            error=None,
            result=SimpleNamespace(complete=False, reliable=True),
        )
        window.download_summary_thread = object()
        window.download_summary_run = window.queue_current
        window.download_summary_exit_code = 2
        window.download_summary_assessment = assess_process_exit(2)

        with patch.object(gui_module, "format_summary_for_ui", return_value=("部分汇总",)):
            MainWindow._finish_download_summary(window)

        self.assertIsNone(window.download_summary_thread)
        self.assertIsNone(window.download_summary_worker)
        self.assertIsNone(window.download_summary_run)
        self.assertIsNone(window.download_summary_exit_code)
        self.assertIsNone(window.download_summary_assessment)

    def test_close_waits_until_summary_finished_callback_clears_state(self) -> None:
        window = self._window_harness()
        window.download_summary_thread = SimpleNamespace(
            isRunning=Mock(return_value=False)
        )
        window.background_thread = None
        window.controller.stop_collector = Mock()
        event = SimpleNamespace(ignore=Mock(), accept=Mock())

        with patch.object(gui_module.QMessageBox, "information") as information:
            MainWindow.closeEvent(window, event)

        information.assert_called_once()
        event.ignore.assert_called_once_with()
        event.accept.assert_not_called()
        window.controller.stop_collector.assert_not_called()

    def test_close_rejects_running_current_before_summary_thread_exists(self) -> None:
        window = self._window_harness()
        window.queue_current.running = True
        window.download_summary_thread = None
        window.background_thread = None
        window.controller.stop_collector = Mock()
        event = SimpleNamespace(ignore=Mock(), accept=Mock())

        with patch.object(gui_module.QMessageBox, "information") as information:
            MainWindow.closeEvent(window, event)

        information.assert_called_once()
        message = " ".join(str(value) for value in information.call_args.args[1:])
        self.assertIn("账号结果汇总", message)
        event.ignore.assert_called_once_with()
        event.accept.assert_not_called()
        window.controller.stop_collector.assert_not_called()

    def test_close_rejects_exited_current_before_summary_poll(self) -> None:
        window = self._window_harness(exit_code=0)
        self.assertFalse(window.queue_current.running)
        window.download_summary_thread = None
        window.background_thread = None
        window.controller.stop_collector = Mock()
        event = SimpleNamespace(ignore=Mock(), accept=Mock())

        with patch.object(gui_module.QMessageBox, "information") as information:
            MainWindow.closeEvent(window, event)

        information.assert_called_once()
        message = " ".join(str(value) for value in information.call_args.args[1:])
        self.assertIn("账号结果汇总", message)
        event.ignore.assert_called_once_with()
        event.accept.assert_not_called()
        window.controller.stop_collector.assert_not_called()

    def test_close_accepts_when_no_current_run_or_background_work_exists(self) -> None:
        window = self._window_harness()
        window.queue_current = None
        window.download_summary_thread = None
        window.background_thread = None
        window.controller.stop_collector = Mock()
        event = SimpleNamespace(ignore=Mock(), accept=Mock())

        with patch.object(gui_module.QMessageBox, "information") as information:
            MainWindow.closeEvent(window, event)

        information.assert_not_called()
        event.ignore.assert_not_called()
        event.accept.assert_called_once_with()
        window.controller.stop_collector.assert_called_once_with()

    def test_close_in_closing_state_skips_unmanaged_collector(self) -> None:
        window = self._window_harness()
        window.queue_current = None
        window.download_summary_thread = None
        window.background_thread = None
        controller = ManagerController.__new__(ManagerController)
        controller.startup_state = StartupState.CLOSING
        controller.engine = SimpleNamespace(current=None)
        controller.collector = SimpleNamespace(process=None)
        window.controller = controller
        window._safe_widgets = []
        window._path_widgets = []
        window._dangerous_widgets = []
        window.coordinator = BackgroundTaskCoordinator(window)
        self.assertTrue(window.coordinator.begin_closing())
        event = SimpleNamespace(ignore=Mock(), accept=Mock())

        try:
            MainWindow.closeEvent(window, event)
        except Exception as exc:  # pragma: no cover - converted into an assertion below
            self.fail(f"unmanaged collector blocked normal close: {exc}")

        event.ignore.assert_not_called()
        event.accept.assert_called_once_with()
        window.deleteLater()

    def test_close_from_degraded_stops_managed_collector_before_accepting(self) -> None:
        window = self._window_harness()
        window.queue_current = None
        window.download_summary_thread = None
        window.background_thread = None
        stopped: list[bool] = []
        controller = ManagerController.__new__(ManagerController)
        controller.startup_state = StartupState.DEGRADED_READ_ONLY
        controller.engine = SimpleNamespace(current=None)
        controller.collector = SimpleNamespace(
            process=object(),
            stop=lambda: stopped.append(True),
        )
        controller.logger = SimpleNamespace(info=lambda *_args: None)
        window.controller = controller
        window._safe_widgets = []
        window._path_widgets = []
        window._dangerous_widgets = []
        event = SimpleNamespace(ignore=Mock(), accept=Mock())

        close_error: Exception | None = None
        try:
            MainWindow.closeEvent(window, event)
        except Exception as exc:  # pragma: no cover - converted into an assertion below
            close_error = exc

        self.assertIsNone(close_error)
        self.assertIs(controller.startup_state, StartupState.CLOSING)
        self.assertTrue(window.coordinator.is_closing)
        self.assertEqual(stopped, [True])
        event.ignore.assert_not_called()
        event.accept.assert_called_once_with()
        window.deleteLater()

    def test_close_keeps_window_open_when_managed_collector_stop_fails(self) -> None:
        window = self._window_harness()
        window.queue_current = None
        window.download_summary_thread = None
        window.background_thread = None
        controller = ManagerController.__new__(ManagerController)
        controller.startup_state = StartupState.READY
        controller.engine = SimpleNamespace(current=None)

        def fail_stop() -> None:
            raise RuntimeError("synthetic collector stop failure")

        controller.collector = SimpleNamespace(process=object(), stop=fail_stop)
        controller.logger = SimpleNamespace(info=lambda *_args: None)
        window.controller = controller
        window._safe_widgets = []
        window._path_widgets = []
        window._dangerous_widgets = []
        event = SimpleNamespace(ignore=Mock(), accept=Mock())

        close_error: Exception | None = None
        with patch.object(gui_module.QMessageBox, "critical") as critical:
            try:
                MainWindow.closeEvent(window, event)
            except Exception as exc:  # pragma: no cover - converted into an assertion below
                close_error = exc

        self.assertIsNone(close_error)
        self.assertIs(controller.startup_state, StartupState.CLOSING)
        critical.assert_called_once()
        event.ignore.assert_called_once_with()
        event.accept.assert_not_called()
        window.deleteLater()

    def test_close_retries_after_managed_collector_stop_task_is_removed(self) -> None:
        window = self._window_harness()
        controller = ManagerController.__new__(ManagerController)
        controller.startup_state = StartupState.READY
        controller.engine = SimpleNamespace(current=None)
        controller.collector = SimpleNamespace(process=object())
        controller.logger = SimpleNamespace(info=lambda *_args: None)

        def stop_collector() -> None:
            controller.collector.process = None

        controller.stop_collector = stop_collector
        window.controller = controller
        window._safe_widgets = []
        window._path_widgets = []
        window._dangerous_widgets = []
        window._background_bindings = {}
        window._background_generations = {}
        window._background_pending = {}
        window._collector_stop_task_id = None
        window.queue_current = None
        window.download_summary_thread = None
        window.background_thread = None
        window.coordinator.task_settled.connect(window._on_background_task_settled)
        window.coordinator.task_removed.connect(window._on_background_task_removed)
        window.coordinator.idle.connect(window._on_background_tasks_idle)

        try:
            window.show()
            window.close()
            for _ in range(300):
                self.app.processEvents()
                if not window.isVisible() and not window.coordinator.has_active_tasks():
                    break
                QTest.qWait(10)

            self.assertFalse(window.isVisible())
            self.assertFalse(window.coordinator.has_active_tasks())
            self.assertIsNone(controller.collector.process)
            self.assertIs(controller.startup_state, StartupState.CLOSING)
        finally:
            window.hide()
            window.deleteLater()
            self.app.processEvents()

    def test_close_during_startup_requests_cooperative_cancel_without_waiting(self) -> None:
        window = self._window_harness()
        window.queue_current = None
        window.download_summary_thread = None
        window.background_thread = None
        controller = ManagerController.__new__(ManagerController)
        controller.startup_state = StartupState.SAFETY_CHECKING
        controller.startup_generation = 1
        controller.engine = SimpleNamespace(current=None)
        controller.stop_collector = Mock()
        window.controller = controller
        window._safe_widgets = []
        window._path_widgets = []
        window._dangerous_widgets = []
        window._close_pending = False
        coordinator = BackgroundTaskCoordinator(window)
        token = CancellationToken()
        record = TaskRecord(
            task_id="startup-task",
            spec=TaskSpec(task_type="startup_safety", display_name="启动安全检查"),
            generation=1,
            token=token,
            thread=None,
            worker=None,
            state=TaskState.RUNNING,
        )
        coordinator._records[record.task_id] = record
        coordinator._idle_emitted = False
        window.coordinator = coordinator
        event = SimpleNamespace(ignore=Mock(), accept=Mock())

        with patch.object(gui_module.QMessageBox, "information") as information:
            MainWindow.closeEvent(window, event)

        self.assertIs(controller.startup_state, StartupState.CLOSING)
        self.assertTrue(coordinator.is_closing)
        self.assertTrue(token.is_cancelled())
        self.assertTrue(window._close_pending)
        event.ignore.assert_called_once_with()
        event.accept.assert_not_called()
        controller.stop_collector.assert_not_called()
        message = " ".join(str(value) for value in information.call_args.args[1:])
        self.assertIn("启动安全检查", message)
        window.deleteLater()

    def test_coordinator_idle_schedules_one_new_close_attempt(self) -> None:
        window = self._window_harness()
        controller = ManagerController.__new__(ManagerController)
        controller.startup_state = StartupState.CLOSING
        window.controller = controller
        window.coordinator = BackgroundTaskCoordinator(window)
        self.assertTrue(window.coordinator.begin_closing())
        window._close_pending = True
        close_attempts: list[str] = []
        window.close = lambda: close_attempts.append("close")

        callback = getattr(window, "_on_background_tasks_idle", lambda: None)
        callback()
        self.app.processEvents()

        self.assertEqual(close_attempts, ["close"])
        self.assertFalse(window._close_pending)
        window.deleteLater()

    def test_late_startup_result_after_closing_cannot_mutate_gui(self) -> None:
        window = self._window_harness()
        controller = ManagerController.__new__(ManagerController)
        controller.startup_state = StartupState.CLOSING
        controller.startup_generation = 8
        window.controller = controller
        original_result = object()
        window._startup_result = original_result
        mutations: list[str] = []
        window.startup_state_label = SimpleNamespace(
            setText=lambda _value: mutations.append("state")
        )
        window.startup_stage_label = SimpleNamespace(
            setText=lambda _value: mutations.append("stage")
        )
        window.startup_summary_label = SimpleNamespace(
            setText=lambda _value: mutations.append("summary")
        )
        window.startup_details = SimpleNamespace(
            setPlainText=lambda _value: mutations.append("details")
        )

        accepted = MainWindow.apply_startup_result(
            window,
            StartupSafetyResult(
                generation=8,
                success=True,
                state=StartupState.READY,
                stage=StartupStage.SNAPSHOT,
                summary="late ready",
                details="must be ignored",
                health={},
            ),
        )

        self.assertFalse(accepted)
        self.assertIs(window._startup_result, original_result)
        self.assertEqual(mutations, [])
        window.deleteLater()

    def test_final_queue_conclusion_flags_incomplete_or_unreliable_summary(self) -> None:
        window = self._window_harness()
        window.queue_pending = []
        window.queue_current = None
        window.queue_summaries_complete = False

        MainWindow._start_next_queue_item(window)

        displayed = "\n".join(
            str(value)
            for call in window._append_info.call_args_list
            for value in call.args[1:]
        )
        self.assertIn("至少一个任务的账号汇总不完整或不可靠", displayed)
        self.assertNotIn("下载成功", displayed)

    def test_final_queue_conclusion_reports_all_summary_evidence_reliable(self) -> None:
        window = self._window_harness()
        window.queue_pending = []
        window.queue_current = None

        MainWindow._start_next_queue_item(window)

        displayed = "\n".join(
            str(value)
            for call in window._append_info.call_args_list
            for value in call.args[1:]
        )
        self.assertIn("每个任务的账号汇总均完整且可靠", displayed)
        self.assertNotIn("下载成功", displayed)

    def test_create_activate_start_is_blocked_while_summary_holds_queue(self) -> None:
        window = self._window_harness()
        window.task_earliest_mode = SimpleNamespace(currentData=lambda: "keep")
        window.task_earliest_value = Mock()
        window.task_expression = SimpleNamespace(text=lambda: "A4")
        window.task_persist_master = SimpleNamespace(isChecked=lambda: False)
        window.task_name = SimpleNamespace(text=lambda: "")
        window.task_output = Mock()
        window.controller.create_task.return_value = None

        with patch.object(gui_module.QMessageBox, "warning") as warning:
            MainWindow._create_task(window, activate=True, start=True)

        warning.assert_called_once()
        window.controller.create_task.assert_not_called()

    def test_create_activate_start_resets_summary_aggregate_for_new_run(self) -> None:
        window = self._window_harness()
        window.queue_active = False
        window.queue_current = None
        window.queue_pending = []
        window.queue_summaries_complete = False
        window.queue_summaries_reliable = False
        window.task_earliest_mode = SimpleNamespace(currentData=lambda: "keep")
        window.task_earliest_value = Mock()
        window.task_expression = SimpleNamespace(text=lambda: "A4")
        window.task_persist_master = SimpleNamespace(isChecked=lambda: False)
        window.task_name = SimpleNamespace(text=lambda: "")
        window.task_pause_console = SimpleNamespace(isChecked=lambda: False)
        window.task_output = Mock()
        window.controller.paths = SimpleNamespace(active_settings=Path("settings.json"))
        task = SimpleNamespace(
            task_path=Path("A4.json"),
            preview=SimpleNamespace(compact="A4"),
            backup_path=None,
        )
        run = SimpleNamespace(process=SimpleNamespace(pid=9876))
        window.controller.create_task.return_value = task
        window.controller.start_current_download = Mock(return_value=run)

        MainWindow._create_task(window, activate=True, start=True)

        self.assertIs(window.queue_current, run)
        self.assertTrue(window.queue_summaries_complete)
        self.assertTrue(window.queue_summaries_reliable)

    def test_activate_selected_task_is_blocked_while_summary_holds_queue(self) -> None:
        window = self._window_harness()
        window._selected_task_paths = Mock(return_value=[Path("A4.json")])

        with patch.object(gui_module.QMessageBox, "warning") as warning:
            MainWindow._activate_selected_task(window)

        warning.assert_called_once()
        window.controller.activate_task.assert_not_called()

    def test_batch_post_action_failure_stops_queue_without_advancing(self) -> None:
        window = self._window_harness()
        window.download_summary_worker = SimpleNamespace(
            error=None,
            result=SimpleNamespace(complete=True, reliable=True),
        )
        window.download_summary_thread = object()
        window.download_summary_run = window.queue_current
        window.download_summary_exit_code = 0
        window.download_summary_assessment = assess_process_exit(0)
        window._run = Mock(return_value=None)

        with patch.object(gui_module, "format_summary_for_ui", return_value=("汇总完成",)):
            MainWindow._finish_download_summary(window)

        displayed = "\n".join(
            str(value)
            for call in window._append_info.call_args_list
            for value in call.args[1:]
        )
        self.assertIn("后续动作失败", displayed)
        self.assertFalse(window.queue_active)
        self.assertIsNone(window.queue_current)
        self.assertEqual(window.queue_pending, [])
        window._start_next_queue_item.assert_not_called()

    def test_queue_post_action_failure_is_not_reported_as_completion(self) -> None:
        window = self._window_harness()
        window.queue_pending = []
        window.queue_current = None
        window._run = Mock(return_value=None)

        MainWindow._start_next_queue_item(window)

        displayed = "\n".join(
            str(value)
            for call in window._append_info.call_args_list
            for value in call.args[1:]
        )
        self.assertIn("队列后续动作失败", displayed)
        self.assertNotIn("队列执行结束", displayed)
        self.assertFalse(window.queue_active)
