from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from douk_manager.core.backup import BackupService
from douk_manager.core.json_store import read_json
from douk_manager.core.settings_tasks import SettingsTaskService
from douk_manager.core.task_order import (
    TaskOrderService,
    move_to_index,
    task_start_number,
)
from tests.helpers import make_test_paths


class TaskOrderTests(unittest.TestCase):
    def test_natural_order_uses_first_enabled_a_number_not_filename_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 20)
            tasks = SettingsTaskService(paths, BackupService(paths))
            a10 = tasks.create_task("A10", task_name="排在后面的自定义名称").task_path
            a2 = tasks.create_task("A2", task_name="ZZZ").task_path

            ordered = TaskOrderService(paths).list_tasks()

            self.assertEqual(ordered, (a2, a10))
            self.assertEqual(task_start_number(a2), 2)
            self.assertEqual(task_start_number(a10), 10)

    def test_manual_slots_accept_new_tasks_at_their_a_positions(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 100)
            tasks = SettingsTaskService(paths, BackupService(paths))
            a51 = tasks.create_task("A51", task_name="A51").task_path
            a61 = tasks.create_task("A61", task_name="A61").task_path
            order = TaskOrderService(paths)
            self.assertEqual(order.list_tasks(), (a51, a61))

            order.save_manual_order((a61, a51))
            a71 = tasks.create_task("A71", task_name="A71").task_path
            a81 = tasks.create_task("A81", task_name="A81").task_path
            a55 = tasks.create_task("A55", task_name="A55").task_path
            a41 = tasks.create_task("A41", task_name="A41").task_path

            expected = (a41, a61, a55, a51, a71, a81)
            self.assertEqual(order.list_tasks(), expected)
            self.assertEqual(TaskOrderService(paths).list_tasks(), expected)

    def test_restore_natural_order_clears_manual_slots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 80)
            tasks = SettingsTaskService(paths, BackupService(paths))
            a51 = tasks.create_task("A51", task_name="A51").task_path
            a61 = tasks.create_task("A61", task_name="A61").task_path
            order = TaskOrderService(paths)
            order.list_tasks()
            order.save_manual_order((a61, a51))

            self.assertEqual(order.restore_natural_order(), (a51, a61))
            self.assertEqual(TaskOrderService(paths).list_tasks(), (a51, a61))

    def test_deleted_task_record_is_pruned_before_same_name_recreation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 80)
            tasks = SettingsTaskService(paths, BackupService(paths))
            a51 = tasks.create_task("A51", task_name="A51").task_path
            a61 = tasks.create_task("A61", task_name="A61").task_path
            order = TaskOrderService(paths)
            order.list_tasks()
            order.save_manual_order((a61, a51))

            a61.unlink()
            self.assertEqual(order.list_tasks(), (a51,))
            saved = read_json(order.state_path)
            self.assertEqual([entry["name"] for entry in saved["entries"]], ["A51.json"])

            recreated = tasks.create_task("A61", task_name="A61").task_path
            a55 = tasks.create_task("A55", task_name="A55").task_path
            self.assertEqual(order.list_tasks(), (a51, a55, recreated))

    def test_same_a_number_new_task_joins_existing_group_at_the_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 20)
            tasks = SettingsTaskService(paths, BackupService(paths))
            first = tasks.create_task("A10", task_name="first").task_path
            order = TaskOrderService(paths)
            self.assertEqual(order.list_tasks(), (first,))

            second = tasks.create_task("A10", task_name="aaa-new").task_path
            self.assertEqual(order.list_tasks(), (first, second))

    def test_move_to_exact_index_supports_dropdown_actions(self) -> None:
        paths = tuple(Path(f"A{number}.json") for number in (1, 2, 3, 4))
        self.assertEqual(
            move_to_index(paths, 2, 0),
            (Path("A3.json"), Path("A1.json"), Path("A2.json"), Path("A4.json")),
        )
        self.assertEqual(
            move_to_index(paths, 0, 3),
            (Path("A2.json"), Path("A3.json"), Path("A4.json"), Path("A1.json")),
        )


if __name__ == "__main__":
    unittest.main()
