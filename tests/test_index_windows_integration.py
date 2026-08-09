from __future__ import annotations

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
    def _shortcut_properties(shortcut_path: Path) -> dict[str, str]:
        output_path = shortcut_path.with_suffix(".properties.json")
        command = (
            "$w=New-Object -ComObject WScript.Shell;"
            "$s=$w.CreateShortcut($env:DOUK_TEST_LINK);"
            "$j=[PSCustomObject]@{Description=$s.Description;TargetPath=$s.TargetPath;"
            "Arguments=$s.Arguments}|ConvertTo-Json -Compress;"
            "$u=New-Object System.Text.UTF8Encoding($false);"
            "[IO.File]::WriteAllText($env:DOUK_TEST_OUTPUT,$j,$u)"
        )
        environment = os.environ.copy()
        environment["DOUK_TEST_LINK"] = str(shortcut_path)
        environment["DOUK_TEST_OUTPUT"] = str(output_path)
        completed = subprocess.run(
            ["powershell.exe", "-NoLogo", "-NoProfile", "-Command", command],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            timeout=30,
        )
        try:
            return json.loads(output_path.read_text(encoding="utf-8"))
        finally:
            output_path.unlink(missing_ok=True)

    def test_corrected_account_names_are_valid_and_second_refresh_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_root:
            root = Path(temporary_root)
            source = root / "source"
            index = root / "index"
            source.mkdir()
            index.mkdir()

            folders = {
                "A1173 38224167560_发布作品": source / "UID2187354221600297_A1173 38224167560_发布作品",
                "A1329小小ljj19777_发布作品": source / "UID70994864192_A1329小小ljj19777_发布作品",
            }
            for folder in folders.values():
                folder.mkdir()
                (folder / "item.txt").write_text("not empty", encoding="utf-8")

            first = self._run_refresh(source, index)
            self.assertEqual(first["Created"], "2")
            self.assertEqual(first["Shortcut failures"], "0")

            for display_name, target in folders.items():
                shortcut = index / f"{display_name}.lnk"
                self.assertTrue(shortcut.is_file())

                properties = self._shortcut_properties(shortcut)
                self.assertEqual(properties["Description"], f"[DoukIndex] {target}")
                self.assertEqual(Path(properties["TargetPath"]), target)
                self.assertEqual(properties["Arguments"], "")

            second = self._run_refresh(source, index)
            self.assertEqual(second["Created"], "0")
            self.assertEqual(second["Updated"], "0")
            self.assertEqual(second["Unchanged"], "2")
            self.assertEqual(second["Shortcut failures"], "0")
            self.assertEqual(len(list(index.glob("*.lnk"))), 2)
