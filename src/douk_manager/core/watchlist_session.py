from __future__ import annotations

from pathlib import Path

from douk_manager.core.locks import LockBusyError, ProcessFileLock


class ManagerInstanceLease:
    """Long-lived ownership of one manager run root.

    The lease deliberately uses the dedicated instance-lock file.  The
    short-lived ``.douk_manager.lock`` remains reserved for individual data
    operations and is never used as the manager-session identity.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file_lock = ProcessFileLock(path, timeout=0.0)
        self._active = False

    @property
    def active(self) -> bool:
        return self._active

    def acquire(self) -> None:
        if self._active:
            return
        self._file_lock.acquire()
        self._active = True

    def release(self) -> None:
        if not self._active:
            return
        try:
            self._file_lock.release()
        finally:
            self._active = False

    def manager_is_still_owner(self) -> bool:
        """Return whether this lease can still prove a held instance lock.

        This is only a local liveness check.  It does not replace the normal
        lease and global-lock checks performed immediately before a write.
        """

        if not self._active:
            return False
        probe = ProcessFileLock(self.path, timeout=0.0)
        try:
            probe.acquire()
        except LockBusyError:
            return True
        else:
            probe.release()
            return False


__all__ = ["ManagerInstanceLease"]
