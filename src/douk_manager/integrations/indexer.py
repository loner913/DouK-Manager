from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from douk_manager.config import resource_path


class IndexError(RuntimeError):
    pass


@dataclass(frozen=True)
class IndexResult:
    return_code: int
    output: str


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
        return IndexResult(completed.returncode, output)

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
        return IndexResult(completed.returncode, output)

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
