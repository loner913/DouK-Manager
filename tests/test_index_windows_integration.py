from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform == "win32", "Windows WScript integration test")
class WindowsIndexIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.script = (
            Path(__file__).parents[1]
            / "resources"
            / "scripts"
            / "Refresh-DoukIndex.ps1"
        )

    @staticmethod
    def _summary(log_path: Path) -> dict[str, str]:
        result: dict[str, str] = {}
        for line in log_path.read_text(encoding="utf-8-sig").splitlines():
            if not line:
                break
            if ": " in line:
                key, value = line.split(": ", 1)
                result[key] = value
        return result

    def _run_refresh(self, source: Path, index: Path) -> dict[str, str]:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(self.script),
                "-SourceRoot",
                str(source),
                "-IndexRoot",
                str(index),
                "-GenerateBrokenReport",
            ],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
        )
        self.assertEqual(
            completed.returncode,
            0,
            msg=f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}",
        )
        newest_log = max(
            (index / "Logs").glob("Refresh-DoukIndex-RunLog_*.txt"),
            key=lambda path: path.stat().st_mtime_ns,
        )
        return self._summary(newest_log)

    @staticmethod
    def _create_v5_bad_fallbacks(index: Path) -> None:
        command = (
            "$w=New-Object -ComObject WScript.Shell;"
            "foreach($account in @('A1173','A1329')){"
            "foreach($suffix in @('', ' (2)', ' (3)')){"
            "$p=Join-Path $env:DOUK_TEST_INDEX ($account+'_Account'+$suffix+'.lnk');"
            "$s=$w.CreateShortcut($p);$s.TargetPath=(Join-Path $env:WINDIR 'explorer.exe');"
            "$s.Arguments='C:\\Users\\Public\\Documents';"
            "$s.Description='[DoukIndex] C:\\corrupted';$s.Save()}}"
        )
        environment = os.environ.copy()
        environment["DOUK_TEST_INDEX"] = str(index)
        subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", command],
            check=True,
            capture_output=True,
            text=True,
            encoding="ascii",
            errors="strict",
            env=environment,
            timeout=30,
        )

    @staticmethod
    def _shortcut_properties(shortcut_path: Path) -> dict[str, str]:
        command = (
            "$w=New-Object -ComObject WScript.Shell;"
            "$s=$w.CreateShortcut($env:DOUK_TEST_LINK);"
            "[PSCustomObject]@{Description=$s.Description;TargetPath=$s.TargetPath;"
            "Arguments=$s.Arguments}|ConvertTo-Json -Compress"
        )
        environment = os.environ.copy()
        environment["DOUK_TEST_LINK"] = str(shortcut_path)
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", command],
            check=True,
            capture_output=True,
            text=True,
            encoding="ascii",
            errors="strict",
            env=environment,
            timeout=30,
        )
        return json.loads(completed.stdout)

    def test_unicode_fallback_is_valid_and_second_refresh_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            source = root / "source"
            index = root / "index"
            source.mkdir()
            index.mkdir()

            folders = {
                "A1173": source / "UID2187354221600297_A1173\u4177\u417738224167560_发布作品",
                "A1329": source / "UID70994864192_A1329小小\U000e0000ljj19777_发布作品",
            }
            for folder in folders.values():
                folder.mkdir()
                (folder / "item.txt").write_text("not empty", encoding="utf-8")

            self._create_v5_bad_fallbacks(index)

            first = self._run_refresh(source, index)
            self.assertEqual(first["Created"], "2")
            self.assertEqual(first["Fallback shortcut names"], "2")
            self.assertEqual(first["Removed obsolete fallback copies"], "4")
            self.assertEqual(first["Shortcut failures"], "0")

            for account, target in folders.items():
                shortcut = index / f"{account}_Account.lnk"
                launcher = index / ".DouKLaunchers" / f"{account}_Account.ps1"
                self.assertTrue(shortcut.is_file())
                self.assertTrue(launcher.is_file())
                self.assertFalse((index / f"{account}_Account (2).lnk").exists())

                properties = self._shortcut_properties(shortcut)
                self.assertTrue(properties["Description"].startswith("[DoukIndexB64] "))
                encoded_target = properties["Description"].split(" ", 1)[1]
                decoded_target = base64.b64decode(encoded_target).decode("utf-16-le")
                self.assertEqual(Path(decoded_target), target)
                self.assertTrue(properties["TargetPath"].lower().endswith("powershell.exe"))
                self.assertIn(str(launcher), properties["Arguments"])
                self.assertLess(len(properties["Arguments"]), 260)

                launcher_text = launcher.read_text(encoding="ascii")
                self.assertIn(encoded_target, launcher_text)
                self.assertIn("Invoke-Item -LiteralPath $targetPath", launcher_text)

            second = self._run_refresh(source, index)
            self.assertEqual(second["Created"], "0")
            self.assertEqual(second["Updated"], "0")
            self.assertEqual(second["Unchanged"], "2")
            self.assertEqual(second["Fallback shortcut names"], "0")
            self.assertEqual(second["Shortcut failures"], "0")
            for account in folders:
                self.assertFalse((index / f"{account}_Account (2).lnk").exists())
