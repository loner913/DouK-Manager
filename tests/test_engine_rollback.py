from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QApplication,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from douk_manager.background import ClosePolicy
from douk_manager.config import AppConfig, ManagedPaths
from douk_manager.controller import ControllerError, ManagerController
from douk_manager.core.backup import BackupService, sha256_file
from douk_manager.core.engine_update import (
    EngineRollbackPoint,
    EngineRollbackPreview,
    EngineUpdateError,
    EngineUpdateService,
    RollbackApplyGate,
    RollbackIntegrity,
    RollbackOrigin,
    rollback_can_apply,
)
from douk_manager.gui import EngineRollbackTable, MainWindow
from douk_manager.operation import OperationContext, TaskCancelled
from douk_manager.startup import StartupState


RUNTIME_ROOT = Path(__file__).resolve().parents[2] / "v016-runtime" / "tmp"


def make_paths(base: Path) -> ManagedPaths:
    engine_root = base / "engine"
    volume = engine_root / "_internal" / "Volume"
    volume.mkdir(parents=True)
    (engine_root / "main.exe").write_bytes(b"current-engine")
    document = {"accounts_urls": [], "run_command": "", "cookie": ""}
    (volume / "settings_master.json").write_text(json.dumps(document), encoding="utf-8")
    (volume / "settings.json").write_text(json.dumps(document), encoding="utf-8")
    with closing(sqlite3.connect(volume / "DouK-Downloader.db")) as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        connection.commit()
    config = AppConfig(
        engine_exe=str(engine_root / "main.exe"),
        video_root=str(base / "videos"),
        index_root=str(base / "index"),
        old_screenshot_dir=str(base / "old"),
    )
    paths = ManagedPaths.from_config(config, base / "manager")
    paths.ensure_manager_directories()
    return paths


def write_point(
    root: Path,
    stamp: str,
    payload: bytes,
    *,
    manifest_name: str | None = "update-manifest.json",
    expected_sha: str | None = None,
    include_volume: bool = False,
) -> Path:
    point = root / stamp
    internal = point / "_internal"
    internal.mkdir(parents=True)
    main = point / "main.exe"
    main.write_bytes(payload)
    (internal / "runtime.dll").write_bytes(b"runtime")
    if include_volume:
        (internal / "Volume" / "unexpected.txt").parent.mkdir()
        (internal / "Volume" / "unexpected.txt").write_bytes(b"volume")
    if manifest_name is not None:
        manifest = {
            "schema": 1,
            "installed_at": "2026-09-06T10:20:30",
            "archive": "engine-package.zip",
            "archive_sha256": "a" * 64,
            "old_main_sha256": expected_sha or hashlib.sha256(payload).hexdigest(),
            "new_main_sha256": "b" * 64,
        }
        if manifest_name == "rollback-manifest.json":
            manifest["replaced_main_sha256"] = expected_sha or hashlib.sha256(payload).hexdigest()
            manifest.pop("old_main_sha256")
        (point / manifest_name).write_text(json.dumps(manifest), encoding="utf-8")
    return point


def rollback_point(integrity: RollbackIntegrity) -> EngineRollbackPoint:
    return EngineRollbackPoint(
        directory=Path("synthetic") / "EngineRollback" / "point",
        origin=RollbackOrigin.ROLLBACK,
        stamp="2026-09-06_10-00-00-000000",
        installed_at=datetime(2026, 9, 6, 10, 0, 0),
        archive_name="synthetic-engine.zip",
        archive_sha256="a" * 64,
        main_exe_sha256="b" * 64 if integrity is RollbackIntegrity.OK else None,
        main_exe_bytes=16,
        internal_file_count=2,
        has_manifest=integrity is RollbackIntegrity.OK,
        integrity=integrity,
        reject_reason="synthetic fixed reason",
    )


class EngineRollbackTests(unittest.TestCase):
    def temporary_directory(self):
        RUNTIME_ROOT.mkdir(parents=True, exist_ok=True)
        return tempfile.TemporaryDirectory(dir=RUNTIME_ROOT)

    def test_can_apply_is_the_single_gate_for_all_integrity_states(self) -> None:
        expected = {
            RollbackIntegrity.OK: RollbackApplyGate.ALLOWED,
            RollbackIntegrity.NO_MANIFEST: RollbackApplyGate.ALLOWED_WITH_WARNING,
            RollbackIntegrity.MISSING_MAIN: RollbackApplyGate.REJECTED,
            RollbackIntegrity.MISSING_INTERNAL: RollbackApplyGate.REJECTED,
            RollbackIntegrity.CONTAINS_VOLUME: RollbackApplyGate.REJECTED,
            RollbackIntegrity.HASH_MISMATCH: RollbackApplyGate.REJECTED,
            RollbackIntegrity.UNREADABLE: RollbackApplyGate.REJECTED,
        }
        for integrity, gate in expected.items():
            with self.subTest(integrity=integrity):
                point = EngineRollbackPoint(
                    directory=Path("point"),
                    origin=RollbackOrigin.ROLLBACK,
                    stamp="point",
                    installed_at=None,
                    archive_name=None,
                    archive_sha256=None,
                    main_exe_sha256=None,
                    main_exe_bytes=None,
                    internal_file_count=None,
                    has_manifest=False,
                    integrity=integrity,
                    reject_reason="reason",
                )
                decision = rollback_can_apply(point)
                self.assertEqual(decision.gate, gate)
                self.assertEqual(
                    decision.requires_second_confirm,
                    gate is RollbackApplyGate.ALLOWED_WITH_WARNING,
                )

    def test_list_scans_rollback_and_superseded_sorted_across_origins(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            rollback_root = paths.updates / "EngineRollback"
            superseded_root = paths.updates / "EngineSuperseded"
            write_point(rollback_root, "2026-09-06_10-00-00-000000", b"old")
            write_point(
                superseded_root,
                "2026-09-06_11-00-00-000000",
                b"replaced",
                manifest_name="rollback-manifest.json",
            )
            points = service.list_rollback_points()
            self.assertEqual([point.origin.value for point in points], ["SUPERSEDED", "ROLLBACK"])
            self.assertEqual(points[0].archive_name, "engine-package.zip")

    def test_list_exposes_observed_main_hash_without_a_manifest(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            payload = b"unverified-engine"
            write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                payload,
                manifest_name=None,
            )
            service = EngineUpdateService(paths, BackupService(paths))

            point = service.list_rollback_points()[0]

            self.assertEqual(point.integrity, RollbackIntegrity.NO_MANIFEST)
            self.assertEqual(
                point.observed_main_sha256,
                hashlib.sha256(payload).hexdigest(),
            )

    def test_rejected_point_still_exposes_observed_main_hash(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            payload = b"incomplete-engine"
            point_dir = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                payload,
                expected_sha="a" * 64,
            )
            shutil.rmtree(point_dir / "_internal")
            service = EngineUpdateService(paths, BackupService(paths))

            point = service.list_rollback_points()[0]

            self.assertEqual(point.integrity, RollbackIntegrity.MISSING_INTERNAL)
            self.assertEqual(point.main_exe_sha256, "a" * 64)
            self.assertEqual(
                point.observed_main_sha256,
                hashlib.sha256(payload).hexdigest(),
            )

    def test_note_persists_across_refresh_and_restart_without_touching_manifest(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            stamp = "2026-09-06_10-00-00-000000"
            point_dir = write_point(
                paths.updates / "EngineRollback",
                stamp,
                b"old",
            )
            superseded_dir = write_point(
                paths.updates / "EngineSuperseded",
                stamp,
                b"newer",
                manifest_name="rollback-manifest.json",
            )
            manifest = point_dir / "update-manifest.json"
            manifest_before = manifest.read_bytes()
            superseded_manifest = superseded_dir / "rollback-manifest.json"
            superseded_manifest_before = superseded_manifest.read_bytes()
            service = EngineUpdateService(paths, BackupService(paths))

            self.assertEqual(service.set_rollback_note(point_dir, "  first note  "), "first note")
            self.assertEqual(
                service.set_rollback_note(superseded_dir, "newer note"),
                "newer note",
            )
            notes_by_origin = {
                point.origin: point.note for point in service.list_rollback_points()
            }
            self.assertEqual(notes_by_origin[RollbackOrigin.ROLLBACK], "first note")
            self.assertEqual(notes_by_origin[RollbackOrigin.SUPERSEDED], "newer note")

            restarted = EngineUpdateService(paths, BackupService(paths))
            notes_by_origin = {
                point.origin: point.note for point in restarted.list_rollback_points()
            }
            self.assertEqual(notes_by_origin[RollbackOrigin.ROLLBACK], "first note")
            self.assertEqual(notes_by_origin[RollbackOrigin.SUPERSEDED], "newer note")
            self.assertEqual(restarted.set_rollback_note(point_dir, "updated note"), "updated note")
            notes_by_origin = {
                point.origin: point.note for point in restarted.list_rollback_points()
            }
            self.assertEqual(notes_by_origin[RollbackOrigin.ROLLBACK], "updated note")
            self.assertEqual(notes_by_origin[RollbackOrigin.SUPERSEDED], "newer note")
            self.assertEqual(manifest.read_bytes(), manifest_before)
            self.assertEqual(superseded_manifest.read_bytes(), superseded_manifest_before)

            self.assertEqual(restarted.set_rollback_note(point_dir, "   "), "")
            notes_by_origin = {
                point.origin: point.note for point in restarted.list_rollback_points()
            }
            self.assertEqual(notes_by_origin[RollbackOrigin.ROLLBACK], "")
            self.assertEqual(notes_by_origin[RollbackOrigin.SUPERSEDED], "newer note")
            notes = json.loads(restarted.rollback_notes_path.read_text(encoding="utf-8"))
            self.assertEqual(
                notes,
                {
                    "schema": 1,
                    "notes": {f"SUPERSEDED:{stamp}": "newer note"},
                },
            )
            self.assertEqual(manifest.read_bytes(), manifest_before)
            self.assertEqual(superseded_manifest.read_bytes(), superseded_manifest_before)

    def test_invalid_note_store_is_not_overwritten(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            point_dir = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
            )
            service = EngineUpdateService(paths, BackupService(paths))
            invalid_document = b"{not valid json"
            service.rollback_notes_path.write_bytes(invalid_document)

            with self.assertRaisesRegex(
                EngineUpdateError,
                "回退点备注记录无法读取",
            ):
                service.set_rollback_note(point_dir, "must not replace the file")

            self.assertEqual(service.rollback_notes_path.read_bytes(), invalid_document)

    def test_all_rejected_integrity_states_are_classified_without_paths(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            root = paths.updates / "EngineRollback"
            missing_main = root / "2026-09-06_10-00-00-000000"
            (missing_main / "_internal").mkdir(parents=True)
            missing_internal = root / "2026-09-06_10-00-01-000000"
            missing_internal.mkdir(parents=True)
            (missing_internal / "main.exe").write_bytes(b"old")
            with_volume = write_point(
                root,
                "2026-09-06_10-00-02-000000",
                b"old",
                include_volume=True,
            )
            mismatch = write_point(
                root,
                "2026-09-06_10-00-03-000000",
                b"old",
                expected_sha="0" * 64,
            )
            unreadable = write_point(
                root,
                "2026-09-06_10-00-04-000000",
                b"old",
            )
            real_sha = service._sha256_file_with_context

            def fail_one(path, *, context):
                if path == unreadable / "main.exe":
                    raise OSError("secret-path")
                return real_sha(path, context=context)

            with patch.object(
                service,
                "_sha256_file_with_context",
                side_effect=fail_one,
            ):
                points = {point.stamp: point for point in service.list_rollback_points()}
            expected = {
                missing_main.name: RollbackIntegrity.MISSING_MAIN,
                missing_internal.name: RollbackIntegrity.MISSING_INTERNAL,
                with_volume.name: RollbackIntegrity.CONTAINS_VOLUME,
                mismatch.name: RollbackIntegrity.HASH_MISMATCH,
                unreadable.name: RollbackIntegrity.UNREADABLE,
            }
            for stamp, integrity in expected.items():
                with self.subTest(stamp=stamp):
                    point = points[stamp]
                    self.assertEqual(point.integrity, integrity)
                    self.assertNotIn("\\", point.reject_reason)
                    self.assertNotIn("/", point.reject_reason)
                    self.assertNotIn(str(base), point.reject_reason)

    def test_malformed_json_manifest_is_unreadable_and_rejected(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
            )
            (point / "update-manifest.json").write_text("{bad", encoding="utf-8")
            inspected = service.list_rollback_points()[0]
            self.assertEqual(inspected.integrity, RollbackIntegrity.UNREADABLE)
            self.assertTrue(inspected.has_manifest)
            self.assertEqual(
                rollback_can_apply(inspected).gate,
                RollbackApplyGate.REJECTED,
            )

    def test_non_object_manifest_is_unreadable_and_rejected(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
            )
            (point / "update-manifest.json").write_text("[]", encoding="utf-8")
            inspected = service.list_rollback_points()[0]
            self.assertEqual(inspected.integrity, RollbackIntegrity.UNREADABLE)
            self.assertEqual(
                rollback_can_apply(inspected).gate,
                RollbackApplyGate.REJECTED,
            )

    def test_non_utf8_manifest_is_unreadable_and_rejected(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
            )
            (point / "update-manifest.json").write_bytes(b"\xff\xfe")
            inspected = service.list_rollback_points()[0]
            self.assertEqual(inspected.integrity, RollbackIntegrity.UNREADABLE)
            self.assertEqual(
                rollback_can_apply(inspected).gate,
                RollbackApplyGate.REJECTED,
            )

    def test_each_origin_requires_its_manifest_main_hash(self) -> None:
        cases = (
            (
                "EngineRollback",
                "update-manifest.json",
                "old_main_sha256",
                RollbackOrigin.ROLLBACK,
            ),
            (
                "EngineSuperseded",
                "rollback-manifest.json",
                "replaced_main_sha256",
                RollbackOrigin.SUPERSEDED,
            ),
        )
        for root_name, manifest_name, required_key, origin in cases:
            with self.subTest(origin=origin):
                with self.temporary_directory() as directory:
                    base = Path(directory)
                    paths = make_paths(base)
                    service = EngineUpdateService(paths, BackupService(paths))
                    point = write_point(
                        paths.updates / root_name,
                        "2026-09-06_10-00-00-000000",
                        b"old",
                        manifest_name=manifest_name,
                    )
                    manifest_path = point / manifest_name
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest.pop(required_key)
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    inspected = service.list_rollback_points()[0]
                    self.assertIs(inspected.origin, origin)
                    self.assertEqual(
                        inspected.integrity,
                        RollbackIntegrity.UNREADABLE,
                    )
                    self.assertEqual(
                        rollback_can_apply(inspected).gate,
                        RollbackApplyGate.REJECTED,
                    )

    def test_invalid_required_manifest_hash_is_unreadable_and_rejected(self) -> None:
        invalid_values = (
            None,
            "",
            "not-a-sha256",
            "g" * 64,
            "0x" + "0" * 62,
            "+" + "0" * 63,
            123,
        )
        for index, value in enumerate(invalid_values):
            with self.subTest(value=value):
                with self.temporary_directory() as directory:
                    base = Path(directory)
                    paths = make_paths(base)
                    service = EngineUpdateService(paths, BackupService(paths))
                    point = write_point(
                        paths.updates / "EngineRollback",
                        f"2026-09-06_10-00-{index:02d}-000000",
                        b"old",
                    )
                    manifest_path = point / "update-manifest.json"
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    manifest["old_main_sha256"] = value
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    inspected = service.list_rollback_points()[0]
                    self.assertEqual(
                        inspected.integrity,
                        RollbackIntegrity.UNREADABLE,
                    )
                    self.assertEqual(
                        rollback_can_apply(inspected).gate,
                        RollbackApplyGate.REJECTED,
                    )

    def test_unreadable_manifest_is_hard_rejected(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
            )
            service = EngineUpdateService(paths, BackupService(paths))
            with patch(
                "douk_manager.core.engine_update.json.loads",
                side_effect=OSError("synthetic unreadable manifest"),
            ):
                inspected = service.list_rollback_points()[0]
            self.assertEqual(inspected.integrity, RollbackIntegrity.UNREADABLE)
            self.assertEqual(
                rollback_can_apply(inspected).gate,
                RollbackApplyGate.REJECTED,
            )

    def test_preview_returns_rejected_point_without_writing(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            point = paths.updates / "EngineRollback" / "2026-09-06_10-00-00-000000"
            point.mkdir(parents=True)
            before = tuple(sorted((path.relative_to(point).as_posix(), path.stat().st_size) for path in point.rglob("*")))
            preview = service.preview_rollback(point)
            after = tuple(sorted((path.relative_to(point).as_posix(), path.stat().st_size) for path in point.rglob("*")))
            self.assertEqual(preview.point.integrity, RollbackIntegrity.MISSING_MAIN)
            self.assertIn("main.exe", preview.point.reject_reason)
            self.assertEqual(before, after)

    def test_apply_rejects_missing_second_confirmation_without_moves(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            point = write_point(paths.updates / "EngineRollback", "2026-09-06_10-00-00-000000", b"old", manifest_name=None)
            with patch("douk_manager.core.engine_update.shutil.move") as move:
                with self.assertRaisesRegex(RuntimeError, "缺少二次确认"):
                    service.apply_rollback(point)
            move.assert_not_called()

    def test_apply_preserves_volume_and_sidecar_and_creates_superseded(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            sidecar = paths.engine_root / "encipher.py"
            sidecar.write_bytes(b"stable-sidecar")
            sidecar_hash = sha256_file(sidecar)
            sidecar_mtime = sidecar.stat().st_mtime_ns
            volume_hashes = {name: sha256_file(paths.volume / name) for name in ("settings_master.json", "settings.json", "DouK-Downloader.db")}
            volume_mtimes = {
                name: (paths.volume / name).stat().st_mtime_ns
                for name in EngineUpdateService.CRITICAL_NAMES
            }
            service = EngineUpdateService(paths, BackupService(paths))
            payload = b"rollback-engine"
            point = write_point(paths.updates / "EngineRollback", "2026-09-06_10-00-00-000000", payload)
            result = service.apply_rollback(point)
            self.assertEqual(paths.engine_exe.read_bytes(), payload)
            self.assertEqual(sha256_file(sidecar), sidecar_hash)
            self.assertEqual(sidecar.stat().st_mtime_ns, sidecar_mtime)
            self.assertEqual({name: sha256_file(paths.volume / name) for name in volume_hashes}, volume_hashes)
            self.assertEqual(
                {
                    name: (paths.volume / name).stat().st_mtime_ns
                    for name in EngineUpdateService.CRITICAL_NAMES
                },
                volume_mtimes,
            )
            self.assertFalse((point / "main.exe").exists())
            self.assertFalse((point / "_internal").exists())
            self.assertTrue(result.superseded_path.exists())
            self.assertTrue((result.superseded_path / "main.exe").is_file())
            self.assertFalse((result.superseded_path / "_internal" / "Volume").exists())
            self.assertEqual(result.source_had_manifest, True)
            self.assertTrue(result.manifest_verified)

    def test_no_manifest_success_records_observed_values_honestly(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            payload = b"unverified-engine"
            point = write_point(paths.updates / "EngineRollback", "2026-09-06_10-00-00-000000", payload, manifest_name=None)
            inspected = service.list_rollback_points()[0]
            self.assertEqual(inspected.integrity, RollbackIntegrity.NO_MANIFEST)
            self.assertEqual(
                rollback_can_apply(inspected).gate,
                RollbackApplyGate.ALLOWED_WITH_WARNING,
            )
            result = service.apply_rollback(point, accept_unverified=True)
            self.assertFalse(result.source_had_manifest)
            self.assertFalse(result.manifest_verified)
            self.assertEqual(result.observed_main_sha256, hashlib.sha256(payload).hexdigest())
            self.assertEqual(result.observed_main_bytes, len(payload))
            text = (result.superseded_path / "rollback-manifest.json").read_text(encoding="utf-8")
            self.assertNotIn("校验通过", text)
            self.assertNotIn("已验证", text)

    def test_existing_invalid_manifest_cannot_be_forced_or_start_transaction(self) -> None:
        cases = (
            ("EngineRollback", "update-manifest.json"),
            ("EngineSuperseded", "rollback-manifest.json"),
        )
        for root_name, manifest_name in cases:
            with self.subTest(root_name=root_name):
                with self.temporary_directory() as directory:
                    base = Path(directory)
                    paths = make_paths(base)
                    point = write_point(
                        paths.updates / root_name,
                        "2026-09-06_10-00-00-000000",
                        b"invalid-manifest-engine",
                        manifest_name=manifest_name,
                    )
                    (point / manifest_name).write_text("{invalid", encoding="utf-8")
                    backup = BackupService(paths)
                    service = EngineUpdateService(paths, backup)
                    original_engine = paths.engine_exe.read_bytes()
                    with (
                        patch.object(backup, "create_full_snapshot") as create_backup,
                        patch("douk_manager.core.engine_update.shutil.move") as move,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "无法读取"):
                            service.apply_rollback(point, accept_unverified=True)
                    create_backup.assert_not_called()
                    move.assert_not_called()
                    self.assertEqual(paths.engine_exe.read_bytes(), original_engine)
                    self.assertTrue(point.joinpath("main.exe").is_file())
                    self.assertTrue(point.joinpath("_internal").is_dir())

    def test_no_manifest_post_move_hash_mismatch_restores_engine_volume_and_source(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            payload = b"unverified-engine"
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                payload,
                manifest_name=None,
            )
            service = EngineUpdateService(paths, BackupService(paths))
            original_engine = paths.engine_exe.read_bytes()
            volume_hashes = {
                name: sha256_file(paths.volume / name)
                for name in EngineUpdateService.CRITICAL_NAMES
            }
            real_move = shutil.move

            def move_then_corrupt(source, destination, *args, **kwargs):
                result = real_move(source, destination, *args, **kwargs)
                if Path(source) == point / "main.exe":
                    self.assertEqual(Path(destination), paths.engine_exe)
                    paths.engine_exe.write_bytes(b"changed-after-move")
                return result

            with patch(
                "douk_manager.core.engine_update.shutil.move",
                side_effect=move_then_corrupt,
            ):
                with self.assertRaisesRegex(RuntimeError, "哈希校验失败"):
                    service.apply_rollback(point, accept_unverified=True)

            self.assertEqual(paths.engine_exe.read_bytes(), original_engine)
            self.assertEqual(
                {
                    name: sha256_file(paths.volume / name)
                    for name in EngineUpdateService.CRITICAL_NAMES
                },
                volume_hashes,
            )
            self.assertEqual(point.joinpath("main.exe").read_bytes(), payload)
            self.assertTrue(point.joinpath("_internal").is_dir())

    def test_superseded_engine_is_listed_and_can_be_used_as_next_source(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"first-engine",
            )
            service.apply_rollback(point)
            superseded = [
                item
                for item in service.list_rollback_points()
                if item.origin is RollbackOrigin.SUPERSEDED
            ]
            self.assertEqual(len(superseded), 1)
            service.apply_rollback(superseded[0].directory)
            self.assertEqual(paths.engine_exe.read_bytes(), b"current-engine")
            self.assertEqual(
                len(list((paths.updates / "EngineSuperseded").iterdir())),
                2,
            )

    def test_rollback_never_prunes_other_engine_history(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            rollback_root = paths.updates / "EngineRollback"
            superseded_root = paths.updates / "EngineSuperseded"
            rollback_points = [
                write_point(
                    rollback_root,
                    f"2026-09-06_10-{index:02d}-00-000000",
                    f"rollback-{index}".encode(),
                )
                for index in range(10)
            ]
            original_superseded = [
                write_point(
                    superseded_root,
                    f"2026-09-05_10-{index:02d}-00-000000",
                    f"superseded-{index}".encode(),
                    manifest_name="rollback-manifest.json",
                )
                for index in range(10)
            ]
            service = EngineUpdateService(paths, BackupService(paths))
            service.apply_rollback(rollback_points[-1])
            self.assertEqual(
                sum(
                    (point / "main.exe").is_file()
                    and (point / "_internal").is_dir()
                    for point in rollback_points
                ),
                9,
            )
            self.assertTrue(
                all(
                    point.joinpath("main.exe").is_file()
                    and point.joinpath("_internal").is_dir()
                    for point in original_superseded
                )
            )
            active_superseded = [
                point
                for point in superseded_root.iterdir()
                if point.joinpath("main.exe").is_file()
                and point.joinpath("_internal").is_dir()
            ]
            self.assertEqual(len(active_superseded), 11)

    def test_rejected_point_cannot_be_forced_with_confirmation(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
                include_volume=True,
            )
            with self.assertRaisesRegex(RuntimeError, "正式 Volume"):
                service.apply_rollback(point, accept_unverified=True)
            self.assertEqual(paths.engine_exe.read_bytes(), b"current-engine")
            self.assertTrue(point.exists())

    def test_apply_failure_restores_engine_volume_and_point(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
            )
            original = paths.engine_exe.read_bytes()
            with patch.object(
                service,
                "_verify_after_rollback",
                side_effect=RuntimeError("verify failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "自动恢复"):
                    service.apply_rollback(point)
            self.assertEqual(paths.engine_exe.read_bytes(), original)
            self.assertTrue((paths.engine_root / "_internal" / "Volume").is_dir())
            self.assertTrue((point / "main.exe").is_file())
            self.assertTrue((point / "_internal").is_dir())

    def test_move_failures_restore_engine_volume_and_candidate(self) -> None:
        for failing_move in (1, 2, 3, 4, 5):
            with self.subTest(failing_move=failing_move):
                with self.temporary_directory() as directory:
                    base = Path(directory)
                    paths = make_paths(base)
                    point = write_point(
                        paths.updates / "EngineRollback",
                        "2026-09-06_10-00-00-000000",
                        b"old",
                    )
                    original_engine = paths.engine_exe.read_bytes()
                    volume_hashes = {
                        name: sha256_file(paths.volume / name)
                        for name in EngineUpdateService.CRITICAL_NAMES
                    }
                    real_move = shutil.move
                    calls = 0

                    def fail_once(source, destination, *args, **kwargs):
                        nonlocal calls
                        calls += 1
                        if calls == failing_move:
                            raise OSError("synthetic move failure")
                        return real_move(source, destination, *args, **kwargs)

                    service = EngineUpdateService(paths, BackupService(paths))
                    with patch(
                        "douk_manager.core.engine_update.shutil.move",
                        side_effect=fail_once,
                    ):
                        with self.assertRaisesRegex(RuntimeError, "自动恢复"):
                            service.apply_rollback(point)

                    self.assertEqual(paths.engine_exe.read_bytes(), original_engine)
                    self.assertEqual(
                        {
                            name: sha256_file(paths.volume / name)
                            for name in EngineUpdateService.CRITICAL_NAMES
                        },
                        volume_hashes,
                    )
                    self.assertTrue(point.joinpath("main.exe").is_file())
                    self.assertTrue(point.joinpath("_internal").is_dir())

    def test_sqlite_verification_failure_restores_engine_and_volume(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
            )
            original_engine = paths.engine_exe.read_bytes()
            volume_hashes = {
                name: sha256_file(paths.volume / name)
                for name in EngineUpdateService.CRITICAL_NAMES
            }
            service = EngineUpdateService(paths, BackupService(paths))
            with patch(
                "douk_manager.core.engine_update.sqlite_quick_check",
                side_effect=RuntimeError("synthetic sqlite failure"),
            ):
                with self.assertRaisesRegex(RuntimeError, "自动恢复"):
                    service.apply_rollback(point)
            self.assertEqual(paths.engine_exe.read_bytes(), original_engine)
            self.assertEqual(
                {
                    name: sha256_file(paths.volume / name)
                    for name in EngineUpdateService.CRITICAL_NAMES
                },
                volume_hashes,
            )
            self.assertTrue(point.joinpath("main.exe").is_file())
            self.assertTrue(point.joinpath("_internal").is_dir())

    def test_recovery_failure_reports_backup_and_superseded_paths(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
            )
            service = EngineUpdateService(paths, BackupService(paths))
            with (
                patch.object(
                    service,
                    "_verify_after_rollback",
                    side_effect=RuntimeError("synthetic verification failure"),
                ),
                patch.object(
                    service,
                    "_restore_failed_rollback",
                    return_value="synthetic recovery failure",
                ),
            ):
                with self.assertRaisesRegex(
                    RuntimeError,
                    "自动恢复也未完整完成",
                ) as raised:
                    service.apply_rollback(point)
            message = str(raised.exception)
            self.assertIn("BeforeEngineRollback", message)
            self.assertIn("EngineSuperseded", message)

    def test_volume_guard_refuses_to_delete_internal_containing_volume(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            superseded = paths.updates / "EngineSuperseded" / "synthetic"
            (superseded / "_internal" / "Volume").mkdir(parents=True)
            (paths.engine_root / "_internal" / "extra").write_bytes(b"candidate")
            error = service._restore_failed_rollback(
                superseded,
                paths.updates / "EngineRollback" / "point",
            )
            self.assertIn("拒绝删除", error)
            self.assertTrue((paths.engine_root / "_internal" / "Volume").is_dir())

    def test_sidecar_in_source_point_is_never_moved_over_live_sidecar(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            live_sidecar = paths.engine_root / "encipher.py"
            live_sidecar.write_bytes(b"live")
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
            )
            (point / "encipher.py").write_bytes(b"candidate")
            service = EngineUpdateService(paths, BackupService(paths))
            service.apply_rollback(point)
            self.assertEqual(live_sidecar.read_bytes(), b"live")
            self.assertFalse(
                any(path.name == "encipher.py" for path in (paths.updates / "EngineSuperseded").rglob("*"))
            )

    def test_measure_usage_is_read_only(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            for root, prefix in ((paths.updates / "EngineRollback", "r"), (paths.updates / "EngineSuperseded", "s")):
                for index in range(3):
                    write_point(root, f"2026-09-06_10-0{index}-00-000000", (prefix * (index + 1)).encode())
            usage = service.measure_rollback_usage()
            self.assertEqual((usage.rollback_count, usage.superseded_count), (3, 3))
            self.assertGreater(usage.total_bytes, 0)

    def test_measure_usage_ignores_consumed_manifest_only_points(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
            )
            service.apply_rollback(point)
            usage = service.measure_rollback_usage()
            self.assertEqual(usage.rollback_count, 0)
            self.assertEqual(usage.superseded_count, 1)

    def test_measure_usage_cancellation_is_read_only(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
            )
            before = tuple(
                sorted(
                    path.relative_to(paths.updates).as_posix()
                    for path in paths.updates.rglob("*")
                )
            )
            context = OperationContext()
            context.request_cancel()
            with self.assertRaises(TaskCancelled):
                service.measure_rollback_usage(context=context)
            after = tuple(
                sorted(
                    path.relative_to(paths.updates).as_posix()
                    for path in paths.updates.rglob("*")
                )
            )
            self.assertEqual(before, after)

    def test_cancel_after_backup_completes_backup_without_formal_moves(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
            )
            backup = BackupService(paths)
            service = EngineUpdateService(paths, backup)
            context = OperationContext()
            real_snapshot = backup.create_full_snapshot

            def snapshot_then_cancel(*args, **kwargs):
                result = real_snapshot(*args, **kwargs)
                self.assertTrue(context.request_cancel())
                return result

            with patch.object(
                backup,
                "create_full_snapshot",
                side_effect=snapshot_then_cancel,
            ), patch("douk_manager.core.engine_update.shutil.move") as move:
                with self.assertRaises(TaskCancelled):
                    service.apply_rollback(point, context=context)
            move.assert_not_called()
            self.assertTrue(any((paths.backups / "BeforeEngineRollback").iterdir()))
            self.assertFalse(context.critical_to_completion)

    def test_service_rechecks_processes_immediately_before_critical_phase(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
            )
            ensure_idle = Mock(side_effect=ControllerError("synthetic process appeared"))
            service = EngineUpdateService(
                paths,
                BackupService(paths),
                process_guard=ensure_idle,
            )
            with patch("douk_manager.core.engine_update.shutil.move") as move:
                with self.assertRaisesRegex(ControllerError, "process appeared"):
                    service.apply_rollback(point)
            ensure_idle.assert_called_once_with()
            move.assert_not_called()

    def test_late_cancellation_after_critical_phase_does_not_interrupt_rollback(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            point = write_point(
                paths.updates / "EngineRollback",
                "2026-09-06_10-00-00-000000",
                b"old",
            )
            service = EngineUpdateService(paths, BackupService(paths))
            context = OperationContext()
            real_verify = service._verify_after_rollback

            def verify(*args, **kwargs):
                self.assertTrue(context.critical_to_completion)
                self.assertFalse(context.request_cancel())
                return real_verify(*args, **kwargs)

            with patch.object(service, "_verify_after_rollback", side_effect=verify):
                result = service.apply_rollback(point, context=context)
            self.assertEqual(paths.engine_exe.read_bytes(), b"old")
            self.assertEqual(result.observed_main_bytes, len(b"old"))

    def test_engine_update_preserves_existing_sidecar(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            sidecar = paths.engine_root / "encipher.py"
            sidecar.write_bytes(b"stable-sidecar")
            sidecar_hash = sha256_file(sidecar)
            sidecar_mtime = sidecar.stat().st_mtime_ns
            archive = base / "engine.zip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
                handle.writestr("main.exe", b"new-engine")
                handle.writestr("_internal/runtime.dll", b"runtime")
                handle.writestr("_internal/Volume/settings.json", b"packaged")
            service = EngineUpdateService(paths, BackupService(paths))
            service.apply(archive)
            self.assertEqual(sha256_file(sidecar), sidecar_hash)
            self.assertEqual(sidecar.stat().st_mtime_ns, sidecar_mtime)

    def test_cancel_before_apply_does_not_move_files(self) -> None:
        with self.temporary_directory() as directory:
            base = Path(directory)
            paths = make_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))
            point = write_point(paths.updates / "EngineRollback", "2026-09-06_10-00-00-000000", b"old")
            context = OperationContext()
            context.request_cancel()
            with self.assertRaises(TaskCancelled):
                service.apply_rollback(point, context=context)
            self.assertEqual(paths.engine_exe.read_bytes(), b"current-engine")
            self.assertTrue(point.exists())

    def test_controller_forwards_rollback_operations_and_rechecks_processes(self) -> None:
        controller = ManagerController.__new__(ManagerController)
        controller.require_operational_ready = Mock()
        controller.require_safe_write = Mock()
        controller.engine = Mock(external_running=Mock(side_effect=(False, True)))
        controller.collector = Mock(running=False)
        controller.collector.health.return_value = False
        controller.engine_updates = Mock()
        controller.engine_updates.list_rollback_points.return_value = ()
        self.assertEqual(controller.list_engine_rollbacks(), ())
        controller.engine_updates.list_rollback_points.assert_called_once_with(context=None)
        with self.assertRaises(ControllerError):
            controller.apply_engine_rollback(Path("point"))
        controller.engine_updates.apply_rollback.assert_not_called()

    def test_controller_saves_rollback_note_after_ready_gate(self) -> None:
        controller = ManagerController.__new__(ManagerController)
        controller.require_operational_ready = Mock()
        controller.engine_updates = Mock()
        controller.engine_updates.set_rollback_note.return_value = "saved note"
        point = Path("synthetic") / "EngineRollback" / "point"

        self.assertEqual(
            controller.save_engine_rollback_note(point, " saved note "),
            "saved note",
        )

        controller.require_operational_ready.assert_called_once_with(
            "保存下载引擎回退点备注"
        )
        controller.engine_updates.set_rollback_note.assert_called_once_with(
            point,
            " saved note ",
        )


class EngineRollbackGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _button() -> SimpleNamespace:
        return SimpleNamespace(setEnabled=Mock())

    def test_refresh_lists_first_then_submits_usage_exactly_once(self) -> None:
        point = rollback_point(RollbackIntegrity.OK)
        controller = SimpleNamespace(
            startup_state=StartupState.READY,
            list_engine_rollbacks=Mock(return_value=(point,)),
        )
        window = SimpleNamespace(
            controller=controller,
            settings_output=object(),
            engine_rollback_refresh_button=self._button(),
            engine_rollback_usage_label=SimpleNamespace(setText=Mock()),
            _engine_rollback_usage_pending=False,
            _render_engine_rollback_points=Mock(),
            _submit_engine_rollback_usage=Mock(),
            _submit_background=Mock(return_value="list-task"),
        )
        MainWindow._refresh_engine_rollbacks(window)
        submitted = window._submit_background.call_args
        spec, action = submitted.args
        self.assertEqual(spec.task_type, "engine_rollback_list")
        self.assertEqual(spec.resource_keys, frozenset({"engine_files"}))
        self.assertEqual(spec.deduplicate_key, "engine_rollback_list")
        self.assertTrue(spec.dynamic_cancellation)
        self.assertIs(spec.close_policy, ClosePolicy.CANCEL)
        context = Mock()
        self.assertEqual(action(context), (point,))
        controller.list_engine_rollbacks.assert_called_once_with(context=context)

        submitted.kwargs["on_success"]((point,))
        window._render_engine_rollback_points.assert_called_once_with((point,))
        window._submit_engine_rollback_usage.assert_not_called()
        submitted.kwargs["on_removed"]()
        submitted.kwargs["on_removed"]()
        window._submit_engine_rollback_usage.assert_called_once_with()

    def test_table_renders_full_hash_and_persistent_editable_note_columns(self) -> None:
        observed_hash = "c" * 64
        point = replace(
            rollback_point(RollbackIntegrity.OK),
            observed_main_sha256=observed_hash,
            note="known-good",
        )
        table = QTableWidget(0, 7)
        window = SimpleNamespace(
            _engine_rollback_points=(),
            engine_rollback_table=table,
            _update_engine_rollback_buttons=Mock(),
        )

        unobserved = rollback_point(RollbackIntegrity.UNREADABLE)
        MainWindow._render_engine_rollback_points(window, (point, unobserved))

        self.assertEqual(table.item(0, 4).text(), observed_hash)
        self.assertEqual(table.item(0, 5).text(), "known-good")
        self.assertEqual(table.item(1, 4).text(), "不可用")
        self.assertTrue(table.item(0, 5).flags() & Qt.ItemFlag.ItemIsEditable)
        for column in (0, 1, 2, 3, 4, 6):
            self.assertFalse(table.item(0, column).flags() & Qt.ItemFlag.ItemIsEditable)

    def test_editing_note_saves_it_and_updates_the_rendered_point(self) -> None:
        point = replace(rollback_point(RollbackIntegrity.OK), note="old note")
        table = QTableWidget(1, 7)
        note_item = table.item(0, 5)
        if note_item is None:
            note_item = QTableWidgetItem("new note")
            table.setItem(0, 5, note_item)
        controller = SimpleNamespace(
            save_engine_rollback_note=Mock(return_value="new note")
        )
        window = SimpleNamespace(
            controller=controller,
            settings_output=object(),
            engine_rollback_table=table,
            _engine_rollback_points=(point,),
            _replace_info=Mock(),
            statusBar=Mock(return_value=SimpleNamespace(showMessage=Mock())),
        )

        MainWindow._save_engine_rollback_note(window, note_item)

        controller.save_engine_rollback_note.assert_called_once_with(
            point.directory,
            "new note",
        )
        self.assertEqual(window._engine_rollback_points[0].note, "new note")
        window._replace_info.assert_called_once_with(
            window.settings_output,
            "回退点备注已保存。",
        )

    def test_rollback_table_clears_visual_selection_after_focus_moves_out(self) -> None:
        host = QWidget()
        layout = QVBoxLayout(host)
        table = EngineRollbackTable(1, 7, host)
        table.setItem(0, 0, QTableWidgetItem("point"))
        outside = QPushButton("outside", host)
        layout.addWidget(table)
        layout.addWidget(outside)
        host.show()
        self.app.processEvents()

        table.selectRow(0)
        table.setFocus()
        self.app.processEvents()
        self.assertEqual(table.currentRow(), 0)
        self.assertTrue(table.selectedItems())

        outside.setFocus()
        self.app.processEvents()
        self.app.processEvents()

        self.assertEqual(table.currentRow(), -1)
        self.assertEqual(table.selectedItems(), [])
        host.close()
        host.deleteLater()
        self.app.processEvents()

    def test_note_save_failure_restores_rendered_value(self) -> None:
        point = replace(rollback_point(RollbackIntegrity.OK), note="old note")
        table = QTableWidget(1, 7)
        note_item = QTableWidgetItem("unsaved note")
        table.setItem(0, 5, note_item)
        controller = SimpleNamespace(
            save_engine_rollback_note=Mock(
                side_effect=ControllerError("synthetic save failure")
            )
        )
        status_bar = SimpleNamespace(showMessage=Mock())
        window = SimpleNamespace(
            controller=controller,
            settings_output=object(),
            engine_rollback_table=table,
            _engine_rollback_points=(point,),
            _replace_info=Mock(),
            statusBar=Mock(return_value=status_bar),
        )

        MainWindow._save_engine_rollback_note(window, note_item)

        self.assertEqual(note_item.text(), "old note")
        self.assertEqual(window._engine_rollback_points, (point,))
        window._replace_info.assert_called_once_with(
            window.settings_output,
            "【备注保存失败】",
            "synthetic save failure",
        )
        status_bar.showMessage.assert_called_once_with("回退点备注保存失败")

    def test_usage_task_is_cancellable_and_reports_cancelled_state(self) -> None:
        controller = SimpleNamespace(
            startup_state=StartupState.READY,
            measure_engine_rollback_usage=Mock(return_value=object()),
        )
        cancel_button = self._button()
        label = SimpleNamespace(setText=Mock())
        window = SimpleNamespace(
            controller=controller,
            settings_output=object(),
            engine_rollback_refresh_button=self._button(),
            engine_rollback_usage_cancel_button=cancel_button,
            engine_rollback_usage_label=label,
            _engine_rollback_usage_task_id=None,
            _submit_background=Mock(return_value="usage-task"),
            _cancel_background=Mock(return_value=True),
        )
        MainWindow._submit_engine_rollback_usage(window)
        submitted = window._submit_background.call_args
        spec, action = submitted.args
        self.assertEqual(spec.task_type, "engine_rollback_usage")
        self.assertEqual(spec.resource_keys, frozenset({"engine_files"}))
        self.assertEqual(spec.deduplicate_key, "engine_rollback_usage")
        self.assertTrue(spec.dynamic_cancellation)
        context = Mock()
        action(context)
        controller.measure_engine_rollback_usage.assert_called_once_with(
            context=context
        )
        self.assertEqual(window._engine_rollback_usage_task_id, "usage-task")

        MainWindow._cancel_engine_rollback_usage(window)
        window._cancel_background.assert_called_once_with("usage-task")
        submitted.kwargs["on_cancelled"](object())
        self.assertEqual(
            label.setText.call_args.args[0],
            "磁盘占用统计已取消（可重新刷新）",
        )
        submitted.kwargs["on_removed"]()
        self.assertIsNone(window._engine_rollback_usage_task_id)

    def test_apply_task_contract_and_closing_gate(self) -> None:
        result = SimpleNamespace(
            manifest_verified=True,
            backup_path=Path("synthetic-backup"),
            superseded_path=Path("synthetic-superseded"),
            restored_main_sha256="a" * 64,
            source_directory=Path("synthetic-source"),
        )
        controller = SimpleNamespace(
            startup_state=StartupState.READY,
            apply_engine_rollback=Mock(return_value=result),
        )
        window = SimpleNamespace(
            controller=controller,
            settings_output=object(),
            engine_rollback_apply_button=self._button(),
            _replace_info=Mock(),
            _submit_background=Mock(return_value="apply-task"),
        )
        point_dir = Path("synthetic-point")
        MainWindow._submit_engine_rollback_apply(
            window,
            point_dir,
            accept_unverified=True,
        )
        submitted = window._submit_background.call_args
        spec, action = submitted.args
        self.assertEqual(spec.task_type, "engine_rollback_apply")
        self.assertEqual(
            spec.resource_keys,
            frozenset(
                {
                    "engine_process",
                    "collector_process",
                    "engine_files",
                    "volume",
                    "settings",
                }
            ),
        )
        self.assertEqual(spec.deduplicate_key, "engine_rollback_apply")
        self.assertEqual(spec.refresh_targets, ("runtime_status",))
        context = Mock()
        self.assertIs(action(context), result)
        controller.apply_engine_rollback.assert_called_once_with(
            point_dir,
            accept_unverified=True,
            context=context,
        )

        closing = SimpleNamespace(
            controller=SimpleNamespace(startup_state=StartupState.CLOSING),
            _submit_background=Mock(),
        )
        MainWindow._submit_engine_rollback_apply(closing, point_dir)
        closing._submit_background.assert_not_called()

    def test_no_manifest_apply_requires_two_confirmations(self) -> None:
        point = rollback_point(RollbackIntegrity.NO_MANIFEST)
        preview = EngineRollbackPreview(
            point=point,
            current_main_sha256="c" * 64,
            current_internal_file_count=2,
            volume_path=Path("synthetic-volume"),
            critical_files={},
            is_same_as_current=False,
        )
        window = SimpleNamespace(
            controller=SimpleNamespace(startup_state=StartupState.READY),
            settings_output=object(),
            engine_rollback_apply_button=self._button(),
            _selected_engine_rollback_point=Mock(return_value=point),
            _canonical_engine_rollback_point=(
                MainWindow._canonical_engine_rollback_point
            ),
            _replace_info=Mock(),
            _submit_coalesced_background=Mock(return_value="preview-task"),
            _submit_engine_rollback_apply=Mock(),
        )
        with patch(
            "douk_manager.gui.QMessageBox.question",
            side_effect=(QMessageBox.Yes, QMessageBox.Yes),
        ) as question:
            MainWindow._apply_engine_rollback(window)
            submitted = window._submit_coalesced_background.call_args
            submitted.kwargs["on_success"](preview)
            submitted.kwargs["on_removed"]()
        self.assertEqual(question.call_count, 2)
        window._submit_engine_rollback_apply.assert_called_once_with(
            point.directory,
            accept_unverified=True,
        )
        messages = "\n".join(call.args[2] for call in question.call_args_list)
        self.assertIn("正式 Volume 不会被替换", messages)
        self.assertIn("无法核对与已安装版本的一致性", messages)

    def test_unverified_success_text_never_claims_verification(self) -> None:
        result = SimpleNamespace(
            manifest_verified=False,
            backup_path=Path("synthetic-backup"),
            superseded_path=Path("synthetic-superseded"),
            restored_main_sha256="a" * 64,
            source_directory=Path("synthetic-source"),
        )
        window = SimpleNamespace(
            controller=SimpleNamespace(
                startup_state=StartupState.READY,
                apply_engine_rollback=Mock(return_value=result),
            ),
            settings_output=object(),
            engine_rollback_apply_button=self._button(),
            _replace_info=Mock(),
            _submit_background=Mock(return_value="apply-task"),
        )
        MainWindow._submit_engine_rollback_apply(
            window,
            Path("synthetic-point"),
            accept_unverified=True,
        )
        window._submit_background.call_args.kwargs["on_success"](result)
        text = "\n".join(window._replace_info.call_args.args[1:])
        self.assertIn("来源无清单，未与已安装版本核对", text)
        for forbidden in ("校验通过", "已验证", "完整性 OK"):
            self.assertNotIn(forbidden, text)


if __name__ == "__main__":
    unittest.main()
