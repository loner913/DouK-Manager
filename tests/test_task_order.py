from __future__ import annotations

import hashlib
import inspect
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt

from douk_manager.background import CancellationToken, TaskFailure, TaskState, TaskWorker
from douk_manager.core.backup import BackupService
from douk_manager.core.json_store import read_json
from douk_manager.core.settings_tasks import SettingsTaskService
from douk_manager.core import task_order as task_order_module
from douk_manager.core.task_order import (
    TaskOrderError,
    TaskOrderService,
    move_to_index,
    task_start_number,
)
from douk_manager.operation import OperationContext
from tests.helpers import make_test_paths


class TaskOrderTests(unittest.TestCase):
    @staticmethod
    def _create_two_tasks(root: Path) -> tuple[object, Path, Path]:
        paths = make_test_paths(root, 20)
        tasks = SettingsTaskService(paths, BackupService(paths))
        a1 = tasks.create_task("A1", task_name="A1").task_path
        a2 = tasks.create_task("A2", task_name="A2").task_path
        return paths, a1, a2

    @staticmethod
    def _run_worker(action, context: OperationContext) -> tuple[object, ...]:
        worker, settlements = TaskOrderTests._make_worker(action, context)

        worker.run()

        if len(settlements) != 1:
            raise AssertionError(f"expected one settlement, got {settlements!r}")
        return settlements[0]

    @staticmethod
    def _make_worker(action, context: OperationContext):
        settlements: list[tuple[object, ...]] = []
        worker = TaskWorker(
            "task-order-contract",
            1,
            action,
            CancellationToken(),
            operation_context=context,
        )
        worker.settled.connect(
            lambda *args: settlements.append(args),
            Qt.ConnectionType.DirectConnection,
        )
        return worker, settlements

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

    def test_duplicate_manual_order_is_rejected_without_changing_saved_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 20)
            tasks = SettingsTaskService(paths, BackupService(paths))
            a1 = tasks.create_task("A1", task_name="A1").task_path
            a2 = tasks.create_task("A2", task_name="A2").task_path
            order = TaskOrderService(paths)
            self.assertEqual(order.list_tasks(), (a1, a2))

            with self.assertRaises(TaskOrderError):
                order.save_manual_order((a2, a2))

            self.assertEqual(TaskOrderService(paths).list_tasks(), (a1, a2))

    def test_list_tasks_context_is_keyword_only_and_legacy_call_still_works(self) -> None:
        signature = inspect.signature(TaskOrderService.list_tasks)
        self.assertIn("context", signature.parameters)
        context_parameter = signature.parameters["context"]
        self.assertIs(context_parameter.kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertIsNone(context_parameter.default)

        with tempfile.TemporaryDirectory() as directory:
            paths, a1, a2 = self._create_two_tasks(Path(directory))
            service = TaskOrderService(paths)

            self.assertEqual(service.list_tasks(), (a1, a2))
            with self.assertRaises(TypeError):
                service.list_tasks(OperationContext())

    def test_cancel_before_glob_reads_or_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 20)
            service = TaskOrderService(paths)
            context = OperationContext()
            original_raise = context.raise_if_cancelled
            checkpoint_calls = 0

            def cancel_at_service_entry() -> None:
                nonlocal checkpoint_calls
                checkpoint_calls += 1
                if checkpoint_calls == 2:
                    self.assertTrue(context.request_cancel())
                original_raise()

            context.raise_if_cancelled = cancel_at_service_entry

            with (
                patch.object(Path, "glob") as glob,
                patch.object(task_order_module, "read_json") as read,
                patch.object(task_order_module, "write_json_atomic") as write,
            ):
                settlement = self._run_worker(
                    lambda worker_context: service.list_tasks(context=worker_context),
                    context,
                )

            self.assertIs(settlement[2], TaskState.CANCELLED)
            self.assertEqual(checkpoint_calls, 2)
            glob.assert_not_called()
            read.assert_not_called()
            write.assert_not_called()
            self.assertFalse(service.state_path.exists())

    def test_cancel_after_order_metadata_read_stops_before_template_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _a1, _a2 = self._create_two_tasks(Path(directory))
            service = TaskOrderService(paths)
            service.list_tasks()
            context = OperationContext()
            original_read = task_order_module.read_json
            metadata_reads: list[Path] = []
            template_reads: list[Path] = []

            def read_then_cancel(path: Path):
                document = original_read(path)
                if path == service.state_path:
                    metadata_reads.append(path)
                    self.assertTrue(context.request_cancel())
                elif path.parent == paths.tasks:
                    template_reads.append(path)
                return document

            with (
                patch.object(task_order_module, "read_json", side_effect=read_then_cancel),
                patch.object(task_order_module, "write_json_atomic") as write,
            ):
                settlement = self._run_worker(
                    lambda worker_context: service.list_tasks(context=worker_context),
                    context,
                )

            self.assertIs(settlement[2], TaskState.CANCELLED)
            self.assertEqual(metadata_reads, [service.state_path])
            self.assertEqual(template_reads, [])
            self.assertEqual(service.last_warning, "")
            write.assert_not_called()

    def test_cancel_after_first_template_preserves_order_file_and_stops_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _a1, _a2 = self._create_two_tasks(Path(directory))
            service = TaskOrderService(paths)
            service.list_tasks()
            before = service.state_path.read_bytes()
            before_hash = hashlib.sha256(before).hexdigest()
            tasks = SettingsTaskService(paths, BackupService(paths))
            tasks.create_task("A3", task_name="A3")
            tasks.create_task("A4", task_name="A4")
            context = OperationContext()
            original_read = task_order_module.read_json
            original_write = task_order_module.write_json_atomic
            template_reads: list[Path] = []

            def read_then_cancel(path: Path):
                document = original_read(path)
                if path.parent == paths.tasks:
                    template_reads.append(path)
                    if len(template_reads) == 1:
                        self.assertTrue(context.request_cancel())
                return document

            with (
                patch.object(task_order_module, "read_json", side_effect=read_then_cancel),
                patch.object(
                    task_order_module,
                    "write_json_atomic",
                    wraps=original_write,
                ) as write,
            ):
                settlement = self._run_worker(
                    lambda worker_context: service.list_tasks(context=worker_context),
                    context,
                )

            after = service.state_path.read_bytes()
            self.assertIs(settlement[2], TaskState.CANCELLED)
            self.assertEqual(len(template_reads), 1)
            write.assert_not_called()
            self.assertEqual(after, before)
            self.assertEqual(hashlib.sha256(after).hexdigest(), before_hash)

    def test_cancel_after_comparison_wins_before_atomic_write(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _a1, _a2 = self._create_two_tasks(Path(directory))
            service = TaskOrderService(paths)
            task_order_module.write_json_atomic(
                service.state_path,
                {"version": service.VERSION, "entries": []},
            )
            before = service.state_path.read_bytes()
            context = OperationContext()
            original_enter = context.enter_critical_phase
            original_read = task_order_module.read_json
            metadata_read_count = 0
            arbitration_calls = 0
            enter_reached = threading.Event()
            release_enter = threading.Event()

            def tracking_read(path: Path):
                nonlocal metadata_read_count
                document = original_read(path)
                if path == service.state_path:
                    metadata_read_count += 1
                return document

            def cancel_then_enter() -> None:
                nonlocal arbitration_calls
                arbitration_calls += 1
                self.assertEqual(metadata_read_count, 2)
                self.assertEqual(service.state_path.read_bytes(), before)
                enter_reached.set()
                if not release_enter.wait(timeout=3):
                    raise AssertionError("test did not release critical arbitration")
                original_enter()

            context.enter_critical_phase = cancel_then_enter
            worker, settlements = self._make_worker(
                lambda worker_context: service.list_tasks(context=worker_context),
                context,
            )
            with (
                patch.object(task_order_module, "read_json", side_effect=tracking_read),
                patch.object(task_order_module, "write_json_atomic") as write,
            ):
                thread = threading.Thread(target=worker.run)
                thread.start()
                try:
                    self.assertTrue(
                        enter_reached.wait(timeout=3),
                        "worker did not reach critical arbitration",
                    )
                    self.assertTrue(context.request_cancel())
                finally:
                    release_enter.set()
                thread.join(timeout=3)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(settlements), 1)
            settlement = settlements[0]
            self.assertEqual(arbitration_calls, 1)
            self.assertIs(settlement[2], TaskState.CANCELLED)
            self.assertFalse(context.critical_to_completion)
            write.assert_not_called()
            self.assertEqual(service.state_path.read_bytes(), before)

    def test_critical_first_rejects_late_cancel_and_real_atomic_write_succeeds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, a1, a2 = self._create_two_tasks(Path(directory))
            service = TaskOrderService(paths)
            task_order_module.write_json_atomic(
                service.state_path,
                {"version": service.VERSION, "entries": []},
            )
            context = OperationContext()
            original_write = task_order_module.write_json_atomic
            original_enter = context.enter_critical_phase
            critical_entered = threading.Event()
            release_write = threading.Event()

            def enter_then_wait() -> None:
                original_enter()
                critical_entered.set()
                if not release_write.wait(timeout=3):
                    raise AssertionError("test did not release atomic write")

            context.enter_critical_phase = enter_then_wait
            worker, settlements = self._make_worker(
                lambda worker_context: service.list_tasks(context=worker_context),
                context,
            )

            with patch.object(
                task_order_module,
                "write_json_atomic",
                wraps=original_write,
            ) as write:
                thread = threading.Thread(target=worker.run)
                thread.start()
                try:
                    self.assertTrue(
                        critical_entered.wait(timeout=3),
                        "worker did not enter the critical phase",
                    )
                    self.assertTrue(context.critical_to_completion)
                    self.assertFalse(context.request_cancel())
                finally:
                    release_write.set()
                thread.join(timeout=3)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(settlements), 1)
            settlement = settlements[0]
            self.assertIs(settlement[2], TaskState.SUCCEEDED)
            self.assertEqual(settlement[3], (a1, a2))
            self.assertTrue(context.critical_to_completion)
            self.assertTrue(context.terminal_sealed)
            write.assert_called_once()
            self.assertEqual(
                read_json(service.state_path),
                {
                    "version": service.VERSION,
                    "entries": [
                        {"name": a1.name, "slot": 1},
                        {"name": a2.name, "slot": 2},
                    ],
                },
            )

    def test_atomic_replace_failure_after_critical_is_failed_and_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _a1, _a2 = self._create_two_tasks(Path(directory))
            service = TaskOrderService(paths)
            task_order_module.write_json_atomic(
                service.state_path,
                {"version": service.VERSION, "entries": []},
            )
            before = service.state_path.read_bytes()
            before_hash = hashlib.sha256(before).hexdigest()
            context = OperationContext()
            late_cancel_results: list[bool] = []

            def fail_replace(_source: Path, _destination: Path) -> None:
                self.assertTrue(context.critical_to_completion)
                late_cancel_results.append(context.request_cancel())
                raise OSError("synthetic task-order replace failure")

            with patch(
                "douk_manager.core.json_store.os.replace",
                side_effect=fail_replace,
            ) as replace:
                settlement = self._run_worker(
                    lambda worker_context: service.list_tasks(context=worker_context),
                    context,
                )

            after = service.state_path.read_bytes()
            temporary_files = tuple(
                service.state_path.parent.glob(f".{service.state_path.name}.*.tmp")
            )
            self.assertIs(settlement[2], TaskState.FAILED)
            self.assertIsInstance(settlement[3], TaskFailure)
            self.assertEqual(settlement[3].error_type, "OSError")
            self.assertIn("synthetic task-order replace failure", settlement[3].message)
            self.assertEqual(late_cancel_results, [False])
            self.assertTrue(context.critical_to_completion)
            self.assertTrue(context.terminal_sealed)
            replace.assert_called_once()
            self.assertEqual(after, before)
            self.assertEqual(hashlib.sha256(after).hexdigest(), before_hash)
            self.assertEqual(temporary_files, ())

    def test_identical_normalized_content_never_writes_or_enters_critical(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, a1, a2 = self._create_two_tasks(Path(directory))
            service = TaskOrderService(paths)
            self.assertEqual(service.list_tasks(), (a1, a2))
            before = service.state_path.read_bytes()
            context = OperationContext()
            original_enter = context.enter_critical_phase
            context.enter_critical_phase = Mock(wraps=original_enter)

            with patch.object(task_order_module, "write_json_atomic") as write:
                settlement = self._run_worker(
                    lambda worker_context: service.list_tasks(context=worker_context),
                    context,
                )

            self.assertIs(settlement[2], TaskState.SUCCEEDED)
            self.assertEqual(settlement[3], (a1, a2))
            context.enter_critical_phase.assert_not_called()
            self.assertFalse(context.critical_to_completion)
            write.assert_not_called()
            self.assertEqual(service.state_path.read_bytes(), before)

    def test_saved_templates_have_two_scan_checkpoints_each(self) -> None:
        class CountingContext(OperationContext):
            def __init__(self) -> None:
                super().__init__()
                self.checkpoints = 0

            def raise_if_cancelled(self) -> None:
                self.checkpoints += 1
                super().raise_if_cancelled()

        with tempfile.TemporaryDirectory() as directory:
            paths, a1, a2 = self._create_two_tasks(Path(directory))
            service = TaskOrderService(paths)
            self.assertEqual(service.list_tasks(), (a1, a2))
            context = CountingContext()

            ordered = service.list_tasks(context=context)

            self.assertEqual(ordered, (a1, a2))
            non_template_checkpoints = 12
            self.assertEqual(
                context.checkpoints,
                non_template_checkpoints + (2 * len(ordered)),
            )


if __name__ == "__main__":
    unittest.main()
