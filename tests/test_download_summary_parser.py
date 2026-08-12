import tempfile
import unittest
from pathlib import Path

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
            ["开始处理第 1 个账号"],
            self._planned(8),
        )

        self.assertEqual(summary.started_outcomes[0].status, AccountStatus.INTERRUPTED)
        self.assertFalse(summary.complete)

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


if __name__ == "__main__":
    unittest.main()
