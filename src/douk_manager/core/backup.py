from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import uuid
from contextlib import closing, nullcontext
from datetime import datetime
from pathlib import Path
from pathlib import PurePosixPath
from typing import TYPE_CHECKING, Any

from douk_manager.config import ManagedPaths
from douk_manager.core.json_store import JsonFileError, read_json, write_json_atomic
from douk_manager.core.locks import critical_section
from douk_manager.operation import TaskCancelled


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
from douk_manager.core.watchlist import (
    CONTROL_SCHEMA_VERSION,
    WATCHLIST_SCHEMA_VERSION,
    WATERMARK_SCHEMA_VERSION,
    WatchlistError,
    WatchlistService,
    validate_control,
    validate_document,
    validate_watermark,
)

if TYPE_CHECKING:
    from douk_manager.operation import OperationContext


class BackupError(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def sqlite_quick_check(path: Path) -> str:
    try:
        with closing(
            sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        ) as connection:
            row = connection.execute("PRAGMA quick_check").fetchone()
    except sqlite3.Error as exc:
        raise BackupError(f"数据库一致性检查失败：{path}：{exc}") from exc
    result = str(row[0]) if row else "missing result"
    if result.lower() != "ok":
        raise BackupError(f"数据库 quick_check 未通过：{result}")
    return result


def sqlite_backup(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with closing(
            sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
        ) as src:
            with closing(sqlite3.connect(destination)) as dst:
                src.backup(dst)
        sqlite_quick_check(destination)
    except sqlite3.Error as exc:
        destination.unlink(missing_ok=True)
        raise BackupError(f"数据库备份失败：{source}：{exc}") from exc


class BackupService:
    CRITICAL_FILENAMES = (
        "settings_master.json",
        "settings.json",
        "DouK-Downloader.db",
    )
    STARTUP_KEEP_LATEST = 20
    STARTUP_MANIFEST_SCHEMA = 3
    STARTUP_DATA_FILES = (
        "watchlist.json",
        "watchlist_w_watermark.json",
        "watchlist_control.json",
    )

    def __init__(self, paths: ManagedPaths) -> None:
        self.paths = paths
        self.last_startup_cleanup_error: str | None = None

    def validate_live_data(self) -> dict[str, Any]:
        missing = [
            path
            for path in (
                self.paths.volume,
                self.paths.database,
                self.paths.master_settings,
                self.paths.active_settings,
            )
            if not path.exists()
        ]
        if missing:
            raise BackupError("正式数据缺失：" + "；".join(str(path) for path in missing))
        master = read_json(self.paths.master_settings)
        active = read_json(self.paths.active_settings)
        accounts = master.get("accounts_urls")
        if not isinstance(accounts, list):
            raise BackupError("settings_master.json 缺少 accounts_urls 数组。")
        sqlite_quick_check(self.paths.database)
        return {
            "master_accounts": len(accounts),
            "active_accounts": len(active.get("accounts_urls", [])),
            "database_quick_check": "ok",
        }

    def create_critical_snapshot(
        self,
        category: str,
        metadata: dict[str, Any] | None = None,
        *,
        keep_latest: int | None = None,
        context: OperationContext | None = None,
    ) -> Path:
        """Back up only the unique settings and database files.

        This is the safe default for frequent automatic checkpoints.  It
        deliberately excludes Cache, Data, logs and nested settings_backups
        from the downloader Volume.
        """

        return self._create_snapshot(
            category,
            metadata,
            scope="critical",
            keep_latest=keep_latest,
            context=context,
        )

    def create_full_snapshot(
        self,
        category: str,
        metadata: dict[str, Any] | None = None,
        *,
        keep_latest: int | None = None,
        context: OperationContext | None = None,
    ) -> Path:
        """Create an explicitly requested complete Volume snapshot."""

        return self._create_snapshot(
            category,
            metadata,
            scope="full",
            keep_latest=keep_latest,
            context=context,
        )

    def create_startup_snapshot(
        self,
        category: str = "Startup",
        metadata: dict[str, Any] | None = None,
        *,
        keep_latest: int = STARTUP_KEEP_LATEST,
        context: OperationContext | None = None,
    ) -> Path:
        """Create the V0.1.7 six-file Startup snapshot.

        The legacy three-file ``create_critical_snapshot`` contract remains
        available to V0.1.6 operations.  This method is the only entry that
        adds observation data and emits manifest schema 3.
        """

        if keep_latest < 1:
            raise BackupError("Startup 备份保留数量必须至少为 1。")
        self.last_startup_cleanup_error = None
        watchlist = WatchlistService(self.paths)
        live_state = self.validate_live_data()
        try:
            document, watermark, control = watchlist.validate_all()
        except WatchlistError as exc:
            raise BackupError(f"观察数据校验失败：{exc.public_message}") from exc
        sources = self._startup_sources()
        self._startup_space_preflight(sources)
        category_name = _safe_category(category)
        category_root = self.paths.backups / category_name
        category_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        final = category_root / stamp
        temp = category_root / f".{stamp}.{uuid.uuid4().hex}.tmp"
        if final.exists():
            raise BackupError(f"备份目标已存在：{final}")
        try:
            temp.mkdir(parents=True)
            for relative, source in sources.items():
                if context is not None and source.resolve() != self.paths.database.resolve():
                    context.raise_if_cancelled()
                target = temp / PurePosixPath(relative)
                if source.resolve() == self.paths.database.resolve():
                    if context is not None:
                        context.enter_critical_phase()
                    sqlite_backup(source, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)

            self._validate_startup_copies(temp, document, watermark, control)
            entries: list[dict[str, Any]] = []
            for relative in sorted(sources):
                copied = temp / PurePosixPath(relative)
                entries.append(
                    {
                        "path": relative,
                        "size": copied.stat().st_size,
                        "sha256": sha256_file(copied),
                    }
                )
            manifest = {
                "schema": self.STARTUP_MANIFEST_SCHEMA,
                "category": category_name,
                "scope": "startup",
                "complete": True,
                "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "source_root": str(self.paths.root),
                "source_volume": str(self.paths.volume),
                "source_engine": str(self.paths.engine_exe),
                "managed_data_schema": {
                    "watchlist": WATCHLIST_SCHEMA_VERSION,
                    "watchlist_w_watermark": WATERMARK_SCHEMA_VERSION,
                    "watchlist_control": CONTROL_SCHEMA_VERSION,
                },
                "validation": {
                    **live_state,
                    "database_quick_check": "ok",
                    "watchlist_revision": document["revision"],
                    "watchlist_next_w_id": document["next_w_id"],
                    "watchlist_write_gate": control["write_gate"],
                    "watchlist_initialization": control["initialization_state"],
                    "watermark_next_w_id": watermark["next_w_id"],
                },
                "metadata": metadata or {},
                "files": entries,
            }
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
            if parsed != manifest:
                raise BackupError("Startup manifest 复读校验失败。")
            os.replace(temp, final)
            if not watchlist.has_unresolved_recovery():
                try:
                    self._prune_startup_snapshots(category_root, keep_latest, final)
                except Exception as exc:
                    # The complete new snapshot is already committed.  A
                    # cleanup failure must not turn that durable success into
                    # a false backup failure or delete older evidence.
                    self.last_startup_cleanup_error = str(exc)[:1000]
            return final
        except Exception as exc:
            shutil.rmtree(temp, ignore_errors=True)
            if isinstance(exc, TaskCancelled):
                raise
            if isinstance(exc, BackupError):
                raise
            raise BackupError(f"创建 Startup 六文件快照失败：{exc}") from exc

    def restore_startup_snapshot(self, snapshot: Path, *, locked: bool = False) -> None:
        """Restore a validated Startup schema-3 snapshot without rollbacking W metadata."""

        guard = nullcontext() if locked else critical_section(self.paths.lock_file, timeout=10.0)
        with guard:
            self._restore_startup_snapshot(snapshot)

    def _restore_startup_snapshot(self, snapshot: Path) -> None:

        manifest, entries = self._validate_startup_manifest(snapshot)
        if manifest.get("category") != "Startup" or manifest["scope"] != "startup":
            raise BackupError("指定快照不是 Startup 快照。")
        expected = set(self._startup_sources())
        if set(entries) != expected:
            raise BackupError("Startup 快照文件白名单不完整。")

        current_watchlist = WatchlistService(self.paths)
        try:
            current_watermark = current_watchlist.load_watermark()
        except WatchlistError:
            current_watermark = None
        try:
            current_control = current_watchlist.load_control()
        except WatchlistError:
            current_control = None
        try:
            snapshot_data = validate_document(
                read_json(snapshot / "Data" / "watchlist.json")
            )
            snapshot_watermark = validate_watermark(
                read_json(snapshot / "Data" / "watchlist_w_watermark.json")
            )
            snapshot_control = validate_control(
                read_json(snapshot / "Data" / "watchlist_control.json")
            )
        except (OSError, JsonFileError, WatchlistError) as exc:
            raise BackupError("Startup 恢复观察数据校验失败。") from exc

        if current_watermark is not None:
            merged_next = max(current_watermark["next_w_id"], snapshot_watermark["next_w_id"])
        else:
            merged_next = snapshot_watermark["next_w_id"]
        snapshot_data["next_w_id"] = max(snapshot_data["next_w_id"], merged_next)
        snapshot_data = validate_document(snapshot_data)
        if current_control is not None:
            if current_control["installation_id"] != snapshot_control["installation_id"]:
                raise BackupError("Startup 恢复发现 installation_id 冲突。")
            merged_receipts = _merge_receipts(
                current_control["request_receipts"], snapshot_control["request_receipts"]
            )
            restored_control = dict(current_control)
            restored_control["request_receipts"] = merged_receipts
            restored_control["write_gate"] = "blocked"
        else:
            restored_control = snapshot_control
            restored_control["write_gate"] = "blocked"
        restored_watermark = {"schema_version": WATERMARK_SCHEMA_VERSION, "next_w_id": merged_next}
        validate_control(restored_control)

        protection = (
            self.paths.backups
            / "Recovery"
            / datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        )
        protection.mkdir(parents=True, exist_ok=True)
        destinations = self._startup_sources()
        replaced: list[tuple[Path, Path | None]] = []
        try:
            for relative, destination in destinations.items():
                source = snapshot / PurePosixPath(relative)
                backup_target: Path | None = None
                if destination.is_file():
                    backup_target = protection / PurePosixPath(relative)
                    backup_target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, backup_target)
                temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.restore")
                temp.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, temp)
                if sha256_file(source) != sha256_file(temp):
                    temp.unlink(missing_ok=True)
                    raise BackupError(f"Startup 恢复临时副本校验失败：{relative}")
                os.replace(temp, destination)
                replaced.append((destination, backup_target))
            write_json_atomic(self.paths.watchlist, snapshot_data)
            write_json_atomic(self.paths.watchlist_w_watermark, restored_watermark)
            write_json_atomic(self.paths.watchlist_control, restored_control)
            WatchlistService(self.paths).validate_all()
        except Exception as exc:
            for destination, backup_target in reversed(replaced):
                if backup_target is None:
                    destination.unlink(missing_ok=True)
                    continue
                restore_temp = destination.with_name(
                    f".{destination.name}.{uuid.uuid4().hex}.rollback"
                )
                try:
                    shutil.copy2(backup_target, restore_temp)
                    os.replace(restore_temp, destination)
                finally:
                    restore_temp.unlink(missing_ok=True)
            if isinstance(exc, BackupError):
                raise
            raise BackupError("Startup 恢复失败，已保留当前文件或保护副本。") from exc

    def _startup_sources(self) -> dict[str, Path]:
        sources = {
            f"Volume/{name}": self.paths.volume / name for name in self.CRITICAL_FILENAMES
        }
        sources.update(
            {
                "Data/watchlist.json": self.paths.watchlist,
                "Data/watchlist_w_watermark.json": self.paths.watchlist_w_watermark,
                "Data/watchlist_control.json": self.paths.watchlist_control,
            }
        )
        return sources

    def _startup_space_preflight(self, sources: dict[str, Path]) -> None:
        try:
            total = sum(path.stat().st_size for path in sources.values())
            required = 2 * total + max(64 * 1024 * 1024, total // 10)
            available = shutil.disk_usage(self.paths.backups.parent).free
        except OSError as exc:
            raise BackupError("无法完成 Startup 备份空间预检。") from exc
        if available < required:
            raise BackupError("Startup 备份空间不足，未删除旧快照。")

    @staticmethod
    def _validate_startup_copies(
        temp: Path,
        document: dict[str, Any],
        watermark: dict[str, Any],
        control: dict[str, Any],
    ) -> None:
        try:
            copied_document = validate_document(read_json(temp / "Data" / "watchlist.json"))
            copied_watermark = validate_watermark(
                read_json(temp / "Data" / "watchlist_w_watermark.json")
            )
            copied_control = validate_control(read_json(temp / "Data" / "watchlist_control.json"))
        except (OSError, JsonFileError, WatchlistError) as exc:
            raise BackupError("Startup 观察数据快照校验失败。") from exc
        if copied_document != document or copied_watermark != watermark or copied_control != control:
            raise BackupError("Startup 观察数据快照复读不一致。")
        sqlite_quick_check(temp / "Volume" / "DouK-Downloader.db")

    @classmethod
    def _prune_startup_snapshots(
        cls,
        category_root: Path,
        keep_latest: int,
        preserve: Path,
    ) -> None:
        snapshots: list[Path] = []
        quarantine = category_root / "_quarantine"
        for path in sorted(category_root.iterdir(), key=lambda item: item.name, reverse=True):
            if not path.is_dir() or path.name == "_quarantine":
                continue
            try:
                manifest, entries = cls._validate_startup_manifest(path)
                if manifest.get("category") != "Startup" or set(entries) != {
                    "Volume/settings_master.json",
                    "Volume/settings.json",
                    "Volume/DouK-Downloader.db",
                    "Data/watchlist.json",
                    "Data/watchlist_w_watermark.json",
                    "Data/watchlist_control.json",
                }:
                    raise BackupError("Startup 快照文件白名单不完整。")
            except Exception:
                cls._quarantine_startup_snapshot(path, quarantine)
                continue
            snapshots.append(path)
        keep = {path.resolve() for path in snapshots[:keep_latest]}
        keep.add(preserve.resolve())
        for snapshot in snapshots:
            if snapshot.resolve() not in keep:
                shutil.rmtree(snapshot)

    @staticmethod
    def _quarantine_startup_snapshot(path: Path, quarantine: Path) -> None:
        quarantine.mkdir(parents=True, exist_ok=True)
        target = quarantine / path.name
        if target.exists():
            target = quarantine / f"{path.name}-{uuid.uuid4().hex}"
        os.replace(path, target)

    @classmethod
    def _validate_startup_manifest(
        cls, snapshot: Path
    ) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
        if not snapshot.is_dir() or snapshot.is_symlink():
            raise BackupError("Startup 快照目录无效。")
        try:
            manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise BackupError("Startup manifest 不可读取。") from exc
        if not isinstance(manifest, dict) or manifest.get("schema") != cls.STARTUP_MANIFEST_SCHEMA:
            raise BackupError("Startup manifest 版本不受支持。")
        if manifest.get("complete") is not True or manifest.get("scope") != "startup":
            raise BackupError("Startup manifest 未完成。")
        files = manifest.get("files")
        if not isinstance(files, list):
            raise BackupError("Startup manifest 文件列表无效。")
        entries: dict[str, dict[str, Any]] = {}
        for item in files:
            if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
                raise BackupError("Startup manifest 文件项无效。")
            relative = item["path"]
            parsed = PurePosixPath(relative) if isinstance(relative, str) else PurePosixPath(".")
            if (
                not isinstance(relative, str)
                or parsed.is_absolute()
                or ".." in parsed.parts
                or not parsed.parts
                or str(parsed) != relative
                or relative in entries
                or not isinstance(item["sha256"], str)
                or not _SHA256_RE.fullmatch(item["sha256"])
                or not _is_nonnegative_int(item["size"])
                or parsed.parts[0] not in {"Volume", "Data"}
            ):
                raise BackupError("Startup manifest 含越界或非法文件项。")
            source = snapshot / parsed
            if source.is_symlink() or not source.is_file():
                raise BackupError("Startup manifest 引用的文件不存在或越界。")
            if source.stat().st_size != item["size"] or sha256_file(source) != item["sha256"]:
                raise BackupError("Startup manifest 文件校验失败。")
            entries[relative] = item
        return manifest, entries

    def _create_snapshot(
        self,
        category: str,
        metadata: dict[str, Any] | None,
        *,
        scope: str,
        keep_latest: int | None,
        context: OperationContext | None,
    ) -> Path:
        state = self.validate_live_data()
        if scope not in {"critical", "full"}:
            raise BackupError(f"备份范围无效：{scope}")
        if keep_latest is not None and keep_latest < 1:
            raise BackupError("自动备份保留数量必须至少为 1。")
        category_name = _safe_category(category)
        category_root = self.paths.backups / category_name
        category_root.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        final = category_root / stamp
        temp = category_root / f".{stamp}.{uuid.uuid4().hex}.tmp"
        if final.exists():
            raise BackupError(f"备份目标已存在：{final}")
        try:
            volume_target = temp / "Volume"
            volume_target.mkdir(parents=True)
            sources = (
                sorted(self.paths.volume.rglob("*"))
                if scope == "full"
                else [self.paths.volume / name for name in self.CRITICAL_FILENAMES]
            )
            for source in sources:
                if context is not None and source.resolve() != self.paths.database.resolve():
                    context.raise_if_cancelled()
                relative = source.relative_to(self.paths.volume)
                target = volume_target / relative
                if source.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if source.resolve() == self.paths.database.resolve():
                    if context is not None:
                        context.enter_critical_phase()
                    sqlite_backup(source, target)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(source, target)

            file_entries: list[dict[str, Any]] = []
            for file_path in sorted(path for path in volume_target.rglob("*") if path.is_file()):
                file_entries.append(
                    {
                        "path": file_path.relative_to(temp).as_posix(),
                        "size": file_path.stat().st_size,
                        "sha256": sha256_file(file_path),
                    }
                )
            manifest = {
                "schema": 2,
                "category": category_name,
                "scope": scope,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "source_volume": str(self.paths.volume),
                "source_engine": str(self.paths.engine_exe),
                "validation": state,
                "metadata": metadata or {},
                "files": file_entries,
            }
            manifest_path = temp / "manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
            if parsed != manifest:
                raise BackupError("备份清单复读校验失败。")
            os.replace(temp, final)
            if keep_latest is not None:
                self._prune_completed_snapshots(
                    category_root,
                    keep_latest=keep_latest,
                    preserve=final,
                )
            return final
        except Exception as exc:
            shutil.rmtree(temp, ignore_errors=True)
            if isinstance(exc, TaskCancelled):
                raise
            if isinstance(exc, BackupError):
                raise
            raise BackupError(f"创建永久备份失败：{exc}") from exc

    @staticmethod
    def _prune_completed_snapshots(
        category_root: Path,
        *,
        keep_latest: int,
        preserve: Path,
    ) -> None:
        """Prune only completed timestamp snapshots in one known category."""

        snapshots = sorted(
            (
                path
                for path in category_root.iterdir()
                if path.is_dir()
                and not path.name.startswith(".")
                and (path / "manifest.json").is_file()
            ),
            key=lambda path: path.name,
            reverse=True,
        )
        keep = {path.resolve() for path in snapshots[:keep_latest]}
        keep.add(preserve.resolve())
        for snapshot in snapshots:
            if snapshot.resolve() in keep:
                continue
            shutil.rmtree(snapshot)

    def restore_named_files(self, snapshot: Path, filenames: tuple[str, ...]) -> None:
        volume_source = snapshot / "Volume"
        if not volume_source.is_dir():
            raise BackupError(f"备份中缺少 Volume：{snapshot}")
        for filename in filenames:
            source = volume_source / filename
            destination = self.paths.volume / filename
            if not source.is_file():
                raise BackupError(f"备份中缺少文件：{source}")
            temp = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.restore")
            shutil.copy2(source, temp)
            if sha256_file(source) != sha256_file(temp):
                temp.unlink(missing_ok=True)
                raise BackupError(f"恢复临时副本校验失败：{filename}")
            os.replace(temp, destination)


def _safe_category(category: str) -> str:
    cleaned = "".join(char for char in category if char.isalnum() or char in "_-" )
    if not cleaned:
        raise BackupError("备份类别无效。")
    return cleaned


def _is_nonnegative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _merge_receipts(
    current: list[dict[str, Any]], restored: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged = {item["request_id"]: copy_receipt(item) for item in current}
    for item in restored:
        request_id = item["request_id"]
        existing = merged.get(request_id)
        if existing is not None and (
            existing["payload_digest"] != item["payload_digest"]
            or existing["normalized_url_digest"] != item["normalized_url_digest"]
            or existing["next_review_at"] != item["next_review_at"]
        ):
            raise BackupError("Startup 恢复发现 request_id 摘要冲突。")
        if existing is None:
            merged[request_id] = copy_receipt(item)
        elif _receipt_rank(item["outcome"]) > _receipt_rank(existing["outcome"]):
            merged[request_id] = copy_receipt(item)
    return list(merged.values())


def _receipt_rank(outcome: str) -> int:
    return {
        "pending": 10,
        "delete_pending": 10,
        "exists": 20,
        "formal_exists": 20,
        "committed": 30,
        "deleted": 40,
    }.get(outcome, 0)


def copy_receipt(value: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(value, ensure_ascii=False))
