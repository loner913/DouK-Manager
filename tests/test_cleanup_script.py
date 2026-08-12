from __future__ import annotations

import unittest
from pathlib import Path


class CleanupScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        scripts = Path(__file__).parents[1] / "resources" / "scripts"
        self.cleanup = (scripts / "Cleanup-BrokenDoukIndex.ps1").read_text(
            encoding="utf-8-sig"
        )
        self.self_test = (scripts / "Test-CleanupBrokenDoukIndex.ps1").read_text(
            encoding="utf-8-sig"
        )

    def test_cleanup_is_limited_to_managed_broken_shortcuts(self) -> None:
        required_safeguards = (
            "[string]$SourceRoot = 'F:\\DouK-Downloader'",
            "$ManagedTag = '[DoukIndex]'",
            "Get-ChildItem -LiteralPath $FolderPath -Filter *.lnk -File",
            "Test-PathIsStrictChildOfRoot -Path $fullTarget -Root $srcFull",
            "$sc.Description.StartsWith($ManagedTag + ' ')",
            "$shortcutParent.Equals($idxFull",
            "$shortcutItem.Extension.Equals('.lnk'",
            "Remove-Item -LiteralPath $fullShortcutPath -Force -ErrorAction Stop",
            "Test-SourceFolderIsEmptyByOriginalRule -FolderPath $actualTarget",
        )
        for safeguard in required_safeguards:
            with self.subTest(safeguard=safeguard):
                self.assertIn(safeguard, self.cleanup)
        self.assertEqual(self.cleanup.count("Remove-Item -LiteralPath"), 1)
        self.assertNotIn("SourceFoldersDeleted", self.cleanup)
        self.assertNotIn("源文件夹删除", self.cleanup)

    def test_self_test_uses_temp_only_and_proves_idempotence(self) -> None:
        required_steps = (
            "[System.IO.Path]::GetTempPath()",
            "DouK-Cleanup-SelfTest-",
            "Cleanup-BrokenDoukIndex.ps1",
            "目标存在的受管快捷方式被误删。",
            "目标不存在的受管快捷方式未被删除。",
            "非受管快捷方式被误删。",
            "现存源文件夹被修改或删除。",
            "DOUK_INDEX_SUMMARY_JSON=",
            "DeletedShortcutsTotal -eq 1",
            "DeletedShortcutsTotal -eq 0",
            "Remove-Item -LiteralPath $testRoot -Recurse -Force",
        )
        for step in required_steps:
            with self.subTest(step=step):
                self.assertIn(step, self.self_test)

    def test_cleanup_report_is_written_inside_explicit_log_root(self) -> None:
        self.assertIn("[Parameter(Mandatory)]", self.cleanup)
        self.assertIn("[string]$LogRoot", self.cleanup)
        self.assertIn("$logFull = Get-NormalizedFullPath -Path $LogRoot", self.cleanup)
        self.assertIn(
            "New-Item -ItemType Directory -Path $logFull -ErrorAction Stop",
            self.cleanup,
        )
        self.assertIn(
            '$reportPath = Join-Path $logFull ("{0}_{1}.txt" -f $CleanupReportFilePrefix, $runTimestamp)',
            self.cleanup,
        )
        self.assertIn("Get-Date -Format 'yyyy-MM-dd_HH-mm-ss-fff'", self.cleanup)
        self.assertIn(
            "$CleanupReportFilePrefix = 'Cleanup-Broken-Shortcut-Report'",
            self.cleanup,
        )
        self.assertNotIn("$logRoot = Join-Path $idxFull 'Logs'", self.cleanup)

    def test_cleanup_self_test_forwards_temporary_log_root(self) -> None:
        self.assertIn("$cleanupLogRoot = Join-Path $testRoot 'logs\\IndexCleanup'", self.self_test)
        self.assertEqual(self.self_test.count("-LogRoot $cleanupLogRoot"), 2)

    def test_cleanup_log_and_console_are_chinese_and_numeric(self) -> None:
        required_text = (
            "空源文件夹：$($emptySourceFolders.Count) 个",
            "目标已移动或删除的文件夹：$movedOrDeletedFolderCount 个",
            "计划删除快捷方式合计：$plannedDeletionCount 个",
            "已删除快捷方式合计：$actualDeletedCount 个",
            "DOUK_INDEX_SUMMARY_JSON=",
        )
        for text in required_text:
            with self.subTest(text=text):
                self.assertIn(text, self.cleanup)
        forbidden_text = (
            "Manual cleanup",
            "Source folders deleted",
            "EMPTY SOURCE FOLDERS",
            "DELETE FAILURES",
        )
        for text in forbidden_text:
            with self.subTest(text=text):
                self.assertNotIn(text, self.cleanup)


if __name__ == "__main__":
    unittest.main()
