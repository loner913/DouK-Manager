from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
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


@dataclass(frozen=True)
class NativeLogSegment:
    path: Path
    offset: int
    length: int


@dataclass(frozen=True)
class LocatedNativeLogs:
    segments: tuple[NativeLogSegment, ...]
    method: str
    reliable: bool
    reason: str = ""


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


def locate_native_logs(
    before: tuple[NativeLogState, ...],
    log_dir: Path,
    started_at: datetime,
    ended_at: datetime,
    planned_count: int,
) -> LocatedNativeLogs:
    del ended_at
    before_by_path = {
        str(state.path.resolve()).casefold(): state
        for state in before
    }
    earliest_mtime = (started_at - timedelta(seconds=2)).timestamp()
    candidates: list[NativeLogSegment] = []
    if log_dir.is_dir():
        for path in sorted(log_dir.glob("*.log"), key=lambda item: item.name.casefold()):
            try:
                resolved = path.resolve()
                stat = resolved.stat()
            except OSError:
                continue
            if stat.st_mtime < earliest_mtime:
                continue
            state = before_by_path.get(str(resolved).casefold())
            if state is None:
                candidates.append(NativeLogSegment(resolved, 0, stat.st_size))
            elif stat.st_size > state.size:
                candidates.append(
                    NativeLogSegment(resolved, state.size, stat.st_size - state.size)
                )

    if len(candidates) == 1:
        return LocatedNativeLogs(tuple(candidates), "size-delta", True)
    if not candidates:
        return LocatedNativeLogs((), "size-delta", False, "未找到本次新增或增长的原生日志。")

    anchor = f"共有 {planned_count} 个账号的作品等待下载"
    evaluations = [_evaluate_log_segment(segment, anchor) for segment in candidates]
    scores = [evaluation[0] for evaluation in evaluations]
    highest_score = max(scores)
    winning_index = scores.index(highest_score)
    if (
        highest_score > 0
        and scores.count(highest_score) == 1
        and evaluations[winning_index][1]
    ):
        return LocatedNativeLogs(
            (candidates[winning_index],), "size-delta-anchor", True
        )
    return LocatedNativeLogs(
        (),
        "size-delta-anchor",
        False,
        "存在多个候选原生日志，无法唯一确定本次日志。",
    )


def _evaluate_log_segment(
    segment: NativeLogSegment, planned_count_anchor: str
) -> tuple[int, bool]:
    try:
        with segment.path.open("rb") as handle:
            handle.seek(segment.offset)
            content = handle.read(min(segment.length, 64 * 1024)).decode(
                "utf-8-sig", errors="replace"
            )
    except OSError:
        return 0, False
    has_planned_count_anchor = planned_count_anchor in content
    score = 1 if has_planned_count_anchor else 0
    if "开始处理第 1 个账号" in content:
        score += 1
    return score, has_planned_count_anchor
