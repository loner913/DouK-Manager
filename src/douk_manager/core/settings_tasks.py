from __future__ import annotations

import copy
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from douk_manager.config import ManagedPaths
from douk_manager.core.backup import BackupService
from douk_manager.core.json_store import read_json, write_json_atomic
from douk_manager.core.locks import critical_section
from douk_manager.core.selector import Selection, compact_numbers, parse_selection, split_batches


class SettingsTaskError(RuntimeError):
    pass


@dataclass(frozen=True)
class EarliestRule:
    change: bool
    value: Any = None

    @classmethod
    def keep(cls) -> "EarliestRule":
        return cls(False, None)

    @classmethod
    def empty(cls) -> "EarliestRule":
        return cls(True, "")

    @classmethod
    def from_text(cls, text: str) -> "EarliestRule":
        stripped = text.strip()
        if not stripped:
            return cls.empty()
        if re.fullmatch(r"-?\d+", stripped):
            return cls(True, int(stripped))
        return cls(True, stripped)


@dataclass(frozen=True)
class SelectionPreview:
    selection: Selection
    total_positions: int
    selected_positions: int
    selected_valid_urls: int
    selected_blank_urls: int
    unselected_positions: int
    compact: str


@dataclass(frozen=True)
class GeneratedTask:
    task_path: Path
    preview: SelectionPreview
    active_path: Path | None = None
    backup_path: Path | None = None


class SettingsTaskService:
    def __init__(self, paths: ManagedPaths, backup: BackupService) -> None:
        self.paths = paths
        self.backup = backup

    def load_master(self) -> dict[str, Any]:
        document = read_json(self.paths.master_settings)
        self._accounts(document, "settings_master.json")
        return document

    @staticmethod
    def _accounts(document: dict[str, Any], name: str) -> list[dict[str, Any]]:
        accounts = document.get("accounts_urls")
        if not isinstance(accounts, list):
            raise SettingsTaskError(f"{name} 缺少 accounts_urls 数组。")
        for index, account in enumerate(accounts, start=1):
            if not isinstance(account, dict):
                raise SettingsTaskError(f"{name} 的 A{index} 不是对象。")
        return accounts

    def preview(self, expression: str) -> SelectionPreview:
        master = self.load_master()
        accounts = self._accounts(master, "settings_master.json")
        selection = parse_selection(expression, len(accounts))
        valid = sum(
            1
            for number in selection.numbers
            if str(accounts[number - 1].get("url", "")).strip()
        )
        return SelectionPreview(
            selection=selection,
            total_positions=len(accounts),
            selected_positions=len(selection.numbers),
            selected_valid_urls=valid,
            selected_blank_urls=len(selection.numbers) - valid,
            unselected_positions=len(accounts) - len(selection.numbers),
            compact=compact_numbers(selection.numbers),
        )

    def build_task_document(
        self,
        master: dict[str, Any],
        selection: Selection,
        task_earliest: EarliestRule,
    ) -> dict[str, Any]:
        task = copy.deepcopy(master)
        accounts = self._accounts(task, "任务配置")
        selected = set(selection.numbers)
        for position, account in enumerate(accounts, start=1):
            account["enable"] = position in selected
            if position in selected and task_earliest.change:
                account["earliest"] = task_earliest.value
        task["run_command"] = "5 1 1 Q"
        return task

    def create_task(
        self,
        expression: str,
        *,
        task_earliest: EarliestRule | None = None,
        persist_master_earliest: bool = False,
        task_name: str | None = None,
        activate: bool = False,
    ) -> GeneratedTask:
        task_earliest = task_earliest or EarliestRule.keep()
        with critical_section(self.paths.lock_file):
            master = self.load_master()
            accounts = self._accounts(master, "settings_master.json")
            selection = parse_selection(expression, len(accounts))
            preview = self.preview(expression)

            master_new = copy.deepcopy(master)
            if persist_master_earliest and task_earliest.change:
                master_accounts = self._accounts(master_new, "settings_master.json")
                for number in selection.numbers:
                    master_accounts[number - 1]["earliest"] = task_earliest.value
            task = self.build_task_document(master_new, selection, task_earliest)

            safe_name = _safe_task_name(task_name or preview.compact.replace(",", "_"))
            task_path = _unique_task_path(self.paths.tasks, safe_name)
            write_json_atomic(task_path, task)

            backup_path: Path | None = None
            active_path: Path | None = None
            if activate or (persist_master_earliest and task_earliest.change):
                backup_path = self.backup.create_critical_snapshot(
                    "BeforeChange",
                    {
                        "operation": "create_task",
                        "selection": preview.compact,
                        "persist_master_earliest": persist_master_earliest,
                        "activate": activate,
                    },
                    keep_latest=20,
                )
            try:
                if persist_master_earliest and task_earliest.change:
                    # enable remains byte-for-byte equal at account-field level.
                    for old, new in zip(accounts, self._accounts(master_new, "主档")):
                        if old.get("enable", True) != new.get("enable", True):
                            raise SettingsTaskError("安全检查失败：主档 enable 被意外修改。")
                    write_json_atomic(self.paths.master_settings, master_new)
                if activate:
                    write_json_atomic(self.paths.active_settings, task)
                    active_path = self.paths.active_settings
            except Exception:
                if backup_path is not None:
                    self.backup.restore_named_files(
                        backup_path, ("settings_master.json", "settings.json")
                    )
                raise
            return GeneratedTask(task_path, preview, active_path, backup_path)

    def activate_existing_task(self, task_path: Path) -> Path:
        with critical_section(self.paths.lock_file):
            stored_task = read_json(task_path)
            stored_accounts = self._accounts(stored_task, task_path.name)
            latest_master = self.load_master()
            latest_accounts = self._accounts(latest_master, "settings_master.json")

            # A task file is a reusable selection/earliest template, not another
            # account master.  Always rebuild it on top of the latest official
            # master so renamed marks, corrected URLs and newly collected accounts
            # cannot be reverted by an old task JSON.
            task = copy.deepcopy(latest_master)
            active_accounts = self._accounts(task, "待激活 settings.json")
            for position, active_account in enumerate(active_accounts):
                if position >= len(stored_accounts):
                    active_account["enable"] = False
                    continue
                stored_account = stored_accounts[position]
                active_account["enable"] = bool(stored_account.get("enable", True))
                if "earliest" in stored_account:
                    active_account["earliest"] = copy.deepcopy(
                        stored_account["earliest"]
                    )

            task["run_command"] = "5 1 1 Q"
            enabled = sum(bool(account.get("enable", True)) for account in active_accounts)
            if enabled < 1:
                raise SettingsTaskError("任务没有启用任何账号。")
            snapshot = self.backup.create_critical_snapshot(
                "BeforeChange",
                {
                    "operation": "activate_task",
                    "task": str(task_path),
                    "stored_accounts": len(stored_accounts),
                    "latest_master_accounts": len(latest_accounts),
                    "account_identity_source": "latest_settings_master",
                },
                keep_latest=20,
            )
            try:
                write_json_atomic(self.paths.active_settings, task)
            except Exception:
                self.backup.restore_named_files(snapshot, ("settings.json",))
                raise
            return self.paths.active_settings

    def generate_batches(
        self,
        start: int,
        end: int,
        batch_size: int,
        *,
        task_earliest: EarliestRule | None = None,
    ) -> tuple[GeneratedTask, ...]:
        master = self.load_master()
        total = len(self._accounts(master, "settings_master.json"))
        if end > total:
            raise SettingsTaskError(f"结束编号 A{end} 超过当前最大编号 A{total}。")
        generated: list[GeneratedTask] = []
        for ordinal, (batch_start, batch_end) in enumerate(
            split_batches(start, end, batch_size), start=1
        ):
            expression = f"A{batch_start}-A{batch_end}"
            name = f"Batch_{ordinal:03d}_A{batch_start}-A{batch_end}"
            generated.append(
                self.create_task(
                    expression,
                    task_earliest=task_earliest or EarliestRule.keep(),
                    task_name=name,
                    activate=False,
                )
            )
        return tuple(generated)


def _safe_task_name(name: str) -> str:
    """Return a Windows-safe filename while preserving readable Unicode names."""

    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", str(name))
    cleaned = re.sub(r"\s+", "_", cleaned).strip(" ._")
    cleaned = cleaned.rstrip(" .")[:120].rstrip(" .")
    if not cleaned:
        return "DouK_Task"

    # Windows reserves these names even when they have a file extension.
    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }
    if cleaned.split(".", 1)[0].upper() in reserved:
        cleaned = f"Task_{cleaned}"
    return cleaned


def _unique_task_path(directory: Path, stem: str) -> Path:
    """任务模板永久保留；同名时递增编号，绝不覆盖旧模板。"""
    directory.mkdir(parents=True, exist_ok=True)
    candidate = directory / f"{stem}.json"
    suffix = 2
    while candidate.exists():
        candidate = directory / f"{stem}_{suffix}.json"
        suffix += 1
    return candidate
