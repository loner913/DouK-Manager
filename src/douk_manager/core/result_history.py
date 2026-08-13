from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from douk_manager.core.download_summary import AccountStatus


class ResultHistoryError(RuntimeError):
    pass


@dataclass(frozen=True)
class AccountHistoryRow:
    task_log: Path
    task_template: str
    ended_at: datetime
    a_number: int
    status: AccountStatus | str
    completed_with_anomaly: bool = False


@dataclass(frozen=True)
class DownloadTaskHistory:
    task_log: Path
    task_template: str
    ended_at: datetime
    exit_code: int | None
    complete: bool | None
    reliable: bool
    account_rows: tuple[AccountHistoryRow, ...]
    details_complete: bool


@dataclass(frozen=True)
class RecentPrivateMatch:
    a_number: int
    ended_at: datetime
    task_template: str
    task_log: Path

    @property
    def source_text(self) -> str:
        return f"{self.ended_at:%Y-%m-%d %H:%M:%S} / {self.task_template}"


_DATE_IN_FILENAME = re.compile(
    r"DownloadTask_(\d{4}-\d{2}-\d{2})_(\d{2}-\d{2}-\d{2})(?:-\d+)?\.log$",
    re.IGNORECASE,
)
_A_TOKEN = re.compile(r"A(\d+)(?:-A(\d+))?", re.IGNORECASE)
_STATUS_LINES: tuple[tuple[str, AccountStatus | str], ...] = (
    ("有新作品下载", AccountStatus.DOWNLOADED),
    ("作品均被引擎跳过", AccountStatus.ALL_SKIPPED),
    ("无符合条件作品", AccountStatus.NO_ELIGIBLE_WORKS),
    ("私密账号", AccountStatus.PRIVATE),
    ("已开始后处理异常", AccountStatus.ERROR),
    ("处理中断", AccountStatus.INTERRUPTED),
    ("进入处理前异常", "pre_start_error"),
    ("未开始", "not_started"),
)
_MAX_LOG_BYTES = 16 * 1024 * 1024
_MAX_LINE_CHARS = 256 * 1024


class ResultHistoryService:
    """Read-only view over DownloadTask_*.log files.

    The task logs remain the single source of truth.  No side database or
    cache is created, so deleting a task template cannot delete history.
    """

    def __init__(self, log_directory: Path) -> None:
        self.log_directory = log_directory

    def list_runs(self, *, limit: int = 500) -> tuple[DownloadTaskHistory, ...]:
        if limit < 1:
            return ()
        self.log_directory.mkdir(parents=True, exist_ok=True)
        paths_with_mtime: list[tuple[int, Path]] = []
        for path in self.log_directory.glob("DownloadTask_*.log"):
            try:
                paths_with_mtime.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue
        paths = [path for _, path in sorted(paths_with_mtime, reverse=True)[:limit]]
        result: list[DownloadTaskHistory] = []
        for path in paths:
            try:
                parsed = self.parse(path)
            except (OSError, UnicodeError, ResultHistoryError):
                continue
            if parsed is not None:
                result.append(parsed)
        result.sort(key=lambda item: item.ended_at, reverse=True)
        return tuple(result)

    def list_account_rows(self, *, limit: int = 500) -> tuple[AccountHistoryRow, ...]:
        return tuple(row for run in self.list_runs(limit=limit) for row in run.account_rows)

    def recent_private(
        self,
        numbers: tuple[int, ...],
        validity_days: int,
        *,
        now: datetime | None = None,
    ) -> tuple[RecentPrivateMatch, ...]:
        if validity_days < 1:
            raise ResultHistoryError("私密账号参考期限必须至少为 1 天。")
        requested = set(numbers)
        if not requested:
            return ()
        threshold = (now or datetime.now()) - timedelta(days=validity_days)
        latest: dict[int, tuple[AccountHistoryRow, bool]] = {}
        unknown: set[int] = set()
        for run in self.list_runs():
            if run.ended_at < threshold:
                continue
            if not run.details_complete:
                # Sparse legacy logs do not prove which requested accounts
                # were outside the run, so they block older automatic results.
                unknown.update(requested - latest.keys() - unknown)
                continue
            for row in run.account_rows:
                if (
                    row.a_number in requested
                    and row.a_number not in latest
                    and row.a_number not in unknown
                ):
                    latest[row.a_number] = (row, run.details_complete)
        matches = [
            RecentPrivateMatch(
                a_number=number,
                ended_at=row.ended_at,
                task_template=row.task_template,
                task_log=row.task_log,
            )
            for number, (row, details_complete) in latest.items()
            if details_complete and row.status is AccountStatus.PRIVATE
        ]
        return tuple(sorted(matches, key=lambda item: item.a_number))

    @staticmethod
    def parse(path: Path) -> DownloadTaskHistory | None:
        if path.stat().st_size > _MAX_LOG_BYTES:
            raise ResultHistoryError(f"任务日志过大，已跳过：{path.name}")

        task_template = "未知任务"
        ended_at: datetime | None = None
        exit_code: int | None = None
        complete: bool | None = None
        reliable = False
        details_complete_marker = False
        number_map: dict[int, AccountStatus | str] = {}
        anomaly_numbers: set[int] = set()
        in_summary = False

        with path.open("r", encoding="utf-8-sig", errors="strict") as handle:
            for raw_line in handle:
                if len(raw_line) > _MAX_LINE_CHARS:
                    raise ResultHistoryError(f"任务日志单行过长，已跳过：{path.name}")
                line = raw_line.strip()
                if "Task template:" in line:
                    task_template = line.split("Task template:", 1)[1].split("；", 1)[0].strip()
                if line == "【下载账号汇总】":
                    in_summary = True
                    number_map.clear()
                    anomaly_numbers.clear()
                    reliable = False
                    complete = None
                    continue
                if not in_summary:
                    continue
                if line.startswith("进程结束时间："):
                    ended_at = _parse_datetime(line.split("：", 1)[1])
                elif line.startswith("退出码："):
                    exit_code = _parse_exit_code(line.split("：", 1)[1])
                elif line.startswith("账号汇总："):
                    complete = line.endswith("完整") and not line.endswith("结果不完整")
                    reliable = True
                elif line.startswith("账号结果：无法可靠汇总"):
                    reliable = False
                elif line.startswith("账号明细版本："):
                    details_complete_marker = line.endswith("1")
                elif line.startswith("附加状态：异常后完成（"):
                    anomaly_numbers.update(_numbers_from_line(line))
                else:
                    for label, status in _STATUS_LINES:
                        if line.startswith(f"{label}（"):
                            for number in _numbers_from_line(line):
                                number_map[number] = status
                            break

        if ended_at is None:
            ended_at = _datetime_from_filename(path)
        if ended_at is None or not in_summary:
            return None
        rows = tuple(
            AccountHistoryRow(
                task_log=path,
                task_template=task_template,
                ended_at=ended_at,
                a_number=number,
                status=status,
                completed_with_anomaly=number in anomaly_numbers,
            )
            for number, status in sorted(number_map.items())
        )
        return DownloadTaskHistory(
            task_log=path,
            task_template=task_template,
            ended_at=ended_at,
            exit_code=exit_code,
            complete=complete,
            reliable=reliable,
            account_rows=rows,
            details_complete=details_complete_marker,
        )


def _numbers_from_line(line: str) -> tuple[int, ...]:
    _, _, value = line.partition("：")
    numbers: set[int] = set()
    for match in _A_TOKEN.finditer(value):
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if end < start or end - start > 1_000_000:
            continue
        numbers.update(range(start, end + 1))
    return tuple(sorted(numbers))


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


def _parse_exit_code(value: str) -> int | None:
    try:
        return int(value.strip())
    except ValueError:
        return None


def _datetime_from_filename(path: Path) -> datetime | None:
    match = _DATE_IN_FILENAME.match(path.name)
    if not match:
        return None
    try:
        return datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H-%M-%S")
    except ValueError:
        return None
