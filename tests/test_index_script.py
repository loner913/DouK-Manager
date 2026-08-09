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

    def test_unencodable_shortcut_name_has_account_number_fallback(self) -> None:
        self.assertIn("function Get-FallbackShortcutBaseName", self.script)
        self.assertIn("$matches[1] + '_Account'", self.script)
        self.assertIn("function Save-EncodedManagedShortcut", self.script)
        self.assertIn("$ManagedBase64Tag", self.script)
        self.assertIn("function Get-FallbackLauncherContent", self.script)
        self.assertIn(".DouKLaunchers", self.script)
        self.assertIn("Invoke-Item -LiteralPath $targetPath", self.script)
        self.assertNotIn("-EncodedCommand", self.script)
        self.assertIn("Encoded shortcut verification failed", self.script)
        self.assertIn("function Remove-ObsoleteFallbackCopies", self.script)
        self.assertIn("fallback failed:", self.script)
