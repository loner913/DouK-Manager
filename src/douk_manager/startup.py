from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from douk_manager.config import ManagedPaths
from douk_manager.core.backup import BackupService
from douk_manager.core.engine import (
    EngineService,
    ProcessProbe,
    ProcessProbeState,
)
from douk_manager.core.locks import critical_section


class StartupState(Enum):
    BOOTSTRAPPING = "BOOTSTRAPPING"
    SAFETY_CHECKING = "SAFETY_CHECKING"
    READY = "READY"
    DEGRADED_READ_ONLY = "DEGRADED_READ_ONLY"
    CLOSING = "CLOSING"


class StartupStage(Enum):
    PATHS = "PATHS"
    PROCESS = "PROCESS"
    LOCK = "LOCK"
    LIVE_DATA = "LIVE_DATA"
    BACKUP = "BACKUP"
    COMMAND_RECOVERY = "COMMAND_RECOVERY"
    SNAPSHOT = "SNAPSHOT"


@dataclass(frozen=True)
class StartupSafetyResult:
    generation: int
    success: bool
    state: StartupState
    stage: StartupStage
    summary: str
    details: str
    health: dict[str, Any]
    process_probe: ProcessProbe | None = None
    startup_backup: Path | None = None
    recovered_command: bool = False

    @property
    def health_snapshot(self) -> dict[str, Any]:
        return self.health


class StartupSafetyService:
    _REQUIRED_PATHS = (
        "engine_exe",
        "volume",
        "master_settings",
        "active_settings",
        "database",
    )

    def __init__(
        self,
        *,
        paths: ManagedPaths,
        engine: EngineService,
        backup: BackupService,
    ) -> None:
        self.paths = paths
        self.engine = engine
        self.backup = backup

    @staticmethod
    def _checkpoint(token: object | None) -> None:
        if token is None:
            return
        raise_if_cancelled = getattr(token, "raise_if_cancelled", None)
        if callable(raise_if_cancelled):
            raise_if_cancelled()
            return
        is_cancelled = getattr(token, "is_cancelled", None)
        if callable(is_cancelled) and is_cancelled():
            raise RuntimeError("startup safety check was cancelled")

    def _failure(
        self,
        generation: int,
        stage: StartupStage,
        summary: str,
        details: str,
        health: dict[str, Any],
        *,
        process_probe: ProcessProbe | None = None,
        startup_backup: Path | None = None,
    ) -> StartupSafetyResult:
        return StartupSafetyResult(
            generation=generation,
            success=False,
            state=StartupState.DEGRADED_READ_ONLY,
            stage=stage,
            summary=summary,
            details=details[:4000],
            health=dict(health),
            process_probe=process_probe,
            startup_backup=startup_backup,
        )

    def run(self, generation: int, token: object | None) -> StartupSafetyResult:
        self._checkpoint(token)
        try:
            health: dict[str, Any] = dict(self.paths.health())
        except Exception as exc:
            return self._failure(
                generation,
                StartupStage.PATHS,
                "无法检查正式路径。",
                f"path health check failed: {type(exc).__name__}: {exc}",
                {},
            )

        missing = [name for name in self._REQUIRED_PATHS if not health.get(name, False)]
        if missing:
            return self._failure(
                generation,
                StartupStage.PATHS,
                "下载引擎或唯一正式 Volume 尚未完整识别。",
                "missing required paths: " + ", ".join(missing),
                health,
            )

        self._checkpoint(token)
        try:
            process_probe = self.engine.probe_external_running()
        except Exception as exc:
            return self._failure(
                generation,
                StartupStage.PROCESS,
                "无法确认下载引擎是否正在运行。",
                f"process probe failed: {type(exc).__name__}: {exc}",
                health,
            )
        if process_probe.state is not ProcessProbeState.SAFE:
            summary = (
                "检测到下载引擎正在运行，未执行启动前备份。"
                if process_probe.state is ProcessProbeState.RUNNING
                else "无法可靠确认下载引擎状态，已进入只读保护。"
            )
            return self._failure(
                generation,
                StartupStage.PROCESS,
                summary,
                process_probe.details,
                health,
                process_probe=process_probe,
            )

        self._checkpoint(token)
        startup_backup: Path | None = None
        lock_entered = False
        try:
            with critical_section(self.paths.lock_file, timeout=5.0):
                lock_entered = True
                self._checkpoint(token)
                try:
                    process_probe = self.engine.probe_external_running()
                except Exception as exc:
                    return self._failure(
                        generation,
                        StartupStage.PROCESS,
                        "锁内复检无法确认下载引擎状态。",
                        f"locked process probe failed: {type(exc).__name__}: {exc}",
                        health,
                    )
                if process_probe.state is not ProcessProbeState.SAFE:
                    return self._failure(
                        generation,
                        StartupStage.PROCESS,
                        "锁内复检未确认安全，未执行启动前备份。",
                        process_probe.details,
                        health,
                        process_probe=process_probe,
                    )

                self._checkpoint(token)
                try:
                    self.backup.validate_live_data()
                except Exception as exc:
                    return self._failure(
                        generation,
                        StartupStage.LIVE_DATA,
                        "正式数据校验失败。",
                        f"live data validation failed: {type(exc).__name__}: {exc}",
                        health,
                        process_probe=process_probe,
                    )

                self._checkpoint(token)
                try:
                    startup_backup = self.backup.create_critical_snapshot(
                        "Startup",
                        {"operation": "application_start"},
                        keep_latest=3,
                    )
                except Exception as exc:
                    return self._failure(
                        generation,
                        StartupStage.BACKUP,
                        "启动前关键文件备份失败。",
                        f"backup failed: {type(exc).__name__}: {exc}",
                        health,
                        process_probe=process_probe,
                    )
        except Exception as exc:
            if lock_entered:
                raise
            return self._failure(
                generation,
                StartupStage.LOCK,
                "无法取得启动安全检查锁。",
                f"lock failed: {type(exc).__name__}: {exc}",
                health,
                process_probe=process_probe,
            )

        self._checkpoint(token)
        try:
            recovered_command = self.engine.recover_batch_command_if_idle()
        except Exception as exc:
            return self._failure(
                generation,
                StartupStage.COMMAND_RECOVERY,
                "启动备份完成，但运行命令恢复失败。",
                f"command recovery failed: {type(exc).__name__}: {exc}",
                health,
                process_probe=process_probe,
                startup_backup=startup_backup,
            )

        self._checkpoint(token)
        try:
            health = dict(self.paths.health())
        except Exception as exc:
            return self._failure(
                generation,
                StartupStage.SNAPSHOT,
                "无法构造启动状态快照。",
                f"startup snapshot failed: {type(exc).__name__}: {exc}",
                health,
                process_probe=process_probe,
                startup_backup=startup_backup,
            )
        return StartupSafetyResult(
            generation=generation,
            success=True,
            state=StartupState.READY,
            stage=StartupStage.SNAPSHOT,
            summary="启动安全检查完成。",
            details=f"startup backup: {startup_backup}",
            health=health,
            process_probe=process_probe,
            startup_backup=startup_backup,
            recovered_command=bool(recovered_command),
        )
