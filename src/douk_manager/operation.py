"""Pure-Python cancellation and progress protocol for Phase 2 operations."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


class TaskCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class OperationProgress:
    phase: str
    message: str
    current: int | None = None
    total: int | None = None
    critical_to_completion: bool = False


class OperationContext:
    """Coordinates cancellation, critical-stage arbitration, and throttled progress."""

    def __init__(
        self,
        *,
        progress_callback: Callable[[OperationProgress], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        progress_interval_seconds: float = 0.15,
    ) -> None:
        if progress_interval_seconds < 0:
            raise ValueError("progress interval must not be negative")
        self._lock = threading.RLock()
        self._cancel_requested = False
        self._critical_to_completion = False
        self._terminal_sealed = False
        self._progress_callback = progress_callback
        self._clock = clock
        self._progress_interval_seconds = progress_interval_seconds
        self._last_progress: OperationProgress | None = None
        self._last_progress_at: float | None = None

    @property
    def cancel_requested(self) -> bool:
        with self._lock:
            return self._cancel_requested

    @property
    def critical_to_completion(self) -> bool:
        with self._lock:
            return self._critical_to_completion

    @property
    def terminal_sealed(self) -> bool:
        with self._lock:
            return self._terminal_sealed

    def set_progress_callback(
        self,
        callback: Callable[[OperationProgress], Any] | None,
    ) -> None:
        with self._lock:
            self._progress_callback = callback

    def request_cancel(self) -> bool:
        """Accept cancellation only while the operation is still cancellable."""
        with self._lock:
            if (
                self._cancel_requested
                or self._critical_to_completion
                or self._terminal_sealed
            ):
                return False
            self._cancel_requested = True
            return True

    def _task_cancelled(self) -> TaskCancelled:
        return TaskCancelled("background operation was cancelled")

    def raise_if_cancelled(self) -> None:
        with self._lock:
            if self._cancel_requested and not self._critical_to_completion:
                raise self._task_cancelled()

    def enter_critical_phase(self) -> None:
        """Atomically win the critical-stage race or raise cancellation."""
        with self._lock:
            if self._terminal_sealed:
                raise RuntimeError("operation terminal state is already sealed")
            if self._cancel_requested:
                raise self._task_cancelled()
            self._critical_to_completion = True

    def seal_terminal(self) -> None:
        """Seal a successful/cancelled terminal decision atomically."""
        with self._lock:
            if self._terminal_sealed:
                return
            if self._cancel_requested and not self._critical_to_completion:
                raise self._task_cancelled()
            self._terminal_sealed = True

    def seal_failure(self) -> None:
        """Seal a failure without allowing a late cancellation to replace it."""
        with self._lock:
            self._terminal_sealed = True

    def report_progress(self, progress: OperationProgress) -> bool:
        """Emit progress only after the pure-Python throttle has admitted it."""
        if not isinstance(progress, OperationProgress):
            raise TypeError("progress must be an OperationProgress")
        with self._lock:
            if self._terminal_sealed:
                return False
            now = self._clock()
            phase_changed = (
                self._last_progress is None
                or self._last_progress.phase != progress.phase
            )
            critical_changed = (
                progress.critical_to_completion
                and not (
                    self._last_progress is not None
                    and self._last_progress.critical_to_completion
                )
            )
            interval_elapsed = (
                self._last_progress_at is None
                or now - self._last_progress_at >= self._progress_interval_seconds
            )
            if not (phase_changed or critical_changed or interval_elapsed):
                return False
            self._last_progress = progress
            self._last_progress_at = now
            callback = self._progress_callback
            if callback is not None:
                callback(progress)
            return True
