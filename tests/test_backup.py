from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from douk_manager.core.backup import BackupService, sha256_file
from tests.helpers import make_test_paths


class BackupTests(unittest.TestCase):
    def test_full_volume_snapshot_is_consistent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory))
            service = BackupService(paths)
            cache_file = paths.volume / "Cache" / "large-cache.bin"
            cache_file.parent.mkdir()
            cache_file.write_bytes(b"cache must only exist in full snapshots")
            snapshot = service.create_full_snapshot("Manual", {"test": True})
            self.assertTrue((snapshot / "Volume" / "settings_master.json").is_file())
            self.assertTrue((snapshot / "Volume" / "settings.json").is_file())
            self.assertTrue((snapshot / "Volume" / "Cache" / "large-cache.bin").is_file())
            backup_db = snapshot / "Volume" / "DouK-Downloader.db"
            with closing(sqlite3.connect(backup_db)) as connection:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT value FROM records").fetchone()[0], "safe")
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["scope"], "full")
            self.assertEqual(manifest["validation"]["database_quick_check"], "ok")
            entries = {item["path"]: item for item in manifest["files"]}
            key = "Volume/settings_master.json"
            self.assertEqual(entries[key]["sha256"], sha256_file(snapshot / key))

    def test_critical_snapshot_excludes_cache_and_prunes_old_automatic_copies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory))
            cache_file = paths.volume / "Cache" / "large-cache.bin"
            cache_file.parent.mkdir()
            cache_file.write_bytes(b"x" * 1024)
            service = BackupService(paths)

            newest = None
            for number in range(4):
                newest = service.create_critical_snapshot(
                    "Startup",
                    {"number": number},
                    keep_latest=2,
                )

            snapshots = sorted(
                path
                for path in (paths.backups / "Startup").iterdir()
                if path.is_dir() and not path.name.startswith(".")
            )
            self.assertEqual(len(snapshots), 2)
            self.assertIn(newest, snapshots)
            self.assertFalse((newest / "Volume" / "Cache").exists())
            self.assertEqual(
                {path.name for path in (newest / "Volume").iterdir()},
                set(service.CRITICAL_FILENAMES),
            )
            manifest = json.loads((newest / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["scope"], "critical")


if __name__ == "__main__":
    unittest.main()
