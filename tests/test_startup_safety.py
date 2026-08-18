from __future__ import annotations

import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from douk_manager.core import engine as engine_module
from douk_manager.core.backup import BackupService
from douk_manager.core.engine import EngineService
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
        self, root: Path
    ) -> tuple[object, Mock, Mock, object, object]:
        module, probe_state = self._api()
        paths = make_test_paths(root)
        engine = Mock(spec=EngineService)
        backup = Mock(spec=BackupService)
        engine.recover_batch_command_if_idle.return_value = False
        service = module.StartupSafetyService(
            paths=paths,
            engine=engine,
            backup=backup,
        )
        return service, engine, backup, probe_state, paths

    @staticmethod
    def _probe(state: object, details: str = "synthetic process probe") -> object:
        return SimpleNamespace(state=state, details=details)

    def test_missing_critical_path_returns_paths_failure_without_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, engine, backup, probe_state, paths = self._service(Path(directory))
            paths.database.unlink()
            engine.probe_external_running.return_value = self._probe(probe_state.SAFE)

            result = service.run(generation=7, token=None)

            self.assertFalse(result.success)
            self.assertEqual(result.state, startup_module.StartupState.DEGRADED_READ_ONLY)
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

    def test_lock_recheck_stops_backup_when_process_becomes_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service, engine, backup, probe_state, _ = self._service(Path(directory))
            engine.probe_external_running.side_effect = (
                self._probe(probe_state.SAFE, "before lock"),
                self._probe(probe_state.RUNNING, "inside lock"),
            )

            result = service.run(generation=12, token=None)

            self.assertFalse(result.success)
            self.assertEqual(result.state, startup_module.StartupState.DEGRADED_READ_ONLY)
            self.assertEqual(result.stage, startup_module.StartupStage.PROCESS)
            self.assertEqual(engine.probe_external_running.call_count, 2)
            backup.create_critical_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
