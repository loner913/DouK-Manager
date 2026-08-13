from __future__ import annotations

import builtins
import json
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

from douk_manager.config import AppConfig
from douk_manager.controller import ManagerController
from douk_manager.core.backup import BackupService
from douk_manager.core.download_summary import PlannedAccount, SummaryWriteError
from douk_manager.core.engine import EngineRun, EngineService
from tests.helpers import make_test_paths


class _RunningProcess:
    pid = 12345
    returncode = None

    @staticmethod
    def poll() -> None:
        return None


class EngineTaskLogTests(unittest.TestCase):
    def test_task_log_names_template_and_explains_pause_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 3)
            active = json.loads(paths.active_settings.read_text(encoding="utf-8"))
            active["run_command"] = "5 1 1 Q"
            for index, account in enumerate(active["accounts_urls"]):
                account["enable"] = index in (0, 2)
            paths.active_settings.write_text(
                json.dumps(active, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            native_log_dir = paths.volume / "Log"
            native_log_dir.mkdir()
            (native_log_dir / "existing.log").write_text("native log", encoding="utf-8")
            config = AppConfig(
                engine_exe=str(paths.engine_exe),
                video_root=str(paths.video_root),
                index_root=str(paths.index_root),
                batch_accounts=50,
                rest_seconds=150,
            )
            service = EngineService(paths, config, BackupService(paths))
            service.external_running = lambda: False

            with patch(
                "douk_manager.core.engine.subprocess.Popen",
                return_value=_RunningProcess(),
            ):
                run = service.start(task_template=Path("A51.json"))

            content = run.task_log.read_text(encoding="utf-8")
            self.assertEqual(run.task_log.parent, paths.download_task_logs)
            self.assertRegex(
                run.task_log.name,
                r"^DownloadTask_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}-\d{6}\.log$",
            )
            self.assertIn("Log scope: one downloader process", content)
            self.assertIn("Task template: A51.json", content)
            self.assertIn("Selected accounts: 2", content)
            self.assertIn("Pause every accounts: 50", content)
            self.assertIn("Pause seconds: 150", content)
            self.assertNotIn("Batch accounts:", content)
            self.assertEqual(run.task_template, "A51.json")
            self.assertEqual(
                [(item.task_index, item.a_number) for item in run.planned_accounts],
                [(1, 1), (2, 3)],
            )
            self.assertEqual(run.selected_accounts, 2)
            self.assertEqual(run.native_log_dir, paths.volume / "Log")
            self.assertEqual(
                [item.path.name for item in run.native_log_snapshot], ["existing.log"]
            )
            self.assertNotIn("native_log_snapshot", content)
            self.assertNotIn(active["accounts_urls"][0]["url"], content)

    def test_finished_run_appends_exactly_one_summary_block(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 1)
            native_log_dir = paths.volume / "Log"
            native_log_dir.mkdir()
            service = EngineService(paths, AppConfig(), BackupService(paths))
            started_at = datetime.now()
            task_log = paths.download_task_logs / "DownloadTask_test.log"
            task_log.write_text("existing task log\n", encoding="utf-8")
            native_log = native_log_dir / "native.log"
            native_log.write_text(
                "共有 1 个账号的作品等待下载\n"
                "开始处理第 1 个账号\n"
                "标识：A1account；账号昵称：safe\n"
                "筛选处理后作品数量: 0\n",
                encoding="utf-8-sig",
            )
            run = EngineRun(
                process=_RunningProcess(),
                started_at=started_at,
                task_log=task_log,
                planned_accounts=(PlannedAccount(1, 1, "A1account"),),
                native_log_snapshot=(),
                native_log_dir=native_log_dir,
            )

            result = service.summarize_finished_run(
                run, 0, started_at + timedelta(seconds=1)
            )
            repeated = service.summarize_finished_run(
                run, 0, started_at + timedelta(seconds=2)
            )

            content = task_log.read_text(encoding="utf-8")
            self.assertTrue(result.complete)
            self.assertIs(repeated, result)
            self.assertEqual(content.count("【下载账号汇总】"), 1)
            self.assertEqual(content.count("【状态说明】"), 1)
            self.assertTrue(content.startswith("existing task log\n"))

    def test_summary_append_failure_raises_summary_write_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 1)
            native_log_dir = paths.volume / "Log"
            native_log_dir.mkdir()
            service = EngineService(paths, AppConfig(), BackupService(paths))
            started_at = datetime.now()
            run = EngineRun(
                process=_RunningProcess(),
                started_at=started_at,
                task_log=paths.download_task_logs,
                planned_accounts=(),
                native_log_snapshot=(),
                native_log_dir=native_log_dir,
            )

            with self.assertRaises(SummaryWriteError):
                service.summarize_finished_run(run, 3, started_at)

    def test_partial_append_failure_is_terminal_and_never_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 1)
            native_log_dir = paths.volume / "Log"
            native_log_dir.mkdir()
            service = EngineService(paths, AppConfig(), BackupService(paths))
            started_at = datetime.now()
            task_log = paths.download_task_logs / "DownloadTask_partial.log"
            task_log.write_text("existing task log\n", encoding="utf-8")
            run = EngineRun(
                process=_RunningProcess(),
                started_at=started_at,
                task_log=task_log,
                planned_accounts=(),
                native_log_snapshot=(),
                native_log_dir=native_log_dir,
            )
            original_open = Path.open
            append_attempts = 0

            class _PartialWriter:
                def __enter__(self):
                    self.handle = builtins.open(task_log, "a", encoding="utf-8")
                    return self

                def write(self, content: str) -> None:
                    self.handle.write(content[:20])
                    self.handle.flush()
                    raise OSError("simulated partial append")

                def __exit__(self, *args) -> None:
                    self.handle.close()

            def guarded_open(path: Path, *args, **kwargs):
                nonlocal append_attempts
                if path == task_log and args and args[0] == "a":
                    append_attempts += 1
                    return _PartialWriter()
                return original_open(path, *args, **kwargs)

            with patch.object(Path, "open", guarded_open):
                with self.assertRaises(SummaryWriteError):
                    service.summarize_finished_run(run, 3, started_at)
                with self.assertRaises(SummaryWriteError):
                    service.summarize_finished_run(run, 3, started_at)

            self.assertEqual(append_attempts, 1)
            self.assertEqual(
                task_log.read_text(encoding="utf-8").count("existing task log"), 1
            )

    def test_concurrent_completion_notifications_share_one_append_and_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 1)
            native_log_dir = paths.volume / "Log"
            native_log_dir.mkdir()
            service = EngineService(paths, AppConfig(), BackupService(paths))
            started_at = datetime.now()
            task_log = paths.download_task_logs / "DownloadTask_concurrent.log"
            task_log.write_text("existing task log\n", encoding="utf-8")
            run = EngineRun(
                process=_RunningProcess(),
                started_at=started_at,
                task_log=task_log,
                planned_accounts=(),
                native_log_snapshot=(),
                native_log_dir=native_log_dir,
            )
            first_append_entered = threading.Event()
            allow_first_append = threading.Event()
            original_open = Path.open
            append_attempts = 0

            class _DelayedWriter:
                def __enter__(self):
                    self.handle = original_open(task_log, "a", encoding="utf-8")
                    first_append_entered.set()
                    allow_first_append.wait(timeout=2)
                    return self.handle

                def __exit__(self, *args):
                    return self.handle.__exit__(*args)

            def guarded_open(path: Path, *args, **kwargs):
                nonlocal append_attempts
                if path == task_log and args and args[0] == "a":
                    append_attempts += 1
                    return _DelayedWriter()
                return original_open(path, *args, **kwargs)

            results = []
            errors = []

            def summarize() -> None:
                try:
                    results.append(
                        service.summarize_finished_run(run, 3, started_at)
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            with patch.object(Path, "open", guarded_open):
                first = threading.Thread(target=summarize)
                second = threading.Thread(target=summarize)
                first.start()
                self.assertTrue(first_append_entered.wait(timeout=2))
                second.start()
                allow_first_append.set()
                first.join(timeout=2)
                second.join(timeout=2)

            self.assertFalse(first.is_alive())
            self.assertFalse(second.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(append_attempts, 1)
            self.assertEqual(len(results), 2)
            self.assertIs(results[0], results[1])
            self.assertEqual(
                task_log.read_text(encoding="utf-8").count("【下载账号汇总】"), 1
            )

    def test_controller_delegates_summary_and_logs_only_safe_audit_fields(self) -> None:
        controller = ManagerController.__new__(ManagerController)
        controller.engine = Mock()
        controller.logger = Mock()
        summary = Mock(
            planned_count=12,
            started_count=8,
            complete=False,
            reliable=True,
        )
        controller.engine.summarize_finished_run.return_value = summary
        ended_at = datetime(2026, 8, 13, 11, 22, 33)
        run = Mock(task_template="A50-A99.json", task_log=Path("safe-task.log"))

        result = controller.summarize_download(run, 0, ended_at)

        self.assertIs(result, summary)
        controller.engine.summarize_finished_run.assert_called_once_with(
            run, 0, ended_at
        )
        logged = controller.logger.info.call_args
        self.assertEqual(
            logged.args[0],
            "下载账号汇总完成：模板=%s；计划=%s；实际开始=%s；完整=%s；可靠=%s；任务日志=%s",
        )
        self.assertEqual(
            logged.args[1:],
            ("A50-A99.json", 12, 8, False, True, Path("safe-task.log")),
        )


if __name__ == "__main__":
    unittest.main()
