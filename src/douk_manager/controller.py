from __future__ import annotations

import logging
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from douk_manager.config import AppConfig, ManagedPaths, application_root, update_config
from douk_manager.core.backup import BackupService
from douk_manager.core.download_summary import DownloadSummary
from douk_manager.core.engine import (
    BATCH_RUN_COMMAND,
    ENGINE_MODE_MONITOR,
    MONITOR_RUN_COMMAND,
    EngineRun,
    EngineService,
)
from douk_manager.core.engine_update import (
    EnginePackagePreview,
    EngineUpdateResult,
    EngineUpdateService,
)
from douk_manager.core.json_store import read_json
from douk_manager.core.locks import critical_section
from douk_manager.core.settings_tasks import EarliestRule, GeneratedTask, SettingsTaskService
from douk_manager.core.task_order import TaskOrderService
from douk_manager.core.result_history import RecentPrivateMatch, ResultHistoryService
from douk_manager.core.result_dashboard import (
    DashboardFileFingerprint,
    ResultDashboardService,
)
from douk_manager.integrations.collector import CollectorService, MigrationResult
from douk_manager.integrations.indexer import IndexResult, IndexService
from douk_manager.integrations.screenshots import ScreenshotPreview, ScreenshotResult, ScreenshotService
from douk_manager.logging_setup import setup_logging
from douk_manager.startup import StartupSafetyResult, StartupState

if TYPE_CHECKING:
    from douk_manager.operation import OperationContext


class ControllerError(RuntimeError):
    pass


class ManagerController:
    _DEGRADED_PATH_FIELDS = frozenset(
        {"engine_exe", "video_root", "index_root", "old_screenshot_dir"}
    )

    def __init__(self) -> None:
        self.root = application_root()
        default_paths = ManagedPaths.from_config(AppConfig(), self.root)
        default_paths.ensure_manager_directories()
        self.config = AppConfig.load(default_paths.config_file)
        self.paths = ManagedPaths.from_config(self.config, self.root)
        self.paths.ensure_manager_directories()
        self.logger, self.log_path = setup_logging(self.paths.manager_logs)
        self.startup_backup: Path | None = None
        self.read_only_reason = ""
        self.startup_state = StartupState.BOOTSTRAPPING
        self.startup_generation = 0
        self._startup_result: StartupSafetyResult | None = None
        self._last_collector_running = False
        self._last_engine_running = False
        self._download_lifecycle_active = False
        self._build_services()

    def _build_services(self) -> None:
        self.backup = BackupService(self.paths)
        self.tasks = SettingsTaskService(self.paths, self.backup)
        self.task_order = TaskOrderService(self.paths)
        self.results = ResultHistoryService(self.paths.download_task_logs)
        self.result_dashboard = ResultDashboardService(self.paths.download_task_logs)
        self.engine = EngineService(self.paths, self.config, self.backup)
        self.engine_updates = EngineUpdateService(self.paths, self.backup)
        self.collector = CollectorService(self.config, self.paths)
        self.screenshots = ScreenshotService()
        self.indexer = IndexService()

    def _current_startup_state(self) -> StartupState:
        # Older tests construct partial controllers with __new__. Real instances
        # always receive BOOTSTRAPPING in __init__ and cannot use this fallback.
        return getattr(self, "startup_state", StartupState.READY)

    def begin_startup_check(self, generation: int) -> bool:
        if self._current_startup_state() in (
            StartupState.SAFETY_CHECKING,
            StartupState.CLOSING,
        ):
            return False
        current_generation = getattr(self, "startup_generation", 0)
        if generation <= current_generation:
            return False
        self.startup_generation = generation
        self.startup_state = StartupState.SAFETY_CHECKING
        self.startup_backup = None
        return True

    def apply_startup_result(self, result: StartupSafetyResult) -> bool:
        if self._current_startup_state() is StartupState.CLOSING:
            return False
        if self._current_startup_state() is not StartupState.SAFETY_CHECKING:
            return False
        if result.generation != getattr(self, "startup_generation", 0):
            return False

        self._startup_result = result
        self.startup_backup = result.startup_backup
        probe = result.process_probe
        if probe is not None:
            self._last_engine_running = probe.state.name != "SAFE"
        if result.success and result.state is StartupState.READY:
            self.startup_state = StartupState.READY
            self.read_only_reason = ""
        else:
            self.startup_state = StartupState.DEGRADED_READ_ONLY
            self.read_only_reason = result.summary or result.details
        return True

    def begin_closing(self) -> bool:
        if self._current_startup_state() is StartupState.CLOSING:
            return False
        self.startup_state = StartupState.CLOSING
        return True

    def startup_error_text(self) -> str:
        result = getattr(self, "_startup_result", None)
        if result is not None and not result.success:
            return "\n".join(part for part in (result.summary, result.details) if part)
        return self.read_only_reason

    def require_operational_ready(self, operation: str) -> None:
        state = self._current_startup_state()
        if state is StartupState.READY:
            return
        if state is StartupState.DEGRADED_READ_ONLY:
            reason = self.startup_error_text() or "启动安全检查未通过。"
        elif state is StartupState.CLOSING:
            reason = "应用正在关闭。"
        else:
            reason = "启动安全检查尚未完成。"
        raise ControllerError(f"{operation}不可用：{reason}")

    def _require_operational_ready_with_context(
        self,
        operation: str,
        *,
        context: OperationContext | None = None,
    ) -> None:
        if context is not None:
            context.raise_if_cancelled()
        try:
            self.require_operational_ready(operation)
        except ControllerError:
            if context is not None:
                context.raise_if_cancelled()
            raise
        if context is not None:
            context.raise_if_cancelled()

    def _require_runtime_diagnostic(
        self,
        *,
        context: OperationContext | None = None,
    ) -> None:
        if context is not None:
            context.raise_if_cancelled()
        state = self._current_startup_state()
        if state not in (StartupState.READY, StartupState.DEGRADED_READ_ONLY):
            if context is not None:
                context.raise_if_cancelled()
            raise ControllerError(f"刷新运行状态不可用：当前状态为 {state.value}。")
        if context is not None:
            context.raise_if_cancelled()

    def require_managed_runtime_control(self, operation: str, *, managed: bool) -> None:
        state = self._current_startup_state()
        if state not in (StartupState.READY, StartupState.CLOSING):
            raise ControllerError(f"{operation}不可用：当前状态为 {state.value}。")
        if not managed:
            raise ControllerError(f"{operation}不可用：没有管理器持有的运行任务。")

    def health(
        self,
        *,
        check_processes: bool = False,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        if context is not None:
            context.raise_if_cancelled()
        result: dict[str, Any] = self.paths.health()
        if context is not None:
            context.raise_if_cancelled()
        if check_processes:
            self._last_collector_running = self.collector.health()
        else:
            if self.collector.process is not None:
                self._last_collector_running = self.collector.running
        if context is not None:
            context.raise_if_cancelled()
        if check_processes:
            self._last_engine_running = self.engine.external_running()
        else:
            if self.engine.current is not None:
                self._last_engine_running = self.engine.current.running
        if context is not None:
            context.raise_if_cancelled()
        result["collector_running"] = self._last_collector_running
        result["engine_running"] = self._last_engine_running
        current = self.engine.current
        if current is not None and current.running:
            result["engine_mode"] = current.mode
        elif result["engine_running"] and self.paths.active_settings.is_file():
            try:
                command = read_json(self.paths.active_settings).get("run_command")
            except Exception:
                command = None
            result["engine_mode"] = (
                ENGINE_MODE_MONITOR if command == MONITOR_RUN_COMMAND else "batch"
            )
        else:
            result["engine_mode"] = ""
        if context is not None:
            context.raise_if_cancelled()
        result["monitor_running"] = result["engine_mode"] == ENGINE_MODE_MONITOR
        result["startup_backup"] = str(self.startup_backup or "")
        result["read_only_reason"] = self.read_only_reason
        if self.paths.master_settings.is_file():
            try:
                master = read_json(self.paths.master_settings)
                accounts = master.get("accounts_urls", [])
                result["master_positions"] = len(accounts) if isinstance(accounts, list) else 0
                result["master_valid_urls"] = (
                    sum(
                        1
                        for item in accounts
                        if isinstance(item, dict) and str(item.get("url", "")).strip()
                    )
                    if isinstance(accounts, list)
                    else 0
                )
            except Exception as exc:
                result["master_error"] = str(exc)
        if context is not None:
            context.raise_if_cancelled()
        return result

    def runtime_status_snapshot(
        self,
        *,
        context: OperationContext | None = None,
    ) -> dict[str, Any]:
        self._require_runtime_diagnostic(context=context)
        return self.health(check_processes=True, context=context)

    def try_startup_backup(self) -> str:
        required = self.paths.health()
        if not all(
            required.get(key, False)
            for key in (
                "engine_exe",
                "volume",
                "master_settings",
                "active_settings",
                "database",
            )
        ):
            self.read_only_reason = "下载引擎或唯一正式 Volume 尚未完整识别。"
            return self.read_only_reason
        self._last_engine_running = self.engine.external_running()
        if self._last_engine_running:
            self.read_only_reason = "检测到下载引擎正在运行，未执行启动前备份。"
            return self.read_only_reason
        try:
            with critical_section(self.paths.lock_file, timeout=5.0):
                self.startup_backup = self.backup.create_critical_snapshot(
                    "Startup",
                    {"operation": "application_start"},
                    keep_latest=3,
                )
            if self.engine.recover_batch_command_if_idle():
                self.logger.warning(
                    "检测到上次后台监听遗留 run_command=%s，已恢复为 %s。",
                    MONITOR_RUN_COMMAND,
                    BATCH_RUN_COMMAND,
                )
            self.read_only_reason = ""
            self.logger.info("启动前关键文件备份完成：%s", self.startup_backup)
            return f"启动前关键文件备份完成：{self.startup_backup}"
        except Exception as exc:
            self.read_only_reason = f"启动前备份失败：{exc}"
            self.logger.exception("启动前备份失败")
            return self.read_only_reason

    def require_download_lifecycle_idle(self) -> None:
        if getattr(self, "_download_lifecycle_active", False):
            raise ControllerError("下载账号结果汇总或后续动作尚未完成，禁止修改配置或重新备份。")

    def require_safe_write(self) -> None:
        self.require_operational_ready("写入正式数据")
        self.require_download_lifecycle_idle()
        if self.engine.external_running():
            raise ControllerError("下载引擎正在运行，禁止修改配置或重新备份。")
        if self.startup_backup is None:
            message = self.try_startup_backup()
            if self.startup_backup is None:
                raise ControllerError(message)
        if self.read_only_reason:
            raise ControllerError(self.read_only_reason)

    def require_collector_start(self) -> None:
        self.require_operational_ready("启动账号采集服务")
        if getattr(self, "_download_lifecycle_active", False):
            return
        if self.engine.external_running():
            return
        self.require_safe_write()

    def reconfigure(self, values: dict[str, Any]) -> str:
        has_startup_state = hasattr(self, "startup_state")
        state = self._current_startup_state()
        if has_startup_state and state not in (
            StartupState.READY,
            StartupState.DEGRADED_READ_ONLY,
        ):
            raise ControllerError("正式路径只可在就绪或只读修复状态下修改。")
        if state is StartupState.DEGRADED_READ_ONLY:
            invalid_fields = set(values) - self._DEGRADED_PATH_FIELDS
            if invalid_fields:
                names = "、".join(sorted(invalid_fields))
                raise ControllerError(f"只读修复状态只能修改正式路径：{names}")
        runtime_active = getattr(self, "_download_lifecycle_active", False)
        engine_running = (
            self.engine.external_running() if state is StartupState.READY else False
        )
        updated_config: AppConfig | None = None
        if isinstance(self.config, AppConfig) and (runtime_active or engine_running):
            updated_config = update_config(self.config, values)
            previous_values = asdict(self.config)
            updated_values = asdict(updated_config)
            changed_fields = {
                key
                for key in previous_values
                if previous_values[key] != updated_values[key]
            }
            if changed_fields <= {"request_avg_delay"}:
                updated_config.save(self.paths.config_file)
                self.config = updated_config
                self.engine.config = updated_config
                self.logger.info(
                    "成功请求等待均值已热保存：%s秒；当前任务环境保持不变",
                    updated_config.request_avg_delay,
                )
                return (
                    "数据请求成功后等待均值已保存，将从下一次启动下载任务生效；"
                    "当前任务保持原值。"
                )
        self.require_download_lifecycle_idle()
        current_engine = self.engine.current
        if (
            state is StartupState.DEGRADED_READ_ONLY
            and current_engine is not None
            and current_engine.running
        ):
            raise ControllerError("下载引擎仍由管理器持有，请先在就绪状态停止后再修复路径。")
        if state is not StartupState.DEGRADED_READ_ONLY and engine_running:
            raise ControllerError("下载引擎正在运行，禁止切换正式路径或重建服务。")
        if self.collector.running:
            if state is StartupState.DEGRADED_READ_ONLY:
                raise ControllerError("账号采集服务仍由管理器持有，请先在就绪状态停止后再修复路径。")
            self.collector.stop()
        self._last_collector_running = False
        self._last_engine_running = False
        self.config = updated_config or update_config(self.config, values)
        new_paths = ManagedPaths.from_config(self.config, self.root)
        new_paths.ensure_manager_directories()
        self.config.save(new_paths.config_file)
        self.paths = new_paths
        self.startup_backup = None
        self._build_services()
        self.logger.info("路径和任务设置已保存：%s", asdict(self.config))
        if has_startup_state:
            if state is StartupState.READY:
                self.startup_state = StartupState.DEGRADED_READ_ONLY
                self.read_only_reason = "正式路径已更改，等待重新执行启动安全检查。"
            return ""
        self.read_only_reason = ""
        return self.try_startup_backup()

    def update_post_options(self, values: dict[str, Any]) -> bool:
        """保存本次队列选项，不重建路径，也不制造一次多余的启动备份。"""
        self.require_operational_ready("保存队列后续动作")
        self.require_download_lifecycle_idle()
        screenshot_mode = values.get(
            "screenshot_post_mode", self.config.screenshot_post_mode
        )
        index_mode = values.get("index_post_mode", self.config.index_post_mode)
        valid_modes = {"none", "batch", "queue"}
        if screenshot_mode not in valid_modes or index_mode not in valid_modes:
            raise ControllerError("后续动作模式无效。")
        allowed_values = {
            key: value
            for key, value in values.items()
            if key
            in {"screenshot_post_mode", "index_post_mode", "cleanup_after_index"}
        }
        self.config = update_config(self.config, allowed_values)
        self.config.save(self.paths.config_file)
        self.engine.config = self.config
        self.collector.config = self.config
        self.logger.info("本次队列后续动作已保存：%s", allowed_values)
        return True

    def preview_selection(self, expression: str):
        return self.tasks.preview(expression)

    def preview_private_skip(
        self,
        expression: str,
        validity_days: int,
        *,
        context: OperationContext | None = None,
    ):
        self._require_operational_ready_with_context(
            "智能私密预览", context=context
        )
        if validity_days < 1 or validity_days > 3650:
            raise ControllerError("私密账号参考期限必须是 1 到 3650 天的整数。")
        requested = self.tasks.preview(expression)
        decisions = self.results.classify_private_reference(
            requested.selection.numbers, validity_days, context=context
        )
        matches = tuple(
            RecentPrivateMatch(
                a_number=decision.a_number,
                ended_at=decision.row.ended_at,
                task_template=decision.row.task_template,
                task_log=decision.row.task_log,
            )
            for decision in decisions
            if decision.category == "recent_private" and decision.row is not None
        )
        return self.tasks.preview_with_private_filter(
            expression, matches, validity_days, decisions
        )

    def create_task(
        self,
        expression: str,
        rule: EarliestRule,
        persist_master: bool,
        task_name: str,
        activate: bool,
        excluded_numbers: tuple[int, ...] = (),
    ) -> GeneratedTask:
        self.require_safe_write()
        result = self.tasks.create_task(
            expression,
            task_earliest=rule,
            persist_master_earliest=persist_master,
            task_name=task_name or None,
            activate=activate,
            excluded_numbers=excluded_numbers,
        )
        self.logger.info(
            "任务已创建：%s；选择=%s；激活=%s；主档earliest=%s",
            result.task_path,
            result.preview.compact,
            activate,
            persist_master,
        )
        return result

    def delete_tasks(
        self, paths: tuple[Path, ...], *, protected_paths: tuple[Path, ...] = ()
    ) -> tuple[Path, ...]:
        self.require_safe_write()
        result = self.tasks.delete_tasks(paths, protected_paths=protected_paths)
        self.logger.info("任务模板已删除：%s", [path.name for path in result])
        return result

    def result_runs(self, *, limit: int = 500):
        return self.results.list_runs(limit=limit)

    def result_rows(self, *, limit: int = 500):
        return self.results.list_account_rows(limit=limit)

    def result_snapshot(
        self,
        *,
        limit: int = 500,
        context: OperationContext | None = None,
    ):
        self._require_operational_ready_with_context(
            "刷新下载结果", context=context
        )
        return self.results.page_snapshot(limit=limit, context=context)

    def result_dashboard_index(
        self,
        *,
        force_refresh: bool = False,
        context: OperationContext | None = None,
    ):
        self._require_operational_ready_with_context(
            "刷新结果看板任务索引", context=context
        )
        return self.result_dashboard.task_index(
            force_refresh=force_refresh, context=context
        )

    def result_dashboard_snapshot(
        self,
        task_log: Path,
        *,
        expected_fingerprint: DashboardFileFingerprint | None = None,
        force_refresh: bool = False,
        context: OperationContext | None = None,
    ):
        self._require_operational_ready_with_context(
            "读取结果看板任务", context=context
        )
        return self.result_dashboard.selected_task(
            task_log,
            expected_fingerprint=expected_fingerprint,
            force_refresh=force_refresh,
            context=context,
        )

    def generate_batches(
        self, start: int, end: int, size: int, rule: EarliestRule
    ) -> tuple[GeneratedTask, ...]:
        self.require_safe_write()
        result = self.tasks.generate_batches(start, end, size, task_earliest=rule)
        self.logger.info("批次任务已生成：%s 个；A%s-A%s；每批%s", len(result), start, end, size)
        return result

    def list_tasks(
        self, *, context: OperationContext | None = None
    ) -> tuple[Path, ...]:
        self._require_operational_ready_with_context(
            "刷新任务列表", context=context
        )
        result = self.task_order.list_tasks(context=context)
        if self.task_order.last_warning:
            self.logger.warning(self.task_order.last_warning)
        return result

    def save_task_order(self, ordered_paths: list[Path]) -> tuple[Path, ...]:
        self.require_operational_ready("保存任务队列顺序")
        result = self.task_order.save_manual_order(ordered_paths)
        self.logger.info("任务队列顺序已保存：%s", [path.name for path in result])
        return result

    def restore_task_order(self) -> tuple[Path, ...]:
        self.require_operational_ready("恢复任务队列顺序")
        result = self.task_order.restore_natural_order()
        self.logger.info("任务队列已恢复按 A 编号排序")
        return result

    def activate_task(self, path: Path) -> Path:
        self.require_safe_write()
        result = self.tasks.activate_existing_task(path)
        self.logger.info("任务已激活：%s", path)
        return result

    def start_current_download(
        self,
        pause_after_exit: bool = False,
        *,
        task_template: Path | None = None,
    ) -> EngineRun:
        self.require_safe_write()
        result = self.engine.start(
            pause_after_exit=pause_after_exit,
            task_template=task_template,
        )
        self._last_engine_running = True
        self.logger.info(
            "下载任务已启动：模板=%s；已选账号=%s；PID=%s；"
            "每%s个账号暂停%s秒；成功请求均值%s秒；结束后保留窗口=%s；任务日志=%s",
            result.task_template,
            result.selected_accounts,
            result.process.pid,
            self.config.batch_accounts,
            self.config.rest_seconds,
            getattr(self.config, "request_avg_delay", 6.0),
            pause_after_exit,
            result.task_log,
        )
        self._download_lifecycle_active = True
        return result

    def start_monitor(self) -> EngineRun:
        self.require_safe_write()
        if self.collector.health() or self.collector.running:
            raise ControllerError("采集服务运行时不能启动后台监听。")
        result = self.engine.start_monitor()
        self._last_engine_running = True
        self.logger.info(
            "后台剪贴板监听已启动：PID=%s；run_command=%s；任务日志=%s",
            result.process.pid,
            MONITOR_RUN_COMMAND,
            result.task_log,
        )
        return result

    def stop_monitor(self) -> Path | None:
        run = getattr(self.engine, "current", None)
        self.require_managed_runtime_control(
            "停止后台监听",
            managed=run is not None and getattr(run, "mode", None) == ENGINE_MODE_MONITOR,
        )
        result = self.engine.stop_monitor()
        self._last_engine_running = False
        self.logger.info(
            "后台剪贴板监听已停止；run_command 已恢复为 %s；配置=%s",
            BATCH_RUN_COMMAND,
            result,
        )
        return result

    def dismiss_result_review(self, run: EngineRun) -> None:
        self.engine.dismiss_result_review(run)

    def set_result_review(self, run: EngineRun, enabled: bool) -> None:
        self.engine.set_result_review(run, enabled)

    def cancel_current_download(self, run: EngineRun | None = None) -> Path:
        current = getattr(self.engine, "current", None)
        target = run or current
        self.require_managed_runtime_control(
            "取消当前下载",
            managed=target is not None and target is current,
        )
        result = self.engine.cancel_batch(run)
        self._last_engine_running = False
        self.logger.info("用户取消当前下载及全部待执行队列任务：任务日志=%s", result)
        return result

    def summarize_download(
        self, run: EngineRun, exit_code: int | None, ended_at: datetime
    ) -> DownloadSummary:
        result = self.engine.summarize_finished_run(run, exit_code, ended_at)
        self.logger.info(
            "下载账号汇总完成：模板=%s；计划=%s；实际开始=%s；完整=%s；可靠=%s；任务日志=%s",
            run.task_template,
            result.planned_count,
            result.started_count,
            result.complete,
            result.reliable,
            run.task_log,
        )
        return result

    def release_download_lifecycle(self) -> None:
        self._download_lifecycle_active = False

    def activate_and_start(
        self,
        task_path: Path,
        pause_after_exit: bool = False,
        *,
        _queue_continuation: bool = False,
    ) -> EngineRun:
        if _queue_continuation and getattr(self, "_download_lifecycle_active", False):
            result = self.tasks.activate_existing_task(task_path)
            self.logger.info("队列任务已激活：%s", result)
            return self.engine.start(
                pause_after_exit=pause_after_exit,
                task_template=task_path,
            )
        self.activate_task(task_path)
        return self.start_current_download(
            pause_after_exit,
            task_template=task_path,
        )

    def backup_now(
        self,
        category: str = "Manual",
        *,
        context: OperationContext | None = None,
    ) -> Path:
        self.require_safe_write()
        with critical_section(self.paths.lock_file):
            kwargs = {"category": category, "metadata": {"operation": "manual_full_volume"}}
            if context is not None:
                kwargs["context"] = context
            result = self.backup.create_full_snapshot(**kwargs)
        self.logger.info("手动完整 Volume 备份完成：%s", result)
        return result

    def preview_engine_update(
        self,
        archive: Path,
        *,
        context: OperationContext | None = None,
    ) -> EnginePackagePreview:
        self._require_operational_ready_with_context(
            "预检下载引擎更新包", context=context
        )
        return self.engine_updates.preview(archive, context=context)

    def apply_engine_update(
        self,
        archive: Path,
        *,
        context: OperationContext | None = None,
    ) -> EngineUpdateResult:
        self.require_safe_write()
        if self.engine.external_running():
            raise ControllerError("下载引擎正在运行，禁止更新。")
        if self.collector.running or self.collector.health():
            raise ControllerError("账号采集服务运行时不能更新下载引擎。")
        result = (
            self.engine_updates.apply(archive)
            if context is None
            else self.engine_updates.apply(archive, context=context)
        )
        self.logger.info(
            "下载引擎安全更新完成：ZIP=%s；备份=%s；旧引擎=%s",
            result.archive,
            result.backup_path,
            result.rollback_path,
        )
        return result

    def start_collector(self, *, context: OperationContext | None = None) -> Path:
        self.require_collector_start()
        if context is None:
            self.collector.start()
        else:
            self.collector.start(context=context)
        self._last_collector_running = True
        log_path = self.collector.last_log_path or self.paths.logs
        self.logger.info(
            "账号采集服务已启动并通过健康检查：http://127.0.0.1:%s/health；日志=%s",
            self.config.collector_port,
            log_path,
        )
        return log_path

    def stop_collector(self) -> None:
        self.require_managed_runtime_control(
            "停止账号采集服务",
            managed=getattr(self.collector, "process", None) is not None,
        )
        self.collector.stop()
        self._last_collector_running = False
        self.logger.info("账号采集服务已停止")

    def migrate_collector(self) -> MigrationResult:
        self.require_safe_write()
        if self.collector.running or self.collector.health():
            raise ControllerError("账号采集服务正在运行，请先停止服务再迁移旧数据。")
        with critical_section(self.paths.lock_file):
            result = self.collector.migrate_old_data(Path(self.config.old_screenshot_dir))
        self.logger.info("旧采集器数据迁移：%s", result)
        return result

    def screenshot_preview(
        self, *, context: OperationContext | None = None
    ) -> ScreenshotPreview:
        self._require_operational_ready_with_context(
            "预览截图归档", context=context
        )
        return self.screenshots.preview(
            self.paths.screenshot_inbox, self.paths.video_root, context=context
        )

    def organize_screenshots(
        self, *, context: OperationContext | None = None
    ) -> ScreenshotResult:
        self.require_operational_ready("整理截图")
        return self._organize_screenshots(context=context)

    def _organize_screenshots(
        self, *, context: OperationContext | None = None
    ) -> ScreenshotResult:
        result = self.screenshots.execute(
            self.paths.screenshot_inbox, self.paths.video_root, context=context
        )
        self.logger.info("截图归档完成：移动%s张", result.moved)
        return result

    def refresh_index(self, *, context: OperationContext | None = None) -> IndexResult:
        self.require_operational_ready("刷新索引")
        return self._refresh_index(context=context)

    def _refresh_index(
        self, *, context: OperationContext | None = None
    ) -> IndexResult:
        arguments = (
            self.paths.video_root,
            self.paths.index_root,
            self.paths.index_refresh_logs,
        )
        result = (
            self.indexer.refresh(*arguments)
            if context is None
            else self.indexer.refresh(*arguments, context=context)
        )
        for line in result.display_lines("索引刷新"):
            self.logger.info(line)
        return result

    def cleanup_index(self, *, context: OperationContext | None = None) -> IndexResult:
        self.require_operational_ready("清理失效索引")
        return self._cleanup_index(context=context)

    def _cleanup_index(
        self, *, context: OperationContext | None = None
    ) -> IndexResult:
        arguments = (
            self.paths.video_root,
            self.paths.index_root,
            self.paths.index_cleanup_logs,
        )
        result = (
            self.indexer.cleanup(*arguments)
            if context is None
            else self.indexer.cleanup(*arguments, context=context)
        )
        for line in result.display_lines("失效快捷方式清理"):
            self.logger.info(line)
        return result

    def cleanup_index_self_test(
        self, *, context: OperationContext | None = None
    ) -> IndexResult:
        self.require_operational_ready("执行索引清理自检")
        result = (
            self.indexer.cleanup_self_test()
            if context is None
            else self.indexer.cleanup_self_test(context=context)
        )
        self.logger.info("失效快捷方式清理隔离自检通过")
        return result

    def run_post_actions(
        self,
        timing: str,
        *,
        context: OperationContext | None = None,
    ) -> list[str]:
        if context is not None:
            context.raise_if_cancelled()
        if timing not in {"batch", "queue"}:
            raise ControllerError(f"下载后续动作时机无效：{timing}")
        try:
            self.require_operational_ready("执行下载后续动作")
            if not getattr(self, "_download_lifecycle_active", False):
                raise ControllerError("当前没有可执行后续动作的下载生命周期。")
        except ControllerError:
            if context is not None:
                context.raise_if_cancelled()
            raise
        if context is not None:
            context.raise_if_cancelled()

        run_screenshots = self.config.screenshot_post_mode == timing
        run_index = self.config.index_post_mode == timing
        run_cleanup = run_index and bool(self.config.cleanup_after_index)
        messages: list[str] = []
        if run_screenshots:
            result = self._organize_screenshots(context=context)
            messages.append(f"截图归档：{result.moved}张")
        if context is not None:
            context.raise_if_cancelled()
        if run_index:
            index_result = self._refresh_index(context=context)
            messages.append(index_result.display_summary("索引刷新"))
        if context is not None:
            context.raise_if_cancelled()
        if run_cleanup:
            cleanup_result = self._cleanup_index(context=context)
            messages.append(cleanup_result.display_summary("失效快捷方式清理"))
        return messages
