from __future__ import annotations

import hashlib
import os
import tempfile
import time
import unittest
from dataclasses import fields, replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QSettings, QThread
from PySide6.QtWidgets import QApplication, QScrollArea, QTextEdit

from douk_manager.background import (
    BackgroundTaskCoordinator,
    ClosePolicy,
    TaskRecord,
    TaskRejectedError,
    TaskFailure,
    TaskSpec,
    TaskState,
)
from douk_manager.config import AppConfig, ManagedPaths
from douk_manager.controller import ControllerError
from douk_manager.core.backup import BackupService
from douk_manager.core.download_summary import (
    AccountStatus,
    DownloadSummary,
    LocatedNativeLogs,
    NativeLogSegment,
    format_summary_for_task_log,
)
from douk_manager.core.engine import (
    EngineError,
    EngineService,
    ProcessProbe,
    ProcessProbeState,
)
from douk_manager.core.log_stats import (
    ENDPOINTS,
    FIELD_PRESENCE,
    KEYWORDS,
    LOG_STATS_SCHEMA,
    MUST_BE_ABSENT,
    UNAVAILABLE_MARKERS,
    LogStats,
    LogStatsLeakError,
    LogStatsReadError,
    SegmentSelectionStatus,
    analyse_segments,
    render_report,
    select_dashboard_segments,
    self_check_text,
)
from douk_manager.core.result_dashboard import (
    DashboardNativeLogSegment,
    ResultDashboardService,
)
from douk_manager.gui import (
    DownloadSummaryTaskBinding,
    MainWindow,
    _render_log_stats_html,
)
from douk_manager.operation import TaskCancelled
from douk_manager.startup import StartupState
from douk_manager.ui_state import WindowStateStore


SYNTHETIC_SENSITIVE = (
    "sessionid=FAKE-SESSION-DO-NOT-USE",
    "msToken=FAKE-MSTOKEN-0000",
    "sec_user_id=FAKE-SEC-UID-0000",
    "测试账号001",
    "FAKE-AWEME-ID-0001",
    "https://example.invalid/fake/path?fake=1",
    "Z:\\FAKE-PATH\\不存在的目录\\",
)

SYNTHETIC_FORBIDDEN_OUTPUTS = (
    ("Cookie", "Cookie: FAKE-COOKIE-DO-NOT-USE"),
    ("Token", "FAKE-TOKEN-DO-NOT-USE"),
    ("sessionid", "sessionid=FAKE-SESSION-DO-NOT-USE"),
    ("sec_user_id", "sec_user_id=FAKE-SEC-UID-0000"),
    ("作品 ID", "FAKE-AWEME-ID-0001"),
    ("昵称或 mark", "测试账号001"),
    ("作品标题", "FAKE-TITLE-DO-NOT-USE"),
    ("URL", "https://example.invalid/fake/path?fake=1"),
    ("查询串", "fake_query=FAKE-QUERY-DO-NOT-USE"),
    ("本地路径", "Z:\\FAKE-PATH\\不存在的目录\\"),
    ("请求头", "X-FAKE-HEADER: FAKE-HEADER-DO-NOT-USE"),
)


def _segment(path: Path, payload: str | bytes) -> NativeLogSegment:
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    path.write_bytes(raw)
    return NativeLogSegment(path, 0, len(raw))


def _filesystem_state(root: Path) -> dict[str, tuple[str, int, int]]:
    state = {}
    for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
        stat = path.stat()
        state[str(path.relative_to(root))] = (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            stat.st_size,
            stat.st_mtime_ns,
        )
    return state


class _CancelAfter:
    def __init__(self, calls: int) -> None:
        self.remaining = calls
        self.call_count = 0

    def raise_if_cancelled(self) -> None:
        self.call_count += 1
        self.remaining -= 1
        if self.remaining <= 0:
            raise TaskCancelled("synthetic cancellation")


def _paths(root: Path) -> ManagedPaths:
    engine_root = root / "synthetic-engine"
    engine_root.mkdir(parents=True)
    engine = engine_root / "main.exe"
    engine.write_bytes(b"synthetic executable")
    video = root / "synthetic-video"
    index = root / "synthetic-index"
    video.mkdir()
    index.mkdir()
    config = AppConfig(
        engine_exe=str(engine),
        video_root=str(video),
        index_root=str(index),
    )
    paths = ManagedPaths.from_config(config, root / "synthetic-manager")
    paths.ensure_manager_directories()
    return paths


def _summary(segments: tuple[NativeLogSegment, ...], reason: str = "") -> DownloadSummary:
    return DownloadSummary(
        planned_count=0,
        started_outcomes=(),
        pre_start_errors=(),
        not_started=(),
        primary_status_counts={},
        completed_with_anomaly=(),
        complete=True,
        reliable=not reason,
        reasons=(),
        located=LocatedNativeLogs(segments, "size-delta-anchor", not reason, reason),
        exit_code=0,
    )


class LogStatsAnalysisTests(unittest.TestCase):
    def test_empty_segments_return_zero_stats_and_clean_report(self) -> None:
        stats = analyse_segments(())
        self.assertEqual(stats.schema, LOG_STATS_SCHEMA)
        self.assertEqual(stats.lines, 0)
        self.assertEqual(stats.bytes_analysed, 0)
        self.assertEqual(stats.active_minutes, 0)
        self.assertTrue(self_check_text(render_report(stats)).clean)

    def test_single_line_tracks_minute_and_level(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            segment = _segment(
                Path(directory) / "synthetic.log",
                "2026-01-02 03:04:59 [INFO] synthetic\n",
            )
            stats = analyse_segments((segment,))
        self.assertEqual(stats.lines, 1)
        self.assertEqual(stats.window_start, "2026-01-02 03:04")
        self.assertEqual(stats.window_end, "2026-01-02 03:04")
        self.assertEqual(stats.active_minutes, 1)
        self.assertEqual(stats.levels["INFO"], 1)

    def test_nonzero_offset_and_multiple_segments_only_count_selected_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.log"
            prefix = b"2026-01-02 03:00:00 [ERROR] outside\n"
            inside = b"2026-01-02 03:01:00 [INFO] inside\n"
            first.write_bytes(prefix + inside)
            second = _segment(
                root / "second.log", "2026-01-02 03:02:00 [WARNING] second\n"
            )
            stats = analyse_segments(
                (NativeLogSegment(first, len(prefix), len(inside)), second)
            )
        self.assertEqual(stats.lines, 2)
        self.assertEqual(stats.levels.get("ERROR", 0), 0)
        self.assertEqual(stats.levels["INFO"], 1)
        self.assertEqual(stats.levels["WARNING"], 1)

    def test_http_and_abnormal_status_share_403_rate_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lines = (
                ["Response Code: 200\n"] * 3
                + ["Response Code: 403\n"]
                + ["响应码异常 Client error '403' synthetic\n"]
                + ["响应码异常 Server error '404' synthetic\n"]
            )
            stats = analyse_segments(
                (_segment(Path(directory) / "status.log", "".join(lines)),)
            )
        self.assertEqual(stats.http.total, 4)
        self.assertEqual(stats.http.counts["403"], 1)
        self.assertEqual(stats.abnormal.total, 2)
        self.assertEqual(stats.abnormal.counts, {"403": 1, "404": 1})
        self.assertAlmostEqual(stats.http_403_rate, 2 / 6)

    def test_abnormal_only_status_has_nonzero_rate_denominator(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stats = analyse_segments(
                (
                    _segment(
                        Path(directory) / "abnormal.log",
                        "响应码异常 Client error '403' synthetic\n",
                    ),
                )
            )
        self.assertEqual(stats.http.total, 0)
        self.assertEqual(stats.abnormal.total, 1)
        self.assertEqual(stats.http_403_rate, 1.0)
        self.assertEqual(stats.request_failed, 1)

    def test_endpoints_and_fields_only_count_request_lines(self) -> None:
        endpoint = ENDPOINTS[0]
        request = (
            f"2026-01-02 03:04:00 [INFO]:  URL: https://example.invalid/{endpoint}"
            "?a_bogus=FAKE&x-secsdk-web-signature=FAKE\n"
        )
        response = (
            f"2026-01-02 03:04:01 [INFO] Response URL: https://example.invalid/{endpoint}"
            "?a_bogus=FAKE&x-secsdk-web-signature=FAKE\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            stats = analyse_segments(
                (_segment(Path(directory) / "requests.log", request + response),)
            )
        self.assertEqual(stats.endpoints[endpoint], 1)
        self.assertEqual(stats.signatures.request_lines, 1)
        self.assertEqual(stats.signatures.presence["a_bogus"], 1)
        self.assertEqual(stats.signatures.presence["x-secsdk-web-signature"], 1)

    def test_a_bogus_and_signature_each_count_five_request_lines(self) -> None:
        # Equality is normal; external signature health is judged only by the latter.
        line = (
            "2026-01-02 03:04:00 [INFO]:  URL: https://example.invalid/"
            "aweme/v1/web/aweme/post/?a_bogus=FAKE&"
            "x-secsdk-web-signature=FAKE\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            stats = analyse_segments(
                (_segment(Path(directory) / "five-requests.log", line * 5),)
            )
        self.assertEqual(stats.signatures.presence["a_bogus"], 5)
        self.assertEqual(stats.signatures.presence["x-secsdk-web-signature"], 5)

    def test_failure_keywords_are_separate_and_private_is_not_request_failed(self) -> None:
        payload = "\n".join(
            ("响应码异常", "下载中断", "视频下载地址解析失败", "私密账号", "私密账号")
        ) + "\n"
        with tempfile.TemporaryDirectory() as directory:
            stats = analyse_segments(
                (_segment(Path(directory) / "failures.log", payload),)
            )
        self.assertEqual(stats.failures.resp_code_abnormal, 1)
        self.assertEqual(stats.failures.download_interrupted, 1)
        self.assertEqual(stats.failures.url_parse_failed, 1)
        self.assertEqual(stats.failures.private_account, 2)
        self.assertEqual(stats.request_failed, 3)
        self.assertEqual(stats.unavailable, 0)

    def test_403_timeout_and_private_account_remain_separate(self) -> None:
        cases = (
            ("Response Code: 403\n", 1, 0),
            ("下载中断：ReadTimeout synthetic\n", 1, 0),
            ("私密账号\n", 0, 1),
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (payload, expected_failed, expected_private) in enumerate(cases):
                with self.subTest(index=index):
                    stats = analyse_segments(
                        (_segment(root / f"case-{index}.log", payload),)
                    )
                    self.assertEqual(stats.request_failed, expected_failed)
                    self.assertEqual(stats.unavailable, 0)
                    self.assertEqual(stats.failures.private_account, expected_private)

    def test_line_and_byte_limits_are_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            segment = _segment(Path(directory) / "large.log", "one\ntwo\nthree\n")
            by_lines = analyse_segments((segment,), max_lines=2)
            by_bytes = analyse_segments((segment,), max_bytes=5)
        self.assertTrue(by_lines.truncated)
        self.assertEqual(by_lines.lines, 2)
        self.assertEqual(by_lines.truncate_reason, "line_limit")
        self.assertTrue(by_bytes.truncated)
        self.assertLessEqual(by_bytes.bytes_analysed, 5)
        self.assertEqual(by_bytes.truncate_reason, "byte_limit")

    def test_declared_segment_beyond_file_raises_fixed_read_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "short.log"
            path.write_bytes(b"short\n")
            with self.assertRaises(LogStatsReadError) as caught:
                analyse_segments((NativeLogSegment(path, 0, 100),))
        self.assertNotIn(str(path), str(caught.exception))

    def test_offset_immediately_after_utf8_bom_keeps_first_appended_line(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bom.log"
            appended = b"2026-01-02 03:04:00 [INFO] appended\n"
            path.write_bytes(b"\xef\xbb\xbf" + appended)
            stats = analyse_segments(
                (NativeLogSegment(path, 3, len(appended)),)
            )
        self.assertEqual(stats.lines, 1)
        self.assertEqual(stats.levels["INFO"], 1)

    def test_invalid_utf8_and_incomplete_last_line_raise_fixed_read_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            invalid = _segment(root / "invalid.log", b"\xff\xfe synthetic\n")
            incomplete = _segment(root / "incomplete.log", b"synthetic")
            for segment in (invalid, incomplete):
                with self.subTest(name=segment.path.name):
                    with self.assertRaises(LogStatsReadError) as caught:
                        analyse_segments((segment,))
                    self.assertNotIn(str(segment.path), str(caught.exception))

    def test_sensitive_input_is_accepted_but_values_cannot_reach_report(self) -> None:
        payload = "2026-01-02 03:04:00 [INFO]:  URL: " + " ".join(
            SYNTHETIC_SENSITIVE
        ) + "\n"
        with tempfile.TemporaryDirectory() as directory:
            stats = analyse_segments(
                (_segment(Path(directory) / "sensitive-input.log", payload),)
            )
        report = render_report(stats)
        self.assertEqual(stats.lines, 1)
        for needle in SYNTHETIC_SENSITIVE:
            self.assertNotIn(needle, report)

    def test_mapping_keys_are_constant_or_three_digit_status_codes(self) -> None:
        payload = "RANDOM-UNTRUSTED-KEY Response Code: 418\n"
        with tempfile.TemporaryDirectory() as directory:
            stats = analyse_segments(
                (_segment(Path(directory) / "keys.log", payload),)
            )
        self.assertEqual(set(stats.http.counts), {"418"})
        self.assertTrue(all(key.isdigit() and len(key) == 3 for key in stats.http.counts))
        self.assertTrue(set(stats.endpoints).issubset(ENDPOINTS))
        self.assertTrue(set(stats.signatures.presence).issubset(FIELD_PRESENCE))
        self.assertTrue(set(stats.keywords).issubset(KEYWORDS))

    def test_analysis_checks_cancellation_repeatedly_and_leaves_logs_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            segment = _segment(root / "cancel.log", "synthetic\n" * 5000)
            before = _filesystem_state(root)
            context = _CancelAfter(3)
            with self.assertRaises(TaskCancelled):
                analyse_segments((segment,), context=context)
            after = _filesystem_state(root)
            completed = analyse_segments((segment,))
        self.assertGreaterEqual(context.call_count, 3)
        self.assertEqual(before, after)
        self.assertEqual(completed.lines, 5000)


class LogStatsWhitelistAndReportTests(unittest.TestCase):
    def test_log_stats_field_whitelist_is_exact_and_has_no_leak_field(self) -> None:
        expected = {
            "schema",
            "lines",
            "bytes_analysed",
            "window_start",
            "window_end",
            "active_minutes",
            "levels",
            "http",
            "abnormal",
            "endpoints",
            "signatures",
            "failures",
            "keywords",
            "truncated",
            "truncate_reason",
        }
        actual = {field.name for field in fields(LogStats)}
        self.assertEqual(actual, expected)
        self.assertNotIn("leak", actual)

        hints = get_type_hints(LogStats)
        string_fields = {
            name
            for name, hint in hints.items()
            if hint is str or hint == (str | None)
        }
        self.assertEqual(
            string_fields, {"window_start", "window_end", "truncate_reason"}
        )

    def test_report_has_schema_sections_fixed_unavailable_boundary_and_clean_tail(self) -> None:
        report = render_report(analyse_segments(()))
        self.assertEqual(report.splitlines()[0].split(), ["schema", "1"])
        self.assertIn("[http status]", report)
        self.assertIn("[abnormal-path status]", report)
        self.assertIn(
            "This version does not infer whether accounts are deactivated or banned",
            report,
        )
        self.assertIn("[LEAK SELF-CHECK]", report)
        self.assertTrue(report.endswith("RESULT: CLEAN\n"))
        self.assertEqual(UNAVAILABLE_MARKERS, ())

        report_lines = report.splitlines()
        metadata_labels = (
            "schema",
            "lines",
            "bytes_analysed",
            "window",
            "active_minutes",
            "log_location_reason",
            "truncated",
            "truncate_reason",
        )
        metadata_width = max(len(label) for label in metadata_labels)
        for line, label in zip(
            report_lines[: len(metadata_labels)], metadata_labels, strict=True
        ):
            self.assertEqual(line[:metadata_width].rstrip(), label)
            self.assertEqual(line[metadata_width : metadata_width + 2], "  ")

        endpoints_start = report_lines.index(
            "[endpoints seen] (path only, query discarded)"
        ) + 1
        endpoint_width = max(len(endpoint) for endpoint in ENDPOINTS)
        for line, endpoint in zip(
            report_lines[endpoints_start : endpoints_start + len(ENDPOINTS)],
            ENDPOINTS,
            strict=True,
        ):
            self.assertEqual(line[2 : 2 + endpoint_width].rstrip(), endpoint)
            self.assertEqual(
                line[2 + endpoint_width : 4 + endpoint_width], "  "
            )

    def test_chinese_report_localises_semantic_labels_and_remains_clean(self) -> None:
        report = render_report(analyse_segments(()), language="zh-CN")
        for label in (
            "统计格式版本：",
            "已分析字节数：",
            "请求失败：",
            "下载中断：",
            "签名字段覆盖率：",
        ):
            self.assertIn(label, report)
        self.assertNotIn("request_failed", report)
        self.assertNotIn("download_interrupted", report)
        self.assertIn("[泄漏自检]", report)
        self.assertTrue(report.endswith("RESULT: CLEAN\n"))
        self.assertTrue(self_check_text(report).clean)

    def test_report_rejects_unknown_language(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported diagnostic report language"):
            render_report(analyse_segments(()), language="synthetic")

    def test_located_reason_is_whitelisted_and_unknown_text_is_not_reflected(self) -> None:
        unsafe = r"Z:\FAKE-PATH\secret.log"
        report = render_report(analyse_segments(()), located_reason=unsafe)
        self.assertNotIn(unsafe, report)
        self.assertIn("log_location_reason  unknown", report)

        unsafe_truncate = replace(analyse_segments(()), truncate_reason=unsafe)
        truncate_report = render_report(unsafe_truncate)
        self.assertNotIn(unsafe, truncate_report)
        self.assertIn(
            ["truncate_reason", "unknown"],
            [line.split() for line in truncate_report.splitlines()],
        )

    def test_product_self_check_detects_bad_output_without_echoing_values(self) -> None:
        text = "safe prefix\nsessionid=FAKE-SESSION-DO-NOT-USE\n"
        check = self_check_text(text)
        self.assertFalse(check.clean)
        self.assertEqual(check.counts["sessionid"], 1)
        error = LogStatsLeakError(check)
        self.assertIn("sessionid", str(error))
        self.assertNotIn("FAKE-SESSION-DO-NOT-USE", str(error))

    def test_self_check_word_boundary_rejects_real_forms_not_kssessionid(self) -> None:
        safe = self_check_text("kssessionid=FAKE\n")
        unsafe = self_check_text(
            "sessionid=FAKE; sessionid FAKE 'sessionid'\n"
        )
        self.assertTrue(safe.clean)
        self.assertEqual(unsafe.counts["sessionid"], 3)

    def test_normal_structural_labels_do_not_trigger_product_self_check(self) -> None:
        stats = analyse_segments(())
        report = render_report(stats)
        check = self_check_text(report)
        self.assertTrue(check.clean)
        self.assertTrue(all(check.counts[name] == 0 for name in MUST_BE_ABSENT))

    def test_all_eleven_forbidden_output_categories_are_individually_absent(self) -> None:
        payload = " ".join(value for _label, value in SYNTHETIC_FORBIDDEN_OUTPUTS) + "\n"
        with tempfile.TemporaryDirectory() as directory:
            stats = analyse_segments(
                (_segment(Path(directory) / "all-forbidden-input.log", payload),)
            )
        report = render_report(stats)
        for label, needle in SYNTHETIC_FORBIDDEN_OUTPUTS:
            with self.subTest(category=label):
                self.assertNotIn(needle, report)


class DashboardSegmentSelectionTests(unittest.TestCase):
    def test_new_format_segments_are_adapted_without_changing_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = _segment(root / "first.log", "first\n")
            second = _segment(root / "second.log", "second\n")
            selected, status = select_dashboard_segments(
                (
                    DashboardNativeLogSegment(first.path, first.offset, first.length),
                    DashboardNativeLogSegment(second.path, second.offset, second.length),
                )
            )
        self.assertEqual(selected, (first, second))
        self.assertIs(status, SegmentSelectionStatus.READY)

    def test_missing_offsets_disable_without_guessing(self) -> None:
        selected, status = select_dashboard_segments(
            (DashboardNativeLogSegment(Path("synthetic.log"), None, None),)
        )
        self.assertEqual(selected, ())
        self.assertIs(status, SegmentSelectionStatus.MISSING_RANGE)

    def test_missing_files_disable_and_partial_selection_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            available = _segment(root / "available.log", "available\n")
            missing = root / "missing.log"
            selected, status = select_dashboard_segments(
                (
                    DashboardNativeLogSegment(missing, 0, 10),
                    DashboardNativeLogSegment(
                        available.path, available.offset, available.length
                    ),
                )
            )
            only_missing, missing_status = select_dashboard_segments(
                (DashboardNativeLogSegment(missing, 0, 10),)
            )
        self.assertEqual(selected, (available,))
        self.assertIs(status, SegmentSelectionStatus.PARTIAL)
        self.assertEqual(only_missing, ())
        self.assertIs(missing_status, SegmentSelectionStatus.MISSING_FILE)

    def test_current_and_dashboard_sources_use_the_same_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            segment = _segment(Path(directory) / "same.log", "Response Code: 403\n")
            selected, status = select_dashboard_segments(
                (DashboardNativeLogSegment(segment.path, 0, segment.length),)
            )
            self.assertIs(status, SegmentSelectionStatus.READY)
            self.assertEqual(analyse_segments((segment,)), analyse_segments(selected))

    def test_dashboard_parser_preserves_new_ranges_and_marks_legacy_range_missing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "first.log"
            second = root / "second.log"
            task = root / "DownloadTask_synthetic.log"
            summary = replace(
                _summary(
                    (
                        NativeLogSegment(first, 10, 20),
                        NativeLogSegment(second, 30, 40),
                    )
                ),
                primary_status_counts={status: 0 for status in AccountStatus},
            )
            block = format_summary_for_task_log(
                summary, datetime(2026, 1, 2, 3, 4, 5)
            ).replace(
                "【状态说明】",
                "原生日志：Z:\\FAKE-PATH\\legacy.log\n【状态说明】",
            )
            task.write_text(
                "[2026-01-02 03:00:00] Started；Task template: synthetic.json\n\n"
                + block,
                encoding="utf-8",
            )
            before = task.read_bytes()
            snapshot = ResultDashboardService(root).selected_task(task)
            self.assertEqual(task.read_bytes(), before)
        self.assertEqual(
            snapshot.native_log_segments,
            (
                DashboardNativeLogSegment(first, 10, 20),
                DashboardNativeLogSegment(second, 30, 40),
                DashboardNativeLogSegment(
                    Path("Z:\\FAKE-PATH\\legacy.log"), None, None
                ),
            ),
        )


class EngineLogStatsTests(unittest.TestCase):
    def _service(self, root: Path) -> EngineService:
        paths = _paths(root)
        service = EngineService(
            paths,
            AppConfig(engine_exe=str(paths.engine_exe)),
            BackupService(paths),
        )
        service.external_running = Mock(return_value=False)
        return service

    def test_analyse_current_run_then_export_without_rereading_native_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            segment = _segment(root / "native.log", "Response Code: 403\n")
            scope = os.path.normcase(os.path.abspath(root / "DownloadTask_synthetic.log"))
            service.current = SimpleNamespace(
                task_log=Path(scope),
                running=False,
                _summary_result=_summary((segment,)),
            )
            stats = service.analyse_run_logs(scope)
            segment.path.unlink()
            report = service.export_diagnostic_report(scope)
            self.assertEqual(stats.http.counts["403"], 1)
            self.assertEqual(report.parent, service.paths.logs / "Diagnostics")
            self.assertIn("RESULT: CLEAN", report.read_text(encoding="utf-8"))

    def test_sensitive_input_exports_clean_and_does_not_touch_task_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            native = _segment(root / "native.log", " ".join(SYNTHETIC_SENSITIVE) + "\n")
            task_log = root / "DownloadTask_synthetic.log"
            task_log.write_text("synthetic task record\n", encoding="utf-8")
            before = task_log.read_bytes()
            scope = "synthetic-history"
            service.analyse_run_logs(scope, segments=(native,))
            report = service.export_diagnostic_report(scope)
            self.assertEqual(task_log.read_bytes(), before)
            text = report.read_text(encoding="utf-8")
            for needle in SYNTHETIC_SENSITIVE:
                self.assertNotIn(needle, text)

    def test_analysis_and_export_leave_entire_synthetic_log_tree_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            native_root = root / "synthetic-native-logs"
            native_root.mkdir()
            segment = _segment(native_root / "native.log", "Response Code: 200\n")
            task_log = native_root / "DownloadTask_synthetic.log"
            task_log.write_text("synthetic task evidence\n", encoding="utf-8")
            before = _filesystem_state(native_root)
            service.analyse_run_logs("scope", segments=(segment,))
            after_analysis = _filesystem_state(native_root)
            report = service.export_diagnostic_report("scope")
            after_export = _filesystem_state(native_root)
            self.assertEqual(before, after_analysis)
            self.assertEqual(before, after_export)
            self.assertEqual(report.parent, service.paths.logs / "Diagnostics")
            self.assertEqual(set(native_root.iterdir()), {task_log, segment.path})

    def test_running_engine_is_rejected_before_log_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory))
            service.current = SimpleNamespace(task_log=Path("synthetic"), running=True)
            with self.assertRaises(EngineError):
                service.analyse_run_logs("synthetic")

    def test_external_or_unknown_engine_is_rejected_before_log_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            service = self._service(Path(directory))
            segment = NativeLogSegment(Path("Z:/FAKE-PATH/missing.log"), 0, 1)
            del service.external_running
            for unsafe_state in (
                ProcessProbeState.RUNNING,
                ProcessProbeState.UNKNOWN,
            ):
                with self.subTest(unsafe_state=unsafe_state), patch.object(
                    service,
                    "probe_external_running",
                    return_value=ProcessProbe(unsafe_state, "synthetic probe"),
                ) as probe:
                    with self.assertRaisesRegex(
                        EngineError, "运行中|状态无法确认"
                    ):
                        service.analyse_run_logs("synthetic", segments=(segment,))
                    probe.assert_called_once_with()

    def test_bad_render_is_blocked_before_any_final_or_partial_file_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            segment = _segment(root / "native.log", "synthetic\n")
            service.analyse_run_logs("scope", segments=(segment,))
            with patch(
                "douk_manager.core.engine.render_report",
                return_value="sessionid=FAKE-SESSION-DO-NOT-USE\n",
            ):
                with self.assertRaises(LogStatsLeakError):
                    service.export_diagnostic_report("scope")
            diagnostics = service.paths.logs / "Diagnostics"
            self.assertEqual(tuple(diagnostics.glob("*")) if diagnostics.exists() else (), ())

    def test_export_runs_product_self_check_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            segment = _segment(root / "native.log", "synthetic\n")
            service.analyse_run_logs("scope", segments=(segment,))
            with patch(
                "douk_manager.core.engine.self_check_text",
                wraps=self_check_text,
            ) as check:
                service.export_diagnostic_report("scope")
            check.assert_called_once()

    def test_unique_exports_do_not_overwrite_and_cancel_cleans_temporary_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            segment = _segment(root / "native.log", "synthetic\n")
            service.analyse_run_logs("scope", segments=(segment,))
            first = service.export_diagnostic_report("scope")
            second = service.export_diagnostic_report("scope")
            self.assertNotEqual(first, second)
            context = _CancelAfter(2)
            with self.assertRaises(TaskCancelled):
                service.export_diagnostic_report("scope", context=context)
            self.assertEqual(
                {path.name for path in first.parent.iterdir()}, {first.name, second.name}
            )

    def test_dual_export_writes_chinese_and_english_from_one_cached_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            segment = _segment(root / "native.log", "Response Code: 403\n")
            service.analyse_run_logs("scope", segments=(segment,))
            segment.path.unlink()

            with patch(
                "douk_manager.core.engine.self_check_text",
                wraps=self_check_text,
            ) as check:
                chinese, english = service.export_diagnostic_reports("scope")

            self.assertEqual(check.call_count, 2)
            self.assertTrue(chinese.name.endswith("-diagnostic-zh-CN.txt"))
            self.assertTrue(english.name.endswith("-diagnostic-en.txt"))
            self.assertEqual(chinese.parent, service.paths.logs / "Diagnostics")
            self.assertEqual(english.parent, chinese.parent)
            chinese_text = chinese.read_text(encoding="utf-8")
            english_text = english.read_text(encoding="utf-8")
            self.assertIn("请求失败：1", chinese_text)
            self.assertIn("request_failed", english_text)
            self.assertTrue(self_check_text(chinese_text).clean)
            self.assertTrue(self_check_text(english_text).clean)

    def test_dual_export_blocks_both_files_when_either_report_fails_self_check(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            segment = _segment(root / "native.log", "synthetic\n")
            service.analyse_run_logs("scope", segments=(segment,))
            clean = render_report(analyse_segments(()), language="zh-CN")
            with patch(
                "douk_manager.core.engine.render_report",
                side_effect=(clean, "sessionid=FAKE-SESSION-DO-NOT-USE\n"),
            ):
                with self.assertRaises(LogStatsLeakError):
                    service.export_diagnostic_reports("scope")
            diagnostics = service.paths.logs / "Diagnostics"
            self.assertEqual(tuple(diagnostics.glob("*")) if diagnostics.exists() else (), ())

    def test_dual_export_cancellation_removes_both_temporary_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            service = self._service(root)
            segment = _segment(root / "native.log", "synthetic\n")
            service.analyse_run_logs("scope", segments=(segment,))
            with self.assertRaises(TaskCancelled):
                service.export_diagnostic_reports("scope", context=_CancelAfter(3))
            diagnostics = service.paths.logs / "Diagnostics"
            self.assertTrue(diagnostics.is_dir())
            self.assertEqual(tuple(diagnostics.iterdir()), ())


class LogStatsGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, root: Path) -> tuple[MainWindow, object]:
        settings = QSettings(
            str(root / "ui-state.ini"), QSettings.Format.IniFormat
        )
        store = WindowStateStore(settings)
        home_patch = patch.dict(os.environ, {"DOUK_MANAGER_HOME": str(root)})
        home_patch.start()
        window = MainWindow(window_state_store=store)
        window.poll_timer.stop()
        window.controller.startup_state = StartupState.READY
        window.controller.engine.external_running = Mock(return_value=False)
        window._apply_action_gate()
        return window, home_patch

    def _dispose(self, window: MainWindow, home_patch: object) -> None:
        window.poll_timer.stop()
        if window.coordinator.has_active_tasks():
            window.coordinator.begin_closing()
            deadline = datetime.now().timestamp() + 4
            while window.coordinator.has_active_tasks() and datetime.now().timestamp() < deadline:
                self.app.processEvents()
        window.hide()
        window.deleteLater()
        self.app.processEvents()
        for handler in list(window.controller.logger.handlers):
            window.controller.logger.removeHandler(handler)
            handler.close()
        home_patch.stop()

    def test_controls_exist_auto_is_default_off_and_no_input_clean_state_is_shown(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            try:
                self.assertFalse(window.log_stats_auto_checkbox.isChecked())
                self.assertEqual(window.log_stats_group.title(), "本次运行日志统计（只含统计量）")
                self.assertIn("尚未执行产物自检", window.log_stats_self_check.text())
                self.assertEqual(
                    window.log_stats_export_button.text(), "导出中英文安全诊断报告"
                )
            finally:
                self._dispose(window, home_patch)

    def test_log_stats_layout_uses_responsive_columns_and_scrolls_at_restored_size(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            try:
                self.assertIsInstance(window.dashboard_page, QScrollArea)
                stats = analyse_segments(())
                unsafe = r"Z:\FAKE-PATH\secret.log"
                wide_html = _render_log_stats_html(
                    stats, located_reason=unsafe, columns=5
                )
                self.assertEqual(
                    wide_html.count('data-log-stats-section="true"'), 5
                )
                self.assertIn("font-size:18px", wide_html)
                self.assertIn("font-size:15px", wide_html)
                self.assertIn("font-size:16px", wide_html)
                self.assertIn("已分析字节数", wide_html)
                self.assertIn("请求失败", wide_html)
                self.assertIn("下载中断", wide_html)
                self.assertNotIn("bytes_analysed", wide_html)
                self.assertNotIn("request_failed", wide_html)
                self.assertNotIn("download_interrupted", wide_html)
                self.assertNotIn("异常 none", wide_html)
                self.assertNotIn(unsafe, wide_html)
                self.assertEqual(
                    window.log_stats_output._column_count_for_width(1800), 5
                )
                self.assertEqual(
                    window.log_stats_output._column_count_for_width(1000), 3
                )
                self.assertEqual(
                    window.log_stats_output._column_count_for_width(500), 1
                )
                window.resize(window.minimumWidth(), window.minimumHeight())
                with patch.object(window, "refresh_result_dashboard"):
                    window.tabs.setCurrentWidget(window.dashboard_page)
                window.log_stats_expand_button.setChecked(True)
                window._apply_log_stats_result(stats, "synthetic")
                window.show()
                self.app.processEvents()

                self.assertEqual(
                    window.log_stats_output.lineWrapMode(),
                    QTextEdit.LineWrapMode.WidgetWidth,
                )
                rendered_text = window.log_stats_output.toPlainText()
                for heading in (
                    "运行概览",
                    "日志与响应",
                    "请求端点与失败",
                    "签名与参数",
                    "业务事件",
                ):
                    self.assertIn(heading, rendered_text)
                self.assertGreater(
                    window.log_stats_output.verticalScrollBar().maximum(), 0
                )
                self.assertGreater(
                    window.dashboard_page.verticalScrollBar().maximum(), 0
                )
                self.assertLess(
                    window.log_stats_output.geometry().bottom(),
                    window.log_stats_self_check.geometry().top(),
                )
            finally:
                self._dispose(window, home_patch)

    def test_log_stats_result_context_warns_after_dashboard_task_switch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                first = root / "DownloadTask_first.log"
                second = root / "DownloadTask_second.log"
                first_key = window._dashboard_key(first)
                second_key = window._dashboard_key(second)
                window._dashboard_entries = {
                    first_key: SimpleNamespace(task_log=first),
                    second_key: SimpleNamespace(task_log=second),
                }
                window.dashboard_task_selector.blockSignals(True)
                window.dashboard_task_selector.addItem("first", first_key)
                window.dashboard_task_selector.addItem("second", second_key)
                window.dashboard_task_selector.setCurrentIndex(0)
                window.dashboard_task_selector.blockSignals(False)

                window._apply_log_stats_result(analyse_segments(()), str(first))
                self.assertFalse(window.log_stats_result_context.property("stale"))
                self.assertIn("所选任务", window.log_stats_result_context.text())

                with patch.object(window, "_load_dashboard_selection"):
                    window.dashboard_task_selector.setCurrentIndex(1)
                self.assertTrue(window.log_stats_result_context.property("stale"))
                self.assertIn(
                    "上一次分析结果", window.log_stats_result_context.text()
                )
                self.assertNotIn(str(root), window.log_stats_result_context.text())

                window._apply_log_stats_result(analyse_segments(()), second.name)
                self.assertFalse(window.log_stats_result_context.property("stale"))
                self.assertIn("所选任务", window.log_stats_result_context.text())
            finally:
                self._dispose(window, home_patch)

    def test_auto_setting_persists_only_in_ui_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                engine_settings = root / "synthetic-volume" / "settings.json"
                engine_settings.parent.mkdir(parents=True, exist_ok=True)
                engine_settings.write_text("synthetic engine settings\n", encoding="utf-8")
                synthetic_paths = replace(
                    window.controller.paths, active_settings=engine_settings
                )
                window.controller.paths = synthetic_paths
                window.controller.engine.paths = synthetic_paths
                before = engine_settings.read_bytes()
                window.log_stats_auto_checkbox.setChecked(True)
                self.assertEqual(engine_settings.read_bytes(), before)
                self.assertTrue(
                    window._window_state_store._settings.value(
                        "log_stats/auto_analyse", False, type=bool
                    )
                )
            finally:
                self._dispose(window, home_patch)

    def test_analysis_and_export_specs_use_exact_resources_and_allow_each_other(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            native = _segment(root / "native.log", "synthetic\n")
            captured = []
            try:
                window._latest_log_stats_run = SimpleNamespace(
                    task_log=root / "DownloadTask_synthetic.log",
                    running=False,
                    _summary_result=_summary((native,)),
                )

                def capture(spec, action, **kwargs):
                    captured.append((spec, action, kwargs))
                    return "synthetic-task"

                with patch.object(window, "_submit_background", side_effect=capture):
                    window._start_current_log_stats()
                    window._log_stats_result = analyse_segments((native,))
                    window._log_stats_scope = "synthetic-scope"
                    window._start_log_stats_export()
                analyse_spec, export_spec = captured[0][0], captured[1][0]
                self.assertEqual(analyse_spec.task_type, "log_stats_analyse")
                self.assertEqual(analyse_spec.resource_keys, frozenset({"task_logs"}))
                self.assertEqual(export_spec.task_type, "log_stats_export")
                self.assertEqual(export_spec.resource_keys, frozenset({"diagnostics_dir"}))
                self.assertIs(analyse_spec.close_policy, ClosePolicy.CANCEL)
                self.assertIs(export_spec.close_policy, ClosePolicy.CANCEL)
            finally:
                self._dispose(window, home_patch)

    def test_controller_rechecks_external_engine_before_log_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            native = _segment(root / "native.log", "synthetic\n")
            try:
                window.controller.engine.external_running.return_value = True
                with patch.object(
                    window.controller.engine, "analyse_run_logs"
                ) as engine_analysis:
                    with self.assertRaisesRegex(
                        ControllerError, "运行中|状态无法确认"
                    ):
                        window.controller.analyse_run_logs(
                            "synthetic", segments=(native,)
                        )
                engine_analysis.assert_not_called()
            finally:
                self._dispose(window, home_patch)

    def test_external_or_unknown_engine_blocks_both_gui_paths_before_submission(
        self,
    ) -> None:
        for unsafe_state in ("RUNNING", "UNKNOWN"):
            with self.subTest(
                unsafe_state=unsafe_state
            ), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                window, home_patch = self._window(root)
                native = _segment(root / "native.log", "synthetic\n")
                try:
                    window._latest_log_stats_run = SimpleNamespace(
                        task_log=root / "DownloadTask_current.log",
                        running=False,
                        _summary_result=_summary((native,)),
                    )
                    window._dashboard_snapshot = SimpleNamespace(
                        task_log=root / "DownloadTask_history.log",
                        native_log_segments=(
                            DashboardNativeLogSegment(
                                native.path, native.offset, native.length
                            ),
                        ),
                    )
                    window.controller.engine.external_running.return_value = True
                    window.controller._last_engine_running = True
                    window._update_log_stats_current_availability()
                    window._update_log_stats_history_availability()
                    self.assertFalse(window.log_stats_current_button.isEnabled())
                    self.assertFalse(window.log_stats_history_button.isEnabled())
                    with patch.object(window, "_submit_background") as submit:
                        window._start_current_log_stats()
                        window._start_historical_log_stats()
                    submit.assert_not_called()
                    self.assertRegex(
                        window.log_stats_output.toPlainText(),
                        "运行中|状态无法确认",
                    )
                finally:
                    self._dispose(window, home_patch)

    def test_current_and_history_actions_use_controller_log_analysis_entry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            native = _segment(root / "native.log", "synthetic\n")
            captured = []
            try:
                window._latest_log_stats_run = SimpleNamespace(
                    task_log=root / "DownloadTask_current.log",
                    running=False,
                    _summary_result=_summary((native,)),
                )
                window._dashboard_snapshot = SimpleNamespace(
                    task_log=root / "DownloadTask_history.log",
                    native_log_segments=(
                        DashboardNativeLogSegment(
                            native.path, native.offset, native.length
                        ),
                    ),
                )

                def capture(spec, action, **kwargs):
                    captured.append(action)
                    return None

                with patch.object(
                    window.controller,
                    "analyse_run_logs",
                    create=True,
                    return_value=analyse_segments((native,)),
                ) as controller_analysis, patch.object(
                    window, "_submit_background", side_effect=capture
                ):
                    window._start_current_log_stats()
                    window._start_historical_log_stats()
                    self.assertEqual(len(captured), 2)
                    for action in captured:
                        action(None)
                self.assertEqual(controller_analysis.call_count, 2)
            finally:
                self._dispose(window, home_patch)

    def test_specific_coordinator_resources_conflict_and_parallelism(self) -> None:
        coordinator = BackgroundTaskCoordinator()
        analysis = TaskSpec(
            task_type="log_stats_analyse",
            display_name="synthetic analysis",
            resource_keys=frozenset({"task_logs"}),
            deduplicate_key="log_stats_analyse:synthetic",
        )
        export = TaskSpec(
            task_type="log_stats_export",
            display_name="synthetic export",
            resource_keys=frozenset({"diagnostics_dir"}),
            deduplicate_key="log_stats_export:synthetic",
        )
        token = SimpleNamespace()
        coordinator._records["analysis"] = TaskRecord(
            "analysis", analysis, 1, token, None, None
        )
        coordinator._validate_start(export)
        with self.assertRaisesRegex(TaskRejectedError, "duplicate"):
            coordinator._validate_start(analysis)
        download_summary = TaskSpec(
            task_type="download_summary",
            display_name="synthetic summary",
            resource_keys=frozenset({"task_logs", "result_logs"}),
        )
        with self.assertRaisesRegex(TaskRejectedError, "task_logs"):
            coordinator._validate_start(download_summary)

        coordinator._records.clear()
        coordinator._records["export"] = TaskRecord(
            "export", export, 1, token, None, None
        )
        with self.assertRaisesRegex(TaskRejectedError, "duplicate"):
            coordinator._validate_start(export)
        coordinator._closing = True
        with self.assertRaisesRegex(TaskRejectedError, "closing"):
            coordinator._validate_start(analysis)

    def test_history_availability_has_only_fixed_missing_and_partial_reasons(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                snapshot = SimpleNamespace(
                    native_log_segments=(
                        DashboardNativeLogSegment(root / "missing.log", None, None),
                    )
                )
                window._dashboard_snapshot = snapshot
                window._update_log_stats_history_availability()
                self.assertFalse(window.log_stats_history_button.isEnabled())
                self.assertIn("未记录日志区间", window.log_stats_history_reason.text())

                available = _segment(root / "available.log", "synthetic\n")
                snapshot.native_log_segments = (
                    DashboardNativeLogSegment(root / "missing.log", 0, 5),
                    DashboardNativeLogSegment(available.path, 0, available.length),
                )
                window._update_log_stats_history_availability()
                self.assertTrue(window.log_stats_history_button.isEnabled())
                self.assertIn("只分析可用片段", window.log_stats_history_reason.text())
            finally:
                self._dispose(window, home_patch)

    def test_history_analysis_is_manual_single_selection_and_never_auto_submitted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                first = _segment(root / "first.log", "Response Code: 200\n")
                second = _segment(root / "second.log", "Response Code: 403\n")
                window._dashboard_snapshot = SimpleNamespace(
                    task_log=root / "DownloadTask_history.log",
                    native_log_segments=(
                        DashboardNativeLogSegment(first.path, 0, first.length),
                        DashboardNativeLogSegment(second.path, 0, second.length),
                    ),
                )
                window.log_stats_auto_checkbox.setChecked(True)
                with patch.object(
                    window, "_submit_log_stats_analysis"
                ) as submit:
                    window._update_log_stats_history_availability()
                    submit.assert_not_called()
                    window._start_historical_log_stats()
                submit.assert_called_once()
                self.assertEqual(submit.call_args.args[0], "DownloadTask_history.log")
                self.assertFalse(submit.call_args.kwargs["automatic"])
                stats = submit.call_args.args[1](SimpleNamespace(
                    raise_if_cancelled=lambda: None
                ))
                self.assertEqual(stats.http.total, 2)
            finally:
                self._dispose(window, home_patch)

    def test_auto_analysis_is_queued_only_after_successful_summary_terminal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                segment = _segment(root / "native.log", "synthetic\n")
                run = SimpleNamespace(
                    task_log=root / "DownloadTask_current.log",
                    running=False,
                    completion_marker=None,
                )
                binding = DownloadSummaryTaskBinding(
                    run=run,
                    assessment=SimpleNamespace(),
                    deduplicate_key="download_summary:synthetic",
                    generation=1,
                )
                window._download_summary_binding = binding
                window._background_generations[binding.deduplicate_key] = 1
                summary = _summary((segment,))
                with patch("douk_manager.gui.format_summary_for_ui", return_value=()):
                    window.log_stats_auto_checkbox.setChecked(False)
                    window._settle_download_summary(
                        binding, TaskState.SUCCEEDED, summary
                    )
                self.assertIsNone(window._pending_auto_log_stats_run)

                second = DownloadSummaryTaskBinding(
                    run=run,
                    assessment=SimpleNamespace(),
                    deduplicate_key="download_summary:synthetic-2",
                    generation=2,
                )
                window._download_summary_binding = second
                window._background_generations[second.deduplicate_key] = 2
                window.log_stats_auto_checkbox.setChecked(True)
                with patch("douk_manager.gui.format_summary_for_ui", return_value=()):
                    window._settle_download_summary(second, TaskState.SUCCEEDED, summary)
                self.assertIs(window._pending_auto_log_stats_run, run)
                self.assertTrue(second.terminal_consumed)
            finally:
                self._dispose(window, home_patch)

    def test_auto_analysis_submits_once_after_summary_removal_but_not_during_closing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                segment = _segment(root / "native.log", "synthetic\n")
                run = SimpleNamespace(
                    task_log=root / "DownloadTask_current.log",
                    running=False,
                    completion_marker=None,
                )
                summary = _summary((segment,))
                binding = DownloadSummaryTaskBinding(
                    run=run,
                    assessment=SimpleNamespace(),
                    deduplicate_key="download_summary:auto",
                    generation=1,
                    business_finalized=True,
                )
                window._download_summary_binding = binding
                window._background_generations[binding.deduplicate_key] = 1
                window.log_stats_auto_checkbox.setChecked(True)
                with patch("douk_manager.gui.format_summary_for_ui", return_value=()):
                    window._settle_download_summary(
                        binding, TaskState.SUCCEEDED, summary
                    )
                with patch.object(window, "_start_current_log_stats_for_run") as start:
                    window._remove_download_summary(binding)
                start.assert_called_once_with(run)

                closing = DownloadSummaryTaskBinding(
                    run=run,
                    assessment=SimpleNamespace(),
                    deduplicate_key="download_summary:closing",
                    generation=2,
                    business_finalized=True,
                )
                window._download_summary_binding = closing
                window._background_generations[closing.deduplicate_key] = 2
                window._pending_auto_log_stats_run = run
                window.controller.startup_state = StartupState.CLOSING
                with patch.object(window, "_start_current_log_stats_for_run") as start:
                    window._remove_download_summary(closing)
                start.assert_not_called()
            finally:
                self._dispose(window, home_patch)

    def test_auto_analysis_is_admitted_before_summary_continues(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                run = SimpleNamespace(
                    task_log=root / "DownloadTask_current.log",
                    running=False,
                    completion_marker=None,
                )
                binding = DownloadSummaryTaskBinding(
                    run=run,
                    assessment=SimpleNamespace(),
                    deduplicate_key="download_summary:ordered-auto",
                    generation=1,
                    outcome=TaskState.SUCCEEDED,
                    terminal_consumed=True,
                    continuation_ready=True,
                )
                window._download_summary_binding = binding
                window._background_generations[binding.deduplicate_key] = 1
                window._pending_auto_log_stats_run = run
                events = []
                with patch.object(
                    window,
                    "_start_current_log_stats_for_run",
                    side_effect=lambda _run: events.append("auto"),
                ), patch.object(
                    window,
                    "_finish_run_after_summary",
                    side_effect=lambda _run, _assessment: events.append("continue"),
                ):
                    window._remove_download_summary(binding)
                self.assertEqual(events, ["auto", "continue"])
            finally:
                self._dispose(window, home_patch)

    def test_queue_start_waits_for_active_auto_analysis_and_resumes_on_removal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            try:
                window._log_stats_task_id = "synthetic-analysis"
                window.queue_pending = [Path("synthetic-next.json")]
                with patch.object(window, "_run") as run:
                    window._start_next_queue_item()
                run.assert_not_called()
                self.assertTrue(window._queue_start_waiting_for_log_stats)

                with patch.object(MainWindow, "_start_next_queue_item") as start_next:
                    window._log_stats_analysis_removed()
                start_next.assert_called_once_with(window)
            finally:
                window._log_stats_task_id = None
                self._dispose(window, home_patch)

    def test_automatic_failure_cancel_and_completion_stay_inline_without_export(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                diagnostics = window.controller.paths.logs / "Diagnostics"
                with patch("douk_manager.gui.QMessageBox") as message_box:
                    window._log_stats_failed("synthetic failure", automatic=True)
                    self.assertIn("自动分析失败", window.log_stats_output.toPlainText())
                    window._log_stats_cancelled(automatic=True)
                    self.assertIn("自动分析已取消", window.log_stats_output.toPlainText())
                    message_box.assert_not_called()
                window._apply_log_stats_result(analyse_segments(()), "synthetic")
                self.assertFalse(diagnostics.exists())
            finally:
                self._dispose(window, home_patch)

    def test_background_failure_messages_never_reflect_paths_or_log_content(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            try:
                unsafe = r"Z:\FAKE-PATH\secret.log sessionid=FAKE-SESSION-DO-NOT-USE"
                payload = TaskFailure("OSError", unsafe, unsafe)
                window._log_stats_failed(payload, automatic=True)
                self.assertNotIn(unsafe, window.log_stats_output.toPlainText())
                with patch.object(window.controller.logger, "error") as logged:
                    window._log_stats_export_failed(payload)
                self.assertNotIn(unsafe, window.log_stats_self_check.text())
                logged.assert_called_once()
                self.assertNotIn(unsafe, str(logged.call_args))
            finally:
                self._dispose(window, home_patch)

    def test_duplicate_analysis_request_is_silently_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                existing = SimpleNamespace(
                    generation_key="log_stats_analyse:synthetic"
                )
                window._background_bindings["existing"] = existing
                with patch.object(window, "_submit_background") as submit:
                    window._submit_log_stats_analysis(
                        "synthetic",
                        lambda _context: analyse_segments(()),
                        automatic=True,
                        refresh_targets=("run_result",),
                    )
                submit.assert_not_called()
                self.assertNotIn("未启动", window.log_stats_output.toPlainText())
            finally:
                window._background_bindings.clear()
                self._dispose(window, home_patch)

    def test_rendered_result_marks_unreliable_reason_and_fixed_abnormal_title(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window, home_patch = self._window(Path(directory))
            try:
                reason = "本次原生日志缺少运行锚点。"
                window._apply_log_stats_result(
                    analyse_segments(()), "synthetic", located_reason=reason
                )
                text = window.log_stats_output.toPlainText()
                self.assertIn(reason, text)
                self.assertIn("异常路径状态码（不在上面那张响应码表里）", text)
                self.assertNotIn("LEAK SELF-CHECK", text)
            finally:
                self._dispose(window, home_patch)

    def test_real_qthreads_for_analysis_and_export_are_removed_after_finished(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                segment = _segment(root / "native.log", "Response Code: 200\n")
                window._latest_log_stats_run = SimpleNamespace(
                    task_log=root / "DownloadTask_current.log",
                    running=False,
                    _summary_result=_summary((segment,)),
                )
                window._start_current_log_stats()
                deadline = time.monotonic() + 5
                while window.coordinator.has_active_tasks() and time.monotonic() < deadline:
                    self.app.processEvents()
                self.assertFalse(window.coordinator.has_active_tasks())
                self.assertIsNotNone(window._log_stats_result)

                window._start_log_stats_export()
                deadline = time.monotonic() + 5
                while window.coordinator.has_active_tasks() and time.monotonic() < deadline:
                    self.app.processEvents()
                while window.findChildren(QThread) and time.monotonic() < deadline:
                    QCoreApplication.sendPostedEvents(
                        None, QEvent.Type.DeferredDelete
                    )
                    self.app.processEvents()
                self.assertFalse(window.coordinator.has_active_tasks())
                self.assertEqual(window.findChildren(QThread), [])
                self.assertIn("RESULT: CLEAN", window.log_stats_self_check.text())
                export_text = window.log_stats_export_path.text()
                self.assertIn("已导出中文版：", export_text)
                self.assertIn("已导出英文版：", export_text)
                diagnostics = window.controller.paths.logs / "Diagnostics"
                self.assertEqual(len(tuple(diagnostics.glob("*.txt"))), 2)
            finally:
                self._dispose(window, home_patch)

    def test_closing_rejects_log_analysis_before_submission(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            window, home_patch = self._window(root)
            try:
                window.controller.startup_state = StartupState.CLOSING
                with patch.object(window, "_submit_background") as submit:
                    window._start_current_log_stats()
                submit.assert_not_called()

                before = window.log_stats_output.toPlainText()
                window._apply_log_stats_result(analyse_segments(()), "late")
                self.assertEqual(window.log_stats_output.toPlainText(), before)
            finally:
                self._dispose(window, home_patch)


if __name__ == "__main__":
    unittest.main()
