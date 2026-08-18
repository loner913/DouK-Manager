from __future__ import annotations

import sqlite3
import threading
import traceback
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from types import TracebackType
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot


class TaskState(Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    CANCELLING = "CANCELLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ClosePolicy(Enum):
    CANCEL = "CANCEL"
    WAIT = "WAIT"
    REJECT = "REJECT"


class TaskRejectedError(RuntimeError):
    pass


class TaskCancelled(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskSpec:
    task_type: str
    display_name: str
    resource_keys: frozenset[str] = field(default_factory=frozenset)
    deduplicate_key: str | None = None
    cancellable: bool = True
    close_policy: ClosePolicy = ClosePolicy.CANCEL
    refresh_targets: tuple[str, ...] = ()
    critical_write_started: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "resource_keys", frozenset(self.resource_keys))
        object.__setattr__(self, "refresh_targets", tuple(self.refresh_targets))


class CancellationToken:
    def __init__(self) -> None:
        self._cancelled = threading.Event()

    def cancel(self) -> None:
        self._cancelled.set()

    def is_cancelled(self) -> bool:
        return self._cancelled.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            raise TaskCancelled("background task was cancelled")


@dataclass(frozen=True)
class TaskFailure:
    error_type: str
    message: str
    traceback_text: str

    def __post_init__(self) -> None:
        if not isinstance(self.error_type, str):
            raise TypeError("TaskFailure.error_type must be a string")
        if not isinstance(self.message, str):
            raise TypeError("TaskFailure.message must be a string")
        if not isinstance(self.traceback_text, str):
            raise TypeError("TaskFailure.traceback_text must be a string")
        object.__setattr__(self, "message", self.message[:4000])
        object.__setattr__(self, "traceback_text", self.traceback_text[-4000:])


def _assert_ordinary_data(
    value: Any,
    *,
    seen: set[int] | None = None,
    depth: int = 0,
) -> None:
    if depth > 32:
        raise TypeError("task payload is nested too deeply")
    if isinstance(value, QObject):
        raise TypeError("task payload cannot contain a QObject")
    if isinstance(value, sqlite3.Connection):
        raise TypeError("task payload cannot contain a SQLite connection")
    if isinstance(value, TracebackType):
        raise TypeError("task payload cannot contain a live traceback")
    if value is None or isinstance(value, (bool, int, float, str, bytes, Path, Enum)):
        return
    if isinstance(value, TaskFailure):
        return

    seen = seen if seen is not None else set()
    marker = id(value)
    if marker in seen:
        raise TypeError("task payload cannot contain a cycle")
    seen.add(marker)
    try:
        if isinstance(value, Mapping):
            for key, item in value.items():
                _assert_ordinary_data(key, seen=seen, depth=depth + 1)
                _assert_ordinary_data(item, seen=seen, depth=depth + 1)
            return
        if isinstance(value, (list, tuple, set, frozenset)):
            for item in value:
                _assert_ordinary_data(item, seen=seen, depth=depth + 1)
            return
        if is_dataclass(value) and not isinstance(value, type):
            for field_info in fields(value):
                _assert_ordinary_data(
                    getattr(value, field_info.name),
                    seen=seen,
                    depth=depth + 1,
                )
            return
    finally:
        seen.remove(marker)
    raise TypeError(f"unsupported task payload type: {type(value).__name__}")


class TaskWorker(QObject):
    settled = Signal(str, int, object, object)

    def __init__(
        self,
        task_id: str,
        generation: int,
        action: Callable[[CancellationToken], Any],
        token: CancellationToken,
    ) -> None:
        super().__init__()
        self._task_id = task_id
        self._generation = generation
        self._action = action
        self._token = token

    @Slot()
    def run(self) -> None:
        outcome = TaskState.FAILED
        payload: Any = TaskFailure(
            error_type="InternalTaskError",
            message="background task did not produce a terminal result",
            traceback_text="",
        )
        try:
            self._token.raise_if_cancelled()
            payload = self._action(self._token)
            self._token.raise_if_cancelled()
            _assert_ordinary_data(payload)
            outcome = TaskState.SUCCEEDED
        except TaskCancelled as exc:
            outcome = TaskState.CANCELLED
            payload = str(exc)
        except Exception as exc:
            outcome = TaskState.FAILED
            payload = TaskFailure(
                error_type=type(exc).__name__,
                message=str(exc),
                traceback_text=traceback.format_exc(limit=20)[-4000:],
            )
        finally:
            self.settled.emit(self._task_id, self._generation, outcome, payload)


@dataclass
class TaskRecord:
    task_id: str
    spec: TaskSpec
    generation: int
    token: CancellationToken
    thread: QThread | None
    worker: TaskWorker | None
    state: TaskState = TaskState.QUEUED
    terminal_seen: bool = False
    thread_finished_seen: bool = False
    payload: Any = None

    def accept_terminal(self, outcome: TaskState, payload: Any) -> bool:
        if self.terminal_seen:
            return False
        if outcome not in (
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLED,
        ):
            raise ValueError(f"invalid terminal task state: {outcome!r}")
        self.terminal_seen = True
        self.state = outcome
        self.payload = payload
        return True

    def observe_thread_finished(self) -> bool:
        if self.thread_finished_seen:
            return False
        self.thread_finished_seen = True
        return True

    @property
    def ready_for_removal(self) -> bool:
        return self.terminal_seen and self.thread_finished_seen


class BackgroundTaskCoordinator(QObject):
    task_settled = Signal(str, int, object, object)
    task_removed = Signal(str)
    idle = Signal()
    internal_error = Signal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._records: dict[str, TaskRecord] = {}
        self._closing = False
        self._idle_emitted = True

    @property
    def is_closing(self) -> bool:
        return self._closing

    def has_active_tasks(self) -> bool:
        return bool(self._records)

    def _validate_start(self, spec: TaskSpec) -> None:
        if self._closing:
            raise TaskRejectedError("background task coordinator is closing")
        for record in self._records.values():
            if (
                spec.deduplicate_key is not None
                and record.spec.deduplicate_key == spec.deduplicate_key
            ):
                raise TaskRejectedError(
                    f"duplicate background task: {spec.deduplicate_key}"
                )
            conflicts = record.spec.resource_keys.intersection(spec.resource_keys)
            if conflicts:
                names = ", ".join(sorted(conflicts))
                raise TaskRejectedError(f"background task resource conflict: {names}")

    def start(
        self,
        spec: TaskSpec,
        generation: int,
        action: Callable[[CancellationToken], Any],
    ) -> str:
        self._validate_start(spec)
        task_id = uuid.uuid4().hex
        token = CancellationToken()
        thread = QThread(self)
        worker = TaskWorker(task_id, generation, action, token)
        record = TaskRecord(
            task_id=task_id,
            spec=spec,
            generation=generation,
            token=token,
            thread=thread,
            worker=worker,
        )
        self._records[task_id] = record
        self._idle_emitted = False

        thread.setObjectName(f"douk-{spec.task_type}-{task_id[:8]}")
        thread.setProperty("douk_task_id", task_id)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.settled.connect(self._on_worker_settled)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._on_thread_finished)
        thread.finished.connect(thread.deleteLater)

        record.state = TaskState.RUNNING
        thread.start()
        return task_id

    def request_cancel(self, task_id: str) -> bool:
        record = self._records.get(task_id)
        if record is None or record.terminal_seen:
            return False
        if not record.spec.cancellable or record.spec.critical_write_started:
            return False
        record.token.cancel()
        if record.state in (TaskState.QUEUED, TaskState.RUNNING):
            record.state = TaskState.CANCELLING
        return True

    def begin_closing(self) -> bool:
        if self._closing:
            return not self.has_active_tasks()
        if any(
            record.spec.close_policy is ClosePolicy.REJECT
            for record in self._records.values()
        ):
            return False
        self._closing = True
        for task_id, record in tuple(self._records.items()):
            if record.spec.close_policy is ClosePolicy.CANCEL:
                self.request_cancel(task_id)
        return not self.has_active_tasks()

    @Slot(str, int, object, object)
    def _on_worker_settled(
        self,
        task_id: str,
        generation: int,
        outcome: object,
        payload: object,
    ) -> None:
        record = self._records.get(task_id)
        if record is None or record.terminal_seen:
            return
        protocol_details: str | None = None
        if (
            generation != record.generation
            or not isinstance(outcome, TaskState)
            or outcome not in (
                TaskState.SUCCEEDED,
                TaskState.FAILED,
                TaskState.CANCELLED,
            )
        ):
            protocol_details = (
                f"invalid terminal protocol for {task_id}: "
                f"generation={generation!r}, outcome={outcome!r}"
            )
        else:
            try:
                _assert_ordinary_data(payload)
            except TypeError as exc:
                protocol_details = f"invalid task payload for {task_id}: {exc}"
        if protocol_details is not None:
            details = protocol_details
            self.internal_error.emit(details)
            outcome = TaskState.FAILED
            payload = TaskFailure(
                error_type="InternalTaskProtocolError",
                message=details,
                traceback_text="",
            )
            generation = record.generation
        if not record.accept_terminal(outcome, payload):
            return
        self.task_settled.emit(task_id, generation, outcome, payload)
        if record.thread is not None and not record.thread_finished_seen:
            record.thread.quit()
        self._maybe_remove(record)

    @Slot()
    def _on_thread_finished(self) -> None:
        thread = self.sender()
        if not isinstance(thread, QThread):
            return
        task_id = thread.property("douk_task_id")
        if not isinstance(task_id, str):
            return
        self._observe_thread_finished(task_id, thread)

    def _observe_thread_finished(self, task_id: str, thread: object) -> None:
        record = self._records.get(task_id)
        if record is None or record.thread is not thread:
            return
        if not record.observe_thread_finished():
            return
        if not record.terminal_seen:
            details = f"thread finished before settled for task {task_id}"
            failure = TaskFailure(
                error_type="InternalTaskProtocolError",
                message=details,
                traceback_text="",
            )
            record.accept_terminal(TaskState.FAILED, failure)
            self.internal_error.emit(details)
        self._maybe_remove(record)

    def _maybe_remove(self, record: TaskRecord) -> None:
        if not record.ready_for_removal:
            return
        if self._records.get(record.task_id) is not record:
            return
        del self._records[record.task_id]
        self.task_removed.emit(record.task_id)
        if not self._records and not self._idle_emitted:
            self._idle_emitted = True
            self.idle.emit()
