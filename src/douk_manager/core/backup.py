from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Any

from douk_manager.config import ManagedPaths
from douk_manager.core.json_store import read_json


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

    def __init__(self, paths: ManagedPaths) -> None:
        self.paths = paths

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
        )

    def create_full_snapshot(
        self,
        category: str,
        metadata: dict[str, Any] | None = None,
        *,
        keep_latest: int | None = None,
    ) -> Path:
        """Create an explicitly requested complete Volume snapshot."""

        return self._create_snapshot(
            category,
            metadata,
            scope="full",
            keep_latest=keep_latest,
        )

    def _create_snapshot(
        self,
        category: str,
        metadata: dict[str, Any] | None,
        *,
        scope: str,
        keep_latest: int | None,
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
                relative = source.relative_to(self.paths.volume)
                target = volume_target / relative
                if source.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                if source.resolve() == self.paths.database.resolve():
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
