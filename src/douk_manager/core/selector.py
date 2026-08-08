from __future__ import annotations

import re
from dataclasses import dataclass


class SelectionError(ValueError):
    def __init__(self, message: str, invalid_parts: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.invalid_parts = invalid_parts


@dataclass(frozen=True)
class Selection:
    numbers: tuple[int, ...]
    duplicate_numbers: tuple[int, ...]
    normalized: str

    @property
    def count(self) -> int:
        return len(self.numbers)


_PART_RE = re.compile(r"A?(\d+)(?:-A?(\d+))?", re.IGNORECASE)


def _normalize(expression: str) -> str:
    text = expression.strip().upper()
    for separator in ("，", "；", ";", "、", "\n", "\r", "\t"):
        text = text.replace(separator, ",")
    for dash in ("–", "—", "－", "~", "～", "至"):
        text = text.replace(dash, "-")
    text = re.sub(r"\s+", "", text)
    text = re.sub(r",+", ",", text).strip(",")
    return text


def parse_selection(expression: str, total: int) -> Selection:
    if total < 1:
        raise SelectionError("主档没有可选择的账号位置。")
    normalized = _normalize(expression)
    if not normalized:
        raise SelectionError("请输入账号编号或范围。")

    chosen: set[int] = set()
    duplicates: set[int] = set()
    invalid: list[str] = []
    problems: list[str] = []

    for part in normalized.split(","):
        match = _PART_RE.fullmatch(part)
        if not match:
            invalid.append(part)
            continue
        start = int(match.group(1))
        end = int(match.group(2) or start)
        if start < 1 or end < 1:
            invalid.append(part)
            problems.append(f"{part} 小于最小编号 A1")
            continue
        if end < start:
            invalid.append(part)
            problems.append(f"{part} 的结束编号小于开始编号")
            continue
        if start > total or end > total:
            invalid.append(part)
            problems.append(f"{part} 超过当前最大编号 A{total}")
            continue
        for number in range(start, end + 1):
            if number in chosen:
                duplicates.add(number)
            chosen.add(number)

    if invalid:
        detail = "；".join(problems) if problems else "包含无法识别的内容"
        raise SelectionError(f"账号表达式无效：{detail}", tuple(invalid))
    if not chosen:
        raise SelectionError("没有解析出有效账号。")
    return Selection(tuple(sorted(chosen)), tuple(sorted(duplicates)), normalized)


def compact_numbers(numbers: tuple[int, ...] | list[int]) -> str:
    values = sorted(set(numbers))
    if not values:
        return ""
    groups: list[str] = []
    start = previous = values[0]
    for number in values[1:] + [None]:  # type: ignore[list-item]
        if number is not None and number == previous + 1:
            previous = number
            continue
        groups.append(f"A{start}" if start == previous else f"A{start}-A{previous}")
        if number is not None:
            start = previous = number
    return ",".join(groups)


def split_batches(start: int, end: int, batch_size: int) -> tuple[tuple[int, int], ...]:
    if start < 1:
        raise SelectionError("批次开始编号必须至少为 A1。")
    if end < start:
        raise SelectionError("批次结束编号不能小于开始编号。")
    if batch_size < 1:
        raise SelectionError("每批账号数必须大于0。")
    result: list[tuple[int, int]] = []
    current = start
    while current <= end:
        batch_end = min(end, current + batch_size - 1)
        result.append((current, batch_end))
        current = batch_end + 1
    return tuple(result)

