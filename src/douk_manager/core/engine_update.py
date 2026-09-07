from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from douk_manager.config import ManagedPaths
from douk_manager.core.backup import BackupService, sha256_file, sqlite_quick_check
from douk_manager.core.json_store import JsonFileError, read_json, write_json_atomic
from douk_manager.core.locks import critical_section
from douk_manager.operation import TaskCancelled

if TYPE_CHECKING:
    from douk_manager.operation import OperationContext


class EngineUpdateError(RuntimeError):
    pass


class RollbackOrigin(str, Enum):
    """Identifies which managed history directory owns a rollback point."""

    ROLLBACK = "ROLLBACK"
    SUPERSEDED = "SUPERSEDED"


class RollbackIntegrity(str, Enum):
    OK = "OK"
    NO_MANIFEST = "NO_MANIFEST"
    MISSING_MAIN = "MISSING_MAIN"
    MISSING_INTERNAL = "MISSING_INTERNAL"
    HASH_MISMATCH = "HASH_MISMATCH"
    CONTAINS_VOLUME = "CONTAINS_VOLUME"
    UNREADABLE = "UNREADABLE"


class RollbackApplyGate(str, Enum):
    ALLOWED = "ALLOWED"
    ALLOWED_WITH_WARNING = "ALLOWED_WITH_WARNING"
    REJECTED = "REJECTED"


@dataclass(frozen=True)
class EngineRollbackPoint:
    directory: Path
    origin: RollbackOrigin
    stamp: str
    installed_at: datetime | None
    archive_name: str | None
    archive_sha256: str | None
    main_exe_sha256: str | None
    main_exe_bytes: int | None
    internal_file_count: int | None
    has_manifest: bool
    integrity: RollbackIntegrity
    reject_reason: str
    observed_main_sha256: str | None = None
    note: str = ""


@dataclass(frozen=True)
class RollbackApplyDecision:
    gate: RollbackApplyGate
    reason: str
    requires_second_confirm: bool


@dataclass(frozen=True)
class EngineRollbackPreview:
    point: EngineRollbackPoint
    current_main_sha256: str
    current_internal_file_count: int
    volume_path: Path
    critical_files: dict[str, str]
    is_same_as_current: bool
    volume_stays: bool = True


@dataclass(frozen=True)
class EngineRollbackResult:
    point: EngineRollbackPoint
    backup_path: Path
    superseded_path: Path
    restored_main_sha256: str
    replaced_main_sha256: str
    preserved_files: tuple[str, ...]
    source_had_manifest: bool
    observed_main_sha256: str
    observed_main_bytes: int
    source_directory: Path
    manifest_verified: bool


@dataclass(frozen=True)
class EngineRollbackUsage:
    rollback_count: int
    rollback_bytes: int
    superseded_count: int
    superseded_bytes: int
    total_bytes: int


_ROLLBACK_REASONS = {
    RollbackIntegrity.OK: "安装清单校验通过。",
    RollbackIntegrity.NO_MANIFEST: "缺少安装清单，无法核对与已安装版本的一致性。",
    RollbackIntegrity.MISSING_MAIN: "回退点缺少 main.exe。",
    RollbackIntegrity.MISSING_INTERNAL: "回退点缺少 _internal。",
    RollbackIntegrity.HASH_MISMATCH: "main.exe 与安装清单哈希不一致。",
    RollbackIntegrity.CONTAINS_VOLUME: "回退点包含正式 Volume，拒绝执行。",
    RollbackIntegrity.UNREADABLE: "回退点文件无法读取。",
}


def rollback_can_apply(point: EngineRollbackPoint) -> RollbackApplyDecision:
    """The one IO-free execution gate shared by UI and service callers."""

    if point.integrity is RollbackIntegrity.OK:
        return RollbackApplyDecision(
            RollbackApplyGate.ALLOWED,
            _ROLLBACK_REASONS[RollbackIntegrity.OK],
            False,
        )
    if point.integrity is RollbackIntegrity.NO_MANIFEST:
        return RollbackApplyDecision(
            RollbackApplyGate.ALLOWED_WITH_WARNING,
            _ROLLBACK_REASONS[RollbackIntegrity.NO_MANIFEST],
            True,
        )
    return RollbackApplyDecision(
        RollbackApplyGate.REJECTED,
        point.reject_reason or _ROLLBACK_REASONS.get(point.integrity, "回退点不可执行。"),
        False,
    )


@dataclass(frozen=True)
class EnginePackagePreview:
    archive: Path
    archive_sha256: str
    package_prefix: str
    file_count: int
    uncompressed_bytes: int
    main_exe_bytes: int
    contains_packaged_volume: bool


@dataclass(frozen=True)
class EngineUpdateResult:
    archive: Path
    backup_path: Path
    rollback_path: Path
    archive_sha256: str
    old_main_sha256: str
    new_main_sha256: str
    preserved_files: tuple[str, ...]


@dataclass(frozen=True)
class _PackageAnalysis:
    preview: EnginePackagePreview
    members: tuple[tuple[zipfile.ZipInfo, PurePosixPath], ...]
    root_parts: tuple[str, ...]


class EngineUpdateService:
    MAX_FILES = 100_000
    MAX_UNCOMPRESSED_BYTES = 8 * 1024 * 1024 * 1024
    CRITICAL_NAMES = (
        "settings_master.json",
        "settings.json",
        "DouK-Downloader.db",
    )
    ENGINE_SIDECAR_NAMES = ("encipher.py",)
    ROLLBACK_NOTES_SCHEMA = 1
    ROLLBACK_NOTES_FILENAME = "engine_rollback_notes.json"
    MAX_ROLLBACK_NOTE_CHARS = 200

    _ROLLBACK_ROOTS = (
        ("EngineRollback", RollbackOrigin.ROLLBACK, "update-manifest.json"),
        ("EngineSuperseded", RollbackOrigin.SUPERSEDED, "rollback-manifest.json"),
    )

    def __init__(
        self,
        paths: ManagedPaths,
        backup: BackupService,
        *,
        process_guard: Callable[[], None] | None = None,
    ) -> None:
        self.paths = paths
        self.backup = backup
        self._process_guard = process_guard

    def preview(
        self,
        archive: Path,
        *,
        context: OperationContext | None = None,
    ) -> EnginePackagePreview:
        return self._analyse(archive, context=context).preview

    def apply(
        self,
        archive: Path,
        *,
        context: OperationContext | None = None,
    ) -> EngineUpdateResult:
        if context is not None:
            context.raise_if_cancelled()
        analysis = self._analyse(archive, context=context)
        preview = analysis.preview
        self.backup.validate_live_data()
        if not self.paths.engine_exe.is_file():
            raise EngineUpdateError(f"当前下载引擎不存在：{self.paths.engine_exe}")
        current_internal = self.paths.engine_root / "_internal"
        if not current_internal.is_dir():
            raise EngineUpdateError(f"当前下载引擎缺少 _internal：{current_internal}")
        if self.paths.volume.resolve() != (current_internal / "Volume").resolve():
            raise EngineUpdateError("正式 Volume 位置与下载引擎目录不一致，拒绝更新。")

        critical_before = self._critical_hashes()
        sidecar_before = self._sidecar_hashes()
        old_main_sha256 = sha256_file(self.paths.engine_exe)
        backup_kwargs = {
            "category": "BeforeEngineUpdate",
            "metadata": {
                "archive": str(preview.archive),
                "archive_sha256": preview.archive_sha256,
                "old_main_sha256": old_main_sha256,
            },
            "keep_latest": 2,
        }
        backup_path = self.backup.create_full_snapshot(**backup_kwargs)
        if context is not None:
            context.raise_if_cancelled()

        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        staging = self.paths.updates / "EngineStaging" / f"{stamp}-{uuid.uuid4().hex}"
        rollback = self.paths.updates / "EngineRollback" / stamp
        staged_package: Path | None = None
        old_exe = rollback / self.paths.engine_exe.name
        old_internal = rollback / "_internal"
        try:
            staging.mkdir(parents=True)
            rollback.mkdir(parents=True)
            self._extract(analysis, staging)
            staged_package = staging.joinpath(*analysis.root_parts)
            new_exe = staged_package / "main.exe"
            new_internal = staged_package / "_internal"
            if not new_exe.is_file() or not new_internal.is_dir():
                raise EngineUpdateError("解压后的更新包结构无效。")

            packaged_volume = new_internal / "Volume"
            if packaged_volume.is_dir():
                shutil.rmtree(packaged_volume)
            elif packaged_volume.exists():
                packaged_volume.unlink()
            new_main_sha256 = sha256_file(new_exe)

            if context is not None:
                context.enter_critical_phase()

            shutil.move(str(self.paths.engine_exe), str(old_exe))
            shutil.move(str(current_internal), str(old_internal))
            shutil.move(str(new_internal), str(current_internal))
            self._move_live_volume(old_internal / "Volume", current_internal / "Volume")
            shutil.move(str(new_exe), str(self.paths.engine_exe))

            self._verify_after_update(
                critical_before,
                new_main_sha256,
                sidecar_before=sidecar_before,
            )
            manifest = {
                "schema": 1,
                "installed_at": datetime.now().isoformat(timespec="seconds"),
                "archive": str(preview.archive),
                "archive_sha256": preview.archive_sha256,
                "backup_path": str(backup_path),
                "engine_exe": str(self.paths.engine_exe),
                "old_main_sha256": old_main_sha256,
                "new_main_sha256": new_main_sha256,
                "critical_files": critical_before,
                "preview": {
                    key: str(value) if isinstance(value, Path) else value
                    for key, value in asdict(preview).items()
                },
            }
            manifest_path = rollback / "update-manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
                raise EngineUpdateError("更新清单复读校验失败。")
            return EngineUpdateResult(
                archive=preview.archive,
                backup_path=backup_path,
                rollback_path=rollback,
                archive_sha256=preview.archive_sha256,
                old_main_sha256=old_main_sha256,
                new_main_sha256=new_main_sha256,
                preserved_files=self.CRITICAL_NAMES,
            )
        except TaskCancelled:
            raise
        except Exception as exc:
            rollback_error = self._restore_failed_update(
                old_exe,
                old_internal,
                sidecar_before=sidecar_before,
            )
            if rollback_error:
                raise EngineUpdateError(
                    "下载引擎更新失败，自动回退也未完整完成。"
                    f"正式数据备份位于：{backup_path}；回退目录：{rollback}；"
                    f"原始错误：{exc}；回退错误：{rollback_error}"
                ) from exc
            if isinstance(exc, EngineUpdateError):
                raise
            raise EngineUpdateError(f"下载引擎更新失败，旧引擎已自动恢复：{exc}") from exc
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def list_rollback_points(
        self,
        *,
        context: OperationContext | None = None,
    ) -> tuple[EngineRollbackPoint, ...]:
        """Read both managed engine-history directories without changing them."""

        points: list[EngineRollbackPoint] = []
        notes = self._read_rollback_notes()
        for directory_name, origin, manifest_name in self._ROLLBACK_ROOTS:
            self._raise_if_cancelled(context)
            root = self.paths.updates / directory_name
            if not root.exists():
                continue
            try:
                candidates = sorted(root.iterdir(), key=lambda path: path.name)
            except OSError as exc:
                raise EngineUpdateError("回退点目录无法读取。") from exc
            for candidate in candidates:
                self._raise_if_cancelled(context)
                try:
                    if not candidate.is_dir():
                        continue
                    # A consumed point retains its manifest but no longer owns an engine.
                    if not (candidate / "main.exe").exists() and not (
                        candidate / "_internal"
                    ).exists():
                        continue
                except OSError:
                    pass
                points.append(
                    self._inspect_rollback_point(
                        candidate,
                        origin=origin,
                        manifest_name=manifest_name,
                        context=context,
                        note=notes.get(self._rollback_note_key(origin, candidate.name), ""),
                    )
                )
        return tuple(sorted(points, key=lambda point: point.stamp, reverse=True))

    def preview_rollback(
        self,
        point_dir: Path,
        *,
        context: OperationContext | None = None,
    ) -> EngineRollbackPreview:
        """Return a read-only preview, including hard-rejected candidates."""

        self._raise_if_cancelled(context)
        point_path, origin, manifest_name = self._rollback_location(point_dir)
        notes = self._read_rollback_notes()
        point = self._inspect_rollback_point(
            point_path,
            origin=origin,
            manifest_name=manifest_name,
            context=context,
            note=notes.get(self._rollback_note_key(origin, point_path.name), ""),
        )
        self._raise_if_cancelled(context)
        self.backup.validate_live_data()
        current_internal = self.paths.engine_root / "_internal"
        if not self.paths.engine_exe.is_file():
            raise EngineUpdateError("当前下载引擎缺少 main.exe。")
        if not current_internal.is_dir():
            raise EngineUpdateError("当前下载引擎缺少 _internal。")
        if self.paths.volume.resolve() != (current_internal / "Volume").resolve():
            raise EngineUpdateError("正式 Volume 位置与下载引擎目录不一致，拒绝回退。")
        current_sha256 = self._sha256_file_with_context(
            self.paths.engine_exe,
            context=context,
        )
        current_file_count = self._count_files(current_internal, context=context)
        critical_files = self._critical_hashes()
        self._raise_if_cancelled(context)
        return EngineRollbackPreview(
            point=point,
            current_main_sha256=current_sha256,
            current_internal_file_count=current_file_count,
            volume_path=self.paths.volume,
            critical_files=critical_files,
            is_same_as_current=(
                point.main_exe_sha256 is not None
                and point.main_exe_sha256 == current_sha256
            ),
        )

    def measure_rollback_usage(
        self,
        *,
        context: OperationContext | None = None,
    ) -> EngineRollbackUsage:
        """Recursively stat both history roots; never mutate or prune them."""

        values: dict[RollbackOrigin, tuple[int, int]] = {}
        for directory_name, origin, _manifest_name in self._ROLLBACK_ROOTS:
            self._raise_if_cancelled(context)
            root = self.paths.updates / directory_name
            if not root.exists():
                values[origin] = (0, 0)
                continue
            count = 0
            size = 0
            try:
                for candidate in root.iterdir():
                    self._raise_if_cancelled(context)
                    if not candidate.is_dir():
                        continue
                    # A consumed point retains only its audit manifest and no
                    # longer represents an engine copy.
                    if not (candidate / "main.exe").exists() and not (
                        candidate / "_internal"
                    ).exists():
                        continue
                    count += 1
                    for path in candidate.rglob("*"):
                        self._raise_if_cancelled(context)
                        if path.is_file():
                            size += path.stat().st_size
            except OSError as exc:
                raise EngineUpdateError("回退点磁盘占用无法读取。") from exc
            values[origin] = (count, size)
        rollback_count, rollback_bytes = values.get(RollbackOrigin.ROLLBACK, (0, 0))
        superseded_count, superseded_bytes = values.get(
            RollbackOrigin.SUPERSEDED,
            (0, 0),
        )
        return EngineRollbackUsage(
            rollback_count=rollback_count,
            rollback_bytes=rollback_bytes,
            superseded_count=superseded_count,
            superseded_bytes=superseded_bytes,
            total_bytes=rollback_bytes + superseded_bytes,
        )

    @property
    def rollback_notes_path(self) -> Path:
        return self.paths.data / self.ROLLBACK_NOTES_FILENAME

    def set_rollback_note(self, point_dir: Path, note: str) -> str:
        """Persist user metadata separately from immutable engine history."""

        point_path, origin, _manifest_name = self._rollback_location(point_dir)
        normalized = self._normalize_rollback_note(note)
        try:
            is_visible_point = point_path.is_dir() and (
                (point_path / "main.exe").exists()
                or (point_path / "_internal").exists()
            )
        except OSError as exc:
            raise EngineUpdateError("回退点状态无法读取，备注未保存。") from exc
        if not is_visible_point:
            raise EngineUpdateError("回退点已不在当前列表中，备注未保存。")

        key = self._rollback_note_key(origin, point_path.name)
        with critical_section(self.paths.lock_file, timeout=1.0):
            notes = self._read_rollback_notes()
            if normalized:
                notes[key] = normalized
            else:
                notes.pop(key, None)
            try:
                write_json_atomic(
                    self.rollback_notes_path,
                    {
                        "schema": self.ROLLBACK_NOTES_SCHEMA,
                        "notes": notes,
                    },
                )
            except (OSError, JsonFileError) as exc:
                raise EngineUpdateError("回退点备注保存失败。") from exc
        return normalized

    def apply_rollback(
        self,
        point_dir: Path,
        *,
        accept_unverified: bool = False,
        context: OperationContext | None = None,
    ) -> EngineRollbackResult:
        """Replace only engine program files and preserve the live Volume."""

        self._raise_if_cancelled(context)
        with critical_section(self.paths.lock_file):
            self._raise_if_cancelled(context)
            preview = self.preview_rollback(point_dir, context=context)
            decision = rollback_can_apply(preview.point)
            if decision.gate is RollbackApplyGate.REJECTED:
                raise EngineUpdateError(decision.reason)
            if decision.requires_second_confirm and not accept_unverified:
                raise EngineUpdateError("无清单回退点缺少二次确认，拒绝执行。")

            # Recheck every mutable prerequisite while the process lock is held.
            self.backup.validate_live_data()
            current_internal = self.paths.engine_root / "_internal"
            if not self.paths.engine_exe.is_file():
                raise EngineUpdateError("当前下载引擎缺少 main.exe。")
            if not current_internal.is_dir():
                raise EngineUpdateError("当前下载引擎缺少 _internal。")
            if self.paths.volume.resolve() != (current_internal / "Volume").resolve():
                raise EngineUpdateError("正式 Volume 位置与下载引擎目录不一致，拒绝回退。")

            point = preview.point
            observed_main_sha256 = self._sha256_file_with_context(
                point.directory / "main.exe",
                context=context,
            )
            observed_main_bytes = (point.directory / "main.exe").stat().st_size
            if (
                decision.gate is RollbackApplyGate.ALLOWED
                and observed_main_sha256 != point.main_exe_sha256
            ):
                raise EngineUpdateError(_ROLLBACK_REASONS[RollbackIntegrity.HASH_MISMATCH])
            critical_before = self._critical_hashes()
            sidecar_before = self._sidecar_hashes()
            replaced_main_sha256 = sha256_file(self.paths.engine_exe)
            backup_path = self.backup.create_full_snapshot(
                category="BeforeEngineRollback",
                metadata={
                    "rollback_point": str(point.directory),
                    "target_main_sha256": observed_main_sha256,
                    "replaced_main_sha256": replaced_main_sha256,
                },
                keep_latest=2,
            )
            self._raise_if_cancelled(context)
            if self._process_guard is not None:
                self._process_guard()

            superseded = self._next_history_directory("EngineSuperseded")
            source_main_recovery = superseded / ".rollback-source-main"
            if context is not None:
                context.enter_critical_phase()
            try:
                superseded.mkdir(parents=True)
                shutil.copy2(point.directory / "main.exe", source_main_recovery)
                shutil.move(
                    str(self.paths.engine_exe),
                    str(superseded / self.paths.engine_exe.name),
                )
                shutil.move(str(current_internal), str(superseded / "_internal"))
                shutil.move(
                    str(point.directory / "_internal"),
                    str(current_internal),
                )
                self._move_live_volume(
                    superseded / "_internal" / "Volume",
                    current_internal / "Volume",
                )
                shutil.move(
                    str(point.directory / "main.exe"),
                    str(self.paths.engine_exe),
                )
                self._verify_after_rollback(
                    critical_before,
                    observed_main_sha256,
                    sidecar_before=sidecar_before,
                )
                manifest = {
                    "schema": 1,
                    "replaced_at": datetime.now().isoformat(timespec="seconds"),
                    "installed_at": datetime.now().isoformat(timespec="seconds"),
                    "source_origin": point.origin.value,
                    "source_directory": str(point.directory),
                    "source_had_manifest": point.has_manifest,
                    "manifest_verified": decision.gate is RollbackApplyGate.ALLOWED,
                    "observed_main_sha256": observed_main_sha256,
                    "observed_main_bytes": observed_main_bytes,
                    "restored_main_sha256": observed_main_sha256,
                    "replaced_main_sha256": replaced_main_sha256,
                    "backup_path": str(backup_path),
                    "critical_files": critical_before,
                }
                manifest_path = superseded / "rollback-manifest.json"
                manifest_path.write_text(
                    json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
                    raise EngineUpdateError("回退结果清单复读校验失败。")
                source_main_recovery.unlink()
            except Exception as exc:
                restore_error = self._restore_failed_rollback(
                    superseded,
                    point.directory,
                    critical_before=critical_before,
                    sidecar_before=sidecar_before,
                    source_main_recovery=source_main_recovery,
                )
                if restore_error:
                    raise EngineUpdateError(
                        "下载引擎回退失败，自动恢复也未完整完成。"
                        f"正式数据备份位于：{backup_path}；被换下引擎目录：{superseded}；"
                        f"原始错误：{exc}；恢复错误：{restore_error}"
                    ) from exc
                if isinstance(exc, EngineUpdateError):
                    raise
                raise EngineUpdateError(
                    f"下载引擎回退失败，原引擎已自动恢复：{exc}"
                ) from exc

            return EngineRollbackResult(
                point=point,
                backup_path=backup_path,
                superseded_path=superseded,
                restored_main_sha256=observed_main_sha256,
                replaced_main_sha256=replaced_main_sha256,
                preserved_files=self.CRITICAL_NAMES + self.ENGINE_SIDECAR_NAMES,
                source_had_manifest=point.has_manifest,
                observed_main_sha256=observed_main_sha256,
                observed_main_bytes=observed_main_bytes,
                source_directory=point.directory,
                manifest_verified=decision.gate is RollbackApplyGate.ALLOWED,
            )

    def _analyse(
        self,
        archive: Path,
        *,
        context: OperationContext | None = None,
    ) -> _PackageAnalysis:
        self._raise_if_cancelled(context)
        archive = archive.expanduser().resolve()
        self._raise_if_cancelled(context)
        if not archive.is_file():
            raise EngineUpdateError(f"更新包不存在：{archive}")
        if archive.suffix.lower() != ".zip":
            raise EngineUpdateError("下载引擎更新包必须是 ZIP 文件。")
        self._raise_if_cancelled(context)
        try:
            self._raise_if_cancelled(context)
            with zipfile.ZipFile(archive) as handle:
                self._raise_if_cancelled(context)
                infos = handle.infolist()
                self._raise_if_cancelled(context)
                bad = self._first_bad_zip_member(
                    handle,
                    infos,
                    context=context,
                )
        except TaskCancelled:
            raise
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise EngineUpdateError(f"无法读取有效 ZIP 更新包：{exc}") from exc
        self._raise_if_cancelled(context)
        if bad:
            raise EngineUpdateError(f"ZIP CRC 校验失败：{bad}")
        if len(infos) > self.MAX_FILES:
            raise EngineUpdateError("更新包文件数量超过安全限制。")
        self._raise_if_cancelled(context)

        members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        seen: set[str] = set()
        total = 0
        for info in infos:
            self._raise_if_cancelled(context)
            path = self._safe_member_path(info)
            self._raise_if_cancelled(context)
            key = path.as_posix().casefold()
            if key in seen:
                raise EngineUpdateError(f"更新包包含重复路径：{path}")
            seen.add(key)
            total += info.file_size
            if total > self.MAX_UNCOMPRESSED_BYTES:
                raise EngineUpdateError("更新包解压大小超过安全限制。")
            members.append((info, path))
            self._raise_if_cancelled(context)

        self._raise_if_cancelled(context)
        file_paths = [path for info, path in members if not info.is_dir()]
        roots: set[tuple[str, ...]] = set()
        for path in file_paths:
            self._raise_if_cancelled(context)
            if path.name.casefold() != "main.exe":
                continue
            root = path.parts[:-1]
            internal_prefix = tuple(part.casefold() for part in (*root, "_internal"))
            contains_internal = False
            for candidate in file_paths:
                self._raise_if_cancelled(context)
                if (
                    tuple(
                        part.casefold()
                        for part in candidate.parts[: len(internal_prefix)]
                    )
                    == internal_prefix
                    and len(candidate.parts) > len(root) + 1
                ):
                    contains_internal = True
                    break
            if contains_internal:
                roots.add(root)
        self._raise_if_cancelled(context)
        if len(roots) != 1:
            raise EngineUpdateError(
                "更新包必须且只能包含一套 main.exe 与对应的 _internal。"
            )
        root_parts = roots.pop()
        main_path = PurePosixPath(*root_parts, "main.exe")
        main_key = main_path.as_posix().casefold()
        main_info = None
        for info, path in members:
            self._raise_if_cancelled(context)
            if path.as_posix().casefold() == main_key:
                main_info = info
                break
        if main_info is None or main_info.file_size <= 0:
            raise EngineUpdateError("更新包中的 main.exe 无效。")
        volume_prefix = tuple(part.casefold() for part in (*root_parts, "_internal", "Volume"))
        contains_volume = False
        for _, path in members:
            self._raise_if_cancelled(context)
            if (
                tuple(part.casefold() for part in path.parts[: len(volume_prefix)])
                == volume_prefix
            ):
                contains_volume = True
                break
        prefix = PurePosixPath(*root_parts).as_posix() if root_parts else "."
        self._raise_if_cancelled(context)
        preview = EnginePackagePreview(
            archive=archive,
            archive_sha256=self._sha256_file_with_context(
                archive,
                context=context,
            ),
            package_prefix=prefix,
            file_count=sum(not info.is_dir() for info, _ in members),
            uncompressed_bytes=total,
            main_exe_bytes=main_info.file_size,
            contains_packaged_volume=contains_volume,
        )
        self._raise_if_cancelled(context)
        return _PackageAnalysis(preview, tuple(members), root_parts)

    @staticmethod
    def _raise_if_cancelled(context: OperationContext | None) -> None:
        if context is not None:
            context.raise_if_cancelled()

    @classmethod
    def _first_bad_zip_member(
        cls,
        handle: zipfile.ZipFile,
        infos: list[zipfile.ZipInfo],
        *,
        context: OperationContext | None,
    ) -> str | None:
        chunk_size = 2**20
        for info in infos:
            cls._raise_if_cancelled(context)
            try:
                with handle.open(info.filename, "r") as source:
                    while True:
                        cls._raise_if_cancelled(context)
                        chunk = source.read(chunk_size)
                        cls._raise_if_cancelled(context)
                        if not chunk:
                            break
            except zipfile.BadZipFile:
                return info.filename
        return None

    @classmethod
    def _sha256_file_with_context(
        cls,
        path: Path,
        *,
        context: OperationContext | None,
    ) -> str:
        digest = hashlib.sha256()
        cls._raise_if_cancelled(context)
        with path.open("rb") as handle:
            while True:
                cls._raise_if_cancelled(context)
                chunk = handle.read(1024 * 1024)
                cls._raise_if_cancelled(context)
                if not chunk:
                    break
                digest.update(chunk)
        cls._raise_if_cancelled(context)
        return digest.hexdigest()

    @staticmethod
    def _safe_member_path(info: zipfile.ZipInfo) -> PurePosixPath:
        raw = info.filename.replace("\\", "/")
        path = PurePosixPath(raw)
        if path.is_absolute() or not path.parts:
            raise EngineUpdateError(f"更新包包含非法路径：{info.filename}")
        if any(part in {"", ".", ".."} or ":" in part for part in path.parts):
            raise EngineUpdateError(f"更新包包含非法路径：{info.filename}")
        mode = info.external_attr >> 16
        if stat.S_ISLNK(mode):
            raise EngineUpdateError(f"更新包不允许符号链接：{info.filename}")
        return path

    @staticmethod
    def _extract(analysis: _PackageAnalysis, destination: Path) -> None:
        with zipfile.ZipFile(analysis.preview.archive) as handle:
            for info, relative in analysis.members:
                target = destination.joinpath(*relative.parts)
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with handle.open(info) as source, target.open("xb") as output:
                    shutil.copyfileobj(source, output, 1024 * 1024)

    @staticmethod
    def _move_live_volume(source: Path, destination: Path) -> None:
        if not source.is_dir():
            raise EngineUpdateError(f"旧引擎中缺少正式 Volume：{source}")
        if destination.exists():
            raise EngineUpdateError(f"新引擎目标已存在 Volume：{destination}")
        shutil.move(str(source), str(destination))

    def _rollback_location(
        self,
        point_dir: Path,
    ) -> tuple[Path, RollbackOrigin, str]:
        point_path = Path(point_dir).expanduser().resolve()
        for directory_name, origin, manifest_name in self._ROLLBACK_ROOTS:
            root = (self.paths.updates / directory_name).resolve()
            if point_path.parent == root:
                return point_path, origin, manifest_name
        raise EngineUpdateError("回退点不在受管的引擎历史目录中。")

    def _inspect_rollback_point(
        self,
        point_dir: Path,
        *,
        origin: RollbackOrigin,
        manifest_name: str,
        context: OperationContext | None,
        note: str = "",
    ) -> EngineRollbackPoint:
        self._raise_if_cancelled(context)
        manifest_path = point_dir / manifest_name
        manifest_present = False
        manifest: dict[str, Any] | None = None
        manifest_unreadable = False
        try:
            manifest_path.lstat()
        except FileNotFoundError:
            pass
        except OSError:
            manifest_present = True
            manifest_unreadable = True
        else:
            manifest_present = True
            try:
                parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                manifest_unreadable = True
            else:
                if isinstance(parsed, dict):
                    manifest = parsed
                else:
                    manifest_unreadable = True

        installed_at = self._parse_datetime(
            manifest.get("installed_at") if manifest is not None else None
        )
        archive_name = None
        archive_sha256 = None
        expected_sha256 = None
        if manifest is not None:
            archive = manifest.get("archive")
            if archive:
                archive_name = Path(str(archive)).name
            raw_archive_sha = manifest.get("archive_sha256")
            if raw_archive_sha:
                archive_sha256 = str(raw_archive_sha)
            expected_key = (
                "old_main_sha256"
                if origin is RollbackOrigin.ROLLBACK
                else "replaced_main_sha256"
            )
            raw_expected = manifest.get(expected_key)
            expected_sha256 = self._normalize_sha256(raw_expected)
            if expected_sha256 is None:
                manifest_unreadable = True

        integrity = (
            RollbackIntegrity.UNREADABLE
            if manifest_unreadable
            else RollbackIntegrity.NO_MANIFEST
        )
        main_bytes: int | None = None
        observed_main_sha256: str | None = None
        internal_file_count: int | None = None
        try:
            main = point_dir / "main.exe"
            internal = point_dir / "_internal"
            main_is_file = main.is_file()
            internal_is_dir = internal.is_dir()
            if main_is_file:
                self._raise_if_cancelled(context)
                observed_main_sha256 = self._sha256_file_with_context(
                    main,
                    context=context,
                )
                main_bytes = main.stat().st_size
            if manifest_unreadable:
                integrity = RollbackIntegrity.UNREADABLE
            elif not main_is_file:
                integrity = RollbackIntegrity.MISSING_MAIN
            elif not internal_is_dir:
                integrity = RollbackIntegrity.MISSING_INTERNAL
            elif (internal / "Volume").exists():
                integrity = RollbackIntegrity.CONTAINS_VOLUME
            else:
                self._raise_if_cancelled(context)
                internal_file_count = self._count_files(internal, context=context)
                if manifest is not None:
                    integrity = (
                        RollbackIntegrity.OK
                        if observed_main_sha256 == expected_sha256
                        else RollbackIntegrity.HASH_MISMATCH
                    )
                else:
                    integrity = RollbackIntegrity.NO_MANIFEST
        except TaskCancelled:
            raise
        except (OSError, ValueError):
            integrity = RollbackIntegrity.UNREADABLE

        return EngineRollbackPoint(
            directory=point_dir,
            origin=origin,
            stamp=point_dir.name,
            installed_at=installed_at,
            archive_name=archive_name,
            archive_sha256=archive_sha256,
            main_exe_sha256=expected_sha256,
            main_exe_bytes=main_bytes,
            internal_file_count=internal_file_count,
            has_manifest=manifest_present,
            integrity=integrity,
            reject_reason=_ROLLBACK_REASONS[integrity],
            observed_main_sha256=observed_main_sha256,
            note=note,
        )

    @staticmethod
    def _rollback_note_key(origin: RollbackOrigin, stamp: str) -> str:
        return f"{origin.value}:{stamp}"

    def _read_rollback_notes(self) -> dict[str, str]:
        path = self.rollback_notes_path
        try:
            if not path.exists():
                return {}
            if not path.is_file():
                raise EngineUpdateError("回退点备注记录无法读取。")
            document = read_json(path)
        except (OSError, JsonFileError) as exc:
            raise EngineUpdateError("回退点备注记录无法读取。") from exc

        raw_notes = document.get("notes")
        if document.get("schema") != self.ROLLBACK_NOTES_SCHEMA or not isinstance(
            raw_notes, dict
        ):
            raise EngineUpdateError("回退点备注记录格式无效，拒绝覆盖。")
        notes: dict[str, str] = {}
        for key, value in raw_notes.items():
            if (
                not isinstance(key, str)
                or not isinstance(value, str)
                or len(value) > self.MAX_ROLLBACK_NOTE_CHARS
            ):
                raise EngineUpdateError("回退点备注记录格式无效，拒绝覆盖。")
            notes[key] = value
        return notes

    def _normalize_rollback_note(self, note: str) -> str:
        if not isinstance(note, str):
            raise EngineUpdateError("回退点备注必须是文本。")
        normalized = " ".join(note.split())
        if len(normalized) > self.MAX_ROLLBACK_NOTE_CHARS:
            raise EngineUpdateError(
                f"回退点备注不能超过 {self.MAX_ROLLBACK_NOTE_CHARS} 个字符。"
            )
        return normalized

    @staticmethod
    def _normalize_sha256(value: object) -> str | None:
        if (
            not isinstance(value, str)
            or len(value) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in value)
        ):
            return None
        return value.lower()

    @staticmethod
    def _parse_datetime(value: object) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _count_files(path: Path, *, context: OperationContext | None) -> int:
        count = 0
        for candidate in path.rglob("*"):
            if context is not None:
                context.raise_if_cancelled()
            if candidate.is_file():
                count += 1
        return count

    def _sidecar_hashes(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for name in self.ENGINE_SIDECAR_NAMES:
            path = self.paths.engine_root / name
            if path.is_file():
                values[name] = sha256_file(path)
        return values

    def _verify_sidecars(self, expected: dict[str, str]) -> None:
        for name, digest in expected.items():
            path = self.paths.engine_root / name
            if not path.is_file() or sha256_file(path) != digest:
                raise EngineUpdateError(f"引擎 sidecar {name} 在操作前后发生变化。")

    def _next_history_directory(self, name: str) -> Path:
        root = self.paths.updates / name
        root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        candidate = root / stamp
        if candidate.exists():
            candidate = root / f"{stamp}-{uuid.uuid4().hex}"
        return candidate

    def _critical_hashes(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for name in self.CRITICAL_NAMES:
            path = self.paths.volume / name
            if not path.is_file():
                raise EngineUpdateError(f"正式 Volume 缺少关键文件：{path}")
            values[name] = sha256_file(path)
        return values

    def _verify_after_update(
        self,
        critical_before: dict[str, str],
        new_main_sha256: str,
        *,
        sidecar_before: dict[str, str] | None = None,
    ) -> None:
        if not self.paths.engine_exe.is_file():
            raise EngineUpdateError("更新后 main.exe 缺失。")
        if sha256_file(self.paths.engine_exe) != new_main_sha256:
            raise EngineUpdateError("更新后 main.exe 哈希校验失败。")
        critical_after = self._critical_hashes()
        if critical_after != critical_before:
            raise EngineUpdateError("更新前后正式 Volume 关键文件哈希不一致。")
        read_json(self.paths.master_settings)
        read_json(self.paths.active_settings)
        sqlite_quick_check(self.paths.database)
        if sidecar_before is not None:
            self._verify_sidecars(sidecar_before)

    def _restore_failed_update(
        self,
        old_exe: Path,
        old_internal: Path,
        *,
        sidecar_before: dict[str, str] | None = None,
    ) -> str:
        current_internal = self.paths.engine_root / "_internal"
        try:
            if old_internal.is_dir():
                old_volume = old_internal / "Volume"
                current_volume = current_internal / "Volume"
                if current_volume.is_dir() and not old_volume.exists():
                    shutil.move(str(current_volume), str(old_volume))
                if current_internal.exists():
                    if (current_internal / "Volume").exists():
                        raise EngineUpdateError(
                            "回退时新 _internal 仍包含 Volume，拒绝删除。"
                        )
                    shutil.rmtree(current_internal)
                shutil.move(str(old_internal), str(current_internal))
            if old_exe.is_file():
                if self.paths.engine_exe.exists():
                    self.paths.engine_exe.unlink()
                shutil.move(str(old_exe), str(self.paths.engine_exe))
            self.backup.validate_live_data()
            if sidecar_before is not None:
                self._verify_sidecars(sidecar_before)
            return ""
        except Exception as exc:  # pragma: no cover - catastrophic fallback reporting
            return str(exc)

    def _verify_after_rollback(
        self,
        critical_before: dict[str, str],
        target_main_sha256: str,
        *,
        sidecar_before: dict[str, str] | None = None,
    ) -> None:
        if not self.paths.engine_exe.is_file():
            raise EngineUpdateError("回退后 main.exe 缺失。")
        actual_sha256 = sha256_file(self.paths.engine_exe)
        if actual_sha256 != target_main_sha256:
            raise EngineUpdateError("回退后 main.exe 哈希校验失败。")
        if self._critical_hashes() != critical_before:
            raise EngineUpdateError("回退前后正式 Volume 关键文件哈希不一致。")
        read_json(self.paths.master_settings)
        read_json(self.paths.active_settings)
        sqlite_quick_check(self.paths.database)
        if sidecar_before is not None:
            self._verify_sidecars(sidecar_before)

    def _restore_failed_rollback(
        self,
        superseded: Path,
        point_dir: Path,
        *,
        critical_before: dict[str, str] | None = None,
        sidecar_before: dict[str, str] | None = None,
        source_main_recovery: Path | None = None,
    ) -> str:
        current_internal = self.paths.engine_root / "_internal"
        old_internal = superseded / "_internal"
        old_exe = superseded / self.paths.engine_exe.name
        point_internal = point_dir / "_internal"
        point_exe = point_dir / "main.exe"
        try:
            if source_main_recovery is not None and source_main_recovery.is_file():
                if point_exe.exists():
                    source_main_recovery.unlink()
                else:
                    shutil.move(str(source_main_recovery), str(point_exe))
            if old_internal.is_dir():
                old_volume = old_internal / "Volume"
                current_volume = current_internal / "Volume"
                if current_volume.is_dir() and not old_volume.exists():
                    shutil.move(str(current_volume), str(old_volume))
                if current_internal.exists():
                    if (current_internal / "Volume").exists():
                        raise EngineUpdateError(
                            "回退失败恢复时 _internal 仍包含 Volume，拒绝删除。"
                        )
                    if not point_internal.exists():
                        shutil.move(str(current_internal), str(point_internal))
                    else:
                        shutil.rmtree(current_internal)
                if current_internal.exists():
                    raise EngineUpdateError("回退失败恢复时当前 _internal 无法移除。")
                shutil.move(str(old_internal), str(current_internal))
            if self.paths.engine_exe.is_file() and not point_exe.exists() and old_exe.exists():
                shutil.move(str(self.paths.engine_exe), str(point_exe))
            if old_exe.is_file():
                if self.paths.engine_exe.exists():
                    self.paths.engine_exe.unlink()
                shutil.move(str(old_exe), str(self.paths.engine_exe))
            self.backup.validate_live_data()
            if critical_before is not None and self._critical_hashes() != critical_before:
                raise EngineUpdateError("恢复后正式 Volume 关键文件哈希不一致。")
            if sidecar_before is not None:
                self._verify_sidecars(sidecar_before)
            return ""
        except Exception as exc:  # pragma: no cover - catastrophic fallback reporting
            return str(exc)
