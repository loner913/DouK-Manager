from __future__ import annotations

import json
import subprocess
import tempfile
from contextlib import contextmanager
from threading import Event
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from douk_manager.config import AppConfig
from douk_manager.core import engine as engine_module
from douk_manager.core.backup import BackupService
from douk_manager.core.engine import EngineService
from douk_manager.core.json_store import read_json
from douk_manager.core.watchlist import WatchlistService
from tests.helpers import make_test_paths

try:
    from douk_manager import startup as startup_module
except ImportError:
    startup_module = None


class ProcessProbeTests(unittest.TestCase):
    def _state(self):
        state = getattr(engine_module, "ProcessProbeState", None)
        self.assertIsNotNone(
            state,
            "Phase 1 must expose douk_manager.core.engine.ProcessProbeState.",
        )
        return state

    def _service(self, root: Path) -> EngineService:
        paths = make_test_paths(root)
        return EngineService(paths, SimpleNamespace(), BackupService(paths))

    def test_power_shell_timeout_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = self._state()
            service = self._service(Path(directory))

            with patch(
                "douk_manager.core.engine.subprocess.run",
                side_effect=subprocess.TimeoutExpired("powershell.exe", 8),
            ):
                probe = service.probe_external_running()

            self.assertEqual(probe.state, state.UNKNOWN)
            self.assertIn("timeout", probe.details.casefold())

    def test_nonzero_power_shell_exit_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = self._state()
            service = self._service(Path(directory))
            completed = SimpleNamespace(returncode=1, stdout="", stderr="access denied")

            with patch(
                "douk_manager.core.engine.subprocess.run", return_value=completed
            ):
                probe = service.probe_external_running()

            self.assertEqual(probe.state, state.UNKNOWN)

    def test_empty_power_shell_output_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = self._state()
            service = self._service(Path(directory))
            completed = SimpleNamespace(returncode=0, stdout="", stderr="")

            with patch(
                "douk_manager.core.engine._windows_engine_mutex_exists",
                return_value=False,
            ), patch(
                "douk_manager.core.engine.subprocess.run", return_value=completed
            ):
                probe = service.probe_external_running()

            self.assertEqual(probe.state, state.UNKNOWN)

    def test_structured_empty_process_list_is_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = self._state()
            service = self._service(Path(directory))
            completed = SimpleNamespace(returncode=0, stdout="[]", stderr="")

            with patch(
                "douk_manager.core.engine._windows_engine_mutex_exists",
                return_value=False,
            ), patch(
                "douk_manager.core.engine.subprocess.run", return_value=completed
            ):
                probe = service.probe_external_running()

            self.assertEqual(probe.state, state.SAFE)

    def test_process_with_unreadable_executable_path_is_unknown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = self._state()
            service = self._service(Path(directory))
            stdout = json.dumps([{"ProcessId": 42, "ExecutablePath": None}])
            completed = SimpleNamespace(returncode=0, stdout=stdout, stderr="")

            with patch(
                "douk_manager.core.engine._windows_engine_mutex_exists",
                return_value=False,
            ), patch(
                "douk_manager.core.engine.subprocess.run", return_value=completed
            ):
                probe = service.probe_external_running()

            self.assertEqual(probe.state, state.UNKNOWN)
            self.assertIn("path", probe.details.casefold())

    def test_structured_exact_executable_path_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            state = self._state()
            service = self._service(Path(directory))
            stdout = json.dumps(
                [{"ProcessId": 42, "ExecutablePath": str(service.paths.engine_exe)}]
            )
            completed = SimpleNamespace(returncode=0, stdout=stdout, stderr="")

            with patch(
                "douk_manager.core.engine._windows_engine_mutex_exists",
                return_value=False,
            ), patch(
                "douk_manager.core.engine.subprocess.run", return_value=completed
            ):
                probe = service.probe_external_running()

            self.assertEqual(probe.state, state.RUNNING)
            self.assertEqual(probe.matched_path, service.paths.engine_exe)


class StartupSafetyServiceTests(unittest.TestCase):
    def _api(self):
        self.assertIsNotNone(
            startup_module,
            "Phase 1 must provide the douk_manager.startup safety contracts.",
        )
        assert startup_module is not None
        for name in ("StartupSafetyService", "StartupSafetyResult", "StartupStage", "StartupState"):
            with self.subTest(interface=name):
                self.assertTrue(
                    hasattr(startup_module, name),
                    f"Phase 1 must expose douk_manager.startup.{name}.",
                )
        state = getattr(engine_module, "ProcessProbeState", None)
        self.assertIsNotNone(
            state,
            "Phase 1 must expose douk_manager.core.engine.ProcessProbeState.",
        )
        return startup_module, state

    def _service(
        self,
        root: Path,
        *,
        activation_context: object | None = None,
    ) -> tuple[object, Mock, Mock, object, object]:
        module, probe_state = self._api()
        paths = make_test_paths(root)
        engine = Mock(spec=EngineService)
        backup = Mock(spec=BackupService)
        backup.has_watchlist_startup_history.return_value = False
        engine.recover_batch_command_if_idle.return_value = False
        service = module.StartupSafetyService(
            paths=paths,
            engine=engine,
            backup=backup,
            activation_context=activation_context,
        )
        return service, engine, backup, probe_state, paths

    def _activation_context(
        self,
        *,
        config_existed: bool,
        persistent_state_existed: bool,
    ) -> object:
        module, _ = self._api()
        return module.WatchlistActivationContext(
            config_existed_at_boot=config_existed,
            persistent_state_existed_at_boot=persistent_state_existed,
        )

    @staticmethod
    def _probe(state: object, details: str = "synthetic process probe") -> object:
        return SimpleNamespace(state=state, details=details)

    def test_missing_critical_path_returns_paths_failure_without_backup(self) -> None:
        for path_name in (
            "engine_exe",
            "volume",
            "master_settings",
            "active_settings",
            "database",
        ):
            with self.subTest(path=path_name), tempfile.TemporaryDirectory() as directory:
                service, engine, backup, probe_state, paths = self._service(
                    Path(directory)
                )
                missing_path = getattr(paths, path_name)
                if path_name == "volume":
                    missing_path.rename(missing_path.with_name("missing-Volume"))
                else:
                    missing_path.unlink()
                engine.probe_external_running.return_value = self._probe(probe_state.SAFE)

                result = service.run(generation=7, token=None)

                self.assertFalse(result.success)
                self.assertEqual(
                    result.state, startup_module.StartupState.DEGRADED_READ_ONLY
                )
                self.assertEqual(result.stage, startup_module.StartupStage.PATHS)
                self.assertIsNone(result.startup_backup)
                backup.create_critical_snapshot.assert_not_called()

    def test_running_process_returns_process_failure_without_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, engine, backup, probe_state, _ = self._service(Path(directory))
            running_probe = self._probe(probe_state.RUNNING, "matching process")
            engine.probe_external_running.return_value = running_probe

            result = service.run(generation=8, token=None)

            self.assertFalse(result.success)
            self.assertEqual(result.state, startup_module.StartupState.DEGRADED_READ_ONLY)
            self.assertEqual(result.stage, startup_module.StartupStage.PROCESS)
            self.assertIs(result.process_probe, running_probe)
            backup.create_critical_snapshot.assert_not_called()

    def test_unknown_process_returns_process_failure_without_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, engine, backup, probe_state, _ = self._service(Path(directory))
            unknown_probe = self._probe(probe_state.UNKNOWN, "PowerShell timeout")
            engine.probe_external_running.return_value = unknown_probe

            result = service.run(generation=9, token=None)

            self.assertFalse(result.success)
            self.assertEqual(result.state, startup_module.StartupState.DEGRADED_READ_ONLY)
            self.assertEqual(result.stage, startup_module.StartupStage.PROCESS)
            self.assertIs(result.process_probe, unknown_probe)
            backup.create_critical_snapshot.assert_not_called()

    def test_live_data_failure_is_reported_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, engine, backup, probe_state, _ = self._service(Path(directory))
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)
            backup.validate_live_data.side_effect = OSError("synthetic SQLite failure")

            result = service.run(generation=10, token=None)

            self.assertFalse(result.success)
            self.assertEqual(result.state, startup_module.StartupState.DEGRADED_READ_ONLY)
            self.assertEqual(result.stage, startup_module.StartupStage.LIVE_DATA)
            self.assertIn("SQLite failure", result.details)
            backup.create_critical_snapshot.assert_not_called()

    def test_backup_failure_returns_backup_stage_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, engine, backup, probe_state, _ = self._service(Path(directory))
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)
            backup.create_critical_snapshot.side_effect = OSError("synthetic backup failure")

            result = service.run(generation=10, token=None)

            self.assertFalse(result.success)
            self.assertEqual(result.state, startup_module.StartupState.DEGRADED_READ_ONLY)
            self.assertEqual(result.stage, startup_module.StartupStage.BACKUP)
            self.assertIsNone(result.startup_backup)

    def test_successful_command_recovery_returns_ready_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, engine, backup, probe_state, paths = self._service(Path(directory))
            snapshot = paths.backups / "Startup-synthetic"
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)
            engine.recover_batch_command_if_idle.return_value = True
            backup.create_critical_snapshot.return_value = snapshot

            result = service.run(generation=11, token=None)

            self.assertTrue(result.success)
            self.assertEqual(result.state, startup_module.StartupState.READY)
            self.assertEqual(result.stage, startup_module.StartupStage.SNAPSHOT)
            self.assertEqual(result.startup_backup, snapshot)
            self.assertTrue(result.recovered_command)

    def test_success_uses_health_snapshot_taken_after_command_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, engine, backup, probe_state, paths = self._service(Path(directory))
            snapshot = paths.backups / "Startup-synthetic"
            initial_health = paths.health()
            final_health = {**initial_health, "collector_excel": True}
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)
            backup.create_critical_snapshot.return_value = snapshot

            with patch.object(
                type(paths), "health", side_effect=(initial_health, final_health)
            ) as health:
                result = service.run(generation=12, token=None)

            self.assertTrue(result.success)
            self.assertEqual(result.stage, startup_module.StartupStage.SNAPSHOT)
            self.assertEqual(result.health_snapshot, final_health)
            self.assertEqual(health.call_count, 2)

    def test_configured_watchlist_uses_startup_six_file_snapshot_and_reopens_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, engine, backup, probe_state, paths = self._service(Path(directory))
            watchlist = WatchlistService(paths)
            watchlist.initialize(initialization_evidence=True)
            watchlist.mark_ready()
            snapshot = paths.backups / "Startup-synthetic-v3"
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)
            backup.create_startup_snapshot.return_value = snapshot

            result = service.run(generation=14, token=None)

            self.assertTrue(result.success)
            backup.create_startup_snapshot.assert_called_once_with(
                "Startup",
                {"operation": "application_start"},
                keep_latest=BackupService.STARTUP_KEEP_LATEST,
            )
            backup.create_critical_snapshot.assert_not_called()
            self.assertEqual(read_json(paths.watchlist_control)["write_gate"], "ready")
            self.assertEqual(
                AppConfig.load(paths.config_file).watchlist_installation_id,
                read_json(paths.watchlist_control)["installation_id"],
            )

    def test_configured_watchlist_backup_failure_leaves_gate_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, engine, backup, probe_state, paths = self._service(Path(directory))
            watchlist = WatchlistService(paths)
            watchlist.initialize(initialization_evidence=True)
            watchlist.mark_ready()
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)
            backup.create_startup_snapshot.side_effect = OSError("synthetic snapshot failure")

            result = service.run(generation=15, token=None)

            self.assertFalse(result.success)
            self.assertEqual(result.stage, startup_module.StartupStage.BACKUP)
            self.assertEqual(read_json(paths.watchlist_control)["write_gate"], "blocked")

    def test_fresh_runtime_initializes_before_first_six_file_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = self._activation_context(
                config_existed=False,
                persistent_state_existed=False,
            )
            service, engine, backup, probe_state, paths = self._service(
                Path(directory),
                activation_context=context,
            )
            snapshot = paths.backups / "Startup-first-activation"
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)
            backup.create_startup_snapshot.return_value = snapshot

            result = service.run(generation=16, token=None)

            self.assertTrue(result.success)
            self.assertEqual(result.startup_backup, snapshot)
            backup.create_startup_snapshot.assert_called_once_with(
                "Startup",
                {"operation": "application_start"},
                keep_latest=BackupService.STARTUP_KEEP_LATEST,
            )
            backup.create_critical_snapshot.assert_not_called()
            document = read_json(paths.watchlist)
            watermark = read_json(paths.watchlist_w_watermark)
            control = read_json(paths.watchlist_control)
            self.assertEqual(document["next_w_id"], 1)
            self.assertEqual(watermark["next_w_id"], 1)
            self.assertEqual(control["initialization_state"], "complete")
            self.assertEqual(control["write_gate"], "ready")
            self.assertEqual(result.watchlist_installation_id, control["installation_id"])
            self.assertEqual(
                AppConfig.load(paths.config_file).watchlist_installation_id,
                control["installation_id"],
            )

    def test_legacy_config_is_trusted_once_for_first_activation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = self._activation_context(
                config_existed=True,
                persistent_state_existed=True,
            )
            service, engine, backup, probe_state, paths = self._service(
                Path(directory),
                activation_context=context,
            )
            AppConfig(
                engine_exe=str(paths.engine_exe),
                video_root=str(paths.video_root),
                index_root=str(paths.index_root),
            ).save(paths.config_file)
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)
            backup.create_startup_snapshot.return_value = paths.backups / "legacy-upgrade"

            result = service.run(generation=17, token=None)

            self.assertTrue(result.success)
            self.assertTrue(WatchlistService(paths).is_initialized())

    def test_ambiguous_preexisting_runtime_does_not_initialize(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = self._activation_context(
                config_existed=False,
                persistent_state_existed=True,
            )
            service, engine, backup, probe_state, paths = self._service(
                Path(directory),
                activation_context=context,
            )
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)

            result = service.run(generation=18, token=None)

            self.assertFalse(result.success)
            self.assertEqual(result.stage, startup_module.StartupStage.BACKUP)
            self.assertIn("无法证明", result.details)
            self.assertFalse(WatchlistService(paths).is_initialized())
            backup.create_startup_snapshot.assert_not_called()
            backup.create_critical_snapshot.assert_not_called()

    def test_recorded_installation_id_prevents_reset_after_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = self._activation_context(
                config_existed=True,
                persistent_state_existed=True,
            )
            service, engine, backup, probe_state, paths = self._service(
                Path(directory),
                activation_context=context,
            )
            AppConfig(
                engine_exe=str(paths.engine_exe),
                video_root=str(paths.video_root),
                index_root=str(paths.index_root),
                watchlist_installation_id="synthetic-prior-installation",
            ).save(paths.config_file)
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)

            result = service.run(generation=19, token=None)

            self.assertFalse(result.success)
            self.assertIn("禁止重新初始化", result.details)
            self.assertFalse(WatchlistService(paths).is_initialized())
            backup.create_startup_snapshot.assert_not_called()

    def test_startup_history_prevents_reset_when_live_observation_files_are_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = self._activation_context(
                config_existed=False,
                persistent_state_existed=False,
            )
            service, engine, backup, probe_state, paths = self._service(
                Path(directory),
                activation_context=context,
            )
            backup.has_watchlist_startup_history.return_value = True
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)

            result = service.run(generation=20, token=None)

            self.assertFalse(result.success)
            self.assertIn("Startup 历史", result.details)
            self.assertFalse(WatchlistService(paths).is_initialized())
            backup.create_startup_snapshot.assert_not_called()

    def test_partial_observation_state_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = self._activation_context(
                config_existed=True,
                persistent_state_existed=True,
            )
            service, engine, backup, probe_state, paths = self._service(
                Path(directory),
                activation_context=context,
            )
            original = b'{"synthetic":"partial"}\n'
            paths.watchlist.write_bytes(original)
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)

            result = service.run(generation=21, token=None)

            self.assertFalse(result.success)
            self.assertIn("文件不完整", result.details)
            self.assertEqual(paths.watchlist.read_bytes(), original)
            self.assertFalse(paths.watchlist_w_watermark.exists())
            self.assertFalse(paths.watchlist_control.exists())
            backup.create_startup_snapshot.assert_not_called()

    def test_first_snapshot_failure_keeps_complete_data_blocked_without_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            context = self._activation_context(
                config_existed=False,
                persistent_state_existed=False,
            )
            service, engine, backup, probe_state, paths = self._service(
                Path(directory),
                activation_context=context,
            )
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)
            backup.create_startup_snapshot.side_effect = OSError("synthetic first snapshot failure")

            result = service.run(generation=22, token=None)

            self.assertFalse(result.success)
            control = read_json(paths.watchlist_control)
            self.assertEqual(control["initialization_state"], "complete")
            self.assertEqual(control["write_gate"], "blocked")
            self.assertEqual(
                AppConfig.load(paths.config_file).watchlist_installation_id,
                "",
            )

    def test_real_first_activation_snapshot_restart_and_loss_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            module, probe_state = self._api()
            paths = make_test_paths(Path(directory))
            engine = Mock(spec=EngineService)
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)
            engine.recover_batch_command_if_idle.return_value = False
            backup = BackupService(paths)
            fresh = self._activation_context(
                config_existed=False,
                persistent_state_existed=False,
            )

            first = module.StartupSafetyService(
                paths=paths,
                engine=engine,
                backup=backup,
                activation_context=fresh,
            ).run(generation=23, token=None)

            self.assertTrue(first.success)
            first_control = read_json(first.startup_backup / "Data" / "watchlist_control.json")
            live_control = read_json(paths.watchlist_control)
            self.assertEqual(first_control["initialization_state"], "complete")
            self.assertEqual(first_control["write_gate"], "blocked")
            self.assertEqual(live_control["write_gate"], "ready")
            installation_id = live_control["installation_id"]
            self.assertEqual(
                AppConfig.load(paths.config_file).watchlist_installation_id,
                installation_id,
            )

            existing = self._activation_context(
                config_existed=True,
                persistent_state_existed=True,
            )
            second = module.StartupSafetyService(
                paths=paths,
                engine=engine,
                backup=backup,
                activation_context=existing,
            ).run(generation=24, token=None)
            self.assertTrue(second.success)
            self.assertEqual(
                read_json(paths.watchlist_control)["installation_id"],
                installation_id,
            )

            for path in (
                paths.watchlist,
                paths.watchlist_w_watermark,
                paths.watchlist_control,
            ):
                path.unlink()
            snapshots_before = {
                item.name
                for item in (paths.backups / "Startup").iterdir()
                if item.is_dir()
            }
            lost = module.StartupSafetyService(
                paths=paths,
                engine=engine,
                backup=backup,
                activation_context=existing,
            ).run(generation=25, token=None)
            self.assertFalse(lost.success)
            self.assertIn("禁止重新初始化", lost.details)
            self.assertFalse(WatchlistService(paths).is_initialized())
            self.assertEqual(
                snapshots_before,
                {
                    item.name
                    for item in (paths.backups / "Startup").iterdir()
                    if item.is_dir()
                },
            )

    def test_lock_recheck_stops_backup_when_process_becomes_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, engine, backup, probe_state, _ = self._service(Path(directory))
            lock_held = Event()
            probe_count = 0

            @contextmanager
            def synthetic_critical_section(*_args, **_kwargs):
                self.assertFalse(lock_held.is_set(), "startup safety must acquire one lock")
                lock_held.set()
                try:
                    yield
                finally:
                    lock_held.clear()

            def probe_external_running():
                nonlocal probe_count
                probe_count += 1
                if probe_count == 1:
                    self.assertFalse(
                        lock_held.is_set(), "the initial process probe precedes lock entry"
                    )
                    return self._probe(probe_state.SAFE, "before lock")
                self.assertEqual(probe_count, 2, "startup safety must probe exactly twice")
                self.assertTrue(
                    lock_held.is_set(),
                    "the second process probe must occur while critical_section is held",
                )
                return self._probe(probe_state.RUNNING, "inside lock")

            engine.probe_external_running.side_effect = probe_external_running
            with patch(
                "douk_manager.startup.critical_section",
                side_effect=synthetic_critical_section,
            ):
                result = service.run(generation=12, token=None)

            self.assertFalse(result.success)
            self.assertEqual(result.state, startup_module.StartupState.DEGRADED_READ_ONLY)
            self.assertEqual(result.stage, startup_module.StartupStage.PROCESS)
            self.assertEqual(probe_count, 2)
            self.assertFalse(lock_held.is_set(), "startup safety must release the lock")
            backup.create_critical_snapshot.assert_not_called()

    def test_raise_only_token_cancellation_inside_lock_propagates(self) -> None:
        class SyntheticCancellation(RuntimeError):
            pass

        class RaiseOnlyToken:
            def __init__(self) -> None:
                self.checkpoints = 0

            def raise_if_cancelled(self) -> None:
                self.checkpoints += 1
                if self.checkpoints == 4:
                    raise SyntheticCancellation("cancelled inside lock")

        with tempfile.TemporaryDirectory() as directory:
            service, engine, backup, probe_state, _ = self._service(Path(directory))
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)
            token = RaiseOnlyToken()

            with self.assertRaises(SyntheticCancellation):
                service.run(generation=13, token=token)

            self.assertEqual(token.checkpoints, 4)
            backup.validate_live_data.assert_not_called()
            backup.create_critical_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
