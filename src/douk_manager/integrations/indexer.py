from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from douk_manager.config import resource_path


class IndexError(RuntimeError):
    pass


@dataclass(frozen=True)
class IndexResult:
    return_code: int
    output: str
    summary: dict[str, Any] | None = None

    def display_lines(self, title: str) -> tuple[str, ...]:
        if self.summary is None:
            return (f"{title}：脚本执行完成，但未返回数字汇总",)

        summary = self.summary
        lines: list[str] = []
        if summary["Mode"] == "Refresh":
            lines.append(
                f"{title}：快捷方式新建{{Created}}、更新{{Updated}}、保持不变{{Unchanged}}、"
                "创建或更新失败{IndexFailures}".format(**summary)
            )
        else:
            lines.append(f"{title}：重新扫描完成")
        lines.extend(
            (
                f"{title}：源目录检查：账号文件夹{{SourceFoldersScanned}}、空源文件夹"
                "{EmptySourceFolders}（仅提示，需手动处理）、忽略非账号文件夹"
                "{IgnoredSourceFolders}".format(**summary),
                f"{title}：目标已移动或删除的文件夹"
                "{MovedOrDeletedTargetFolders}".format(
                    **summary
                ),
                f"{title}：空源目录对应快捷方式：发现"
                "{EmptySourceShortcutsDetected}、"
                "实际删除{DeletedEmptySourceShortcuts}、剩余"
                "{RemainingEmptySourceShortcuts}".format(**summary),
                f"{title}：目标不存在的快捷方式：发现"
                "{MissingTargetShortcutsDetected}、"
                "实际删除{DeletedMissingTargetShortcuts}、剩余"
                "{RemainingMissingTargetShortcuts}".format(**summary),
                f"{title}：快捷方式清理：计划{{PlannedShortcutDeletions}}、实际删除"
                "{DeletedShortcutsTotal}、删除失败{ShortcutDeleteFailures}、"
                "读取失败{ShortcutReadFailures}".format(**summary),
                f"{title}：删除来源仅限 {summary['IndexRoot']} 顶层带 "
                "[DoukIndex] 标记的 .lnk",
            )
        )
        return tuple(lines)

    def display_summary(self, title: str) -> str:
        """Return semantic lines; the UI assigns a timestamp to every line."""

        return "\n".join(self.display_lines(title))


_SUMMARY_PREFIX = "DOUK_INDEX_SUMMARY_JSON="
_COMMON_COUNT_FIELDS = (
    "SourceFoldersScanned",
    "IgnoredSourceFolders",
    "EmptySourceFolders",
    "MovedOrDeletedTargetFolders",
    "EmptySourceShortcutsDetected",
    "MissingTargetShortcutsDetected",
    "PlannedShortcutDeletions",
    "DeletedEmptySourceShortcuts",
    "DeletedMissingTargetShortcuts",
    "DeletedShortcutsTotal",
    "ShortcutDeleteFailures",
    "RemainingEmptySourceShortcuts",
    "RemainingMissingTargetShortcuts",
    "ShortcutReadFailures",
)
_REFRESH_COUNT_FIELDS = ("Created", "Updated", "Unchanged", "IndexFailures")


def parse_index_output(output: str) -> tuple[str, dict[str, Any] | None]:
    """Remove the private JSON line and validate all counts before display."""

    visible_lines: list[str] = []
    raw_summary: dict[str, Any] | None = None
    for line in output.splitlines():
        if not line.startswith(_SUMMARY_PREFIX):
            visible_lines.append(line)
            continue
        if raw_summary is not None:
            raise IndexError("索引脚本返回了重复的数字汇总。")
        try:
            decoded = json.loads(line[len(_SUMMARY_PREFIX) :])
        except json.JSONDecodeError as exc:
            raise IndexError(f"索引脚本返回的数字汇总不是有效 JSON：{exc}") from exc
        if not isinstance(decoded, dict):
            raise IndexError("索引脚本返回的数字汇总格式无效。")
        raw_summary = decoded

    if raw_summary is None:
        return "\n".join(visible_lines).strip(), None

    mode = raw_summary.get("Mode")
    if mode not in {"Refresh", "ManualCleanup"}:
        raise IndexError(f"索引脚本返回了未知运行方式：{mode!r}")
    for text_field in ("SourceRoot", "IndexRoot"):
        if not isinstance(raw_summary.get(text_field), str) or not raw_summary[text_field]:
            raise IndexError(f"索引脚本数字汇总缺少字段：{text_field}")
    required_counts = list(_COMMON_COUNT_FIELDS)
    if mode == "Refresh":
        required_counts.extend(_REFRESH_COUNT_FIELDS)
    for field in required_counts:
        value = raw_summary.get(field)
        if type(value) is not int or value < 0:
            raise IndexError(f"索引脚本数字汇总字段无效：{field}")
    if raw_summary["EmptySourceFolders"] > raw_summary["SourceFoldersScanned"]:
        raise IndexError("索引脚本数字汇总不一致：空源文件夹超过扫描账号数。")
    if raw_summary["PlannedShortcutDeletions"] != (
        raw_summary["EmptySourceShortcutsDetected"]
        + raw_summary["MissingTargetShortcutsDetected"]
    ):
        raise IndexError("索引脚本数字汇总不一致：计划删除数不等于两类来源之和。")
    if raw_summary["DeletedShortcutsTotal"] != (
        raw_summary["DeletedEmptySourceShortcuts"]
        + raw_summary["DeletedMissingTargetShortcuts"]
    ):
        raise IndexError("索引脚本数字汇总不一致：实际删除总数不等于两类结果之和。")
    if (
        raw_summary["DeletedEmptySourceShortcuts"]
        > raw_summary["EmptySourceShortcutsDetected"]
        or raw_summary["DeletedMissingTargetShortcuts"]
        > raw_summary["MissingTargetShortcutsDetected"]
    ):
        raise IndexError("索引脚本数字汇总不一致：实际删除数超过发现数。")

    return "\n".join(visible_lines).strip(), raw_summary


class IndexService:
    def _run(self, script_name: str, source: Path, index: Path, open_folder: bool) -> IndexResult:
        if os.name != "nt":
            raise IndexError("快捷方式索引仅支持 Windows。")
        if not source.is_dir():
            raise IndexError(f"视频账号目录不存在：{source}")
        index.mkdir(parents=True, exist_ok=True)
        script = resource_path(f"resources/scripts/{script_name}")
        if not script.is_file():
            raise IndexError(f"索引脚本缺失：{script}")
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-SourceRoot",
            str(source),
            "-IndexRoot",
            str(index),
        ]
        if open_folder:
            command.append("-OpenIndexFolderAfterRun")
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        if completed.returncode != 0:
            raise IndexError(f"索引脚本执行失败（{completed.returncode}）：\n{output}")
        visible_output, summary = parse_index_output(output)
        return IndexResult(completed.returncode, visible_output, summary)

    def refresh(
        self,
        source: Path,
        index: Path,
        *,
        open_folder: bool = False,
        prompt_delete_broken: bool = False,
    ) -> IndexResult:
        if os.name != "nt":
            raise IndexError("快捷方式索引仅支持 Windows。")
        script = resource_path("resources/scripts/Refresh-DoukIndex.ps1")
        command = [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            "-SourceRoot",
            str(source),
            "-IndexRoot",
            str(index),
        ]
        if open_folder:
            command.append("-OpenIndexFolderAfterRun")
        if prompt_delete_broken:
            command.append("-PromptDeleteBrokenShortcuts")
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        output = "\n".join(part for part in (completed.stdout, completed.stderr) if part)
        if completed.returncode != 0:
            raise IndexError(f"索引刷新失败（{completed.returncode}）：\n{output}")
        visible_output, summary = parse_index_output(output)
        return IndexResult(completed.returncode, visible_output, summary)

    def cleanup(self, source: Path, index: Path, *, open_folder: bool = False) -> IndexResult:
        return self._run(
            "Cleanup-BrokenDoukIndex.ps1", source, index, open_folder
        )

    def cleanup_self_test(self) -> IndexResult:
        """Exercise the production cleanup script in an isolated temp folder."""

        if os.name != "nt":
            raise IndexError("失效快捷方式清理自检仅支持 Windows。")
        script = resource_path(
            "resources/scripts/Test-CleanupBrokenDoukIndex.ps1"
        )
        if not script.is_file():
            raise IndexError(f"清理自检脚本缺失：{script}")
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoLogo",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(script),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=subprocess.CREATE_NO_WINDOW,
            timeout=120,
        )
        output = "\n".join(
            part for part in (completed.stdout, completed.stderr) if part
        )
        if completed.returncode != 0:
            raise IndexError(
                f"失效快捷方式清理自检失败（{completed.returncode}）：\n{output}"
            )
        return IndexResult(completed.returncode, output)
