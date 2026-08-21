from __future__ import annotations

import hashlib
import queue
import shutil
import tempfile
import threading
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from douk_manager.core.backup import BackupService, sha256_file
from douk_manager.core.engine_update import EngineUpdateError, EngineUpdateService
from douk_manager.operation import OperationContext, TaskCancelled
from tests.helpers import make_test_paths


def write_update_zip(
    path: Path,
    *,
    traversal: bool = False,
    compression: int = zipfile.ZIP_DEFLATED,
    extra_members: dict[str, bytes] | None = None,
) -> None:
    with zipfile.ZipFile(path, "w", compression=compression) as handle:
        handle.writestr("main.exe", b"new executable")
        handle.writestr("_internal/new-runtime.dll", b"new runtime")
        handle.writestr("_internal/Volume/settings.json", b"untrusted default volume")
        if traversal:
            handle.writestr("../outside.txt", b"blocked")
        for name, payload in (extra_members or {}).items():
            handle.writestr(name, payload)


def corrupt_stored_member(path: Path, member: str) -> None:
    with zipfile.ZipFile(path) as handle:
        info = handle.getinfo(member)
    with path.open("r+b") as handle:
        handle.seek(info.header_offset)
        header = handle.read(30)
        name_length = int.from_bytes(header[26:28], "little")
        extra_length = int.from_bytes(header[28:30], "little")
        data_offset = info.header_offset + 30 + name_length + extra_length
        handle.seek(data_offset)
        original = handle.read(1)
        if not original:
            raise AssertionError(f"stored ZIP member has no payload: {member}")
        handle.seek(data_offset)
        handle.write(bytes((original[0] ^ 0xFF,)))


class DeterministicBoundary:
    TIMEOUT_SECONDS = 5

    def __init__(self) -> None:
        self.signals: queue.Queue[tuple[str, object]] = queue.Queue()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self._used = False

    def wait(self) -> None:
        with self._lock:
            if self._used:
                return
            self._used = True
        self.signals.put(("boundary", None))
        if not self.release.wait(timeout=self.TIMEOUT_SECONDS):
            raise AssertionError("test did not release deterministic boundary")


class GatedReader:
    def __init__(self, handle, boundary: DeterministicBoundary) -> None:
        self._handle = handle
        self._boundary = boundary
        self.chunk_reads = 0

    def read(self, size: int = -1):
        payload = self._handle.read(size)
        if size > 0 and payload:
            self.chunk_reads += 1
            if self.chunk_reads == 2:
                self._boundary.wait()
        return payload

    def __enter__(self):
        self._handle.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return self._handle.__exit__(exc_type, exc_value, traceback)

    def __getattr__(self, name: str):
        return getattr(self._handle, name)


class EngineUpdateTests(unittest.TestCase):
    def _run_at_boundary(
        self,
        operation,
        boundary: DeterministicBoundary,
        boundary_action,
    ):
        def run() -> None:
            try:
                result = operation()
            except BaseException as exc:
                boundary.signals.put(("outcome", (False, exc)))
            else:
                boundary.signals.put(("outcome", (True, result)))

        worker = threading.Thread(target=run, name="engine-update-contract-test")
        worker.start()
        try:
            try:
                signal, payload = boundary.signals.get(
                    timeout=boundary.TIMEOUT_SECONDS
                )
            except queue.Empty as exc:
                raise AssertionError("operation did not reach deterministic boundary") from exc
            if signal == "outcome":
                succeeded, value = payload
                if succeeded:
                    raise AssertionError(
                        f"operation completed before deterministic boundary: {value!r}"
                    )
                raise value
            self.assertEqual(signal, "boundary")
            boundary_action()
        finally:
            boundary.release.set()
            worker.join(timeout=boundary.TIMEOUT_SECONDS)
        self.assertFalse(worker.is_alive(), "operation did not finish after boundary release")
        try:
            signal, payload = boundary.signals.get(timeout=boundary.TIMEOUT_SECONDS)
        except queue.Empty as exc:
            raise AssertionError("operation produced no terminal outcome") from exc
        self.assertEqual(signal, "outcome")
        succeeded, value = payload
        if not succeeded:
            raise value
        return value

    def _assert_cancelled_at_boundary(
        self,
        operation,
        context: OperationContext,
        boundary: DeterministicBoundary,
    ) -> None:
        with self.assertRaises(TaskCancelled):
            self._run_at_boundary(
                operation,
                boundary,
                lambda: self.assertTrue(context.request_cancel()),
            )
        self.assertTrue(context.cancel_requested)
        self.assertFalse(context.critical_to_completion)

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

    def test_preview_forwards_same_context_to_analyse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "new-engine.zip"
            write_update_zip(archive)
            service = EngineUpdateService(paths, BackupService(paths))
            context = OperationContext()

            with patch.object(service, "_analyse", wraps=service._analyse) as analyse:
                service.preview(archive, context=context)

            analyse.assert_called_once()
            self.assertIs(analyse.call_args.kwargs.get("context"), context)

    def test_apply_forwards_same_context_to_analyse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "new-engine.zip"
            write_update_zip(archive)
            service = EngineUpdateService(paths, BackupService(paths))
            context = OperationContext()

            with patch.object(service, "_analyse", wraps=service._analyse) as analyse:
                service.apply(archive, context=context)

            analyse.assert_called_once()
            self.assertIs(analyse.call_args.kwargs.get("context"), context)

    def test_preview_cancel_before_zip_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "new-engine.zip"
            write_update_zip(archive)
            service = EngineUpdateService(paths, BackupService(paths))
            context = OperationContext()
            self.assertTrue(context.request_cancel())

            with patch("douk_manager.core.engine_update.zipfile.ZipFile") as zip_file:
                with self.assertRaises(TaskCancelled):
                    service.preview(archive, context=context)

            zip_file.assert_not_called()

    def test_preview_cancel_after_infolist_prevents_crc_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "new-engine.zip"
            write_update_zip(archive)
            service = EngineUpdateService(paths, BackupService(paths))
            context = OperationContext()
            boundary = DeterministicBoundary()
            real_infolist = zipfile.ZipFile.infolist

            def gated_infolist(handle):
                infos = real_infolist(handle)
                boundary.wait()
                return infos

            with (
                patch.object(
                    zipfile.ZipFile,
                    "infolist",
                    autospec=True,
                    side_effect=gated_infolist,
                ),
                patch.object(
                    zipfile.ZipFile,
                    "open",
                    autospec=True,
                    side_effect=AssertionError(
                        "CRC member opened after cancellation won at infolist boundary"
                    ),
                ) as member_open,
            ):
                self._assert_cancelled_at_boundary(
                    lambda: service.preview(archive, context=context),
                    context,
                    boundary,
                )

            member_open.assert_not_called()

    def test_preview_cancel_during_chunked_crc_stops_more_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "large-engine.zip"
            write_update_zip(
                archive,
                compression=zipfile.ZIP_STORED,
                extra_members={"payload.bin": b"c" * (3 * 1024 * 1024 + 17)},
            )
            service = EngineUpdateService(paths, BackupService(paths))
            context = OperationContext()
            boundary = DeterministicBoundary()
            real_read = zipfile.ZipExtFile.read
            payload_reads = 0

            def gated_read(handle, size=-1):
                nonlocal payload_reads
                data = real_read(handle, size)
                if Path(handle.name).name == "payload.bin" and size > 0 and data:
                    payload_reads += 1
                    if payload_reads == 2:
                        boundary.wait()
                return data

            with patch.object(
                zipfile.ZipExtFile,
                "read",
                autospec=True,
                side_effect=gated_read,
            ):
                self._assert_cancelled_at_boundary(
                    lambda: service.preview(archive, context=context),
                    context,
                    boundary,
                )

            self.assertEqual(payload_reads, 2)

    def test_preview_cancel_midway_through_member_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "new-engine.zip"
            write_update_zip(archive)
            service = EngineUpdateService(paths, BackupService(paths))
            context = OperationContext()
            boundary = DeterministicBoundary()
            real_safe_member_path = service._safe_member_path
            traversed = 0

            def gated_safe_member_path(info):
                nonlocal traversed
                path = real_safe_member_path(info)
                traversed += 1
                if traversed == 2:
                    boundary.wait()
                return path

            with patch.object(
                service,
                "_safe_member_path",
                side_effect=gated_safe_member_path,
            ):
                self._assert_cancelled_at_boundary(
                    lambda: service.preview(archive, context=context),
                    context,
                    boundary,
                )

            self.assertEqual(traversed, 2)

    def test_preview_cancel_before_structure_wins_over_structure_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "incomplete.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("main.exe", b"new executable")
            service = EngineUpdateService(paths, BackupService(paths))
            context = OperationContext()
            boundary = DeterministicBoundary()
            real_safe_member_path = service._safe_member_path
            real_raise_if_cancelled = context.raise_if_cancelled
            traversed = 0

            def count_member(info):
                nonlocal traversed
                traversed += 1
                return real_safe_member_path(info)

            def gate_before_structure() -> None:
                if traversed == 1:
                    boundary.wait()
                real_raise_if_cancelled()

            with (
                patch.object(
                    service,
                    "_safe_member_path",
                    side_effect=count_member,
                ),
                patch.object(
                    context,
                    "raise_if_cancelled",
                    side_effect=gate_before_structure,
                ),
            ):
                self._assert_cancelled_at_boundary(
                    lambda: service.preview(archive, context=context),
                    context,
                    boundary,
                )

    def test_preview_cancel_during_chunked_archive_sha_stops_more_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "large-engine.zip"
            write_update_zip(
                archive,
                compression=zipfile.ZIP_STORED,
                extra_members={"payload.bin": b"s" * (3 * 1024 * 1024 + 17)},
            )
            service = EngineUpdateService(paths, BackupService(paths))
            context = OperationContext()
            boundary = DeterministicBoundary()
            real_path_open = Path.open
            gated_readers: list[GatedReader] = []

            def gated_open(path, mode="r", *args, **kwargs):
                handle = real_path_open(path, mode, *args, **kwargs)
                if path.resolve() == archive.resolve() and mode == "rb":
                    reader = GatedReader(handle, boundary)
                    gated_readers.append(reader)
                    return reader
                return handle

            with patch.object(Path, "open", autospec=True, side_effect=gated_open):
                self._assert_cancelled_at_boundary(
                    lambda: service.preview(archive, context=context),
                    context,
                    boundary,
                )

            self.assertEqual(len(gated_readers), 1)
            self.assertEqual(gated_readers[0].chunk_reads, 2)

    def test_preview_without_cancel_matches_legacy_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "new-engine.zip"
            write_update_zip(archive)
            service = EngineUpdateService(paths, BackupService(paths))

            legacy = service.preview(archive)
            cancellable = service.preview(archive, context=OperationContext())

            self.assertEqual(cancellable, legacy)

    def test_preview_context_preserves_existing_validation_errors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            service = EngineUpdateService(paths, BackupService(paths))

            traversal = base / "traversal.zip"
            write_update_zip(traversal, traversal=True)

            duplicate = base / "duplicate.zip"
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", UserWarning)
                with zipfile.ZipFile(duplicate, "w") as handle:
                    handle.writestr("main.exe", b"new executable")
                    handle.writestr("_internal/runtime.dll", b"runtime")
                    handle.writestr("_internal/runtime.dll", b"duplicate")

            bad_crc = base / "bad-crc.zip"
            write_update_zip(bad_crc, compression=zipfile.ZIP_STORED)
            corrupt_stored_member(bad_crc, "main.exe")

            too_many = base / "too-many.zip"
            write_update_zip(too_many)

            too_large = base / "too-large.zip"
            write_update_zip(too_large)

            incomplete = base / "incomplete.zip"
            with zipfile.ZipFile(incomplete, "w") as handle:
                handle.writestr("main.exe", b"new executable")

            cases = (
                ("illegal path", traversal, None, None),
                ("duplicate path", duplicate, None, None),
                ("CRC failure", bad_crc, None, None),
                ("file-count limit", too_many, 2, None),
                ("size limit", too_large, None, 5),
                ("missing internal", incomplete, None, None),
            )
            for name, archive, max_files, max_bytes in cases:
                with self.subTest(case=name):
                    files_limit = service.MAX_FILES if max_files is None else max_files
                    bytes_limit = (
                        service.MAX_UNCOMPRESSED_BYTES
                        if max_bytes is None
                        else max_bytes
                    )
                    with (
                        patch.object(service, "MAX_FILES", files_limit),
                        patch.object(
                            service,
                            "MAX_UNCOMPRESSED_BYTES",
                            bytes_limit,
                        ),
                    ):
                        with self.assertRaises(EngineUpdateError) as legacy_error:
                            service.preview(archive)
                        with self.assertRaises(EngineUpdateError) as context_error:
                            service.preview(archive, context=OperationContext())
                    self.assertEqual(
                        type(context_error.exception),
                        type(legacy_error.exception),
                    )
                    self.assertEqual(
                        str(context_error.exception),
                        str(legacy_error.exception),
                    )

    def test_apply_cancellation_during_backup_is_latched_until_backup_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "new-engine.zip"
            write_update_zip(archive)
            backup = BackupService(paths)
            service = EngineUpdateService(paths, backup)
            context = OperationContext()
            boundary = DeterministicBoundary()
            backup_finished = threading.Event()
            real_snapshot = backup.create_full_snapshot
            real_move = shutil.move

            def gated_snapshot(*args, **kwargs):
                boundary.wait()
                result = real_snapshot(*args, **kwargs)
                backup_finished.set()
                return result

            with (
                patch.object(
                    backup,
                    "create_full_snapshot",
                    side_effect=gated_snapshot,
                ),
                patch(
                    "douk_manager.core.engine_update.shutil.move",
                    wraps=real_move,
                ) as move,
            ):
                self._assert_cancelled_at_boundary(
                    lambda: service.apply(archive, context=context),
                    context,
                    boundary,
                )

            self.assertTrue(backup_finished.is_set())
            move.assert_not_called()

    def test_apply_cancellation_after_backup_has_zero_formal_moves(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "new-engine.zip"
            write_update_zip(archive)
            backup = BackupService(paths)
            service = EngineUpdateService(paths, backup)
            context = OperationContext()
            real_snapshot = backup.create_full_snapshot
            real_move = shutil.move

            def snapshot_then_cancel(*args, **kwargs):
                result = real_snapshot(*args, **kwargs)
                self.assertTrue(context.request_cancel())
                return result

            with (
                patch.object(
                    backup,
                    "create_full_snapshot",
                    side_effect=snapshot_then_cancel,
                ),
                patch(
                    "douk_manager.core.engine_update.shutil.move",
                    wraps=real_move,
                ) as move,
            ):
                with self.assertRaises(TaskCancelled):
                    service.apply(archive, context=context)

            move.assert_not_called()
            self.assertFalse(context.critical_to_completion)

    def test_apply_cancellation_after_staging_is_cancelled_before_formal_move(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "new-engine.zip"
            write_update_zip(archive)
            service = EngineUpdateService(paths, BackupService(paths))
            context = OperationContext()
            real_extract = service._extract
            real_move = shutil.move

            def extract_then_cancel(analysis, destination) -> None:
                real_extract(analysis, destination)
                self.assertTrue(context.request_cancel())

            with (
                patch.object(service, "_extract", side_effect=extract_then_cancel),
                patch(
                    "douk_manager.core.engine_update.shutil.move",
                    wraps=real_move,
                ) as move,
            ):
                with self.assertRaises(TaskCancelled):
                    service.apply(archive, context=context)

            move.assert_not_called()
            self.assertTrue(context.cancel_requested)
            self.assertFalse(context.critical_to_completion)

    def test_apply_critical_wins_and_rejects_late_cancellation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            archive = base / "new-engine.zip"
            write_update_zip(archive)
            service = EngineUpdateService(paths, BackupService(paths))
            context = OperationContext()
            boundary = DeterministicBoundary()
            real_move = shutil.move
            formal_moves = 0

            def gated_move(source, destination, *args, **kwargs):
                nonlocal formal_moves
                formal_moves += 1
                if formal_moves == 1:
                    boundary.wait()
                return real_move(source, destination, *args, **kwargs)

            def reject_late_cancel() -> None:
                self.assertTrue(context.critical_to_completion)
                self.assertFalse(context.request_cancel())

            with patch(
                "douk_manager.core.engine_update.shutil.move",
                side_effect=gated_move,
            ):
                result = self._run_at_boundary(
                    lambda: service.apply(archive, context=context),
                    boundary,
                    reject_late_cancel,
                )

            self.assertEqual(result.new_main_sha256, sha256_file(paths.engine_exe))
            self.assertGreaterEqual(formal_moves, 5)
            self.assertTrue(context.critical_to_completion)
            self.assertFalse(context.cancel_requested)

    def test_apply_verification_failure_completes_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = make_test_paths(base)
            old_executable = paths.engine_exe.read_bytes()
            old_runtime = paths.engine_root / "_internal" / "old-runtime.dll"
            old_runtime.write_bytes(b"old runtime")
            critical_before = {
                name: sha256_file(paths.volume / name)
                for name in EngineUpdateService.CRITICAL_NAMES
            }
            archive = base / "new-engine.zip"
            write_update_zip(archive)
            service = EngineUpdateService(paths, BackupService(paths))
            context = OperationContext()

            with (
                patch.object(
                    service,
                    "_verify_after_update",
                    side_effect=EngineUpdateError("injected verification failure"),
                ),
                patch.object(
                    service,
                    "_restore_failed_update",
                    wraps=service._restore_failed_update,
                ) as restore,
            ):
                with self.assertRaisesRegex(
                    EngineUpdateError,
                    "injected verification failure",
                ):
                    service.apply(archive, context=context)

            restore.assert_called_once()
            self.assertTrue(context.critical_to_completion)
            self.assertEqual(paths.engine_exe.read_bytes(), old_executable)
            self.assertEqual(old_runtime.read_bytes(), b"old runtime")
            self.assertFalse(
                (paths.engine_root / "_internal" / "new-runtime.dll").exists()
            )
            self.assertEqual(
                {
                    name: sha256_file(paths.volume / name)
                    for name in service.CRITICAL_NAMES
                },
                critical_before,
            )


if __name__ == "__main__":
    unittest.main()
