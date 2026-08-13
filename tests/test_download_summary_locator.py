import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from douk_manager.core.download_summary import (
    NativeLogSegment,
    PlannedAccount,
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
                (PlannedAccount(1, 1, "A1one"), PlannedAccount(2, 2, "A2two")),
            )

            self.assertTrue(result.reliable)
            self.assertEqual(
                result.segments,
                (NativeLogSegment(native.resolve(), 0, native.stat().st_size),),
            )
            self.assertEqual(list(log_dir.iterdir()), [native])

    def test_new_empty_log_is_a_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            before = snapshot_native_logs(log_dir)
            native = log_dir / "empty.log"
            native.touch()
            os.utime(native, (START.timestamp(), START.timestamp()))

            result = locate_native_logs(before, log_dir, START, END, 1)

            self.assertFalse(result.reliable)
            self.assertEqual(
                result.segments,
                (NativeLogSegment(native.resolve(), 0, 0),),
            )

    def test_unique_unanchored_or_post_end_log_is_not_reliable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            before = snapshot_native_logs(log_dir)
            native = log_dir / "unrelated.log"
            native.write_text("unrelated\n", encoding="utf-8")
            os.utime(native, (END.timestamp() + 3, END.timestamp() + 3))

            result = locate_native_logs(before, log_dir, START, END, 1)

            self.assertFalse(result.reliable)

    def test_rotation_joins_unique_contiguous_account_segments(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            before = snapshot_native_logs(log_dir)
            first = log_dir / "z-first.log"
            second = log_dir / "a-second.log"
            first.write_text(
                "共有 2 个账号的作品等待下载\n开始处理第 1 个账号\n标识：A1one\n",
                encoding="utf-8-sig",
            )
            second.write_text(
                "筛选处理后作品数量: 0\n开始处理第 2 个账号\n标识：A2two\n",
                encoding="utf-8-sig",
            )
            os.utime(first, (START.timestamp(), START.timestamp()))
            os.utime(second, (START.timestamp() + 1, START.timestamp() + 1))

            result = locate_native_logs(
                before,
                log_dir,
                START,
                END,
                2,
                (PlannedAccount(1, 1, "A1one"), PlannedAccount(2, 2, "A2two")),
            )

            self.assertTrue(result.reliable)
            self.assertEqual([item.path for item in result.segments], [first.resolve(), second.resolve()])

    def test_locator_never_reads_an_entire_large_segment_at_once(self) -> None:
        class GuardedReader:
            def __init__(self, handle) -> None:
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback) -> None:
                self.handle.close()

            def read(self, size: int = -1):
                if size < 0 or size > 64 * 1024:
                    raise AssertionError(f"unbounded read: {size}")
                return self.handle.read(size)

            def __getattr__(self, name):
                return getattr(self.handle, name)

        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            before = snapshot_native_logs(log_dir)
            native = log_dir / "large.log"
            native.write_text(
                "共有 1 个账号的作品等待下载\n"
                "开始处理第 1 个账号\n"
                "标识：A1one\n"
                + "填充行\n" * 20000,
                encoding="utf-8-sig",
            )
            os.utime(native, (START.timestamp(), START.timestamp()))
            original_open = Path.open

            def guarded_open(path: Path, *args, **kwargs):
                return GuardedReader(original_open(path, *args, **kwargs))

            with patch.object(Path, "open", guarded_open):
                result = locate_native_logs(
                    before,
                    log_dir,
                    START,
                    END,
                    1,
                    (PlannedAccount(1, 1, "A1one"),),
                )

            self.assertTrue(result.reliable)
            self.assertEqual(result.segments[0].path, native.resolve())

    def test_grown_existing_log_starts_at_old_byte_offset(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            native = log_dir / "current.log"
            native.write_bytes(b"old\n")
            before = snapshot_native_logs(log_dir)
            with native.open("ab") as handle:
                handle.write(
                    "共有 1 个账号的作品等待下载\n开始处理第 1 个账号\n".encode("utf-8")
                )
            os.utime(native, (START.timestamp(), START.timestamp()))

            result = locate_native_logs(
                before, log_dir, START, END, 1, (PlannedAccount(1, 1, "A1one"),)
            )

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
            first.write_text(
                "共有 2 个账号的作品等待下载\n开始处理第 1 个账号\n",
                encoding="utf-8-sig",
            )
            second.write_text("共有 3 个账号的作品等待下载\n", encoding="utf-8-sig")
            os.utime(first, (START.timestamp(), START.timestamp()))
            os.utime(second, (START.timestamp(), START.timestamp()))

            result = locate_native_logs(
                before,
                log_dir,
                START,
                END,
                2,
                (PlannedAccount(1, 1, "A1one"), PlannedAccount(2, 2, "A2two")),
            )

            self.assertTrue(result.reliable)
            self.assertEqual(
                result.segments,
                (NativeLogSegment(first.resolve(), 0, first.stat().st_size),),
            )

    def test_unanchored_trailing_candidate_is_not_joined_to_the_run(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            before = snapshot_native_logs(log_dir)
            first = log_dir / "run.log"
            unrelated = log_dir / "unrelated.log"
            first.write_text(
                "共有 1 个账号的作品等待下载\n开始处理第 1 个账号\n",
                encoding="utf-8-sig",
            )
            unrelated.write_text("[ERROR]: unrelated\n", encoding="utf-8-sig")
            os.utime(first, (START.timestamp(), START.timestamp()))
            os.utime(unrelated, (START.timestamp() + 1, START.timestamp() + 1))

            result = locate_native_logs(
                before,
                log_dir,
                START,
                END,
                1,
                (PlannedAccount(1, 1, "A1one"),),
            )

            self.assertTrue(result.reliable)
            self.assertEqual(
                result.segments,
                (NativeLogSegment(first.resolve(), 0, first.stat().st_size),),
            )

    def test_unique_unanchored_candidate_has_readable_failure_reason(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            before = snapshot_native_logs(log_dir)
            native = log_dir / "unanchored.log"
            native.write_text("开始处理第 1 个账号\n", encoding="utf-8-sig")
            os.utime(native, (START.timestamp(), START.timestamp()))

            result = locate_native_logs(before, log_dir, START, END, 1)

            self.assertFalse(result.reliable)
            self.assertEqual(result.reason, "本次原生日志缺少运行锚点。")

    def test_anchor_after_first_64_kib_does_not_resolve_ambiguity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            before = snapshot_native_logs(log_dir)
            first = log_dir / "late-anchor.log"
            second = log_dir / "no-anchor.log"
            first.write_bytes(
                b"x" * (64 * 1024) + "共有 2 个账号的作品等待下载\n".encode("utf-8")
            )
            second.write_bytes(b"no task anchor\n")
            os.utime(first, (START.timestamp(), START.timestamp()))
            os.utime(second, (START.timestamp(), START.timestamp()))

            result = locate_native_logs(before, log_dir, START, END, 2)

            self.assertFalse(result.reliable)
            self.assertEqual(
                result.reason,
                "存在多个候选原生日志，无法唯一确定本次日志。",
            )


if __name__ == "__main__":
    unittest.main()
