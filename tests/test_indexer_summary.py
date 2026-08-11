from __future__ import annotations

import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from douk_manager.controller import ManagerController
from douk_manager.integrations.indexer import IndexError, IndexResult, parse_index_output


def _summary(**overrides: object) -> dict[str, object]:
    result: dict[str, object] = {
        "Mode": "Refresh",
        "SourceRoot": r"F:\DouK-Downloader",
        "IndexRoot": r"F:\Douk videos",
        "SourceFoldersScanned": 1245,
        "IgnoredSourceFolders": 2,
        "EmptySourceFolders": 87,
        "MovedOrDeletedTargetFolders": 3,
        "EmptySourceShortcutsDetected": 4,
        "MissingTargetShortcutsDetected": 3,
        "PlannedShortcutDeletions": 7,
        "DeletedEmptySourceShortcuts": 4,
        "DeletedMissingTargetShortcuts": 2,
        "DeletedShortcutsTotal": 6,
        "ShortcutDeleteFailures": 1,
        "RemainingEmptySourceShortcuts": 0,
        "RemainingMissingTargetShortcuts": 1,
        "ShortcutReadFailures": 0,
        "Created": 5,
        "Updated": 6,
        "Unchanged": 1147,
        "IndexFailures": 0,
    }
    result.update(overrides)
    return result


class IndexSummaryTests(unittest.TestCase):
    def test_machine_json_is_hidden_and_every_number_is_displayed(self) -> None:
        raw = "中文详细输出\nDOUK_INDEX_SUMMARY_JSON=" + json.dumps(
            _summary(), ensure_ascii=False, separators=(",", ":")
        )

        visible, summary = parse_index_output(raw)

        self.assertEqual(visible, "中文详细输出")
        self.assertIsNotNone(summary)
        text = IndexResult(0, visible, summary).display_summary("索引刷新")
        expected_fragments = (
            "新建5",
            "更新6",
            "保持不变1147",
            "空源文件夹87",
            "目标已移动或删除的文件夹3",
            "计划7（空源目录对应4、目标不存在3）",
            "实际删除6（空源目录对应4、目标不存在2）",
            "失败1",
            r"删除来源仅限 F:\Douk videos",
        )
        for fragment in expected_fragments:
            with self.subTest(fragment=fragment):
                self.assertIn(fragment, text)
        self.assertNotIn("DOUK_INDEX_SUMMARY_JSON", visible)
        self.assertNotIn("源文件夹删除", text)

    def test_invalid_or_incomplete_numeric_summary_is_rejected(self) -> None:
        invalid_cases = (
            "DOUK_INDEX_SUMMARY_JSON={not-json}",
            "DOUK_INDEX_SUMMARY_JSON=" + json.dumps(_summary(EmptySourceFolders="87")),
            "DOUK_INDEX_SUMMARY_JSON=" + json.dumps(_summary(ShortcutDeleteFailures=-1)),
            "DOUK_INDEX_SUMMARY_JSON=" + json.dumps(_summary(PlannedShortcutDeletions=8)),
        )
        for raw in invalid_cases:
            with self.subTest(raw=raw), self.assertRaises(IndexError):
                parse_index_output(raw)

    def test_manager_no_longer_uses_bare_completed_messages(self) -> None:
        controller = (
            Path(__file__).parents[1] / "src" / "douk_manager" / "controller.py"
        ).read_text(encoding="utf-8")
        self.assertNotIn('messages.append("索引刷新：完成")', controller)
        self.assertNotIn('messages.append("失效快捷方式清理：完成")', controller)
        self.assertIn('display_summary("索引刷新")', controller)
        self.assertIn('display_summary("失效快捷方式清理")', controller)

    def test_post_action_returns_numeric_refresh_and_cleanup_messages(self) -> None:
        controller = ManagerController.__new__(ManagerController)
        controller.config = SimpleNamespace(
            screenshot_post_mode="none",
            index_post_mode="queue",
            cleanup_after_index=True,
        )
        refresh_result = IndexResult(0, "", _summary())
        cleanup_result = IndexResult(0, "", _summary(Mode="ManualCleanup"))
        controller.refresh_index = lambda: refresh_result
        controller.cleanup_index = lambda: cleanup_result

        messages = controller.run_post_actions("queue")

        self.assertEqual(len(messages), 2)
        self.assertIn("索引刷新：快捷方式新建5", messages[0])
        self.assertIn("计划7", messages[0])
        self.assertIn("失效快捷方式清理：源目录检查", messages[1])
        self.assertIn("实际删除6", messages[1])


if __name__ == "__main__":
    unittest.main()
