from __future__ import annotations

import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any

from douk_manager.config import AppConfig, ManagedPaths, application_root, update_config
from douk_manager.core.backup import BackupService
from douk_manager.core.engine import EngineRun, EngineService
from douk_manager.core.json_store import read_json
from douk_manager.core.locks import critical_section
from douk_manager.core.settings_tasks import EarliestRule, GeneratedTask, SettingsTaskService
from douk_manager.integrations.collector import CollectorService, MigrationResult
from douk_manager.integrations.indexer import IndexResult, IndexService
from douk_manager.integrations.screenshots import ScreenshotPreview, ScreenshotResult, ScreenshotService
from douk_manager.logging_setup import setup_logging


class ControllerError(RuntimeError):
    pass


class ManagerController:
    def __init__(self) -> None:
        self.root = application_root()
        default_paths = ManagedPaths.from_config(AppConfig(), self.root)
        default_paths.ensure_manager_directories()
        self.config = AppConfig.load(default_paths.config_file)
        self.paths = ManagedPaths.from_config(self.config, self.root)
        self.paths.ensure_manager_directories()
        self.logger, self.log_path = setup_logging(self.paths.logs)
        self.startup_backup: Path | None = None
        self.read_only_reason = ""
        self._build_services()

    def _build_services(self) -> None:
        self.backup = BackupService(self.paths)
        self.tasks = SettingsTaskService(self.paths, self.backup)
        self.engine = EngineService(self.paths, self.config, self.backup)
        self.collector = CollectorService(self.config, self.paths)
        self.screenshots = ScreenshotService()
        self.indexer = IndexService()

    def health(self) -> dict[str, Any]:
        result: dict[str, Any] = self.paths.health()
        result["collector_running"] = self.collector.health()
        result["engine_running"] = self.engine.external_running()
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
        return result

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
        if self.engine.external_running():
            self.read_only_reason = "检测到下载引擎正在运行，未执行启动前备份。"
            return self.read_only_reason
        try:
            with critical_section(self.paths.lock_file, timeout=5.0):
                self.startup_backup = self.backup.create_snapshot(
                    "Startup", {"operation": "application_start"}
                )
            self.read_only_reason = ""
            self.logger.info("启动前永久备份完成：%s", self.startup_backup)
            return f"启动前备份完成：{self.startup_backup}"
        except Exception as exc:
            self.read_only_reason = f"启动前备份失败：{exc}"
            self.logger.exception("启动前备份失败")
            return self.read_only_reason

    def require_safe_write(self) -> None:
        if self.engine.external_running():
            raise ControllerError("下载引擎正在运行，禁止修改配置或重新备份。")
        if self.startup_backup is None:
            message = self.try_startup_backup()
            if self.startup_backup is None:
                raise ControllerError(message)
        if self.read_only_reason:
            raise ControllerError(self.read_only_reason)

    def reconfigure(self, values: dict[str, Any]) -> str:
        if self.engine.external_running():
            raise ControllerError("下载引擎正在运行，禁止切换正式路径或重建服务。")
        if self.collector.running:
            self.collector.stop()
        self.config = update_config(self.config, values)
        new_paths = ManagedPaths.from_config(self.config, self.root)
        new_paths.ensure_manager_directories()
        self.config.save(new_paths.config_file)
        self.paths = new_paths
        self.startup_backup = None
        self.read_only_reason = ""
        self._build_services()
        self.logger.info("路径和任务设置已保存：%s", asdict(self.config))
        return self.try_startup_backup()

    def update_post_options(self, values: dict[str, Any]) -> bool:
        """保存本次队列选项，不重建路径，也不制造一次多余的启动备份。"""
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

    def create_task(
        self,
        expression: str,
        rule: EarliestRule,
        persist_master: bool,
        task_name: str,
        activate: bool,
    ) -> GeneratedTask:
        self.require_safe_write()
        result = self.tasks.create_task(
            expression,
            task_earliest=rule,
            persist_master_earliest=persist_master,
            task_name=task_name or None,
            activate=activate,
        )
        self.logger.info(
            "任务已创建：%s；选择=%s；激活=%s；主档earliest=%s",
            result.task_path,
            result.preview.compact,
            activate,
            persist_master,
        )
        return result

    def generate_batches(
        self, start: int, end: int, size: int, rule: EarliestRule
    ) -> tuple[GeneratedTask, ...]:
        self.require_safe_write()
        result = self.tasks.generate_batches(start, end, size, task_earliest=rule)
        self.logger.info("批次任务已生成：%s 个；A%s-A%s；每批%s", len(result), start, end, size)
        return result

    def list_tasks(self) -> tuple[Path, ...]:
        self.paths.tasks.mkdir(parents=True, exist_ok=True)
        return tuple(sorted(self.paths.tasks.glob("*.json")))

    def activate_task(self, path: Path) -> Path:
        self.require_safe_write()
        result = self.tasks.activate_existing_task(path)
        self.logger.info("任务已激活：%s", path)
        return result

    def start_current_download(self) -> EngineRun:
        self.require_safe_write()
        result = self.engine.start()
        self.logger.info("下载引擎已启动：PID=%s", result.process.pid)
        return result

    def activate_and_start(self, task_path: Path) -> EngineRun:
        self.activate_task(task_path)
        return self.start_current_download()

    def backup_now(self, category: str = "Manual") -> Path:
        self.require_safe_write()
        with critical_section(self.paths.lock_file):
            result = self.backup.create_snapshot(category, {"operation": "manual"})
        self.logger.info("手动永久备份完成：%s", result)
        return result

    def start_collector(self) -> bool:
        self.require_safe_write()
        self.collector.start()
        self.logger.info("账号采集服务已启动")
        return True

    def stop_collector(self) -> None:
        self.collector.stop()
        self.logger.info("账号采集服务已停止")

    def migrate_collector(self) -> MigrationResult:
        self.require_safe_write()
        if self.collector.running or self.collector.health():
            raise ControllerError("账号采集服务正在运行，请先停止服务再迁移旧数据。")
        with critical_section(self.paths.lock_file):
            result = self.collector.migrate_old_data(Path(self.config.old_screenshot_dir))
        self.logger.info("旧采集器数据迁移：%s", result)
        return result

    def screenshot_preview(self) -> ScreenshotPreview:
        return self.screenshots.preview(self.paths.screenshot_inbox, self.paths.video_root)

    def organize_screenshots(self) -> ScreenshotResult:
        result = self.screenshots.execute(self.paths.screenshot_inbox, self.paths.video_root)
        self.logger.info("截图归档完成：移动%s张", result.moved)
        return result

    def refresh_index(self) -> IndexResult:
        result = self.indexer.refresh(self.paths.video_root, self.paths.index_root)
        self.logger.info("索引刷新完成")
        return result

    def cleanup_index(self) -> IndexResult:
        result = self.indexer.cleanup(self.paths.video_root, self.paths.index_root)
        self.logger.info("失效快捷方式清理完成")
        return result

    def run_post_actions(self, timing: str) -> list[str]:
        messages: list[str] = []
        if self.config.screenshot_post_mode == timing:
            result = self.organize_screenshots()
            messages.append(f"截图归档：{result.moved}张")
        if self.config.index_post_mode == timing:
            self.refresh_index()
            messages.append("索引刷新：完成")
            if self.config.cleanup_after_index:
                self.cleanup_index()
                messages.append("失效快捷方式清理：完成")
        return messages
