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
            "$ManagedTag              = '[DoukIndex]'",
            "Get-ChildItem -LiteralPath $FolderPath -Filter *.lnk -File",
            "Test-PathUnderRoot -Path $fullTarget -Root $srcFull",
            "-not (Test-Path -LiteralPath $fullTarget -PathType Container)",
            "Refusing to delete file outside index folder",
            "Refusing to delete unmanaged shortcut",
            "Remove-Item -LiteralPath $item.ShortcutPath -Force",
        )
        for safeguard in required_safeguards:
            with self.subTest(safeguard=safeguard):
                self.assertIn(safeguard, self.cleanup)

    def test_self_test_uses_temp_only_and_proves_idempotence(self) -> None:
        required_steps = (
            "[System.IO.Path]::GetTempPath()",
            "DouK-Cleanup-SelfTest-",
            "Cleanup-BrokenDoukIndex.ps1",
            "Managed shortcut with an existing target was deleted.",
            "Managed shortcut with a missing target was not deleted.",
            "Unmanaged shortcut was deleted.",
            "Existing source folder was modified or deleted.",
            "Deleted 1 broken shortcuts",
            "Deleted 0 broken shortcuts",
            "Remove-Item -LiteralPath $testRoot -Recurse -Force",
        )
        for step in required_steps:
            with self.subTest(step=step):
                self.assertIn(step, self.self_test)


if __name__ == "__main__":
    unittest.main()
