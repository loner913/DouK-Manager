from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from douk_manager.config import AppConfig
from douk_manager.integrations.collector import CollectorService, CollectorServiceError
from douk_manager.operation import OperationContext, TaskCancelled
from tests.helpers import make_test_paths


class FakeProcess:
    def __init__(self, poll_values: list[int | None]) -> None:
        self._poll_values = iter(poll_values)
        self._last: int | None = None

    def poll(self) -> int | None:
        try:
            self._last = next(self._poll_values)
        except StopIteration:
            pass
        return self._last

    def terminate(self) -> None:
        self._last = 0

    def wait(self, timeout: float | None = None) -> int:
        return int(self._last or 0)

    def kill(self) -> None:
        self._last = -9


class CollectorStartupTests(unittest.TestCase):
    def _service(self, root: Path) -> CollectorService:
        paths = make_test_paths(root)
        paths.collector_excel.write_bytes(b"test workbook placeholder")
        config = AppConfig(
            engine_exe=str(paths.engine_exe),
            video_root=str(paths.video_root),
            index_root=str(paths.index_root),
            collector_port=18765,
        )
        return CollectorService(config, paths)

    def test_start_reports_success_only_after_health_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory))
            process = FakeProcess([None, None, None])
            service.health = Mock(side_effect=[False, False, True])
            with patch(
                "douk_manager.integrations.collector.subprocess.Popen",
                return_value=process,
            ) as popen, patch("douk_manager.integrations.collector.time.sleep"):
                service.start()
            try:
                self.assertTrue(service.running)
                self.assertIsNotNone(service.last_log_path)
                self.assertEqual(
                    service.last_log_path.parent, service.paths.collector_logs
                )
                self.assertRegex(
                    service.last_log_path.name,
                    r"^Collector_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}\.log$",
                )
                child_env = popen.call_args.kwargs["env"]
                self.assertEqual(child_env["PYTHONIOENCODING"], "utf-8")
                self.assertEqual(child_env["PYTHONUTF8"], "1")
                self.assertEqual(
                    child_env["DOUK_GLOBAL_LOCK_PATH"], str(service.paths.lock_file)
                )
            finally:
                service.stop()

    def test_early_process_exit_includes_log_and_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory))

            def fake_popen(*args, **kwargs):
                kwargs["stdout"].write("worker import failed\n")
                kwargs["stdout"].flush()
                return FakeProcess([7])

            service.health = Mock(return_value=False)
            with patch(
                "douk_manager.integrations.collector.subprocess.Popen",
                side_effect=fake_popen,
            ):
                with self.assertRaises(CollectorServiceError) as caught:
                    service.start()
            message = str(caught.exception)
            self.assertIn("退出码 7", message)
            self.assertIn("worker import failed", message)
            self.assertIn("Collector_", message)
            self.assertIsNone(service.process)

    def test_cancel_during_health_wait_stops_created_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory))
            process = FakeProcess([None, None])
            context = OperationContext()
            health_calls = 0

            def cancel_after_process_created(*_args, **_kwargs):
                nonlocal health_calls
                health_calls += 1
                if health_calls == 2:
                    context.request_cancel()
                return False

            service.health = Mock(side_effect=cancel_after_process_created)
            with patch(
                "douk_manager.integrations.collector.subprocess.Popen",
                return_value=process,
            ):
                with self.assertRaises(TaskCancelled):
                    service.start(context=context)
            self.assertIsNone(service.process)
            self.assertEqual(process.poll(), 0)

    def test_health_success_seals_collector_start_against_late_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory))
            process = FakeProcess([None, None])
            context = OperationContext()
            service.health = Mock(side_effect=[False, True])
            with patch(
                "douk_manager.integrations.collector.subprocess.Popen",
                return_value=process,
            ):
                service.start(context=context)
            try:
                self.assertTrue(context.critical_to_completion)
                self.assertFalse(context.request_cancel())
                self.assertTrue(service.running)
            finally:
                service.stop()


if __name__ == "__main__":
    unittest.main()
