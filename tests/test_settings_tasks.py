from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from douk_manager.core.backup import BackupService
from douk_manager.core.json_store import read_json, write_json_atomic
from douk_manager.core.settings_tasks import (
    ActivatedTask,
    EarliestRule,
    SettingsTaskError,
    SettingsTaskService,
)
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
                [True, False, True, False, False, False, False, False],
            )
            for number in (1, 3):
                self.assertEqual(master["accounts_urls"][number - 1]["earliest"], 7)
                self.assertEqual(active["accounts_urls"][number - 1]["earliest"], 7)
            self.assertEqual(active["run_command"], "5 1 1 Q")
            self.assertTrue(result.task_path.is_file())
            self.assertTrue(result.backup_path and result.backup_path.is_dir())

    def test_generate_250_batches(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 501)
            service = SettingsTaskService(paths, BackupService(paths))
            master = read_json(paths.master_settings)
            for account in master["accounts_urls"]:
                account["enable"] = True
            write_json_atomic(paths.master_settings, master)
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
            second = service.create_task("A3", task_name="keep-me")
            self.assertEqual(first.task_path.name, "keep-me.json")
            self.assertEqual(second.task_path.name, "keep-me_2.json")
            self.assertTrue(read_json(first.task_path)["accounts_urls"][0]["enable"])
            self.assertFalse(read_json(first.task_path)["accounts_urls"][1]["enable"])

    def test_chinese_task_name_is_preserved_and_windows_characters_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 8)
            service = SettingsTaskService(paths, BackupService(paths))
            readable = service.create_task("A1", task_name="A1331_全流程测试")
            invalid = service.create_task("A3", task_name='测试<>:"/\\|?*名称')
            reserved = service.create_task("A5", task_name="CON")
            self.assertEqual(readable.task_path.name, "A1331_全流程测试.json")
            self.assertEqual(invalid.task_path.name, "测试_名称.json")
            self.assertEqual(reserved.task_path.name, "Task_CON.json")

    def test_old_task_activation_uses_latest_master_identity_and_disables_new_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 3)
            service = SettingsTaskService(paths, BackupService(paths))
            generated = service.create_task(
                "A1,A3",
                task_earliest=EarliestRule.from_text("30"),
                task_name="reusable",
            )

            master = read_json(paths.master_settings)
            master["cookie"] = "latest cookie"
            master["accounts_urls"][0]["mark"] = "A1 corrected mark"
            master["accounts_urls"][0]["url"] = "https://www.douyin.com/user/corrected"
            master["accounts_urls"][1]["mark"] = "A2 latest mark"
            new_account = dict(master["accounts_urls"][2])
            new_account.update(
                {
                    "mark": "A4 newly collected",
                    "url": "https://www.douyin.com/user/new-account",
                    "enable": True,
                    "earliest": "latest-master-value",
                }
            )
            master["accounts_urls"].append(new_account)
            write_json_atomic(paths.master_settings, master)

            service.activate_existing_task(generated.task_path)
            active = read_json(paths.active_settings)
            accounts = active["accounts_urls"]
            self.assertEqual(active["cookie"], "latest cookie")
            self.assertEqual(accounts[0]["mark"], "A1 corrected mark")
            self.assertEqual(
                accounts[0]["url"], "https://www.douyin.com/user/corrected"
            )
            self.assertEqual(accounts[1]["mark"], "A2 latest mark")
            self.assertEqual(
                [account["enable"] for account in accounts],
                [True, False, True, False],
            )
            self.assertEqual(accounts[0]["earliest"], 30)
            self.assertEqual(accounts[2]["earliest"], 30)
            self.assertEqual(accounts[3]["earliest"], "latest-master-value")
            self.assertEqual(active["run_command"], "5 1 1 Q")

    def test_old_template_cannot_reenable_master_tombstone(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 5)
            service = SettingsTaskService(paths, BackupService(paths))
            generated = service.create_task("A1,A5", task_name="before-tombstone")
            master_before = read_json(paths.master_settings)
            master_after = read_json(paths.master_settings)
            master_after["accounts_urls"][4]["enable"] = False
            write_json_atomic(paths.master_settings, master_after)

            result = service.activate_existing_task(generated.task_path)

            self.assertIsInstance(result, ActivatedTask)
            self.assertEqual(result.vetoed_numbers, (5,))
            self.assertEqual(
                [item["enable"] for item in read_json(paths.active_settings)["accounts_urls"]],
                [True, False, False, False, False],
            )
            persisted_master = read_json(paths.master_settings)
            self.assertEqual(persisted_master, master_after)
            self.assertEqual(
                [
                    {key: value for key, value in current.items() if key != "enable"}
                    for current in persisted_master["accounts_urls"]
                ],
                [
                    {key: value for key, value in current.items() if key != "enable"}
                    for current in master_before["accounts_urls"]
                ],
            )

    def test_all_template_accounts_vetoed_has_specific_error_and_no_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 3)
            service = SettingsTaskService(paths, BackupService(paths))
            generated = service.create_task("A1", task_name="all-vetoed")
            master = read_json(paths.master_settings)
            master["accounts_urls"][0]["enable"] = False
            write_json_atomic(paths.master_settings, master)
            active_before = paths.active_settings.read_bytes()

            with self.assertRaisesRegex(
                SettingsTaskError, "模板内账号均被主档永久停用"
            ):
                service.activate_existing_task(generated.task_path)

            self.assertEqual(paths.active_settings.read_bytes(), active_before)
            self.assertFalse((paths.backups / "BeforeChange").exists())

    def test_activation_veto_result_contains_only_a_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 5)
            service = SettingsTaskService(paths, BackupService(paths))
            generated = service.create_task("A1,A3,A5", task_name="veto-list")
            master = read_json(paths.master_settings)
            master["accounts_urls"][2]["enable"] = False
            master["accounts_urls"][4]["enable"] = False
            write_json_atomic(paths.master_settings, master)

            result = service.activate_existing_task(generated.task_path)

            self.assertEqual(result.vetoed_numbers, (3, 5))
            self.assertTrue(all(isinstance(number, int) for number in result.vetoed_numbers))

    def test_new_task_filters_master_tombstones_before_template_and_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 3)
            service = SettingsTaskService(paths, BackupService(paths))

            result = service.create_task("A1,A2", task_name="and-rule", activate=True)

            self.assertEqual(result.preview.selection.numbers, (1,))
            template = read_json(result.task_path)
            active = read_json(paths.active_settings)
            self.assertEqual(
                [item["enable"] for item in template["accounts_urls"]],
                [True, False, False],
            )
            self.assertEqual(
                [item["enable"] for item in active["accounts_urls"]],
                [True, False, False],
            )

    def test_new_task_rejects_selection_containing_only_master_tombstones(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 3)
            service = SettingsTaskService(paths, BackupService(paths))

            with self.assertRaisesRegex(SettingsTaskError, "所选账号均被主档永久停用"):
                service.create_task("A2", task_name="blocked")

            self.assertFalse((paths.tasks / "blocked.json").exists())


if __name__ == "__main__":
    unittest.main()
