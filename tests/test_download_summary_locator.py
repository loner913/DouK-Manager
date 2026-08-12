import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from douk_manager.core.download_summary import (
    NativeLogSegment,
    locate_native_logs,
    snapshot_native_logs,
)


START = datetime(2026, 8, 13, 10, 0, 0)
END = datetime(2026, 8, 13, 10, 1, 0)


class NativeLogLocatorTests(unittest.TestCase):
    def test_new_log_is_selected_without_copying_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            before = snapshot_native_logs(log_dir)
            native = log_dir / "2026-08-13 10.00.00.log"
            native.write_text(
                "共有 2 个账号的作品等待下载\n开始处理第 1 个账号\n",
                encoding="utf-8-sig",
            )
            os.utime(native, (START.timestamp(), START.timestamp()))

            result = locate_native_logs(
                before,
                log_dir,
                datetime(2026, 8, 13, 10, 0, 0),
                datetime(2026, 8, 13, 10, 1, 0),
                2,
            )

            self.assertTrue(result.reliable)
            self.assertEqual(
                result.segments,
                (NativeLogSegment(native.resolve(), 0, native.stat().st_size),),
            )
            self.assertEqual(list(log_dir.iterdir()), [native])

    def test_grown_existing_log_starts_at_old_byte_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            native = log_dir / "current.log"
            native.write_bytes(b"old\n")
            before = snapshot_native_logs(log_dir)
            with native.open("ab") as handle:
                handle.write("共有 1 个账号的作品等待下载\n".encode("utf-8"))
            os.utime(native, (START.timestamp(), START.timestamp()))

            result = locate_native_logs(before, log_dir, START, END, 1)

            self.assertTrue(result.reliable)
            self.assertEqual(result.segments[0].offset, 4)
            self.assertEqual(result.segments[0].length, native.stat().st_size - 4)

    def test_two_new_logs_are_rejected_as_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            before = snapshot_native_logs(log_dir)
            first = log_dir / "one.log"
            second = log_dir / "two.log"
            first.write_text("无任务锚点\n", encoding="utf-8")
            second.write_text("无任务锚点\n", encoding="utf-8")
            os.utime(first, (START.timestamp(), START.timestamp()))
            os.utime(second, (START.timestamp(), START.timestamp()))

            result = locate_native_logs(before, log_dir, START, END, 2)

            self.assertFalse(result.reliable)
            self.assertEqual(
                result.reason,
                "存在多个候选原生日志，无法唯一确定本次日志。",
            )

    def test_no_changed_log_returns_unreliable_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            (log_dir / "old.log").write_text("old\n", encoding="utf-8")
            before = snapshot_native_logs(log_dir)

            result = locate_native_logs(
                before=before,
                log_dir=log_dir,
                started_at=START,
                ended_at=END,
                planned_count=1,
            )

            self.assertFalse(result.reliable)
            self.assertEqual(
                result.reason,
                "未找到本次新增或增长的原生日志。",
            )

    def test_unique_anchor_match_resolves_multiple_candidates(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            before = snapshot_native_logs(log_dir)
            first = log_dir / "one.log"
            second = log_dir / "two.log"
            first.write_text("共有 2 个账号的作品等待下载\n", encoding="utf-8-sig")
            second.write_text("共有 3 个账号的作品等待下载\n", encoding="utf-8-sig")
            os.utime(first, (START.timestamp(), START.timestamp()))
            os.utime(second, (START.timestamp(), START.timestamp()))

            result = locate_native_logs(before, log_dir, START, END, 2)

            self.assertTrue(result.reliable)
            self.assertEqual(result.segments[0].path, first.resolve())


if __name__ == "__main__":
    unittest.main()
