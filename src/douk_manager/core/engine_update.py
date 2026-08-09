from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from douk_manager.config import ManagedPaths
from douk_manager.core.backup import BackupService, sha256_file, sqlite_quick_check
from douk_manager.core.json_store import read_json


class EngineUpdateError(RuntimeError):
    pass


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

    def __init__(self, paths: ManagedPaths, backup: BackupService) -> None:
        self.paths = paths
        self.backup = backup

    def preview(self, archive: Path) -> EnginePackagePreview:
        return self._analyse(archive).preview

    def apply(self, archive: Path) -> EngineUpdateResult:
        analysis = self._analyse(archive)
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
        old_main_sha256 = sha256_file(self.paths.engine_exe)
        backup_path = self.backup.create_snapshot(
            "BeforeEngineUpdate",
            {
                "archive": str(preview.archive),
                "archive_sha256": preview.archive_sha256,
                "old_main_sha256": old_main_sha256,
            },
        )

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

            shutil.move(str(self.paths.engine_exe), str(old_exe))
            shutil.move(str(current_internal), str(old_internal))
            shutil.move(str(new_internal), str(current_internal))
            self._move_live_volume(old_internal / "Volume", current_internal / "Volume")
            shutil.move(str(new_exe), str(self.paths.engine_exe))

            self._verify_after_update(critical_before, new_main_sha256)
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
        except Exception as exc:
            rollback_error = self._restore_failed_update(old_exe, old_internal)
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

    def _analyse(self, archive: Path) -> _PackageAnalysis:
        archive = archive.expanduser().resolve()
        if not archive.is_file():
            raise EngineUpdateError(f"更新包不存在：{archive}")
        if archive.suffix.lower() != ".zip":
            raise EngineUpdateError("下载引擎更新包必须是 ZIP 文件。")
        try:
            with zipfile.ZipFile(archive) as handle:
                infos = handle.infolist()
                bad = handle.testzip()
        except (OSError, zipfile.BadZipFile, RuntimeError) as exc:
            raise EngineUpdateError(f"无法读取有效 ZIP 更新包：{exc}") from exc
        if bad:
            raise EngineUpdateError(f"ZIP CRC 校验失败：{bad}")
        if len(infos) > self.MAX_FILES:
            raise EngineUpdateError("更新包文件数量超过安全限制。")

        members: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        seen: set[str] = set()
        total = 0
        for info in infos:
            path = self._safe_member_path(info)
            key = path.as_posix().casefold()
            if key in seen:
                raise EngineUpdateError(f"更新包包含重复路径：{path}")
            seen.add(key)
            total += info.file_size
            if total > self.MAX_UNCOMPRESSED_BYTES:
                raise EngineUpdateError("更新包解压大小超过安全限制。")
            members.append((info, path))

        file_paths = [path for info, path in members if not info.is_dir()]
        roots: set[tuple[str, ...]] = set()
        for path in file_paths:
            if path.name.casefold() != "main.exe":
                continue
            root = path.parts[:-1]
            internal_prefix = tuple(part.casefold() for part in (*root, "_internal"))
            if any(
                tuple(part.casefold() for part in candidate.parts[: len(internal_prefix)])
                == internal_prefix
                and len(candidate.parts) > len(root) + 1
                for candidate in file_paths
            ):
                roots.add(root)
        if len(roots) != 1:
            raise EngineUpdateError(
                "更新包必须且只能包含一套 main.exe 与对应的 _internal。"
            )
        root_parts = roots.pop()
        main_path = PurePosixPath(*root_parts, "main.exe")
        main_info = next(
            (info for info, path in members if path.as_posix().casefold() == main_path.as_posix().casefold()),
            None,
        )
        if main_info is None or main_info.file_size <= 0:
            raise EngineUpdateError("更新包中的 main.exe 无效。")
        volume_prefix = tuple(part.casefold() for part in (*root_parts, "_internal", "Volume"))
        contains_volume = any(
            tuple(part.casefold() for part in path.parts[: len(volume_prefix)])
            == volume_prefix
            for _, path in members
        )
        prefix = PurePosixPath(*root_parts).as_posix() if root_parts else "."
        preview = EnginePackagePreview(
            archive=archive,
            archive_sha256=sha256_file(archive),
            package_prefix=prefix,
            file_count=sum(not info.is_dir() for info, _ in members),
            uncompressed_bytes=total,
            main_exe_bytes=main_info.file_size,
            contains_packaged_volume=contains_volume,
        )
        return _PackageAnalysis(preview, tuple(members), root_parts)

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

    def _critical_hashes(self) -> dict[str, str]:
        values: dict[str, str] = {}
        for name in self.CRITICAL_NAMES:
            path = self.paths.volume / name
            if not path.is_file():
                raise EngineUpdateError(f"正式 Volume 缺少关键文件：{path}")
            values[name] = sha256_file(path)
        return values

    def _verify_after_update(
        self, critical_before: dict[str, str], new_main_sha256: str
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

    def _restore_failed_update(self, old_exe: Path, old_internal: Path) -> str:
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
            return ""
        except Exception as exc:  # pragma: no cover - catastrophic fallback reporting
            return str(exc)
