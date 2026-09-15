"""Synthetic task logs produced by the application's own log formatter."""

from datetime import datetime, timedelta
from pathlib import Path

from douk_manager.core.download_summary import (
    AccountOutcome, AccountStatus, DownloadSummary, LocatedNativeLogs,
    format_summary_for_task_log,
)
from douk_manager.ui_messages import format_information


def write_task(directory: Path, name: str, ended: datetime, *, count=1200,
               duration=9000, errors=23, anomalies=8, exit_code=0, distribution=None,
               pre_start_errors=()):
    directory.mkdir(parents=True, exist_ok=True)
    states = [AccountStatus.DOWNLOADED] * (count - errors) + [AccountStatus.ERROR] * errors
    if distribution is not None:
        states = [state for state, amount in zip(AccountStatus, distribution) for _ in range(amount)]
        if len(states) != count:
            raise ValueError("Fixture distribution must match planned count")
    outcomes = tuple(AccountOutcome(i, i, state, i <= anomalies)
                     for i, state in enumerate(states, 1))
    summary = DownloadSummary(
        planned_count=count + len(pre_start_errors), started_outcomes=outcomes,
        pre_start_errors=tuple(pre_start_errors), not_started=(),
        primary_status_counts={s: states.count(s) for s in AccountStatus},
        completed_with_anomaly=tuple(range(1, anomalies + 1)),
        complete=exit_code == 0, reliable=True, reasons=(),
        located=LocatedNativeLogs((), "synthetic-fixture", True), exit_code=exit_code,
    )
    start = format_information("Started", "Log scope: one downloader process",
        f"Task template: {name}", f"Selected accounts: {count + len(pre_start_errors)}",
        at=ended - timedelta(seconds=duration), merge=True, include_date=True)
    path = directory / f"DownloadTask_{name}.log"
    path.write_text(start + "\n\n" + format_summary_for_task_log(summary, ended), encoding="utf-8")
    return path
