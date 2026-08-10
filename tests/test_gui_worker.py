from __future__ import annotations

import inspect
import os
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6.QtWidgets  # noqa: F401
except (ImportError, ModuleNotFoundError):
    ActionWorker = None  # type: ignore[assignment,misc]
    MainWindow = None  # type: ignore[assignment,misc]
else:
    from douk_manager.gui import ActionWorker, MainWindow


class QueueInteractionSourceTests(unittest.TestCase):
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


@unittest.skipIf(ActionWorker is None, "PySide6 is installed by the Windows build workflow")
class ActionWorkerTests(unittest.TestCase):
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
