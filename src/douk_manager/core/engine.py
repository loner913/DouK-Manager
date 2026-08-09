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
    pause_after_exit: bool = False

    @property
    def running(self) -> bool:
        return self.process.poll() is None


@dataclass(frozen=True)
class EngineExitAssessment:
    """Describe process completion without claiming that downloads succeeded."""

    exit_code: int | None
    normal_exit: bool
    headline: str
    detail: str
    log_status: str


def assess_process_exit(exit_code: int | None) -> EngineExitAssessment:
    """Translate an engine exit code into an honest, user-facing result.

    DouK-Downloader can finish with exit code 0 even when an individual
    account request fails (for example, an HTTP 403 handled by the engine).
    Exit code 0 therefore confirms only that the process ended normally.
    """

    if exit_code == 0:
        return EngineExitAssessment(
            exit_code=exit_code,
            normal_exit=True,
            headline="下载进程已正常结束，退出码=0。",
            detail=(
                "这只表示下载器进程正常退出；不代表所有账号均获取或下载成功，"
                "请结合下载器窗口及原生日志确认结果。"
            ),
            log_status="normal_exit_download_result_unverified",
        )
    return EngineExitAssessment(
        exit_code=exit_code,
        normal_exit=False,
        headline=f"下载进程异常结束，退出码={exit_code}。",
        detail="队列已停止；剩余任务不会启动，请先检查下载器窗口和日志。",
        log_status="abnormal_exit",
    )


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

    def start(self, *, pause_after_exit: bool = False) -> EngineRun:
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
                    "pause_after_exit": pause_after_exit,
                },
            )
        env = os.environ.copy()
        env["DOUK_ACCOUNT_BATCH_SIZE"] = str(self.config.batch_accounts)
        env["DOUK_ACCOUNT_REST_SECONDS"] = str(self.config.rest_seconds)
        env["DOUK_MANAGER_BACKUP"] = str(snapshot)
        creationflags = 0
        command = [str(self.paths.engine_exe)]
        if os.name == "nt":
            creationflags = subprocess.CREATE_NEW_CONSOLE
            if pause_after_exit:
                command = [
                    "cmd.exe",
                    "/d",
                    "/c",
                    str(self._write_pause_wrapper()),
                ]
        try:
            process = subprocess.Popen(
                command,
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
                    f"Pause after exit: {pause_after_exit}",
                    f"Backup: {snapshot}",
                    "",
                )
            ),
            encoding="utf-8",
        )
        self.current = EngineRun(process, datetime.now(), task_log, pause_after_exit)
        return self.current

    def _write_pause_wrapper(self) -> Path:
        """Create a small ASCII-only launcher that keeps the native console open."""

        wrapper_dir = self.paths.data / "RunWrappers"
        wrapper_dir.mkdir(parents=True, exist_ok=True)
        wrapper_path = wrapper_dir / "run_downloader_and_pause.cmd"
        engine_path = str(self.paths.engine_exe).replace("%", "%%")
        content = "\r\n".join(
            (
                "@echo off",
                f'call "{engine_path}"',
                'set "DOUK_ENGINE_EXIT=%ERRORLEVEL%"',
                "echo.",
                "echo ============================================================",
                "echo DouK-Downloader finished. Exit code: %DOUK_ENGINE_EXIT%",
                "echo Review the download, skip and failure statistics above.",
                "echo Press any key to close this window...",
                "pause >nul",
                "exit /b %DOUK_ENGINE_EXIT%",
                "",
            )
        )
        temporary = wrapper_path.with_suffix(".tmp")
        temporary.write_text(content, encoding="ascii", newline="")
        os.replace(temporary, wrapper_path)
        return wrapper_path
