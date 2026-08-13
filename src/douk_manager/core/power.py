from __future__ import annotations

import os
import subprocess


class PowerError(RuntimeError):
    pass


def request_normal_shutdown() -> bool:
    """Ask Windows to perform a normal shutdown without force-closing apps.

    The manager owns the countdown and calls ``shutdown.exe /s /t 0`` only
    after every post-action and log write has succeeded.  Omitting ``/f`` lets
    Windows and other applications save work or block the shutdown normally.
    """

    if os.name != "nt":
        raise PowerError("完成后关机仅支持 Windows。")
    try:
        completed = subprocess.run(
            ["shutdown.exe", "/s", "/t", "0"],
            capture_output=True,
            text=True,
            encoding="mbcs",
            errors="replace",
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PowerError(f"无法请求正常关机：{exc}") from exc
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "未知错误").strip()
        raise PowerError(f"Windows 拒绝关机请求：{detail}")
    return True
