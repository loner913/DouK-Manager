from __future__ import annotations

import inspect
import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    import PySide6.QtWidgets  # noqa: F401
except (ImportError, ModuleNotFoundError):
    ActionWorker = None  # type: ignore[assignment,misc]
    MainWindow = None  # type: ignore[assignment,misc]
else:
    from douk_manager.gui import ActionWorker, MainWindow, ReorderableTaskList


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

    def test_queue_tab_exposes_real_internal_move_and_restore_controls(self) -> None:
        queue_source = inspect.getsource(MainWindow._queue_tab)
        drag_source = inspect.getsource(MainWindow._task_order_dragged)
        drop_source = inspect.getsource(ReorderableTaskList.dropEvent)
        move_source = inspect.getsource(MainWindow._move_highlighted_task)
        restore_source = inspect.getsource(MainWindow._restore_task_order)

        self.assertIn("DragDropMode.InternalMove", queue_source)
        self.assertIn('QPushButton("移动高亮任务")', queue_source)
        self.assertIn('QPushButton("恢复按 A 编号排序")', queue_source)
        self.assertIn('QLabel("指定第几位")', queue_source)
        self.assertIn("takeItem", drop_source)
        self.assertNotIn("super().dropEvent", drop_source)
        self.assertIn("refresh_tasks()", drag_source)
        self.assertIn("save_task_order", move_source)
        self.assertIn("refresh_tasks()", move_source)
        self.assertIn("restore_task_order", restore_source)
        self.assertIn("refresh_tasks()", restore_source)
