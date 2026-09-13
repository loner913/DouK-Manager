"""Read-only reminder classification shared by all watchlist views."""

from datetime import datetime, timezone

from .watchlist import WatchlistError, parse_utc


def reminder_kind(record, now=None, local_timezone=None):
    now = now or datetime.now(timezone.utc)
    if record.get("state") != "watching":
        return "inactive"
    value = record.get("next_review_at")
    if not isinstance(value, str):
        return "missing"
    try:
        due = parse_utc(value)
    except (WatchlistError, ValueError, TypeError):
        return "invalid"
    if due > now:
        return "upcoming"
    # Convert each instant separately so OS local DST rules apply to both dates.
    if due.astimezone(local_timezone).date() < now.astimezone(local_timezone).date():
        return "overdue"
    return "due"


def reminder_summary(records, now=None, local_timezone=None):
    now = now or datetime.now(timezone.utc)
    watching = [row for row in records if row.get("state") == "watching"]
    kinds = [reminder_kind(row, now, local_timezone) for row in watching]
    created = [parse_utc(row["created_at"]) for row in watching if row.get("created_at")]
    return {
        "due": kinds.count("due") + kinds.count("overdue"),
        "overdue": kinds.count("overdue"),
        "recovery": sum(row.get("promotion_recovery") is not None for row in watching),
        "oldest": min(created).astimezone(local_timezone).strftime("%Y-%m-%d") if created else "--",
    }
