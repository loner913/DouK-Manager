from __future__ import annotations

import unittest

try:
    from douk_manager.gui import ActionWorker
except ModuleNotFoundError as exc:
    if exc.name != "PySide6":
        raise
    ActionWorker = None  # type: ignore[assignment,misc]


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
