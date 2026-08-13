from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from douk_manager.core.download_summary import (
    AccountOutcome,
    AccountStatus,
    DownloadSummary,
    LocatedNativeLogs,
    NativeLogSegment,
    format_summary_for_task_log,
    format_summary_for_ui,
)


ENDED_AT = datetime(2026, 8, 13, 11, 22, 33)
NATIVE_LOG = Path(r"F:\Downloader\Volume\Log\2026-08-13 10.00.00.log")


def make_summary(*, reliable: bool = True, reasons: tuple[str, ...] = ()) -> DownloadSummary:
    outcomes = (
        AccountOutcome(1, 50, AccountStatus.DOWNLOADED),
        AccountOutcome(2, 51, AccountStatus.ALL_SKIPPED),
        AccountOutcome(3, 52, AccountStatus.NO_ELIGIBLE_WORKS),
        AccountOutcome(4, 55, AccountStatus.PRIVATE),
        AccountOutcome(5, 61, AccountStatus.DOWNLOADED, True),
        AccountOutcome(6, 68, AccountStatus.ERROR),
        AccountOutcome(7, 74, AccountStatus.ALL_SKIPPED, True),
        AccountOutcome(8, 81, AccountStatus.INTERRUPTED),
    )
    counts = {status: 0 for status in AccountStatus}
    for outcome in outcomes:
        counts[outcome.status] += 1
    located = LocatedNativeLogs(
        (NativeLogSegment(NATIVE_LOG, 128, 4096),) if reliable else (),
        "size-delta",
        reliable,
        reasons[0] if reasons else "",
    )
    return DownloadSummary(
        planned_count=12,
        started_outcomes=outcomes,
        pre_start_errors=(70,),
        not_started=(82, 83, 84),
        primary_status_counts=counts,
        completed_with_anomaly=(61, 74),
        complete=False,
        reliable=reliable,
        reasons=reasons,
        located=located,
        exit_code=0,
    )


class DownloadSummaryOutputTests(unittest.TestCase):
    def test_ui_contains_counts_and_complete_actionable_account_lists(self) -> None:
        lines = format_summary_for_ui(make_summary())

        self.assertEqual(lines[0], "下载进程：正常退出（退出码 0）")
        self.assertIn("账号汇总：结果不完整", lines)
        self.assertIn("计划账号：12", lines)
        self.assertIn("实际开始：8", lines)
        self.assertIn("有新作品下载：2", lines)
        self.assertIn("作品均被引擎跳过：2", lines)
        self.assertIn("无符合条件作品：1", lines)
        self.assertIn("私密账号：1", lines)
        self.assertIn("处理异常，需核对：2", lines)
        self.assertIn("处理中断：1", lines)
        self.assertIn("未开始：3", lines)
        self.assertIn("完成但有异常记录：2", lines)
        self.assertIn("私密账号（1）：A55", lines)
        self.assertIn("处理异常（2）：A68、A70", lines)
        self.assertIn("处理中断（1）：A81", lines)
        self.assertIn("未开始（3）：A82-A84", lines)
        self.assertIn("异常后完成（2）：A61、A74", lines)
        self.assertIn(f"原生日志：{NATIVE_LOG.resolve()}", lines)
        joined = "\n".join(lines)
        self.assertNotIn("有新作品下载（", joined)
        self.assertNotIn("作品均被引擎跳过（", joined)
        self.assertNotIn("无符合条件作品（", joined)

    def test_task_log_contains_location_validations_lists_and_fixed_definitions(self) -> None:
        text = format_summary_for_task_log(make_summary(), ENDED_AT)

        self.assertIn("【下载账号汇总】", text)
        self.assertIn("进程结束时间：2026-08-13 11:22:33", text)
        self.assertIn("退出码：0", text)
        self.assertIn("日志定位方式：size-delta", text)
        self.assertIn(f"日志区间：{NATIVE_LOG.resolve()}；偏移=128；长度=4096", text)
        self.assertIn("一级核对（计划账号=实际开始+进入处理前异常+未开始）：通过", text)
        self.assertIn("二级核对（实际开始=各主状态之和）：通过", text)
        self.assertIn("处理异常（2）：A68、A70", text)
        self.assertIn("私密账号（1）：A55", text)
        self.assertIn("处理中断（1）：A81", text)
        self.assertIn("未开始（3）：A82-A84", text)
        self.assertIn("异常后完成（2）：A61、A74", text)
        self.assertIn("【状态说明】", text)
        expected_definitions = (
            "有新作品下载：日志最终统计的下载作品数大于 0。",
            "作品均被引擎跳过：筛选后有作品，但视频、图集和实况最终全部计入跳过。",
            "无符合条件作品：筛选处理后的作品数量为 0。",
            "私密账号：出现明确的私密账号提示。",
            "处理异常，需核对：账号块中出现无法确认已恢复的错误，或 URL/sec_user_id 解析失败而未进入账号处理。",
            "完成但有异常记录：出现网络中断或重试，但之后仍产生完整的作品统计；该项是附加备注，不重复计入主状态数量。",
            "处理中断：账号已经开始处理，但进程结束前未形成可确认的最终结果。",
            "未开始：仅指本次任务启动时 enable=true 且 URL 有效、但在进程结束前尚未轮到的账号；本次 enable=false 或 URL 为空的账号不参与统计。",
            "结果不完整：用户停止、异常退出、日志截断、计划账号未全部完成，或其他证据不足导致无法确认完整任务结果。",
        )
        for definition in expected_definitions:
            self.assertIn(definition, text)
        self.assertEqual(text.count("【状态说明】"), 1)

    def test_unreliable_summary_does_not_invent_counts_or_leak_evidence(self) -> None:
        evidence = (
            "https://secret.example/path Cookie=private Response Headers Authorization bearer",
        )
        summary = make_summary(reliable=False, reasons=evidence)

        ui_text = "\n".join(format_summary_for_ui(summary))
        task_text = format_summary_for_task_log(summary, ENDED_AT)

        self.assertIn("下载进程：正常退出（退出码 0）", ui_text)
        self.assertIn("账号结果：无法可靠汇总；原因：汇总证据不可靠。", ui_text)
        self.assertNotIn("计划账号：", ui_text)
        self.assertIn(
            "一级核对（计划账号=实际开始+进入处理前异常+未开始）：无法验证",
            task_text,
        )
        self.assertIn(
            "二级核对（实际开始=各主状态之和）：无法验证",
            task_text,
        )
        self.assertNotIn(
            "核对（计划账号=实际开始+进入处理前异常+未开始）：通过",
            task_text,
        )
        self.assertNotIn("核对（实际开始=各主状态之和）：通过", task_text)
        for forbidden in ("https://", "Cookie", "Response Headers", "Authorization"):
            self.assertNotIn(forbidden, ui_text)
            self.assertNotIn(forbidden, task_text)

    def test_task_log_formats_every_located_segment_path_and_offset(self) -> None:
        summary = make_summary()
        second_log = Path(r"F:\Downloader\Volume\Log\continued.log")
        summary = DownloadSummary(
            planned_count=summary.planned_count,
            started_outcomes=summary.started_outcomes,
            pre_start_errors=summary.pre_start_errors,
            not_started=summary.not_started,
            primary_status_counts=summary.primary_status_counts,
            completed_with_anomaly=summary.completed_with_anomaly,
            complete=summary.complete,
            reliable=summary.reliable,
            reasons=summary.reasons,
            located=LocatedNativeLogs(
                (
                    NativeLogSegment(NATIVE_LOG, 128, 4096),
                    NativeLogSegment(second_log, 32, 2048),
                ),
                "size-delta-anchor",
                True,
            ),
            exit_code=summary.exit_code,
        )

        text = format_summary_for_task_log(summary, ENDED_AT)

        self.assertIn(
            f"日志区间：{NATIVE_LOG.resolve()}；偏移=128；长度=4096", text
        )
        self.assertIn(
            f"日志区间：{second_log.resolve()}；偏移=32；长度=2048", text
        )


if __name__ == "__main__":
    unittest.main()
