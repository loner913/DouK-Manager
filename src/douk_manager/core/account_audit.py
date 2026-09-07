"""Pure account-audit models and evidence evaluation.

This module deliberately contains no persistence, network, or GUI behavior.
Checkpoint 03-A only turns already-classified, read-only history into audit
evidence and conservative suggestions.  Applying a suggestion is a separate,
explicit operation implemented by later checkpoints.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import TYPE_CHECKING, AbstractSet, Iterable, Mapping

from douk_manager.core.download_summary import AccountStatus
from douk_manager.core.result_history import AccountHistoryRow, ResultHistoryService

if TYPE_CHECKING:
    from douk_manager.operation import OperationContext


DEFAULT_CONSECUTIVE_ERROR_THRESHOLD = 5
DEFAULT_MINIMUM_EVIDENCE_RUNS = 3
# No stable account-removed/banned native-log marker has been approved yet.
# ERROR streaks therefore remain request_failed unless a later, explicit
# read-only evidence source supplies an unavailable account number.
UNAVAILABLE_MARKERS: tuple[str, ...] = ()
UNCERTAIN_EARLY_HISTORY_WARNING = (
    "存在无法确认 A 编号对应关系的早期历史记录；"
    "未配置证据起始时间点时不作猜测性排除。"
)
NATIVE_LOG_DISABLED_WARNING = "未启用原生日志分析，可达性判定仅基于任务汇总。"


class IdentityState(str, Enum):
    CONFIRMED = "confirmed"
    CONFLICT = "conflict"
    DUPLICATE = "duplicate"
    UNRESOLVED = "unresolved"


class ReachabilityState(str, Enum):
    REACHABLE = "reachable"
    UNAVAILABLE = "unavailable"
    REQUEST_FAILED = "request_failed"
    UNKNOWN = "unknown"


class PrivacyState(str, Enum):
    PUBLIC = "public"
    PRIVATE = "private"
    UNKNOWN = "unknown"


class Disposition(str, Enum):
    ENABLED = "enabled"
    PERMANENTLY_DISABLED = "permanently_disabled"
    PENDING_REVIEW = "pending_review"


class Suggestion(str, Enum):
    KEEP = "KEEP"
    REVIEW = "REVIEW"
    SUGGEST_DISABLE = "SUGGEST_DISABLE"
    SUGGEST_REENABLE = "SUGGEST_REENABLE"
    NO_EVIDENCE = "NO_EVIDENCE"


@dataclass(frozen=True)
class IdentityObservation:
    """An internal identity token observed for one array position.

    Tokens are used only for equality.  They never appear in audit entries,
    reasons, or persisted audit state.
    """

    a_number: int
    account_id: str


@dataclass(frozen=True)
class IdentityAssessment:
    state: IdentityState
    duplicate_group: int | None = None


@dataclass(frozen=True)
class AccountEvidence:
    a_number: int
    runs_seen: int
    last_run_stamp: str | None
    status_counts: Mapping[AccountStatus, int]
    consecutive_error_runs: int
    consecutive_private_runs: int
    consecutive_no_eligible_runs: int
    last_downloaded_run: str | None
    consecutive_error_run_ids: tuple[str, ...]
    last_classified_status: AccountStatus | None

    @property
    def classified_runs(self) -> int:
        return sum(self.status_counts.values())


@dataclass(frozen=True)
class AccountAuditEntry:
    a_number: int
    identity: IdentityState
    reachability: ReachabilityState
    privacy: PrivacyState
    disposition: Disposition
    master_enable: bool
    suggestion: Suggestion
    suggestion_reason: str
    evidence: AccountEvidence
    duplicate_group: int | None = None


@dataclass(frozen=True)
class AccountAuditReport:
    generated_at: datetime
    master_sha256: str
    total_accounts: int
    entries: tuple[AccountAuditEntry, ...]
    runs_scanned: int
    oldest_run: str | None
    newest_run: str | None
    duplicate_groups: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class AuditDecision:
    a_number: int
    disposition: Disposition
    reason: str = ""


@dataclass(frozen=True)
class AuditApplyPreview:
    to_disable: tuple[int, ...]
    to_enable: tuple[int, ...]
    to_pending: tuple[int, ...]
    unchanged: int
    enabled_after: int
    array_length_before: int
    array_length_after: int


class AccountAuditService:
    """Read-only checkpoint-03-A orchestration over result history."""

    def __init__(self, history_service: ResultHistoryService) -> None:
        self.history_service = history_service

    def build_entries(
        self,
        master_enable_by_number: Mapping[int, bool],
        *,
        evidence_since: datetime | None = None,
        identity_observations: Iterable[IdentityObservation] = (),
        explicitly_unavailable: AbstractSet[int] = frozenset(),
        error_threshold: int = DEFAULT_CONSECUTIVE_ERROR_THRESHOLD,
        minimum_evidence_runs: int = DEFAULT_MINIMUM_EVIDENCE_RUNS,
        context: OperationContext | None = None,
    ) -> tuple[AccountAuditEntry, ...]:
        numbers = tuple(sorted(master_enable_by_number))
        history = self.history_service.account_rows_by_number(
            numbers=numbers,
            evidence_since=evidence_since,
            context=context,
        )
        return build_audit_entries(
            master_enable_by_number,
            history,
            identity_observations=identity_observations,
            explicitly_unavailable=explicitly_unavailable,
            error_threshold=error_threshold,
            minimum_evidence_runs=minimum_evidence_runs,
        )

    def build_report(
        self,
        master_enable_by_number: Mapping[int, bool],
        *,
        master_sha256: str,
        generated_at: datetime | None = None,
        evidence_since: datetime | None = None,
        native_log_analysis: bool = False,
        identity_observations: Iterable[IdentityObservation] = (),
        explicitly_unavailable: AbstractSet[int] = frozenset(),
        error_threshold: int = DEFAULT_CONSECUTIVE_ERROR_THRESHOLD,
        minimum_evidence_runs: int = DEFAULT_MINIMUM_EVIDENCE_RUNS,
        context: OperationContext | None = None,
    ) -> AccountAuditReport:
        """Build a read-only report from one historical scan."""

        if native_log_analysis:
            raise NotImplementedError(
                "native-log analysis is outside checkpoint 03-A"
            )
        numbers = tuple(sorted(master_enable_by_number))
        snapshot = self.history_service.account_audit_snapshot(
            numbers=numbers,
            evidence_since=evidence_since,
            context=context,
        )
        entries = build_audit_entries(
            master_enable_by_number,
            snapshot.rows_by_number,
            identity_observations=identity_observations,
            explicitly_unavailable=explicitly_unavailable,
            error_threshold=error_threshold,
            minimum_evidence_runs=minimum_evidence_runs,
        )
        warnings = [UNCERTAIN_EARLY_HISTORY_WARNING]
        if evidence_since is not None:
            warnings.append(
                "证据起始时间点之前的历史记录已排除，不作为永久停用依据。"
            )
        warnings.append(NATIVE_LOG_DISABLED_WARNING)
        groups = {
            entry.duplicate_group
            for entry in entries
            if entry.duplicate_group is not None
        }
        return AccountAuditReport(
            generated_at=generated_at or datetime.now(),
            master_sha256=master_sha256,
            total_accounts=len(entries),
            entries=entries,
            runs_scanned=snapshot.runs_scanned,
            oldest_run=_format_run_time(snapshot.oldest_run),
            newest_run=_format_run_time(snapshot.newest_run),
            duplicate_groups=len(groups),
            warnings=tuple(warnings),
        )


_SAFE_TASK_NAME = re.compile(r"DownloadTask_[0-9A-Za-z_-]+\.log$", re.IGNORECASE)
_SKIPPED_STATUSES = frozenset(
    (AccountStatus.INTERRUPTED, "pre_start_error", "not_started")
)
_PUBLIC_STATUSES = frozenset(
    (
        AccountStatus.DOWNLOADED,
        AccountStatus.ALL_SKIPPED,
        AccountStatus.NO_ELIGIBLE_WORKS,
    )
)


def classify_identities(
    account_numbers: Iterable[int],
    observations: Iterable[IdentityObservation],
) -> dict[int, IdentityAssessment]:
    """Classify identity conflicts and duplicates without retaining IDs."""

    numbers = tuple(sorted(set(account_numbers)))
    number_set = set(numbers)
    ids_by_number: dict[int, set[str]] = {number: set() for number in numbers}
    for observation in observations:
        token = observation.account_id.strip()
        if observation.a_number in number_set and token:
            ids_by_number[observation.a_number].add(token)

    stable_id_by_number = {
        number: next(iter(tokens))
        for number, tokens in ids_by_number.items()
        if len(tokens) == 1
    }
    numbers_by_id: dict[str, list[int]] = defaultdict(list)
    for number, token in stable_id_by_number.items():
        numbers_by_id[token].append(number)
    duplicate_sets = sorted(
        (tuple(sorted(group)) for group in numbers_by_id.values() if len(group) >= 2)
    )
    duplicate_group_by_number = {
        number: group_index
        for group_index, group in enumerate(duplicate_sets, start=1)
        for number in group
    }

    result: dict[int, IdentityAssessment] = {}
    for number in numbers:
        tokens = ids_by_number[number]
        if len(tokens) > 1:
            result[number] = IdentityAssessment(IdentityState.CONFLICT)
        elif number in duplicate_group_by_number:
            result[number] = IdentityAssessment(
                IdentityState.DUPLICATE, duplicate_group_by_number[number]
            )
        elif len(tokens) == 1:
            result[number] = IdentityAssessment(IdentityState.CONFIRMED)
        else:
            result[number] = IdentityAssessment(IdentityState.UNRESOLVED)
    return result


def summarize_account_history(
    a_number: int,
    rows: Iterable[AccountHistoryRow],
) -> AccountEvidence:
    """Summarize classified history using the approved three-way streak rule."""

    ordered = tuple(
        sorted(
            (row for row in rows if row.a_number == a_number),
            key=lambda row: (row.ended_at, row.task_log.name),
        )
    )
    counts: Counter[AccountStatus] = Counter(
        row.status for row in ordered if isinstance(row.status, AccountStatus)
    )
    frozen_counts = MappingProxyType(
        {status: counts.get(status, 0) for status in AccountStatus}
    )
    classified = tuple(
        row for row in ordered if isinstance(row.status, AccountStatus)
    )
    error_rows = _trailing_streak(ordered, AccountStatus.ERROR)
    private_rows = _trailing_streak(ordered, AccountStatus.PRIVATE)
    no_eligible_rows = _trailing_streak(ordered, AccountStatus.NO_ELIGIBLE_WORKS)
    downloaded = tuple(
        row for row in ordered if row.status is AccountStatus.DOWNLOADED
    )
    return AccountEvidence(
        a_number=a_number,
        runs_seen=len(ordered),
        last_run_stamp=_safe_run_identifier(ordered[-1]) if ordered else None,
        status_counts=frozen_counts,
        consecutive_error_runs=len(error_rows),
        consecutive_private_runs=len(private_rows),
        consecutive_no_eligible_runs=len(no_eligible_rows),
        last_downloaded_run=(
            _safe_run_identifier(downloaded[-1]) if downloaded else None
        ),
        consecutive_error_run_ids=tuple(
            _safe_run_identifier(row) for row in error_rows
        ),
        last_classified_status=(classified[-1].status if classified else None),
    )


def build_audit_entries(
    master_enable_by_number: Mapping[int, bool],
    history_by_number: Mapping[int, Iterable[AccountHistoryRow]],
    *,
    identity_observations: Iterable[IdentityObservation] = (),
    explicitly_unavailable: AbstractSet[int] = frozenset(),
    error_threshold: int = DEFAULT_CONSECUTIVE_ERROR_THRESHOLD,
    minimum_evidence_runs: int = DEFAULT_MINIMUM_EVIDENCE_RUNS,
) -> tuple[AccountAuditEntry, ...]:
    """Build immutable audit entries without changing files or input objects."""

    if error_threshold < 1:
        raise ValueError("error_threshold must be at least 1")
    if minimum_evidence_runs < 1:
        raise ValueError("minimum_evidence_runs must be at least 1")
    account_numbers = tuple(sorted(master_enable_by_number))
    identities = classify_identities(account_numbers, identity_observations)
    entries: list[AccountAuditEntry] = []
    for number in account_numbers:
        evidence = summarize_account_history(number, history_by_number.get(number, ()))
        identity = identities[number]
        reachability = _derive_reachability(
            evidence, unavailable=number in explicitly_unavailable
        )
        privacy = _derive_privacy(evidence)
        disposition = (
            Disposition.ENABLED
            if bool(master_enable_by_number[number])
            else Disposition.PERMANENTLY_DISABLED
        )
        suggestion, reason = _derive_suggestion(
            number=number,
            disposition=disposition,
            identity=identity,
            reachability=reachability,
            privacy=privacy,
            evidence=evidence,
            error_threshold=error_threshold,
            minimum_evidence_runs=minimum_evidence_runs,
        )
        entries.append(
            AccountAuditEntry(
                a_number=number,
                identity=identity.state,
                reachability=reachability,
                privacy=privacy,
                disposition=disposition,
                master_enable=bool(master_enable_by_number[number]),
                suggestion=suggestion,
                suggestion_reason=reason,
                evidence=evidence,
                duplicate_group=identity.duplicate_group,
            )
        )
    return tuple(entries)


def _trailing_streak(
    rows: tuple[AccountHistoryRow, ...],
    target: AccountStatus,
) -> tuple[AccountHistoryRow, ...]:
    streak: list[AccountHistoryRow] = []
    for row in reversed(rows):
        if row.status in _SKIPPED_STATUSES:
            continue
        if row.status is target:
            streak.append(row)
            continue
        break
    streak.reverse()
    return tuple(streak)


def _derive_reachability(
    evidence: AccountEvidence, *, unavailable: bool
) -> ReachabilityState:
    if unavailable:
        return ReachabilityState.UNAVAILABLE
    if evidence.last_classified_status is AccountStatus.ERROR:
        return ReachabilityState.REQUEST_FAILED
    if evidence.last_classified_status in _PUBLIC_STATUSES or (
        evidence.last_classified_status is AccountStatus.PRIVATE
    ):
        return ReachabilityState.REACHABLE
    return ReachabilityState.UNKNOWN


def _derive_privacy(evidence: AccountEvidence) -> PrivacyState:
    if evidence.last_classified_status is AccountStatus.PRIVATE:
        return PrivacyState.PRIVATE
    if evidence.last_classified_status in _PUBLIC_STATUSES:
        return PrivacyState.PUBLIC
    return PrivacyState.UNKNOWN


def _derive_suggestion(
    *,
    number: int,
    disposition: Disposition,
    identity: IdentityAssessment,
    reachability: ReachabilityState,
    privacy: PrivacyState,
    evidence: AccountEvidence,
    error_threshold: int,
    minimum_evidence_runs: int,
) -> tuple[Suggestion, str]:
    if (
        disposition is Disposition.PERMANENTLY_DISABLED
        and evidence.last_classified_status is AccountStatus.DOWNLOADED
    ):
        return Suggestion.SUGGEST_REENABLE, f"A{number} 最近一轮有成功下载，可复核后恢复启用。"
    if reachability is ReachabilityState.UNAVAILABLE:
        return Suggestion.SUGGEST_DISABLE, f"A{number} 有明确的账号不可用证据。"
    if evidence.consecutive_error_runs >= error_threshold:
        run_ids = ",".join(evidence.consecutive_error_run_ids)
        return (
            Suggestion.SUGGEST_DISABLE,
            f"A{number} 连续 {evidence.consecutive_error_runs} 个 ERROR：{run_ids}；"
            "请求层失败不代表账号已注销，此项仅为停用建议。",
        )
    if identity.state is IdentityState.DUPLICATE:
        return (
            Suggestion.REVIEW,
            f"A{number} 属于重复组 {identity.duplicate_group}，需人工选择保留位置。",
        )
    if identity.state is IdentityState.CONFLICT:
        return Suggestion.REVIEW, f"A{number} 的历史身份不一致，需人工复核。"
    if reachability is ReachabilityState.REQUEST_FAILED:
        return (
            Suggestion.REVIEW,
            f"A{number} 出现请求层失败，可能是签名或风控，不代表账号失效。",
        )
    if (
        privacy is PrivacyState.PRIVATE
        and evidence.consecutive_private_runs >= minimum_evidence_runs
    ):
        return Suggestion.REVIEW, f"A{number} 连续为私密状态；私密可逆，不建议永久停用。"
    if evidence.consecutive_no_eligible_runs >= minimum_evidence_runs:
        return Suggestion.REVIEW, f"A{number} 连续无符合条件作品，建议稍后复核。"
    if evidence.classified_runs < minimum_evidence_runs:
        return Suggestion.NO_EVIDENCE, f"A{number} 的可用历史证据不足。"
    return Suggestion.KEEP, f"A{number} 暂无需要改变主档状态的证据。"


def _safe_run_identifier(row: AccountHistoryRow) -> str:
    name = row.task_log.name
    if _SAFE_TASK_NAME.fullmatch(name):
        return row.task_log.stem
    return f"run-{row.ended_at:%Y%m%d%H%M%S}"


def _format_run_time(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value is not None else None
