"""Account-audit evidence evaluation and guarded tombstone persistence.

Checkpoint 03-A supplies the read-only evidence model.  Checkpoint 03-B adds
the explicit preview, backup, master-enable, and audit-sidecar transaction.
Network, controller, and GUI behavior remain outside this module.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, AbstractSet, Iterable, Mapping

from douk_manager.config import ManagedPaths
from douk_manager.core.backup import BackupService, sha256_file
from douk_manager.core.download_summary import AccountStatus
from douk_manager.core.json_store import read_json, write_json_atomic
from douk_manager.core.locks import critical_section
from douk_manager.core.result_history import AccountHistoryRow, ResultHistoryService
from douk_manager.operation import TaskCancelled

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


class AccountAuditError(RuntimeError):
    pass


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


@dataclass(frozen=True)
class AuditApplyResult:
    preview: AuditApplyPreview
    backup_path: Path
    audit_state_path: Path
    master_sha256_after: str


class AccountAuditService:
    """Audit reporting plus the explicit checkpoint-03-B write path."""

    def __init__(
        self,
        history_service: ResultHistoryService,
        *,
        paths: ManagedPaths | None = None,
        backup: BackupService | None = None,
    ) -> None:
        if (paths is None) != (backup is None):
            raise ValueError("paths and backup must be supplied together")
        self.history_service = history_service
        self.paths = paths
        self.backup = backup

    def build_entries(
        self,
        master_enable_by_number: Mapping[int, bool],
        *,
        evidence_since: datetime | None = None,
        identity_observations: Iterable[IdentityObservation] = (),
        explicitly_unavailable: AbstractSet[int] = frozenset(),
        error_threshold: int = DEFAULT_CONSECUTIVE_ERROR_THRESHOLD,
        minimum_evidence_runs: int = DEFAULT_MINIMUM_EVIDENCE_RUNS,
        saved_dispositions: Mapping[int, Disposition] | None = None,
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
            saved_dispositions=saved_dispositions,
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
        saved_dispositions: Mapping[int, Disposition] | None = None,
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
            saved_dispositions=saved_dispositions,
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

    def build_current_report(
        self,
        *,
        generated_at: datetime | None = None,
        evidence_since: datetime | None = None,
        error_threshold: int = DEFAULT_CONSECUTIVE_ERROR_THRESHOLD,
        minimum_evidence_runs: int = DEFAULT_MINIMUM_EVIDENCE_RUNS,
        context: OperationContext | None = None,
    ) -> AccountAuditReport:
        paths, _backup = self._persistence_services()
        with critical_section(paths.lock_file):
            document, master_sha256 = _read_master_with_sha(paths.master_settings)
            accounts = _accounts(document)
            master_enable = {
                number: bool(account.get("enable", True))
                for number, account in enumerate(accounts, start=1)
            }
            saved = _load_saved_dispositions(paths.account_audit / "audit-state.json")
            if not set(saved).issubset(master_enable):
                raise AccountAuditError("审计侧档的 A 编号超出当前主档范围。")
        return self.build_report(
            master_enable,
            master_sha256=master_sha256,
            generated_at=generated_at,
            evidence_since=evidence_since,
            error_threshold=error_threshold,
            minimum_evidence_runs=minimum_evidence_runs,
            saved_dispositions=saved,
            context=context,
        )

    @staticmethod
    def preview_decisions(
        report: AccountAuditReport,
        decisions: Iterable[AuditDecision],
    ) -> AuditApplyPreview:
        return preview_audit_decisions(report, decisions)

    def apply_decisions(
        self,
        report: AccountAuditReport,
        decisions: Iterable[AuditDecision],
        *,
        context: OperationContext | None = None,
    ) -> AuditApplyResult:
        paths, backup = self._persistence_services()
        decision_tuple = tuple(decisions)
        preview = preview_audit_decisions(report, decision_tuple)
        if context is not None:
            context.raise_if_cancelled()

        with critical_section(paths.lock_file):
            try:
                backup.validate_live_data()
                snapshot = backup.create_full_snapshot(
                    "BeforeAccountAudit",
                    {
                        "operation": "apply_account_audit",
                        "to_disable": list(preview.to_disable),
                        "to_enable": list(preview.to_enable),
                        "to_pending": list(preview.to_pending),
                    },
                    keep_latest=2,
                    context=context,
                )
            except Exception as exc:
                if isinstance(exc, TaskCancelled):
                    raise
                raise AccountAuditError(f"账号审计备份失败：{exc}") from exc

            document, current_sha256 = _read_master_with_sha(paths.master_settings)
            if current_sha256 != report.master_sha256:
                raise AccountAuditError("主档在审计后发生变化，请重新审计后再应用。")
            accounts = _accounts(document)
            if len(accounts) != report.total_accounts:
                raise AccountAuditError("主档账号数量已变化，请重新审计后再应用。")

            expected = _build_expected_master(document, decision_tuple)
            expected_accounts = _accounts(expected)
            if not any(bool(account.get("enable", True)) for account in expected_accounts):
                raise AccountAuditError("应用后必须至少保留一个启用账号。")
            state_path = paths.account_audit / "audit-state.json"
            previous_state = _read_existing_state(state_path)
            master_write_attempted = False
            try:
                if context is not None:
                    context.enter_critical_phase()
                if expected != document:
                    master_write_attempted = True
                    write_json_atomic(paths.master_settings, expected)
                    verified = read_json(paths.master_settings)
                    _verify_master_write(document, expected, verified, decision_tuple)
                    master_sha256_after = sha256_file(paths.master_settings)
                else:
                    master_sha256_after = current_sha256
                state = _build_audit_state(
                    previous_state,
                    report,
                    decision_tuple,
                    master_sha256_after=master_sha256_after,
                    decided_at=datetime.now(),
                )
                write_json_atomic(state_path, state)
                if read_json(state_path) != state:
                    raise AccountAuditError("审计侧档复读校验失败。")
            except Exception as exc:
                _restore_after_apply_failure(
                    backup,
                    snapshot,
                    state_path,
                    previous_state,
                    restore_master=master_write_attempted,
                )
                if isinstance(exc, AccountAuditError):
                    raise
                raise AccountAuditError(f"账号审计写入失败：{exc}") from exc
            return AuditApplyResult(
                preview=preview,
                backup_path=snapshot,
                audit_state_path=state_path,
                master_sha256_after=master_sha256_after,
            )

    def _persistence_services(self) -> tuple[ManagedPaths, BackupService]:
        if self.paths is None or self.backup is None:
            raise AccountAuditError("账号审计写入服务尚未配置。")
        return self.paths, self.backup


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
    saved_dispositions: Mapping[int, Disposition] | None = None,
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
        saved = (saved_dispositions or {}).get(number)
        if saved is Disposition.PENDING_REVIEW:
            disposition = Disposition.PENDING_REVIEW
        else:
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


def preview_audit_decisions(
    report: AccountAuditReport,
    decisions: Iterable[AuditDecision],
) -> AuditApplyPreview:
    entries = {entry.a_number: entry for entry in report.entries}
    expected_numbers = set(range(1, report.total_accounts + 1))
    if len(entries) != len(report.entries) or set(entries) != expected_numbers:
        raise AccountAuditError("审计报告的 A 编号与主档位置不一致，请重新审计。")

    decision_tuple = tuple(decisions)
    seen: set[int] = set()
    to_disable: list[int] = []
    to_enable: list[int] = []
    to_pending: list[int] = []
    enabled_after = {
        number: entry.master_enable for number, entry in entries.items()
    }
    for decision in decision_tuple:
        if decision.a_number in seen:
            raise AccountAuditError(f"A{decision.a_number} 出现重复决定。")
        seen.add(decision.a_number)
        entry = entries.get(decision.a_number)
        if entry is None:
            raise AccountAuditError(f"A{decision.a_number} 不在当前审计报告中。")
        if not isinstance(decision.disposition, Disposition):
            raise AccountAuditError(f"A{decision.a_number} 的决定类型无效。")
        _safe_decision_reason(decision.reason)
        if decision.disposition is entry.disposition:
            continue
        if decision.disposition is Disposition.PERMANENTLY_DISABLED:
            to_disable.append(decision.a_number)
            enabled_after[decision.a_number] = False
        elif decision.disposition is Disposition.ENABLED:
            to_enable.append(decision.a_number)
            enabled_after[decision.a_number] = True
        else:
            to_pending.append(decision.a_number)

    changed = len(to_disable) + len(to_enable) + len(to_pending)
    enabled_count = sum(enabled_after.values())
    if enabled_count < 1:
        raise AccountAuditError("应用后必须至少保留一个启用账号。")
    return AuditApplyPreview(
        to_disable=tuple(sorted(to_disable)),
        to_enable=tuple(sorted(to_enable)),
        to_pending=tuple(sorted(to_pending)),
        unchanged=report.total_accounts - changed,
        enabled_after=enabled_count,
        array_length_before=report.total_accounts,
        array_length_after=report.total_accounts,
    )


def _read_master_with_sha(path: Path) -> tuple[dict, str]:
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AccountAuditError(f"无法读取有效账号主档：{exc}") from exc
    if not isinstance(document, dict):
        raise AccountAuditError("账号主档顶层必须是对象。")
    return document, hashlib.sha256(raw).hexdigest()


def _accounts(document: dict) -> list[dict]:
    accounts = document.get("accounts_urls")
    if not isinstance(accounts, list):
        raise AccountAuditError("账号主档缺少 accounts_urls 数组。")
    for number, account in enumerate(accounts, start=1):
        if not isinstance(account, dict):
            raise AccountAuditError(f"账号主档的 A{number} 不是对象。")
    return accounts


def _build_expected_master(
    document: dict,
    decisions: tuple[AuditDecision, ...],
) -> dict:
    expected = copy.deepcopy(document)
    accounts = _accounts(expected)
    for decision in decisions:
        if decision.disposition is Disposition.PENDING_REVIEW:
            continue
        if decision.a_number < 1 or decision.a_number > len(accounts):
            raise AccountAuditError(f"A{decision.a_number} 不在当前主档中。")
        accounts[decision.a_number - 1]["enable"] = (
            decision.disposition is Disposition.ENABLED
        )
    return expected


def _verify_master_write(
    before: dict,
    expected: dict,
    actual: dict,
    decisions: tuple[AuditDecision, ...],
) -> None:
    before_accounts = _accounts(before)
    expected_accounts = _accounts(expected)
    actual_accounts = _accounts(actual)
    if len(before_accounts) != len(expected_accounts) or len(actual_accounts) != len(
        before_accounts
    ):
        raise AccountAuditError("主档复读校验失败：账号数组长度发生变化。")
    targets = {
        decision.a_number
        for decision in decisions
        if decision.disposition is not Disposition.PENDING_REVIEW
    }
    for number, (old, wanted, current) in enumerate(
        zip(before_accounts, expected_accounts, actual_accounts), start=1
    ):
        old_without_enable = {key: value for key, value in old.items() if key != "enable"}
        wanted_without_enable = {
            key: value for key, value in wanted.items() if key != "enable"
        }
        current_without_enable = {
            key: value for key, value in current.items() if key != "enable"
        }
        if not (
            old_without_enable == wanted_without_enable == current_without_enable
        ):
            raise AccountAuditError(
                f"主档复读校验失败：A{number} 的非 enable 字段发生变化。"
            )
        if current.get("enable", True) != wanted.get("enable", True):
            raise AccountAuditError(
                f"主档复读校验失败：A{number} 的 enable 与决定不一致。"
            )
        if number not in targets and current.get("enable", True) != old.get(
            "enable", True
        ):
            raise AccountAuditError(
                f"主档复读校验失败：A{number} 的 enable 被意外修改。"
            )
    if actual != expected:
        raise AccountAuditError("主档复读校验失败：写入结果与预期不一致。")


def _read_existing_state(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        state = read_json(path)
    except Exception as exc:
        raise AccountAuditError(f"无法读取有效审计侧档：{exc}") from exc
    _validate_state_document(state)
    return state


def _load_saved_dispositions(path: Path) -> dict[int, Disposition]:
    state = _read_existing_state(path)
    if state is None:
        return {}
    result: dict[int, Disposition] = {}
    for raw_number, entry in state["entries"].items():
        try:
            result[int(raw_number)] = Disposition(entry["disposition"])
        except (KeyError, TypeError, ValueError) as exc:
            raise AccountAuditError("审计侧档包含无效决定。") from exc
    return result


def _validate_state_document(state: dict) -> None:
    if state.get("schema") != 1 or not isinstance(state.get("entries"), dict):
        raise AccountAuditError("审计侧档结构无效。")
    for raw_number, entry in state["entries"].items():
        if not str(raw_number).isdigit() or int(raw_number) < 1:
            raise AccountAuditError("审计侧档包含无效 A 编号。")
        if not isinstance(entry, dict):
            raise AccountAuditError("审计侧档条目必须是对象。")
        try:
            Disposition(entry.get("disposition"))
        except ValueError as exc:
            raise AccountAuditError("审计侧档包含无效决定。") from exc
        _safe_decision_reason(str(entry.get("decided_reason", "")))


def _build_audit_state(
    previous: dict | None,
    report: AccountAuditReport,
    decisions: tuple[AuditDecision, ...],
    *,
    master_sha256_after: str,
    decided_at: datetime,
) -> dict:
    entries = copy.deepcopy(previous.get("entries", {}) if previous else {})
    report_entries = {entry.a_number: entry for entry in report.entries}
    timestamp = decided_at.isoformat(timespec="seconds")
    for decision in decisions:
        observed = report_entries[decision.a_number]
        entries[str(decision.a_number)] = {
            "disposition": decision.disposition.value,
            "decided_at": timestamp,
            "decided_reason": _safe_decision_reason(decision.reason),
            "last_observed": {
                "identity": observed.identity.value,
                "reachability": observed.reachability.value,
                "privacy": observed.privacy.value,
            },
        }
    return {
        "schema": 1,
        "generated_at": timestamp,
        "source_master_sha256": master_sha256_after,
        "entries": entries,
    }


def _safe_decision_reason(reason: str) -> str:
    cleaned = reason.strip()
    if len(cleaned) > 200 or "\n" in cleaned or "\r" in cleaned:
        raise AccountAuditError("决定备注必须是最多 200 字的单行文本。")
    lowered = cleaned.casefold()
    forbidden = ("://", "sec_user_id", "cookie", "authorization", "uid=")
    if any(token in lowered for token in forbidden):
        raise AccountAuditError("决定备注不能包含账号标识或认证信息。")
    return cleaned


def _restore_after_apply_failure(
    backup: BackupService,
    snapshot: Path,
    state_path: Path,
    previous_state: dict | None,
    *,
    restore_master: bool,
) -> None:
    try:
        if restore_master:
            backup.restore_named_files(snapshot, ("settings_master.json",))
        if previous_state is None:
            state_path.unlink(missing_ok=True)
        else:
            write_json_atomic(state_path, previous_state)
    except Exception as exc:
        raise AccountAuditError(f"账号审计失败后的恢复也失败：{exc}") from exc


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
