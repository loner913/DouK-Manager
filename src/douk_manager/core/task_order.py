from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from douk_manager.config import ManagedPaths
from douk_manager.core.json_store import read_json, write_json_atomic
from douk_manager.operation import TaskCancelled

if TYPE_CHECKING:
    from douk_manager.operation import OperationContext


class TaskOrderError(RuntimeError):
    pass


_A_NUMBER = re.compile(r"A\s*0*(\d+)", re.IGNORECASE)
_FALLBACK_SLOT = 2_147_483_647


def _checkpoint(context: OperationContext | None) -> None:
    if context is not None:
        context.raise_if_cancelled()


def task_start_number(
    path: Path,
    *,
    context: OperationContext | None = None,
) -> int:
    """Return the first enabled account position used by a task template.

    Generated task JSON files explicitly store an ``enable`` flag for every
    account.  Reading that data is more reliable than depending on a custom
    filename.  A filename A-number remains a safe fallback for older or
    hand-written templates that cannot be parsed.
    """

    _checkpoint(context)
    enabled_number: int | None = None
    try:
        document = read_json(path)
        accounts = document.get("accounts_urls")
        if isinstance(accounts, list):
            for number, account in enumerate(accounts, start=1):
                if isinstance(account, dict) and bool(account.get("enable", True)):
                    enabled_number = number
                    break
    except TaskCancelled:
        raise
    except Exception:
        pass
    _checkpoint(context)

    if enabled_number is not None:
        return enabled_number

    match = _A_NUMBER.search(path.stem)
    if match:
        return int(match.group(1))
    return _FALLBACK_SLOT


def move_to_index(
    paths: Iterable[Path], source_index: int, target_index: int
) -> tuple[Path, ...]:
    """Move one item to an exact final zero-based position."""

    result = list(paths)
    if not result:
        return ()
    if not 0 <= source_index < len(result):
        raise TaskOrderError("当前高亮任务已经不在列表中，请刷新后重试。")
    target_index = max(0, min(int(target_index), len(result) - 1))
    item = result.pop(source_index)
    result.insert(target_index, item)
    return tuple(result)


class TaskOrderService:
    """Persist queue slots without changing task templates or official data."""

    VERSION = 1

    def __init__(self, paths: ManagedPaths) -> None:
        self.paths = paths
        self.state_path = paths.data / "task_order.json"
        self.last_warning = ""

    def list_tasks(
        self,
        *,
        context: OperationContext | None = None,
    ) -> tuple[Path, ...]:
        _checkpoint(context)
        self.paths.tasks.mkdir(parents=True, exist_ok=True)
        _checkpoint(context)
        tasks = tuple(self.paths.tasks.glob("*.json"))
        _checkpoint(context)
        entries = self._load_entries(context=context)
        task_by_name = {path.name: path for path in tasks}
        surviving = [entry for entry in entries if entry["name"] in task_by_name]
        if len(surviving) != len(entries):
            # Closing deleted slots keeps the remaining manual relative order,
            # while ensuring a later same-name recreation uses its natural A
            # position instead of inheriting a deleted record.
            surviving_slots = sorted(
                task_start_number(
                    task_by_name[entry["name"]],
                    context=context,
                )
                for entry in surviving
            )
            entries = [
                {"name": entry["name"], "slot": surviving_slots[index]}
                for index, entry in enumerate(surviving)
            ]
        saved_slots = {entry["name"]: entry["slot"] for entry in entries}
        saved_ranks = {entry["name"]: rank for rank, entry in enumerate(entries)}

        records: list[tuple[int, int, Any, str, Path]] = []
        for path in tasks:
            _checkpoint(context)
            name = path.name
            if name in saved_slots:
                records.append(
                    (saved_slots[name], 0, saved_ranks[name], name.casefold(), path)
                )
            else:
                # A new task joins its natural A-number slot.  If that slot
                # already contains manually positioned tasks, it follows that
                # existing group instead of disturbing their relative order.
                records.append(
                    (
                        task_start_number(path, context=context),
                        1,
                        name.casefold(),
                        name.casefold(),
                        path,
                    )
                )
            _checkpoint(context)

        _checkpoint(context)
        records.sort(key=lambda record: record[:4])
        _checkpoint(context)
        ordered = tuple(record[4] for record in records)
        normalized = [
            {"name": record[4].name, "slot": int(record[0])} for record in records
        ]
        _checkpoint(context)
        self._save_entries(normalized, context=context)
        return ordered

    def save_manual_order(self, ordered_paths: Iterable[Path]) -> tuple[Path, ...]:
        requested = tuple(Path(path) for path in ordered_paths)
        current = tuple(self.paths.tasks.glob("*.json"))
        requested_names = [path.name for path in requested]
        current_names = {path.name for path in current}
        if len(requested_names) != len(set(requested_names)):
            raise TaskOrderError("任务列表中存在重复项，无法保存顺序。")
        if set(requested_names) != current_names:
            raise TaskOrderError("任务目录内容已经变化，请先刷新任务列表再调整顺序。")

        # First merge new tasks and prune deleted tasks.  Reassigning the
        # resulting sorted slots to the requested rows implements the agreed
        # slot swap: moving A61 above A51 gives A61 slot 51 and A51 slot 61.
        self.list_tasks()
        entries = self._load_entries()
        slot_by_name = {entry["name"]: entry["slot"] for entry in entries}
        slots = sorted(slot_by_name[name] for name in requested_names)
        normalized = [
            {"name": path.name, "slot": int(slots[index])}
            for index, path in enumerate(requested)
        ]
        self._save_entries(normalized)
        return requested

    def restore_natural_order(self) -> tuple[Path, ...]:
        self.paths.tasks.mkdir(parents=True, exist_ok=True)
        ordered = tuple(
            sorted(
                self.paths.tasks.glob("*.json"),
                key=lambda path: (task_start_number(path), path.name.casefold()),
            )
        )
        self._save_entries(
            [
                {"name": path.name, "slot": task_start_number(path)}
                for path in ordered
            ]
        )
        return ordered

    def _load_entries(
        self,
        *,
        context: OperationContext | None = None,
    ) -> list[dict[str, Any]]:
        self.last_warning = ""
        _checkpoint(context)
        if not self.state_path.is_file():
            _checkpoint(context)
            return []
        try:
            document = read_json(self.state_path)
        except TaskCancelled:
            raise
        except Exception as exc:
            _checkpoint(context)
            self.last_warning = f"任务顺序记录无效，已恢复按 A 编号排序：{exc}"
            return []
        _checkpoint(context)

        try:
            if document.get("version") != self.VERSION:
                raise TaskOrderError("顺序记录版本无效")
            raw_entries = document.get("entries")
            if not isinstance(raw_entries, list):
                raise TaskOrderError("顺序记录缺少 entries 数组")
            result: list[dict[str, Any]] = []
            seen: set[str] = set()
            for raw in raw_entries:
                if not isinstance(raw, dict):
                    raise TaskOrderError("顺序记录项格式无效")
                name = raw.get("name")
                slot = raw.get("slot")
                if (
                    not isinstance(name, str)
                    or not name
                    or Path(name).name != name
                    or not name.lower().endswith(".json")
                    or isinstance(slot, bool)
                    or not isinstance(slot, int)
                    or slot < 1
                    or name in seen
                ):
                    raise TaskOrderError("顺序记录项内容无效")
                seen.add(name)
                result.append({"name": name, "slot": slot})
        except TaskCancelled:
            raise
        except Exception as exc:
            _checkpoint(context)
            self.last_warning = f"任务顺序记录无效，已恢复按 A 编号排序：{exc}"
            return []
        _checkpoint(context)
        return result

    def _save_entries(
        self,
        entries: list[dict[str, Any]],
        *,
        context: OperationContext | None = None,
    ) -> None:
        document = {"version": self.VERSION, "entries": entries}
        _checkpoint(context)
        if self.state_path.is_file():
            try:
                current = read_json(self.state_path)
            except TaskCancelled:
                raise
            except Exception:
                _checkpoint(context)
            else:
                _checkpoint(context)
                unchanged = current == document
                _checkpoint(context)
                if unchanged:
                    return
        else:
            _checkpoint(context)
        if context is not None:
            context.raise_if_cancelled()
            context.enter_critical_phase()
        write_json_atomic(self.state_path, document)
