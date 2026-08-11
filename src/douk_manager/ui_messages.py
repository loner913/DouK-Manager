from __future__ import annotations

from datetime import datetime
from typing import Iterable


def _message_lines(messages: Iterable[object]) -> list[str]:
    lines: list[str] = []
    for message in messages:
        for raw_line in str(message).splitlines():
            line = raw_line.strip()
            if line:
                lines.append(line)
    return lines


def format_information(
    *messages: object,
    at: datetime | None = None,
    merge: bool = False,
    include_date: bool = False,
) -> str:
    """Timestamp semantic lines; merge only when the caller marks one event."""

    lines = _message_lines(messages)
    if not lines:
        return ""
    moment = at or datetime.now()
    pattern = "%Y-%m-%d %H:%M:%S" if include_date else "%H:%M:%S"
    prefix = f"[{moment.strftime(pattern)}] "
    if merge:
        merged_parts = [line.rstrip("；;。.!！？?") for line in lines[:-1]]
        merged_parts.append(lines[-1])
        content = "；".join(merged_parts)
        return prefix + content
    return "\n".join(prefix + line for line in lines)
