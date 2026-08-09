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
        self.assertIn("function Test-ManagedShortcutIsCurrent", self.script)
        self.assertIn("$unchanged++", self.script)
        self.assertIn("Unchanged shortcuts: $unchanged", self.script)
        self.assertIn("$shortcut.TargetPath.Equals($ExpectedTargetPath", self.script)

    def test_refresh_has_no_account_name_fallback_or_launcher_files(self) -> None:
        self.assertIn("function Save-ManagedShortcut", self.script)
        self.assertIn("$shortcut.TargetPath = $TargetPath", self.script)
        self.assertIn("$shortcut.WorkingDirectory = $TargetPath", self.script)
        self.assertIn('$shortcut.Description = "$ManagedTag $TargetPath"', self.script)
        self.assertIn("$directTarget = Get-NormalizedFullPath", self.script)
        self.assertIn("^UID[0-9]+_A[1-9][0-9]*([^0-9]|$)", self.script)
        self.assertNotIn("$shortcut.TargetPath = $script:explorerPath", self.script)
        self.assertNotIn("$shortcut.Arguments =", self.script)
        self.assertNotIn("ManagedBase64Tag", self.script)
        self.assertNotIn("DouKLaunchers", self.script)
        self.assertNotIn("Get-FallbackShortcutBaseName", self.script)
        self.assertNotIn("_Account", self.script)

    def test_shortcut_creation_keeps_proven_original_direct_folder_form(self) -> None:
        expected_lines = (
            "$shortcut = $script:shell.CreateShortcut($ShortcutPath)",
            "$shortcut.TargetPath = $TargetPath",
            "$shortcut.WorkingDirectory = $TargetPath",
            '$shortcut.Description = "$ManagedTag $TargetPath"',
            "$shortcut.Save()",
        )
        positions = [self.script.index(line) for line in expected_lines]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn("explorer.exe", self.script)
        self.assertNotIn(".Arguments =", self.script)

    def test_refresh_uses_one_complete_run_log_instead_of_duplicate_broken_report(self) -> None:
        self.assertIn("$RunLogFilePrefix        = 'Refresh-DoukIndex-RunLog'", self.script)
        self.assertIn('"Detected broken shortcuts: $brokenDetectedCount"', self.script)
        self.assertIn('"Shortcut: $($item.ShortcutName)"', self.script)
        self.assertIn('"Target: $($item.TargetPath)"', self.script)
        self.assertIn('"Action: $($item.Action)"', self.script)
        self.assertNotIn("Broken-Shortcut-Report", self.script)
        self.assertNotIn("GenerateBrokenReport", self.script)
