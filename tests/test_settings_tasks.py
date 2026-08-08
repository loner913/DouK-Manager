from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from douk_manager.core.backup import BackupService
from douk_manager.core.json_store import read_json
from douk_manager.core.settings_tasks import EarliestRule, SettingsTaskService
from tests.helpers import make_test_paths


class SettingsTaskTests(unittest.TestCase):
    def test_master_enable_is_unchanged_and_active_enable_is_selected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 8)
            service = SettingsTaskService(paths, BackupService(paths))
            original = read_json(paths.master_settings)
            original_enables = [item["enable"] for item in original["accounts_urls"]]
            result = service.create_task(
                "A1,A3-A4,A8",
                task_earliest=EarliestRule.from_text("7"),
                persist_master_earliest=True,
                task_name="mixed",
                activate=True,
            )
            master = read_json(paths.master_settings)
            active = read_json(paths.active_settings)
            self.assertEqual(
                [item["enable"] for item in master["accounts_urls"]], original_enables
            )
            self.assertEqual(
                [item["enable"] for item in active["accounts_urls"]],
                [True, False, True, True, False, False, False, True],
            )
            for number in (1, 3, 4, 8):
                self.assertEqual(master["accounts_urls"][number - 1]["earliest"], 7)
                self.assertEqual(active["accounts_urls"][number - 1]["earliest"], 7)
            self.assertEqual(active["run_command"], "5 1 1 Q")
            self.assertTrue(result.task_path.is_file())
            self.assertTrue(result.backup_path and result.backup_path.is_dir())

    def test_generate_250_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 501)
            service = SettingsTaskService(paths, BackupService(paths))
            result = service.generate_batches(1, 501, 250)
            self.assertEqual(len(result), 3)
            self.assertEqual(result[0].preview.compact, "A1-A250")
            self.assertEqual(result[2].preview.compact, "A501")
            third = read_json(result[2].task_path)
            self.assertTrue(third["accounts_urls"][500]["enable"])
            self.assertFalse(third["accounts_urls"][499]["enable"])

    def test_same_task_name_never_overwrites_old_template(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 8)
            service = SettingsTaskService(paths, BackupService(paths))
            first = service.create_task("A1", task_name="keep-me")
            second = service.create_task("A2", task_name="keep-me")
            self.assertEqual(first.task_path.name, "keep-me.json")
            self.assertEqual(second.task_path.name, "keep-me_2.json")
            self.assertTrue(read_json(first.task_path)["accounts_urls"][0]["enable"])
            self.assertFalse(read_json(first.task_path)["accounts_urls"][1]["enable"])


if __name__ == "__main__":
    unittest.main()
