from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from douk_manager.core.json_store import read_json, write_json_atomic


DEFAULT_ENGINE_EXE = (
    "F:\\DouK-Downloader_Custom_50_150\\"
    "DouK-Downloader_Windows_X64_20260626\\main.exe"
)


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def application_root() -> Path:
    override = os.environ.get("DOUK_MANAGER_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return project_root() / "runtime"


def resource_path(relative: str) -> Path:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    else:
        base = project_root()
    return base / relative


@dataclass
class AppConfig:
    engine_exe: str = DEFAULT_ENGINE_EXE
    video_root: str = r"F:\DouK-Downloader"
    index_root: str = r"F:\Douk videos"
    old_screenshot_dir: str = (
        r"F:\DouK-Downloader_Custom_50_150\DouK accounts collector\账号页面截图"
    )
    batch_accounts: int = 50
    rest_seconds: int = 150
    collector_port: int = 8765
    collector_token: str = "DOUK_COLLECTOR_V248_20260719"
    screenshot_post_mode: str = "queue"
    index_post_mode: str = "queue"
    cleanup_after_index: bool = True

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        if not path.exists():
            return cls()
        raw = read_json(path)
        allowed = cls.__dataclass_fields__.keys()
        values = {key: value for key, value in raw.items() if key in allowed}
        return cls(**values)

    def save(self, path: Path) -> None:
        write_json_atomic(path, asdict(self))


@dataclass(frozen=True)
class ManagedPaths:
    root: Path
    data: Path
    config_file: Path
    logs: Path
    backups: Path
    tasks: Path
    updates: Path
    lock_file: Path
    screenshot_inbox: Path
    collector_data: Path
    collector_excel: Path
    engine_exe: Path
    engine_root: Path
    volume: Path
    master_settings: Path
    active_settings: Path
    database: Path
    video_root: Path
    index_root: Path

    @classmethod
    def from_config(cls, config: AppConfig, root: Path | None = None) -> "ManagedPaths":
        app_root = (root or application_root()).resolve()
        data = app_root / "Data"
        engine_exe = Path(config.engine_exe)
        engine_root = engine_exe.parent
        volume = engine_root / "_internal" / "Volume"
        collector_data = data / "Collector"
        return cls(
            root=app_root,
            data=data,
            config_file=data / "app_config.json",
            logs=app_root / "Logs",
            backups=app_root / "Backups",
            tasks=data / "Tasks",
            updates=app_root / "Updates",
            lock_file=data / ".douk_manager.lock",
            screenshot_inbox=data / "Screenshots" / "Inbox",
            collector_data=collector_data,
            collector_excel=collector_data / "录制名单.xlsx",
            engine_exe=engine_exe,
            engine_root=engine_root,
            volume=volume,
            master_settings=volume / "settings_master.json",
            active_settings=volume / "settings.json",
            database=volume / "DouK-Downloader.db",
            video_root=Path(config.video_root),
            index_root=Path(config.index_root),
        )

    def ensure_manager_directories(self) -> None:
        for path in (
            self.data,
            self.logs,
            self.backups,
            self.tasks,
            self.updates,
            self.screenshot_inbox,
            self.collector_data,
        ):
            path.mkdir(parents=True, exist_ok=True)

    def health(self) -> dict[str, bool]:
        return {
            "engine_exe": self.engine_exe.is_file(),
            "volume": self.volume.is_dir(),
            "master_settings": self.master_settings.is_file(),
            "active_settings": self.active_settings.is_file(),
            "database": self.database.is_file(),
            "video_root": self.video_root.is_dir(),
            "index_root": self.index_root.is_dir(),
            "collector_excel": self.collector_excel.is_file(),
        }


def update_config(config: AppConfig, values: dict[str, Any]) -> AppConfig:
    allowed = config.__dataclass_fields__.keys()
    merged = asdict(config)
    merged.update({key: value for key, value in values.items() if key in allowed})
    return AppConfig(**merged)
