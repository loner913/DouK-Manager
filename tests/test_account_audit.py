from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from douk_manager.core.account_audit import (
    DEFAULT_CONSECUTIVE_ERROR_THRESHOLD,
    AccountAuditService,
    AccountAuditError,
    AuditApplyResult,
    AuditDecision,
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
from douk_manager.core.backup import BackupService, sha256_file
from douk_manager.core.download_summary import freeze_planned_accounts
from douk_manager.core.json_store import read_json, write_json_atomic
from douk_manager.core.result_history import (
    AccountHistoryRow,
    DownloadTaskHistory,
    ResultHistoryService,
)
from douk_manager.operation import OperationContext, TaskCancelled
from tests.helpers import make_test_paths


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

    def test_enabled_native_log_analysis_without_sources_is_honest(self) -> None:
        history = ResultHistoryService(Path("unused"))

        report = AccountAuditService(history).build_report(
            {1: True},
            master_sha256="synthetic-sha256",
            native_log_analysis=True,
        )

        self.assertTrue(report.native_log_analysis)
        self.assertEqual(report.native_log_runs_scanned, 0)
        self.assertNotIn(NATIVE_LOG_DISABLED_WARNING, report.warnings)
        self.assertTrue(any("没有可验证" in warning for warning in report.warnings))


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

    def test_identity_risk_overrides_consecutive_error_disable_suggestion(self) -> None:
        rows = tuple(_row(number, AccountStatus.ERROR) for number in range(1, 6))
        observations = (
            IdentityObservation(1, "duplicate-token"),
            IdentityObservation(2, "duplicate-token"),
            IdentityObservation(3, "historical-token"),
            IdentityObservation(3, "current-token"),
        )

        entries = build_audit_entries(
            {1: True, 2: True, 3: True},
            {1: rows, 2: rows, 3: rows},
            identity_observations=observations,
        )

        self.assertEqual(entries[0].identity, IdentityState.DUPLICATE)
        self.assertEqual(entries[1].identity, IdentityState.DUPLICATE)
        self.assertEqual(entries[2].identity, IdentityState.CONFLICT)
        self.assertEqual(
            [entry.suggestion for entry in entries],
            [Suggestion.REVIEW, Suggestion.REVIEW, Suggestion.REVIEW],
        )

    def test_build_is_pure_and_does_not_mutate_inputs(self) -> None:
        master = {1: True}
        history = {1: [_row(number, AccountStatus.ERROR) for number in range(1, 6)]}
        original_rows = tuple(history[1])

        entries = build_audit_entries(master, history)

        self.assertIsInstance(entries[0], AccountAuditEntry)
        self.assertEqual(master, {1: True})
        self.assertEqual(tuple(history[1]), original_rows)


class AccountAuditPersistenceTests(unittest.TestCase):
    def _service_and_report(self, root: Path, account_count: int = 5):
        paths = make_test_paths(root, account_count)
        service = AccountAuditService(
            ResultHistoryService(paths.download_task_logs),
            paths=paths,
            backup=BackupService(paths),
        )
        report = service.build_current_report(
            generated_at=datetime(2026, 9, 8, 12, 0, 0)
        )
        return paths, service, report

    def test_managed_paths_declares_isolated_account_audit_directory(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 3)

            self.assertEqual(paths.account_audit, paths.data / "AccountAudit")
            self.assertTrue(paths.account_audit.is_dir())

    def test_profile_url_variants_duplicate_but_case_and_unresolved_urls_do_not(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 5)
            master = read_json(paths.master_settings)
            accounts = master["accounts_urls"]
            stable_identity = "Ab_" + ("X" * 52)
            accounts[0]["url"] = (
                f"https://www.douyin.com/user/{stable_identity}?from=search#profile"
            )
            accounts[1]["url"] = (
                f"HTTPS://DOUYIN.COM/user/{stable_identity}/"
            )
            accounts[2]["url"] = (
                f"https://www.douyin.com/user/{stable_identity.lower()}"
            )
            accounts[3]["url"] = "https://v.douyin.com/synthetic-short-link/"
            accounts[4]["url"] = f"https://synthetic.invalid/user/{stable_identity}"
            write_json_atomic(paths.master_settings, master)
            service = AccountAuditService(
                ResultHistoryService(paths.download_task_logs),
                paths=paths,
                backup=BackupService(paths),
            )

            report = service.build_current_report()

            self.assertEqual(report.entries[0].identity, IdentityState.DUPLICATE)
            self.assertEqual(report.entries[1].identity, IdentityState.DUPLICATE)
            self.assertEqual(
                report.entries[0].duplicate_group,
                report.entries[1].duplicate_group,
            )
            self.assertEqual(report.entries[2].identity, IdentityState.CONFIRMED)
            self.assertEqual(report.entries[3].identity, IdentityState.UNRESOLVED)
            self.assertEqual(report.entries[4].identity, IdentityState.UNRESOLVED)
            self.assertEqual(report.duplicate_groups, 1)

    def test_native_identity_conflict_never_leaks_raw_id_to_report_or_sidecar(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 1)
            current_identity = "C" * 55
            historical_identity = "H" * 55
            master = read_json(paths.master_settings)
            master["accounts_urls"][0]["url"] = (
                f"https://www.douyin.com/user/{current_identity}"
            )
            write_json_atomic(paths.master_settings, master)
            native_root = paths.volume / "Log"
            native_root.mkdir()
            native_log = native_root / "synthetic-native.log"
            native_log.write_text(
                "共有 1 个账号的作品等待下载\n"
                "开始处理第 1 个账号\n"
                "标识：A1account1\n"
                f"{historical_identity} 获取账号信息失败，请检查 Cookie 登录状态！\n"
                "筛选处理后作品数量: 0\n",
                encoding="utf-8",
            )
            task_log = (
                paths.download_task_logs / "DownloadTask_2026-09-08_12-00-00.log"
            )
            task_log.write_text(
                "【下载账号汇总】\n"
                "进程结束时间：2026-09-08 12:00:00\n"
                "退出码：0\n"
                "账号汇总：完整\n"
                "账号明细版本：1\n"
                "无符合条件作品（1）：A1\n"
                f"日志区间：{native_log.resolve()}；偏移=0；长度={native_log.stat().st_size}\n",
                encoding="utf-8",
            )
            service = AccountAuditService(
                ResultHistoryService(paths.download_task_logs),
                paths=paths,
                backup=BackupService(paths),
            )

            report = service.build_current_report(native_log_analysis=True)
            self.assertEqual(report.entries[0].identity, IdentityState.CONFLICT)
            self.assertEqual(report.entries[0].suggestion, Suggestion.REVIEW)
            rendered_report = repr(report)
            self.assertNotIn(current_identity, rendered_report)
            self.assertNotIn(historical_identity, rendered_report)

            result = service.apply_decisions(
                report,
                (AuditDecision(1, Disposition.PENDING_REVIEW, "synthetic review"),),
            )
            rendered_sidecar = result.audit_state_path.read_text(encoding="utf-8")
            self.assertNotIn(current_identity, rendered_sidecar)
            self.assertNotIn(historical_identity, rendered_sidecar)

    def test_native_log_analysis_rechecks_exact_historical_segments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 3)
            native_root = paths.volume / "Log"
            native_root.mkdir()
            native_log = native_root / "synthetic-native.log"
            native_log.write_text(
                "共有 3 个账号的作品等待下载\n"
                "开始处理第 1 个账号\n"
                "标识：A1account1\n"
                "[ERROR]: Response Code: 403\n"
                "开始处理第 2 个账号\n"
                "标识：A2account2\n"
                "该账号为私密账号\n"
                "开始处理第 3 个账号\n"
                "标识：A3account3\n"
                "筛选处理后作品数量: 0\n",
                encoding="utf-8",
            )
            task_log = paths.download_task_logs / "DownloadTask_2026-09-08_12-00-00.log"
            task_log.write_text(
                "【下载账号汇总】\n"
                "进程结束时间：2026-09-08 12:00:00\n"
                "退出码：0\n"
                "账号汇总：完整\n"
                "账号明细版本：1\n"
                "无符合条件作品（3）：A1-A3\n"
                f"日志区间：{native_log.resolve()}；偏移=0；长度={native_log.stat().st_size}\n",
                encoding="utf-8",
            )
            service = AccountAuditService(
                ResultHistoryService(paths.download_task_logs),
                paths=paths,
                backup=BackupService(paths),
            )

            summary_only = service.build_current_report(native_log_analysis=False)
            deep = service.build_current_report(native_log_analysis=True)
            cached = service.build_current_report(native_log_analysis=True)

            self.assertEqual(summary_only.entries[0].reachability, ReachabilityState.REACHABLE)
            self.assertEqual(deep.entries[0].reachability, ReachabilityState.REQUEST_FAILED)
            self.assertEqual(deep.entries[1].privacy, PrivacyState.PRIVATE)
            self.assertEqual(deep.entries[2].privacy, PrivacyState.PUBLIC)
            self.assertTrue(deep.native_log_analysis)
            self.assertEqual(deep.native_log_runs_scanned, 1)
            self.assertEqual(deep.native_log_segments_scanned, 1)
            self.assertNotIn(NATIVE_LOG_DISABLED_WARNING, deep.warnings)
            self.assertIs(cached, deep)
            native_log.write_text(native_log.read_text(encoding="utf-8") + "\n", encoding="utf-8")
            refreshed = service.build_current_report(native_log_analysis=True)
            self.assertIsNot(refreshed, deep)

    def test_native_log_analysis_honours_cancel_after_last_run_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 1)
            native_root = paths.volume / "Log"
            native_root.mkdir()
            native_log = native_root / "synthetic-native.log"
            native_log.write_text(
                "共有 1 个账号的作品等待下载\n"
                "开始处理第 1 个账号\n"
                "标识：A1account1\n"
                "筛选处理后作品数量: 0\n",
                encoding="utf-8",
            )
            task_log = paths.download_task_logs / "DownloadTask_2026-09-08_12-00-00.log"
            task_log.write_text(
                "【下载账号汇总】\n"
                "进程结束时间：2026-09-08 12:00:00\n"
                "退出码：0\n"
                "账号汇总：完整\n"
                "账号明细版本：1\n"
                "无符合条件作品（1）：A1\n"
                f"日志区间：{native_log.resolve()}；偏移=0；长度={native_log.stat().st_size}\n",
                encoding="utf-8",
            )
            service = AccountAuditService(
                ResultHistoryService(paths.download_task_logs),
                paths=paths,
                backup=BackupService(paths),
            )
            context = OperationContext(progress_interval_seconds=0)

            def cancel_on_native_progress(progress) -> None:
                if progress.phase == "account_audit_native_logs":
                    context.request_cancel()

            context.set_progress_callback(cancel_on_native_progress)
            master_before = paths.master_settings.read_bytes()

            with self.assertRaises(TaskCancelled):
                service.build_current_report(
                    native_log_analysis=True,
                    context=context,
                )

            self.assertEqual(paths.master_settings.read_bytes(), master_before)
            self.assertFalse((paths.account_audit / "audit-state.json").exists())

    def test_native_log_analysis_never_reads_a_declared_path_outside_volume_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = make_test_paths(root, 1)
            (paths.volume / "Log").mkdir()
            outside = root / "outside-native.log"
            outside.write_text(
                "共有 1 个账号的作品等待下载\n"
                "开始处理第 1 个账号\n"
                "标识：A1account1\n"
                "[ERROR]: Response Code: 403\n",
                encoding="utf-8",
            )
            task_log = paths.download_task_logs / "DownloadTask_2026-09-08_12-00-00.log"
            task_log.write_text(
                "【下载账号汇总】\n"
                "进程结束时间：2026-09-08 12:00:00\n"
                "退出码：0\n"
                "账号汇总：完整\n"
                "账号明细版本：1\n"
                "无符合条件作品（1）：A1\n"
                f"日志区间：{outside.resolve()}；偏移=0；长度={outside.stat().st_size}\n",
                encoding="utf-8",
            )
            service = AccountAuditService(
                ResultHistoryService(paths.download_task_logs),
                paths=paths,
                backup=BackupService(paths),
            )

            report = service.build_current_report(native_log_analysis=True)

            self.assertEqual(report.entries[0].reachability, ReachabilityState.REACHABLE)
            self.assertEqual(report.native_log_runs_scanned, 0)
            self.assertTrue(any("没有可验证" in warning for warning in report.warnings))
            self.assertNotIn(str(outside), "\n".join(report.warnings))

    def test_preview_lists_changes_and_keeps_array_length(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            _paths, service, report = self._service_and_report(Path(directory))
            decisions = (
                AuditDecision(1, Disposition.PERMANENTLY_DISABLED),
                AuditDecision(2, Disposition.ENABLED),
                AuditDecision(3, Disposition.PENDING_REVIEW),
            )

            preview = service.preview_decisions(report, decisions)

            self.assertEqual(preview.to_disable, (1,))
            self.assertEqual(preview.to_enable, (2,))
            self.assertEqual(preview.to_pending, (3,))
            self.assertEqual(preview.unchanged, 2)
            self.assertEqual(preview.enabled_after, 3)
            self.assertEqual(preview.array_length_before, 5)
            self.assertEqual(preview.array_length_after, 5)

    def test_apply_changes_only_enable_preserves_positions_and_writes_safe_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service, report = self._service_and_report(Path(directory))
            master_before = read_json(paths.master_settings)
            decisions = (
                AuditDecision(1, Disposition.PERMANENTLY_DISABLED, "manual review"),
                AuditDecision(2, Disposition.ENABLED),
                AuditDecision(3, Disposition.PENDING_REVIEW),
            )

            result = service.apply_decisions(report, decisions)

            self.assertIsInstance(result, AuditApplyResult)
            self.assertTrue(result.backup_path.is_dir())
            self.assertEqual(result.backup_path.parent.name, "BeforeAccountAudit")
            manifest = json.loads(
                (result.backup_path / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["scope"], "full")
            master_after = read_json(paths.master_settings)
            self.assertEqual(len(master_after["accounts_urls"]), 5)
            self.assertEqual(
                [item["enable"] for item in master_after["accounts_urls"]],
                [False, True, True, False, True],
            )
            for before, after in zip(
                master_before["accounts_urls"], master_after["accounts_urls"]
            ):
                self.assertEqual(
                    {key: value for key, value in before.items() if key != "enable"},
                    {key: value for key, value in after.items() if key != "enable"},
                )
            planned_after = freeze_planned_accounts(master_after)
            before_by_number = {
                number: item["mark"]
                for number, item in enumerate(master_before["accounts_urls"], start=1)
            }
            after_by_number = {item.a_number: item.mark for item in planned_after}
            for number in after_by_number:
                self.assertEqual(after_by_number[number], before_by_number[number])
            state = read_json(paths.account_audit / "audit-state.json")
            self.assertEqual(set(state["entries"]), {"1", "2", "3"})
            state_text = str(state)
            for forbidden in ("accounts_urls", "url", "mark", "account1"):
                self.assertNotIn(forbidden, state_text)
            self.assertEqual(result.master_sha256_after, sha256_file(paths.master_settings))

    def test_pending_disposition_round_trips_without_changing_master_enable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service, report = self._service_and_report(Path(directory), 3)
            master_before = paths.master_settings.read_bytes()

            service.apply_decisions(
                report, (AuditDecision(3, Disposition.PENDING_REVIEW),)
            )
            refreshed = service.build_current_report()

            self.assertEqual(paths.master_settings.read_bytes(), master_before)
            self.assertEqual(refreshed.entries[2].disposition, Disposition.PENDING_REVIEW)

    def test_master_enable_remains_authoritative_over_sidecar_trace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service, report = self._service_and_report(Path(directory), 3)
            service.apply_decisions(
                report, (AuditDecision(1, Disposition.PERMANENTLY_DISABLED),)
            )
            state_path = paths.account_audit / "audit-state.json"
            state = read_json(state_path)
            state["entries"]["1"]["disposition"] = Disposition.ENABLED.value
            write_json_atomic(state_path, state)

            refreshed = service.build_current_report()

            self.assertFalse(refreshed.entries[0].master_enable)
            self.assertEqual(
                refreshed.entries[0].disposition,
                Disposition.PERMANENTLY_DISABLED,
            )

    def test_stale_master_fingerprint_rejects_without_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service, report = self._service_and_report(Path(directory), 3)
            changed = read_json(paths.master_settings)
            changed["run_command"] = "changed-after-audit"
            write_json_atomic(paths.master_settings, changed)
            changed_bytes = paths.master_settings.read_bytes()

            with self.assertRaisesRegex(AccountAuditError, "重新审计"):
                service.apply_decisions(
                    report, (AuditDecision(1, Disposition.PERMANENTLY_DISABLED),)
                )

            self.assertEqual(paths.master_settings.read_bytes(), changed_bytes)
            self.assertFalse((paths.account_audit / "audit-state.json").exists())

    def test_rejects_decisions_that_would_disable_every_account(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service, report = self._service_and_report(Path(directory), 3)
            decisions = tuple(
                AuditDecision(number, Disposition.PERMANENTLY_DISABLED)
                for number in (1, 2, 3)
            )
            master_before = paths.master_settings.read_bytes()

            with self.assertRaisesRegex(AccountAuditError, "至少保留一个启用账号"):
                service.apply_decisions(report, decisions)

            self.assertEqual(paths.master_settings.read_bytes(), master_before)
            self.assertFalse((paths.backups / "BeforeAccountAudit").exists())

    def test_verification_failure_restores_master_from_full_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service, report = self._service_and_report(Path(directory), 3)
            master_before = paths.master_settings.read_bytes()
            real_write = write_json_atomic

            def corrupt_master(path: Path, document: dict) -> None:
                if path == paths.master_settings:
                    damaged = deepcopy(document)
                    damaged["accounts_urls"].pop()
                    real_write(path, damaged)
                    return
                real_write(path, document)

            with patch(
                "douk_manager.core.account_audit.write_json_atomic",
                side_effect=corrupt_master,
            ):
                with self.assertRaisesRegex(AccountAuditError, "复读校验"):
                    service.apply_decisions(
                        report,
                        (AuditDecision(1, Disposition.PERMANENTLY_DISABLED),),
                    )

            self.assertEqual(paths.master_settings.read_bytes(), master_before)
            self.assertFalse((paths.account_audit / "audit-state.json").exists())

    def test_cancel_before_critical_write_leaves_master_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service, report = self._service_and_report(Path(directory), 3)
            master_before = paths.master_settings.read_bytes()
            context = OperationContext()
            self.assertTrue(context.request_cancel())

            with self.assertRaises(TaskCancelled):
                service.apply_decisions(
                    report,
                    (AuditDecision(1, Disposition.PERMANENTLY_DISABLED),),
                    context=context,
                )

            self.assertEqual(paths.master_settings.read_bytes(), master_before)
            self.assertFalse((paths.backups / "BeforeAccountAudit").exists())

    def test_invalid_master_is_rejected_without_sidecar_or_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service, report = self._service_and_report(Path(directory), 3)
            paths.master_settings.write_text("{invalid", encoding="utf-8")
            invalid_bytes = paths.master_settings.read_bytes()

            with self.assertRaisesRegex(AccountAuditError, "备份失败"):
                service.apply_decisions(
                    report, (AuditDecision(1, Disposition.PERMANENTLY_DISABLED),)
                )

            self.assertEqual(paths.master_settings.read_bytes(), invalid_bytes)
            self.assertFalse((paths.backups / "BeforeAccountAudit").exists())
            self.assertFalse((paths.account_audit / "audit-state.json").exists())

    def test_sidecar_verification_failure_restores_master_and_previous_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service, report = self._service_and_report(Path(directory), 3)
            service.apply_decisions(
                report, (AuditDecision(3, Disposition.PENDING_REVIEW),)
            )
            refreshed = service.build_current_report()
            master_before = paths.master_settings.read_bytes()
            state_path = paths.account_audit / "audit-state.json"
            state_before = read_json(state_path)
            real_write = write_json_atomic
            state_writes = 0

            def corrupt_state(path: Path, document: dict) -> None:
                nonlocal state_writes
                if path == state_path:
                    state_writes += 1
                    if state_writes == 1:
                        real_write(path, {"schema": 1, "entries": {}})
                        return
                real_write(path, document)

            with patch(
                "douk_manager.core.account_audit.write_json_atomic",
                side_effect=corrupt_state,
            ):
                with self.assertRaisesRegex(AccountAuditError, "侧档复读校验"):
                    service.apply_decisions(
                        refreshed,
                        (AuditDecision(1, Disposition.PERMANENTLY_DISABLED),),
                    )

            self.assertEqual(paths.master_settings.read_bytes(), master_before)
            self.assertEqual(read_json(state_path), state_before)

    def test_sensitive_decision_reason_is_rejected_before_backup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, service, report = self._service_and_report(Path(directory), 3)

            with self.assertRaisesRegex(AccountAuditError, "认证信息"):
                service.apply_decisions(
                    report,
                    (
                        AuditDecision(
                            1,
                            Disposition.PERMANENTLY_DISABLED,
                            "cookie=synthetic-secret",
                        ),
                    ),
                )

            self.assertFalse((paths.backups / "BeforeAccountAudit").exists())


if __name__ == "__main__":
    unittest.main()
