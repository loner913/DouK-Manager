from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Mapping

from douk_manager.core.download_summary import AccountStatus

if TYPE_CHECKING:
    from douk_manager.operation import OperationContext


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
class ResultPageSnapshot:
    runs: tuple[DownloadTaskHistory, ...]
    rows: tuple[AccountHistoryRow, ...]
    incomplete_old_runs: int


@dataclass(frozen=True)
class AccountAuditHistorySnapshot:
    runs_scanned: int
    oldest_run: datetime | None
    newest_run: datetime | None
    rows_by_number: Mapping[int, tuple[AccountHistoryRow, ...]]
    excluded_old_rows: int


@dataclass(frozen=True)
class RecentPrivateMatch:
    a_number: int
    ended_at: datetime
    task_template: str
    task_log: Path

    @property
    def source_text(self) -> str:
        return f"{self.ended_at:%Y-%m-%d %H:%M:%S} / {self.task_template}"


@dataclass(frozen=True)
class PrivateReferenceDecision:
    """Explain how a requested account was classified for smart skipping."""

    a_number: int
    category: str
    row: AccountHistoryRow | None = None

    @property
    def source_text(self) -> str:
        if self.row is None:
            return "无可用历史记录"
        return (
            f"{self.row.ended_at:%Y-%m-%d %H:%M:%S} / "
            f"{self.row.task_template} / {self.row.task_log.name}"
        )


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

    def list_runs(
        self,
        *,
        limit: int | None = 500,
        context: OperationContext | None = None,
    ) -> tuple[DownloadTaskHistory, ...]:
        if limit is not None and limit < 1:
            return ()
        if context is not None:
            context.raise_if_cancelled()
        if not self.log_directory.is_dir():
            return ()
        paths_with_mtime: list[tuple[int, Path]] = []
        for path in self.log_directory.glob("DownloadTask_*.log"):
            try:
                paths_with_mtime.append((path.stat().st_mtime_ns, path))
            except OSError:
                continue
        ordered_paths = [path for _, path in sorted(paths_with_mtime, reverse=True)]
        paths = ordered_paths if limit is None else ordered_paths[:limit]
        result: list[DownloadTaskHistory] = []
        for path in paths:
            if context is not None:
                context.raise_if_cancelled()
            try:
                parsed = self.parse(path)
            except (OSError, UnicodeError, ResultHistoryError):
                continue
            if parsed is not None:
                result.append(parsed)
        result.sort(key=lambda item: item.ended_at, reverse=True)
        return tuple(result)

    def account_rows_by_number(
        self,
        *,
        numbers: tuple[int, ...] | None = None,
        evidence_since: datetime | None = None,
        context: OperationContext | None = None,
    ) -> dict[int, tuple[AccountHistoryRow, ...]]:
        """Return audit evidence grouped by stable array position.

        Rows are chronological within each account.  Missing task rows are not
        invented: an account absent from a run is unselected or unknown and
        therefore neither increments nor clears any status streak.  A caller-
        supplied evidence boundary conservatively excludes older rows without
        trying to infer historical array-position changes.
        """

        return dict(
            self.account_audit_snapshot(
                numbers=numbers,
                evidence_since=evidence_since,
                context=context,
            ).rows_by_number
        )

    def account_audit_snapshot(
        self,
        *,
        numbers: tuple[int, ...] | None = None,
        evidence_since: datetime | None = None,
        context: OperationContext | None = None,
    ) -> AccountAuditHistorySnapshot:
        """Scan history once and return immutable audit-oriented metadata."""

        requested = None if numbers is None else tuple(sorted(set(numbers)))
        requested_set = None if requested is None else set(requested)
        grouped: dict[int, list[AccountHistoryRow]] = (
            {} if requested is None else {number: [] for number in requested}
        )
        excluded_old_rows = 0
        runs = self.list_runs(limit=None, context=context)
        for run in runs:
            if context is not None:
                context.raise_if_cancelled()
            for row in run.account_rows:
                if requested_set is not None and row.a_number not in requested_set:
                    continue
                if evidence_since is not None and row.ended_at < evidence_since:
                    excluded_old_rows += 1
                    continue
                grouped.setdefault(row.a_number, []).append(row)
        frozen_rows = {
            number: tuple(
                sorted(rows, key=lambda row: (row.ended_at, row.task_log.name))
            )
            for number, rows in sorted(grouped.items())
        }
        ended_times = tuple(run.ended_at for run in runs)
        return AccountAuditHistorySnapshot(
            runs_scanned=len(runs),
            oldest_run=min(ended_times, default=None),
            newest_run=max(ended_times, default=None),
            rows_by_number=MappingProxyType(frozen_rows),
            excluded_old_rows=excluded_old_rows,
        )

    def page_snapshot(
        self,
        *,
        limit: int = 500,
        context: OperationContext | None = None,
    ) -> ResultPageSnapshot:
        runs = self.list_runs(limit=limit, context=context)
        rows = tuple(row for run in runs for row in run.account_rows)
        incomplete_old_runs = sum(
            1 for run in runs if run.account_rows and not run.details_complete
        )
        return ResultPageSnapshot(runs, rows, incomplete_old_runs)

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
        decisions = self.classify_private_reference(
            tuple(sorted(requested)), validity_days, now=now
        )
        matches = [
            RecentPrivateMatch(
                a_number=decision.a_number,
                ended_at=decision.row.ended_at,
                task_template=decision.row.task_template,
                task_log=decision.row.task_log,
            )
            for decision in decisions
            if decision.category == "recent_private" and decision.row is not None
        ]
        return tuple(sorted(matches, key=lambda item: item.a_number))

    def classify_private_reference(
        self,
        numbers: tuple[int, ...],
        validity_days: int,
        *,
        now: datetime | None = None,
        context: OperationContext | None = None,
    ) -> tuple[PrivateReferenceDecision, ...]:
        """Classify every requested account without guessing missing results.

        Every task log in the requested time window is considered, not merely
        the 500 rows shown by the result page.  An old-format sparse log may
        safely contribute an account that it explicitly lists; accounts that
        are absent from that log remain unknown.  Likewise, ``not_started``
        and interrupted/error rows are uncertain evidence and cannot erase a
        newer-or-older explicit private/healthy result in the same window.
        """

        if validity_days < 1:
            raise ResultHistoryError("私密账号参考期限必须至少为 1 天。")
        requested = tuple(sorted(set(numbers)))
        if not requested:
            return ()
        threshold = (now or datetime.now()) - timedelta(days=validity_days)
        requested_set = set(requested)
        recent_confirmed: dict[int, AccountHistoryRow] = {}
        recent_uncertain: dict[int, AccountHistoryRow] = {}
        expired_confirmed: dict[int, AccountHistoryRow] = {}
        expired_uncertain: dict[int, AccountHistoryRow] = {}
        confirmed_statuses = {
            AccountStatus.DOWNLOADED,
            AccountStatus.ALL_SKIPPED,
            AccountStatus.NO_ELIGIBLE_WORKS,
            AccountStatus.PRIVATE,
        }
        for run in self.list_runs(limit=None, context=context):
            if context is not None:
                context.raise_if_cancelled()
            for row in run.account_rows:
                if row.a_number not in requested_set:
                    continue
                confirmed = row.status in confirmed_statuses
                if row.ended_at >= threshold:
                    target = recent_confirmed if confirmed else recent_uncertain
                else:
                    target = expired_confirmed if confirmed else expired_uncertain
                current = target.get(row.a_number)
                if current is None or row.ended_at > current.ended_at:
                    target[row.a_number] = row

        decisions: list[PrivateReferenceDecision] = []
        for number in requested:
            recent = recent_confirmed.get(number)
            uncertain = recent_uncertain.get(number)
            expired = expired_confirmed.get(number)
            old_uncertain = expired_uncertain.get(number)
            if recent is not None and recent.status is AccountStatus.PRIVATE:
                category = "recent_private"
                row = recent
            elif recent is not None:
                category = "recent_non_private"
                row = recent
            elif uncertain is not None:
                category = "recent_uncertain"
                row = uncertain
            elif expired is not None and expired.status is AccountStatus.PRIVATE:
                category = "expired_private"
                row = expired
            elif expired is not None:
                category = "expired_non_private"
                row = expired
            elif old_uncertain is not None:
                category = "expired_uncertain"
                row = old_uncertain
            else:
                category = "no_record"
                row = None
            decisions.append(PrivateReferenceDecision(number, category, row))
        return tuple(decisions)

    @staticmethod
    def parse(path: Path) -> DownloadTaskHistory | None:
        if path.stat().st_size > _MAX_LOG_BYTES:
            raise ResultHistoryError(f"任务日志过大，已跳过：{path.name}")

        task_template = "未知任务"
        ended_at: datetime | None = None
        exit_code: int | None = None
        complete: bool | None = None
        reliable = False
        explicit_unreliable_marker = False
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
                    explicit_unreliable_marker = False
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
                    # An incomplete partial-detail block is intentionally
                    # still parseable for traceability, but it must never
                    # regain the reliable flag merely because its account
                    # count section appears after the explicit warning.
                    if not explicit_unreliable_marker:
                        reliable = True
                elif line.startswith("账号结果：无法可靠汇总"):
                    reliable = False
                    explicit_unreliable_marker = True
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
