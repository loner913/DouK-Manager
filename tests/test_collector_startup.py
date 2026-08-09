from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from douk_manager.config import AppConfig
from douk_manager.integrations.collector import CollectorService, CollectorServiceError
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
            ), patch("douk_manager.integrations.collector.time.sleep"):
                service.start()
            self.assertTrue(service.running)
            self.assertIsNotNone(service.last_log_path)
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


if __name__ == "__main__":
    unittest.main()
