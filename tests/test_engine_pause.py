from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from douk_manager.config import AppConfig
from douk_manager.core.backup import BackupService
from douk_manager.core.engine import ENGINE_MODE_BATCH, EngineService
from tests.helpers import make_test_paths


class EnginePauseTests(unittest.TestCase):
    def test_pause_wrapper_only_waits_when_review_control_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 2)
            config = AppConfig(
                engine_exe=str(paths.engine_exe),
                video_root=str(paths.video_root),
                index_root=str(paths.index_root),
            )
            service = EngineService(paths, config, BackupService(paths))
            marker = paths.data / "RunWrappers" / "download.exit"
            control = paths.data / "RunWrappers" / "download.review"
            wrapper = service._write_pause_wrapper(marker, control)
            content = wrapper.read_text(encoding="ascii")
            self.assertIn(f'call "{paths.engine_exe}"', content)
            self.assertIn('set "DOUK_ENGINE_EXIT=%ERRORLEVEL%"', content)
            self.assertIn('set "DOUK_RESULT_REVIEW=0"', content)
            self.assertIn(
                'if exist "{}" set /p DOUK_RESULT_REVIEW=<"{}"'.format(
                    control, control
                ),
                content,
            )
            self.assertIn('if "%DOUK_RESULT_REVIEW%"=="1" (', content)
            self.assertIn("pause >nul", content)
            self.assertIn("exit /b %DOUK_ENGINE_EXIT%", content)

    def test_result_review_control_updates_run_and_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 2)
            service = EngineService(
                paths,
                AppConfig(engine_exe=str(paths.engine_exe)),
                BackupService(paths),
            )
            control = paths.data / "RunWrappers" / "download.review"
            run = Mock(pause_after_exit=True, review_control=control)

            service.set_result_review(run, False)

            self.assertFalse(run.pause_after_exit)
            self.assertEqual(control.read_text(encoding="ascii"), "0\n")

    def test_dismiss_result_review_closes_the_windows_process_tree(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 2)
            service = EngineService(paths, AppConfig(engine_exe=str(paths.engine_exe)), BackupService(paths))
            process = Mock(pid=4321)
            process.poll.return_value = None
            run = Mock(process=process)

            with patch.object(service, "_terminate_process_tree") as terminate:
                service.dismiss_result_review(run)

            terminate.assert_called_once_with(process, timeout=5.0, force=True)

    def test_cancel_batch_closes_tree_records_cancel_and_clears_current(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 2)
            service = EngineService(paths, AppConfig(engine_exe=str(paths.engine_exe)), BackupService(paths))
            task_log = paths.download_task_logs / "DownloadTask_cancel.log"
            task_log.write_text("started\n", encoding="utf-8")
            process = Mock(pid=4321)
            run = Mock(
                process=process,
                mode=ENGINE_MODE_BATCH,
                running=True,
                task_log=task_log,
            )
            service.current = run

            with patch("douk_manager.core.engine._is_windows", return_value=False), patch.object(
                service, "_terminate_process_tree"
            ) as terminate:
                result = service.cancel_batch(run)

            self.assertEqual(result, task_log)
            terminate.assert_called_once_with(process, timeout=15.0, force=True)
            self.assertIsNone(service.current)
            self.assertIn("Cancelled", task_log.read_text(encoding="utf-8"))

    def test_windows_tree_close_targets_only_the_manager_owned_pid(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 2)
            service = EngineService(
                paths,
                AppConfig(engine_exe=str(paths.engine_exe)),
                BackupService(paths),
            )
            process = Mock(pid=2468)
            process.poll.return_value = None

            with patch("douk_manager.core.engine._is_windows", return_value=True), patch(
                "douk_manager.core.engine.subprocess.run"
            ) as run_command:
                service._terminate_process_tree(process, timeout=5.0, force=True)

            self.assertEqual(
                run_command.call_args.args[0],
                ["taskkill", "/PID", "2468", "/T", "/F"],
            )
            process.wait.assert_called_once_with(timeout=5.0)


if __name__ == "__main__":
    unittest.main()
