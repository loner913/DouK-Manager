from __future__ import annotations

import os
import re
from collections import OrderedDict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Literal

from douk_manager.core.download_summary import AccountStatus

if TYPE_CHECKING:
    from douk_manager.operation import OperationContext


class ResultDashboardError(RuntimeError):
    pass


class DashboardTaskUnavailableError(ResultDashboardError):
    pass


class DashboardFileChangedError(ResultDashboardError):
    pass


@dataclass(frozen=True)
class DashboardFileFingerprint:
    normalized_path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class DashboardTaskIndexEntry:
    task_log: Path
    fingerprint: DashboardFileFingerprint
    task_template: str | None
    ended_at: datetime | None
    state: Literal["ready", "pending", "unavailable"]
    reason: str

    @property
    def displayable(self) -> bool:
        return self.state == "ready"


@dataclass(frozen=True)
class DashboardTaskIndex:
    entries: tuple[DashboardTaskIndexEntry, ...]
    default_task: Path | None
    pending_count: int


@dataclass(frozen=True)
class DashboardNativeLogSegment:
    path: Path
    offset: int | None
    length: int | None


@dataclass(frozen=True)
class DashboardAccountRow:
    a_number: int
    status: AccountStatus | str
    completed_with_anomaly: bool


@dataclass(frozen=True)
class ResultDashboardSnapshot:
    fingerprint: DashboardFileFingerprint
    task_log: Path
    task_template: str | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_seconds: int | None
    exit_code: int | None
    planned_count: int | None
    started_count: int | None
    main_status_counts: tuple[tuple[AccountStatus, int | None], ...]
    completed_with_anomaly_count: int | None
    pre_start_error_count: int | None
    not_started_count: int | None
    unattributed_planned_count: int | None
    complete: bool | None
    reliable: bool
    reliability_reasons: tuple[str, ...]
    details_complete: bool
    locator_method: str | None
    native_log_segments: tuple[DashboardNativeLogSegment, ...]
    account_rows: tuple[DashboardAccountRow, ...]

    def count_for(self, status: AccountStatus) -> int | None:
        return dict(self.main_status_counts).get(status)


_TASK_NAME = re.compile(r"DownloadTask_.+\.log$", re.IGNORECASE)
_STARTED_LINE = re.compile(r"^\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\].*\bStarted\b")
_A_TOKEN = re.compile(r"A(\d+)(?:-A(\d+))?", re.IGNORECASE)
_SEGMENT_LINE = re.compile(r"^日志区间：(.*)；偏移=(\d+)；长度=(\d+)$")
_SUMMARY_MARKER = "【下载账号汇总】"
_SUMMARY_TERMINATOR = (
    "结果不完整：用户停止、异常退出、日志截断、计划账号未全部完成，"
    "或其他证据不足导致无法确认完整任务结果。"
)
_MAX_LOG_BYTES = 16 * 1024 * 1024
_MAX_LINE_CHARS = 256 * 1024
_INDEX_PREFIX_BYTES = 64 * 1024
_INDEX_TAIL_BYTES = 4 * 1024 * 1024

_COUNT_LABELS: tuple[tuple[str, AccountStatus], ...] = (
    ("有新作品下载", AccountStatus.DOWNLOADED),
    ("作品均被引擎跳过", AccountStatus.ALL_SKIPPED),
    ("无符合条件作品", AccountStatus.NO_ELIGIBLE_WORKS),
    ("私密账号", AccountStatus.PRIVATE),
    ("处理异常，需核对（已开始主状态）", AccountStatus.ERROR),
    ("处理中断", AccountStatus.INTERRUPTED),
)
_DETAIL_LABELS: tuple[tuple[str, AccountStatus | str], ...] = (
    ("有新作品下载", AccountStatus.DOWNLOADED),
    ("作品均被引擎跳过", AccountStatus.ALL_SKIPPED),
    ("无符合条件作品", AccountStatus.NO_ELIGIBLE_WORKS),
    ("私密账号", AccountStatus.PRIVATE),
    ("已开始后处理异常", AccountStatus.ERROR),
    ("处理中断", AccountStatus.INTERRUPTED),
    ("进入处理前异常", "pre_start_error"),
    ("未开始", "not_started"),
)


class ResultDashboardService:
    """Read-only, process-local dashboard over DownloadTask log evidence."""

    def __init__(self, log_directory: Path, *, cache_capacity: int = 32) -> None:
        if cache_capacity < 1:
            raise ValueError("cache_capacity must be positive")
        self.log_directory = log_directory
        self.cache_capacity = cache_capacity
        self._lock = RLock()
        self._index_signature: tuple[tuple[str, int, int], ...] | None = None
        self._index_cache: DashboardTaskIndex | None = None
        self._parse_cache: OrderedDict[
            DashboardFileFingerprint, ResultDashboardSnapshot
        ] = OrderedDict()

    def task_index(
        self,
        *,
        force_refresh: bool = False,
        context: OperationContext | None = None,
    ) -> DashboardTaskIndex:
        _check_context(context)
        paths = self._scan_paths(context)
        signature = tuple(
            (fingerprint.normalized_path, fingerprint.size, fingerprint.mtime_ns)
            for _, fingerprint in paths
        )
        with self._lock:
            if (
                not force_refresh
                and signature == self._index_signature
                and self._index_cache is not None
            ):
                return self._index_cache

        entries: list[DashboardTaskIndexEntry] = []
        for path, fingerprint in paths:
            _check_context(context)
            entries.append(self._inspect_index_entry(path, fingerprint))
        entries.sort(
            key=lambda item: (
                item.ended_at is not None,
                item.ended_at or datetime.min,
                item.task_log.name.casefold(),
            ),
            reverse=True,
        )
        default = next(
            (
                entry.task_log
                for entry in entries
                if entry.displayable and entry.ended_at is not None
            ),
            None,
        )
        result = DashboardTaskIndex(
            entries=tuple(entries),
            default_task=default,
            pending_count=sum(entry.state == "pending" for entry in entries),
        )
        with self._lock:
            self._index_signature = signature
            self._index_cache = result
        return result

    def selected_task(
        self,
        task_log: Path,
        *,
        expected_fingerprint: DashboardFileFingerprint | None = None,
        force_refresh: bool = False,
        context: OperationContext | None = None,
    ) -> ResultDashboardSnapshot:
        _check_context(context)
        path = self._validated_task_path(task_log)
        before = self._fingerprint(path)
        if expected_fingerprint is not None and before != expected_fingerprint:
            raise DashboardFileChangedError("任务日志已变化，请刷新任务索引后重试。")
        with self._lock:
            if not force_refresh:
                cached = self._parse_cache.get(before)
                if cached is not None:
                    self._parse_cache.move_to_end(before)
                    return cached

        snapshot = self._parse_path(path, before, context)
        _check_context(context)
        after = self._fingerprint(path)
        if after != before:
            raise DashboardFileChangedError("读取期间任务日志发生变化，已丢弃本次结果。")
        with self._lock:
            self._parse_cache[after] = snapshot
            self._parse_cache.move_to_end(after)
            while len(self._parse_cache) > self.cache_capacity:
                self._parse_cache.popitem(last=False)
        return snapshot

    def _scan_paths(
        self, context: OperationContext | None
    ) -> list[tuple[Path, DashboardFileFingerprint]]:
        if not self.log_directory.is_dir():
            return []
        result: list[tuple[Path, DashboardFileFingerprint]] = []
        for path in self.log_directory.glob("DownloadTask_*.log"):
            _check_context(context)
            try:
                result.append((path, self._fingerprint(path)))
            except OSError:
                continue
        result.sort(key=lambda item: item[0].name.casefold())
        return result

    def _inspect_index_entry(
        self, path: Path, fingerprint: DashboardFileFingerprint
    ) -> DashboardTaskIndexEntry:
        if fingerprint.size > _MAX_LOG_BYTES:
            return DashboardTaskIndexEntry(
                path,
                fingerprint,
                None,
                None,
                "unavailable",
                "任务日志超过安全读取上限。",
            )
        try:
            prefix, tail = _read_index_fragments(path, fingerprint.size)
        except (OSError, UnicodeError, ResultDashboardError) as exc:
            return DashboardTaskIndexEntry(
                path,
                fingerprint,
                None,
                None,
                "unavailable",
                f"无法读取任务索引：{exc}",
            )
        task_template = _task_template_from_text(prefix)
        marker_at = tail.rfind(_SUMMARY_MARKER)
        if marker_at < 0:
            reason = (
                "账号汇总超出轻量索引范围。"
                if _SUMMARY_TERMINATOR in tail
                else "当前任务正在运行或尚未写入账号汇总。"
            )
            state: Literal["pending", "unavailable"] = (
                "unavailable" if _SUMMARY_TERMINATOR in tail else "pending"
            )
            return DashboardTaskIndexEntry(
                path, fingerprint, task_template, None, state, reason
            )
        summary_lines = _meaningful_lines(tail[marker_at:])
        ended_at = _value_datetime(summary_lines, "进程结束时间：")
        if not summary_lines or summary_lines[-1] != _SUMMARY_TERMINATOR:
            return DashboardTaskIndexEntry(
                path,
                fingerprint,
                task_template,
                ended_at,
                "pending",
                "账号汇总尚未写入完成。",
            )
        return DashboardTaskIndexEntry(
            path, fingerprint, task_template, ended_at, "ready", ""
        )

    def _validated_task_path(self, task_log: Path) -> Path:
        root = self.log_directory.resolve()
        candidate = task_log.resolve()
        if (
            os.path.normcase(str(candidate.parent)) != os.path.normcase(str(root))
            or not _TASK_NAME.fullmatch(candidate.name)
        ):
            raise DashboardTaskUnavailableError("只能读取任务日志目录中的 DownloadTask 日志。")
        return candidate

    @staticmethod
    def _fingerprint(path: Path) -> DashboardFileFingerprint:
        stat = path.stat()
        return DashboardFileFingerprint(
            normalized_path=os.path.normcase(str(path.resolve())),
            size=stat.st_size,
            mtime_ns=stat.st_mtime_ns,
        )

    @staticmethod
    def _parse_path(
        path: Path,
        fingerprint: DashboardFileFingerprint,
        context: OperationContext | None,
    ) -> ResultDashboardSnapshot:
        if fingerprint.size > _MAX_LOG_BYTES:
            raise DashboardTaskUnavailableError("任务日志超过安全读取上限。")

        started_at: datetime | None = None
        task_template: str | None = None
        summary_lines: list[str] | None = None
        with path.open("r", encoding="utf-8-sig", errors="strict") as handle:
            for raw_line in handle:
                _check_context(context)
                if len(raw_line) > _MAX_LINE_CHARS:
                    raise DashboardTaskUnavailableError("任务日志包含超长行。")
                line = raw_line.strip()
                if started_at is None:
                    match = _STARTED_LINE.match(line)
                    if match:
                        started_at = _parse_datetime(match.group(1))
                if task_template is None and "Task template:" in line:
                    value = line.split("Task template:", 1)[1].split("；", 1)[0].strip()
                    task_template = value or None
                if line == _SUMMARY_MARKER:
                    summary_lines = [line]
                elif summary_lines is not None:
                    summary_lines.append(line)

        meaningful = tuple(line for line in (summary_lines or ()) if line)
        if not meaningful:
            raise DashboardTaskUnavailableError("当前任务尚未写入账号汇总。")
        if meaningful[-1] != _SUMMARY_TERMINATOR:
            raise DashboardTaskUnavailableError("账号汇总尚未写入完成。")
        return _snapshot_from_lines(
            path,
            fingerprint,
            task_template,
            started_at,
            meaningful,
        )


def _snapshot_from_lines(
    path: Path,
    fingerprint: DashboardFileFingerprint,
    task_template: str | None,
    started_at: datetime | None,
    lines: tuple[str, ...],
) -> ResultDashboardSnapshot:
    ended_at = _value_datetime(lines, "进程结束时间：")
    exit_code = _value_int(lines, "退出码：")
    locator_method = _value_text(lines, "日志定位方式：")
    complete: bool | None = None
    account_summary = _value_text(lines, "账号汇总：")
    if account_summary is not None:
        if account_summary == "完整":
            complete = True
        elif account_summary == "结果不完整":
            complete = False

    planned_count = _value_int(lines, "计划账号：")
    started_count = _value_int(lines, "实际开始：")
    counts = tuple(
        (status, _exact_label_int(lines, label)) for label, status in _COUNT_LABELS
    )
    pre_start_count = _exact_label_int(lines, "进入处理前异常（不计入主状态合计）")
    not_started_count = _exact_label_int(lines, "未开始（不计入主状态合计）")
    anomaly_count = _exact_label_int(
        lines, "完成但有异常记录（附加状态，不计入主状态合计）"
    )

    reasons: list[str] = []
    explicitly_unreliable = False
    for line in lines:
        if line.startswith("账号结果：无法可靠汇总"):
            explicitly_unreliable = True
            _, marker, reason = line.partition("原因：")
            if marker and reason.strip():
                _add_reason(reasons, reason.strip())
    reliable = account_summary is not None and not explicitly_unreliable

    known_counts = [count for _, count in counts]
    if started_count is not None and all(count is not None for count in known_counts):
        if started_count != sum(count for count in known_counts if count is not None):
            reliable = False
            _add_reason(reasons, "实际开始数与六类主状态合计不一致。")

    unattributed: int | None = None
    if (
        planned_count is not None
        and started_count is not None
        and pre_start_count is not None
        and not_started_count is not None
    ):
        remainder = planned_count - started_count - pre_start_count - not_started_count
        if remainder < 0:
            reliable = False
            _add_reason(reasons, "计划账号核对公式不成立。")
        else:
            unattributed = remainder
            if remainder:
                reliable = False
                _add_reason(reasons, f"有 {remainder} 个计划账号无法可靠归类。")

    row_status: dict[int, AccountStatus | str] = {}
    anomaly_numbers: set[int] = set()
    for line in lines:
        if line.startswith("附加状态：异常后完成（"):
            anomaly_numbers.update(_numbers_from_line(line))
            continue
        for label, status in _DETAIL_LABELS:
            if line.startswith(f"{label}（"):
                for number in _numbers_from_line(line):
                    previous = row_status.get(number)
                    if previous is not None and previous != status:
                        reliable = False
                        _add_reason(reasons, f"A{number} 出现在多个互斥状态中。")
                        continue
                    row_status[number] = status
                break

    details_marker = any(line == "账号明细版本：1" for line in lines)
    details_complete = details_marker
    expected_rows = None
    if (
        started_count is not None
        and pre_start_count is not None
        and not_started_count is not None
    ):
        expected_rows = started_count + pre_start_count + not_started_count
    if details_marker and expected_rows is not None and len(row_status) != expected_rows:
        details_complete = False
        reliable = False
        _add_reason(reasons, "账号明细数量与任务级统计不一致。")

    segments: list[DashboardNativeLogSegment] = []
    segment_paths: set[str] = set()
    for line in lines:
        match = _SEGMENT_LINE.match(line)
        if match:
            segment = DashboardNativeLogSegment(
                Path(match.group(1)), int(match.group(2)), int(match.group(3))
            )
            segments.append(segment)
            segment_paths.add(os.path.normcase(str(segment.path)))
        elif line.startswith("原生日志："):
            native_path = Path(line.split("：", 1)[1].strip())
            key = os.path.normcase(str(native_path))
            if key not in segment_paths:
                segments.append(DashboardNativeLogSegment(native_path, None, None))
                segment_paths.add(key)

    duration_seconds = None
    if started_at is not None and ended_at is not None and ended_at >= started_at:
        duration_seconds = int((ended_at - started_at).total_seconds())
    if not reliable and not reasons:
        _add_reason(reasons, "任务日志未提供可靠汇总结论。")

    rows = tuple(
        DashboardAccountRow(
            a_number=number,
            status=status,
            completed_with_anomaly=number in anomaly_numbers,
        )
        for number, status in sorted(row_status.items())
    )
    return ResultDashboardSnapshot(
        fingerprint=fingerprint,
        task_log=path,
        task_template=task_template,
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=duration_seconds,
        exit_code=exit_code,
        planned_count=planned_count,
        started_count=started_count,
        main_status_counts=counts,
        completed_with_anomaly_count=anomaly_count,
        pre_start_error_count=pre_start_count,
        not_started_count=not_started_count,
        unattributed_planned_count=unattributed,
        complete=complete,
        reliable=reliable,
        reliability_reasons=tuple(reasons),
        details_complete=details_complete,
        locator_method=locator_method,
        native_log_segments=tuple(segments),
        account_rows=rows,
    )


def _read_index_fragments(path: Path, size: int) -> tuple[str, str]:
    with path.open("rb") as handle:
        if size <= _INDEX_PREFIX_BYTES + _INDEX_TAIL_BYTES:
            data = handle.read()
            text = data.decode("utf-8-sig", errors="strict")
            _check_lines(text)
            return text, text
        prefix_bytes = handle.read(_INDEX_PREFIX_BYTES)
        handle.seek(max(0, size - _INDEX_TAIL_BYTES))
        tail_bytes = handle.read(_INDEX_TAIL_BYTES)
    newline = tail_bytes.find(b"\n")
    if newline < 0:
        raise ResultDashboardError("轻量索引范围内没有完整行。")
    tail_bytes = tail_bytes[newline + 1 :]
    prefix = prefix_bytes.decode("utf-8-sig", errors="strict")
    tail = tail_bytes.decode("utf-8", errors="strict")
    _check_lines(prefix)
    _check_lines(tail)
    return prefix, tail


def _check_lines(text: str) -> None:
    if any(len(line) > _MAX_LINE_CHARS for line in text.splitlines()):
        raise ResultDashboardError("任务日志包含超长行。")


def _meaningful_lines(text: str) -> tuple[str, ...]:
    return tuple(line.strip() for line in text.splitlines() if line.strip())


def _task_template_from_text(text: str) -> str | None:
    for line in text.splitlines():
        if "Task template:" in line:
            value = line.split("Task template:", 1)[1].split("；", 1)[0].strip()
            return value or None
    return None


def _value_text(lines: tuple[str, ...], prefix: str) -> str | None:
    for line in lines:
        if line.startswith(prefix):
            value = line[len(prefix) :].strip()
            return value or None
    return None


def _value_int(lines: tuple[str, ...], prefix: str) -> int | None:
    value = _value_text(lines, prefix)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _exact_label_int(lines: tuple[str, ...], label: str) -> int | None:
    return _value_int(lines, f"{label}：")


def _value_datetime(lines: tuple[str, ...], prefix: str) -> datetime | None:
    value = _value_text(lines, prefix)
    return _parse_datetime(value) if value is not None else None


def _parse_datetime(value: str) -> datetime | None:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None


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


def _add_reason(reasons: list[str], reason: str) -> None:
    if reason and reason not in reasons:
        reasons.append(reason)


def _check_context(context: OperationContext | None) -> None:
    if context is not None:
        context.raise_if_cancelled()
