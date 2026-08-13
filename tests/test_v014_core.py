from __future__ import annotations

import json
import os
import signal
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

from douk_manager.config import AppConfig
from douk_manager.core.backup import BackupService
from douk_manager.core.result_history import ResultHistoryService
from douk_manager.core.settings_tasks import EarliestRule, SettingsTaskError, SettingsTaskService
from douk_manager.core.engine import (
    BATCH_RUN_COMMAND,
    MONITOR_RUN_COMMAND,
    EngineError,
    EngineService,
)
from tests.helpers import make_test_paths


def _write_log(directory: Path, name: str, ended: str, private: str, normal: str = "") -> Path:
    path = directory / name
    path.write_text(
        "[2026-08-13 10:00:00] Started；Task template: A1_任务\n"
        "【下载账号汇总】\n"
        f"进程结束时间：{ended}\n"
        "退出码：0\n"
        "账号汇总：完整\n"
        "账号明细版本：1\n"
        f"私密账号（{1 if private else 0}）：{private}\n"
        + (f"有新作品下载（1）：{normal}\n" if normal else ""),
        encoding="utf-8",
    )
    return path


class V014CoreTests(unittest.TestCase):
    def test_incomplete_newer_run_without_requested_row_blocks_older_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_log(
                root,
                "DownloadTask_2026-08-12_10-00-00.log",
                "2026-08-12 10:00:00",
                "A3",
            )
            sparse = _write_log(
                root,
                "DownloadTask_2026-08-13_10-00-00.log",
                "2026-08-13 10:00:00",
                "",
                "",
            )
            sparse.write_text(
                sparse.read_text(encoding="utf-8").replace("账号明细版本：1\n", ""),
                encoding="utf-8",
            )

            service = ResultHistoryService(root)
            self.assertEqual(service.list_runs()[0].account_rows, ())
            self.assertEqual(
                service.recent_private((3,), 7, now=datetime(2026, 8, 13, 12)),
                (),
            )

    def test_sparse_newer_result_does_not_override_complete_private_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_log(
                root,
                "DownloadTask_2026-08-12_10-00-00.log",
                "2026-08-12 10:00:00",
                "A3",
            )
            sparse = _write_log(
                root,
                "DownloadTask_2026-08-13_10-00-00.log",
                "2026-08-13 10:00:00",
                "",
                "A3",
            )
            sparse.write_text(
                sparse.read_text(encoding="utf-8").replace("账号明细版本：1\n", ""),
                encoding="utf-8",
            )

            service = ResultHistoryService(root)
            runs = service.list_runs()
            self.assertFalse(runs[0].details_complete)
            self.assertEqual(runs[0].account_rows[0].a_number, 3)
            self.assertEqual(
                service.recent_private((3,), 7, now=datetime(2026, 8, 13, 12)),
                (),
            )

    def test_incomplete_private_result_is_visible_but_not_automatic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sparse = _write_log(
                root,
                "DownloadTask_2026-08-13_10-00-00.log",
                "2026-08-13 10:00:00",
                "A3",
            )
            sparse.write_text(
                sparse.read_text(encoding="utf-8").replace("账号明细版本：1\n", ""),
                encoding="utf-8",
            )

            service = ResultHistoryService(root)
            self.assertEqual(service.list_runs()[0].account_rows[0].a_number, 3)
            self.assertEqual(
                service.recent_private((3,), 7, now=datetime(2026, 8, 13, 12)),
                (),
            )

    def test_result_history_reads_complete_mapping_and_latest_private(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _write_log(
                root, "DownloadTask_2026-08-12_10-00-00.log", "2026-08-12 10:00:00", "A3"
            )
            second = _write_log(
                root, "DownloadTask_2026-08-13_10-00-00.log", "2026-08-13 10:00:00", "", "A3"
            )
            service = ResultHistoryService(root)
            runs = service.list_runs()
            self.assertEqual(len(runs), 2)
            self.assertTrue(runs[0].details_complete)
            self.assertEqual(runs[0].account_rows[0].a_number, 3)
            self.assertEqual(service.recent_private((3,), 7, now=datetime(2026, 8, 13, 12)), ())
            self.assertEqual(first.name, "DownloadTask_2026-08-12_10-00-00.log")
            self.assertEqual(second.name, "DownloadTask_2026-08-13_10-00-00.log")

    def test_private_reference_expiry_and_old_log_is_conservative(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            _write_log(
                root, "DownloadTask_2026-08-01_10-00-00.log", "2026-08-01 10:00:00", "A2"
            )
            service = ResultHistoryService(root)
            self.assertEqual(
                service.recent_private((2,), 3, now=datetime(2026, 8, 13, 10)), ()
            )

    def test_delete_tasks_only_allows_task_directory_and_protects_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 2)
            service = SettingsTaskService(paths, BackupService(paths))
            created = service.create_task("A1", task_earliest=EarliestRule.keep())
            deleted = service.delete_tasks((created.task_path,))
            self.assertEqual(deleted, (created.task_path.resolve(),))
            self.assertFalse(created.task_path.exists())
            outside = paths.data / "outside.json"
            outside.write_text("{}", encoding="utf-8")
            with self.assertRaises(SettingsTaskError):
                service.delete_tasks((outside,))

    def test_monitor_switches_command_and_restores_after_normal_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 2)
            active = json.loads(paths.active_settings.read_text(encoding="utf-8"))
            active["run_command"] = BATCH_RUN_COMMAND
            paths.active_settings.write_text(json.dumps(active), encoding="utf-8")
            backup = BackupService(paths)
            backup.create_critical_snapshot = Mock(return_value=paths.backups / "snapshot")
            service = EngineService(paths, AppConfig(engine_exe=str(paths.engine_exe)), backup)
            process = Mock()
            process.poll.return_value = None
            service.external_running = Mock(return_value=False)
            with patch("douk_manager.core.engine.subprocess.Popen", return_value=process):
                run = service.start_monitor()
            self.assertEqual(run.mode, "monitor")
            self.assertEqual(
                json.loads(paths.active_settings.read_text(encoding="utf-8"))["run_command"],
                MONITOR_RUN_COMMAND,
            )
            service.stop_monitor()
            self.assertEqual(
                json.loads(paths.active_settings.read_text(encoding="utf-8"))["run_command"],
                BATCH_RUN_COMMAND,
            )
            if os.name == "nt":
                process.send_signal.assert_called_once_with(signal.CTRL_BREAK_EVENT)
                process.terminate.assert_not_called()
            else:
                process.terminate.assert_called_once_with()
                process.send_signal.assert_not_called()
            process.wait.assert_called_once_with(timeout=15.0)

    def test_monitor_backup_failure_releases_mutex_without_launching(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 2)
            backup = BackupService(paths)
            backup.create_critical_snapshot = Mock(side_effect=OSError("backup failed"))
            service = EngineService(paths, AppConfig(engine_exe=str(paths.engine_exe)), backup)
            service.external_running = Mock(return_value=False)
            mutex = Mock()

            with patch("douk_manager.core.engine._WindowsEngineMutex.acquire", return_value=mutex), patch(
                "douk_manager.core.engine.subprocess.Popen"
            ) as popen:
                with self.assertRaises(EngineError):
                    service.start_monitor()

            mutex.close.assert_called_once_with()
            popen.assert_not_called()
            self.assertIsNone(service.current)

    def test_monitor_post_launch_failure_terminates_process_and_restores_command(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 2)
            backup = BackupService(paths)
            backup.create_critical_snapshot = Mock(return_value=paths.backups / "snapshot")
            service = EngineService(paths, AppConfig(engine_exe=str(paths.engine_exe)), backup)
            service.external_running = Mock(return_value=False)
            process = Mock()
            process.poll.return_value = None

            with patch("douk_manager.core.engine.subprocess.Popen", return_value=process), patch(
                "douk_manager.core.engine.format_information",
                side_effect=RuntimeError("log failed"),
            ):
                with self.assertRaises(EngineError):
                    service.start_monitor()

            if os.name == "nt":
                process.send_signal.assert_called_once_with(signal.CTRL_BREAK_EVENT)
                process.terminate.assert_not_called()
            else:
                process.terminate.assert_called_once_with()
                process.send_signal.assert_not_called()
            process.wait.assert_called_once()
            self.assertEqual(
                json.loads(paths.active_settings.read_text(encoding="utf-8"))["run_command"],
                BATCH_RUN_COMMAND,
            )
            self.assertIsNone(service.current)


if __name__ == "__main__":
    unittest.main()
