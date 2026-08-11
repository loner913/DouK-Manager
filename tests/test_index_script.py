from __future__ import annotations

import unittest
from pathlib import Path


class IndexScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = (
            Path(__file__).parents[1]
            / "resources"
            / "scripts"
            / "Refresh-DoukIndex.ps1"
        ).read_text(encoding="utf-8-sig")

    def test_incremental_refresh_keeps_legacy_and_current_shortcuts(self) -> None:
        self.assertIn("$isManaged = [bool]($sc.Description", self.script)
        self.assertIn("if ($sc.TargetPath)", self.script)
        self.assertIn("$managedShortcutMap.ContainsKey($targetPath)", self.script)
        self.assertIn("$shortcut.Description -ne $desiredDescription", self.script)
        self.assertIn("$unchanged++", self.script)
        self.assertIn("保持不变：$unchanged 个", self.script)

    def test_refresh_has_no_account_name_fallback_or_launcher_files(self) -> None:
        self.assertIn("$shortcut.TargetPath = $targetPath", self.script)
        self.assertIn("$shortcut.WorkingDirectory = $targetPath", self.script)
        self.assertIn('$shortcut.Description = "$ManagedTag $targetPath"', self.script)
        self.assertIn("$candidateTarget = Get-NormalizedFullPath", self.script)
        self.assertIn("^UID[0-9]+_A[1-9][0-9]*([^0-9]|$)", self.script)
        self.assertNotIn("$shortcut.TargetPath = $script:explorerPath", self.script)
        self.assertNotIn("$shortcut.Arguments =", self.script)
        self.assertNotIn("ManagedBase64Tag", self.script)
        self.assertNotIn("DouKLaunchers", self.script)
        self.assertNotIn("Get-FallbackShortcutBaseName", self.script)
        self.assertNotIn("_Account", self.script)

    def test_shortcut_creation_keeps_proven_original_direct_folder_form(self) -> None:
        creation_start = self.script.index(
            "$displayName = Get-ShortcutDisplayName -FolderName $folder.Name"
        )
        creation_end = self.script.index("$created++", creation_start)
        creation = self.script[creation_start:creation_end]
        expected_lines = (
            "$shortcut.TargetPath = $targetPath",
            "$shortcut.WorkingDirectory = $targetPath",
            '$shortcut.Description = "$ManagedTag $targetPath"',
            "$shortcut.Save()",
        )
        positions = [creation.index(line) for line in expected_lines]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("explorer.exe", self.script)
        self.assertNotIn(".Arguments =", self.script)

    def test_refresh_uses_one_complete_run_log_instead_of_duplicate_broken_report(self) -> None:
        self.assertIn("$RunLogFilePrefix = 'Refresh-DoukIndex-RunLog'", self.script)
        self.assertIn('"发现目标不存在的快捷方式：$($missingShortcutCandidates.Count) 个"', self.script)
        self.assertIn('"快捷方式：$($item.ShortcutName)"', self.script)
        self.assertIn('"目标：$($item.TargetPath)"', self.script)
        self.assertIn('"处理：$actionText"', self.script)
        self.assertNotIn("CleanupReportFilePrefix", self.script)
        self.assertNotIn("GenerateCleanupReport", self.script)

    def test_refresh_deletes_only_two_kinds_of_managed_index_shortcuts(self) -> None:
        safeguards = (
            "$sc.Description.StartsWith($ManagedTag + ' ')",
            "$shortcutParent.Equals($idxFull",
            "$shortcutItem.Extension.Equals('.lnk'",
            "[ValidateSet('EmptySource', 'MissingTarget')]",
            "Remove-Item -LiteralPath $fullShortcutPath -Force -ErrorAction Stop",
        )
        for safeguard in safeguards:
            with self.subTest(safeguard=safeguard):
                self.assertIn(safeguard, self.script)
        self.assertEqual(self.script.count("Remove-Item -LiteralPath"), 1)

    def test_refresh_outputs_chinese_numeric_summary_without_fixed_source_delete_count(self) -> None:
        required = (
            "空源文件夹：$($emptySourceFolders.Count) 个",
            "目标已移动或删除的文件夹：$movedOrDeletedFolderCount 个",
            "计划删除快捷方式合计：$plannedDeletionCount 个",
            "实际删除快捷方式：空源目录对应",
            "DOUK_INDEX_SUMMARY_JSON=",
        )
        for text in required:
            with self.subTest(text=text):
                self.assertIn(text, self.script)
        self.assertNotIn("SourceFoldersDeleted", self.script)
        self.assertNotIn("Source folders deleted", self.script)
        self.assertNotIn("源文件夹删除", self.script)
