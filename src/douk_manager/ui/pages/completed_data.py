"""Read-only completed-task projection over the existing log parser."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from douk_manager.core.download_summary import AccountStatus
from douk_manager.core.result_dashboard import (
    ResultDashboardService, ResultDashboardSnapshot, ResultDashboardError,
    DashboardFileChangedError,
)


@dataclass(frozen=True)
class CompletedData:
    snapshot: ResultDashboardSnapshot | None
    labels: tuple[str, ...]
    series: tuple[tuple[str, tuple[int, ...], str], ...]
    notice: str
    trend_incomplete: bool


def read_completed(service: ResultDashboardService, today: date, context=None) -> CompletedData:
    index = service.task_index(context=context)
    signature = tuple(entry.fingerprint for entry in index.entries)
    days = tuple(today - timedelta(days=i) for i in range(6, -1, -1))
    totals = [[0] * 7 for _ in range(3)]
    latest = None
    incomplete = False
    notices = set()
    seen = set()
    entries = sorted(index.entries, key=lambda e: (
        e.ended_at is not None, e.ended_at or datetime.min,
        e.fingerprint.normalized_path,
    ), reverse=True)
    for entry in entries:
        if context is not None:
            context.raise_if_cancelled()
        key = entry.fingerprint.normalized_path
        if key in seen:
            continue
        seen.add(key)
        if not entry.displayable or entry.ended_at is None:
            notices.add("存在汇总中或不可用的任务日志")
            incomplete = True
            continue
        in_week = entry.ended_at.date() in days
        if latest is not None and not in_week:
            continue
        try:
            snapshot = service.selected_task(entry.task_log,
                expected_fingerprint=entry.fingerprint, context=context)
        except (ResultDashboardError, OSError, UnicodeError):
            notices.add("部分任务日志读取失败")
            incomplete = incomplete or in_week
            continue
        if latest is None:
            latest = snapshot
        if in_week:
            values = (snapshot.planned_count, snapshot.count_for(AccountStatus.DOWNLOADED),
                      snapshot.count_for(AccountStatus.ERROR))
            if any(v is None for v in values) or not snapshot.reliable:
                incomplete = True
            else:
                position = days.index(entry.ended_at.date())
                for row, value in zip(totals, values):
                    row[position] += value
    # Unknown evidence must not become zero-valued points or complete-looking lines.
    series = () if incomplete or latest is None else tuple(
        (name, tuple(values), color) for (name, color), values in zip(
            (("计划账号", "#3287FF"), ("有新作品", "#19C37D"), ("处理异常", "#FF5967")), totals)
    )
    if tuple(entry.fingerprint for entry in service.task_index(context=context).entries) != signature:
        raise DashboardFileChangedError("任务日志在汇总期间变化，请刷新")
    return CompletedData(latest, tuple(day.strftime("%m-%d") for day in days),
                         series, "；".join(sorted(notices)), incomplete)
