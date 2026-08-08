from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
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
        with sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True) as connection:
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
        with sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True) as src:
            with sqlite3.connect(destination) as dst:
                src.backup(dst)
        sqlite_quick_check(destination)
    except sqlite3.Error as exc:
        destination.unlink(missing_ok=True)
        raise BackupError(f"数据库备份失败：{source}：{exc}") from exc


class BackupService:
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

    def create_snapshot(
        self, category: str, metadata: dict[str, Any] | None = None
    ) -> Path:
        state = self.validate_live_data()
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
            for source in sorted(self.paths.volume.rglob("*")):
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
                "schema": 1,
                "category": category_name,
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
            return final
        except Exception as exc:
            shutil.rmtree(temp, ignore_errors=True)
            if isinstance(exc, BackupError):
                raise
            raise BackupError(f"创建永久备份失败：{exc}") from exc

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

