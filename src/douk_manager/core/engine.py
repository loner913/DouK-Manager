from __future__ import annotations

import os
import signal
import subprocess
import ctypes
import hashlib
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from threading import Lock

from douk_manager.config import AppConfig, ManagedPaths
from douk_manager.core.backup import BackupService
from douk_manager.core.download_summary import (
    DownloadSummary,
    NativeLogState,
    PlannedAccount,
    SummaryInputError,
    SummaryWriteError,
    format_summary_for_task_log,
    freeze_planned_accounts,
    locate_native_logs,
    parse_download_summary,
    snapshot_native_logs,
)
from douk_manager.core.json_store import read_json, write_json_atomic
from douk_manager.core.locks import critical_section
from douk_manager.ui_messages import format_information


class EngineError(RuntimeError):
    pass


BATCH_RUN_COMMAND = "5 1 1 Q"
MONITOR_RUN_COMMAND = "6"
ENGINE_MODE_BATCH = "batch"
ENGINE_MODE_MONITOR = "monitor"


@dataclass
class EngineRun:
    process: subprocess.Popen
    started_at: datetime
    task_log: Path
    planned_accounts: tuple[PlannedAccount, ...]
    native_log_snapshot: tuple[NativeLogState, ...]
    native_log_dir: Path
    mode: str = ENGINE_MODE_BATCH
    pause_after_exit: bool = False
    task_template: str = "current settings.json"
    _summary_lock: Lock = field(
        default_factory=Lock, init=False, repr=False, compare=False
    )
    _summary_result: DownloadSummary | None = field(
        default=None, init=False, repr=False, compare=False
    )
    _summary_write_error: SummaryWriteError | None = field(
        default=None, init=False, repr=False, compare=False
    )

    @property
    def selected_accounts(self) -> int:
        return len(self.planned_accounts)

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
        if _windows_engine_mutex_exists(self.paths.engine_exe):
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

    def validate_ready(self) -> int:
        if not self.paths.engine_exe.is_file():
            raise EngineError(f"下载引擎不存在：{self.paths.engine_exe}")
        active = read_json(self.paths.active_settings)
        if active.get("run_command") != BATCH_RUN_COMMAND:
            raise EngineError(
                f'正式 settings.json 的 run_command 必须为 "{BATCH_RUN_COMMAND}"。'
            )
        accounts = active.get("accounts_urls")
        if not isinstance(accounts, list):
            raise EngineError("正式 settings.json 缺少 accounts_urls。")
        selected_accounts = sum(
            1
            for account in accounts
            if isinstance(account, dict)
            and account.get("enable", True)
            and str(account.get("url", "")).strip()
        )
        if selected_accounts < 1:
            raise EngineError("正式 settings.json 没有启用且 URL 有效的账号。")
        return selected_accounts

    def start(
        self,
        *,
        pause_after_exit: bool = False,
        task_template: Path | None = None,
    ) -> EngineRun:
        if self.external_running():
            raise EngineError("下载引擎已经在运行。")
        display_template = (
            task_template.name if task_template else "current settings.json"
        )
        with critical_section(self.paths.lock_file):
            if self.external_running():
                raise EngineError("下载引擎已经在运行。")
            engine_mutex = _WindowsEngineMutex.acquire(self.paths.engine_exe)
            try:
                if not self.paths.engine_exe.is_file():
                    raise EngineError(f"Engine is missing: {self.paths.engine_exe}")
                active = read_json(self.paths.active_settings)
                if active.get("run_command") != BATCH_RUN_COMMAND:
                    raise EngineError(
                        f"settings.json run_command must be '{BATCH_RUN_COMMAND}'."
                    )
                try:
                    planned_accounts = freeze_planned_accounts(active)
                except SummaryInputError as exc:
                    raise EngineError(str(exc)) from exc
                native_log_dir = self.paths.volume / "Log"
                native_log_snapshot = snapshot_native_logs(native_log_dir)
                selected_accounts = len(planned_accounts)
                snapshot = self.backup.create_critical_snapshot(
                    "BeforeDownload",
                    {
                        "task_template": display_template,
                        "selected_accounts": selected_accounts,
                        "batch_accounts": self.config.batch_accounts,
                        "rest_seconds": self.config.rest_seconds,
                        "run_command": BATCH_RUN_COMMAND,
                        "pause_after_exit": pause_after_exit,
                    },
                    keep_latest=5,
                )
                env = os.environ.copy()
                env["DOUK_ACCOUNT_BATCH_SIZE"] = str(self.config.batch_accounts)
                env["DOUK_ACCOUNT_REST_SECONDS"] = str(self.config.rest_seconds)
                env["DOUK_MANAGER_BACKUP"] = str(snapshot)
                creationflags = 0
                command = [str(self.paths.engine_exe)]
                popen_options = {}
                if os.name == "nt":
                    creationflags = subprocess.CREATE_NEW_CONSOLE
                    if pause_after_exit:
                        command = [
                            "cmd.exe",
                            "/d",
                            "/c",
                            str(self._write_pause_wrapper()),
                        ]
                    popen_options = engine_mutex.popen_options()
                try:
                    process = subprocess.Popen(
                        command,
                        cwd=str(self.paths.engine_root),
                        env=env,
                        creationflags=creationflags,
                        **popen_options,
                    )
                except OSError as exc:
                    raise EngineError(f"无法启动下载引擎：{exc}") from exc
            except OSError as exc:
                raise EngineError(f"无法准备下载引擎启动：{exc}") from exc
            finally:
                engine_mutex.close()
        task_log = (
            self.paths.download_task_logs
            / f"DownloadTask_{datetime.now():%Y-%m-%d_%H-%M-%S-%f}.log"
        )
        started_at = datetime.now()
        task_log.write_text(
            format_information(
                "Started",
                "Log scope: one downloader process",
                f"Task template: {display_template}",
                f"Engine: {self.paths.engine_exe}",
                f"Active settings: {self.paths.active_settings}",
                f"run_command: {BATCH_RUN_COMMAND}",
                f"Selected accounts: {selected_accounts}",
                f"Pause every accounts: {self.config.batch_accounts}",
                f"Pause seconds: {self.config.rest_seconds}",
                f"Pause after exit: {pause_after_exit}",
                f"Backup: {snapshot}",
                at=started_at,
                merge=True,
                include_date=True,
            )
            + "\n",
            encoding="utf-8",
        )
        self.current = EngineRun(
            process=process,
            started_at=started_at,
            task_log=task_log,
            planned_accounts=planned_accounts,
            native_log_snapshot=native_log_snapshot,
            native_log_dir=native_log_dir,
            mode=ENGINE_MODE_BATCH,
            pause_after_exit=pause_after_exit,
            task_template=display_template,
        )
        return self.current

    def start_monitor(self) -> EngineRun:
        """Launch the downloader's clipboard monitoring menu (run_command=6)."""

        if self.external_running():
            raise EngineError("下载引擎已经在运行，不能同时启动后台监听。")
        process: subprocess.Popen | None = None
        command_changed = False
        with critical_section(self.paths.lock_file):
            if self.external_running():
                raise EngineError("下载引擎已经在运行，不能同时启动后台监听。")
            if not self.paths.engine_exe.is_file():
                raise EngineError(f"下载引擎不存在：{self.paths.engine_exe}")
            engine_mutex = _WindowsEngineMutex.acquire(self.paths.engine_exe)
            try:
                snapshot = self.backup.create_critical_snapshot(
                    "BeforeMonitor",
                    {
                        "operation": "start_clipboard_monitor",
                        "run_command_before": read_json(
                            self.paths.active_settings
                        ).get("run_command"),
                        "run_command_after": MONITOR_RUN_COMMAND,
                    },
                    keep_latest=5,
                )
                self._set_run_command(MONITOR_RUN_COMMAND)
                command_changed = True
                creationflags = 0
                popen_options = {}
                if os.name == "nt":
                    creationflags = (
                        subprocess.CREATE_NEW_CONSOLE
                        | subprocess.CREATE_NEW_PROCESS_GROUP
                    )
                    popen_options = engine_mutex.popen_options()
                process = subprocess.Popen(
                    [str(self.paths.engine_exe)],
                    cwd=str(self.paths.engine_root),
                    env=os.environ.copy(),
                    creationflags=creationflags,
                    **popen_options,
                )
            except Exception as exc:
                if command_changed:
                    try:
                        self._set_run_command(BATCH_RUN_COMMAND)
                    except Exception:
                        pass
                if isinstance(exc, EngineError):
                    raise
                raise EngineError(f"无法启动下载后台监听：{exc}") from exc
            finally:
                engine_mutex.close()

        try:
            started_at = datetime.now()
            task_log = (
                self.paths.download_task_logs
                / f"DownloadTask_{started_at:%Y-%m-%d_%H-%M-%S-%f}.log"
            )
            task_log.write_text(
                format_information(
                    "Started",
                    "Log scope: one downloader clipboard monitor process",
                    "Task template: 后台剪贴板监听",
                    f"Engine: {self.paths.engine_exe}",
                    f"Active settings: {self.paths.active_settings}",
                    f"run_command: {MONITOR_RUN_COMMAND}",
                    f"Backup: {snapshot}",
                    at=started_at,
                    merge=True,
                    include_date=True,
                )
                + "\n",
                encoding="utf-8",
            )
            self.current = EngineRun(
                process=process,
                started_at=started_at,
                task_log=task_log,
                planned_accounts=(),
                native_log_snapshot=(),
                native_log_dir=self.paths.volume / "Log",
                mode=ENGINE_MODE_MONITOR,
                task_template="后台剪贴板监听",
            )
        except Exception as exc:
            self.current = None
            try:
                if os.name == "nt":
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
                process.wait(timeout=15.0)
            except (OSError, subprocess.SubprocessError) as cleanup_exc:
                raise EngineError(
                    "后台监听启动收尾失败，且新进程尚未确认退出；run_command 未恢复。"
                ) from cleanup_exc
            try:
                self._set_run_command(BATCH_RUN_COMMAND)
            except Exception as restore_exc:
                raise EngineError(
                    f"后台监听启动失败，新进程已退出但 run_command 恢复失败：{restore_exc}"
                ) from restore_exc
            raise EngineError(f"无法完成下载后台监听启动：{exc}") from exc
        return self.current

    def stop_monitor(self, *, timeout: float = 15.0) -> Path | None:
        run = self.current
        if run is not None and run.mode != ENGINE_MODE_MONITOR and run.running:
            raise EngineError("当前运行的是账号批量下载，不是后台监听。")
        if run is not None and run.running:
            try:
                if os.name == "nt":
                    run.process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    run.process.terminate()
                run.process.wait(timeout=timeout)
            except (OSError, subprocess.SubprocessError) as exc:
                raise EngineError(
                    "后台监听尚未正常停止，run_command 未恢复；请先关闭下载器监听窗口后重试。"
                ) from exc
        if self.external_running():
            raise EngineError(
                "仍检测到下载引擎进程，run_command 未恢复；请先关闭监听窗口后重试。"
            )
        with critical_section(self.paths.lock_file):
            changed = self._set_run_command(BATCH_RUN_COMMAND)
        if run is not None:
            with run.task_log.open("a", encoding="utf-8") as handle:
                handle.write(
                    format_information(
                        "Stopped",
                        f"run_command restored: {BATCH_RUN_COMMAND}",
                        merge=True,
                        include_date=True,
                    )
                    + "\n"
                )
        self.current = None
        return self.paths.active_settings if changed else None

    def recover_batch_command_if_idle(self) -> bool:
        """Repair a stale run_command=6 left by an earlier abnormal exit."""

        if self.external_running() or not self.paths.active_settings.is_file():
            return False
        active = read_json(self.paths.active_settings)
        if active.get("run_command") != MONITOR_RUN_COMMAND:
            return False
        with critical_section(self.paths.lock_file):
            if self.external_running():
                return False
            self.backup.create_critical_snapshot(
                "BeforeChange",
                {
                    "operation": "recover_stale_monitor_command",
                    "run_command_before": MONITOR_RUN_COMMAND,
                    "run_command_after": BATCH_RUN_COMMAND,
                },
                keep_latest=20,
            )
            self._set_run_command(BATCH_RUN_COMMAND)
        return True

    def _set_run_command(self, command: str) -> bool:
        active = read_json(self.paths.active_settings)
        if active.get("run_command") == command:
            return False
        active["run_command"] = command
        write_json_atomic(self.paths.active_settings, active)
        return True

    def summarize_finished_run(
        self, run: EngineRun, exit_code: int | None, ended_at: datetime
    ) -> DownloadSummary:
        with run._summary_lock:
            if run._summary_result is not None:
                return run._summary_result
            if run._summary_write_error is not None:
                raise run._summary_write_error

            located = locate_native_logs(
                run.native_log_snapshot,
                run.native_log_dir,
                run.started_at,
                ended_at,
                len(run.planned_accounts),
                run.planned_accounts,
            )
            summary = parse_download_summary(run.planned_accounts, located, exit_code)
            block = format_summary_for_task_log(summary, ended_at)
            try:
                with run.task_log.open("a", encoding="utf-8", newline="") as handle:
                    handle.write("\n" + block)
            except (OSError, UnicodeError) as exc:
                error = SummaryWriteError("无法将账号汇总写入现有任务日志。")
                run._summary_write_error = error
                raise error from exc
            run._summary_result = summary
            return summary

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


class _SecurityAttributes(ctypes.Structure):
    _fields_ = (
        ("nLength", wintypes.DWORD),
        ("lpSecurityDescriptor", wintypes.LPVOID),
        ("bInheritHandle", wintypes.BOOL),
    )


class _WindowsEngineMutex:
    ERROR_ALREADY_EXISTS = 183

    def __init__(self, handle: int | None = None) -> None:
        self.handle = handle

    @classmethod
    def acquire(cls, engine_exe: Path) -> "_WindowsEngineMutex":
        if os.name != "nt":
            return cls()
        handle, already_exists = _create_windows_mutex(engine_exe, inheritable=True)
        if not handle:
            raise EngineError("无法创建下载引擎单实例互斥对象。")
        if already_exists:
            _close_windows_handle(handle)
            raise EngineError("下载引擎已经在运行。")
        return cls(handle)

    def popen_options(self) -> dict:
        if self.handle is None:
            return {}
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.lpAttributeList = {"handle_list": [self.handle]}
        return {"startupinfo": startupinfo, "close_fds": True}

    def close(self) -> None:
        if self.handle is not None:
            _close_windows_handle(self.handle)
            self.handle = None


def _engine_mutex_name(engine_exe: Path) -> str:
    normalized = os.path.normcase(os.path.normpath(str(engine_exe.resolve())))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return f"Local\\DouKManager.Engine.{digest}"


def _create_windows_mutex(
    engine_exe: Path, *, inheritable: bool
) -> tuple[int | None, bool]:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (
        ctypes.POINTER(_SecurityAttributes),
        wintypes.BOOL,
        wintypes.LPCWSTR,
    )
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    attributes = _SecurityAttributes(
        ctypes.sizeof(_SecurityAttributes), None, bool(inheritable)
    )
    ctypes.set_last_error(0)
    handle = kernel32.CreateMutexW(
        ctypes.byref(attributes) if inheritable else None,
        False,
        _engine_mutex_name(engine_exe),
    )
    return handle, ctypes.get_last_error() == _WindowsEngineMutex.ERROR_ALREADY_EXISTS


def _close_windows_handle(handle: int) -> None:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle(handle)


def _windows_engine_mutex_exists(engine_exe: Path) -> bool:
    if os.name != "nt":
        return False
    handle, already_exists = _create_windows_mutex(engine_exe, inheritable=False)
    if handle:
        _close_windows_handle(handle)
    return already_exists
