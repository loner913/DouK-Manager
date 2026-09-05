import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from douk_manager.core.download_summary import (
    AccountStatus,
    LocatedNativeLogs,
    NativeLogSegment,
    PlannedAccount,
    parse_download_summary,
)


class DownloadSummaryParserTests(unittest.TestCase):
    def _parse(
        self,
        lines: list[str],
        planned: tuple[PlannedAccount, ...],
        *,
        exit_code: int | None = 0,
        reliable: bool = True,
    ):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "native.log"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
        located = LocatedNativeLogs(
            (NativeLogSegment(path, 0, path.stat().st_size),),
            "synthetic",
            reliable,
            "synthetic location ambiguity" if not reliable else "",
        )
        return parse_download_summary(planned, located, exit_code)

    @staticmethod
    def _planned(*a_numbers: int) -> tuple[PlannedAccount, ...]:
        return tuple(
            PlannedAccount(task_index, a_number, f"A{a_number}example")
            for task_index, a_number in enumerate(a_numbers, start=1)
        )

    def test_downloaded_skipped_zero_private_and_recovered_anomaly_are_classified(
        self,
    ) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A10example",
                "[WARNING]: 正在进行第 1 次重试",
                "筛选处理后作品数量: 1",
                "下载视频作品 1 个",
                "跳过视频作品 0 个",
                "下载图集作品 0 个",
                "跳过图集作品 0 个",
                "下载实况作品 0 个",
                "跳过实况作品 0 个",
                "开始处理第 2 个账号",
                "标识：A20example",
                "筛选处理后作品数量: 3",
                "下载视频作品 0 个",
                "跳过视频作品 1 个",
                "下载图集作品 0 个",
                "跳过图集作品 1 个",
                "下载实况作品 0 个",
                "跳过实况作品 1 个",
                "开始处理第 3 个账号",
                "标识：A30example",
                "筛选处理后作品数量: 0",
                "开始处理第 4 个账号",
                "标识：A40example",
                "该账号为私密账号",
            ],
            self._planned(10, 20, 30, 40),
        )

        self.assertEqual(
            [outcome.status for outcome in summary.started_outcomes],
            [
                AccountStatus.DOWNLOADED,
                AccountStatus.ALL_SKIPPED,
                AccountStatus.NO_ELIGIBLE_WORKS,
                AccountStatus.PRIVATE,
            ],
        )
        self.assertEqual(summary.primary_status_counts[AccountStatus.DOWNLOADED], 1)
        self.assertEqual(summary.primary_status_counts[AccountStatus.ALL_SKIPPED], 1)
        self.assertEqual(
            summary.primary_status_counts[AccountStatus.NO_ELIGIBLE_WORKS], 1
        )
        self.assertEqual(summary.primary_status_counts[AccountStatus.PRIVATE], 1)
        self.assertEqual(sum(summary.primary_status_counts.values()), 4)
        self.assertEqual(summary.completed_with_anomaly, (10,))
        self.assertTrue(summary.complete)
        self.assertTrue(summary.reliable)

    def test_unrecovered_error_overrides_zero_filtered_result(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A7example",
                "[ERROR]: 视频下载地址解析失败",
                "筛选处理后作品数量: 0",
            ],
            self._planned(7),
        )

        self.assertEqual(summary.started_outcomes[0].status, AccountStatus.ERROR)
        self.assertEqual(
            summary.primary_status_counts[AccountStatus.NO_ELIGIBLE_WORKS], 0
        )

    def test_error_account_without_logged_mark_is_not_trusted(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "[ERROR]: 视频下载地址解析失败",
                "筛选处理后作品数量: 0",
            ],
            self._planned(7),
        )

        self.assertEqual(summary.started_outcomes, ())
        self.assertFalse(summary.reliable)
        self.assertTrue(any("缺少" in reason for reason in summary.reasons))

    def test_recovered_error_is_anomaly_overlay(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A7example",
                "[ERROR]: 视频下载地址解析失败",
                "筛选处理后作品数量: 1",
                "下载视频作品 1 个",
                "跳过视频作品 0 个",
                "下载图集作品 0 个",
                "跳过图集作品 0 个",
                "下载实况作品 0 个",
                "跳过实况作品 0 个",
            ],
            self._planned(7),
        )

        self.assertEqual(summary.started_outcomes[0].status, AccountStatus.DOWNLOADED)
        self.assertEqual(summary.completed_with_anomaly, (7,))

    def test_error_after_complete_statistics_is_unrecovered(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A7example",
                "筛选处理后作品数量: 1",
                "下载视频作品 1 个",
                "跳过视频作品 0 个",
                "下载图集作品 0 个",
                "跳过图集作品 0 个",
                "下载实况作品 0 个",
                "跳过实况作品 0 个",
                "[ERROR]: 后续处理失败",
            ],
            self._planned(7),
        )

        self.assertEqual(summary.started_outcomes[0].status, AccountStatus.ERROR)

    def test_nonzero_exit_is_incomplete_but_location_can_remain_reliable(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A7example",
                "筛选处理后作品数量: 0",
            ],
            self._planned(7),
            exit_code=2,
        )

        self.assertFalse(summary.complete)
        self.assertTrue(summary.reliable)

    def test_started_but_unclosed_account_is_interrupted(self) -> None:
        summary = self._parse(
            ["开始处理第 1 个账号", "标识：A8example"],
            self._planned(8),
        )

        self.assertEqual(summary.started_outcomes[0].status, AccountStatus.INTERRUPTED)
        self.assertFalse(summary.complete)

    def test_interrupted_account_without_logged_mark_is_not_trusted(self) -> None:
        summary = self._parse(["开始处理第 1 个账号"], self._planned(8))

        self.assertEqual(summary.started_outcomes, ())
        self.assertFalse(summary.reliable)
        self.assertTrue(any("缺少" in reason for reason in summary.reasons))

    def test_nonzero_exit_trailing_unmarked_account_uses_frozen_mapping(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A51example",
                "筛选处理后作品数量: 0",
                "开始处理第 2 个账号",
            ],
            self._planned(51, 52, 53),
            exit_code=0xC000013A,
        )

        self.assertTrue(summary.reliable)
        self.assertFalse(summary.complete)
        self.assertEqual(summary.started_count, 2)
        self.assertEqual(
            summary.started_outcomes[1].status, AccountStatus.INTERRUPTED
        )
        self.assertEqual(summary.started_outcomes[1].a_number, 52)
        self.assertEqual(summary.not_started, (53,))

    def test_nonzero_exit_does_not_trust_earlier_unmarked_account(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "开始处理第 2 个账号",
                "标识：A52example",
                "筛选处理后作品数量: 0",
            ],
            self._planned(51, 52),
            exit_code=0xC000013A,
        )

        self.assertFalse(summary.reliable)
        self.assertTrue(any("缺少" in reason for reason in summary.reasons))

    def test_nonzero_exit_never_accepts_a_conflicting_trailing_mark(self) -> None:
        summary = self._parse(
            ["开始处理第 1 个账号", "标识：A99wrong"],
            self._planned(51),
            exit_code=0xC000013A,
        )

        self.assertFalse(summary.reliable)
        self.assertTrue(any("不一致" in reason for reason in summary.reasons))

    def test_read_failure_finalizes_current_as_interrupted_without_trailing_guess(
        self,
    ) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "native.log"
        path.touch()
        located = LocatedNativeLogs(
            (NativeLogSegment(path, 0, 100),), "synthetic-failure", True
        )

        def failing_lines(_segments):
            yield "开始处理第 1 个账号"
            yield "标识：A10example"
            raise OSError("sensitive payload must not escape")

        with patch(
            "douk_manager.core.download_summary._iter_located_lines",
            side_effect=failing_lines,
        ):
            summary = parse_download_summary(self._planned(10, 20, 30), located, 1)

        self.assertEqual(summary.started_outcomes[0].status, AccountStatus.INTERRUPTED)
        self.assertEqual(summary.not_started, ())
        self.assertFalse(summary.reliable)
        self.assertFalse(summary.complete)
        self.assertTrue(any("OSError" in reason for reason in summary.reasons))
        self.assertFalse(
            any("sensitive payload" in reason for reason in summary.reasons)
        )

    def test_read_failure_preserves_prior_completed_and_current_partial_evidence(
        self,
    ) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "native.log"
        path.touch()
        located = LocatedNativeLogs(
            (NativeLogSegment(path, 0, 100),), "synthetic-failure", True
        )

        def failing_lines(_segments):
            yield "开始处理第 1 个账号"
            yield "标识：A10example"
            yield "筛选处理后作品数量: 0"
            yield "开始处理第 2 个账号"
            yield "标识：A20example"
            raise OSError("redacted")

        with patch(
            "douk_manager.core.download_summary._iter_located_lines",
            side_effect=failing_lines,
        ):
            summary = parse_download_summary(self._planned(10, 20, 30), located, 1)

        self.assertEqual(
            [outcome.status for outcome in summary.started_outcomes],
            [AccountStatus.NO_ELIGIBLE_WORKS, AccountStatus.INTERRUPTED],
        )
        self.assertEqual(summary.not_started, ())
        self.assertFalse(summary.reliable)

    def test_disabled_accounts_never_appear_in_not_started(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A2example",
                "筛选处理后作品数量: 0",
            ],
            self._planned(2, 4),
        )

        self.assertEqual(summary.not_started, (4,))
        all_result_numbers = {
            *(outcome.a_number for outcome in summary.started_outcomes),
            *summary.pre_start_errors,
            *summary.not_started,
        }
        self.assertEqual(all_result_numbers, {2, 4})

    def test_duplicate_or_out_of_range_sequence_is_unreliable(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "筛选处理后作品数量: 0",
                "开始处理第 1 个账号",
                "筛选处理后作品数量: 0",
                "开始处理第 3 个账号",
            ],
            self._planned(1, 2),
        )

        self.assertFalse(summary.reliable)
        self.assertTrue(any("重复" in reason for reason in summary.reasons))
        self.assertTrue(any("越界" in reason for reason in summary.reasons))

    def test_unreliable_location_does_not_invent_not_started_accounts(self) -> None:
        summary = self._parse([], self._planned(1, 2), reliable=False)

        self.assertFalse(summary.reliable)
        self.assertEqual(summary.not_started, ())

    def test_pre_start_url_failure_is_neither_started_nor_not_started(self) -> None:
        summary = self._parse(
            [
                "配置文件 accounts_urls 参数的 url https://redacted.invalid "
                "提取 sec_user_id 失败，错误配置：{'mark': 'A50example'}",
                "开始处理第 2 个账号",
                "标识：A53example",
                "筛选处理后作品数量: 0",
            ],
            self._planned(50, 53, 80),
        )

        self.assertEqual(summary.pre_start_errors, (50,))
        self.assertEqual(
            [(item.task_index, item.a_number) for item in summary.started_outcomes],
            [(2, 53)],
        )
        self.assertEqual(summary.not_started, (80,))
        self.assertTrue(summary.reliable)

    def test_unidentified_missing_interior_index_is_unreliable(self) -> None:
        summary = self._parse(
            [
                "开始处理第 2 个账号",
                "标识：A53example",
                "筛选处理后作品数量: 0",
            ],
            self._planned(50, 53, 80),
        )

        self.assertFalse(summary.reliable)
        self.assertTrue(any("内部缺失" in reason for reason in summary.reasons))
        self.assertNotIn(50, summary.not_started)
        self.assertEqual(summary.not_started, (80,))

    def test_identity_failed_observed_index_keeps_prior_gap_uncertain(self) -> None:
        summary = self._parse(
            [
                "开始处理第 2 个账号",
                "标识：A99wrong",
                "筛选处理后作品数量: 0",
            ],
            self._planned(10, 20, 30),
        )

        self.assertEqual(summary.started_outcomes, ())
        self.assertNotIn(10, summary.not_started)
        self.assertEqual(summary.not_started, (30,))
        self.assertFalse(summary.reliable)
        self.assertTrue(any("内部缺失" in reason for reason in summary.reasons))

    def test_pre_start_failure_uses_exact_frozen_numeric_leading_mark(self) -> None:
        summary = self._parse(
            [
                "配置文件 accounts_urls 参数的 url https://redacted.invalid "
                "提取 sec_user_id 失败，错误配置：{'mark': 'A1164example'}",
                "开始处理第 2 个账号",
                "标识：A200example",
                "筛选处理后作品数量: 0",
            ],
            (
                PlannedAccount(1, 116, "A1164example"),
                PlannedAccount(2, 200, "A200example"),
            ),
        )

        self.assertEqual(summary.pre_start_errors, (116,))
        self.assertTrue(summary.reliable)

    def test_pre_start_error_line_does_not_taint_the_previous_account(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A10example",
                "筛选处理后作品数量: 0",
                "[ERROR]: 配置文件 accounts_urls 参数的 url redacted "
                "提取 sec_user_id 失败，错误配置：{'mark': 'A20example'}",
                "开始处理第 3 个账号",
                "标识：A30example",
                "筛选处理后作品数量: 0",
            ],
            self._planned(10, 20, 30),
        )

        self.assertEqual(summary.started_outcomes[0].status, AccountStatus.NO_ELIGIBLE_WORKS)
        self.assertFalse(summary.started_outcomes[0].completed_with_anomaly)
        self.assertEqual(summary.pre_start_errors, (20,))

    def test_same_account_pre_start_failure_and_start_is_unreliable(self) -> None:
        summary = self._parse(
            [
                "配置文件 accounts_urls 参数的 url redacted "
                "提取 sec_user_id 失败，错误配置：{'mark': 'A10example'}",
                "开始处理第 1 个账号",
                "标识：A10example",
                "筛选处理后作品数量: 0",
            ],
            self._planned(10),
        )

        self.assertFalse(summary.reliable)
        self.assertTrue(any("同时" in reason for reason in summary.reasons))

    def test_normal_completed_account_without_logged_mark_is_not_trusted(self) -> None:
        summary = self._parse(
            ["开始处理第 1 个账号", "筛选处理后作品数量: 0"],
            self._planned(10),
        )

        self.assertEqual(summary.started_outcomes, ())
        self.assertFalse(summary.reliable)
        self.assertTrue(any("缺少" in reason for reason in summary.reasons))

    def test_mismatched_logged_mark_does_not_emit_a_trusted_outcome(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A99wrong",
                "筛选处理后作品数量: 0",
            ],
            self._planned(10),
        )

        self.assertEqual(summary.started_outcomes, ())
        self.assertEqual(summary.numbers_for(AccountStatus.NO_ELIGIBLE_WORKS), ())
        self.assertFalse(summary.reliable)
        self.assertTrue(any("不一致" in reason for reason in summary.reasons))

    def test_private_account_without_logged_mark_uses_frozen_mapping(self) -> None:
        summary = self._parse(
            ["开始处理第 1 个账号", "该账号为私密账号"],
            self._planned(10),
        )

        self.assertEqual(summary.started_outcomes[0].status, AccountStatus.PRIVATE)
        self.assertEqual(summary.started_outcomes[0].a_number, 10)
        self.assertTrue(summary.reliable)

    def test_segment_offset_discards_only_a_partial_first_line(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "native.log"
        prefix = "旧内容开始处理第 1 个账号"
        current = (
            "仍是旧内容\n开始处理第 1 个账号\n标识：A9example\n"
            "筛选处理后作品数量: 0\n"
        )
        path.write_bytes((prefix + current).encode("utf-8"))
        offset = len(prefix.encode("utf-8"))

        summary = parse_download_summary(
            self._planned(9),
            LocatedNativeLogs(
                (NativeLogSegment(path, offset, path.stat().st_size - offset),),
                "synthetic-offset",
                True,
            ),
            0,
        )

        self.assertEqual(len(summary.started_outcomes), 1)
        self.assertEqual(
            summary.started_outcomes[0].status, AccountStatus.NO_ELIGIBLE_WORKS
        )

    def test_offset_after_utf8_bom_preserves_the_appended_first_line(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "native.log"
        appended = (
            "开始处理第 1 个账号\n标识：A9example\n筛选处理后作品数量: 0\n"
        ).encode("utf-8")
        path.write_bytes(b"\xef\xbb\xbf" + appended)

        summary = parse_download_summary(
            self._planned(9),
            LocatedNativeLogs(
                (NativeLogSegment(path, 3, len(appended)),), "bom-offset", True
            ),
            0,
        )

        self.assertEqual(summary.started_count, 1)
        self.assertEqual(
            summary.started_outcomes[0].status, AccountStatus.NO_ELIGIBLE_WORKS
        )

    def test_offset_three_after_non_bom_newline_preserves_first_new_line(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "native.log"
        appended = (
            "开始处理第 1 个账号\n标识：A9example\n筛选处理后作品数量: 0\n"
        ).encode("utf-8")
        path.write_bytes(b"ab\n" + appended)

        summary = parse_download_summary(
            self._planned(9),
            LocatedNativeLogs(
                (NativeLogSegment(path, 3, len(appended)),), "non-bom-offset", True
            ),
            0,
        )

        self.assertEqual(summary.started_count, 1)
        self.assertEqual(
            summary.started_outcomes[0].status, AccountStatus.NO_ELIGIBLE_WORKS
        )
        self.assertTrue(summary.reliable)

    def test_offset_inside_utf8_character_discards_stale_bytes_before_decoding(
        self,
    ) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "native.log"
        stale = "旧的中文残行\n".encode("utf-8")
        current = (
            "开始处理第 1 个账号\n标识：A9example\n筛选处理后作品数量: 0\n"
        ).encode("utf-8")
        path.write_bytes(stale + current)
        offset = len("旧".encode("utf-8")) + 1

        summary = parse_download_summary(
            self._planned(9),
            LocatedNativeLogs(
                (
                    NativeLogSegment(
                        path, offset, path.stat().st_size - offset
                    ),
                ),
                "mid-character-offset",
                True,
            ),
            0,
        )

        self.assertEqual(summary.started_count, 1)
        self.assertEqual(
            summary.started_outcomes[0].status, AccountStatus.NO_ELIGIBLE_WORKS
        )
        self.assertTrue(summary.reliable)

    def test_invalid_utf8_replacement_marks_partial_result_unreliable(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "native.log"
        path.write_bytes(
            "开始处理第 1 个账号\n标识：A9example\n".encode("utf-8")
            + b"\xff\n"
        )

        summary = parse_download_summary(
            self._planned(9),
            LocatedNativeLogs(
                (NativeLogSegment(path, 0, path.stat().st_size),), "bad-utf8", True
            ),
            1,
        )

        self.assertEqual(summary.started_outcomes[0].status, AccountStatus.INTERRUPTED)
        self.assertFalse(summary.reliable)
        self.assertTrue(any("编码" in reason for reason in summary.reasons))

    def test_eof_before_declared_segment_length_marks_result_unreliable(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "native.log"
        path.write_text(
            "开始处理第 1 个账号\n标识：A9example\n筛选处理后作品数量: 0\n",
            encoding="utf-8",
        )

        summary = parse_download_summary(
            self._planned(9),
            LocatedNativeLogs(
                (NativeLogSegment(path, 0, path.stat().st_size + 10),), "short", True
            ),
            0,
        )

        self.assertFalse(summary.reliable)
        self.assertTrue(any("提前结束" in reason for reason in summary.reasons))

    def test_newline_free_oversized_line_is_rejected_without_payload_reason(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "native.log"
        path.write_bytes(b"x" * (2 * 1024 * 1024))

        summary = parse_download_summary(
            self._planned(9),
            LocatedNativeLogs(
                (NativeLogSegment(path, 0, path.stat().st_size),), "oversized", True
            ),
            1,
        )

        self.assertFalse(summary.reliable)
        self.assertEqual(summary.not_started, ())
        self.assertTrue(any("单行" in reason for reason in summary.reasons))
        self.assertFalse(any("xxxxx" in reason for reason in summary.reasons))

    def test_newline_terminated_oversized_line_is_rejected_before_yield(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "native.log"
        path.write_bytes(b"x" * (1024 * 1024 + 1) + b"\n")

        summary = parse_download_summary(
            self._planned(9),
            LocatedNativeLogs(
                (NativeLogSegment(path, 0, path.stat().st_size),), "oversized", True
            ),
            1,
        )

        self.assertFalse(summary.reliable)
        self.assertTrue(any("单行" in reason for reason in summary.reasons))

    def test_frozen_a_prefix_disambiguates_a_numeric_leading_mark(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A1164example",
                "筛选处理后作品数量: 0",
            ],
            (PlannedAccount(1, 116, "A1164example"),),
        )

        self.assertTrue(summary.reliable)

    def test_logged_mark_that_does_not_match_frozen_mapping_is_unreliable(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A117example",
                "筛选处理后作品数量: 0",
            ],
            self._planned(116),
        )

        self.assertFalse(summary.reliable)
        self.assertTrue(any("不一致" in reason for reason in summary.reasons))

    def test_engine_cleaned_marks_keep_the_frozen_account_identity(self) -> None:
        cases = (
            ("A1164example.", "A1164example"),
            ("A1164example..", "A1164example"),
            ("A1164example...", "A1164example"),
            ("A1164alpha  beta", "A1164alpha beta"),
            ("A1164alpha\t\u00a0beta", "A1164alpha beta"),
            ("A1164C\ufe0fexample", "A1164Cexample"),
            ("A116\ufe0e4example", "A1164example"),
            ("A1164C\ufe0f  example..", "A1164C example"),
        )
        for raw_mark, logged_mark in cases:
            with self.subTest(raw_mark=raw_mark):
                planned = (PlannedAccount(1, 116, raw_mark),)
                summary = self._parse(
                    [
                        "开始处理第 1 个账号",
                        f"标识：{logged_mark}",
                        "筛选处理后作品数量: 0",
                    ],
                    planned,
                )
                self.assertTrue(summary.reliable)
                self.assertTrue(summary.complete)
                self.assertEqual(summary.started_outcomes[0].a_number, 116)
                self.assertEqual(planned[0].mark, raw_mark)

    def test_mark_alias_never_normalizes_unexpected_logged_text(self) -> None:
        cases = (
            ("A10example", "A10example."),
            ("A10example..", "A10example..."),
            ("A10alpha beta", "A10alpha  beta"),
            ("A10example.", "A10Example"),
            ("A10a.b.", "A10ab"),
            ("A10example.", "A11example"),
        )
        for raw_mark, logged_mark in cases:
            with self.subTest(raw_mark=raw_mark, logged_mark=logged_mark):
                summary = self._parse(
                    [
                        "开始处理第 1 个账号",
                        f"标识：{logged_mark}",
                        "筛选处理后作品数量: 0",
                    ],
                    (PlannedAccount(1, 10, raw_mark),),
                )
                self.assertFalse(summary.reliable)
                self.assertEqual(summary.started_outcomes, ())

    def test_colliding_cleaned_marks_are_not_trusted(self) -> None:
        # Numeric-leading mark bodies can make different A positions share text.
        planned = (
            PlannedAccount(1, 1, "A116example."),
            PlannedAccount(2, 11, "A116example.."),
        )
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A116example",
                "筛选处理后作品数量: 0",
                "开始处理第 2 个账号",
                "标识：A116example",
                "筛选处理后作品数量: 0",
            ],
            planned,
        )
        self.assertFalse(summary.reliable)
        self.assertEqual(summary.started_outcomes, ())

    def test_alias_collision_with_another_raw_mark_is_not_trusted(self) -> None:
        planned = (
            PlannedAccount(1, 1, "A116example."),
            PlannedAccount(2, 11, "A116example"),
        )
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A116example",
                "筛选处理后作品数量: 0",
                "开始处理第 2 个账号",
                "标识：A116example",
                "筛选处理后作品数量: 0",
            ],
            planned,
        )
        self.assertFalse(summary.reliable)
        self.assertEqual(summary.started_count, 1)
        self.assertEqual(summary.started_outcomes[0].a_number, 11)

    def test_prefix_like_logged_mark_never_matches_frozen_mark(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A10other",
                "筛选处理后作品数量: 0",
            ],
            (PlannedAccount(1, 1, "A1"),),
        )

        self.assertFalse(summary.reliable)
        self.assertEqual(summary.started_outcomes, ())

    def test_conflicting_or_contradictory_final_totals_are_unreliable(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A1example",
                "筛选处理后作品数量: 1",
                "下载视频作品 2 个",
                "跳过视频作品 0 个",
                "下载图集作品 0 个",
                "跳过图集作品 0 个",
                "下载实况作品 0 个",
                "跳过实况作品 0 个",
            ],
            self._planned(1),
        )

        self.assertFalse(summary.reliable)
        self.assertFalse(summary.complete)

    def test_zero_filtered_count_with_nonzero_partial_total_is_unreliable(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A1example",
                "筛选处理后作品数量: 0",
                "下载视频作品 1 个",
            ],
            self._planned(1),
        )

        self.assertFalse(summary.reliable)
        self.assertEqual(summary.started_outcomes, ())

    def test_error_discards_prior_conflicting_statistics_before_recovery(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A1example",
                "筛选处理后作品数量: 1",
                "下载视频作品 2 个",
                "[ERROR]: 本轮统计作废并重试",
                "筛选处理后作品数量: 1",
                "下载视频作品 1 个",
                "跳过视频作品 0 个",
                "下载图集作品 0 个",
                "跳过图集作品 0 个",
                "下载实况作品 0 个",
                "跳过实况作品 0 个",
            ],
            self._planned(1),
        )

        self.assertTrue(summary.reliable)
        self.assertEqual(summary.started_outcomes[0].status, AccountStatus.DOWNLOADED)
        self.assertTrue(summary.started_outcomes[0].completed_with_anomaly)

    def test_pre_start_and_started_events_share_strict_order(self) -> None:
        summary = self._parse(
            [
                "配置文件 accounts_urls 参数的 url redacted 提取 sec_user_id 失败，错误配置：{'mark': 'A20example'}",
                "开始处理第 1 个账号",
                "标识：A10example",
                "筛选处理后作品数量: 0",
            ],
            self._planned(10, 20),
        )

        self.assertFalse(summary.reliable)
        self.assertEqual(summary.not_started, ())

    def test_unterminated_final_record_is_not_accepted(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "native.log"
        path.write_text("开始处理第 1 个账号\n标识：A1example", encoding="utf-8")
        summary = parse_download_summary(
            self._planned(1),
            LocatedNativeLogs((NativeLogSegment(path, 0, path.stat().st_size),), "synthetic", True),
            0,
        )

        self.assertFalse(summary.reliable)
        self.assertFalse(summary.complete)

    def test_repeated_cached_stat_line_does_not_recover_latest_error(self) -> None:
        summary = self._parse(
            [
                "开始处理第 1 个账号",
                "标识：A7example",
                "筛选处理后作品数量: 1",
                "下载视频作品 1 个",
                "跳过视频作品 0 个",
                "下载图集作品 0 个",
                "跳过图集作品 0 个",
                "下载实况作品 0 个",
                "跳过实况作品 0 个",
                "[ERROR]: 后续处理失败",
                "跳过实况作品 0 个",
            ],
            self._planned(7),
        )

        self.assertEqual(summary.started_outcomes[0].status, AccountStatus.ERROR)

    def test_out_of_order_unique_task_indices_are_unreliable(self) -> None:
        summary = self._parse(
            [
                "开始处理第 2 个账号",
                "标识：A20example",
                "筛选处理后作品数量: 0",
                "开始处理第 1 个账号",
                "标识：A10example",
                "筛选处理后作品数量: 0",
                "开始处理第 3 个账号",
                "标识：A30example",
                "筛选处理后作品数量: 0",
            ],
            self._planned(10, 20, 30),
        )

        self.assertFalse(summary.reliable)
        self.assertTrue(any("顺序" in reason for reason in summary.reasons))


if __name__ == "__main__":
    unittest.main()
