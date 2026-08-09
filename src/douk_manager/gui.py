from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, TypeVar

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from douk_manager.controller import ManagerController
from douk_manager.core.engine import assess_process_exit
from douk_manager.core.settings_tasks import EarliestRule


T = TypeVar("T")


POST_MODES = (
    ("不执行", "none"),
    ("每一批完成后", "batch"),
    ("整个队列完成后", "queue"),
)


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.controller = ManagerController()
        self.queue_pending: list[Path] = []
        self.queue_active = False
        self.queue_current = None
        self.setWindowTitle("DouK全流程一体化管理器")
        self.resize(1260, 820)
        self.setMinimumSize(1080, 700)
        self._build_ui()
        self._apply_style()
        startup_message = self.controller.try_startup_backup()
        self.overview_output.setPlainText(startup_message)
        self.refresh_all()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_processes)
        self.poll_timer.start(500)

    def _build_ui(self) -> None:
        tabs = QTabWidget()
        self.setCentralWidget(tabs)
        tabs.addTab(self._overview_tab(), "总览")
        tabs.addTab(self._task_tab(), "账号任务")
        tabs.addTab(self._batch_tab(), "批次生成")
        tabs.addTab(self._queue_tab(), "下载队列")
        tabs.addTab(self._collector_tab(), "账号采集")
        tabs.addTab(self._post_tab(), "截图与索引")
        tabs.addTab(self._settings_tab(), "路径与安全设置")
        self.statusBar().showMessage("管理器已启动")

    def _overview_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("DouK 全流程一体化管理器")
        title.setObjectName("title")
        subtitle = QLabel(
            "统一管理账号采集、任务配置、5-1-1 下载、截图归档、快捷方式索引和永久备份。"
        )
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        status_box = QGroupBox("正式数据与服务状态")
        grid = QGridLayout(status_box)
        self.status_labels: dict[str, QLabel] = {}
        rows = (
            ("engine_exe", "下载引擎"),
            ("volume", "唯一正式 Volume"),
            ("master_settings", "settings_master.json"),
            ("active_settings", "settings.json"),
            ("database", "DouK-Downloader.db"),
            ("video_root", "视频账号目录"),
            ("index_root", "索引目录"),
            ("collector_running", "采集服务"),
            ("engine_running", "下载进程"),
        )
        for row, (key, text) in enumerate(rows):
            grid.addWidget(QLabel(text), row, 0)
            label = QLabel("检查中")
            self.status_labels[key] = label
            grid.addWidget(label, row, 1)
        self.account_summary = QLabel("")
        grid.addWidget(QLabel("主档账号"), len(rows), 0)
        grid.addWidget(self.account_summary, len(rows), 1)
        layout.addWidget(status_box)

        buttons = QHBoxLayout()
        for text, callback in (
            ("刷新状态", self._refresh_status),
            ("立即创建永久备份", self._manual_backup),
            ("打开备份目录", lambda: self._open_path(self.controller.paths.backups)),
            ("打开任务目录", lambda: self._open_path(self.controller.paths.tasks)),
            ("打开日志目录", lambda: self._open_path(self.controller.paths.logs)),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.overview_output = QTextEdit()
        self.overview_output.setReadOnly(True)
        layout.addWidget(self.overview_output, 1)
        return page

    def _task_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        help_label = QLabel(
            "支持 A1,A10-A99,A879 或 1,10-99,879。A编号始终对应主档数组位置，空URL不会导致重新编号。"
        )
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        form_box = QGroupBox("创建单个或混合账号任务")
        form = QFormLayout(form_box)
        self.task_expression = QLineEdit("A1")
        self.task_expression.setPlaceholderText("例如：A1,A10-A99,A879")
        self.task_name = QLineEdit()
        self.task_name.setPlaceholderText("可留空，将按账号范围自动命名")
        self.task_earliest_mode = self._earliest_combo()
        self.task_earliest_value = QLineEdit()
        self.task_earliest_value.setPlaceholderText("例如 7、2026-01-01；清空模式无需填写")
        self.task_persist_master = QCheckBox(
            "将所选账号 earliest 同步写回 settings_master.json（主档 enable 永远不改）"
        )
        form.addRow("账号表达式", self.task_expression)
        form.addRow("任务名称", self.task_name)
        form.addRow("earliest 处理", self.task_earliest_mode)
        form.addRow("earliest 值", self.task_earliest_value)
        form.addRow("主档持久化", self.task_persist_master)
        layout.addWidget(form_box)
        buttons = QHBoxLayout()
        for text, callback in (
            ("预览", self._preview_task),
            ("只创建任务模板", self._create_task_template),
            ("创建并设为正式 settings.json", self._create_and_activate_task),
            ("创建、激活并开始下载", self._create_activate_start),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.task_output = QTextEdit()
        self.task_output.setReadOnly(True)
        layout.addWidget(self.task_output, 1)
        return page

    def _batch_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel(
            "按固定数量自动创建完整任务JSON。例如 A1-A1392 每批250，会生成 A1-A250、A251-A500 等独立任务。"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        box = QGroupBox("批次范围")
        form = QFormLayout(box)
        self.batch_start = self._spin(1, 100000, 1)
        self.batch_end = self._spin(1, 100000, 1392)
        self.batch_size = self._spin(1, 100000, 250)
        self.batch_earliest_mode = self._earliest_combo()
        self.batch_earliest_value = QLineEdit()
        self.batch_earliest_value.setPlaceholderText("可为所有生成任务设置同一个 earliest")
        form.addRow("开始编号 A", self.batch_start)
        form.addRow("结束编号 A", self.batch_end)
        form.addRow("每批账号数", self.batch_size)
        form.addRow("earliest 处理", self.batch_earliest_mode)
        form.addRow("earliest 值", self.batch_earliest_value)
        layout.addWidget(box)
        button_row = QHBoxLayout()
        generate = QPushButton("生成全部批次任务")
        generate.clicked.connect(self._generate_batches)
        button_row.addWidget(generate)
        open_tasks = QPushButton("打开任务目录")
        open_tasks.clicked.connect(lambda: self._open_path(self.controller.paths.tasks))
        button_row.addWidget(open_tasks)
        button_row.addStretch()
        layout.addLayout(button_row)
        self.batch_output = QTextEdit()
        self.batch_output.setReadOnly(True)
        layout.addWidget(self.batch_output, 1)
        return page

    def _queue_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        label = QLabel(
            "选择一个或多个任务顺序运行。任何时候只启动一个 main.exe，共用唯一 "
            "DouK-Downloader.db。自动模式使用 run_command = 5 1 1 Q，下载器会直接进入"
            "抖音批量账号下载，不显示蓝色菜单，这是原程序的正常自动执行方式。"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self.task_list = QListWidget()
        self.task_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        layout.addWidget(self.task_list, 1)
        options = QGroupBox("本次队列后续动作")
        form = QFormLayout(options)
        self.queue_screenshot_mode = self._post_combo(self.controller.config.screenshot_post_mode)
        self.queue_index_mode = self._post_combo(self.controller.config.index_post_mode)
        self.queue_cleanup = QCheckBox("索引刷新后清理失效快捷方式")
        self.queue_cleanup.setChecked(self.controller.config.cleanup_after_index)
        form.addRow("截图归档", self.queue_screenshot_mode)
        form.addRow("索引刷新", self.queue_index_mode)
        form.addRow("索引清理", self.queue_cleanup)
        layout.addWidget(options)
        buttons = QHBoxLayout()
        refresh = QPushButton("刷新任务列表")
        refresh.clicked.connect(self.refresh_tasks)
        activate = QPushButton("仅激活所选任务")
        activate.clicked.connect(self._activate_selected_task)
        run_current = QPushButton("启动当前正式 settings.json")
        run_current.clicked.connect(self._start_current)
        run_queue = QPushButton("按顺序运行所选任务")
        run_queue.clicked.connect(self._start_queue)
        for button in (refresh, activate, run_current, run_queue):
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.queue_output = QTextEdit()
        self.queue_output.setReadOnly(True)
        layout.addWidget(self.queue_output, 1)
        return page

    def _collector_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        text = QLabel(
            "管理器内置原采集器服务并保持油猴接口、端口8765和固定令牌兼容。采集器直接写唯一正式主档，不复制第二份主档。"
        )
        text.setWordWrap(True)
        layout.addWidget(text)
        self.collector_path_label = QLabel()
        self.collector_path_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.collector_path_label)
        buttons = QHBoxLayout()
        for text, callback in (
            ("迁移旧Excel/分类/截图", self._migrate_collector),
            ("启动采集服务", self._start_collector),
            ("停止采集服务", self._stop_collector),
            ("导出油猴脚本", self._export_userscript),
            ("打开采集数据目录", lambda: self._open_path(self.controller.paths.collector_data)),
            ("打开截图收件箱", lambda: self._open_path(self.controller.paths.screenshot_inbox)),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.collector_output = QTextEdit()
        self.collector_output.setReadOnly(True)
        layout.addWidget(self.collector_output, 1)
        return page

    def _post_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        paths_box = QGroupBox("当前路径")
        form = QFormLayout(paths_box)
        self.screenshot_path_label = QLabel()
        self.video_path_label = QLabel()
        self.index_path_label = QLabel()
        for label in (self.screenshot_path_label, self.video_path_label, self.index_path_label):
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("截图收件箱", self.screenshot_path_label)
        form.addRow("视频账号目录", self.video_path_label)
        form.addRow("快捷方式索引", self.index_path_label)
        layout.addWidget(paths_box)
        buttons = QHBoxLayout()
        for text, callback in (
            ("预览截图归档", self._preview_screenshots),
            ("立即安全归档截图", self._organize_screenshots),
            ("立即刷新索引", self._refresh_index),
            ("立即清理失效快捷方式", self._cleanup_index),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        buttons.addStretch()
        layout.addLayout(buttons)
        self.post_output = QTextEdit()
        self.post_output.setReadOnly(True)
        layout.addWidget(self.post_output, 1)
        return page

    def _settings_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        warning = QLabel(
            "正式 Volume 始终由 main.exe 所在目录推导，不能单独选择另一份数据库或主档。保存路径后会立即尝试创建启动前永久备份。"
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        box = QGroupBox("正式路径")
        grid = QGridLayout(box)
        self.engine_edit = QLineEdit(self.controller.config.engine_exe)
        self.video_edit = QLineEdit(self.controller.config.video_root)
        self.index_edit = QLineEdit(self.controller.config.index_root)
        self.old_screenshot_edit = QLineEdit(self.controller.config.old_screenshot_dir)
        entries = (
            ("下载引擎 main.exe", self.engine_edit, self._browse_engine),
            ("视频账号目录", self.video_edit, lambda: self._browse_dir(self.video_edit)),
            ("索引目录", self.index_edit, lambda: self._browse_dir(self.index_edit)),
            ("旧截图目录（仅迁移）", self.old_screenshot_edit, lambda: self._browse_dir(self.old_screenshot_edit)),
        )
        for row, (label, edit, callback) in enumerate(entries):
            grid.addWidget(QLabel(label), row, 0)
            grid.addWidget(edit, row, 1)
            button = QPushButton("选择")
            button.clicked.connect(callback)
            grid.addWidget(button, row, 2)
        layout.addWidget(box)

        task_box = QGroupBox("下载与任务后续动作默认值")
        form = QFormLayout(task_box)
        self.setting_batch_accounts = self._spin(1, 100000, self.controller.config.batch_accounts)
        self.setting_rest_seconds = self._spin(0, 86400, self.controller.config.rest_seconds)
        self.setting_screenshot_mode = self._post_combo(self.controller.config.screenshot_post_mode)
        self.setting_index_mode = self._post_combo(self.controller.config.index_post_mode)
        self.setting_cleanup = QCheckBox("刷新索引后清理失效快捷方式")
        self.setting_cleanup.setChecked(self.controller.config.cleanup_after_index)
        form.addRow("每批账号数", self.setting_batch_accounts)
        form.addRow("暂停秒数", self.setting_rest_seconds)
        form.addRow("默认截图归档", self.setting_screenshot_mode)
        form.addRow("默认索引刷新", self.setting_index_mode)
        form.addRow("默认索引清理", self.setting_cleanup)
        layout.addWidget(task_box)
        note = QLabel(
            "兼容版下载引擎会读取这里的批次和暂停数值；旧版仍按自身内置值运行。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        save = QPushButton("保存设置并重新验证正式数据")
        save.clicked.connect(self._save_settings)
        layout.addWidget(save)

        update_box = QGroupBox("下载引擎安全更新（永久保留唯一正式 Volume）")
        update_grid = QGridLayout(update_box)
        self.engine_update_zip = QLineEdit()
        self.engine_update_zip.setPlaceholderText("选择 GitHub Actions 生成的 Windows X64 ZIP")
        update_grid.addWidget(QLabel("新下载引擎 ZIP"), 0, 0)
        update_grid.addWidget(self.engine_update_zip, 0, 1)
        browse_update = QPushButton("选择")
        browse_update.clicked.connect(self._browse_engine_update)
        update_grid.addWidget(browse_update, 0, 2)
        preview_update = QPushButton("只读预检更新包")
        preview_update.clicked.connect(self._preview_engine_update)
        apply_update = QPushButton("备份并安全安装")
        apply_update.clicked.connect(self._apply_engine_update)
        update_grid.addWidget(preview_update, 1, 1)
        update_grid.addWidget(apply_update, 1, 2)
        layout.addWidget(update_box)
        self.settings_output = QTextEdit()
        self.settings_output.setReadOnly(True)
        layout.addWidget(self.settings_output, 1)
        return page

    @staticmethod
    def _spin(minimum: int, maximum: int, value: int) -> QSpinBox:
        spin = QSpinBox()
        spin.setRange(minimum, maximum)
        spin.setValue(value)
        return spin

    @staticmethod
    def _earliest_combo() -> QComboBox:
        combo = QComboBox()
        combo.addItem("保持原值", "keep")
        combo.addItem("清空", "empty")
        combo.addItem("设置数字/日期/文本", "custom")
        return combo

    @staticmethod
    def _post_combo(current: str) -> QComboBox:
        combo = QComboBox()
        for label, value in POST_MODES:
            combo.addItem(label, value)
            if value == current:
                combo.setCurrentIndex(combo.count() - 1)
        return combo

    @staticmethod
    def _earliest_rule(combo: QComboBox, value: QLineEdit) -> EarliestRule:
        mode = combo.currentData()
        if mode == "keep":
            return EarliestRule.keep()
        if mode == "empty":
            return EarliestRule.empty()
        return EarliestRule.from_text(value.text())

    def _run(self, action: Callable[[], T], output: QTextEdit | None = None) -> T | None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = action()
            return result
        except Exception as exc:
            message = str(exc)
            self.controller.logger.exception("界面操作失败：%s", message)
            if output is not None:
                output.append("【失败】" + message)
            self.statusBar().showMessage("操作失败")
            QMessageBox.critical(self, "操作失败", message)
            return None
        finally:
            QApplication.restoreOverrideCursor()
            self.refresh_all()

    def _refresh_status(self, _checked: bool = False) -> None:
        self.refresh_all(check_processes=True)

    def refresh_all(self, *, check_processes: bool = False) -> None:
        health = self.controller.health(check_processes=check_processes)
        for key, label in self.status_labels.items():
            value = bool(health.get(key, False))
            if key.endswith("_running"):
                label.setText("运行中" if value else "未运行")
            else:
                label.setText("正常" if value else "缺失/未配置")
            label.setProperty("ok", value)
            label.style().unpolish(label)
            label.style().polish(label)
        self.account_summary.setText(
            f"位置 {health.get('master_positions', 0)}；有效URL {health.get('master_valid_urls', 0)}"
        )
        self.collector_path_label.setText(
            f"正式主档：{self.controller.paths.master_settings}\n"
            f"Excel：{self.controller.paths.collector_excel}\n"
            f"截图收件箱：{self.controller.paths.screenshot_inbox}"
        )
        self.screenshot_path_label.setText(str(self.controller.paths.screenshot_inbox))
        self.video_path_label.setText(str(self.controller.paths.video_root))
        self.index_path_label.setText(str(self.controller.paths.index_root))
        if health.get("master_positions"):
            self.batch_end.setMaximum(int(health["master_positions"]))
            if self.batch_end.value() > int(health["master_positions"]):
                self.batch_end.setValue(int(health["master_positions"]))
        self.refresh_tasks()

    def refresh_tasks(self) -> None:
        selected_names = {item.text() for item in self.task_list.selectedItems()}
        self.task_list.clear()
        for path in self.controller.list_tasks():
            item = QListWidgetItem(path.name)
            item.setData(Qt.UserRole, str(path))
            self.task_list.addItem(item)
            if path.name in selected_names:
                item.setSelected(True)

    def _preview_task(self) -> None:
        preview = self._run(
            lambda: self.controller.preview_selection(self.task_expression.text()),
            self.task_output,
        )
        if preview:
            self.task_output.setPlainText(
                "\n".join(
                    (
                        f"标准化选择：{preview.compact}",
                        f"主档位置总数：{preview.total_positions}",
                        f"选中位置：{preview.selected_positions}",
                        f"有效URL：{preview.selected_valid_urls}",
                        f"空白URL位置：{preview.selected_blank_urls}",
                        f"其他位置将设为 false：{preview.unselected_positions}",
                        f"重复编号：{len(preview.selection.duplicate_numbers)}",
                        "主档 enable：不会修改",
                    )
                )
            )

    def _create_task(self, activate: bool, start: bool) -> None:
        rule = self._earliest_rule(self.task_earliest_mode, self.task_earliest_value)
        result = self._run(
            lambda: self.controller.create_task(
                self.task_expression.text(),
                rule,
                self.task_persist_master.isChecked(),
                self.task_name.text(),
                activate,
            ),
            self.task_output,
        )
        if result:
            self.task_output.append(f"任务文件：{result.task_path}")
            self.task_output.append(f"账号范围：{result.preview.compact}")
            if result.backup_path:
                self.task_output.append(f"永久备份：{result.backup_path}")
            if activate:
                self.task_output.append(f"已激活：{self.controller.paths.active_settings}")
            if start:
                run = self._run(self.controller.start_current_download, self.task_output)
                if run:
                    self.task_output.append(f"下载引擎已启动，PID={run.process.pid}")

    def _create_task_template(self) -> None:
        self._create_task(False, False)

    def _create_and_activate_task(self) -> None:
        self._create_task(True, False)

    def _create_activate_start(self) -> None:
        self._create_task(True, True)

    def _generate_batches(self) -> None:
        rule = self._earliest_rule(self.batch_earliest_mode, self.batch_earliest_value)
        result = self._run(
            lambda: self.controller.generate_batches(
                self.batch_start.value(),
                self.batch_end.value(),
                self.batch_size.value(),
                rule,
            ),
            self.batch_output,
        )
        if result is not None:
            lines = [f"已生成 {len(result)} 个任务："]
            lines.extend(f"{item.task_path.name}：{item.preview.selected_positions}个位置" for item in result)
            self.batch_output.setPlainText("\n".join(lines))
            self.refresh_tasks()

    def _selected_task_paths(self) -> list[Path]:
        return [Path(item.data(Qt.UserRole)) for item in self.task_list.selectedItems()]

    def _activate_selected_task(self) -> None:
        paths = self._selected_task_paths()
        if len(paths) != 1:
            QMessageBox.information(self, "请选择一个任务", "仅激活时必须且只能选择一个任务。")
            return
        result = self._run(lambda: self.controller.activate_task(paths[0]), self.queue_output)
        if result:
            self.queue_output.append(f"已激活：{paths[0].name}")

    def _apply_queue_options(self) -> bool:
        values = {
            "screenshot_post_mode": self.queue_screenshot_mode.currentData(),
            "index_post_mode": self.queue_index_mode.currentData(),
            "cleanup_after_index": self.queue_cleanup.isChecked(),
        }
        result = self._run(
            lambda: self.controller.update_post_options(values), self.queue_output
        )
        return bool(result)

    def _start_current(self) -> None:
        if self.queue_active:
            QMessageBox.warning(self, "队列运行中", "已有下载队列正在运行。")
            return
        if not self._apply_queue_options():
            return
        run = self._run(self.controller.start_current_download, self.queue_output)
        if run:
            self.queue_active = True
            self.queue_current = run
            self.queue_pending = []
            self.queue_output.append(f"启动当前 settings.json，PID={run.process.pid}")

    def _start_queue(self) -> None:
        if self.queue_active:
            QMessageBox.warning(self, "队列运行中", "已有下载队列正在运行。")
            return
        paths = self._selected_task_paths()
        if not paths:
            QMessageBox.information(self, "未选择任务", "请在任务列表中选择一个或多个任务。")
            return
        if not self._apply_queue_options():
            return
        self.queue_pending = paths
        self.queue_active = True
        self.queue_output.append(f"队列开始，共 {len(paths)} 个任务。")
        self._start_next_queue_item()

    def _start_next_queue_item(self) -> None:
        if not self.queue_pending:
            messages = self._run(
                lambda: self.controller.run_post_actions("queue"), self.queue_output
            )
            if messages:
                self.queue_output.append("；".join(messages))
            self.queue_output.append(
                "队列执行结束（仅表示所选下载器进程均已正常退出，"
                "不代表每个账号均下载成功）。"
            )
            self.queue_active = False
            self.queue_current = None
            return
        path = self.queue_pending.pop(0)
        run = self._run(lambda: self.controller.activate_and_start(path), self.queue_output)
        if run is None:
            self.queue_active = False
            self.queue_pending.clear()
            return
        self.queue_current = run
        self.queue_output.append(
            f"正在运行：{path.name}；PID={run.process.pid}；剩余={len(self.queue_pending)}"
        )

    def _poll_processes(self) -> None:
        if not self.queue_active or self.queue_current is None:
            return
        if self.queue_current.running:
            return
        code = self.queue_current.process.returncode
        assessment = assess_process_exit(code)
        self.queue_output.append(assessment.headline)
        self.queue_output.append(assessment.detail)
        self.controller.logger.info(
            "下载进程已退出：PID=%s；退出码=%s；状态=%s",
            self.queue_current.process.pid,
            code,
            assessment.log_status,
        )
        try:
            with self.queue_current.task_log.open("a", encoding="utf-8") as handle:
                handle.write(f"Exited: code={code}\n")
                handle.write(f"Process status: {assessment.log_status}\n")
                if assessment.normal_exit:
                    handle.write("Download result: unverified\n")
        except OSError:
            self.controller.logger.exception("写入下载任务退出日志失败")
        if not assessment.normal_exit:
            self.queue_pending.clear()
            self.queue_active = False
            self.queue_current = None
            return
        messages = self._run(lambda: self.controller.run_post_actions("batch"), self.queue_output)
        if messages:
            self.queue_output.append("；".join(messages))
        self.queue_current = None
        self._start_next_queue_item()

    def _manual_backup(self) -> None:
        result = self._run(self.controller.backup_now, self.overview_output)
        if result:
            self.overview_output.append(f"永久备份完成：{result}")

    def _migrate_collector(self) -> None:
        result = self._run(self.controller.migrate_collector, self.collector_output)
        if result:
            self.collector_output.setPlainText(
                "\n".join(
                    (
                        "旧采集器数据复制完成（源文件未删除）。",
                        "已复制：" + ("、".join(result.copied_files) or "无"),
                        "已跳过：" + ("、".join(result.skipped_files) or "无"),
                        f"复制截图：{result.copied_screenshots}",
                        f"跳过同名截图：{result.skipped_screenshots}",
                        f"Excel 对齐：A{result.excel_original_max} → A{result.excel_final_max}",
                        f"Excel 新增行：{result.excel_rows_added}",
                        (
                            "历史链接写法差异："
                            f"{result.excel_existing_url_differences} 行（已原样保留）"
                        ),
                        "settings_master.json 未复制，采集器将直接使用唯一正式主档。",
                    )
                )
            )

    def _start_collector(self) -> None:
        result = self._run(self.controller.start_collector, self.collector_output)
        if result:
            self.collector_output.append("账号采集服务已启动。")

    def _stop_collector(self) -> None:
        self.controller.stop_collector()
        self.collector_output.append("账号采集服务已停止。")
        self.refresh_all()

    def _export_userscript(self) -> None:
        result = self._run(self.controller.collector.export_userscript, self.collector_output)
        if result:
            self.collector_output.append(f"油猴脚本已导出：{result}")
            self._open_path(result.parent)

    def _preview_screenshots(self) -> None:
        result = self._run(self.controller.screenshot_preview, self.post_output)
        if result:
            self.post_output.setPlainText(
                "\n".join(
                    (
                        f"识别账号文件夹：{result.recognized_folders}",
                        f"识别截图：{result.recognized_images}",
                        f"可以安全归档：{result.movable}",
                        f"找不到账号文件夹：{result.missing_account_folder}",
                        f"目标已有同名文件：{result.already_existing}",
                        f"忽略非规范账号文件夹：{result.unmatched_folders}",
                    )
                )
            )

    def _organize_screenshots(self) -> None:
        result = self._run(self.controller.organize_screenshots, self.post_output)
        if result:
            self.post_output.append(f"完成：安全归档 {result.moved} 张。")

    def _refresh_index(self) -> None:
        result = self._run(self.controller.refresh_index, self.post_output)
        if result:
            self.post_output.append("索引刷新完成。\n" + result.output)

    def _cleanup_index(self) -> None:
        result = self._run(self.controller.cleanup_index, self.post_output)
        if result:
            self.post_output.append("失效快捷方式清理完成。\n" + result.output)

    def _save_settings(self) -> None:
        values = {
            "engine_exe": self.engine_edit.text().strip(),
            "video_root": self.video_edit.text().strip(),
            "index_root": self.index_edit.text().strip(),
            "old_screenshot_dir": self.old_screenshot_edit.text().strip(),
            "batch_accounts": self.setting_batch_accounts.value(),
            "rest_seconds": self.setting_rest_seconds.value(),
            "screenshot_post_mode": self.setting_screenshot_mode.currentData(),
            "index_post_mode": self.setting_index_mode.currentData(),
            "cleanup_after_index": self.setting_cleanup.isChecked(),
        }
        result = self._run(lambda: self.controller.reconfigure(values), self.settings_output)
        if result:
            self.settings_output.setPlainText(result)
            self.queue_screenshot_mode.setCurrentIndex(self.setting_screenshot_mode.currentIndex())
            self.queue_index_mode.setCurrentIndex(self.setting_index_mode.currentIndex())
            self.queue_cleanup.setChecked(self.setting_cleanup.isChecked())

    def _browse_engine(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self, "选择 DouK 下载引擎", self.engine_edit.text(), "Windows 程序 (*.exe)"
        )
        if selected:
            self.engine_edit.setText(selected)

    def _browse_engine_update(self) -> None:
        selected, _ = QFileDialog.getOpenFileName(
            self,
            "选择下载引擎 ZIP",
            self.engine_update_zip.text(),
            "ZIP 压缩包 (*.zip)",
        )
        if selected:
            self.engine_update_zip.setText(selected)

    def _preview_engine_update(self) -> None:
        archive = Path(self.engine_update_zip.text().strip())
        result = self._run(
            lambda: self.controller.preview_engine_update(archive), self.settings_output
        )
        if result:
            packaged_volume = "有（安装时会丢弃，绝不覆盖正式 Volume）" if result.contains_packaged_volume else "无"
            self.settings_output.setPlainText(
                "\n".join(
                    (
                        "更新包只读预检通过。",
                        f"ZIP：{result.archive}",
                        f"SHA-256：{result.archive_sha256}",
                        f"包内根目录：{result.package_prefix}",
                        f"文件数：{result.file_count}",
                        f"解压大小：{result.uncompressed_bytes / 1024 / 1024:.1f} MB",
                        f"main.exe：{result.main_exe_bytes / 1024 / 1024:.1f} MB",
                        f"包内 Volume：{packaged_volume}",
                        "当前正式 Volume 尚未修改。",
                    )
                )
            )

    def _apply_engine_update(self) -> None:
        archive = Path(self.engine_update_zip.text().strip())
        preview = self._run(
            lambda: self.controller.preview_engine_update(archive), self.settings_output
        )
        if not preview:
            return
        answer = QMessageBox.question(
            self,
            "确认安全更新下载引擎",
            "管理器将先永久备份完整正式 Volume，再替换 main.exe 和 _internal 程序文件。\n\n"
            "settings_master.json、settings.json 和 DouK-Downloader.db 将通过移动保留，"
            "更新前后进行 SHA-256 与数据库一致性校验。\n\n"
            "旧引擎程序会永久保存在 Updates\\EngineRollback。\n\n确认继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        result = self._run(
            lambda: self.controller.apply_engine_update(archive), self.settings_output
        )
        if result:
            self.settings_output.setPlainText(
                "\n".join(
                    (
                        "下载引擎安全更新完成。",
                        f"更新前永久备份：{result.backup_path}",
                        f"旧引擎回退目录：{result.rollback_path}",
                        f"旧 main.exe：{result.old_main_sha256}",
                        f"新 main.exe：{result.new_main_sha256}",
                        "settings_master.json：哈希一致",
                        "settings.json：哈希一致",
                        "DouK-Downloader.db：哈希一致且 quick_check=ok",
                        "现在可以通过下载队列启动兼容版下载引擎。",
                    )
                )
            )

    def _browse_dir(self, edit: QLineEdit) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择文件夹", edit.text())
        if selected:
            edit.setText(selected)

    @staticmethod
    def _open_path(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def closeEvent(self, event) -> None:  # noqa: N802
        try:
            self.controller.stop_collector()
        finally:
            event.accept()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow { background: #f5f7fb; }
            QWidget { font-family: "Microsoft YaHei UI"; font-size: 13px; }
            QLabel#title { font-size: 25px; font-weight: 700; color: #15325b; }
            QGroupBox { font-weight: 600; border: 1px solid #cbd5e1; border-radius: 7px;
                        margin-top: 10px; padding-top: 9px; background: white; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QPushButton { background: #2563eb; color: white; border: 0; border-radius: 5px;
                          padding: 7px 12px; min-height: 22px; }
            QPushButton:hover { background: #1d4ed8; }
            QLineEdit, QSpinBox, QComboBox, QListWidget, QTextEdit {
                background: white; border: 1px solid #cbd5e1; border-radius: 5px; padding: 5px;
            }
            QTabWidget::pane { border: 1px solid #cbd5e1; background: #f8fafc; }
            QTabBar::tab { padding: 9px 15px; background: #e2e8f0; }
            QTabBar::tab:selected { background: white; color: #1d4ed8; font-weight: 600; }
            QLabel[ok="true"] { color: #15803d; font-weight: 600; }
            QLabel[ok="false"] { color: #b91c1c; font-weight: 600; }
            """
        )
