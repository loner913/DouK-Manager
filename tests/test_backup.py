from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from uuid import uuid4
from unittest.mock import patch

from douk_manager.core.backup import BackupError, BackupService, sha256_file
from douk_manager.core.json_store import read_json, write_json_atomic
from douk_manager.core.watchlist import WatchlistService
from tests.helpers import make_test_paths


class BackupTests(unittest.TestCase):
    def _watchlist_service(self, directory: str) -> tuple[object, WatchlistService]:
        paths = make_test_paths(Path(directory), account_count=2)
        watchlist = WatchlistService(paths)
        watchlist.initialize(initialization_evidence=True)
        watchlist.mark_ready()
        return paths, watchlist

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

    def test_startup_snapshot_contains_six_files_and_schema_three(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, watchlist = self._watchlist_service(directory)
            watchlist.observe(
                {
                    "request_id": str(uuid4()),
                    "url": "https://www.douyin.com/user/42",
                    "captured_nickname": "合成昵称",
                    "display_name": "合成名称",
                    "douyin_id": "synthetic_42",
                    "nickname_blank": False,
                    "reasons": ["few_works"],
                    "note": "synthetic",
                }
            )
            service = BackupService(paths)
            snapshot = service.create_startup_snapshot("Startup", {"synthetic": True})
            manifest = read_json(snapshot / "manifest.json")
            self.assertEqual(manifest["schema"], 3)
            self.assertEqual(manifest["scope"], "startup")
            self.assertTrue(manifest["complete"])
            self.assertEqual(
                {item["path"] for item in manifest["files"]},
                {
                    "Volume/settings_master.json",
                    "Volume/settings.json",
                    "Volume/DouK-Downloader.db",
                    "Data/watchlist.json",
                    "Data/watchlist_w_watermark.json",
                    "Data/watchlist_control.json",
                },
            )
            for relative in manifest["files"]:
                copied = snapshot / relative["path"]
                self.assertEqual(relative["size"], copied.stat().st_size)
                self.assertEqual(relative["sha256"], sha256_file(copied))
            self.assertEqual(read_json(snapshot / "Data" / "watchlist_control.json")["write_gate"], "ready")

    def test_startup_rotation_keeps_only_latest_valid_completed_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, watchlist = self._watchlist_service(directory)
            service = BackupService(paths)
            newest = None
            for _ in range(3):
                newest = service.create_startup_snapshot("Startup", keep_latest=2)
            snapshots = [
                item
                for item in (paths.backups / "Startup").iterdir()
                if item.is_dir() and not item.name.startswith(".")
            ]
            self.assertEqual(len(snapshots), 2)
            self.assertIn(newest, snapshots)
            self.assertFalse(watchlist.has_unresolved_recovery())

    def test_unresolved_pending_state_does_not_rotate_old_startup_snapshots(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, watchlist = self._watchlist_service(directory)
            service = BackupService(paths)
            first = service.create_startup_snapshot("Startup", keep_latest=1)
            control = read_json(paths.watchlist_control)
            control["request_receipts"].append(
                {
                    "request_id": str(uuid4()),
                    "payload_digest": "0" * 64,
                    "normalized_url_digest": "1" * 64,
                    "outcome": "pending",
                    "w_id": 1,
                    "a_number": None,
                    "next_review_at": "2030-01-01T00:00:00Z",
                    "created_at": "2026-09-12T00:00:00Z",
                }
            )
            write_json_atomic(paths.watchlist_control, control)
            second = service.create_startup_snapshot("Startup", keep_latest=1)
            snapshots = [
                item
                for item in (paths.backups / "Startup").iterdir()
                if item.is_dir() and not item.name.startswith(".")
            ]
            self.assertEqual(len(snapshots), 2)
            self.assertIn(first, snapshots)
            self.assertIn(second, snapshots)

    def test_invalid_startup_directories_are_quarantined_and_not_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = self._watchlist_service(directory)
            service = BackupService(paths)
            startup_root = paths.backups / "Startup"
            startup_root.mkdir(parents=True, exist_ok=True)
            invalid = startup_root / "invalid-old"
            invalid.mkdir()
            (invalid / "unexpected.txt").write_text("synthetic", encoding="utf-8")
            incomplete = startup_root / "incomplete-old"
            incomplete.mkdir()
            (incomplete / "manifest.json").write_text(
                json.dumps({"schema": 3, "scope": "startup", "complete": False}),
                encoding="utf-8",
            )
            current = service.create_startup_snapshot("Startup", keep_latest=1)
            quarantine = startup_root / "_quarantine"
            self.assertTrue((quarantine / invalid.name).is_dir())
            self.assertTrue((quarantine / incomplete.name).is_dir())
            self.assertTrue(current.is_dir())
            valid_snapshots = [
                item
                for item in startup_root.iterdir()
                if item.is_dir() and not item.name.startswith(".") and item.name != "_quarantine"
            ]
            self.assertEqual(valid_snapshots, [current])

    def test_cleanup_failure_keeps_committed_snapshot_and_reports_cleanup_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = self._watchlist_service(directory)
            service = BackupService(paths)
            with patch.object(
                BackupService,
                "_prune_startup_snapshots",
                side_effect=PermissionError("synthetic cleanup denial"),
            ):
                snapshot = service.create_startup_snapshot("Startup", keep_latest=1)
            self.assertTrue(snapshot.is_dir())
            self.assertIn("cleanup denial", service.last_startup_cleanup_error or "")

    def test_restore_rejects_path_traversal_and_bad_hash_before_live_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = self._watchlist_service(directory)
            service = BackupService(paths)
            snapshot = service.create_startup_snapshot("Startup")
            before = {
                path: read_json(path)
                for path in (paths.watchlist, paths.watchlist_w_watermark, paths.watchlist_control)
            }
            manifest = read_json(snapshot / "manifest.json")
            manifest["files"][0]["path"] = "Volume/../outside.json"
            write_json_atomic(snapshot / "manifest.json", manifest)
            with self.assertRaises(BackupError):
                service.restore_startup_snapshot(snapshot)
            for path, value in before.items():
                self.assertEqual(read_json(path), value)

            manifest["files"][0]["path"] = "Volume/settings_master.json"
            manifest["files"][0]["sha256"] = "f" * 64
            write_json_atomic(snapshot / "manifest.json", manifest)
            with self.assertRaises(BackupError):
                service.restore_startup_snapshot(snapshot)
            for path, value in before.items():
                self.assertEqual(read_json(path), value)

    def test_restore_rejects_snapshot_outside_startup_before_live_writes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, _ = self._watchlist_service(directory)
            service = BackupService(paths)
            snapshot = service.create_startup_snapshot("Startup")
            outside = Path(directory) / "outside-startup-snapshot"
            shutil.copytree(snapshot, outside)
            before = {
                path: path.read_bytes()
                for path in (
                    paths.watchlist,
                    paths.watchlist_w_watermark,
                    paths.watchlist_control,
                )
            }

            with self.assertRaises(BackupError):
                service.restore_startup_snapshot(outside)

            for path, value in before.items():
                self.assertEqual(path.read_bytes(), value)

    def test_restore_merges_current_watermark_and_receipts_and_blocks_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths, watchlist = self._watchlist_service(directory)
            service = BackupService(paths)
            first_payload = {
                "request_id": str(uuid4()),
                "url": "https://www.douyin.com/user/42",
                "captured_nickname": "合成昵称",
                "display_name": "合成名称",
                "douyin_id": "synthetic_42",
                "nickname_blank": False,
                "reasons": ["few_works"],
                "note": "synthetic",
            }
            watchlist.observe(first_payload)
            snapshot = service.create_startup_snapshot("Startup")
            formal_volume_before = {
                name: (paths.volume / name).read_bytes()
                for name in service.CRITICAL_FILENAMES
            }
            second_payload = dict(first_payload)
            second_payload["request_id"] = str(uuid4())
            second_payload["url"] = "https://www.douyin.com/user/43"
            watchlist.observe(second_payload)
            service.restore_startup_snapshot(snapshot)
            restored = read_json(paths.watchlist)
            restored_watermark = read_json(paths.watchlist_w_watermark)
            restored_control = read_json(paths.watchlist_control)
            self.assertEqual([record["w_id"] for record in restored["records"]], [1])
            self.assertEqual(restored["next_w_id"], 3)
            self.assertEqual(restored_watermark["next_w_id"], 3)
            self.assertEqual(restored_control["write_gate"], "blocked")
            self.assertEqual(len(restored_control["request_receipts"]), 2)
            self.assertTrue(watchlist.has_unresolved_recovery())
            for name, value in formal_volume_before.items():
                self.assertEqual((paths.volume / name).read_bytes(), value)


if __name__ == "__main__":
    unittest.main()
