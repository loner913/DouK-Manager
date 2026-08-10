from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

try:
    from PySide6.QtWidgets import QAbstractItemView, QApplication, QPushButton

    from douk_manager.gui import ActionWorker, MainWindow
except ModuleNotFoundError as exc:
    if exc.name != "PySide6":
        raise
    ActionWorker = None  # type: ignore[assignment,misc]
    MainWindow = None  # type: ignore[assignment,misc]


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
        with tempfile.TemporaryDirectory() as directory:
            previous_root = os.environ.get("DOUK_MANAGER_HOME")
            os.environ["DOUK_MANAGER_HOME"] = str(Path(directory) / "manager")
            app = QApplication.instance() or QApplication([])
            window = None
            try:
                window = MainWindow()
                self.assertEqual(
                    window.task_list.dragDropMode(),
                    QAbstractItemView.DragDropMode.InternalMove,
                )
                self.assertEqual(window.queue_move_target.count(), 4)
                button_texts = {
                    button.text() for button in window.findChildren(QPushButton)
                }
                self.assertIn("移动高亮任务", button_texts)
                self.assertIn("恢复按 A 编号排序", button_texts)
            finally:
                if window is not None:
                    window.close()
                app.processEvents()
                if previous_root is None:
                    os.environ.pop("DOUK_MANAGER_HOME", None)
                else:
                    os.environ["DOUK_MANAGER_HOME"] = previous_root
