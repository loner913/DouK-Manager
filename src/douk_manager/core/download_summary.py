from __future__ import annotations

import re
from codecs import getincrementaldecoder
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Iterator

from douk_manager.core.account_identity import log_identity_tokens
from douk_manager.core.selector import compact_numbers


@dataclass(frozen=True)
class PlannedAccount:
    task_index: int
    a_number: int
    mark: str


@dataclass(frozen=True)
class NativeLogState:
    path: Path
    size: int
    mtime_ns: int


@dataclass(frozen=True)
class NativeLogSegment:
    path: Path
    offset: int
    length: int


@dataclass(frozen=True)
class LocatedNativeLogs:
    segments: tuple[NativeLogSegment, ...]
    method: str
    reliable: bool
    reason: str = ""


class SummaryInputError(ValueError):
    pass


class SummaryWriteError(RuntimeError):
    pass


class _LogReadError(Exception):
    pass


class AccountStatus(str, Enum):
    DOWNLOADED = "downloaded"
    ALL_SKIPPED = "all_skipped"
    NO_ELIGIBLE_WORKS = "no_eligible_works"
    PRIVATE = "private"
    ERROR = "error"
    INTERRUPTED = "interrupted"


@dataclass(frozen=True)
class AccountOutcome:
    task_index: int
    a_number: int
    status: AccountStatus
    completed_with_anomaly: bool = False
    identity_tokens: tuple[str, ...] = ()


@dataclass(frozen=True)
class DownloadSummary:
    planned_count: int
    started_outcomes: tuple[AccountOutcome, ...]
    pre_start_errors: tuple[int, ...]
    not_started: tuple[int, ...]
    primary_status_counts: dict[AccountStatus, int]
    completed_with_anomaly: tuple[int, ...]
    complete: bool
    reliable: bool
    reasons: tuple[str, ...]
    located: LocatedNativeLogs
    exit_code: int | None

    @property
    def started_count(self) -> int:
        return len(self.started_outcomes)

    def numbers_for(self, status: AccountStatus) -> tuple[int, ...]:
        return tuple(
            outcome.a_number
            for outcome in self.started_outcomes
            if outcome.status is status
        )


_STATUS_DEFINITIONS = (
    "已开始账号主状态：每个实际开始账号只计入一种主状态，六类主状态合计等于实际开始账号数。",
    "有新作品下载：日志最终统计的下载作品数大于 0。",
    "作品均被引擎跳过：筛选后有作品，但视频、图集和实况最终全部计入跳过。",
    "无符合条件作品：筛选处理后的作品数量为 0。",
    "私密账号：出现明确的私密账号提示。",
    "处理异常，需核对（已开始主状态）：账号块中出现无法确认已恢复的错误。",
    "处理中断：账号已经开始处理，但进程结束前未形成可确认的最终结果。",
    "进入处理前异常（不计入已开始主状态合计）：URL/sec_user_id 解析失败而未进入账号处理。",
    "未开始（不计入已开始主状态合计）：仅指本次任务启动时 enable=true 且 URL 有效、但在进程结束前尚未轮到的账号；本次 enable=false 或 URL 为空的账号不参与统计。",
    "完成但有异常记录（附加状态，可与主状态重叠，不计入主状态合计）：出现网络中断或重试，但之后仍产生完整的作品统计。",
    "结果不完整：用户停止、异常退出、日志截断、计划账号未全部完成，或其他证据不足导致无法确认完整任务结果。",
)


def format_summary_for_ui(summary: DownloadSummary) -> tuple[str, ...]:
    lines = [_format_process_outcome(summary.exit_code)]
    if not summary.reliable:
        if _has_traceable_partial_details(summary):
            lines.append(
                "账号结果：结果不完整；已保留可追溯部分明细；"
                "未开始、处理中断和身份不明部分不会作为智能跳过依据。"
                f"原因：{_safe_failure_reason(summary)}"
            )
            lines.extend(_format_account_result_lines(summary))
        else:
            lines.append(f"账号结果：无法可靠汇总；原因：{_safe_failure_reason(summary)}")
        return tuple(lines)

    lines.extend(_format_account_result_lines(summary))
    return tuple(lines)


def format_summary_for_task_log(
    summary: DownloadSummary, ended_at: datetime
) -> str:
    account_formula_valid, status_formula_valid = _validation_outcomes(summary)
    account_validation = _format_validation(summary.reliable, account_formula_valid)
    status_validation = _format_validation(summary.reliable, status_formula_valid)
    lines = [
        "【下载账号汇总】",
        f"进程结束时间：{ended_at:%Y-%m-%d %H:%M:%S}",
        f"退出码：{summary.exit_code}",
        f"日志定位方式：{summary.located.method}",
    ]
    if summary.located.segments:
        lines.extend(
            f"日志区间：{segment.path.resolve()}；偏移={segment.offset}；长度={segment.length}"
            for segment in summary.located.segments
        )
    else:
        lines.append("日志区间：无可靠区间")

    lines.extend(
        (
            "一级核对（计划账号=实际开始+进入处理前异常+未开始）："
            + account_validation,
            "二级核对（实际开始=各主状态之和）："
            + status_validation,
        )
    )
    if summary.reliable:
        lines.extend(_format_account_result_lines(summary))
        # V0.1.4 keeps the UI concise, but records a complete account-to-status
        # mapping in the existing task log.  The result history page and the
        # optional private-account filter read these lines directly; no second
        # database or cache is introduced.
        lines.append("账号明细版本：1")
        for label, status in (
            ("有新作品下载", AccountStatus.DOWNLOADED),
            ("作品均被引擎跳过", AccountStatus.ALL_SKIPPED),
            ("无符合条件作品", AccountStatus.NO_ELIGIBLE_WORKS),
        ):
            _append_number_line(lines, label, summary.numbers_for(status))
    else:
        if _has_traceable_partial_details(summary):
            lines.append(
                "账号结果：无法可靠汇总；已写入可追溯部分明细；"
                "未开始、处理中断和身份不明部分不会作为智能跳过依据。"
                f"原因：{_safe_failure_reason(summary)}"
            )
            lines.extend(_format_account_result_lines(summary))
            lines.append("账号明细版本：1")
            for label, status in (
                ("有新作品下载", AccountStatus.DOWNLOADED),
                ("作品均被引擎跳过", AccountStatus.ALL_SKIPPED),
                ("无符合条件作品", AccountStatus.NO_ELIGIBLE_WORKS),
            ):
                _append_number_line(lines, label, summary.numbers_for(status))
        else:
            lines.append(f"账号结果：无法可靠汇总；原因：{_safe_failure_reason(summary)}")
    lines.extend(("【状态说明】", *_STATUS_DEFINITIONS))
    return "\n".join(lines) + "\n"


def _format_process_outcome(exit_code: int | None) -> str:
    if exit_code == 0:
        return "下载进程：正常退出（退出码 0）"
    if exit_code is None:
        return "下载进程：退出状态未知"
    return f"下载进程：异常退出（退出码 {exit_code}）"


def _has_traceable_partial_details(summary: DownloadSummary) -> bool:
    """Return whether an incomplete run has safe, attributable account evidence.

    A non-empty parsed outcome is not sufficient on its own: the native log
    segment must have passed the strict locator checks, otherwise rows from a
    different run could be exposed as this task's results.  The explicit
    pre-start/not-started sets are also evidence that the frozen plan was
    actually compared with the located run, even when no account completed.
    """

    return bool(
        summary.located.reliable
        and summary.located.segments
        and (
            summary.started_outcomes
            or summary.pre_start_errors
            or summary.not_started
        )
    )


def _format_account_result_lines(summary: DownloadSummary) -> list[str]:
    status = "完整" if summary.complete else "结果不完整"
    started_error_numbers = summary.numbers_for(AccountStatus.ERROR)
    pre_start_error_numbers = summary.pre_start_errors
    lines = [
        f"账号汇总：{status}",
        f"计划账号：{summary.planned_count}",
        f"实际开始：{summary.started_count}",
        "【已开始账号主状态（互斥，以下六项合计=实际开始）】",
        f"有新作品下载：{summary.primary_status_counts[AccountStatus.DOWNLOADED]}",
        f"作品均被引擎跳过：{summary.primary_status_counts[AccountStatus.ALL_SKIPPED]}",
        f"无符合条件作品：{summary.primary_status_counts[AccountStatus.NO_ELIGIBLE_WORKS]}",
        f"私密账号：{summary.primary_status_counts[AccountStatus.PRIVATE]}",
        f"处理异常，需核对（已开始主状态）：{summary.primary_status_counts[AccountStatus.ERROR]}",
        f"处理中断：{summary.primary_status_counts[AccountStatus.INTERRUPTED]}",
        "【未进入处理的计划账号（不计入已开始主状态合计）】",
        f"进入处理前异常（不计入主状态合计）：{len(pre_start_error_numbers)}",
        f"未开始（不计入主状态合计）：{len(summary.not_started)}",
        "【附加状态（可与主状态重叠，不计入主状态合计）】",
        f"完成但有异常记录（附加状态，不计入主状态合计）：{len(summary.completed_with_anomaly)}",
    ]
    _append_number_line(lines, "私密账号", summary.numbers_for(AccountStatus.PRIVATE))
    _append_number_line(lines, "已开始后处理异常", started_error_numbers)
    _append_number_line(lines, "进入处理前异常", pre_start_error_numbers)
    _append_number_line(lines, "处理中断", summary.numbers_for(AccountStatus.INTERRUPTED))
    _append_number_line(lines, "未开始", summary.not_started)
    _append_number_line(lines, "附加状态：异常后完成", summary.completed_with_anomaly)
    lines.extend(
        f"原生日志：{segment.path.resolve()}" for segment in summary.located.segments
    )
    return lines


def _append_number_line(lines: list[str], label: str, numbers: tuple[int, ...]) -> None:
    if not numbers:
        return
    compact = compact_numbers(numbers).replace(",", "、")
    lines.append(f"{label}（{len(set(numbers))}）：{compact}")


def _validation_outcomes(summary: DownloadSummary) -> tuple[bool, bool]:
    account_formula_valid = (
        summary.planned_count
        == summary.started_count + len(summary.pre_start_errors) + len(summary.not_started)
    )
    status_formula_valid = summary.started_count == sum(
        summary.primary_status_counts.values()
    )
    return account_formula_valid, status_formula_valid


def _format_validation(reliable: bool, valid: bool) -> str:
    if not reliable:
        return "无法验证"
    return "通过" if valid else "未通过"


def _safe_failure_reason(summary: DownloadSummary) -> str:
    reason_text = " ".join(summary.reasons).casefold()
    if "多个候选" in reason_text or "唯一" in reason_text:
        return "无法唯一确定本次原生日志。"
    if "未找到" in reason_text or "缺失" in reason_text:
        return "未找到本次原生日志。"
    if "读取" in reason_text or "编码" in reason_text or "截断" in reason_text:
        return "原生日志读取不完整。"
    if "映射" in reason_text or "编号" in reason_text or "序号" in reason_text:
        return "账号身份校验未通过。"
    if "核对公式" in reason_text:
        return "账号统计校验未通过。"
    return "汇总证据不可靠。"


@dataclass
class _AccountBlock:
    task_index: int
    mapped: PlannedAccount | None
    expected_cleaned_mark: str | None = None
    logged_a_number: int | None = None
    logged_mark_mismatch: bool = False
    filtered_count: int | None = None
    downloaded_video: int | None = None
    downloaded_gallery: int | None = None
    downloaded_live: int | None = None
    skipped_video: int | None = None
    skipped_gallery: int | None = None
    skipped_live: int | None = None
    private: bool = False
    anomaly_signal: bool = False
    unrecovered_error: bool = False
    statistics_conflict: bool = False
    identity_tokens: set[str] = field(default_factory=set)

    def set_total(self, category: str, downloaded: bool, value: int) -> None:
        field = ("downloaded_" if downloaded else "skipped_") + category
        previous = getattr(self, field)
        if previous is not None and previous != value:
            self.statistics_conflict = True
        setattr(self, field, value)

    def clear_final_statistics(self) -> None:
        self.filtered_count = None
        self.downloaded_video = None
        self.downloaded_gallery = None
        self.downloaded_live = None
        self.skipped_video = None
        self.skipped_gallery = None
        self.skipped_live = None
        self.statistics_conflict = False

    @property
    def all_final_totals_present(self) -> bool:
        totals = (
            self.downloaded_video,
            self.downloaded_gallery,
            self.downloaded_live,
            self.skipped_video,
            self.skipped_gallery,
            self.skipped_live,
        )
        return self.filtered_count is not None and all(
            value is not None for value in totals
        )

    @property
    def final_statistics_complete(self) -> bool:
        if self.statistics_conflict:
            return False
        totals = (
            self.downloaded_video,
            self.downloaded_gallery,
            self.downloaded_live,
            self.skipped_video,
            self.skipped_gallery,
            self.skipped_live,
        )
        if self.filtered_count == 0:
            return all(value in (None, 0) for value in totals)
        return (
            self.filtered_count is not None
            and all(value is not None for value in totals)
            and sum(value or 0 for value in totals) == self.filtered_count
        )

    @property
    def downloaded_total(self) -> int:
        return sum(
            value or 0
            for value in (
                self.downloaded_video,
                self.downloaded_gallery,
                self.downloaded_live,
            )
        )

    @property
    def skipped_total(self) -> int:
        return sum(
            value or 0
            for value in (
                self.skipped_video,
                self.skipped_gallery,
                self.skipped_live,
            )
        )


_START_RE = re.compile(r"开始处理第\s*(\d+)\s*个账号")
_LOGGED_MARK_RE = re.compile(r"标识：\s*([^；;\r\n]+)")
_FILTERED_RE = re.compile(r"筛选处理后作品数量:\s*(\d+)")
_TOTAL_RE = re.compile(r"(下载|跳过)(视频|图集|实况)作品\s*(\d+)\s*个")
_PRE_START_MARK_RE = re.compile(
    r"['\"]mark['\"]\s*:\s*['\"]([^'\"]+)['\"]", re.IGNORECASE
)
_RUN_ANCHOR_RE = re.compile(r"共有\s*\d+\s*个账号的作品等待下载")
_CATEGORY_NAMES = {"视频": "video", "图集": "gallery", "实况": "live"}
_READ_CHUNK_SIZE = 64 * 1024
_MAX_BUFFERED_LINE_SIZE = 1024 * 1024
_UTF8_BOM = b"\xef\xbb\xbf"


def _cleaned_mark_alias(mark: str) -> str:
    """Mirror only the engine transformations observed in account logs.

    The engine removes control characters and presentation selectors, collapses
    whitespace, and strips outer ASCII full stops before displaying a mark.
    Other changes (such as an unexpected nickname) remain untrusted.
    This alias is for comparison only; the frozen mark is never rewritten.
    """

    text = re.sub(r"[\x00-\x1f\x7f]", "", mark)
    text = text.replace("\ufe0e", "").replace("\ufe0f", "")
    return " ".join(text.split()).strip(".")


def _unique_cleaned_mark_aliases(
    planned_accounts: tuple[PlannedAccount, ...],
) -> dict[int, str]:
    candidates = [
        (account, _cleaned_mark_alias(account.mark)) for account in planned_accounts
    ]
    owners: dict[str, set[int]] = {}
    for account, alias in candidates:
        for value in {account.mark, alias}:
            owners.setdefault(value, set()).add(account.task_index)
    return {
        account.task_index: alias
        for account, alias in candidates
        if alias
        and alias != account.mark
        and owners[alias] == {account.task_index}
    }


def freeze_planned_accounts(document: dict) -> tuple[PlannedAccount, ...]:
    accounts = document.get("accounts_urls")
    if not isinstance(accounts, list):
        raise SummaryInputError("settings.json 缺少 accounts_urls 数组。")
    planned: list[PlannedAccount] = []
    for position, raw in enumerate(accounts, start=1):
        if not isinstance(raw, dict):
            raise SummaryInputError(f"settings.json 的 A{position} 不是对象。")
        url = raw.get("url", "")
        url = url.strip() if isinstance(url, str) else ""
        if not raw.get("enable", True) or not url:
            continue
        mark = raw.get("mark", "")
        if not isinstance(mark, str) or not mark.strip():
            raise SummaryInputError(f"settings.json 的 A{position} 缺少有效 mark。")
        mark = mark.strip()
        planned.append(PlannedAccount(len(planned) + 1, position, mark))
    if not planned:
        raise SummaryInputError("settings.json 没有启用且 URL 有效的账号。")
    return tuple(planned)


def snapshot_native_logs(log_dir: Path) -> tuple[NativeLogState, ...]:
    if not log_dir.is_dir():
        return ()
    states = []
    for path in sorted(log_dir.glob("*.log"), key=lambda item: item.name.casefold()):
        try:
            stat = path.stat()
        except OSError:
            continue
        states.append(NativeLogState(path.resolve(), stat.st_size, stat.st_mtime_ns))
    return tuple(states)


def locate_native_logs(
    before: tuple[NativeLogState, ...],
    log_dir: Path,
    started_at: datetime,
    ended_at: datetime,
    planned_count: int,
    planned_accounts: tuple[PlannedAccount, ...] = (),
) -> LocatedNativeLogs:
    before_by_path = {
        str(state.path.resolve()).casefold(): state
        for state in before
    }
    earliest_mtime = (started_at - timedelta(seconds=2)).timestamp()
    latest_mtime = (ended_at + timedelta(seconds=2)).timestamp()
    candidate_records: list[tuple[int, str, NativeLogSegment]] = []
    if log_dir.is_dir():
        for path in log_dir.glob("*.log"):
            try:
                resolved = path.resolve()
                stat = resolved.stat()
            except OSError:
                continue
            if stat.st_mtime < earliest_mtime or stat.st_mtime > latest_mtime:
                continue
            state = before_by_path.get(str(resolved).casefold())
            if state is None:
                segment = NativeLogSegment(resolved, 0, stat.st_size)
            elif stat.st_size > state.size:
                segment = NativeLogSegment(
                    resolved, state.size, stat.st_size - state.size
                )
            else:
                continue
            candidate_records.append(
                (stat.st_mtime_ns, resolved.name.casefold(), segment)
            )

    candidate_records.sort(key=lambda item: (item[0], item[1]))
    candidates = [item[2] for item in candidate_records]

    if not candidates:
        return LocatedNativeLogs((), "size-delta", False, "未找到本次新增或增长的原生日志。")

    anchor = f"共有 {planned_count} 个账号的作品等待下载"
    evaluations = [_evaluate_log_segment(segment, anchor) for segment in candidates]
    anchor_indices = [index for index, evaluation in enumerate(evaluations) if evaluation]
    if len(anchor_indices) == 1:
        first_index = anchor_indices[0]
        segments = _select_run_segments(
            candidates,
            first_index,
            planned_count,
            planned_accounts,
        )
        if segments is not None:
            return LocatedNativeLogs(segments, "size-delta-anchor", True)
    if len(candidates) == 1:
        return LocatedNativeLogs(
            tuple(candidates),
            "size-delta-anchor",
            False,
            "本次原生日志缺少运行锚点。",
        )
    return LocatedNativeLogs(
        (),
        "size-delta-anchor",
        False,
        "存在多个候选原生日志，无法唯一确定本次日志。",
    )


def _evaluate_log_segment(
    segment: NativeLogSegment, planned_count_anchor: str
) -> bool:
    try:
        with segment.path.open("rb") as handle:
            handle.seek(segment.offset)
            content = handle.read(min(segment.length, 64 * 1024)).decode(
                "utf-8-sig", errors="replace"
            )
    except OSError:
        return False
    return planned_count_anchor in content


def _select_run_segments(
    candidates: list[NativeLogSegment],
    first_index: int,
    planned_count: int,
    planned_accounts: tuple[PlannedAccount, ...],
) -> tuple[NativeLogSegment, ...] | None:
    selected = [candidates[first_index]]
    try:
        events = _segment_task_events(candidates[first_index], planned_accounts)
        if not _events_continue_after(events, 0, planned_count):
            return None
        last_event = events[-1] if events else 0

        for segment in candidates[first_index + 1 :]:
            if last_event >= planned_count or _segment_has_run_anchor(segment):
                break
            continuation = _segment_task_events(segment, planned_accounts)
            if not continuation:
                continue
            if not _events_continue_after(continuation, last_event, planned_count):
                return None
            selected.append(segment)
            last_event = continuation[-1]
    except (OSError, UnicodeError, _LogReadError):
        return None
    return tuple(selected)


def _segment_has_run_anchor(segment: NativeLogSegment) -> bool:
    try:
        with segment.path.open("rb") as handle:
            handle.seek(segment.offset)
            content = handle.read(min(segment.length, 64 * 1024)).decode(
                "utf-8-sig", errors="replace"
            )
    except OSError:
        return False
    return _RUN_ANCHOR_RE.search(content) is not None


def _segment_task_events(
    segment: NativeLogSegment,
    planned_accounts: tuple[PlannedAccount, ...],
) -> tuple[int, ...]:
    events: list[int] = []
    for line in _iter_segment_lines(segment):
        start_match = _START_RE.search(line)
        if start_match:
            events.append(int(start_match.group(1)))
            continue
        if "提取 sec_user_id 失败，错误配置：" not in line:
            continue
        mark_match = _PRE_START_MARK_RE.search(line)
        if not mark_match:
            continue
        planned = _match_pre_start_mark(
            mark_match.group(1).strip(), planned_accounts
        )
        if planned is not None:
            events.append(planned.task_index)
    return tuple(events)


def _events_continue_after(
    events: tuple[int, ...], previous: int, planned_count: int
) -> bool:
    if not events:
        return True
    expected = tuple(range(previous + 1, previous + 1 + len(events)))
    return events == expected and events[-1] <= planned_count


def parse_download_summary(
    planned_accounts: tuple[PlannedAccount, ...],
    located: LocatedNativeLogs,
    exit_code: int | None,
    *,
    context=None,
) -> DownloadSummary:
    plan_by_index = {account.task_index: account for account in planned_accounts}
    cleaned_mark_aliases = _unique_cleaned_mark_aliases(planned_accounts)
    reasons: list[str] = []
    started_outcomes: list[AccountOutcome] = []
    started_indices: set[int] = set()
    observed_started_indices: set[int] = set()
    pre_start_indices: set[int] = set()
    current: _AccountBlock | None = None
    parser_aborted = False
    last_event_index = 0

    if not located.reliable:
        _add_reason(reasons, located.reason or "原生日志定位不可靠。")

    def finalize(
        block: _AccountBlock,
        *,
        force_interrupted: bool = False,
        allow_trailing_unmarked_interrupted: bool = False,
    ) -> None:
        mapped = block.mapped
        if mapped is None or block.task_index in started_indices:
            return
        identity_mismatch = block.logged_mark_mismatch or (
            block.logged_a_number is not None
            and block.logged_a_number != mapped.a_number
        )
        if identity_mismatch:
            _add_reason(
                reasons,
                f"任务序号 {block.task_index} 的日志 A 编号与冻结映射不一致。",
            )
            return
        observed_total = block.downloaded_total + block.skipped_total
        totals_exceed_filtered = (
            block.filtered_count is not None
            and observed_total > block.filtered_count
        )
        complete_totals_disagree = (
            block.all_final_totals_present
            and observed_total != block.filtered_count
        )
        if (
            block.statistics_conflict
            or totals_exceed_filtered
            or complete_totals_disagree
        ):
            _add_reason(reasons, f"任务序号 {block.task_index} 的最终统计不一致。")
            return
        status = (
            AccountStatus.INTERRUPTED if force_interrupted else _classify_block(block)
        )
        frozen_interrupted_identity = (
            allow_trailing_unmarked_interrupted
            and status is AccountStatus.INTERRUPTED
            and exit_code is not None
            and exit_code != 0
        )
        if (
            status is not AccountStatus.PRIVATE
            and block.logged_a_number is None
            and not frozen_interrupted_identity
        ):
            _add_reason(
                reasons,
                f"任务序号 {block.task_index} 缺少可验证的日志 A 编号。",
            )
            return
        completed_with_anomaly = (
            block.anomaly_signal
            and status
            in (
                AccountStatus.DOWNLOADED,
                AccountStatus.ALL_SKIPPED,
                AccountStatus.NO_ELIGIBLE_WORKS,
            )
        )
        started_indices.add(block.task_index)
        started_outcomes.append(
            AccountOutcome(
                block.task_index,
                mapped.a_number,
                status,
                completed_with_anomaly,
                tuple(sorted(block.identity_tokens)),
            )
        )

    try:
        lines = (
            _iter_located_lines(located.segments)
            if context is None
            else _iter_located_lines(located.segments, context=context)
        )
        for line in lines:
            if context is not None:
                context.raise_if_cancelled()
            start_match = _START_RE.search(line)
            if start_match:
                if current is not None:
                    finalize(current)
                task_index = int(start_match.group(1))
                if task_index not in plan_by_index:
                    _add_reason(reasons, f"日志任务序号 {task_index} 越界。")
                elif task_index in observed_started_indices:
                    _add_reason(reasons, f"日志任务序号 {task_index} 重复。")
                elif task_index in pre_start_indices:
                    _add_reason(
                        reasons,
                        f"任务序号 {task_index} 同时被标记为启动前错误和已开始。",
                    )
                if task_index <= last_event_index:
                    _add_reason(reasons, f"日志任务序号 {task_index} 顺序异常。")
                last_event_index = max(last_event_index, task_index)
                observed_started_indices.add(task_index)
                current = _AccountBlock(
                    task_index,
                    plan_by_index.get(task_index),
                    expected_cleaned_mark=cleaned_mark_aliases.get(task_index),
                )
                continue

            if "提取 sec_user_id 失败，错误配置：" in line:
                mark_match = _PRE_START_MARK_RE.search(line)
                if mark_match:
                    logged_mark = mark_match.group(1).strip()
                    planned = _match_pre_start_mark(logged_mark, planned_accounts)
                    if planned is None:
                        _add_reason(reasons, "启动前错误的 A 编号不在冻结计划中。")
                    elif planned.task_index in pre_start_indices:
                        _add_reason(
                            reasons,
                            f"任务序号 {planned.task_index} 的启动前错误重复。",
                        )
                    elif planned.task_index in started_indices or (
                        current is not None
                        and current.task_index == planned.task_index
                    ):
                        _add_reason(
                            reasons,
                            f"任务序号 {planned.task_index} 同时被标记为启动前错误和已开始。",
                        )
                    else:
                        if planned.task_index <= last_event_index:
                            _add_reason(reasons, f"日志任务序号 {planned.task_index} 顺序异常。")
                        pre_start_indices.add(planned.task_index)
                        last_event_index = max(last_event_index, planned.task_index)
                else:
                    _add_reason(reasons, "启动前错误缺少可识别的严格 A 编号。")
                continue

            if current is not None:
                _consume_account_line(current, line)
        if current is not None:
            finalize(
                current,
                allow_trailing_unmarked_interrupted=(
                    exit_code is not None and exit_code != 0
                ),
            )
    except _LogReadError as exc:
        _add_reason(reasons, str(exc))
        parser_aborted = True
        if current is not None:
            finalize(current, force_interrupted=True)
    except (OSError, UnicodeError) as exc:
        _add_reason(reasons, f"读取原生日志失败：{type(exc).__name__}。")
        parser_aborted = True
        if current is not None:
            finalize(current, force_interrupted=True)

    started_outcomes.sort(key=lambda outcome: outcome.task_index)
    pre_start_indices.difference_update(started_indices)
    highest_observed = max(observed_started_indices | pre_start_indices, default=0)
    remaining_indices = (
        set(plan_by_index) - observed_started_indices - pre_start_indices
    )
    interior_missing = sorted(
        task_index
        for task_index in remaining_indices
        if task_index < highest_observed
    )
    if interior_missing:
        _add_reason(
            reasons,
            "存在无法解释的内部缺失任务序号："
            + "、".join(str(task_index) for task_index in interior_missing)
            + "。",
        )
    not_started_indices = (
        sorted(
            task_index
            for task_index in remaining_indices
            if task_index > highest_observed
        )
        if located.reliable and not parser_aborted
        else []
    )

    counts = {status: 0 for status in AccountStatus}
    for outcome in started_outcomes:
        counts[outcome.status] += 1

    account_formula_valid = (
        len(planned_accounts)
        == len(started_outcomes)
        + len(pre_start_indices)
        + len(not_started_indices)
    )
    status_formula_valid = len(started_outcomes) == sum(counts.values())
    if not account_formula_valid:
        _add_reason(reasons, "计划账号核对公式不成立。")
    if not status_formula_valid:
        _add_reason(reasons, "已开始账号状态核对公式不成立。")
    reliable = located.reliable and not reasons
    if exit_code is None:
        _add_reason(reasons, "下载进程退出状态未知。")
    elif exit_code != 0:
        _add_reason(reasons, f"下载进程非零退出：{exit_code}。")

    interrupted = counts[AccountStatus.INTERRUPTED] > 0
    complete = (
        reliable
        and exit_code == 0
        and not interrupted
        and not not_started_indices
        and account_formula_valid
        and status_formula_valid
    )
    return DownloadSummary(
        planned_count=len(planned_accounts),
        started_outcomes=tuple(started_outcomes),
        pre_start_errors=tuple(
            plan_by_index[index].a_number for index in sorted(pre_start_indices)
        ),
        not_started=tuple(
            plan_by_index[index].a_number for index in not_started_indices
        ),
        primary_status_counts=counts,
        completed_with_anomaly=tuple(
            outcome.a_number
            for outcome in started_outcomes
            if outcome.completed_with_anomaly
        ),
        complete=complete,
        reliable=reliable,
        reasons=tuple(reasons),
        located=located,
        exit_code=exit_code,
    )


def _consume_account_line(
    block: _AccountBlock,
    line: str,
) -> None:
    block.identity_tokens.update(log_identity_tokens(line))
    mark_match = _LOGGED_MARK_RE.search(line)
    if mark_match:
        logged_mark = mark_match.group(1).strip()
        if block.mapped is not None and (
            logged_mark == block.mapped.mark
            or logged_mark == block.expected_cleaned_mark
        ):
            block.logged_a_number = block.mapped.a_number
        else:
            block.logged_mark_mismatch = True

    filtered_match = _FILTERED_RE.search(line)
    if filtered_match:
        value = int(filtered_match.group(1))
        if block.filtered_count is not None and block.filtered_count != value:
            block.statistics_conflict = True
        block.filtered_count = value

    total_match = _TOTAL_RE.search(line)
    if total_match:
        block.set_total(
            _CATEGORY_NAMES[total_match.group(2)],
            total_match.group(1) == "下载",
            int(total_match.group(3)),
        )
        if block.unrecovered_error and block.all_final_totals_present:
            block.unrecovered_error = False

    if "该账号为私密账号" in line:
        block.private = True
    is_warning = "[WARNING]" in line or "重试" in line or "下载中断" in line
    is_error = "[ERROR]" in line
    block.anomaly_signal = block.anomaly_signal or is_warning or is_error
    if is_error:
        block.clear_final_statistics()
        block.unrecovered_error = True


def _classify_block(block: _AccountBlock) -> AccountStatus:
    if block.private:
        return AccountStatus.PRIVATE
    if block.unrecovered_error:
        return AccountStatus.ERROR
    if not block.final_statistics_complete:
        return AccountStatus.INTERRUPTED
    if block.downloaded_total > 0:
        return AccountStatus.DOWNLOADED
    if (
        block.filtered_count is not None
        and block.filtered_count > 0
        and block.skipped_total == block.filtered_count
    ):
        return AccountStatus.ALL_SKIPPED
    if block.filtered_count == 0:
        return AccountStatus.NO_ELIGIBLE_WORKS
    return AccountStatus.INTERRUPTED


def _match_pre_start_mark(
    logged_mark: str,
    planned_accounts: tuple[PlannedAccount, ...],
) -> PlannedAccount | None:
    exact = [account for account in planned_accounts if account.mark == logged_mark]
    return exact[0] if len(exact) == 1 else None


def _iter_located_lines(
    segments: tuple[NativeLogSegment, ...], *, context=None
) -> Iterator[str]:
    for segment in segments:
        if context is not None:
            context.raise_if_cancelled()
        if context is None:
            yield from _iter_segment_lines(segment)
        else:
            yield from _iter_segment_lines(segment, context=context)


def _iter_segment_lines(segment: NativeLogSegment, *, context=None) -> Iterator[str]:
    with segment.path.open("rb") as handle:
        start = max(segment.offset, 0)
        remaining = max(segment.length, 0)
        discard_partial = False
        if start > 0:
            if start == len(_UTF8_BOM):
                handle.seek(0)
                starts_after_bom = handle.read(len(_UTF8_BOM)) == _UTF8_BOM
                if not starts_after_bom:
                    handle.seek(start - 1)
                    previous = handle.read(1)
                    discard_partial = previous not in (b"\n", b"\r")
            else:
                handle.seek(start - 1)
                previous = handle.read(1)
                discard_partial = previous not in (b"\n", b"\r")
        handle.seek(start)
        if discard_partial:
            while remaining > 0:
                if context is not None:
                    context.raise_if_cancelled()
                chunk = handle.read(min(_READ_CHUNK_SIZE, remaining))
                if not chunk:
                    raise _LogReadError("原生日志在声明片段结束前提前结束。")
                newline_index = chunk.find(b"\n")
                if newline_index < 0:
                    remaining -= len(chunk)
                    continue
                consumed = newline_index + 1
                remaining -= consumed
                handle.seek(consumed - len(chunk), 1)
                break
        decoder = getincrementaldecoder("utf-8-sig")(errors="replace")
        buffer = ""
        while remaining > 0:
            if context is not None:
                context.raise_if_cancelled()
            chunk = handle.read(min(_READ_CHUNK_SIZE, remaining))
            if not chunk:
                raise _LogReadError("原生日志在声明片段结束前提前结束。")
            remaining -= len(chunk)
            decoded = decoder.decode(chunk, final=False)
            buffer += decoded
            while "\n" in buffer:
                line, buffer = buffer.split("\n", 1)
                if len(line.encode("utf-8")) > _MAX_BUFFERED_LINE_SIZE:
                    raise _LogReadError("原生日志单行长度超过解析上限。")
                if "\ufffd" in line:
                    raise _LogReadError("原生日志包含无法解码的 UTF-8 编码。")
                yield line.rstrip("\r")
            if "\ufffd" in buffer:
                raise _LogReadError("原生日志包含无法解码的 UTF-8 编码。")
            if len(buffer.encode("utf-8")) > _MAX_BUFFERED_LINE_SIZE:
                raise _LogReadError("原生日志单行长度超过解析上限。")
        buffer += decoder.decode(b"", final=True)
        if "\ufffd" in buffer:
            raise _LogReadError("原生日志包含无法解码的 UTF-8 编码。")
        if buffer:
            raise _LogReadError("原生日志最后一行未换行，记录不完整。")


def _add_reason(reasons: list[str], reason: str) -> None:
    if reason and reason not in reasons:
        reasons.append(reason)
