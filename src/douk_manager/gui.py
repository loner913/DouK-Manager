from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Callable, TypeVar

from PySide6.QtCore import QObject, QThread, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
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
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from douk_manager.controller import ManagerController
from douk_manager.background import (
    BackgroundTaskCoordinator,
    ClosePolicy,
    TaskFailure,
    TaskSpec,
    TaskState,
)
from douk_manager.core.download_summary import AccountStatus, format_summary_for_ui
from douk_manager.core.engine import ENGINE_MODE_MONITOR, assess_process_exit
from douk_manager.core.power import request_normal_shutdown
from douk_manager.core.settings_tasks import EarliestRule
from douk_manager.core.task_order import move_to_index
from douk_manager.ui_messages import format_information
from douk_manager.startup import (
    StartupSafetyResult,
    StartupSafetyService,
    StartupStage,
    StartupState,
)


T = TypeVar("T")


POST_MODES = (
    ("不执行", "none"),
    ("每一批完成后", "batch"),
    ("整个队列完成后", "queue"),
)


def _status_label(status: AccountStatus | str) -> str:
    labels = {
        AccountStatus.DOWNLOADED: "有新作品下载",
        AccountStatus.ALL_SKIPPED: "作品均被引擎跳过",
        AccountStatus.NO_ELIGIBLE_WORKS: "无符合条件作品",
        AccountStatus.PRIVATE: "私密账号",
        AccountStatus.ERROR: "处理异常",
        AccountStatus.INTERRUPTED: "处理中断",
    }
    return labels.get(status, str(status))


class ActionWorker(QObject):
    done = Signal()

    def __init__(self, action: Callable[[], object]) -> None:
        super().__init__()
        self.action = action
        self.result: object | None = None
        self.error: Exception | None = None

    @Slot()
    def run(self) -> None:
        try:
            self.result = self.action()
        except Exception as exc:  # The main thread presents and logs the error.
            self.error = exc
        finally:
            self.done.emit()


class TaskTemplateList(QListWidget):
    """A checklist whose left-drag gesture applies one state to a range."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._drag_anchor_row = -1
        self._drag_target_state: Qt.CheckState | None = None
        self._drag_press_position: tuple[int, int] | None = None

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            position = event.position().toPoint()
            item = self.itemAt(position)
            if item is not None:
                # The whole row owns this gesture, including its checkbox.
                # A press/release toggles one row; holding and crossing rows
                # applies that same target state to the entire range.
                self.setFocus(Qt.FocusReason.MouseFocusReason)
                self.setCurrentItem(item)
                self._drag_anchor_row = self.row(item)
                self._drag_target_state = (
                    Qt.CheckState.Unchecked
                    if item.checkState() == Qt.CheckState.Checked
                    else Qt.CheckState.Checked
                )
                self._drag_press_position = (position.x(), position.y())
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_anchor_row < 0 or not (
            event.buttons() & Qt.MouseButton.LeftButton
        ):
            super().mouseMoveEvent(event)
            return
        if self._drag_press_position is not None:
            x, y = self._drag_press_position
            current = event.position().toPoint()
            if abs(current.x() - x) < 4 and abs(current.y() - y) < 4:
                event.accept()
                return
        item = self.itemAt(event.position().toPoint())
        if item is not None:
            self._apply_drag_state(self.row(item))
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._drag_anchor_row < 0:
            super().mouseReleaseEvent(event)
            return
        item = self.itemAt(event.position().toPoint())
        if item is not None:
            self._apply_drag_state(self.row(item))
        event.accept()
        self._drag_anchor_row = -1
        self._drag_target_state = None
        self._drag_press_position = None

    def _apply_drag_state(self, target_row: int) -> None:
        if self._drag_anchor_row < 0 or self._drag_target_state is None:
            return
        first, last = sorted((self._drag_anchor_row, target_row))
        signals_were_blocked = self.blockSignals(True)
        try:
            for row in range(first, last + 1):
                self.item(row).setCheckState(self._drag_target_state)
        finally:
            self.blockSignals(signals_were_blocked)
        for row in range(first, last + 1):
            self.item(row).setText(
                f"{'【已勾选】' if self.item(row).checkState() == Qt.CheckState.Checked else '【未勾选】'} "
                f"{Path(self.item(row).data(Qt.ItemDataRole.UserRole)).name}"
            )


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.controller = ManagerController()
        self.coordinator = BackgroundTaskCoordinator(self)
        self.coordinator.task_settled.connect(self._on_startup_task_settled)
        self.coordinator.idle.connect(self._on_background_tasks_idle)
        self.startup_generation = 0
        self._startup_task_id: str | None = None
        self._startup_result: StartupSafetyResult | None = None
        self._close_pending = False
        self._safe_widgets: list[QWidget] = []
        self._path_widgets: list[QWidget] = []
        self._dangerous_widgets: list[QWidget] = []
        self.queue_pending: list[Path] = []
        self.queue_active = False
        self.queue_current = None
        self.queue_paused = False
        self.queue_shutdown_requested = False
        self.shutdown_timer: QTimer | None = None
        self.shutdown_remaining = 0
        self._syncing_task_selection = False
        self.queue_summaries_complete = True
        self.queue_summaries_reliable = True
        self.queue_cancel_requested = False
        self.background_thread: QThread | None = None
        self.background_worker: ActionWorker | None = None
        self.background_output: QTextEdit | None = None
        self.background_success: Callable[[object], None] | None = None
        self.download_summary_thread: QThread | None = None
        self.download_summary_worker: ActionWorker | None = None
        self.download_summary_run = None
        self.download_summary_exit_code: int | None = None
        self.download_summary_assessment = None
        self.queue_run_source = ""
        self.queue_started_at: datetime | None = None
        self.current_task_started_at: datetime | None = None
        self.result_refresh_timer = QTimer(self)
        self.result_refresh_timer.setSingleShot(True)
        self.result_refresh_timer.timeout.connect(self._refresh_results_if_startup_applied)
        self.startup_recheck_timer = QTimer(self)
        self.startup_recheck_timer.setSingleShot(True)
        self.startup_recheck_timer.timeout.connect(self._run_startup_recheck)
        self.setWindowTitle("DouK全流程一体化管理器")
        self.resize(1260, 820)
        self.setMinimumSize(1080, 700)
        self._build_ui()
        self._apply_style()
        self._finalize_action_gates()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_processes)
        self.poll_timer.start(500)

    def _mark_safe_widget(self, widget: QWidget) -> QWidget:
        if widget not in self._safe_widgets:
            self._safe_widgets.append(widget)
        return widget

    def _mark_path_widget(self, widget: QWidget) -> QWidget:
        if widget not in self._path_widgets:
            self._path_widgets.append(widget)
        return widget

    def _finalize_action_gates(self) -> None:
        controls = (
            QPushButton,
            QCheckBox,
            QComboBox,
            QSpinBox,
            QLineEdit,
            QListWidget,
        )
        self._dangerous_widgets = [
            widget
            for widget in self.findChildren(QWidget)
            if isinstance(widget, controls)
            and widget not in self._safe_widgets
            and widget not in self._path_widgets
        ]
        self._apply_action_gate()

    def _apply_action_gate(self) -> None:
        state = self.controller.startup_state
        dangerous_enabled = state is StartupState.READY
        path_enabled = state in (StartupState.READY, StartupState.DEGRADED_READ_ONLY)
        safe_enabled = state is not StartupState.CLOSING
        for widget in self._dangerous_widgets:
            widget.setEnabled(dangerous_enabled)
        for widget in self._path_widgets:
            widget.setEnabled(path_enabled)
        for widget in self._safe_widgets:
            widget.setEnabled(safe_enabled)

    def begin_startup_check(self) -> bool:
        if self.controller.startup_state is StartupState.CLOSING:
            return False
        generation = max(
            self.startup_generation,
            getattr(self.controller, "startup_generation", 0),
        ) + 1
        if not self.controller.begin_startup_check(generation):
            return False
        self.startup_generation = generation
        self.startup_state_label.setText(StartupState.SAFETY_CHECKING.value)
        self.startup_stage_label.setText("启动安全检查")
        self.startup_summary_label.setText("检查中")
        self.startup_details.setPlainText("")
        self._apply_action_gate()
        try:
            service = StartupSafetyService(
                paths=self.controller.paths,
                engine=self.controller.engine,
                backup=self.controller.backup,
            )
            spec = TaskSpec(
                task_type="startup_safety",
                display_name="启动安全检查",
                resource_keys=frozenset({"startup_safety"}),
                deduplicate_key="startup_safety",
                cancellable=True,
                close_policy=ClosePolicy.CANCEL,
                refresh_targets=("runtime_status",),
            )
            self._startup_task_id = self.coordinator.start(
                spec,
                generation,
                lambda token: service.run(generation, token),
            )
        except Exception as exc:
            self._startup_task_id = None
            self.controller.logger.exception("启动安全检查排队失败：%s", exc)
            self._apply_startup_failure(
                generation,
                "启动安全检查无法启动，已进入只读保护。",
                f"{type(exc).__name__}: {exc}",
            )
            return False
        self.statusBar().showMessage("正在执行启动安全检查……")
        return True

    @Slot(str, int, object, object)
    def _on_startup_task_settled(
        self,
        task_id: str,
        generation: int,
        outcome: object,
        payload: object,
    ) -> None:
        if task_id != self._startup_task_id:
            return
        if outcome is TaskState.SUCCEEDED and isinstance(payload, StartupSafetyResult):
            self.apply_startup_result(payload)
            return
        if generation != self.startup_generation:
            return
        if isinstance(payload, TaskFailure):
            message = (
                payload.message[:2000]
                if payload.traceback_text
                else payload.message[:4000]
            )
            separator = "\n\n" if message and payload.traceback_text else ""
            traceback_budget = 4000 - len(message) - len(separator)
            traceback_text = (
                payload.traceback_text[-traceback_budget:]
                if traceback_budget > 0
                else ""
            )
            details = f"{message}{separator}{traceback_text}"
        else:
            details = str(payload)
        self._apply_startup_failure(
            generation,
            "启动安全检查后台任务失败，已进入只读保护。",
            details,
        )

    def _apply_startup_failure(
        self,
        generation: int,
        summary: str,
        details: str,
        *,
        stage: StartupStage = StartupStage.SNAPSHOT,
    ) -> bool:
        result = StartupSafetyResult(
            generation=generation,
            success=False,
            state=StartupState.DEGRADED_READ_ONLY,
            stage=stage,
            summary=summary,
            details=details,
            health={},
        )
        return self.apply_startup_result(result)

    def apply_startup_result(self, result: StartupSafetyResult) -> bool:
        if not self.controller.apply_startup_result(result):
            return False
        self._startup_result = result
        self.startup_state_label.setText(self.controller.startup_state.value)
        self.startup_stage_label.setText(result.stage.value)
        self.startup_summary_label.setText(result.summary)
        self.startup_details.setPlainText(result.details or "无额外技术详情。")
        self._apply_action_gate()
        self._render_health_snapshot(result.health_snapshot)
        if self.controller.startup_state is not StartupState.READY:
            self._apply_action_gate()
        self.statusBar().showMessage(
            "启动安全检查通过" if result.success else "启动安全检查失败，已进入只读保护"
        )
        QTimer.singleShot(0, self._refresh_noncritical_after_startup)
        return True

    def _refresh_noncritical_after_startup(self) -> None:
        if self.controller.startup_state is StartupState.CLOSING:
            return
        self.refresh_tasks()
        if hasattr(self, "result_table"):
            self.refresh_results()

    def _refresh_results_if_startup_applied(self) -> None:
        if self.controller.startup_state in (
            StartupState.READY,
            StartupState.DEGRADED_READ_ONLY,
        ):
            self.refresh_results()

    def _copy_startup_error(self) -> None:
        summary = self.startup_summary_label.text()
        details = self.startup_details.toPlainText()
        QApplication.clipboard().setText("\n".join(part for part in (summary, details) if part))
        self.statusBar().showMessage("启动诊断已复制")

    def _open_manager_log(self) -> None:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.controller.log_path)))

    def _run_startup_recheck(self) -> None:
        if self.isVisible():
            self.begin_startup_check()

    @Slot()
    def _on_background_tasks_idle(self) -> None:
        if not self._close_pending:
            return
        if not self.coordinator.is_closing:
            return
        if self.controller.startup_state is not StartupState.CLOSING:
            return
        self._close_pending = False
        QTimer.singleShot(0, self.close)

    def _save_paths(self) -> None:
        values = {
            "engine_exe": self.engine_edit.text().strip(),
            "video_root": self.video_edit.text().strip(),
            "index_root": self.index_edit.text().strip(),
            "old_screenshot_dir": self.old_screenshot_edit.text().strip(),
        }
        try:
            result = self.controller.reconfigure(values)
        except Exception as exc:
            self._replace_info(self.settings_output, "【失败】", str(exc))
            self.statusBar().showMessage("路径修复失败")
            return
        self._replace_info(
            self.settings_output,
            result or "路径修复配置已保存，正在重新执行启动安全检查。",
        )
        self.statusBar().showMessage("路径已保存，等待重新检查")
        self.startup_recheck_timer.start(0)

    def _build_ui(self) -> None:
        tabs = QTabWidget()
        self.tabs = tabs
        self.setCentralWidget(tabs)
        tabs.addTab(self._overview_tab(), "总览")
        tabs.addTab(self._task_tab(), "账号任务")
        tabs.addTab(self._batch_tab(), "批次生成")
        tabs.addTab(self._queue_tab(), "下载队列")
        tabs.addTab(self._collector_tab(), "账号采集")
        tabs.addTab(self._post_tab(), "截图与索引")
        tabs.addTab(self._settings_tab(), "路径与安全设置")
        self.result_page = self._result_tab()
        tabs.addTab(self.result_page, "下载结果")
        self.result_tab_index = tabs.indexOf(self.result_page)
        tabs.currentChanged.connect(self._tab_changed)
        corner = QWidget()
        corner_layout = QHBoxLayout(corner)
        corner_layout.setContentsMargins(5, 0, 5, 0)
        self.global_collector_title = QLabel("采集服务：")
        self.global_collector_status = QLabel("检查中")
        self.global_engine_title = QLabel("下载进程：")
        self.global_engine_status = QLabel("检查中")
        for label in (
            self.global_collector_title,
            self.global_engine_title,
        ):
            label.setStyleSheet("color: #111827; font-weight: 600;")
        corner_layout.addWidget(self.global_collector_title)
        corner_layout.addWidget(self.global_collector_status)
        corner_layout.addSpacing(8)
        corner_layout.addWidget(self.global_engine_title)
        corner_layout.addWidget(self.global_engine_status)
        tabs.setCornerWidget(corner, Qt.Corner.TopRightCorner)
        self.statusBar().showMessage("管理器已启动")

    def _tab_changed(self, index: int) -> None:
        if (
            index == getattr(self, "result_tab_index", -1)
            and self.controller.startup_state
            in (StartupState.READY, StartupState.DEGRADED_READ_ONLY)
        ):
            self.refresh_results()

    def _overview_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        title = QLabel("DouK 全流程一体化管理器")
        title.setObjectName("title")
        subtitle = QLabel(
            "统一管理账号采集、任务配置、5-1-1 下载、截图归档、快捷方式索引和分级备份。"
        )
        subtitle.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(subtitle)

        startup_box = QGroupBox("启动安全状态")
        startup_layout = QGridLayout(startup_box)
        self.startup_state_label = QLabel(StartupState.BOOTSTRAPPING.value)
        self.startup_stage_label = QLabel("尚未开始")
        self.startup_summary_label = QLabel("检查中")
        self.startup_summary_label.setWordWrap(True)
        startup_layout.addWidget(QLabel("当前状态"), 0, 0)
        startup_layout.addWidget(self.startup_state_label, 0, 1)
        startup_layout.addWidget(QLabel("阶段"), 1, 0)
        startup_layout.addWidget(self.startup_stage_label, 1, 1)
        startup_layout.addWidget(QLabel("用户摘要"), 2, 0)
        startup_layout.addWidget(self.startup_summary_label, 2, 1)
        self.startup_details = QTextEdit()
        self.startup_details.setReadOnly(True)
        self.startup_details.setMaximumHeight(82)
        startup_layout.addWidget(QLabel("技术详情"), 3, 0, Qt.AlignmentFlag.AlignTop)
        startup_layout.addWidget(self.startup_details, 3, 1)
        startup_buttons = QHBoxLayout()
        self.startup_copy_button = self._mark_safe_widget(QPushButton("复制错误"))
        self.startup_copy_button.clicked.connect(self._copy_startup_error)
        self.startup_log_button = self._mark_safe_widget(QPushButton("打开管理器日志"))
        self.startup_log_button.clicked.connect(self._open_manager_log)
        self.startup_recheck_button = self._mark_safe_widget(QPushButton("重新检查"))
        self.startup_recheck_button.clicked.connect(self.begin_startup_check)
        for button in (
            self.startup_copy_button,
            self.startup_log_button,
            self.startup_recheck_button,
        ):
            startup_buttons.addWidget(button)
        startup_buttons.addStretch()
        startup_layout.addLayout(startup_buttons, 4, 1)
        layout.addWidget(startup_box)

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
            ("手动完整备份 Volume（大文件）", self._manual_backup),
            ("打开备份目录", lambda: self._open_path(self.controller.paths.backups)),
            ("打开任务目录", lambda: self._open_path(self.controller.paths.tasks)),
            ("打开日志目录", lambda: self._open_path(self.controller.paths.logs)),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            if text == "刷新状态":
                self.refresh_status_button = button
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
        self.task_smart_private = QCheckBox("智能跳过近期已确认私密账号")
        self.task_smart_private.setToolTip(
            "扫描最近有效期内全部可解析任务日志，只跳过日志中明确记为“私密账号”的 "
            "A 编号；旧日志未列出的账号及错误/中断/未开始状态不会被猜测。创建前会预览"
            "跳过名单，可强制包含全部账号。"
        )
        self.task_private_days = self._spin(1, 3650, 3)
        self.task_private_days.setToolTip(
            "参考期限：扫描最近 N 天内的全部可解析下载任务日志。例如 3、7、15；"
            "若之后出现明确正常结果，账号会重新纳入。"
        )
        self.task_pause_console = QCheckBox(
            "下载结束后保留黑框，查看统计后按任意键关闭"
        )
        self.task_pause_console.setChecked(True)
        self.task_pause_console.setToolTip(
            "当前任务完成后保留下载引擎黑框，需按任意键才会进入后续流程；"
            "会影响暂停队列和完成后关机倒计时。完全无人值守时请取消勾选。"
        )
        self.task_pause_console.stateChanged.connect(
            lambda state: self._result_view_option_changed("task", state)
        )
        form.addRow("账号表达式", self.task_expression)
        form.addRow("任务名称", self.task_name)
        form.addRow("earliest 处理", self.task_earliest_mode)
        form.addRow("earliest 值", self.task_earliest_value)
        form.addRow("主档持久化", self.task_persist_master)
        form.addRow("私密账号智能跳过", self.task_smart_private)
        form.addRow("私密参考期限（天）", self.task_private_days)
        form.addRow("结果查看", self.task_pause_console)
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
            "这里列出的是可反复使用的任务模板，不是历史记录。请勾选模板；激活或运行时，"
            "管理器会先备份，再将它复制为下载器唯一读取的正式 settings.json。任何时候只"
            "启动一个 main.exe，共用唯一 DouK-Downloader.db。自动模式使用 "
            "run_command = 5 1 1 Q，会直接进入抖音批量账号下载。"
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        task_row = QHBoxLayout()
        self.task_list = TaskTemplateList()
        self.task_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.task_list.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.task_list.setDragEnabled(False)
        self.task_list.setAcceptDrops(False)
        self.task_list.viewport().setAcceptDrops(False)
        self.task_list.setDropIndicatorShown(False)
        self.task_list.setDefaultDropAction(Qt.DropAction.IgnoreAction)
        self.task_list.setToolTip(
            "勾选决定是否参加队列；高亮一项后使用右侧位置控制，"
            "或按 Alt+↑ / Alt+↓ 移动。列表拖放已完全关闭。"
        )
        self.task_list.itemChanged.connect(self._task_check_changed)
        task_row.addWidget(self.task_list, 1)

        order_box = QGroupBox("调整队列顺序")
        order_box.setMinimumWidth(220)
        order_layout = QVBoxLayout(order_box)
        order_help = QLabel("先高亮一个任务，再选择位置；勾选状态不受影响。")
        order_help.setWordWrap(True)
        order_layout.addWidget(order_help)
        self.queue_move_target = QComboBox()
        order_layout.addWidget(self.queue_move_target)
        position_row = QHBoxLayout()
        position_row.addWidget(QLabel("指定第几位"))
        self.queue_move_position = QSpinBox()
        self.queue_move_position.setRange(1, 1)
        self.queue_move_position.setSuffix(" 位")
        self.queue_move_position.setKeyboardTracking(False)
        position_row.addWidget(self.queue_move_position, 1)
        order_layout.addLayout(position_row)
        self.queue_move_target.currentIndexChanged.connect(
            self._update_move_position_enabled
        )
        self.task_list.currentRowChanged.connect(self._task_highlight_changed)
        move_button = QPushButton("移动高亮任务")
        move_button.clicked.connect(self._move_highlighted_task)
        order_layout.addWidget(move_button)
        self.queue_move_up_shortcut = QShortcut(QKeySequence("Alt+Up"), self.task_list)
        self.queue_move_up_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self.queue_move_up_shortcut.activated.connect(
            lambda: self._move_highlighted_task_by_action("up")
        )
        self.queue_move_down_shortcut = QShortcut(
            QKeySequence("Alt+Down"), self.task_list
        )
        self.queue_move_down_shortcut.setContext(
            Qt.ShortcutContext.WidgetWithChildrenShortcut
        )
        self.queue_move_down_shortcut.activated.connect(
            lambda: self._move_highlighted_task_by_action("down")
        )
        restore_button = QPushButton("恢复按 A 编号排序")
        restore_button.setToolTip("清除人工调整的槽位顺序，恢复按任务首个 A 编号自然排序。")
        restore_button.clicked.connect(self._restore_task_order)
        order_layout.addWidget(restore_button)
        select_all = QPushButton("全选模板")
        select_all.setToolTip("勾选下载队列中的全部模板。")
        select_all.clicked.connect(lambda: self._set_all_task_checks(True))
        clear_all = QPushButton("取消全选")
        clear_all.setToolTip("取消下载队列中全部模板的勾选。")
        clear_all.clicked.connect(lambda: self._set_all_task_checks(False))
        delete = QPushButton("删除已选模板")
        delete.setToolTip(
            "只删除 Data/Tasks 中勾选的模板，不删除排队任务、历史日志或数据库。"
        )
        delete.clicked.connect(self._delete_selected_tasks)
        for button in (select_all, clear_all, delete):
            order_layout.addWidget(button)
        order_note = QLabel(
            "精确移动、Alt+↑ / Alt+↓ 和顺序执行共用同一顺序，重启后保留；"
            "列表不支持拖放。"
        )
        order_note.setWordWrap(True)
        order_layout.addWidget(order_note)
        order_layout.addStretch()
        task_row.addWidget(order_box)
        layout.addLayout(task_row, 1)
        options = QGroupBox("本次队列后续动作")
        form = QFormLayout(options)
        self.queue_screenshot_mode = self._post_combo(self.controller.config.screenshot_post_mode)
        self.queue_index_mode = self._post_combo(self.controller.config.index_post_mode)
        self.queue_cleanup = QCheckBox("索引刷新后再次扫描并重试清理")
        self.queue_cleanup.setChecked(self.controller.config.cleanup_after_index)
        self.queue_cleanup.setToolTip(
            "索引刷新本身已自动清理两类受管快捷方式；勾选后会再运行一次独立清理，"
            "用于复查或重试第一次删除失败的项目。"
        )
        self.queue_pause_console = QCheckBox(
            "下载结束后保留黑框，查看统计后按任意键关闭（手动检查推荐）"
        )
        self.queue_pause_console.setChecked(True)
        self.queue_pause_console.setToolTip(
            "当前任务完成后保留下载引擎黑框，按任意键才会进入后续队列；"
            "会影响暂停队列和完成后关机倒计时。完全无人值守时请取消勾选。"
        )
        self.queue_pause_console.stateChanged.connect(
            lambda state: self._result_view_option_changed("queue", state)
        )
        self.queue_pause_button = QPushButton("暂停队列")
        self.queue_pause_button.setToolTip(
            "只暂停队列继续启动下一个模板，不会暂停当前已经运行的下载进程；"
            "用于和以后可能支持的“暂停当前下载”区分。"
        )
        self.queue_pause_button.clicked.connect(self._toggle_queue_pause)
        self.queue_cancel_button = QPushButton("取消全部下载任务")
        self.queue_cancel_button.setToolTip(
            "立即结束当前由管理器启动的批量下载进程，并清空本次剩余模板队列；"
            "不会删除任务模板、历史日志、settings 或数据库。"
        )
        self.queue_cancel_button.clicked.connect(self._cancel_all_downloads)
        self.queue_shutdown = QCheckBox("全部任务完成后正常关机（60秒倒计时，可取消）")
        self.queue_shutdown.setToolTip(
            "仅在所有任务、结果汇总和后续动作成功后执行；采集服务启用/运行时不可勾选。"
            "完全无人值守下载并关机时，请取消结果查看；若同时勾选结果查看，"
            "必须按任意键确认后才会进入关机倒计时。使用 Windows 正常关机，"
            "不强制关闭其他应用。"
        )
        self.queue_shutdown.stateChanged.connect(self._shutdown_option_changed)
        form.addRow("截图归档", self.queue_screenshot_mode)
        form.addRow("索引刷新", self.queue_index_mode)
        form.addRow("清理复查", self.queue_cleanup)
        form.addRow("结果查看", self.queue_pause_console)
        form.addRow("队列控制", self.queue_pause_button)
        form.addRow("终止任务", self.queue_cancel_button)
        self.queue_elapsed_label = QLabel("本次队列耗时：未开始")
        self.task_elapsed_label = QLabel("当前任务耗时：未开始")
        form.addRow("耗时统计", self.queue_elapsed_label)
        form.addRow("", self.task_elapsed_label)
        form.addRow("完成后动作", self.queue_shutdown)
        layout.addWidget(options)
        # Keep the action buttons in two predictable rows.  The queue page has
        # enough actions that a single horizontal row becomes unreadable at
        # the minimum window width.
        buttons = QGridLayout()
        refresh = QPushButton("刷新任务列表")
        refresh.clicked.connect(self.refresh_tasks)
        activate = QPushButton("应用为正式 setting")
        activate.setToolTip("只能勾选一个模板；将模板复制为下载器唯一读取的正式 setting。")
        activate.clicked.connect(self._activate_selected_task)
        run_current = QPushButton("运行当前 setting")
        run_current.setToolTip("直接运行当前正式 setting，不重新选择模板。")
        run_current.clicked.connect(self._start_current)
        run_queue = QPushButton("按顺序运行已选")
        run_queue.setToolTip("按列表当前顺序逐个激活并运行已勾选模板。")
        run_queue.clicked.connect(self._start_queue)
        native_logs = QPushButton("打开下载器日志")
        native_logs.setToolTip("打开下载引擎 Volume/Log 原生日志目录。")
        native_logs.clicked.connect(
            lambda: self._open_path(self.controller.paths.volume / "log")
        )
        open_tasks = QPushButton("打开任务目录")
        open_tasks.setToolTip("打开 Data/Tasks 模板目录。")
        open_tasks.clicked.connect(lambda: self._open_path(self.controller.paths.tasks))
        open_logs = QPushButton("打开日志目录")
        open_logs.setToolTip("打开管理器 Logs 目录，查看任务结果和管理器日志。")
        open_logs.clicked.connect(lambda: self._open_path(self.controller.paths.logs))
        self.monitor_start_button = QPushButton("启动后台监听")
        self.monitor_start_button.setToolTip(
            "把正式 setting 的 run_command 原子切换为 6，启动下载引擎剪贴板监听；"
            "停止监听后恢复为 5 1 1 Q。"
        )
        self.monitor_start_button.clicked.connect(self._start_monitor)
        self.monitor_stop_button = QPushButton("停止后台监听")
        self.monitor_stop_button.setToolTip(
            "向监听进程发送正常停止信号并等待退出；确认退出后才恢复批量下载命令。"
        )
        self.monitor_stop_button.clicked.connect(self._stop_monitor)
        queue_buttons = (
            refresh, activate, run_current, run_queue, open_tasks, open_logs, native_logs,
            self.monitor_start_button, self.monitor_stop_button,
        )
        for index, button in enumerate(queue_buttons):
            buttons.addWidget(button, index // 5, index % 5)
        for column in range(5):
            buttons.setColumnStretch(column, 1)
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
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        self.refresh_index_button = QPushButton("立即刷新索引")
        self.refresh_index_button.clicked.connect(self._refresh_index)
        buttons.addWidget(self.refresh_index_button)
        self.cleanup_index_button = QPushButton("立即重新扫描并清理快捷方式")
        self.cleanup_index_button.setToolTip(
            "独立扫描并清理两类受管快捷方式，可用于不刷新索引时的维护，"
            "或重试上次删除失败的项目。"
        )
        self.cleanup_index_button.clicked.connect(self._cleanup_index)
        buttons.addWidget(self.cleanup_index_button)
        self.cleanup_test_button = QPushButton("安全自检清理功能")
        self.cleanup_test_button.setToolTip(
            "仅在 Windows 临时目录测试清理规则，不读取或修改正式视频目录和索引目录。"
        )
        self.cleanup_test_button.clicked.connect(self._cleanup_index_self_test)
        buttons.addWidget(self.cleanup_test_button)
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
            "正式 Volume 始终由 main.exe 所在目录推导，不能单独选择另一份数据库或主档。"
            "自动备份只保存 settings_master.json、settings.json 和 DouK-Downloader.db，并按类别限制保留数量；"
            "只有手动完整备份和下载引擎更新前才复制整个 Volume。"
        )
        warning.setWordWrap(True)
        layout.addWidget(warning)
        box = QGroupBox("正式路径")
        grid = QGridLayout(box)
        self.engine_edit = QLineEdit(self.controller.config.engine_exe)
        self.video_edit = QLineEdit(self.controller.config.video_root)
        self.index_edit = QLineEdit(self.controller.config.index_root)
        self.old_screenshot_edit = QLineEdit(self.controller.config.old_screenshot_dir)
        for edit in (
            self.engine_edit,
            self.video_edit,
            self.index_edit,
            self.old_screenshot_edit,
        ):
            self._mark_path_widget(edit)
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
            self._mark_path_widget(button)
            grid.addWidget(button, row, 2)
        layout.addWidget(box)

        task_box = QGroupBox("下载与任务后续动作默认值")
        form = QFormLayout(task_box)
        self.setting_batch_accounts = self._spin(1, 100000, self.controller.config.batch_accounts)
        self.setting_rest_seconds = self._spin(0, 86400, self.controller.config.rest_seconds)
        self.setting_screenshot_mode = self._post_combo(self.controller.config.screenshot_post_mode)
        self.setting_index_mode = self._post_combo(self.controller.config.index_post_mode)
        self.setting_cleanup = QCheckBox("刷新索引后再次扫描并重试清理")
        self.setting_cleanup.setChecked(self.controller.config.cleanup_after_index)
        self.setting_cleanup.setToolTip(
            "刷新已自动清理；此选项会额外运行独立清理，用于复查或重试失败项。"
        )
        form.addRow("每批账号数", self.setting_batch_accounts)
        form.addRow("暂停秒数", self.setting_rest_seconds)
        form.addRow("默认截图归档", self.setting_screenshot_mode)
        form.addRow("默认索引刷新", self.setting_index_mode)
        form.addRow("默认清理复查", self.setting_cleanup)
        layout.addWidget(task_box)
        note = QLabel(
            "兼容版下载引擎会读取这里的批次和暂停数值；旧版仍按自身内置值运行。"
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.path_save_button = self._mark_path_widget(
            QPushButton("仅保存正式路径并重新检查")
        )
        self.path_save_button.clicked.connect(self._save_paths)
        layout.addWidget(self.path_save_button)
        self.settings_save_button = QPushButton("保存全部设置并重新验证正式数据")
        self.settings_save_button.clicked.connect(self._save_settings)
        layout.addWidget(self.settings_save_button)

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

    def _result_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        intro = QLabel(
            "这里读取已有 Logs\\DownloadTasks\\DownloadTask_*.log，不建立第二份结果数据库。"
            "V0.1.4 新日志可以按 A 编号追溯主状态；V0.1.3 旧日志只显示其中实际记录的状态。"
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        filters = QHBoxLayout()
        self.result_status_filter = QComboBox()
        self._mark_safe_widget(self.result_status_filter)
        self.result_status_filter.addItem("全部状态", "")
        for status in AccountStatus:
            self.result_status_filter.addItem(_status_label(status), status.value)
        self.result_account_filter = QLineEdit()
        self._mark_safe_widget(self.result_account_filter)
        self.result_account_filter.setPlaceholderText("A 编号，例如 55")
        self.result_task_filter = QLineEdit()
        self._mark_safe_widget(self.result_task_filter)
        self.result_task_filter.setPlaceholderText("任务名称关键字")
        self.result_status_filter.currentIndexChanged.connect(
            self._schedule_result_refresh
        )
        self.result_account_filter.textChanged.connect(self._schedule_result_refresh)
        self.result_task_filter.textChanged.connect(self._schedule_result_refresh)
        filters.addWidget(QLabel("状态"))
        filters.addWidget(self.result_status_filter)
        filters.addWidget(self.result_account_filter)
        filters.addWidget(self.result_task_filter)
        refresh_button = QPushButton("立即刷新结果")
        refresh_button.setToolTip("立即重新读取 Logs\\DownloadTasks 中的最新任务结果。")
        refresh_button.clicked.connect(self.refresh_results)
        self._mark_safe_widget(refresh_button)
        filters.addWidget(refresh_button)
        layout.addLayout(filters)
        self.result_table = QTableWidget(0, 6)
        self.result_table.setHorizontalHeaderLabels(
            ("结束时间", "账号", "状态", "异常附加", "任务模板", "来源日志")
        )
        self.result_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.result_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.result_table.horizontalHeader().setStretchLastSection(True)
        self.result_table.cellDoubleClicked.connect(self._open_result_log)
        self.result_table.setToolTip("双击“来源日志”单元格可直接打开对应任务日志。")
        layout.addWidget(self.result_table, 1)
        self.result_note = QLabel()
        self.result_note.setWordWrap(True)
        layout.addWidget(self.result_note)
        self.result_last_refresh = QLabel("最近刷新：未刷新")
        layout.addWidget(self.result_last_refresh)
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

    @staticmethod
    def _append_info(output: QTextEdit, *messages: object, merge: bool = False) -> None:
        text = format_information(*messages, merge=merge)
        if text:
            output.append(text)

    @staticmethod
    def _replace_info(output: QTextEdit, *messages: object, merge: bool = False) -> None:
        output.setPlainText(format_information(*messages, merge=merge))

    def _run(
        self,
        action: Callable[[], T],
        output: QTextEdit | None = None,
        *,
        cancel_shutdown: bool = True,
    ) -> T | None:
        if cancel_shutdown:
            self._cancel_shutdown_for_new_work()
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            result = action()
            return result
        except Exception as exc:
            message = str(exc)
            self.controller.logger.exception("界面操作失败：%s", message)
            if output is not None:
                self._append_info(output, "【失败】" + message)
            self.statusBar().showMessage("操作失败")
            QMessageBox.critical(self, "操作失败", message)
            return None
        finally:
            QApplication.restoreOverrideCursor()
            self.refresh_all()

    def _run_index_background(
        self,
        action: Callable[[], object],
        *,
        started_message: str,
        success: Callable[[object], None],
    ) -> None:
        self._cancel_shutdown_for_new_work()
        if self.background_thread is not None and self.background_thread.isRunning():
            self._append_info(self.post_output, "已有索引任务正在运行，请等待完成。")
            return

        self._replace_info(self.post_output, started_message)
        self.statusBar().showMessage(started_message)
        self.refresh_index_button.setEnabled(False)
        self.cleanup_index_button.setEnabled(False)
        self.cleanup_test_button.setEnabled(False)

        thread = QThread(self)
        worker = ActionWorker(action)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(thread.quit)
        thread.finished.connect(self._finish_index_background)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self.background_thread = thread
        self.background_worker = worker
        self.background_output = self.post_output
        self.background_success = success
        thread.start()

    @Slot()
    def _finish_index_background(self) -> None:
        worker = self.background_worker
        try:
            if worker is None:
                return
            if worker.error is not None:
                message = str(worker.error)
                self.controller.logger.exception(
                    "后台索引操作失败：%s", message, exc_info=worker.error
                )
                if self.background_output is not None:
                    self._append_info(self.background_output, "【失败】" + message)
                self.statusBar().showMessage("操作失败")
                QMessageBox.critical(self, "操作失败", message)
            elif self.background_success is not None:
                self.background_success(worker.result)
                self.statusBar().showMessage("操作完成")
        finally:
            self.refresh_index_button.setEnabled(True)
            self.cleanup_index_button.setEnabled(True)
            self.cleanup_test_button.setEnabled(True)
            self.background_thread = None
            self.background_worker = None
            self.background_output = None
            self.background_success = None
            self.refresh_all()

    def _refresh_status(self, _checked: bool = False) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        self.refresh_all(check_processes=True)

    def refresh_all(self, *, check_processes: bool = False) -> None:
        health = self.controller.health(check_processes=check_processes)
        self._apply_action_gate()
        self._render_health_snapshot(health)
        if self.controller.startup_state is not StartupState.READY:
            self._apply_action_gate()
        self.refresh_tasks()
        if hasattr(self, "result_table"):
            self.refresh_results()

    def _render_health_snapshot(self, health: dict[str, object]) -> None:
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
        collector_running = bool(health.get("collector_running"))
        engine_running = bool(health.get("engine_running"))
        mode = str(health.get("engine_mode") or "")
        self.global_collector_status.setText("运行中" if collector_running else "未运行")
        self.global_engine_status.setText(
            "后台监听"
            if mode == ENGINE_MODE_MONITOR
            else ("批量下载" if engine_running else "未运行")
        )
        for label, value in (
            (self.global_collector_status, collector_running),
            (self.global_engine_status, engine_running),
        ):
            label.setProperty("ok", value)
            label.style().unpolish(label)
            label.style().polish(label)
        if hasattr(self, "queue_shutdown"):
            self.queue_shutdown.setEnabled(
                not collector_running and mode != ENGINE_MODE_MONITOR
            )
            if collector_running and self.queue_shutdown.isChecked():
                self.queue_shutdown.setChecked(False)
        if hasattr(self, "queue_pause_button"):
            self.queue_pause_button.setEnabled(
                self.queue_active
                and not self.queue_cancel_requested
                and str(health.get("engine_mode") or "") != ENGINE_MODE_MONITOR
            )
        if hasattr(self, "queue_cancel_button"):
            self.queue_cancel_button.setEnabled(
                self.queue_active
                and not self.queue_cancel_requested
                and str(health.get("engine_mode") or "") != ENGINE_MODE_MONITOR
            )
        if hasattr(self, "monitor_start_button"):
            monitor_running = mode == ENGINE_MODE_MONITOR
            self.monitor_start_button.setEnabled(not engine_running and not collector_running)
            self.monitor_stop_button.setEnabled(monitor_running)
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

    def refresh_tasks(self) -> None:
        checked_paths = {
            item.data(Qt.UserRole)
            for index in range(self.task_list.count())
            if (item := self.task_list.item(index)).checkState() == Qt.Checked
        }
        current_item = self.task_list.currentItem()
        current_path = current_item.data(Qt.UserRole) if current_item else None
        restored_current: QListWidgetItem | None = None
        signals_were_blocked = self.task_list.blockSignals(True)
        try:
            self.task_list.clear()
            for path in self.controller.list_tasks():
                checked = str(path) in checked_paths
                state_text = "【已勾选】" if checked else "【未勾选】"
                item = QListWidgetItem(f"{state_text} {path.name}")
                item.setData(Qt.UserRole, str(path))
                item.setFlags(
                    (item.flags() | Qt.ItemIsUserCheckable)
                    & ~Qt.ItemIsDragEnabled
                    & ~Qt.ItemIsDropEnabled
                )
                item.setCheckState(Qt.Checked if checked else Qt.Unchecked)
                item.setToolTip(
                    "勾选后可复制为正式 settings.json，或加入顺序下载队列；"
                    "高亮后可使用右侧位置控制或 Alt+↑ / Alt+↓ 调整执行顺序。"
                )
                self.task_list.addItem(item)
                if str(path) == current_path:
                    restored_current = item
            if restored_current is not None:
                self.task_list.setCurrentItem(restored_current)
        finally:
            self.task_list.blockSignals(signals_were_blocked)
        self._update_move_targets(self.task_list.count())

    def _selection_checks(self) -> None:
        if self._syncing_task_selection:
            return
        self._syncing_task_selection = True
        try:
            # Qt's ExtendedSelection gives normal Ctrl/Shift and left-button
            # drag selection.  Keeping selected rows checked makes that
            # gesture useful for queue/delete actions without reintroducing
            # unsafe list drag-and-drop reordering.
            for item in self.task_list.selectedItems():
                if item.checkState() != Qt.CheckState.Checked:
                    item.setCheckState(Qt.CheckState.Checked)
        finally:
            self._syncing_task_selection = False

    def _set_all_task_checks(self, checked: bool) -> None:
        if self.queue_active:
            QMessageBox.information(self, "队列运行中", "队列运行时不能修改模板勾选状态。")
            return
        signals_were_blocked = self.task_list.blockSignals(True)
        try:
            for index in range(self.task_list.count()):
                self.task_list.item(index).setCheckState(
                    Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
                )
        finally:
            self.task_list.blockSignals(signals_were_blocked)
        for index in range(self.task_list.count()):
            self._task_check_changed(self.task_list.item(index))

    def _delete_selected_tasks(self) -> None:
        if self._queue_state_locked():
            return
        paths = tuple(self._selected_task_paths())
        if not paths:
            QMessageBox.information(self, "未勾选模板", "请先勾选要删除的模板。")
            return
        answer = QMessageBox.question(
            self,
            "确认删除任务模板",
            "将永久删除以下任务模板文件（只限 Data\\Tasks，不删除历史日志和已排队任务）：\n\n"
            + "\n".join(path.name for path in paths)
            + "\n\n确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        result = self._run(lambda: self.controller.delete_tasks(paths), self.queue_output)
        if result:
            self._append_info(self.queue_output, f"已删除 {len(result)} 个任务模板；历史结果日志未删除。")
            self.refresh_tasks()

    def _schedule_result_refresh(self, *_args) -> None:
        if self.controller.startup_state not in (
            StartupState.READY,
            StartupState.DEGRADED_READ_ONLY,
        ):
            return
        if hasattr(self, "result_refresh_timer"):
            self.result_refresh_timer.start(150)
        else:
            self.refresh_results()

    def _open_result_log(self, row: int, column: int) -> None:
        if column != 5 or not hasattr(self, "result_table"):
            return
        item = self.result_table.item(row, column)
        if item is None:
            return
        path = Path(item.text().strip())
        if not path.is_file():
            QMessageBox.information(self, "日志不存在", f"来源日志不存在：\n{path}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.information(self, "无法打开日志", f"无法打开来源日志：\n{path}")

    def refresh_results(self) -> None:
        if not hasattr(self, "result_table"):
            return
        selected_status = str(self.result_status_filter.currentData() or "")
        account_text = self.result_account_filter.text().strip().lstrip("Aa")
        task_text = self.result_task_filter.text().strip().casefold()
        try:
            account_number = int(account_text) if account_text else None
        except ValueError:
            account_number = -1
        rows = self.controller.result_rows(limit=500)
        filtered = tuple(
            row
            for row in rows
            if (not selected_status or getattr(row.status, "value", row.status) == selected_status)
            and (account_number is None or row.a_number == account_number)
            and (not task_text or task_text in row.task_template.casefold())
        )
        self.result_table.setRowCount(len(filtered))
        for index, row in enumerate(filtered):
            values = (
                row.ended_at.strftime("%Y-%m-%d %H:%M:%S"),
                f"A{row.a_number}",
                _status_label(row.status),
                "是" if row.completed_with_anomaly else "否",
                row.task_template,
                str(row.task_log),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 5:
                    item.setToolTip("双击打开来源日志")
                self.result_table.setItem(index, column, item)
        runs = self.controller.result_runs(limit=500)
        incomplete_old = sum(1 for run in runs if run.account_rows and not run.details_complete)
        self.result_note.setText(
            f"共读取 {len(runs)} 次任务日志，显示 {len(filtered)} 条账号结果。"
            + (f"其中 {incomplete_old} 次旧日志没有完整列出正常账号，页面不会猜测缺失状态。" if incomplete_old else "")
        )
        self.result_last_refresh.setText(
            f"最近刷新：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def _update_move_targets(self, count: int) -> None:
        previous_action = str(self.queue_move_target.currentData() or "")
        signals_were_blocked = self.queue_move_target.blockSignals(True)
        try:
            self.queue_move_target.clear()
            self.queue_move_target.addItem("移到最前", "first")
            self.queue_move_target.addItem("上移一位", "up")
            self.queue_move_target.addItem("下移一位", "down")
            self.queue_move_target.addItem("移到最后", "last")
            self.queue_move_target.addItem("移到指定位置", "position")
            previous_index = self.queue_move_target.findData(previous_action)
            if previous_index >= 0:
                self.queue_move_target.setCurrentIndex(previous_index)
        finally:
            self.queue_move_target.blockSignals(signals_were_blocked)
        self.queue_move_position.setRange(1, max(1, count))
        current_row = self.task_list.currentRow()
        if current_row >= 0:
            self.queue_move_position.setValue(current_row + 1)
        self._update_move_position_enabled()

    def _update_move_position_enabled(self) -> None:
        self.queue_move_position.setEnabled(
            self.task_list.count() > 0
            and self.queue_move_target.currentData() == "position"
        )

    def _task_highlight_changed(self, row: int) -> None:
        if row >= 0:
            self.queue_move_position.setValue(row + 1)

    def _task_paths_in_list(self) -> list[Path]:
        return [
            Path(self.task_list.item(index).data(Qt.UserRole))
            for index in range(self.task_list.count())
        ]

    def _move_highlighted_task(self) -> None:
        self._move_highlighted_task_by_action(
            str(self.queue_move_target.currentData() or "")
        )

    def _move_highlighted_task_by_action(self, action: str) -> None:
        if self.queue_active:
            QMessageBox.information(
                self,
                "队列运行中",
                "当前队列已经锁定执行顺序；请等待结束后再调整下一次的顺序。",
            )
            return
        source = self.task_list.currentRow()
        if source < 0:
            QMessageBox.information(self, "未高亮任务", "请先单击高亮一个要移动的任务。")
            return
        count = self.task_list.count()
        if action == "first":
            target = 0
        elif action == "up":
            target = source - 1
        elif action == "down":
            target = source + 1
        elif action == "last":
            target = count - 1
        elif action == "position":
            target = self.queue_move_position.value() - 1
        else:
            QMessageBox.warning(self, "位置无效", "请选择一个有效的移动位置。")
            return

        target = max(0, min(target, count - 1))
        reordered = list(move_to_index(self._task_paths_in_list(), source, target))
        selected_path = reordered[target]
        result = self._run(
            lambda: self.controller.save_task_order(reordered), self.queue_output
        )
        if result is not None:
            self.refresh_tasks()
            for index in range(self.task_list.count()):
                item = self.task_list.item(index)
                if Path(item.data(Qt.UserRole)) == selected_path:
                    self.task_list.setCurrentItem(item)
                    break
            self._append_info(
                self.queue_output,
                f"已将 {selected_path.name} 移到第 {target + 1} 位并保存。",
            )

    def _restore_task_order(self) -> None:
        if self.queue_active:
            QMessageBox.information(
                self,
                "队列运行中",
                "当前队列已经锁定执行顺序；请等待结束后再恢复排序。",
            )
            return
        result = self._run(self.controller.restore_task_order, self.queue_output)
        if result is not None:
            self.refresh_tasks()
            self._append_info(
                self.queue_output, "已恢复按任务首个 A 编号排序，并清除人工槽位顺序。"
            )

    def _task_check_changed(self, item: QListWidgetItem) -> None:
        path_text = item.data(Qt.UserRole)
        if not path_text:
            return
        state_text = "【已勾选】" if item.checkState() == Qt.Checked else "【未勾选】"
        desired = f"{state_text} {Path(path_text).name}"
        if item.text() == desired:
            return
        signals_were_blocked = self.task_list.blockSignals(True)
        try:
            item.setText(desired)
        finally:
            self.task_list.blockSignals(signals_were_blocked)

    def _preview_task(self) -> None:
        if self.task_smart_private.isChecked():
            preview = self._run(
                lambda: self.controller.preview_private_skip(
                    self.task_expression.text(), self.task_private_days.value()
                ),
                self.task_output,
            )
            if preview:
                self._replace_info(self.task_output, *self._smart_preview_lines(preview))
            return
        preview = self._run(
            lambda: self.controller.preview_selection(self.task_expression.text()),
            self.task_output,
        )
        if preview:
            self._replace_info(
                self.task_output,
                f"标准化选择：{preview.compact}",
                f"主档位置总数：{preview.total_positions}",
                f"选中位置：{preview.selected_positions}",
                f"有效URL：{preview.selected_valid_urls}",
                f"空白URL位置：{preview.selected_blank_urls}",
                f"其他位置将设为 false：{preview.unselected_positions}",
                f"重复编号：{len(preview.selection.duplicate_numbers)}",
                "主档 enable：不会修改",
            )

    @staticmethod
    def _smart_preview_lines(preview) -> tuple[str, ...]:
        category_labels = {
            "recent_private": "近期明确私密（将跳过）",
            "recent_non_private": "近期明确非私密（纳入）",
            "recent_uncertain": "近期仅有未开始/错误/中断（纳入）",
            "expired_private": "历史私密但已过期（纳入）",
            "expired_non_private": "历史非私密但已过期（纳入）",
            "expired_uncertain": "历史仅有不确定状态（纳入）",
            "no_record": "没有可靠历史记录（纳入）",
        }
        grouped: dict[str, list[str]] = {key: [] for key in category_labels}
        sources: list[str] = []
        for decision in getattr(preview, "decisions", ()):
            grouped.setdefault(decision.category, []).append(f"A{decision.a_number}")
            if decision.category == "recent_private":
                sources.append(f"A{decision.a_number}：{decision.source_text}")
        skipped = tuple(match.a_number for match in preview.private_matches)
        effective = preview.effective.compact if preview.effective else "（跳过后为空）"
        lines: list[str] = [
            f"智能跳过已启用：扫描最近 {preview.validity_days} 天内全部可解析任务日志",
            f"输入账号：{preview.requested.compact}（共 {preview.requested.selected_positions} 个，"
            f"有效URL {preview.requested.selected_valid_urls} 个）",
            f"最终纳入：{effective}",
        ]
        for category, label in category_labels.items():
            values = grouped.get(category) or []
            lines.append(f"{label}：{'、'.join(values) if values else '无'}")
        if sources:
            lines.append("近期私密依据：")
            lines.extend(sources)
        lines.append("创建时可选择：按预览跳过、强制包含全部或取消创建。")
        return tuple(lines)

    def _queue_state_locked(self) -> bool:
        health_fn = getattr(self.controller, "health", None)
        health = health_fn() if callable(health_fn) else {}
        if health.get("monitor_running"):
            QMessageBox.warning(
                self,
                "后台监听运行中",
                "当前为后台剪贴板监听模式，请先停止监听后再运行批量下载任务。",
            )
            return True
        if not self.queue_active and self.download_summary_thread is None:
            return False
        QMessageBox.warning(
            self,
            "队列运行中",
            "当前下载任务仍在运行或正在汇总账号结果；请等待完成后再激活或启动其他任务。",
        )
        return True

    @staticmethod
    def _format_elapsed(seconds: float) -> str:
        total = max(0, int(seconds))
        hours, remainder = divmod(total, 3600)
        minutes, seconds = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _update_elapsed_labels(self) -> None:
        now = datetime.now()
        if self.queue_started_at is not None:
            elapsed = self._format_elapsed((now - self.queue_started_at).total_seconds())
            self.queue_elapsed_label.setText(f"本次队列耗时：{elapsed}（运行中）")
        if self.current_task_started_at is not None:
            elapsed = self._format_elapsed(
                (now - self.current_task_started_at).total_seconds()
            )
            self.task_elapsed_label.setText(f"当前任务耗时：{elapsed}（运行中）")

    def _mark_download_started(self, run) -> None:
        now = datetime.now()
        if self.queue_started_at is None:
            self.queue_started_at = now
            self.queue_elapsed_label.setText("本次队列耗时：00:00:00（运行中）")
        self.current_task_started_at = now
        self.task_elapsed_label.setText("当前任务耗时：00:00:00（运行中）")

    def _record_task_elapsed(self, run, reason: str) -> None:
        started = self.current_task_started_at
        if started is None:
            return
        elapsed = self._format_elapsed((datetime.now() - started).total_seconds())
        self.current_task_started_at = None
        template = getattr(run, "task_template", None) or "当前 settings.json"
        text = f"本次下载任务耗时：{elapsed}；模板={template}；结果={reason}"
        self.task_elapsed_label.setText(f"当前任务耗时：{elapsed}（{reason}）")
        self._append_info(self.queue_output, text)
        self.controller.logger.info(text)

    def _record_queue_elapsed(self, reason: str) -> None:
        started = self.queue_started_at
        if started is None:
            return
        elapsed = self._format_elapsed((datetime.now() - started).total_seconds())
        self.queue_started_at = None
        text = f"本次批量下载队列耗时：{elapsed}；结果={reason}"
        self.queue_elapsed_label.setText(f"本次队列耗时：{elapsed}（{reason}）")
        self._append_info(self.queue_output, text)
        self.controller.logger.info(text)

    def _create_task(self, activate: bool, start: bool) -> None:
        if activate and self._queue_state_locked():
            return
        rule = self._earliest_rule(self.task_earliest_mode, self.task_earliest_value)
        excluded_numbers: tuple[int, ...] = ()
        private_preview = None
        if self.task_smart_private.isChecked():
            private_preview = self._run(
                lambda: self.controller.preview_private_skip(
                    self.task_expression.text(), self.task_private_days.value()
                ),
                self.task_output,
            )
            if private_preview is None:
                return
            matches = private_preview.private_matches
            if matches:
                box = QMessageBox(self)
                box.setWindowTitle("智能跳过预览")
                box.setText("\n".join(self._smart_preview_lines(private_preview)))
                skip_button = box.addButton("按预览跳过", QMessageBox.ButtonRole.AcceptRole)
                force_button = box.addButton("强制包含全部", QMessageBox.ButtonRole.DestructiveRole)
                cancel_button = box.addButton("取消创建", QMessageBox.ButtonRole.RejectRole)
                skip_button.setEnabled(private_preview.effective is not None)
                box.exec()
                if box.clickedButton() is cancel_button:
                    return
                if box.clickedButton() is skip_button:
                    excluded_numbers = private_preview.skipped_numbers
                elif box.clickedButton() is force_button:
                    excluded_numbers = ()
                else:
                    return
        result = self._run(
            lambda: self.controller.create_task(
                self.task_expression.text(),
                rule,
                self.task_persist_master.isChecked(),
                self.task_name.text(),
                activate,
                excluded_numbers,
            ),
            self.task_output,
        )
        if result:
            messages: list[object] = [
                f"任务文件：{result.task_path}",
                f"账号范围：{result.preview.compact}",
            ]
            if private_preview is not None:
                messages.append(
                    "智能跳过预览："
                    + (
                        "、".join(f"A{number}" for number in excluded_numbers)
                        if excluded_numbers
                        else "没有跳过账号或已选择强制包含"
                    )
                )
            if result.backup_path:
                messages.append(f"修改前关键文件备份：{result.backup_path}")
            if activate:
                messages.append(f"已激活：{self.controller.paths.active_settings}")
            self._append_info(self.task_output, *messages)
            if start:
                run = self._run(
                    lambda: self.controller.start_current_download(
                        self.task_pause_console.isChecked(),
                        task_template=result.task_path,
                    ),
                    self.task_output,
                )
                if run:
                    self._mark_download_started(run)
                    self.queue_active = True
                    self.queue_current = run
                    self.queue_run_source = "task"
                    self.queue_pending = []
                    self.queue_summaries_complete = True
                    self.queue_summaries_reliable = True
                    self.queue_cancel_requested = False
                    self._append_info(
                        self.task_output, f"下载引擎已启动，PID={run.process.pid}"
                    )

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
            self._replace_info(self.batch_output, *lines)
            self.refresh_tasks()

    def _selected_task_paths(self) -> list[Path]:
        return [
            Path(item.data(Qt.UserRole))
            for index in range(self.task_list.count())
            if (item := self.task_list.item(index)).checkState() == Qt.Checked
        ]

    def _activate_selected_task(self) -> None:
        if self._queue_state_locked():
            return
        paths = self._selected_task_paths()
        if len(paths) != 1:
            QMessageBox.information(
                self, "请勾选一个任务", "设为正式 settings.json 时必须且只能勾选一个任务。"
            )
            return
        result = self._run(lambda: self.controller.activate_task(paths[0]), self.queue_output)
        if result:
            self._append_info(
                self.queue_output,
                f"已将模板 {paths[0].name} 复制为正式 settings.json。",
                "任务模板仍永久保留，以后可以再次勾选复用。",
            )

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
        if self._queue_state_locked():
            return
        if not self._apply_queue_options():
            return
        if self.queue_shutdown.isChecked() and self._collector_is_enabled():
            QMessageBox.warning(
                self,
                "无法设置完成后关机",
                "检测到采集服务已启用或正在运行。请停止采集服务后再勾选完成后关机。",
            )
            self.queue_shutdown.setChecked(False)
            return
        self.queue_shutdown_requested = self.queue_shutdown.isChecked()
        self.queue_cancel_requested = False
        self.queue_run_source = "current"
        run = self._run(
            lambda: self.controller.start_current_download(
                self.queue_pause_console.isChecked()
            ),
            self.queue_output,
        )
        if run:
            self._mark_download_started(run)
            self.queue_active = True
            self.queue_current = run
            self.queue_pending = []
            self.queue_summaries_complete = True
            self.queue_summaries_reliable = True
            self._append_info(
                self.queue_output, f"启动当前 settings.json，PID={run.process.pid}"
            )
            self.refresh_all()

    def _start_queue(self) -> None:
        if self._queue_state_locked():
            return
        paths = self._selected_task_paths()
        if not paths:
            QMessageBox.information(
                self, "未勾选任务", "请在任务列表中勾选一个或多个任务。"
            )
            return
        if not self._apply_queue_options():
            return
        if self.queue_shutdown.isChecked() and self._collector_is_enabled():
            QMessageBox.warning(
                self,
                "无法设置完成后关机",
                "检测到采集服务已启用或正在运行。请停止采集服务后再勾选完成后关机。",
            )
            self.queue_shutdown.setChecked(False)
            return
        self.queue_shutdown_requested = self.queue_shutdown.isChecked()
        self.queue_run_source = "queue"
        self.queue_paused = False
        self.queue_cancel_requested = False
        self.queue_pause_button.setText("暂停队列")
        self.queue_pending = paths
        self.queue_active = True
        self.queue_started_at = None
        self.current_task_started_at = None
        self.queue_elapsed_label.setText("本次队列耗时：未开始")
        self.task_elapsed_label.setText("当前任务耗时：未开始")
        self.queue_summaries_complete = True
        self.queue_summaries_reliable = True
        order_text = " → ".join(path.name for path in paths)
        self._append_info(
            self.queue_output,
            f"队列开始，共 {len(paths)} 个任务。",
            f"本次执行顺序：{order_text}",
            merge=True,
        )
        self.controller.logger.info(
            "下载队列开始：任务数=%s；执行顺序=%s",
            len(paths),
            order_text,
        )
        self._start_next_queue_item()
        self.refresh_all()

    def _result_view_option_changed(self, source: str, state: int) -> None:
        run = self.queue_current
        if run is None or not self.queue_active:
            return
        expected_source = "task" if self.queue_run_source == "task" else "queue"
        if source != expected_source:
            return
        checked = bool(state)
        try:
            self.controller.set_result_review(run, checked)
        except Exception as exc:
            self.controller.logger.exception("更新结果查看状态失败：%s", exc)
            self._append_info(
                self.queue_output,
                f"【失败】无法动态更新结果查看状态：{exc}",
            )
            return
        if not checked and getattr(run, "result_review_waiting", False):
            if self._close_result_wrapper(run):
                self._append_info(
                    self.queue_output,
                    "已取消本任务结果查看，下载器黑框正在关闭；队列暂停状态保持不变。",
                )

    @staticmethod
    def _completion_marker_code(run) -> int | None:
        marker = getattr(run, "completion_marker", None)
        if marker is None or not Path(marker).is_file():
            return None
        try:
            return int(Path(marker).read_text(encoding="ascii").strip().splitlines()[0])
        except (OSError, UnicodeError, ValueError, IndexError):
            return None

    def _close_result_wrapper(self, run) -> bool:
        marker = getattr(run, "completion_marker", None)
        if marker is None or not run.running:
            return True
        try:
            self.controller.dismiss_result_review(run)
            return True
        except Exception as exc:
            self.controller.logger.exception("关闭结果查看黑框失败：%s", exc)
            self._append_info(
                self.queue_output,
                f"【失败】结果查看黑框无法自动关闭：{exc}",
                "队列已停止，剩余任务不会启动。",
            )
            self._record_task_elapsed(run, "结果查看关闭失败")
            self._record_queue_elapsed("结果查看关闭失败")
            self.queue_pending.clear()
            self.queue_current = None
            self.queue_active = False
            self._release_download_lifecycle()
            return False

    @staticmethod
    def _cleanup_completion_marker(run) -> None:
        for attribute in ("completion_marker", "review_control"):
            path = getattr(run, attribute, None)
            if path is None:
                continue
            try:
                Path(path).unlink(missing_ok=True)
            except OSError:
                pass

    def _finish_run_after_summary(self, run, assessment) -> None:
        self._cleanup_completion_marker(run)
        if self.queue_cancel_requested:
            self._finish_cancelled_queue(run)
            return
        if not assessment.normal_exit:
            self._record_task_elapsed(run, "异常中止")
            self._record_queue_elapsed("异常中止")
            self.queue_pending.clear()
            self.queue_current = None
            self.queue_active = False
            self._release_download_lifecycle()
            return

        summary = getattr(run, "_summary_result", None)
        if summary is not None:
            self.queue_summaries_complete = (
                self.queue_summaries_complete and summary.complete
            )
            self.queue_summaries_reliable = (
                self.queue_summaries_reliable and summary.reliable
            )
        self._record_task_elapsed(run, "已完成")
        messages = self._run(
            lambda: self.controller.run_post_actions("batch"), self.queue_output
        )
        if messages is None:
            self._record_queue_elapsed("后续动作失败")
            self._append_info(
                self.queue_output,
                "【失败】本批后续动作失败；队列已停止，剩余任务不会启动。",
            )
            self.queue_pending.clear()
            self.queue_current = None
            self.queue_active = False
            self._release_download_lifecycle()
            return
        if messages:
            self._append_info(self.queue_output, *messages)
        self.queue_current = None
        self._start_next_queue_item()

    def _shutdown_option_changed(self, state: int) -> None:
        checked = bool(state)
        self.queue_shutdown_requested = checked
        if not checked and self.shutdown_timer is not None and self.shutdown_timer.isActive():
            self._cancel_shutdown()

    def _start_next_queue_item(self) -> None:
        if self.queue_pending and self.queue_paused:
            self._append_info(
                self.queue_output,
                "队列已暂停：当前任务结束后不会启动下一个模板；这不会暂停当前下载进程。",
            )
            return
        if not self.queue_pending:
            messages = self._run(
                lambda: self.controller.run_post_actions("queue"), self.queue_output
            )
            if messages is None:
                self._record_queue_elapsed("后续动作失败")
                self._append_info(
                    self.queue_output,
                    "【失败】队列后续动作失败；队列已停止，请检查上方错误。",
                )
                self.queue_active = False
                self.queue_current = None
                self._release_download_lifecycle()
                return
            if self.queue_summaries_complete and self.queue_summaries_reliable:
                summary_conclusion = "每个任务的账号汇总均完整且可靠。"
            else:
                summary_conclusion = (
                    "至少一个任务的账号汇总不完整或不可靠；请查看上方信息及任务日志。"
                )
            self.controller.logger.info(
                "下载队列执行结束：%s", summary_conclusion
            )
            post_lines = [
                line.strip()
                for message in (messages or [])
                for line in str(message).splitlines()
                if line.strip()
            ]
            if post_lines:
                for line in post_lines:
                    self.controller.logger.info("队列后续动作：%s", line)
            else:
                self.controller.logger.info("队列后续动作：无")
            final_message = f"队列执行结束。{summary_conclusion}"
            self._append_info(self.queue_output, *(messages or []), final_message)
            self._record_queue_elapsed("已完成")
            self.queue_active = False
            self.queue_current = None
            self._release_download_lifecycle()
            self._begin_shutdown_countdown_if_requested()
            return
        path = self.queue_pending.pop(0)
        run = self._run(
            lambda: self.controller.activate_and_start(
                path,
                self.queue_pause_console.isChecked(),
                _queue_continuation=True,
            ),
            self.queue_output,
        )
        if run is None:
            self._record_queue_elapsed("启动任务失败")
            self.queue_active = False
            self.queue_pending.clear()
            self._release_download_lifecycle()
            return
        self.queue_current = run
        self._mark_download_started(run)
        self._append_info(
            self.queue_output,
            f"正在运行：{path.name}；PID={run.process.pid}；剩余={len(self.queue_pending)}"
        )

    def _start_download_summary(self, run, exit_code: int | None, assessment) -> None:
        if self.download_summary_thread is not None:
            return

        ended_at = datetime.now()
        thread = QThread(self)
        worker = ActionWorker(
            lambda: self.controller.summarize_download(run, exit_code, ended_at)
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.done.connect(thread.quit)
        thread.finished.connect(self._finish_download_summary)
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)

        self.download_summary_thread = thread
        self.download_summary_worker = worker
        self.download_summary_run = run
        self.download_summary_exit_code = exit_code
        self.download_summary_assessment = assessment
        thread.start()

    @Slot()
    def _finish_download_summary(self) -> None:
        worker = self.download_summary_worker
        assessment = self.download_summary_assessment
        try:
            if worker is None or assessment is None:
                return
            if worker.error is not None:
                message = str(worker.error)
                self.controller.logger.exception(
                    "账号结果汇总失败：%s", message, exc_info=worker.error
                )
                if self.queue_cancel_requested:
                    self._append_info(
                        self.queue_output,
                        f"取消任务后账号结果汇总未能完成：{message}",
                    )
                    self._finish_cancelled_queue(self.download_summary_run)
                    return
                self._append_info(
                    self.queue_output,
                    f"【失败】账号结果汇总失败：{message}",
                    "队列已停止；剩余任务不会启动。",
                )
                self._record_task_elapsed(self.download_summary_run, "汇总失败")
                self._record_queue_elapsed("汇总失败")
                self._cleanup_completion_marker(self.download_summary_run)
                self.queue_pending.clear()
                self.queue_current = None
                self.queue_active = False
                self._release_download_lifecycle()
                return

            summary = worker.result
            self._append_info(self.queue_output, *format_summary_for_ui(summary))
            run = self.download_summary_run
            if run is None:
                return
            if getattr(run, "completion_marker", None) is not None and run.running:
                if getattr(run, "pause_after_exit", False):
                    run.result_review_waiting = True
                    self._append_info(
                        self.queue_output,
                        "本任务结果已汇总；黑框保留等待人工查看，查看完成后请按任意键继续。",
                    )
                    return
                if not self._close_result_wrapper(run):
                    return
            self._finish_run_after_summary(run, assessment)
        finally:
            self.download_summary_thread = None
            self.download_summary_worker = None
            self.download_summary_run = None
            self.download_summary_exit_code = None
            self.download_summary_assessment = None
            self.refresh_all()
            self.refresh_results()

    def _collector_is_enabled(self) -> bool:
        return bool(self.controller.collector.running or self.controller.collector.health())

    def _toggle_queue_pause(self) -> None:
        if not self.queue_active:
            self._append_info(self.queue_output, "当前没有正在排队的模板任务。")
            return
        if self.controller.health().get("monitor_running"):
            self._append_info(self.queue_output, "后台监听模式下没有可暂停的管理器队列。")
            return
        self.queue_paused = not self.queue_paused
        self.queue_pause_button.setText("恢复队列" if self.queue_paused else "暂停队列")
        self._append_info(
            self.queue_output,
            "队列已暂停：当前下载仍会继续，当前任务结束后等待恢复。"
            if self.queue_paused
            else "队列已恢复，将继续启动剩余模板。",
        )
        if not self.queue_paused and self.queue_current is None:
            self._start_next_queue_item()

    def _cancel_all_downloads(self) -> None:
        if not self.queue_active:
            QMessageBox.information(self, "没有下载任务", "当前没有由管理器运行的批量下载任务。")
            return
        answer = QMessageBox.question(
            self,
            "确认取消全部下载任务",
            "将立即结束当前批量下载，并取消本次队列中所有尚未开始的模板。\n\n"
            "不会删除任务模板、历史日志、settings 或数据库。确定继续吗？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.queue_cancel_requested = True
        self.queue_pending.clear()
        self.queue_paused = False
        self.queue_pause_button.setText("暂停队列")
        self.queue_shutdown.setChecked(False)
        run = self.queue_current
        if run is None:
            self._finish_cancelled_queue(None)
            return
        if run.running:
            result = self._run(
                lambda: self.controller.cancel_current_download(run),
                self.queue_output,
            )
            if result is None:
                return
            self._append_info(
                self.queue_output,
                f"已请求取消当前下载及全部待执行任务；取消记录：{result}",
            )
        if self.download_summary_thread is None and getattr(
            run, "result_review_waiting", False
        ) and not run.running:
            self._finish_run_after_summary(
                run, assess_process_exit(getattr(run, "engine_exit_code", None))
            )

    def _finish_cancelled_queue(self, run) -> None:
        self._record_task_elapsed(run, "已取消")
        self._record_queue_elapsed("已取消")
        if run is not None:
            self._cleanup_completion_marker(run)
        self.queue_pending.clear()
        self.queue_current = None
        self.queue_active = False
        self.queue_paused = False
        self.queue_cancel_requested = False
        self.queue_pause_button.setText("暂停队列")
        self._release_download_lifecycle()
        self._append_info(
            self.queue_output,
            "当前下载和本次队列内全部待执行任务均已取消；模板与历史日志仍保留。",
        )

    def _begin_shutdown_countdown_if_requested(self) -> None:
        # Read the checkbox at finalization time so changes made while a
        # download is running take effect for the current queue.
        self.queue_shutdown_requested = self.queue_shutdown.isChecked()
        if not self.queue_shutdown_requested:
            return
        if not self.queue_summaries_complete or not self.queue_summaries_reliable:
            self._append_info(
                self.queue_output,
                "结果存在不完整或不可靠证据，已取消完成后关机；请先核对任务日志。",
            )
            self.queue_shutdown_requested = False
            return
        if self._collector_is_enabled():
            self._append_info(self.queue_output, "检测到采集服务已启用，已取消完成后关机。")
            self.queue_shutdown_requested = False
            return
        self.shutdown_remaining = 60
        if self.shutdown_timer is None:
            self.shutdown_timer = QTimer(self)
            self.shutdown_timer.timeout.connect(self._shutdown_tick)
        self.shutdown_timer.start(1000)
        self._append_info(
            self.queue_output,
            "所有任务、结果汇总和后续动作均已完成。60 秒后执行 Windows 正常关机；"
            "如需取消，请点击下方取消按钮。",
        )
        self.shutdown_cancel_button = QMessageBox(self)
        self.shutdown_cancel_button.setWindowTitle("完成后关机倒计时")
        self.shutdown_cancel_button.setText("60 秒后正常关机（不强制关闭其他应用）。")
        cancel = self.shutdown_cancel_button.addButton(
            "取消关机", QMessageBox.ButtonRole.RejectRole
        )
        self.shutdown_cancel_button.rejected.connect(self._cancel_shutdown)
        self.shutdown_cancel_button.show()

    def _shutdown_tick(self) -> None:
        self.shutdown_remaining -= 1
        if hasattr(self, "shutdown_cancel_button"):
            self.shutdown_cancel_button.setText(
                f"{max(self.shutdown_remaining, 0)} 秒后正常关机（不强制关闭其他应用）。"
            )
        self.statusBar().showMessage(f"完成后关机倒计时：{max(self.shutdown_remaining, 0)} 秒")
        if self.shutdown_remaining > 0:
            return
        health = self.controller.health(check_processes=True)
        summary_running = self.download_summary_thread is not None and (
            not hasattr(self.download_summary_thread, "isRunning")
            or self.download_summary_thread.isRunning()
        )
        background_running = self.background_thread is not None and (
            not hasattr(self.background_thread, "isRunning")
            or self.background_thread.isRunning()
        )
        if (
            self._collector_is_enabled()
            or health.get("engine_running")
            or health.get("monitor_running")
            or self.queue_active
            or summary_running
            or background_running
        ):
            self._cancel_shutdown()
            self._append_info(
                self.queue_output,
                "检测到新的采集、下载、监听、队列、结果汇总或后台工作，已取消完成后关机。",
            )
            return
        if self.shutdown_timer is not None:
            self.shutdown_timer.stop()
        if hasattr(self, "shutdown_cancel_button"):
            self.shutdown_cancel_button.close()
        self._run(
            request_normal_shutdown,
            self.queue_output,
            cancel_shutdown=False,
        )

    def _cancel_shutdown_for_new_work(self) -> None:
        timer_active = self.shutdown_timer is not None and self.shutdown_timer.isActive()
        if not timer_active:
            return
        self._cancel_shutdown()
        self._append_info(
            self.queue_output,
            "检测到新的工作操作，已取消完成后关机。",
        )

    def _cancel_shutdown(self) -> None:
        if self.shutdown_timer is not None:
            self.shutdown_timer.stop()
        self.queue_shutdown_requested = False
        self.statusBar().showMessage("已取消完成后关机")
        self._append_info(self.queue_output, "已取消完成后关机，电脑保持开机。")

    def _start_monitor(self) -> None:
        if self._queue_state_locked():
            return
        run = self._run(self.controller.start_monitor, self.queue_output)
        if run:
            self._append_info(
                self.queue_output,
                f"后台监听已启动，PID={run.process.pid}；复制账号 URL 到剪贴板即可由下载引擎接收。",
                "管理器已将 run_command 设置为 6；停止监听并确认进程退出后会恢复为 5 1 1 Q。",
            )
            self.refresh_all(check_processes=True)

    def _stop_monitor(self) -> None:
        if self.queue_active:
            QMessageBox.information(self, "队列运行中", "请等待下载队列结束后再停止后台监听。")
            return
        result = self._run(self.controller.stop_monitor, self.queue_output)
        if result is not None:
            self._append_info(
                self.queue_output,
                "后台监听已正常停止。",
                f"正式 settings.json 的 run_command 已恢复为 5 1 1 Q：{result}",
            )

    def _release_download_lifecycle(self) -> None:
        release = getattr(self.controller, "release_download_lifecycle", None)
        if release is not None:
            release()

    def _poll_processes(self) -> None:
        self._update_elapsed_labels()
        monitor = getattr(getattr(self.controller, "engine", None), "current", None)
        if (
            monitor is not None
            and getattr(monitor, "mode", "") == ENGINE_MODE_MONITOR
            and not monitor.running
        ):
            try:
                self.controller.stop_monitor()
                self._append_info(
                    self.queue_output,
                    "检测到后台监听窗口已退出，已自动恢复 run_command=5 1 1 Q。",
                )
            except Exception as exc:
                self.controller.logger.exception("后台监听退出后的自动恢复失败：%s", exc)
                self._append_info(self.queue_output, f"【失败】后台监听退出后恢复设置失败：{exc}")
            self.refresh_all(check_processes=True)
            return
        if not self.queue_active or self.queue_current is None:
            return
        if self.download_summary_thread is not None:
            return
        run = self.queue_current
        if not getattr(run, "interruption_detected", False):
            try:
                if self.controller.engine.detect_interruption(run):
                    run.interruption_detected = True
                    message = (
                        "检测到下载引擎日志记录了用户主动中止；"
                        "等待进程退出后仍会执行最终账号结果汇总。"
                    )
                    self._append_info(self.queue_output, message)
                    self.controller.logger.info(
                        "检测到下载引擎主动中止：模板=%s；PID=%s",
                        run.task_template,
                        run.process.pid,
                    )
            except Exception as exc:
                self.controller.logger.exception("检测下载引擎中止日志失败：%s", exc)
        if getattr(run, "result_review_waiting", False):
            if not run.running:
                run.result_review_waiting = False
                self._finish_run_after_summary(
                    run,
                    assess_process_exit(getattr(run, "engine_exit_code", None)),
                )
            return
        marker_code = self._completion_marker_code(run)
        if marker_code is not None and getattr(run, "engine_exit_code", None) is None:
            run.engine_exit_code = marker_code
            assessment = assess_process_exit(marker_code)
            self._append_info(
                self.queue_output,
                assessment.headline,
                assessment.detail,
                "正在汇总账号结果，请等待。",
                merge=True,
            )
            self.controller.logger.info(
                "下载器主进程已退出，黑框包装器仍在等待：模板=%s；PID=%s；退出码=%s",
                run.task_template,
                run.process.pid,
                marker_code,
            )
            self._start_download_summary(run, marker_code, assessment)
            return
        if run.running:
            return
        code = getattr(run, "engine_exit_code", None)
        if code is None:
            code = run.process.returncode
        assessment = assess_process_exit(code)
        self._append_info(
            self.queue_output,
            assessment.headline,
            assessment.detail,
            "正在汇总账号结果，请等待。",
            merge=True,
        )
        self.controller.logger.info(
            "下载进程已退出：模板=%s；已选账号=%s；PID=%s；"
            "退出码=%s；状态=%s",
            run.task_template,
            run.selected_accounts,
            run.process.pid,
            code,
            assessment.log_status,
        )
        self._start_download_summary(run, code, assessment)

    def _manual_backup(self) -> None:
        answer = QMessageBox.question(
            self,
            "确认完整备份 Volume",
            "此操作会复制整个正式 Volume，可能占用数 GB 空间。\n\n"
            "日常自动备份已经保存 3 个关键文件，无需频繁执行完整备份。\n\n"
            "仍要继续吗？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        result = self._run(self.controller.backup_now, self.overview_output)
        if result:
            self._append_info(self.overview_output, f"完整 Volume 备份完成：{result}")

    def _migrate_collector(self) -> None:
        result = self._run(self.controller.migrate_collector, self.collector_output)
        if result:
            self._replace_info(
                self.collector_output,
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

    def _start_collector(self) -> None:
        result = self._run(self.controller.start_collector, self.collector_output)
        if result:
            self._append_info(
                self.collector_output,
                "账号采集服务已启动，并已通过 "
                f"http://127.0.0.1:{self.controller.config.collector_port}/health 验证。",
                f"采集服务日志：{result}",
            )

    def _stop_collector(self) -> None:
        self.controller.stop_collector()
        self._append_info(self.collector_output, "账号采集服务已停止。")
        self.refresh_all()

    def _export_userscript(self) -> None:
        result = self._run(self.controller.collector.export_userscript, self.collector_output)
        if result:
            self._append_info(self.collector_output, f"油猴脚本已导出：{result}")
            self._open_path(result.parent)

    def _preview_screenshots(self) -> None:
        result = self._run(self.controller.screenshot_preview, self.post_output)
        if result:
            self._replace_info(
                self.post_output,
                f"识别账号文件夹：{result.recognized_folders}",
                f"识别截图：{result.recognized_images}",
                f"可以安全归档：{result.movable}",
                f"找不到账号文件夹：{result.missing_account_folder}",
                f"目标已有同名文件：{result.already_existing}",
                f"忽略非规范账号文件夹：{result.unmatched_folders}",
            )

    def _organize_screenshots(self) -> None:
        result = self._run(self.controller.organize_screenshots, self.post_output)
        if result:
            self._append_info(self.post_output, f"完成：安全归档 {result.moved} 张。")

    def _refresh_index(self) -> None:
        def show_result(result: object) -> None:
            if result is not None:
                messages = [result.display_summary("索引刷新")]
                if result.output:
                    messages.extend(("详细输出：", result.output))
                self._replace_info(self.post_output, *messages)

        self._run_index_background(
            self.controller.refresh_index,
            started_message="正在后台刷新索引，界面可以继续使用……",
            success=show_result,
        )

    def _cleanup_index(self) -> None:
        def show_result(result: object) -> None:
            if result is not None:
                messages = [result.display_summary("失效快捷方式清理")]
                if result.output:
                    messages.extend(("详细输出：", result.output))
                self._replace_info(self.post_output, *messages)

        self._run_index_background(
            self.controller.cleanup_index,
            started_message="正在后台重新扫描并清理受管快捷方式，界面可以继续使用……",
            success=show_result,
        )

    def _cleanup_index_self_test(self) -> None:
        def show_result(result: object) -> None:
            if result is not None:
                self._replace_info(
                    self.post_output,
                    "清理功能隔离自检通过。正式视频目录和索引目录均未参与测试。",
                    result.output,
                )

        self._run_index_background(
            self.controller.cleanup_index_self_test,
            started_message=(
                "正在 Windows 临时目录测试清理功能；不会读取或修改正式目录……"
            ),
            success=show_result,
        )

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
        if result is not None:
            self._replace_info(self.settings_output, result or "设置已保存，正在重新验证正式数据。")
            self.queue_screenshot_mode.setCurrentIndex(self.setting_screenshot_mode.currentIndex())
            self.queue_index_mode.setCurrentIndex(self.setting_index_mode.currentIndex())
            self.queue_cleanup.setChecked(self.setting_cleanup.isChecked())
            self.startup_recheck_timer.start(0)

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
            self._replace_info(
                self.settings_output,
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
            self._replace_info(
                self.settings_output,
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

    def _browse_dir(self, edit: QLineEdit) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择文件夹", edit.text())
        if selected:
            edit.setText(selected)

    @staticmethod
    def _open_path(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def closeEvent(self, event) -> None:  # noqa: N802
        if self.queue_current is not None or self.download_summary_thread is not None:
            QMessageBox.information(
                self,
                "下载任务尚未汇总完成",
                "请等待当前下载任务结束并完成账号结果汇总后再关闭管理器。",
            )
            event.ignore()
            return
        current_engine = getattr(getattr(self.controller, "engine", None), "current", None)
        if current_engine is not None and getattr(current_engine, "mode", "") == ENGINE_MODE_MONITOR and current_engine.running:
            QMessageBox.information(
                self,
                "后台监听仍在运行",
                "请先点击“停止后台监听”，确认下载引擎退出并恢复批量下载命令后再关闭管理器。",
            )
            event.ignore()
            return
        if self.background_thread is not None and self.background_thread.isRunning():
            QMessageBox.information(self, "索引任务运行中", "请等待索引任务完成后再关闭管理器。")
            event.ignore()
            return
        if self.coordinator.has_active_tasks():
            self._close_pending = True
            self.controller.begin_closing()
            self.coordinator.begin_closing()
            self._apply_action_gate()
            QMessageBox.information(
                self,
                "启动安全检查正在结束",
                "已请求取消启动安全检查；后台线程安全退出后管理器将自动关闭。",
            )
            event.ignore()
            return
        begin_closing = getattr(self.controller, "begin_closing", None)
        if callable(begin_closing):
            self.coordinator.begin_closing()
            begin_closing()
            self._apply_action_gate()
        try:
            collector = getattr(self.controller, "collector", None)
            if collector is None or getattr(collector, "process", None) is not None:
                self.controller.stop_collector()
        except Exception as exc:
            logger = getattr(self.controller, "logger", None)
            if logger is not None and hasattr(logger, "exception"):
                logger.exception("关闭前停止账号采集服务失败：%s", exc)
            QMessageBox.critical(
                self,
                "账号采集服务停止失败",
                f"管理器仍保持打开，未遗留关闭状态不明的采集进程。\n\n{exc}",
            )
            event.ignore()
            return
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
            QListWidget::indicator { width: 17px; height: 17px; border: 1px solid #64748b;
                                     border-radius: 3px; background: white; }
            QListWidget::indicator:checked { background: #2563eb; border-color: #1d4ed8; }
            QTabWidget::pane { border: 1px solid #cbd5e1; background: #f8fafc; }
            QTabBar::tab { padding: 9px 15px; background: #e2e8f0; }
            QTabBar::tab:selected { background: white; color: #1d4ed8; font-weight: 600; }
            QLabel[ok="true"] { color: #15803d; font-weight: 600; }
            QLabel[ok="false"] { color: #b91c1c; font-weight: 600; }
            """
        )
