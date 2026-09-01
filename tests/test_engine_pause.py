from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from douk_manager.config import AppConfig
from douk_manager.core.backup import BackupService
from douk_manager.core.engine import BATCH_RUN_COMMAND, ENGINE_MODE_BATCH, EngineService
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

    def test_start_passes_frozen_success_delay_to_engine_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 2)
            active = json.loads(paths.active_settings.read_text(encoding="utf-8"))
            active["run_command"] = BATCH_RUN_COMMAND
            active["accounts_urls"][0]["enable"] = True
            paths.active_settings.write_text(json.dumps(active), encoding="utf-8")
            backup = BackupService(paths)
            backup.create_critical_snapshot = Mock(return_value=paths.backups / "snapshot")
            config = AppConfig(engine_exe=str(paths.engine_exe), request_avg_delay=5)
            service = EngineService(paths, config, backup)
            service.external_running = Mock(return_value=False)
            process = Mock(pid=1234)
            with patch(
                "douk_manager.core.engine.subprocess.Popen", return_value=process
            ) as popen:
                run = service.start()
            self.assertEqual(popen.call_args.kwargs["env"]["DOUK_REQUEST_AVG_DELAY"], "5.0")
            self.assertEqual(run.request_avg_delay, 5.0)

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
