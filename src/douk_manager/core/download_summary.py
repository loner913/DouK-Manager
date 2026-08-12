from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class PlannedAccount:
    task_index: int
    a_number: int
    mark: str


@dataclass(frozen=True)
class NativeLogState:
    path: Path
    size: int
    mtime_ns: int


class SummaryInputError(ValueError):
    pass


def freeze_planned_accounts(document: dict) -> tuple[PlannedAccount, ...]:
    accounts = document.get("accounts_urls")
    if not isinstance(accounts, list):
        raise SummaryInputError("settings.json 缺少 accounts_urls 数组。")
    planned: list[PlannedAccount] = []
    for position, raw in enumerate(accounts, start=1):
        if not isinstance(raw, dict):
            raise SummaryInputError(f"settings.json 的 A{position} 不是对象。")
        url = str(raw.get("url", "")).strip()
        if not raw.get("enable", True) or not url:
            continue
        mark = str(raw.get("mark", "")).strip()
        if re.match(rf"^A{position}(?!\d)", mark, re.IGNORECASE) is None:
            raise SummaryInputError(f"settings.json 的 A{position} mark 与数组位置不一致。")
        planned.append(PlannedAccount(len(planned) + 1, position, mark))
    if not planned:
        raise SummaryInputError("settings.json 没有启用且 URL 有效的账号。")
    return tuple(planned)


def snapshot_native_logs(log_dir: Path) -> tuple[NativeLogState, ...]:
    if not log_dir.is_dir():
        return ()
    states = []
    for path in sorted(log_dir.glob("*.log"), key=lambda item: item.name.casefold()):
        try:
            stat = path.stat()
        except OSError:
            continue
        states.append(NativeLogState(path.resolve(), stat.st_size, stat.st_mtime_ns))
    return tuple(states)
