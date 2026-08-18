from __future__ import annotations

import ast
import hashlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from openpyxl import Workbook, load_workbook

from douk_manager.config import AppConfig
from douk_manager.controller import ControllerError, ManagerController
from douk_manager.core.backup import BackupService
from douk_manager.core.engine import EngineError, EngineService, _WindowsEngineMutex
from douk_manager.core.json_store import read_json, write_json_atomic
from douk_manager.core.settings_tasks import EarliestRule, SettingsTaskService
from douk_manager.vendor import collector_server
from tests.helpers import make_test_paths

try:
    from douk_manager import startup as startup_module
except ImportError:
    startup_module = None


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_controller(root: Path, *, engine_running: bool) -> ManagerController:
    paths = make_test_paths(root, account_count=3)
    controller = ManagerController.__new__(ManagerController)
    controller.paths = paths
    controller.config = SimpleNamespace(collector_port=18765)
    controller.logger = Mock()
    controller.startup_backup = paths.backups / "existing-startup-backup"
    controller.read_only_reason = ""
    controller._last_collector_running = False
    controller._last_engine_running = engine_running
    controller.engine = Mock()
    controller.engine.external_running.return_value = engine_running
    controller.collector = Mock()
    controller.collector.last_log_path = paths.logs / "Collector_test.log"
    controller.tasks = Mock()
    controller.backup = Mock()
    controller.engine_updates = Mock()
    return controller


def create_collector_workbook(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = collector_server.EXCEL_SHEET_NAME
    sheet["A2"] = "1.A1"
    sheet["B2"] = "existing111"
    sheet["D2"] = "https://www.douyin.com/user/existing"
    sheet["A3"] = "2.A2"
    sheet.merge_cells("B3:C3")
    sheet.merge_cells("D3:J3")
    workbook.save(path)
    workbook.close()


class ControllerRuntimeSafetyTests(unittest.TestCase):
    def _startup_state(self):
        self.assertIsNotNone(
            startup_module,
            "Phase 1 must provide the douk_manager.startup state contracts.",
        )
        assert startup_module is not None
        self.assertTrue(
            hasattr(startup_module, "StartupState"),
            "Phase 1 must expose douk_manager.startup.StartupState.",
        )
        return startup_module.StartupState

    def _degraded_controller(self, root: Path) -> ManagerController:
        state = self._startup_state()
        controller = make_controller(root, engine_running=False)
        controller.root = controller.paths.root
        controller.config = AppConfig(
            engine_exe=str(controller.paths.engine_exe),
            video_root=str(controller.paths.video_root),
            index_root=str(controller.paths.index_root),
        )
        controller.task_order = Mock()
        controller.screenshots = Mock()
        controller.indexer = Mock()
        controller.startup_state = state.DEGRADED_READ_ONLY
        controller.startup_generation = 4
        controller.read_only_reason = "synthetic startup safety failure"
        controller.collector.running = False
        controller.collector.health.return_value = False
        controller.engine.external_running.return_value = False
        return controller

    def test_degraded_read_only_rejects_every_dangerous_entry_point(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = self._degraded_controller(root)
            controller.config.screenshot_post_mode = "batch"
            blocked_operations = (
                lambda: controller.update_post_options({"cleanup_after_index": True}),
                lambda: controller.create_task(
                    "A1", EarliestRule.keep(), False, "blocked", True
                ),
                lambda: controller.delete_tasks((controller.paths.tasks / "task.json",)),
                lambda: controller.generate_batches(1, 3, 1, EarliestRule.keep()),
                lambda: controller.save_task_order([]),
                lambda: controller.restore_task_order(),
                lambda: controller.activate_task(controller.paths.tasks / "task.json"),
                lambda: controller.start_current_download(),
                lambda: controller.start_monitor(),
                lambda: controller.activate_and_start(controller.paths.tasks / "task.json"),
                lambda: controller.backup_now(),
                lambda: controller.apply_engine_update(root / "engine.zip"),
                lambda: controller.start_collector(),
                lambda: controller.migrate_collector(),
                lambda: controller.organize_screenshots(),
                lambda: controller.refresh_index(),
                lambda: controller.cleanup_index(),
                lambda: controller.cleanup_index_self_test(),
                lambda: controller.run_post_actions("batch"),
            )

            for operation in blocked_operations:
                with self.subTest(operation=operation):
                    with self.assertRaises(ControllerError):
                        operation()

    def test_degraded_read_only_accepts_path_reconfiguration_without_ready_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = self._degraded_controller(root)
            controller._build_services = Mock()
            controller.try_startup_backup = Mock(
                side_effect=AssertionError("reconfiguration must wait for a new startup result")
            )

            result = controller.reconfigure(
                {"engine_exe": str(root / "replacement" / "main.exe")}
            )

            self.assertEqual(result, "")
            self.assertNotEqual(controller.startup_state, self._startup_state().READY)
            self.assertIsNone(controller.startup_backup)
            controller._build_services.assert_called_once_with()
            controller.try_startup_backup.assert_not_called()

    def test_degraded_reconfiguration_rejects_non_path_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = self._degraded_controller(Path(directory))
            controller._build_services = Mock()
            original_port = controller.config.collector_port

            with self.assertRaises(ControllerError):
                controller.reconfigure({"collector_port": 19999})

            self.assertEqual(controller.config.collector_port, original_port)
            controller._build_services.assert_not_called()
            controller.collector.stop.assert_not_called()

    def test_degraded_path_reconfiguration_does_not_stop_managed_collector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = self._degraded_controller(root)
            controller._build_services = Mock()
            controller.collector.running = True
            controller.collector.process = object()

            with self.assertRaises(ControllerError):
                controller.reconfigure({"engine_exe": str(root / "replacement" / "main.exe")})

            controller.collector.stop.assert_not_called()
            controller._build_services.assert_not_called()

    def test_degraded_path_reconfiguration_skips_uncertain_process_probe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = self._degraded_controller(root)
            controller._build_services = Mock()
            controller.engine.external_running.return_value = True
            replacement = root / "replacement" / "main.exe"

            try:
                result = controller.reconfigure({"engine_exe": str(replacement)})
            except ControllerError as exc:
                self.fail(f"degraded path repair must not depend on process certainty: {exc}")

            self.assertEqual(result, "")
            self.assertEqual(controller.config.engine_exe, str(replacement))
            controller.engine.external_running.assert_not_called()
            controller._build_services.assert_called_once_with()

    def test_startup_check_is_single_flight_while_safety_checking(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = make_controller(Path(directory), engine_running=False)
            controller.startup_state = self._startup_state().BOOTSTRAPPING
            controller.startup_generation = 0

            self.assertTrue(controller.begin_startup_check(1))
            self.assertFalse(controller.begin_startup_check(2))
            self.assertEqual(controller.startup_generation, 1)
            self.assertEqual(controller.startup_state, self._startup_state().SAFETY_CHECKING)

    def test_runtime_stop_and_cancel_reject_non_operational_states(self) -> None:
        state = self._startup_state()
        for blocked_state in (
            state.BOOTSTRAPPING,
            state.SAFETY_CHECKING,
            state.DEGRADED_READ_ONLY,
        ):
            with self.subTest(state=blocked_state), tempfile.TemporaryDirectory() as directory:
                controller = make_controller(Path(directory), engine_running=False)
                controller.startup_state = blocked_state
                controller.engine.current = SimpleNamespace(mode="monitor")
                controller.collector.process = object()

                for operation in (
                    controller.stop_monitor,
                    controller.cancel_current_download,
                    controller.stop_collector,
                ):
                    with self.subTest(operation=operation.__name__):
                        with self.assertRaises(ControllerError):
                            operation()

    def test_runtime_stop_and_cancel_require_manager_owned_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = make_controller(Path(directory), engine_running=False)
            controller.startup_state = self._startup_state().READY
            controller.engine.current = None
            controller.collector.process = None

            for operation in (
                controller.stop_monitor,
                controller.cancel_current_download,
                controller.stop_collector,
            ):
                with self.subTest(operation=operation.__name__):
                    with self.assertRaises(ControllerError):
                        operation()

    def test_runtime_stop_and_cancel_allow_managed_work_when_ready_or_closing(self) -> None:
        state = self._startup_state()
        for allowed_state in (state.READY, state.CLOSING):
            with self.subTest(state=allowed_state), tempfile.TemporaryDirectory() as directory:
                controller = make_controller(Path(directory), engine_running=False)
                controller.startup_state = allowed_state
                controller.engine.current = SimpleNamespace(mode="monitor")
                controller.engine.stop_monitor.return_value = controller.paths.active_settings
                controller.engine.cancel_batch.return_value = controller.paths.logs / "task.log"
                controller.collector.process = object()

                controller.stop_monitor()
                controller.cancel_current_download()
                controller.stop_collector()

                controller.engine.stop_monitor.assert_called_once_with()
                controller.engine.cancel_batch.assert_called_once_with(None)
                controller.collector.stop.assert_called_once_with()

    def test_monitor_start_requires_successful_startup_backup_before_engine_launch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = make_controller(Path(directory), engine_running=False)
            controller.startup_backup = None
            controller.read_only_reason = "启动前备份失败：test failure"
            controller.try_startup_backup = Mock(return_value=controller.read_only_reason)
            controller.collector.health.return_value = False
            controller.collector.running = False
            active_before = controller.paths.active_settings.read_bytes()

            with self.assertRaisesRegex(ControllerError, "启动前备份失败"):
                controller.start_monitor()

            controller.engine.start_monitor.assert_not_called()
            self.assertEqual(controller.paths.active_settings.read_bytes(), active_before)

    def test_monitor_start_honors_existing_read_only_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = make_controller(Path(directory), engine_running=False)
            controller.read_only_reason = "只读保护仍然有效"
            controller.collector.health.return_value = False
            controller.collector.running = False
            active_before = controller.paths.active_settings.read_bytes()

            with self.assertRaisesRegex(ControllerError, "只读保护"):
                controller.start_monitor()

            controller.engine.start_monitor.assert_not_called()
            self.assertEqual(controller.paths.active_settings.read_bytes(), active_before)

    def test_downloader_running_allows_collector_start_without_touching_active_or_db(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = make_controller(Path(directory), engine_running=True)
            controller.startup_backup = None
            controller.read_only_reason = "检测到下载引擎正在运行，未执行启动前备份。"
            active_before = file_sha256(controller.paths.active_settings)
            database_before = file_sha256(controller.paths.database)

            result = controller.start_collector()

            controller.collector.start.assert_called_once_with()
            controller.engine.external_running.assert_called_once_with()
            self.assertEqual(result, controller.collector.last_log_path)
            self.assertTrue(controller._last_collector_running)
            self.assertEqual(file_sha256(controller.paths.active_settings), active_before)
            self.assertEqual(file_sha256(controller.paths.database), database_before)

    def test_downloader_running_allows_collector_stop_without_touching_active_or_db(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = make_controller(Path(directory), engine_running=True)
            controller._last_collector_running = True
            active_before = file_sha256(controller.paths.active_settings)
            database_before = file_sha256(controller.paths.database)

            controller.stop_collector()

            controller.collector.stop.assert_called_once_with()
            self.assertFalse(controller._last_collector_running)
            self.assertEqual(file_sha256(controller.paths.active_settings), active_before)
            self.assertEqual(file_sha256(controller.paths.database), database_before)

    def test_collector_start_still_requires_backup_when_downloader_is_not_running(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = make_controller(Path(directory), engine_running=False)
            controller.startup_backup = None
            controller.read_only_reason = "启动前备份失败：test failure"
            controller.try_startup_backup = Mock(return_value=controller.read_only_reason)

            with self.assertRaisesRegex(ControllerError, "启动前备份失败"):
                controller.start_collector()

            controller.collector.start.assert_not_called()

    def test_downloader_running_still_rejects_dangerous_controller_operations(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = make_controller(Path(directory), engine_running=True)
            blocked_operations = (
                lambda: controller.migrate_collector(),
                lambda: controller.create_task(
                    "A1", EarliestRule.keep(), False, "blocked", True
                ),
                lambda: controller.generate_batches(1, 3, 1, EarliestRule.keep()),
                lambda: controller.activate_task(controller.paths.tasks / "task.json"),
                lambda: controller.backup_now(),
                lambda: controller.apply_engine_update(Path(directory) / "engine.zip"),
                lambda: controller.reconfigure({"engine_exe": "other-main.exe"}),
                lambda: controller.start_current_download(),
            )

            for operation in blocked_operations:
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(ControllerError, "下载引擎正在运行"):
                        operation()

            controller.collector.migrate_old_data.assert_not_called()
            controller.tasks.create_task.assert_not_called()
            controller.tasks.generate_batches.assert_not_called()
            controller.tasks.activate_existing_task.assert_not_called()
            controller.backup.create_full_snapshot.assert_not_called()
            controller.engine_updates.apply.assert_not_called()
            controller.engine.start.assert_not_called()
            self.assertFalse(hasattr(controller, "restore_backup"))

    def test_summary_lifecycle_lease_blocks_dangerous_operations_but_not_collector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = make_controller(Path(directory), engine_running=False)
            controller._download_lifecycle_active = True

            blocked_operations = (
                lambda: controller.create_task(
                    "A1", EarliestRule.keep(), False, "blocked", True
                ),
                lambda: controller.generate_batches(1, 3, 1, EarliestRule.keep()),
                lambda: controller.activate_task(controller.paths.tasks / "task.json"),
                lambda: controller.backup_now(),
                lambda: controller.apply_engine_update(Path(directory) / "engine.zip"),
                lambda: controller.reconfigure({"engine_exe": "other-main.exe"}),
                lambda: controller.update_post_options({"cleanup_after_index": True}),
                lambda: controller.start_current_download(),
            )

            for operation in blocked_operations:
                with self.subTest(operation=operation):
                    with self.assertRaisesRegex(ControllerError, "账号结果汇总"):
                        operation()

            controller.start_collector()
            controller.stop_collector()
            controller.collector.start.assert_called_once_with()
            controller.collector.stop.assert_called_once_with()

    def test_reconfigure_can_repair_paths_without_an_existing_startup_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = make_controller(root, engine_running=False)
            controller.root = controller.paths.root
            controller.config = AppConfig(
                engine_exe=str(controller.paths.engine_exe),
                video_root=str(controller.paths.video_root),
                index_root=str(controller.paths.index_root),
            )
            controller.startup_backup = None
            controller.read_only_reason = "旧路径不完整。"
            controller.collector.running = False
            controller._build_services = Mock()
            controller.try_startup_backup = Mock(return_value="新路径仍待检查。")

            result = controller.reconfigure(
                {"engine_exe": str(root / "replacement" / "main.exe")}
            )

            self.assertEqual(result, "新路径仍待检查。")
            controller.try_startup_backup.assert_called_once_with()
            controller._build_services.assert_called_once_with()

    def test_queue_options_do_not_require_a_startup_backup_while_idle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            controller = make_controller(root, engine_running=False)
            controller.config = AppConfig(
                engine_exe=str(controller.paths.engine_exe),
                video_root=str(controller.paths.video_root),
                index_root=str(controller.paths.index_root),
            )
            controller.startup_backup = None
            controller.try_startup_backup = Mock(
                side_effect=AssertionError("queue options must not trigger a backup")
            )

            result = controller.update_post_options(
                {"cleanup_after_index": False}
            )

            self.assertTrue(result)
            self.assertFalse(controller.config.cleanup_after_index)
            controller.try_startup_backup.assert_not_called()

    def test_two_concurrent_engine_starts_launch_only_one_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory))
            active = read_json(paths.active_settings)
            active["run_command"] = "5 1 1 Q"
            write_json_atomic(paths.active_settings, active)
            config = SimpleNamespace(batch_accounts=50, rest_seconds=150)
            services = [
                EngineService(paths, config, Mock()) for _ in range(2)
            ]
            barrier = threading.Barrier(2)
            launched = threading.Event()
            checks = [0, 0]

            def checker(index: int):
                def external_running() -> bool:
                    checks[index] += 1
                    if checks[index] == 1:
                        barrier.wait(timeout=3)
                        return False
                    return launched.is_set()

                return external_running

            for index, service in enumerate(services):
                service.external_running = Mock(side_effect=checker(index))
                service.backup.create_critical_snapshot.return_value = (
                    paths.backups / "concurrent-start"
                )

            process = Mock()
            process.poll.return_value = None
            results: list[object] = []
            errors: list[BaseException] = []

            def run(service: EngineService) -> None:
                try:
                    results.append(service.start())
                except BaseException as exc:
                    errors.append(exc)

            def launch(*args, **kwargs):
                launched.set()
                return process

            threads = [threading.Thread(target=run, args=(service,)) for service in services]
            with patch("douk_manager.core.engine.subprocess.Popen", side_effect=launch) as popen:
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(timeout=5)

            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(len(results), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], EngineError)
            self.assertIn("已经在运行", str(errors[0]))
            self.assertEqual(popen.call_count, 1)

    @unittest.skipUnless(os.name == "nt", "Windows named mutex is required")
    def test_inherited_engine_mutex_blocks_until_child_process_exits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory))
            service = EngineService(paths, SimpleNamespace(), Mock())
            mutex = _WindowsEngineMutex.acquire(paths.engine_exe)
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(0.4)"],
                creationflags=subprocess.CREATE_NO_WINDOW,
                **mutex.popen_options(),
            )
            mutex.close()
            try:
                with patch("douk_manager.core.engine.subprocess.run") as wmi:
                    self.assertTrue(service.external_running())
                wmi.assert_not_called()
                with self.assertRaisesRegex(EngineError, "已经在运行"):
                    _WindowsEngineMutex.acquire(paths.engine_exe)
            finally:
                child.wait(timeout=3)

            released = _WindowsEngineMutex.acquire(paths.engine_exe)
            released.close()

    @unittest.skipUnless(os.name == "nt", "Windows named mutex is required")
    def test_failed_process_launch_releases_engine_mutex(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory))
            active = read_json(paths.active_settings)
            active["run_command"] = "5 1 1 Q"
            write_json_atomic(paths.active_settings, active)
            config = SimpleNamespace(batch_accounts=50, rest_seconds=150)
            service = EngineService(paths, config, Mock())
            service.external_running = Mock(return_value=False)
            service.backup.create_critical_snapshot.return_value = (
                paths.backups / "failed-start"
            )

            with patch(
                "douk_manager.core.engine.subprocess.Popen",
                side_effect=OSError("test launch failure"),
            ):
                with self.assertRaisesRegex(EngineError, "无法启动下载引擎"):
                    service.start()

            released = _WindowsEngineMutex.acquire(paths.engine_exe)
            released.close()

    def test_new_master_account_cannot_join_already_active_task_or_change_database(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), account_count=3)
            tasks = SettingsTaskService(paths, BackupService(paths))
            reusable = tasks.create_task("A1,A3", task_name="running-task")
            tasks.activate_existing_task(reusable.task_path)
            active_before = paths.active_settings.read_bytes()
            database_before = file_sha256(paths.database)

            master = read_json(paths.master_settings)
            master["accounts_urls"].append(
                {
                    "mark": "A4 newly collected",
                    "url": "https://www.douyin.com/user/new-account",
                    "tab": "post",
                    "earliest": "",
                    "latest": "",
                    "enable": True,
                }
            )
            write_json_atomic(paths.master_settings, master)

            self.assertEqual(paths.active_settings.read_bytes(), active_before)
            self.assertEqual(file_sha256(paths.database), database_before)
            with closing(
                sqlite3.connect(f"file:{paths.database.as_posix()}?mode=ro", uri=True)
            ) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM records").fetchone()[0],
                    "safe",
                )

            tasks.activate_existing_task(reusable.task_path)
            active_after = read_json(paths.active_settings)
            self.assertEqual(
                [account["enable"] for account in active_after["accounts_urls"]],
                [True, False, True, False],
            )

    def test_real_collector_add_changes_master_and_excel_but_not_active_or_database(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = make_test_paths(root, account_count=2)
            master = read_json(paths.master_settings)
            master["accounts_urls"] = [
                {
                    "mark": "A1existing111",
                    "url": "https://www.douyin.com/user/existing",
                    "tab": "post",
                    "earliest": "",
                    "latest": "",
                    "enable": True,
                },
                {
                    "mark": "",
                    "url": "",
                    "tab": "post",
                    "earliest": "",
                    "latest": "",
                    "enable": True,
                },
            ]
            write_json_atomic(paths.master_settings, master)
            create_collector_workbook(paths.collector_excel)
            active_before = file_sha256(paths.active_settings)
            database_before = file_sha256(paths.database)

            module_path = Path(collector_server.__file__).resolve()
            module_name = "douk_manager.vendor.collector_server_isolated_test"
            spec = importlib.util.spec_from_file_location(module_name, module_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader)
            isolated = importlib.util.module_from_spec(spec)
            environment = {
                "DOUK_COLLECTOR_DATA_DIR": str(paths.collector_data),
                "DOUK_MASTER_PATH": str(paths.master_settings),
                "DOUK_COLLECTOR_EXCEL": str(paths.collector_excel),
                "DOUK_SCREENSHOT_DIR": str(paths.screenshot_inbox),
                "DOUK_GLOBAL_LOCK_PATH": str(paths.lock_file),
            }
            with patch.dict("os.environ", environment, clear=False):
                sys.modules[module_name] = isolated
                try:
                    spec.loader.exec_module(isolated)
                finally:
                    sys.modules.pop(module_name, None)

                result = isolated.add_record(
                    {
                        "nickname": "new-account",
                        "douyin_id": "222",
                        "url": "https://www.douyin.com/user/new-user?from=search",
                    }
                )

            self.assertTrue(result["ok"])
            saved_master = read_json(paths.master_settings)
            self.assertEqual(saved_master["accounts_urls"][1]["mark"], "A2new-account222")
            self.assertEqual(
                saved_master["accounts_urls"][1]["url"],
                "https://www.douyin.com/user/new-user",
            )
            workbook = load_workbook(paths.collector_excel, keep_links=True)
            try:
                sheet = workbook[collector_server.EXCEL_SHEET_NAME]
                self.assertEqual(sheet["B3"].value, "new-account222")
                self.assertEqual(
                    sheet["D3"].value, "https://www.douyin.com/user/new-user"
                )
                self.assertIsNone(sheet["D3"].hyperlink)
            finally:
                workbook.close()
            self.assertEqual(file_sha256(paths.active_settings), active_before)
            self.assertEqual(file_sha256(paths.database), database_before)
            with closing(
                sqlite3.connect(f"file:{paths.database.as_posix()}?mode=ro", uri=True)
            ) as connection:
                self.assertEqual(
                    connection.execute("SELECT value FROM records").fetchone()[0],
                    "safe",
                )

    def test_collector_post_routes_use_shared_short_lived_file_lock(self) -> None:
        source_path = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "douk_manager"
            / "vendor"
            / "collector_server.py"
        )
        tree = ast.parse(source_path.read_text(encoding="utf-8"))
        do_post = next(
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef) and node.name == "do_POST"
        )
        lock_calls = [
            item.context_expr
            for item in ast.walk(do_post)
            if isinstance(item, ast.withitem)
            and isinstance(item.context_expr, ast.Call)
            and isinstance(item.context_expr.func, ast.Name)
            and item.context_expr.func.id == "critical_section"
        ]

        self.assertEqual(len(lock_calls), 1)
        lock_call = lock_calls[0]
        self.assertEqual(ast.unparse(lock_call.args[0]), "GLOBAL_LOCK_PATH")
        timeout = next(
            keyword.value for keyword in lock_call.keywords if keyword.arg == "timeout"
        )
        self.assertEqual(ast.literal_eval(timeout), 3.0)

    def test_collector_post_returns_busy_without_writes_when_shared_lock_is_held(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = make_test_paths(root, account_count=2)
            create_collector_workbook(paths.collector_excel)
            before = {
                "master": file_sha256(paths.master_settings),
                "excel": file_sha256(paths.collector_excel),
                "active": file_sha256(paths.active_settings),
                "database": file_sha256(paths.database),
            }
            ready_path = root / "lock-ready.txt"
            lock_script = (
                "import sys,time\n"
                "from pathlib import Path\n"
                "from douk_manager.core.locks import ProcessFileLock\n"
                "with ProcessFileLock(Path(sys.argv[1])):\n"
                "    Path(sys.argv[2]).write_text('ready', encoding='ascii')\n"
                "    time.sleep(6)\n"
            )
            environment = dict(os.environ)
            environment["PYTHONDONTWRITEBYTECODE"] = "1"
            environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
            holder = subprocess.Popen(
                [sys.executable, "-c", lock_script, str(paths.lock_file), str(ready_path)],
                env=environment,
            )
            try:
                deadline = time.monotonic() + 3
                while not ready_path.is_file() and time.monotonic() < deadline:
                    time.sleep(0.05)
                self.assertTrue(ready_path.is_file(), "lock holder did not become ready")

                module_path = Path(collector_server.__file__).resolve()
                module_name = "douk_manager.vendor.collector_server_lock_test"
                spec = importlib.util.spec_from_file_location(module_name, module_path)
                self.assertIsNotNone(spec)
                self.assertIsNotNone(spec.loader)
                isolated = importlib.util.module_from_spec(spec)
                module_environment = {
                    "DOUK_COLLECTOR_DATA_DIR": str(paths.collector_data),
                    "DOUK_MASTER_PATH": str(paths.master_settings),
                    "DOUK_COLLECTOR_EXCEL": str(paths.collector_excel),
                    "DOUK_SCREENSHOT_DIR": str(paths.screenshot_inbox),
                    "DOUK_GLOBAL_LOCK_PATH": str(paths.lock_file),
                }
                with patch.dict("os.environ", module_environment, clear=False):
                    sys.modules[module_name] = isolated
                    try:
                        spec.loader.exec_module(isolated)
                    finally:
                        sys.modules.pop(module_name, None)

                payload = json.dumps(
                    {
                        "client_version": isolated.VERSION,
                        "page_visible": True,
                        "nickname": "blocked",
                        "douyin_id": "333",
                        "url": "https://www.douyin.com/user/blocked",
                    }
                ).encode("utf-8")
                handler = object.__new__(isolated.Handler)
                handler.path = "/add"
                handler.headers = {
                    "X-DouK-Token": isolated.ACCESS_TOKEN,
                    "Content-Length": str(len(payload)),
                }
                handler.rfile = io.BytesIO(payload)
                responses: list[tuple[dict, int]] = []
                handler._send_json = lambda body, status=200: responses.append(
                    (body, status)
                )

                handler.do_POST()

                self.assertEqual(len(responses), 1)
                self.assertEqual(responses[0][1], 409)
                self.assertEqual(responses[0][0]["code"], "MANAGER_BUSY")
                self.assertEqual(
                    {
                        "master": file_sha256(paths.master_settings),
                        "excel": file_sha256(paths.collector_excel),
                        "active": file_sha256(paths.active_settings),
                        "database": file_sha256(paths.database),
                    },
                    before,
                )
            finally:
                if holder.poll() is None:
                    holder.terminate()
                holder.wait(timeout=3)


if __name__ == "__main__":
    unittest.main()
