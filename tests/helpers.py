from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from douk_manager.config import AppConfig, ManagedPaths


def make_test_paths(base: Path, account_count: int = 8) -> ManagedPaths:
    manager_root = base / "manager"
    engine_root = base / "engine"
    engine_exe = engine_root / "main.exe"
    volume = engine_root / "_internal" / "Volume"
    video = base / "videos"
    index = base / "index"
    volume.mkdir(parents=True)
    video.mkdir()
    index.mkdir()
    engine_exe.write_bytes(b"test executable")
    accounts = []
    for number in range(1, account_count + 1):
        accounts.append(
            {
                "mark": f"A{number}account{number}",
                "url": "" if number == 4 else f"https://www.douyin.com/user/{number}",
                "tab": "post",
                "earliest": "",
                "latest": "",
                "enable": number % 2 == 1,
            }
        )
    document = {
        "accounts_urls": accounts,
        "run_command": "",
        "cookie": "",
    }
    (volume / "settings_master.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (volume / "settings.json").write_text(
        json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    with sqlite3.connect(volume / "DouK-Downloader.db") as connection:
        connection.execute("CREATE TABLE records (id INTEGER PRIMARY KEY, value TEXT)")
        connection.execute("INSERT INTO records(value) VALUES ('safe')")
    config = AppConfig(
        engine_exe=str(engine_exe),
        video_root=str(video),
        index_root=str(index),
        old_screenshot_dir=str(base / "old_collector" / "账号页面截图"),
    )
    paths = ManagedPaths.from_config(config, manager_root)
    paths.ensure_manager_directories()
    return paths

