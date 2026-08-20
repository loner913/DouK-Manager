from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import threading
import traceback
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QEventLoop,
    QObject,
    QThread,
    QTimer,
    Qt,
    Slot,
    qInstallMessageHandler,
)
from PySide6.QtWidgets import QApplication, QMessageBox

from douk_manager.background import (
    BackgroundTaskCoordinator,
    ClosePolicy,
    TaskSpec,
    TaskState,
    TaskWorker,
)
from douk_manager.config import AppConfig
from douk_manager.core.download_summary import (
    AccountStatus,
    DownloadSummary,
    LocatedNativeLogs,
)
from douk_manager.core.engine import EngineExitAssessment, EngineRun
from douk_manager.gui import MainWindow
from douk_manager.integrations.collector import MigrationResult
from douk_manager.integrations.indexer import IndexResult
from douk_manager.startup import StartupState
from tests.helpers import make_test_paths


ROUNDS = 20
REQUIRED_ENTRIES = (
    "collector_stop",
    "collector_migration",
    "manual_backup",
    "index_self_test",
    "task_scan",
    "engine_preview",
)
ENTRY_TASK_TYPES = {
    "collector_stop": "collector_stop",
    "collector_migration": "collector_migration",
    "manual_backup": "full_volume_backup",
    "index_self_test": "index_cleanup_self_test",
    "task_scan": "task_list_snapshot",
    "engine_preview": "engine_update_preview",
}
REQUIRED_CONTROLLER_CALLS = (
    "summarize_download",
    "run_post_actions",
    "stop_collector",
    "migrate_collector",
    "backup_now",
    "cleanup_index_self_test",
    "list_tasks",
    "preview_engine_update",
)
FORBIDDEN_DIAGNOSTICS = (
    "QThread: Destroyed while thread is still running",
    "Internal C++ object already deleted",
)
EXPECTED_REAL_SPECS = {
    "download_summary": {
        "task_type": "download_summary",
        "display_name": "汇总下载结果",
        "resource_keys": ["result_logs", "task_logs"],
        "cancellable": False,
        "close_policy": "WAIT",
        "refresh_targets": [],
        "critical_write_started": False,
        "dynamic_cancellation": False,
        "allow_during_closing": False,
    },
    "download_post_actions": {
        "task_type": "download_post_actions",
        "display_name": "下载后续动作",
        "resource_keys": [],
        "cancellable": True,
        "close_policy": "CANCEL",
        "refresh_targets": ["runtime_status", "download_results"],
        "critical_write_started": False,
        "dynamic_cancellation": True,
        "allow_during_closing": False,
    },
    "collector_stop": {
        "task_type": "collector_stop",
        "display_name": "停止账号采集服务",
        "resource_keys": ["collector_process"],
        "deduplicate_key": "collector_stop",
        "cancellable": False,
        "close_policy": "WAIT",
        "refresh_targets": ["runtime_status"],
        "critical_write_started": False,
        "dynamic_cancellation": False,
        "allow_during_closing": False,
    },
    "collector_migration": {
        "task_type": "collector_migration",
        "display_name": "迁移旧采集器数据",
        "resource_keys": [
            "collector_data",
            "collector_process",
            "screenshots",
            "settings",
        ],
        "deduplicate_key": "collector_migration",
        "cancellable": False,
        "close_policy": "WAIT",
        "refresh_targets": ["runtime_status"],
        "critical_write_started": False,
        "dynamic_cancellation": False,
        "allow_during_closing": False,
    },
    "full_volume_backup": {
        "task_type": "full_volume_backup",
        "display_name": "完整 Volume 备份",
        "resource_keys": ["settings", "volume"],
        "deduplicate_key": "full_volume_backup",
        "cancellable": True,
        "close_policy": "CANCEL",
        "refresh_targets": ["runtime_status"],
        "critical_write_started": False,
        "dynamic_cancellation": True,
        "allow_during_closing": False,
    },
    "index_cleanup_self_test": {
        "task_type": "index_cleanup_self_test",
        "display_name": "自检索引清理",
        "resource_keys": ["index"],
        "deduplicate_key": "index_operation",
        "cancellable": True,
        "close_policy": "CANCEL",
        "refresh_targets": [],
        "critical_write_started": False,
        "dynamic_cancellation": True,
        "allow_during_closing": False,
    },
    "task_list_snapshot": {
        "task_type": "task_list_snapshot",
        "display_name": "刷新任务列表",
        "resource_keys": ["settings", "task_templates"],
        "deduplicate_key": "task_list_snapshot",
        "cancellable": True,
        "close_policy": "CANCEL",
        "refresh_targets": [],
        "critical_write_started": False,
        "dynamic_cancellation": True,
        "allow_during_closing": False,
    },
    "engine_update_preview": {
        "task_type": "engine_update_preview",
        "display_name": "预检更新包",
        "resource_keys": ["engine_files"],
        "cancellable": True,
        "close_policy": "CANCEL",
        "refresh_targets": [],
        "critical_write_started": False,
        "dynamic_cancellation": True,
        "allow_during_closing": False,
    },
}


def _type_name(value: object) -> str:
    value_type = type(value)
    return f"{value_type.__module__}.{value_type.__qualname__}"


def _json_value(value: object) -> object:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, Path):
        return os.fspath(value)
    if isinstance(value, TaskState):
        return value.value
    if isinstance(value, (tuple, list, set, frozenset)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return repr(value)


def run_until(condition: Callable[[], bool], *, timeout_ms: int = 5000) -> None:
    if condition():
        return
    loop = QEventLoop()
    timed_out: list[bool] = []
    poll = QTimer()
    poll.setInterval(1)

    def check_condition() -> None:
        if condition():
            poll.stop()
            loop.quit()

    timeout = QTimer()
    timeout.setSingleShot(True)
    timeout.timeout.connect(lambda: (timed_out.append(True), loop.quit()))
    poll.timeout.connect(check_condition)
    poll.start()
    timeout.start(timeout_ms)
    loop.exec()
    poll.stop()
    timeout.stop()
    if timed_out or not condition():
        raise RuntimeError("bounded Phase 2 Qt lifecycle probe timed out")


def construct_real_main_window() -> MainWindow:
    return MainWindow()


def required_main_window_calls(window: MainWindow, entry: str) -> None:
    calls = {
        "collector_stop": window._stop_collector,
        "collector_migration": window._migrate_collector,
        "index_self_test": window._cleanup_index_self_test,
        "task_scan": window.refresh_tasks,
        "engine_preview": window._preview_engine_update,
    }
    if entry == "manual_backup":
        with patch(
            "douk_manager.gui.QMessageBox.question", return_value=QMessageBox.Yes
        ):
            window._manual_backup()
        return
    try:
        action = calls[entry]
    except KeyError as exc:
        raise ValueError(f"unknown required MainWindow entry: {entry}") from exc
    action()


class _ExitedProcess:
    pid = 41005

    @staticmethod
    def poll() -> int:
        return 0


class _ManagedCollectorProcess:
    pid = 41006

    @staticmethod
    def poll() -> int:
        return 0


class _TaskLifecycleObserver(QObject):
    def __init__(
        self,
        task: dict[str, Any],
        task_id: str,
        qt_refs: dict[str, tuple[TaskWorker, QThread, QObject]],
        parent: QObject,
    ) -> None:
        super().__init__(parent)
        self._task = task
        self._task_id = task_id
        self._qt_refs = qt_refs

    @Slot()
    def observe_thread_finished(self) -> None:
        self._task["thread_finished_count"] += 1
        self._task["lifecycle_events"].append("thread_finished_signal")

    @Slot()
    def observe_thread_destroyed(self) -> None:
        self._observe_destroyed("thread")

    @Slot()
    def observe_worker_destroyed(self) -> None:
        self._observe_destroyed("worker")

    def _observe_destroyed(self, object_name: str) -> None:
        self._task[f"{object_name}_wrapper_retained_during_destroyed"] = (
            self._task_id in self._qt_refs
        )
        self._task[f"{object_name}_destroyed_count"] += 1
        self._task["lifecycle_events"].append(f"{object_name}_destroyed")


def _summary_payload() -> DownloadSummary:
    return DownloadSummary(
        planned_count=0,
        started_outcomes=(),
        pre_start_errors=(),
        not_started=(),
        primary_status_counts={status: 0 for status in AccountStatus},
        completed_with_anomaly=(),
        complete=True,
        reliable=True,
        reasons=(),
        located=LocatedNativeLogs(segments=(), method="phase2-probe", reliable=True),
        exit_code=0,
    )


def _write_synthetic_inputs(base: Path) -> tuple[object, Path, Path]:
    paths = make_test_paths(base)
    config = AppConfig(
        engine_exe=os.fspath(paths.engine_exe),
        video_root=os.fspath(paths.video_root),
        index_root=os.fspath(paths.index_root),
        old_screenshot_dir=os.fspath(base / "old-collector-screenshots"),
        screenshot_post_mode="disabled",
        index_post_mode="disabled",
        cleanup_after_index=False,
    )
    config.save(paths.config_file)
    task_path = paths.tasks / "Task_A1_probe.json"
    task_path.write_text(
        json.dumps(
            {
                "accounts_urls": [
                    {
                        "mark": "A1probe",
                        "url": "https://www.douyin.com/user/probe",
                        "tab": "post",
                        "earliest": "",
                        "latest": "",
                        "enable": True,
                    }
                ],
                "run_command": "",
                "cookie": "",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    archive = base / "engine-update-probe.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("package/main.exe", b"phase2 probe executable")
        bundle.writestr("package/_internal/new-runtime.dll", b"phase2 probe runtime")
    return paths, task_path, archive


class _RoundObserver:
    def __init__(self, window: MainWindow, round_number: int) -> None:
        self.window = window
        self.round_number = round_number
        self.coordinator = window.coordinator
        self.tasks: list[dict[str, Any]] = []
        self.tasks_by_id: dict[str, dict[str, Any]] = {}
        self._qt_refs: dict[
            str,
            tuple[TaskWorker, QThread, QObject],
        ] = {}
        self.required_entry_calls: list[str] = []
        self.summary_post_sequence: list[str] = []
        self.controller_calls: list[dict[str, Any]] = []
        self.service_calls: list[dict[str, Any]] = []
        self.refresh_events: list[dict[str, Any]] = []
        self.call_sequence: list[str] = []
        self.internal_errors: list[str] = []
        self.contract_errors: list[str] = []
        self.current_invocation: str | None = None
        self.product_object_types = {
            "window": _type_name(window),
            "controller": _type_name(window.controller),
            "coordinator": _type_name(window.coordinator),
            "task_order_service": _type_name(window.controller.task_order),
            "engine_update_service": _type_name(window.controller.engine_updates),
        }
        self.generic_protocol: dict[str, Any] = {}
        self.closing_protocol: dict[str, Any] = {}
        self.validated_real_specs: list[str] = []
        self._hold_task_types: set[str] = set()
        self._held_releases: dict[str, threading.Event] = {}
        self._control_events: list[threading.Event] = []
        self._backup_protocol_mode: str | None = None
        self._backup_protocol_entered = threading.Event()
        self._backup_protocol_release = threading.Event()
        self._control_events.append(self._backup_protocol_release)
        self._restorations: list[tuple[object, str, bool, object]] = []
        self._original_start = self.coordinator.start
        self._settled_slot = self._on_settled
        self._removed_slot = self._on_removed
        self._internal_error_slot = self.internal_errors.append
        self.coordinator.task_settled.connect(self._settled_slot)
        self.coordinator.task_removed.connect(self._removed_slot)
        self.coordinator.internal_error.connect(self._internal_error_slot)
        self._replace_attribute(self.coordinator, "start", self._observe_start)

    def _replace_attribute(self, target: object, name: str, value: object) -> None:
        namespace = getattr(target, "__dict__", {})
        had_instance_value = name in namespace
        previous = namespace.get(name) if had_instance_value else getattr(target, name)
        self._restorations.append((target, name, had_instance_value, previous))
        setattr(target, name, value)

    @staticmethod
    def _spec_evidence(spec: TaskSpec) -> dict[str, Any]:
        return {
            "task_type": spec.task_type,
            "display_name": spec.display_name,
            "resource_keys": sorted(spec.resource_keys),
            "deduplicate_key": spec.deduplicate_key,
            "cancellable": spec.cancellable,
            "close_policy": spec.close_policy.value,
            "refresh_targets": list(spec.refresh_targets),
            "critical_write_started": spec.critical_write_started,
            "dynamic_cancellation": spec.dynamic_cancellation,
            "allow_during_closing": spec.allow_during_closing,
        }

    def _observe_start(
        self,
        spec: TaskSpec,
        generation: int,
        action: Callable[[object], object],
    ) -> str:
        entered = threading.Event()
        release = threading.Event()
        terminal_records = [
            record
            for record in self.coordinator._records.values()
            if record.terminal_seen and not record.thread_finished_seen
        ]
        task: dict[str, Any] = {
            "task_id": None,
            "task_type": spec.task_type,
            "generation": generation,
            "invocation": self.current_invocation,
            "spec": self._spec_evidence(spec),
            "coordinator_type": _type_name(self.coordinator),
            "thread_type": None,
            "worker_type": None,
            "finished_observer_type": None,
            "finished_observer_gui_affinity": False,
            "thread_name": None,
            "thread_started": False,
            "thread_running_after_start": False,
            "action_thread_type": None,
            "action_thread_name": None,
            "settled_count": 0,
            "outcome": None,
            "payload": None,
            "removed_count": 0,
            "thread_finished_count": 0,
            "thread_destroyed_count": 0,
            "worker_destroyed_count": 0,
            "thread_wrapper_retained_during_destroyed": False,
            "worker_wrapper_retained_during_destroyed": False,
            "qt_refs_released_after_destroyed": False,
            "lifecycle_events": [],
            "expected_outcome": TaskState.SUCCEEDED.value,
            "triggered_by_terminal_task_ids": [
                record.task_id for record in terminal_records
            ],
            "triggered_by_terminal_task_types": [
                record.spec.task_type for record in terminal_records
            ],
        }
        if terminal_records:
            self.refresh_events.append(
                {
                    "spawned_task_type": spec.task_type,
                    "source_task_ids": [record.task_id for record in terminal_records],
                    "source_task_types": [
                        record.spec.task_type for record in terminal_records
                    ],
                    "closing": self.coordinator.is_closing,
                }
            )
            self.call_sequence.append(
                "refresh-spawn:"
                f"{','.join(record.spec.task_type for record in terminal_records)}"
                f"->{spec.task_type}"
            )

        def observed_action(context: object) -> object:
            current = QThread.currentThread()
            task["thread_started"] = True
            task["action_thread_type"] = _type_name(current)
            task["action_thread_name"] = current.objectName()
            self.call_sequence.append(f"worker:{spec.task_type}")
            entered.set()
            if not release.wait(timeout=5.0):
                raise RuntimeError(f"release timed out for {spec.task_type}")
            return action(context)

        self.call_sequence.append(f"coordinator.start:{spec.task_type}")
        if spec.task_type == "download_summary":
            self.summary_post_sequence.append("summary_started")
        elif spec.task_type == "download_post_actions":
            summary_tasks = [
                item for item in self.tasks if item["task_type"] == "download_summary"
            ]
            summary_id = summary_tasks[-1]["task_id"] if summary_tasks else None
            if not summary_tasks:
                self.contract_errors.append("post started without a summary task")
            elif summary_id in self.coordinator._records:
                self.contract_errors.append("post started before summary record removal")
            elif self.window._download_summary_binding is not None:
                self.contract_errors.append("post started before summary binding retirement")
            elif self.summary_post_sequence != ["summary_started", "summary_settled"]:
                self.contract_errors.append(
                    "post started before the expected summary terminal sequence"
                )
            else:
                self.summary_post_sequence.extend(
                    ["summary_removed", "post_started_after_summary_removal"]
                )

        task_id = self._original_start(spec, generation, observed_action)
        record = self.coordinator._records[task_id]
        thread = record.thread
        worker = record.worker
        if thread is None or worker is None:
            release.set()
            raise RuntimeError(f"Coordinator did not create Qt objects for {spec.task_type}")
        task.update(
            {
                "task_id": task_id,
                "thread_type": _type_name(thread),
                "worker_type": _type_name(worker),
                "thread_name": thread.objectName(),
            }
        )
        self.tasks.append(task)
        self.tasks_by_id[task_id] = task
        lifecycle_observer = _TaskLifecycleObserver(
            task,
            task_id,
            self._qt_refs,
            self.window,
        )
        task["finished_observer_type"] = _type_name(lifecycle_observer)
        task["finished_observer_gui_affinity"] = (
            lifecycle_observer.thread() is self.window.thread()
        )
        self._qt_refs[task_id] = (worker, thread, lifecycle_observer)
        thread.finished.connect(lifecycle_observer.observe_thread_finished)
        thread.destroyed.connect(lifecycle_observer.observe_thread_destroyed)
        worker.destroyed.connect(lifecycle_observer.observe_worker_destroyed)
        try:
            run_until(entered.is_set)
            task["thread_running_after_start"] = thread.isRunning()
        finally:
            if spec.task_type in self._hold_task_types:
                self._held_releases[task_id] = release
            else:
                release.set()
        return task_id

    def _release_destroyed_qt_refs(self, tasks: list[dict[str, Any]]) -> None:
        for task in tasks:
            if task["qt_refs_released_after_destroyed"]:
                continue
            if (
                task["thread_destroyed_count"] != 1
                or task["worker_destroyed_count"] != 1
            ):
                continue
            task["qt_refs_released_after_destroyed"] = (
                self._qt_refs.pop(task["task_id"], None) is not None
            )

    def release_qt_refs_after_window_dispose(self) -> None:
        self._qt_refs.clear()

    def release_held_task(self, task_id: str) -> None:
        try:
            release = self._held_releases.pop(task_id)
        except KeyError as exc:
            raise RuntimeError(f"task was not held by the probe: {task_id}") from exc
        release.set()

    def register_control_event(self, event: threading.Event) -> None:
        self._control_events.append(event)

    def _on_settled(
        self, task_id: str, generation: int, outcome: object, payload: object
    ) -> None:
        task = self.tasks_by_id.get(task_id)
        if task is None:
            self.contract_errors.append(f"unobserved task settled: {task_id}")
            return
        task["settled_count"] += 1
        task["outcome"] = outcome.value if isinstance(outcome, TaskState) else repr(outcome)
        task["payload"] = _json_value(payload)
        if (
            task["invocation"] is None
            and outcome is TaskState.CANCELLED
            and task["spec"]["cancellable"]
        ):
            task["expected_outcome"] = TaskState.CANCELLED.value
        task["lifecycle_events"].append("settled")
        self.call_sequence.append(f"settled:{task['task_type']}:{task['outcome']}")
        if task["task_type"] == "download_summary":
            self.summary_post_sequence.append("summary_settled")
        elif task["task_type"] == "download_post_actions":
            self.summary_post_sequence.append("post_settled")

    def _on_removed(self, task_id: str) -> None:
        task = self.tasks_by_id.get(task_id)
        if task is None:
            self.contract_errors.append(f"unobserved task removed: {task_id}")
            return
        task["removed_count"] += 1
        task["lifecycle_events"].append("removed")
        self.call_sequence.append(f"removed:{task['task_type']}")
        if task["task_type"] == "download_post_actions":
            self.summary_post_sequence.append("post_removed")

    def wrap_controller_methods(self) -> None:
        controller = self.window.controller
        for method_name in REQUIRED_CONTROLLER_CALLS:
            original = getattr(controller, method_name)

            def observed(
                *args: object,
                _method_name: str = method_name,
                _original: Callable[..., object] = original,
                **kwargs: object,
            ) -> object:
                current = QThread.currentThread()
                self.controller_calls.append(
                    {
                        "method": _method_name,
                        "controller_type": _type_name(controller),
                        "thread_type": _type_name(current),
                        "thread_name": current.objectName(),
                        "args": _json_value(args),
                        "kwargs": _json_value(kwargs),
                    }
                )
                self.call_sequence.append(f"controller:{_method_name}")
                return _original(*args, **kwargs)

            self._replace_attribute(controller, method_name, observed)

    def install_service_boundaries(self, summary: DownloadSummary) -> None:
        controller = self.window.controller
        self._replace_attribute(controller.engine, "external_running", lambda: False)
        controller.engine.current = None
        controller.collector.process = _ManagedCollectorProcess()
        self._replace_attribute(
            controller.collector,
            "health",
            lambda *_args, **_kwargs: False,
        )

        def record_service(name: str, **details: object) -> None:
            self.service_calls.append(
                {"service": name, **{key: _json_value(value) for key, value in details.items()}}
            )
            self.call_sequence.append(f"service:{name}")

        def summarize_service(*args: object, **kwargs: object) -> DownloadSummary:
            record_service("engine.summarize_finished_run", args=args, kwargs=kwargs)
            return summary

        def stop_service() -> None:
            record_service("collector.stop")
            controller.collector.process = None

        migration = MigrationResult((), (), 0, 0, 1, 1, 0, 0)

        def migrate_service(source: Path) -> MigrationResult:
            record_service("collector.migrate_old_data", source=source)
            return migration

        backup_path = controller.paths.backups / "phase2-probe-full-backup"

        def backup_service(*args: object, **kwargs: object) -> Path:
            context = kwargs.get("context")
            mode = self._backup_protocol_mode
            if mode == "cancel_before_critical":
                self._backup_protocol_entered.set()
                if not self._backup_protocol_release.wait(timeout=5.0):
                    raise RuntimeError("backup cancellation protocol release timed out")
            if context is not None:
                context.enter_critical_phase()
            if mode == "critical_before_cancel":
                self._backup_protocol_entered.set()
                if not self._backup_protocol_release.wait(timeout=5.0):
                    raise RuntimeError("backup critical protocol release timed out")
            record_service("backup.create_full_snapshot", args=args, kwargs=kwargs)
            return backup_path

        self_test = IndexResult(0, "phase2 probe self-test")

        def self_test_service(*args: object, **kwargs: object) -> IndexResult:
            context = kwargs.get("context")
            if context is not None:
                context.enter_critical_phase()
            record_service("indexer.cleanup_self_test", args=args, kwargs=kwargs)
            return self_test

        self._replace_attribute(
            controller.engine,
            "summarize_finished_run",
            summarize_service,
        )
        self._replace_attribute(controller.collector, "stop", stop_service)
        self._replace_attribute(
            controller.collector,
            "migrate_old_data",
            migrate_service,
        )
        self._replace_attribute(
            controller.backup,
            "create_full_snapshot",
            backup_service,
        )
        self._replace_attribute(
            controller.indexer,
            "cleanup_self_test",
            self_test_service,
        )

        def observe_real_service(service: object, method_name: str, evidence_name: str) -> None:
            original = getattr(service, method_name)

            def observed(*args: object, **kwargs: object) -> object:
                record_service(
                    evidence_name,
                    object_type=_type_name(service),
                    args=args,
                    kwargs=kwargs,
                    delegated_to_real_method=True,
                )
                return original(*args, **kwargs)

            self._replace_attribute(service, method_name, observed)

        observe_real_service(controller.task_order, "list_tasks", "task_order.list_tasks")
        observe_real_service(
            controller.engine_updates,
            "preview",
            "engine_updates.preview",
        )

    def wait_for_task_type(self, task_type: str, start_index: int) -> None:
        run_until(
            lambda: any(
                task["task_type"] == task_type for task in self.tasks[start_index:]
            )
        )
        self.wait_until_clean(start_index)

    def wait_until_clean(self, start_index: int = 0) -> None:
        def protocol_complete() -> bool:
            current_tasks = self.tasks[start_index:]
            return bool(current_tasks) and all(
                task["settled_count"] == 1
                and task["removed_count"] == 1
                and task["thread_finished_count"] == 1
                for task in current_tasks
            )

        run_until(
            lambda: protocol_complete()
            and not self.coordinator._records
            and not self.window._background_bindings
            and not self.window._background_pending
        )

        def qt_objects_destroyed() -> bool:
            QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
            return all(
                task["thread_destroyed_count"] == 1
                and task["worker_destroyed_count"] == 1
                for task in self.tasks[start_index:]
            )

        run_until(qt_objects_destroyed)
        self._release_destroyed_qt_refs(self.tasks[start_index:])

    def invoke_required_entry(self, entry: str) -> None:
        start_index = len(self.tasks)
        self.required_entry_calls.append(entry)
        self.call_sequence.append(f"mainwindow:{entry}")
        self.current_invocation = f"required:{entry}"
        try:
            required_main_window_calls(self.window, entry)
        finally:
            self.current_invocation = None
        self.wait_for_task_type(ENTRY_TASK_TYPES[entry], start_index)

    def run_generic_protocol(self) -> None:
        start_index = len(self.tasks)
        critical_entered = threading.Event()
        finish = threading.Event()
        self.register_control_event(finish)
        task_id_holder: list[str] = []
        finished_observation: list[dict[str, Any]] = []
        cancel_before_critical = self.round_number % 2 == 1

        def action(context: object) -> dict[str, object]:
            context.enter_critical_phase()
            critical_entered.set()
            if not finish.wait(timeout=5.0):
                raise RuntimeError("generic protocol finish timed out")
            return {"round": self.round_number, "critical_won": True}

        spec = TaskSpec(
            task_type="phase2_probe",
            display_name="Phase 2 protocol probe",
            resource_keys=frozenset({"phase2_probe"}),
            deduplicate_key=f"phase2_probe:{self.round_number}",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )

        def create_observed_thread(parent: object) -> QThread:
            thread = QThread(parent)
            if cancel_before_critical:

                def observe_before_coordinator() -> None:
                    task_id = task_id_holder[0]
                    record = self.coordinator._records.get(task_id)
                    finished_observation.append(
                        {
                            "observer": "direct_before_coordinator",
                            "record_present": record is not None,
                            "terminal_seen": bool(record and record.terminal_seen),
                            "thread_finished_seen": bool(
                                record and record.thread_finished_seen
                            ),
                        }
                    )

                thread.finished.connect(
                    observe_before_coordinator,
                    Qt.ConnectionType.DirectConnection,
                )
            return thread

        self._hold_task_types.add(spec.task_type)
        self.current_invocation = "generic_protocol"
        try:
            with patch(
                "douk_manager.background.QThread",
                side_effect=create_observed_thread,
            ):
                task_id = self.coordinator.start(spec, self.round_number, action)
            task_id_holder.append(task_id)
            task = self.tasks_by_id[task_id]
            if cancel_before_critical:
                task["expected_outcome"] = TaskState.CANCELLED.value
                cancellation_accepted = self.coordinator.request_cancel(task_id)
                self.release_held_task(task_id)
                if not cancellation_accepted:
                    raise RuntimeError("pre-critical generic cancellation was rejected")
            else:
                self.release_held_task(task_id)
                run_until(critical_entered.is_set)
                cancellation_accepted = self.coordinator.request_cancel(task_id)
                if cancellation_accepted:
                    raise RuntimeError("critical generic cancellation was accepted")
                finish.set()
            self.wait_until_clean(start_index)
            self.generic_protocol = {
                "task_id": task_id,
                "mode": (
                    "cancel_before_critical"
                    if cancel_before_critical
                    else "critical_before_cancel"
                ),
                "cancellation_accepted": cancellation_accepted,
                "expected_outcome": task["expected_outcome"],
                "actual_outcome": task["outcome"],
                "finished_observation": finished_observation,
            }
        finally:
            self.current_invocation = None
            self._hold_task_types.discard(spec.task_type)
            for release in self._held_releases.values():
                release.set()
            finish.set()

    def _ui_snapshot(self) -> dict[str, dict[str, object]]:
        snapshot: dict[str, dict[str, object]] = {}
        for name in (
            "queue_output",
            "collector_output",
            "overview_output",
            "post_output",
            "settings_output",
        ):
            widget = getattr(self.window, name)
            value = widget.toPlainText()
            snapshot[name] = {
                "length": len(value),
                "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            }
        return snapshot

    def run_closing_protocol(self) -> None:
        start_index = len(self.tasks)
        mode = (
            "cancel_before_critical"
            if self.round_number % 2 == 1
            else "critical_before_cancel"
        )
        self._backup_protocol_mode = mode
        self.current_invocation = "closing_protocol:manual_backup"
        try:
            required_main_window_calls(self.window, "manual_backup")
        finally:
            self.current_invocation = None
        run_until(self._backup_protocol_entered.is_set)
        protocol_tasks = self.tasks[start_index:]
        if len(protocol_tasks) != 1 or protocol_tasks[0]["task_type"] != "full_volume_backup":
            raise RuntimeError(f"closing protocol did not start one backup: {protocol_tasks!r}")
        task = protocol_tasks[0]
        task_id = task["task_id"]
        expected_outcome = (
            TaskState.CANCELLED if mode == "cancel_before_critical" else TaskState.SUCCEEDED
        )
        task["expected_outcome"] = expected_outcome.value
        before_ui = self._ui_snapshot()
        refresh_count_before = len(self.refresh_events)
        self.window.controller.startup_state = StartupState.CLOSING
        close_completed_immediately = self.coordinator.begin_closing()
        for active_task_id, active_record in self.coordinator._records.items():
            if active_record.state is TaskState.CANCELLING:
                self.tasks_by_id[active_task_id]["expected_outcome"] = (
                    TaskState.CANCELLED.value
                )
        record = self.coordinator._records[task_id]
        context = record.operation_context
        if context is None:
            raise RuntimeError("closing backup did not receive an OperationContext")
        arbitration = {
            "cancel_requested": context.cancel_requested,
            "critical_to_completion": context.critical_to_completion,
        }
        self._backup_protocol_release.set()
        self.wait_until_clean(start_index)
        after_ui = self._ui_snapshot()
        refresh_count_after = len(self.refresh_events)

        tasks_before_rejected_entry = len(self.tasks)
        generations_before_rejected_entry = dict(self.window._background_generations)
        self.window.refresh_tasks()
        self.closing_protocol = {
            "task_id": task_id,
            "mode": mode,
            "close_completed_immediately": close_completed_immediately,
            "arbitration": arbitration,
            "expected_outcome": expected_outcome.value,
            "actual_outcome": task["outcome"],
            "ui_before": before_ui,
            "ui_after": after_ui,
            "ui_unchanged": before_ui == after_ui,
            "refresh_count_before": refresh_count_before,
            "refresh_count_after": refresh_count_after,
            "late_refresh_suppressed": refresh_count_before == refresh_count_after,
            "closing_entry_rejected": (
                len(self.tasks) == tasks_before_rejected_entry
                and self.window._background_generations
                == generations_before_rejected_entry
            ),
        }

    def cleanup_before_dispose(self) -> dict[str, object]:
        cleanup_errors: list[str] = []
        for release in self._held_releases.values():
            release.set()
        for event in self._control_events:
            event.set()
        try:
            if (
                self.coordinator._records
                or self.window._background_bindings
                or self.window._background_pending
            ):
                run_until(
                    lambda: not self.coordinator._records
                    and not self.window._background_bindings
                    and not self.window._background_pending
                )
            run_until(
                lambda: (
                    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                    is None
                )
                and not self.window.findChildren(QThread)
            )
            self._release_destroyed_qt_refs(self.tasks)
        except Exception:
            cleanup_errors.append(traceback.format_exc())
        for signal, slot in (
            (self.coordinator.task_settled, self._settled_slot),
            (self.coordinator.task_removed, self._removed_slot),
            (self.coordinator.internal_error, self._internal_error_slot),
        ):
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                cleanup_errors.append(traceback.format_exc())
        for target, name, had_instance_value, previous in reversed(self._restorations):
            try:
                if had_instance_value:
                    setattr(target, name, previous)
                else:
                    delattr(target, name)
            except (AttributeError, RuntimeError):
                cleanup_errors.append(traceback.format_exc())
        self._restorations.clear()
        return {
            "active_records_after_cleanup": len(self.coordinator._records),
            "bindings_after_cleanup": len(self.window._background_bindings),
            "pending_after_cleanup": len(self.window._background_pending),
            "qthreads_after_cleanup": len(self.window.findChildren(QThread)),
            "qt_refs_before_window_dispose": len(self._qt_refs),
            "errors": cleanup_errors,
        }

    def validate(self) -> None:
        errors = self.contract_errors
        if self.required_entry_calls != list(REQUIRED_ENTRIES):
            errors.append(f"required entry order mismatch: {self.required_entry_calls!r}")
        expected_summary_post = [
            "summary_started",
            "summary_settled",
            "summary_removed",
            "post_started_after_summary_removal",
            "post_settled",
            "post_removed",
        ]
        if self.summary_post_sequence != expected_summary_post:
            errors.append(
                f"summary/post sequence mismatch: {self.summary_post_sequence!r}"
            )
        task_types = {task["task_type"] for task in self.tasks}
        required_task_types = {
            "download_summary",
            "download_post_actions",
            *ENTRY_TASK_TYPES.values(),
        }
        missing_task_types = required_task_types - task_types
        if missing_task_types:
            errors.append(f"missing real task types: {sorted(missing_task_types)!r}")
        task_ids = [task["task_id"] for task in self.tasks]
        if any(not task_id for task_id in task_ids) or len(task_ids) != len(set(task_ids)):
            errors.append("task IDs were missing or reused")

        deduplicate_prefixes = {
            "download_summary": "download_summary:",
            "download_post_actions": "download_post_actions:",
            "engine_update_preview": "engine_update_preview:",
        }
        for task_type, expected in EXPECTED_REAL_SPECS.items():
            candidates = [task for task in self.tasks if task["task_type"] == task_type]
            if task_type == "full_volume_backup":
                candidates = [
                    task
                    for task in candidates
                    if task["invocation"] == "required:manual_backup"
                ]
            if len(candidates) != 1:
                errors.append(
                    f"expected one primary {task_type} task, found {len(candidates)}"
                )
                continue
            task = candidates[0]
            actual_spec = task["spec"]
            if set(actual_spec) != set(TaskSpec.__dataclass_fields__):
                errors.append(f"{task_type} did not expose every TaskSpec field")
                continue
            expected_spec = dict(expected)
            prefix = deduplicate_prefixes.get(task_type)
            if prefix is not None:
                deduplicate_key = actual_spec["deduplicate_key"]
                if not isinstance(deduplicate_key, str) or not deduplicate_key.startswith(
                    prefix
                ):
                    errors.append(
                        f"{task_type} deduplicate key mismatch: {deduplicate_key!r}"
                    )
                expected_spec["deduplicate_key"] = deduplicate_key
            if actual_spec != expected_spec:
                errors.append(
                    f"{task_type} TaskSpec mismatch: actual={actual_spec!r} "
                    f"expected={expected_spec!r}"
                )
            elif task["generation"] != 1:
                errors.append(
                    f"primary {task_type} generation was {task['generation']!r}"
                )
            else:
                self.validated_real_specs.append(task_type)

        generic_tasks = [
            task for task in self.tasks if task["invocation"] == "generic_protocol"
        ]
        if len(generic_tasks) != 1:
            errors.append(f"expected one generic protocol task, found {len(generic_tasks)}")
        else:
            generic = generic_tasks[0]
            expected_generic_spec = {
                "task_type": "phase2_probe",
                "display_name": "Phase 2 protocol probe",
                "resource_keys": ["phase2_probe"],
                "deduplicate_key": f"phase2_probe:{self.round_number}",
                "cancellable": True,
                "close_policy": "CANCEL",
                "refresh_targets": [],
                "critical_write_started": False,
                "dynamic_cancellation": True,
                "allow_during_closing": False,
            }
            if generic["spec"] != expected_generic_spec:
                errors.append(f"generic protocol TaskSpec mismatch: {generic['spec']!r}")

        controller_methods = [call["method"] for call in self.controller_calls]
        for method_name in REQUIRED_CONTROLLER_CALLS:
            if method_name not in controller_methods:
                errors.append(f"real Controller method was not called: {method_name}")
        for task in self.tasks:
            if not isinstance(task["generation"], int) or task["generation"] < 1:
                errors.append(f"invalid generation for {task['task_type']}")
            if not task["thread_started"] or not task["thread_running_after_start"]:
                errors.append(f"task did not prove a running QThread: {task['task_type']}")
            if task["worker_type"] != "douk_manager.background.TaskWorker":
                errors.append(f"task did not use the real Worker: {task['task_type']}")
            if task["thread_type"] != "PySide6.QtCore.QThread":
                errors.append(f"task did not use a real QThread: {task['task_type']}")
            if not task["finished_observer_type"].endswith(
                "._TaskLifecycleObserver"
            ):
                errors.append(
                    f"task did not use a QObject finished observer: {task['task_type']}"
                )
            if not task["finished_observer_gui_affinity"]:
                errors.append(
                    f"finished observer had wrong thread affinity: {task['task_type']}"
                )
            if task["action_thread_name"] != task["thread_name"]:
                errors.append(f"task action ran on the wrong thread: {task['task_type']}")
            for field in (
                "settled_count",
                "removed_count",
                "thread_finished_count",
                "thread_destroyed_count",
                "worker_destroyed_count",
            ):
                if task[field] != 1:
                    errors.append(
                        f"{task['task_type']} has {field}={task[field]!r}"
                    )
            for field in (
                "thread_wrapper_retained_during_destroyed",
                "worker_wrapper_retained_during_destroyed",
                "qt_refs_released_after_destroyed",
            ):
                if not task[field]:
                    errors.append(
                        f"{task['task_type']} did not prove {field}"
                    )
            if task["outcome"] != task["expected_outcome"]:
                errors.append(
                    f"{task['task_type']} outcome was {task['outcome']!r}; "
                    f"expected {task['expected_outcome']!r}"
                )
        service_names = [call["service"] for call in self.service_calls]
        for service_name in ("task_order.list_tasks", "engine_updates.preview"):
            if service_name not in service_names:
                errors.append(f"real product service was not called: {service_name}")

        expected_generic_mode = (
            "cancel_before_critical"
            if self.round_number % 2 == 1
            else "critical_before_cancel"
        )
        if self.generic_protocol.get("mode") != expected_generic_mode:
            errors.append(f"generic protocol mode mismatch: {self.generic_protocol!r}")
        if self.generic_protocol.get("actual_outcome") != self.generic_protocol.get(
            "expected_outcome"
        ):
            errors.append(f"generic protocol outcome mismatch: {self.generic_protocol!r}")
        if self.round_number % 2 == 1:
            direct = self.generic_protocol.get("finished_observation")
            if direct != [
                {
                    "observer": "direct_before_coordinator",
                    "record_present": True,
                    "terminal_seen": True,
                    "thread_finished_seen": False,
                }
            ]:
                errors.append(f"direct finished ordering was not proved: {direct!r}")

        if self.closing_protocol.get("mode") != expected_generic_mode:
            errors.append(f"closing protocol mode mismatch: {self.closing_protocol!r}")
        if self.closing_protocol.get("actual_outcome") != self.closing_protocol.get(
            "expected_outcome"
        ):
            errors.append(f"closing protocol outcome mismatch: {self.closing_protocol!r}")
        arbitration = self.closing_protocol.get("arbitration", {})
        expected_arbitration = (
            {"cancel_requested": True, "critical_to_completion": False}
            if self.round_number % 2 == 1
            else {"cancel_requested": False, "critical_to_completion": True}
        )
        if arbitration != expected_arbitration:
            errors.append(f"closing arbitration mismatch: {arbitration!r}")
        for field in ("ui_unchanged", "late_refresh_suppressed", "closing_entry_rejected"):
            if not self.closing_protocol.get(field):
                errors.append(f"closing protocol did not prove {field}")
        if any(event["closing"] for event in self.refresh_events):
            errors.append("a refresh was emitted while the window was closing")

        if self.internal_errors:
            errors.append(f"Coordinator internal errors: {self.internal_errors!r}")
        if self.coordinator._records:
            errors.append("Coordinator retained active records")
        if self.window._background_bindings:
            errors.append("MainWindow retained background bindings")
        if self.window._background_pending:
            errors.append("MainWindow retained pending requests")
        if self.window.findChildren(QThread):
            errors.append("MainWindow retained QThread children")
        if self._qt_refs:
            errors.append(
                "probe retained destroyed Qt wrappers after callback drain: "
                f"{sorted(self._qt_refs)!r}"
            )
        if errors:
            raise RuntimeError("; ".join(errors))

    def evidence(self) -> dict[str, Any]:
        return {
            "round": self.round_number,
            "status": "passed",
            "window_type": _type_name(self.window),
            "coordinator_type": _type_name(self.coordinator),
            "controller_type": _type_name(self.window.controller),
            "product_object_types": dict(self.product_object_types),
            "window_instances": 1,
            "required_entry_calls": list(self.required_entry_calls),
            "summary_post_sequence": list(self.summary_post_sequence),
            "controller_calls": list(self.controller_calls),
            "service_calls": list(self.service_calls),
            "refresh_events": list(self.refresh_events),
            "call_sequence": list(self.call_sequence),
            "tasks": list(self.tasks),
            "validated_real_specs": list(self.validated_real_specs),
            "generic_protocol": dict(self.generic_protocol),
            "closing_protocol": dict(self.closing_protocol),
            "active_records_after": len(self.coordinator._records),
            "bindings_after": len(self.window._background_bindings),
            "pending_after": len(self.window._background_pending),
            "qthreads_after": len(self.window.findChildren(QThread)),
            "coordinator_internal_errors": list(self.internal_errors),
            "contract_errors": list(self.contract_errors),
            "exception": None,
        }


def _dispose_window(window: MainWindow) -> dict[str, object]:
    app = QApplication.instance()
    for timer_name in (
        "poll_timer",
        "result_refresh_timer",
        "startup_recheck_timer",
        "shutdown_timer",
    ):
        timer = getattr(window, timer_name, None)
        if timer is not None:
            timer.stop()
    logger = getattr(window.controller, "logger", None)
    handlers = list(logger.handlers) if logger is not None else []
    destroyed: list[bool] = []
    window.destroyed.connect(lambda *_args: destroyed.append(True))
    window.hide()
    window.deleteLater()
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
    if app is not None:
        app.processEvents()
    if not destroyed:
        run_until(lambda: bool(destroyed))
    if logger is not None:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()
    return {
        "window_destroyed_count": len(destroyed),
        "logger_handlers_closed": len(handlers),
    }


def run_round(round_number: int) -> dict[str, Any]:
    app = QApplication.instance() or QApplication([])
    window: MainWindow | None = None
    observer: _RoundObserver | None = None
    evidence: dict[str, Any]
    with tempfile.TemporaryDirectory(prefix=f"phase2-qt-round-{round_number:02d}-") as directory:
        base = Path(directory)
        paths, task_path, archive = _write_synthetic_inputs(base)
        with patch.dict(os.environ, {"DOUK_MANAGER_HOME": os.fspath(paths.root)}):
            try:
                window = construct_real_main_window()
                window.show()
                app.processEvents()
                window.poll_timer.stop()
                window.controller.startup_state = StartupState.READY
                window.controller.startup_backup = paths.backups / "startup-backup"
                window.controller.read_only_reason = ""
                window.engine_update_zip.setText(os.fspath(archive))

                observer = _RoundObserver(window, round_number)
                observer.call_sequence.extend(
                    ["construct_real_main_window", "mainwindow.show"]
                )
                observer.install_service_boundaries(_summary_payload())
                observer.wrap_controller_methods()

                run = EngineRun(
                    process=_ExitedProcess(),  # type: ignore[arg-type]
                    started_at=datetime.now(),
                    task_log=paths.download_task_logs / f"phase2-round-{round_number}.log",
                    planned_accounts=(),
                    native_log_snapshot=(),
                    native_log_dir=paths.download_task_logs,
                    task_template=task_path.name,
                )
                assessment = EngineExitAssessment(
                    exit_code=0,
                    normal_exit=True,
                    headline="phase2 probe process exited",
                    detail="phase2 probe summary follows",
                    log_status="synthetic",
                )
                window.queue_pending = [task_path]
                window.queue_paused = True
                window.queue_active = True
                window.queue_current = run
                window.controller._download_lifecycle_active = True
                window._mark_download_started(run)

                observer.call_sequence.append("mainwindow:_start_download_summary")
                start_index = len(observer.tasks)
                observer.current_invocation = "summary_post_chain"
                try:
                    window._start_download_summary(run, 0, assessment)
                finally:
                    observer.current_invocation = None
                observer.wait_for_task_type("download_post_actions", start_index)
                window.queue_pending.clear()
                window.queue_current = None
                window.queue_active = False
                window._release_download_lifecycle()

                for entry in REQUIRED_ENTRIES:
                    observer.invoke_required_entry(entry)

                observer.run_generic_protocol()
                observer.run_closing_protocol()
                observer.wait_until_clean()
                observer.validate()
                evidence = observer.evidence()
            except Exception as exc:
                if observer is not None:
                    evidence = observer.evidence()
                else:
                    evidence = {
                        "round": round_number,
                        "status": "failed",
                        "window_type": _type_name(window) if window is not None else None,
                        "window_instances": int(window is not None),
                        "tasks": [],
                    }
                evidence["status"] = "failed"
                evidence["exception"] = {
                    "type": type(exc).__name__,
                    "message": str(exc),
                    "traceback": traceback.format_exc(),
                }
            finally:
                if observer is not None:
                    evidence["round_cleanup"] = observer.cleanup_before_dispose()
                if window is not None:
                    evidence["window_cleanup"] = _dispose_window(window)
                if observer is not None:
                    observer.release_qt_refs_after_window_dispose()
                    evidence["round_cleanup"]["qt_refs_after_cleanup"] = len(
                        observer._qt_refs
                    )
                cleanup_errors = evidence.get("round_cleanup", {}).get("errors", [])
                window_destroyed = evidence.get("window_cleanup", {}).get(
                    "window_destroyed_count", 0
                )
                if cleanup_errors or window_destroyed != 1:
                    evidence["status"] = "failed"
                    if evidence.get("exception") is None:
                        evidence["exception"] = {
                            "type": "RoundCleanupError",
                            "message": (
                                f"cleanup_errors={cleanup_errors!r}; "
                                f"window_destroyed_count={window_destroyed!r}"
                            ),
                            "traceback": "",
                        }
    return evidence


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Phase 2 real Qt lifecycle probe")
    parser.add_argument("--rounds", type=int, default=ROUNDS)
    parser.add_argument("--evidence-jsonl", type=Path)
    args = parser.parse_args(argv)
    if args.rounds < 1:
        parser.error("--rounds must be at least 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    app = QApplication.instance() or QApplication([])
    diagnostics: list[str] = []
    evidence_file = None
    if args.evidence_jsonl is not None:
        args.evidence_jsonl.parent.mkdir(parents=True, exist_ok=True)
        evidence_file = args.evidence_jsonl.open("x", encoding="utf-8")
    previous_handler = qInstallMessageHandler(
        lambda _mode, _context, message: diagnostics.append(message)
    )
    passed = 0
    try:
        for round_number in range(1, args.rounds + 1):
            diagnostic_start = len(diagnostics)
            evidence = run_round(round_number)
            evidence["qt_diagnostics"] = diagnostics[diagnostic_start:]
            forbidden = [
                message
                for message in evidence["qt_diagnostics"]
                if any(
                    fragment.casefold() in message.casefold()
                    for fragment in FORBIDDEN_DIAGNOSTICS
                )
            ]
            evidence["forbidden_qt_diagnostics"] = forbidden
            if forbidden:
                evidence["status"] = "failed"
                evidence["exception"] = {
                    "type": "ForbiddenQtDiagnostic",
                    "message": repr(forbidden),
                    "traceback": "",
                }
            line = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
            print(line, flush=True)
            if evidence_file is not None:
                evidence_file.write(line + "\n")
                evidence_file.flush()
            if evidence["status"] != "passed":
                print(
                    f"phase2 Qt lifecycle probe failed in round {round_number}",
                    file=sys.stderr,
                )
                return 1
            passed += 1
    finally:
        qInstallMessageHandler(previous_handler)
        if evidence_file is not None:
            evidence_file.close()
        app.processEvents()
    print(f"phase2 Qt lifecycle probe: {passed}/{args.rounds} rounds passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
