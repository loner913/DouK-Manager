from __future__ import annotations

import unittest
from datetime import datetime
from pathlib import Path

from douk_manager.ui_messages import format_information


class InformationFormattingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.moment = datetime(2026, 8, 11, 9, 7, 5)

    def test_separate_information_lines_each_have_the_same_real_time(self) -> None:
        text = format_information(
            "第一项",
            "第二项\n\n第三项",
            at=self.moment,
        )

        self.assertEqual(
            text,
            "[09:07:05] 第一项\n[09:07:05] 第二项\n[09:07:05] 第三项",
        )

    def test_same_moment_details_can_merge_into_one_information_line(self) -> None:
        text = format_information(
            "下载进程正常结束。",
            "退出码=0",
            "下载结果需核对原生日志",
            at=self.moment,
            merge=True,
        )

        self.assertEqual(
            text,
            "[09:07:05] 下载进程正常结束；退出码=0；下载结果需核对原生日志",
        )
        self.assertEqual(text.count("[09:07:05]"), 1)

    def test_file_log_uses_full_date_and_time(self) -> None:
        text = format_information(
            "Exited: code=0",
            "Process status: normal",
            at=self.moment,
            merge=True,
            include_date=True,
        )

        self.assertEqual(
            text,
            "[2026-08-11 09:07:05] Exited: code=0；Process status: normal",
        )

    def test_queue_combines_details_from_one_event(self) -> None:
        gui_source = (
            Path(__file__).parents[1] / "src" / "douk_manager" / "gui.py"
        ).read_text(encoding="utf-8")

        self.assertIn("self._append_info(", gui_source)
        self.assertIn('f"队列开始，共 {len(paths)} 个任务。"', gui_source)
        self.assertIn('f"本次执行顺序：{order_text}"', gui_source)
        self.assertIn(
            "self._append_info(self.queue_output, assessment.headline, assessment.detail)",
            gui_source,
        )


if __name__ == "__main__":
    unittest.main()
