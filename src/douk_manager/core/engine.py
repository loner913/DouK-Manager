from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from douk_manager.config import AppConfig, ManagedPaths
from douk_manager.core.backup import BackupService
from douk_manager.core.json_store import read_json
from douk_manager.core.locks import critical_section


class EngineError(RuntimeError):
    pass


@dataclass
class EngineRun:
    process: subprocess.Popen
    started_at: datetime
    task_log: Path

    @property
    def running(self) -> bool:
        return self.process.poll() is None


class EngineService:
    def __init__(
        self, paths: ManagedPaths, config: AppConfig, backup: BackupService
    ) -> None:
        self.paths = paths
        self.config = config
        self.backup = backup
        self.current: EngineRun | None = None

    def external_running(self) -> bool:
        if self.current and self.current.running:
            return True
        if os.name != "nt" or not self.paths.engine_exe.name:
            return False
        escaped_name = self.paths.engine_exe.name.replace("'", "''")
        command = (
            f"Get-CimInstance Win32_Process -Filter \"Name='{escaped_name}'\" | "
            "Select-Object -ExpandProperty ExecutablePath"
        )
        try:
            completed = subprocess.run(
                ["powershell.exe", "-NoProfile", "-Command", command],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        expected = os.path.normcase(os.path.normpath(str(self.paths.engine_exe)))
        return any(
            os.path.normcase(os.path.normpath(line.strip())) == expected
            for line in completed.stdout.splitlines()
            if line.strip()
        )

    def validate_ready(self) -> None:
        if not self.paths.engine_exe.is_file():
            raise EngineError(f"下载引擎不存在：{self.paths.engine_exe}")
        active = read_json(self.paths.active_settings)
        if active.get("run_command") != "5 1 1 Q":
            raise EngineError('正式 settings.json 的 run_command 必须为 "5 1 1 Q"。')
        accounts = active.get("accounts_urls")
        if not isinstance(accounts, list):
            raise EngineError("正式 settings.json 缺少 accounts_urls。")
        if not any(
            isinstance(account, dict)
            and account.get("enable", True)
            and str(account.get("url", "")).strip()
            for account in accounts
        ):
            raise EngineError("正式 settings.json 没有启用且 URL 有效的账号。")

    def start(self) -> EngineRun:
        if self.external_running():
            raise EngineError("下载引擎已经在运行。")
        with critical_section(self.paths.lock_file):
            self.validate_ready()
            snapshot = self.backup.create_snapshot(
                "BeforeDownload",
                {
                    "batch_accounts": self.config.batch_accounts,
                    "rest_seconds": self.config.rest_seconds,
                    "run_command": "5 1 1 Q",
                },
            )
        env = os.environ.copy()
        env["DOUK_ACCOUNT_BATCH_SIZE"] = str(self.config.batch_accounts)
        env["DOUK_ACCOUNT_REST_SECONDS"] = str(self.config.rest_seconds)
        env["DOUK_MANAGER_BACKUP"] = str(snapshot)
        creationflags = 0
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_CONSOLE
        try:
            process = subprocess.Popen(
                [str(self.paths.engine_exe)],
                cwd=str(self.paths.engine_root),
                env=env,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise EngineError(f"无法启动下载引擎：{exc}") from exc
        task_log = self.paths.logs / f"DownloadTask_{datetime.now():%Y-%m-%d_%H-%M-%S}.log"
        task_log.write_text(
            "\n".join(
                (
                    f"Started: {datetime.now().isoformat(timespec='seconds')}",
                    f"Engine: {self.paths.engine_exe}",
                    "run_command: 5 1 1 Q",
                    f"Batch accounts: {self.config.batch_accounts}",
                    f"Rest seconds: {self.config.rest_seconds}",
                    f"Backup: {snapshot}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        self.current = EngineRun(process, datetime.now(), task_log)
        return self.current
