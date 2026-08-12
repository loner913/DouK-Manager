from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from douk_manager.config import AppConfig
from douk_manager.core.backup import BackupService
from douk_manager.core.engine import EngineService
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


if __name__ == "__main__":
    unittest.main()
