from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path

from douk_manager.core.backup import BackupService, sha256_file
from douk_manager.core.engine_update import EngineUpdateError, EngineUpdateService
from tests.helpers import make_test_paths


def write_update_zip(path: Path, *, traversal: bool = False) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        handle.writestr("main.exe", b"new executable")
        handle.writestr("_internal/new-runtime.dll", b"new runtime")
        handle.writestr("_internal/Volume/settings.json", b"untrusted default volume")
        if traversal:
            handle.writestr("../outside.txt", b"blocked")


class EngineUpdateTests(unittest.TestCase):
    def test_update_replaces_code_and_preserves_unique_volume(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            old_runtime = paths.engine_root / "_internal" / "old-runtime.dll"
            old_runtime.write_bytes(b"old runtime")
            archive = base / "new-engine.zip"
            write_update_zip(archive)
            service = EngineUpdateService(paths, BackupService(paths))
            critical_before = {
                name: sha256_file(paths.volume / name)
                for name in service.CRITICAL_NAMES
            }

            preview = service.preview(archive)
            self.assertTrue(preview.contains_packaged_volume)
            result = service.apply(archive)

            self.assertEqual(paths.engine_exe.read_bytes(), b"new executable")
            self.assertTrue((paths.engine_root / "_internal" / "new-runtime.dll").is_file())
            self.assertFalse((paths.engine_root / "_internal" / "old-runtime.dll").exists())
            self.assertFalse((paths.volume / "untrusted default volume").exists())
            self.assertEqual(
                {name: sha256_file(paths.volume / name) for name in service.CRITICAL_NAMES},
                critical_before,
            )
            self.assertTrue(result.backup_path.is_dir())
            self.assertEqual(
                (result.rollback_path / "main.exe").read_bytes(), b"test executable"
            )
            self.assertTrue(
                (result.rollback_path / "_internal" / "old-runtime.dll").is_file()
            )
            self.assertFalse((result.rollback_path / "_internal" / "Volume").exists())
            self.assertTrue((result.rollback_path / "update-manifest.json").is_file())

    def test_traversal_zip_is_rejected_without_touching_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "bad-engine.zip"
            write_update_zip(archive, traversal=True)
            service = EngineUpdateService(paths, BackupService(paths))
            original_exe = hashlib.sha256(paths.engine_exe.read_bytes()).hexdigest()

            with self.assertRaises(EngineUpdateError):
                service.preview(archive)

            self.assertEqual(
                hashlib.sha256(paths.engine_exe.read_bytes()).hexdigest(), original_exe
            )
            self.assertTrue(paths.database.is_file())

    def test_missing_internal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "incomplete.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("main.exe", b"new executable")
            service = EngineUpdateService(paths, BackupService(paths))

            with self.assertRaises(EngineUpdateError):
                service.preview(archive)


if __name__ == "__main__":
    unittest.main()
