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
            snapshot = service.create_snapshot("Startup", {"test": True})
            self.assertTrue((snapshot / "Volume" / "settings_master.json").is_file())
            self.assertTrue((snapshot / "Volume" / "settings.json").is_file())
            backup_db = snapshot / "Volume" / "DouK-Downloader.db"
            with closing(sqlite3.connect(backup_db)) as connection:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
                self.assertEqual(connection.execute("SELECT value FROM records").fetchone()[0], "safe")
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["validation"]["database_quick_check"], "ok")
            entries = {item["path"]: item for item in manifest["files"]}
            key = "Volume/settings_master.json"
            self.assertEqual(entries[key]["sha256"], sha256_file(snapshot / key))


if __name__ == "__main__":
    unittest.main()
