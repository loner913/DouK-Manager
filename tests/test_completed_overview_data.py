from datetime import datetime, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from douk_manager.core.result_dashboard import ResultDashboardService
from douk_manager.ui.pages.completed_data import read_completed
from tests.completed_overview_helpers import write_task


class CompletedDataTests(unittest.TestCase):
    def test_interrupted_and_pre_start_errors_preserve_counts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            end = datetime(2026, 9, 13, 10)
            write_task(root, "interrupted", end, count=10, anomalies=0,
                       distribution=(2, 1, 1, 1, 2, 3), exit_code=1,
                       pre_start_errors=(11, 12))
            snapshot = read_completed(ResultDashboardService(root), end.date()).snapshot
            self.assertEqual(snapshot.planned_count, 12)
            self.assertEqual(snapshot.started_count, 10)
            self.assertEqual(snapshot.pre_start_error_count, 2)
            self.assertFalse(snapshot.complete)
            self.assertEqual(sum(value for _, value in snapshot.main_status_counts), 10)
            self.assertEqual([r.a_number for r in snapshot.account_rows
                              if r.status == "interrupted"], [8, 9, 10])
            self.assertEqual([r.a_number for r in snapshot.account_rows
                              if r.status == "pre_start_error"], [11, 12])

    def test_formatter_parser_latest_failed_task_and_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            end = datetime(2026, 9, 12, 20)
            old = write_task(root, "old", end-timedelta(days=1))
            latest = write_task(root, "new", end, exit_code=1)
            service = ResultDashboardService(root)
            with patch.object(service, "_parse_path", wraps=service._parse_path) as parse:
                data = read_completed(service, end.date())
                self.assertEqual(data.snapshot.task_log, latest)
                self.assertFalse(data.snapshot.complete)
                self.assertEqual(data.snapshot.started_count * 60 / data.snapshot.duration_seconds, 8)
                calls = parse.call_count
                service.selected_task(old)
                self.assertEqual(read_completed(service, end.date()).snapshot.task_log, latest)
                self.assertEqual(parse.call_count, calls)
                write_task(root, "new", end, count=600)
                self.assertEqual(read_completed(service, end.date()).snapshot.started_count, 600)

    def test_empty_partial_and_unknown_do_not_create_complete_trend(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            end = datetime(2026, 9, 12, 20)
            service = ResultDashboardService(root)
            self.assertIsNone(read_completed(service, end.date()).snapshot)
            ready = write_task(root, "ready", end)
            partial = root / "DownloadTask_partial.log"
            partial.write_text("[2026-09-12 21:00:00] Started\n", encoding="utf-8")
            result = read_completed(service, end.date())
            self.assertEqual(result.snapshot.task_log, ready)
            self.assertTrue(result.notice)
            self.assertEqual(result.series, ())
            partial.unlink()
            ready.write_text(ready.read_text(encoding="utf-8").replace("计划账号：1200", "计划账号：未知"), encoding="utf-8")
            result = read_completed(service, end.date())
            self.assertIsNone(result.snapshot.planned_count)
            self.assertTrue(result.trend_incomplete)

    def test_week_aggregates_tasks_not_unique_accounts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            end = datetime(2026, 9, 12, 20)
            write_task(root, "one", end, count=100, errors=10)
            write_task(root, "two", end-timedelta(hours=1), count=100, errors=10)
            write_task(root, "outside", end-timedelta(days=7), count=100, errors=10)
            data = read_completed(ResultDashboardService(root), end.date())
            self.assertEqual(data.series[0][1][-1], 200)
            self.assertEqual(data.series[1][1][-1], 180)
            self.assertEqual(data.series[2][1][-1], 20)
            self.assertEqual(sum(data.series[0][1]), 200)
