from __future__ import annotations

import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from unittest.mock import Mock

from douk_manager.controller import ControllerError, ManagerController
from douk_manager.core.download_summary import (
    AccountOutcome,
    AccountStatus,
    DownloadSummary,
    LocatedNativeLogs,
    NativeLogSegment,
    format_summary_for_task_log,
)
from douk_manager.core.result_dashboard import (
    DashboardFileChangedError,
    DashboardFileFingerprint,
    DashboardTaskUnavailableError,
    ResultDashboardService,
)
from douk_manager.startup import StartupState
from douk_manager.operation import OperationContext, TaskCancelled


class ResultDashboardServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.logs = self.root / "Logs" / "DownloadTasks"
        self.logs.mkdir(parents=True)
        self.service = ResultDashboardService(self.logs, cache_capacity=2)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _task(self, name: str, body: str) -> Path:
        path = self.logs / name
        path.write_text(body, encoding="utf-8")
        return path

    def _complete_log(
        self,
        name: str,
        *,
        started: str = "2026-08-21 09:00:00",
        ended: str = "2026-08-21 09:01:30",
    ) -> Path:
        native = self.root / "native.log"
        summary = DownloadSummary(
            planned_count=5,
            started_outcomes=(
                AccountOutcome(1, 1, AccountStatus.DOWNLOADED, True),
                AccountOutcome(2, 2, AccountStatus.ALL_SKIPPED),
                AccountOutcome(3, 3, AccountStatus.NO_ELIGIBLE_WORKS),
                AccountOutcome(4, 4, AccountStatus.PRIVATE),
            ),
            pre_start_errors=(5,),
            not_started=(),
            primary_status_counts={
                AccountStatus.DOWNLOADED: 1,
                AccountStatus.ALL_SKIPPED: 1,
                AccountStatus.NO_ELIGIBLE_WORKS: 1,
                AccountStatus.PRIVATE: 1,
                AccountStatus.ERROR: 0,
                AccountStatus.INTERRUPTED: 0,
            },
            completed_with_anomaly=(1,),
            complete=True,
            reliable=True,
            reasons=(),
            located=LocatedNativeLogs(
                (NativeLogSegment(native, 10, 200),), "size-delta", True
            ),
            exit_code=0,
        )
        block = format_summary_for_task_log(
            summary, datetime.strptime(ended, "%Y-%m-%d %H:%M:%S")
        )
        return self._task(
            name,
            f"[{started}] Started；Task template: A1-A5.json\n\n{block}",
        )

    def test_index_excludes_running_and_partial_summary_and_selects_latest_end(self) -> None:
        older = self._complete_log(
            "DownloadTask_older.log", ended="2026-08-21 09:01:30"
        )
        newer = self._complete_log(
            "DownloadTask_newer.log", ended="2026-08-21 10:01:30"
        )
        running = self._task(
            "DownloadTask_running.log",
            "[2026-08-21 11:00:00] Started；Task template: running.json\n",
        )
        partial = self._task(
            "DownloadTask_partial.log",
            "[2026-08-21 12:00:00] Started；Task template: partial.json\n"
            "【下载账号汇总】\n进程结束时间：2026-08-21 12:02:00\n退出码：0\n",
        )

        index = self.service.task_index()

        self.assertEqual(index.default_task, newer)
        by_path = {entry.task_log: entry for entry in index.entries}
        self.assertTrue(by_path[older].displayable)
        self.assertTrue(by_path[newer].displayable)
        self.assertEqual(by_path[running].state, "pending")
        self.assertEqual(by_path[partial].state, "pending")
        self.assertEqual(index.pending_count, 2)

    def test_index_uses_summary_end_time_not_file_mtime(self) -> None:
        older = self._complete_log(
            "DownloadTask_mtime-newer.log", ended="2026-08-21 09:01:30"
        )
        newer = self._complete_log(
            "DownloadTask_mtime-older.log", ended="2026-08-21 10:01:30"
        )
        future = datetime(2030, 1, 1).timestamp()
        os.utime(older, (future, future))

        index = self.service.task_index()

        self.assertEqual(index.default_task, newer)

    def test_selected_task_maps_counts_rows_integrity_and_native_log_without_reading_it(self) -> None:
        task = self._complete_log("DownloadTask_complete.log")
        native = self.root / "native.log"
        native.write_text("this must not be parsed", encoding="utf-8")

        snapshot = self.service.selected_task(task)

        self.assertEqual(snapshot.task_template, "A1-A5.json")
        self.assertEqual(snapshot.duration_seconds, 90)
        self.assertEqual(snapshot.planned_count, 5)
        self.assertEqual(snapshot.started_count, 4)
        self.assertEqual(snapshot.count_for(AccountStatus.PRIVATE), 1)
        self.assertEqual(snapshot.completed_with_anomaly_count, 1)
        self.assertEqual(snapshot.pre_start_error_count, 1)
        self.assertEqual(snapshot.not_started_count, 0)
        self.assertEqual(snapshot.unattributed_planned_count, 0)
        self.assertTrue(snapshot.complete)
        self.assertTrue(snapshot.reliable)
        self.assertTrue(snapshot.details_complete)
        self.assertEqual([row.a_number for row in snapshot.account_rows], [1, 2, 3, 4, 5])
        self.assertTrue(snapshot.account_rows[0].completed_with_anomaly)
        self.assertEqual(snapshot.native_log_segments[0].path, native)
        self.assertEqual(snapshot.native_log_segments[0].offset, 10)
        self.assertEqual(snapshot.native_log_segments[0].length, 200)

    def test_old_count_only_log_keeps_missing_fields_unknown(self) -> None:
        task = self._task(
            "DownloadTask_old.log",
            "Task template: old.json\n"
            "【下载账号汇总】\n"
            "账号汇总：完整\n"
            "计划账号：3\n"
            "实际开始：3\n"
            "有新作品下载：1\n"
            "作品均被引擎跳过：1\n"
            "无符合条件作品：1\n"
            "私密账号：0\n"
            "处理异常，需核对（已开始主状态）：0\n"
            "处理中断：0\n"
            "结果不完整：用户停止、异常退出、日志截断、计划账号未全部完成，"
            "或其他证据不足导致无法确认完整任务结果。\n",
        )

        snapshot = self.service.selected_task(task)

        self.assertIsNone(snapshot.started_at)
        self.assertIsNone(snapshot.ended_at)
        self.assertIsNone(snapshot.exit_code)
        self.assertIsNone(snapshot.pre_start_error_count)
        self.assertIsNone(snapshot.not_started_count)
        self.assertIsNone(snapshot.unattributed_planned_count)
        self.assertFalse(snapshot.details_complete)
        self.assertEqual(snapshot.account_rows, ())
        self.assertTrue(snapshot.reliable)

    def test_unreliable_partial_log_preserves_evidence_and_reason(self) -> None:
        task = self._task(
            "DownloadTask_partial-evidence.log",
            "[2026-08-21 09:00:00] Started；Task template: partial.json\n"
            "【下载账号汇总】\n"
            "进程结束时间：2026-08-21 09:02:00\n"
            "退出码：9\n"
            "账号结果：无法可靠汇总；已写入可追溯部分明细；原因：原生日志截断。\n"
            "账号汇总：结果不完整\n"
            "计划账号：5\n实际开始：2\n"
            "有新作品下载：1\n作品均被引擎跳过：0\n无符合条件作品：0\n"
            "私密账号：0\n处理异常，需核对（已开始主状态）：0\n处理中断：1\n"
            "进入处理前异常（不计入主状态合计）：1\n"
            "未开始（不计入主状态合计）：1\n"
            "完成但有异常记录（附加状态，不计入主状态合计）：0\n"
            "处理中断（1）：A2\n进入处理前异常（1）：A3\n未开始（1）：A4\n"
            "账号明细版本：1\n有新作品下载（1）：A1\n"
            "结果不完整：用户停止、异常退出、日志截断、计划账号未全部完成，"
            "或其他证据不足导致无法确认完整任务结果。\n",
        )

        snapshot = self.service.selected_task(task)

        self.assertFalse(snapshot.complete)
        self.assertFalse(snapshot.reliable)
        self.assertIn("原生日志截断。", snapshot.reliability_reasons)
        self.assertEqual(snapshot.unattributed_planned_count, 1)
        self.assertIn("有 1 个计划账号无法可靠归类。", snapshot.reliability_reasons)
        self.assertEqual(len(snapshot.account_rows), 4)

    def test_exit_code_zero_without_account_conclusion_is_not_success(self) -> None:
        task = self._task(
            "DownloadTask_exit-zero-only.log",
            "【下载账号汇总】\n进程结束时间：2026-08-21 09:02:00\n退出码：0\n"
            "结果不完整：用户停止、异常退出、日志截断、计划账号未全部完成，"
            "或其他证据不足导致无法确认完整任务结果。\n",
        )

        snapshot = self.service.selected_task(task)

        self.assertEqual(snapshot.exit_code, 0)
        self.assertIsNone(snapshot.complete)
        self.assertFalse(snapshot.reliable)
        self.assertEqual(
            snapshot.reliability_reasons,
            ("任务日志未提供可靠汇总结论。",),
        )

    def test_cache_hits_same_fingerprint_invalidates_on_change_and_force_bypasses(self) -> None:
        task = self._complete_log("DownloadTask_cache.log")
        first = self.service.selected_task(task)
        cached = self.service.selected_task(task)
        self.assertIs(first, cached)

        forced = self.service.selected_task(task, force_refresh=True)
        self.assertIsNot(first, forced)

        task.write_text(task.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        changed = self.service.selected_task(task)
        self.assertIsNot(forced, changed)
        self.assertNotEqual(forced.fingerprint, changed.fingerprint)
        self.assertFalse(any(self.logs.glob("*.cache")))

    def test_index_cache_invalidates_on_metadata_change_and_force_bypasses(self) -> None:
        task = self._complete_log("DownloadTask_index-cache.log")
        first = self.service.task_index()
        self.assertIs(first, self.service.task_index())
        forced = self.service.task_index(force_refresh=True)
        self.assertIsNot(first, forced)

        task.write_text(task.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        changed = self.service.task_index()
        self.assertIsNot(forced, changed)

    def test_expected_fingerprint_and_changed_during_read_are_rejected(self) -> None:
        task = self._complete_log("DownloadTask_change.log")
        current = self.service._fingerprint(task)
        stale = DashboardFileFingerprint(
            current.normalized_path, current.size + 1, current.mtime_ns
        )
        with self.assertRaises(DashboardFileChangedError):
            self.service.selected_task(task, expected_fingerprint=stale)

        second = DashboardFileFingerprint(
            current.normalized_path, current.size, current.mtime_ns + 1
        )
        with patch.object(self.service, "_fingerprint", side_effect=(current, second)):
            with self.assertRaises(DashboardFileChangedError):
                self.service.selected_task(task, force_refresh=True)

    def test_rejects_arbitrary_paths_instead_of_reading_a_second_source(self) -> None:
        outside = self.root / "DownloadTask_outside.log"
        outside.write_text("secret", encoding="utf-8")

        with self.assertRaises(DashboardTaskUnavailableError):
            self.service.selected_task(outside)

    def test_path_normalization_is_part_of_fingerprint(self) -> None:
        task = self._complete_log("DownloadTask_normalized.log")
        snapshot = self.service.selected_task(task)
        self.assertEqual(
            snapshot.fingerprint.normalized_path,
            os.path.normcase(str(task.resolve())),
        )

    def test_index_prefix_incremental_decoder_accepts_split_utf8_character(self) -> None:
        complete = self._complete_log("DownloadTask_source.log").read_text(
            encoding="utf-8"
        )
        summary = complete[complete.index("【下载账号汇总】") :]
        header = "Task template: split.json\n"
        ascii_padding = "x" * (65535 - len(header.encode("utf-8")))
        task = self._task(
            "DownloadTask_split-utf8.log",
            header + ascii_padding + "汉\n" + summary,
        )

        entry = next(
            item for item in self.service.task_index().entries if item.task_log == task
        )

        self.assertTrue(entry.displayable)
        self.assertEqual(entry.task_template, "split.json")

    def test_oversize_and_invalid_encoding_are_structured_unavailable_entries(self) -> None:
        oversize = self.logs / "DownloadTask_oversize.log"
        with oversize.open("wb") as handle:
            handle.truncate(16 * 1024 * 1024 + 1)
        invalid = self.logs / "DownloadTask_invalid.log"
        invalid.write_bytes(b"\xff\xfe\xff")

        by_path = {entry.task_log: entry for entry in self.service.task_index().entries}

        self.assertEqual(by_path[oversize].state, "unavailable")
        self.assertIn("安全读取上限", by_path[oversize].reason)
        self.assertEqual(by_path[invalid].state, "unavailable")
        self.assertIn("无法读取任务索引", by_path[invalid].reason)

    def test_cancelled_index_and_parse_stop_at_cooperative_checkpoints(self) -> None:
        task = self._complete_log("DownloadTask_cancel.log")
        context = OperationContext()
        self.assertTrue(context.request_cancel())

        with self.assertRaises(TaskCancelled):
            self.service.task_index(context=context)
        with self.assertRaises(TaskCancelled):
            self.service.selected_task(task, context=context)

    def test_concurrent_same_fingerprint_returns_consistent_snapshots(self) -> None:
        task = self._complete_log("DownloadTask_concurrent.log")
        barrier = threading.Barrier(3)
        snapshots = []
        errors = []

        def read() -> None:
            try:
                barrier.wait()
                snapshots.append(self.service.selected_task(task))
            except Exception as exc:  # pragma: no cover - retained for assertion
                errors.append(exc)

        threads = [threading.Thread(target=read) for _ in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join(2.0)

        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(len(snapshots), 2)
        self.assertEqual(snapshots[0], snapshots[1])


class ResultDashboardControllerTests(unittest.TestCase):
    @staticmethod
    def _controller(state: StartupState = StartupState.READY) -> ManagerController:
        controller = ManagerController.__new__(ManagerController)
        controller.startup_state = state
        controller.read_only_reason = ""
        controller.result_dashboard = SimpleNamespace(
            task_index=Mock(return_value="index"),
            selected_task=Mock(return_value="snapshot"),
        )
        return controller

    def test_index_and_selected_snapshot_are_read_only_ready_gated_queries(self) -> None:
        controller = self._controller()
        context = Mock()
        fingerprint = DashboardFileFingerprint("task", 10, 20)

        index = controller.result_dashboard_index(
            force_refresh=True, context=context
        )
        snapshot = controller.result_dashboard_snapshot(
            Path("DownloadTask_one.log"),
            expected_fingerprint=fingerprint,
            force_refresh=True,
            context=context,
        )

        self.assertEqual(index, "index")
        self.assertEqual(snapshot, "snapshot")
        controller.result_dashboard.task_index.assert_called_once_with(
            force_refresh=True, context=context
        )
        controller.result_dashboard.selected_task.assert_called_once_with(
            Path("DownloadTask_one.log"),
            expected_fingerprint=fingerprint,
            force_refresh=True,
            context=context,
        )
        self.assertGreaterEqual(context.raise_if_cancelled.call_count, 4)

    def test_closing_rejects_dashboard_queries_before_service_access(self) -> None:
        controller = self._controller(StartupState.CLOSING)

        with self.assertRaisesRegex(ControllerError, "应用正在关闭"):
            controller.result_dashboard_index()
        with self.assertRaisesRegex(ControllerError, "应用正在关闭"):
            controller.result_dashboard_snapshot(Path("DownloadTask_one.log"))

        controller.result_dashboard.task_index.assert_not_called()
        controller.result_dashboard.selected_task.assert_not_called()


if __name__ == "__main__":
    unittest.main()
