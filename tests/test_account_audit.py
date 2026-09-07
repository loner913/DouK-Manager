from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from douk_manager.core.account_audit import (
    DEFAULT_CONSECUTIVE_ERROR_THRESHOLD,
    AccountAuditService,
    NATIVE_LOG_DISABLED_WARNING,
    UNCERTAIN_EARLY_HISTORY_WARNING,
    AccountAuditEntry,
    Disposition,
    IdentityObservation,
    IdentityState,
    PrivacyState,
    ReachabilityState,
    Suggestion,
    build_audit_entries,
    classify_identities,
    summarize_account_history,
)
from douk_manager.core.download_summary import AccountStatus
from douk_manager.core.result_history import (
    AccountHistoryRow,
    DownloadTaskHistory,
    ResultHistoryService,
)


BASE_TIME = datetime(2026, 9, 1, 12, 0, 0)


def _row(
    sequence: int,
    status: AccountStatus | str,
    *,
    a_number: int = 1,
) -> AccountHistoryRow:
    ended_at = BASE_TIME.replace(day=sequence)
    return AccountHistoryRow(
        task_log=Path(f"DownloadTask_2026-09-{sequence:02d}_12-00-00.log"),
        task_template="synthetic-task.json",
        ended_at=ended_at,
        a_number=a_number,
        status=status,
    )


def _run(sequence: int, *rows: AccountHistoryRow) -> DownloadTaskHistory:
    ended_at = BASE_TIME.replace(day=sequence)
    return DownloadTaskHistory(
        task_log=Path(f"DownloadTask_2026-09-{sequence:02d}_12-00-00.log"),
        task_template="synthetic-task.json",
        ended_at=ended_at,
        exit_code=0,
        complete=True,
        reliable=True,
        account_rows=tuple(rows),
        details_complete=True,
    )


class ResultHistoryAuditAggregationTests(unittest.TestCase):
    def test_groups_all_history_by_a_number_in_chronological_order(self) -> None:
        service = ResultHistoryService(Path("unused"))
        service.list_runs = lambda **_kwargs: (
            _run(3, _row(3, AccountStatus.ERROR, a_number=2)),
            _run(
                1,
                _row(1, AccountStatus.DOWNLOADED, a_number=1),
                _row(1, AccountStatus.PRIVATE, a_number=2),
            ),
            _run(2, _row(2, AccountStatus.ERROR, a_number=1)),
        )

        grouped = service.account_rows_by_number(numbers=(1, 2, 3))

        self.assertEqual([row.ended_at.day for row in grouped[1]], [1, 2])
        self.assertEqual([row.ended_at.day for row in grouped[2]], [1, 3])
        self.assertEqual(grouped[3], ())

    def test_evidence_start_excludes_earlier_rows_without_guessing(self) -> None:
        service = ResultHistoryService(Path("unused"))
        service.list_runs = lambda **_kwargs: (
            _run(1, _row(1, AccountStatus.ERROR)),
            _run(2, _row(2, AccountStatus.DOWNLOADED)),
        )

        grouped = service.account_rows_by_number(
            numbers=(1,), evidence_since=BASE_TIME.replace(day=2)
        )

        self.assertEqual([row.ended_at.day for row in grouped[1]], [2])

    def test_history_snapshot_reports_scan_bounds_and_excluded_rows(self) -> None:
        service = ResultHistoryService(Path("unused"))
        service.list_runs = lambda **_kwargs: (
            _run(3, _row(3, AccountStatus.ERROR)),
            _run(1, _row(1, AccountStatus.ERROR)),
            _run(2, _row(2, AccountStatus.DOWNLOADED)),
        )

        snapshot = service.account_audit_snapshot(
            numbers=(1,), evidence_since=BASE_TIME.replace(day=2)
        )

        self.assertEqual(snapshot.runs_scanned, 3)
        self.assertEqual(snapshot.oldest_run, BASE_TIME.replace(day=1))
        self.assertEqual(snapshot.newest_run, BASE_TIME.replace(day=3))
        self.assertEqual(snapshot.excluded_old_rows, 1)
        self.assertEqual([row.ended_at.day for row in snapshot.rows_by_number[1]], [2, 3])

    def test_missing_log_directory_remains_absent_during_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-history"

            self.assertEqual(ResultHistoryService(missing).list_runs(limit=None), ())
            self.assertFalse(missing.exists())

    def test_read_only_service_builds_entries_from_grouped_history(self) -> None:
        history = ResultHistoryService(Path("unused"))
        history.account_rows_by_number = lambda **_kwargs: {
            1: tuple(_row(number, AccountStatus.ERROR) for number in range(1, 6))
        }

        entries = AccountAuditService(history).build_entries({1: True})

        self.assertEqual(entries[0].suggestion, Suggestion.SUGGEST_DISABLE)

    def test_report_is_read_only_and_records_conservative_warnings(self) -> None:
        history = ResultHistoryService(Path("unused"))
        history.list_runs = lambda **_kwargs: (
            _run(1, _row(1, AccountStatus.DOWNLOADED)),
            _run(2, _row(2, AccountStatus.ALL_SKIPPED)),
            _run(3, _row(3, AccountStatus.NO_ELIGIBLE_WORKS)),
        )
        generated_at = datetime(2026, 9, 8, 10, 0, 0)

        report = AccountAuditService(history).build_report(
            {1: True}, master_sha256="synthetic-sha256", generated_at=generated_at
        )

        self.assertEqual(report.generated_at, generated_at)
        self.assertEqual(report.runs_scanned, 3)
        self.assertEqual(report.oldest_run, "2026-09-01 12:00:00")
        self.assertEqual(report.newest_run, "2026-09-03 12:00:00")
        self.assertIn(UNCERTAIN_EARLY_HISTORY_WARNING, report.warnings)
        self.assertIn(NATIVE_LOG_DISABLED_WARNING, report.warnings)

    def test_report_does_not_claim_unimplemented_native_log_analysis(self) -> None:
        history = ResultHistoryService(Path("unused"))

        with self.assertRaisesRegex(NotImplementedError, "03-A"):
            AccountAuditService(history).build_report(
                {1: True},
                master_sha256="synthetic-sha256",
                native_log_analysis=True,
            )


class AccountAuditPureLogicTests(unittest.TestCase):
    def test_default_error_threshold_is_five(self) -> None:
        self.assertEqual(DEFAULT_CONSECUTIVE_ERROR_THRESHOLD, 5)

    def test_five_consecutive_errors_suggest_disable_with_safe_run_ids(self) -> None:
        rows = tuple(_row(number, AccountStatus.ERROR) for number in range(1, 6))

        entry = build_audit_entries({1: True}, {1: rows})[0]

        self.assertEqual(entry.suggestion, Suggestion.SUGGEST_DISABLE)
        self.assertEqual(entry.evidence.consecutive_error_runs, 5)
        self.assertEqual(len(entry.evidence.consecutive_error_run_ids), 5)
        self.assertEqual(entry.reachability, ReachabilityState.REQUEST_FAILED)
        self.assertNotEqual(entry.reachability, ReachabilityState.UNAVAILABLE)
        self.assertIn("A1", entry.suggestion_reason)
        for number in range(1, 6):
            self.assertIn(f"DownloadTask_2026-09-{number:02d}_12-00-00", entry.suggestion_reason)
        for forbidden in ("synthetic-task", "mark", "nickname", "token-alpha"):
            self.assertNotIn(forbidden, entry.suggestion_reason)

    def test_four_errors_are_review_not_disable(self) -> None:
        rows = tuple(_row(number, AccountStatus.ERROR) for number in range(1, 5))

        entry = build_audit_entries({1: True}, {1: rows})[0]

        self.assertEqual(entry.evidence.consecutive_error_runs, 4)
        self.assertEqual(entry.suggestion, Suggestion.REVIEW)

    def test_single_and_three_request_failures_are_review(self) -> None:
        for count in (1, 3):
            with self.subTest(count=count):
                rows = tuple(_row(number, AccountStatus.ERROR) for number in range(1, count + 1))

                entry = build_audit_entries({1: True}, {1: rows})[0]

                self.assertEqual(entry.reachability, ReachabilityState.REQUEST_FAILED)
                self.assertEqual(entry.suggestion, Suggestion.REVIEW)

    def test_higher_error_threshold_prevents_disable(self) -> None:
        rows = tuple(_row(number, AccountStatus.ERROR) for number in range(1, 6))

        entry = build_audit_entries({1: True}, {1: rows}, error_threshold=6)[0]

        self.assertNotEqual(entry.suggestion, Suggestion.SUGGEST_DISABLE)

    def test_unselected_runs_do_not_break_or_increase_error_streak(self) -> None:
        rows = tuple(_row(number, AccountStatus.ERROR) for number in (1, 3, 5, 6, 7))

        entry = build_audit_entries({1: True}, {1: rows})[0]

        self.assertEqual(entry.evidence.consecutive_error_runs, 5)
        self.assertEqual(entry.suggestion, Suggestion.SUGGEST_DISABLE)

    def test_classified_success_private_and_no_eligible_each_reset_errors(self) -> None:
        for reset_status in (
            AccountStatus.DOWNLOADED,
            AccountStatus.PRIVATE,
            AccountStatus.NO_ELIGIBLE_WORKS,
        ):
            with self.subTest(reset_status=reset_status):
                rows = tuple(_row(number, AccountStatus.ERROR) for number in range(1, 6))
                rows += (_row(6, reset_status),)

                evidence = summarize_account_history(1, rows)

                self.assertEqual(evidence.consecutive_error_runs, 0)
                self.assertEqual(evidence.consecutive_error_run_ids, ())

    def test_interrupted_pre_start_and_not_started_are_skipped(self) -> None:
        statuses: tuple[AccountStatus | str, ...] = (
            AccountStatus.ERROR,
            AccountStatus.ERROR,
            AccountStatus.INTERRUPTED,
            "pre_start_error",
            "not_started",
            AccountStatus.ERROR,
            AccountStatus.ERROR,
            AccountStatus.ERROR,
        )
        rows = tuple(_row(number, status) for number, status in enumerate(statuses, 1))

        entry = build_audit_entries({1: True}, {1: rows})[0]

        self.assertEqual(entry.evidence.consecutive_error_runs, 5)
        self.assertEqual(entry.suggestion, Suggestion.SUGGEST_DISABLE)

    def test_only_uncertain_rows_produce_no_evidence(self) -> None:
        rows = (
            _row(1, AccountStatus.INTERRUPTED),
            _row(2, "pre_start_error"),
            _row(3, "not_started"),
        )

        entry = build_audit_entries({1: True}, {1: rows})[0]

        self.assertEqual(entry.evidence.consecutive_error_runs, 0)
        self.assertEqual(entry.suggestion, Suggestion.NO_EVIDENCE)
        self.assertEqual(entry.identity, IdentityState.UNRESOLVED)
        self.assertEqual(entry.reachability, ReachabilityState.UNKNOWN)
        self.assertEqual(entry.privacy, PrivacyState.UNKNOWN)

    def test_two_healthy_runs_are_below_minimum_evidence_threshold(self) -> None:
        rows = (
            _row(1, AccountStatus.DOWNLOADED),
            _row(2, AccountStatus.ALL_SKIPPED),
        )

        entry = build_audit_entries({1: True}, {1: rows})[0]

        self.assertEqual(entry.suggestion, Suggestion.NO_EVIDENCE)

    def test_explicit_unavailable_evidence_is_separate_from_error_streak(self) -> None:
        rows = (_row(1, AccountStatus.ERROR),)

        entry = build_audit_entries(
            {1: True}, {1: rows}, explicitly_unavailable=frozenset((1,))
        )[0]

        self.assertEqual(entry.reachability, ReachabilityState.UNAVAILABLE)
        self.assertEqual(entry.evidence.consecutive_error_runs, 1)
        self.assertEqual(entry.suggestion, Suggestion.SUGGEST_DISABLE)

    def test_success_resets_total_errors_to_recent_streak_only(self) -> None:
        statuses = (
            AccountStatus.ERROR,
            AccountStatus.ERROR,
            AccountStatus.DOWNLOADED,
            AccountStatus.ERROR,
            AccountStatus.ERROR,
            AccountStatus.ERROR,
        )

        evidence = summarize_account_history(
            1, tuple(_row(number, status) for number, status in enumerate(statuses, 1))
        )

        self.assertEqual(evidence.status_counts[AccountStatus.ERROR], 5)
        self.assertEqual(evidence.consecutive_error_runs, 3)

    def test_private_is_review_and_never_suggests_disable(self) -> None:
        rows = tuple(_row(number, AccountStatus.PRIVATE) for number in range(1, 5))

        entry = build_audit_entries({1: True}, {1: rows})[0]

        self.assertEqual(entry.privacy, PrivacyState.PRIVATE)
        self.assertEqual(entry.suggestion, Suggestion.REVIEW)

    def test_disabled_account_with_recent_download_suggests_reenable(self) -> None:
        rows = (
            _row(1, AccountStatus.ALL_SKIPPED),
            _row(2, AccountStatus.NO_ELIGIBLE_WORKS),
            _row(3, AccountStatus.DOWNLOADED),
        )

        entry = build_audit_entries({1: False}, {1: rows})[0]

        self.assertEqual(entry.disposition, Disposition.PERMANENTLY_DISABLED)
        self.assertEqual(entry.suggestion, Suggestion.SUGGEST_REENABLE)

    def test_identity_duplicate_and_conflict_are_separate_and_deterministic(self) -> None:
        observations = (
            IdentityObservation(1, "token-alpha"),
            IdentityObservation(2, "token-alpha"),
            IdentityObservation(3, "token-beta"),
            IdentityObservation(3, "token-gamma"),
        )

        assessments = classify_identities((1, 2, 3, 4), observations)

        self.assertEqual(assessments[1].state, IdentityState.DUPLICATE)
        self.assertEqual(assessments[1].duplicate_group, assessments[2].duplicate_group)
        self.assertEqual(assessments[3].state, IdentityState.CONFLICT)
        self.assertEqual(assessments[4].state, IdentityState.UNRESOLVED)

    def test_build_is_pure_and_does_not_mutate_inputs(self) -> None:
        master = {1: True}
        history = {1: [_row(number, AccountStatus.ERROR) for number in range(1, 6)]}
        original_rows = tuple(history[1])

        entries = build_audit_entries(master, history)

        self.assertIsInstance(entries[0], AccountAuditEntry)
        self.assertEqual(master, {1: True})
        self.assertEqual(tuple(history[1]), original_rows)


if __name__ == "__main__":
    unittest.main()
