from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


class JsonFileError(RuntimeError):
    pass


def read_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8-sig")
        value = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise JsonFileError(f"无法读取有效 JSON：{path}：{exc}") from exc
    if not isinstance(value, dict):
        raise JsonFileError(f"JSON 顶层必须是对象：{path}")
    return value


def write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    """在同一目录写临时文件、复读校验，再原子替换正式文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        verified = read_json(temp)
        if verified != value:
            raise JsonFileError(f"临时 JSON 复读校验不一致：{temp}")
        os.replace(temp, path)
    except Exception:
        try:
            temp.unlink(missing_ok=True)
        except OSError:
            pass
        raise

