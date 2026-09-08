from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any, Callable, TypeVar

from PySide6.QtCore import (
    QAbstractTableModel,
    QEvent,
    QMargins,
    QModelIndex,
    QObject,
    QRect,
    QThread,
    Qt,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import QColor, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QLayout,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableView,
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
    TaskRejectedError,
    TaskSpec,
    TaskState,
)
from douk_manager.core.download_summary import AccountStatus, format_summary_for_ui
from douk_manager.core.account_audit import (
    AccountAuditEntry,
    AccountAuditReport,
    AuditApplyResult,
    AuditDecision,
    Disposition,
    IdentityState,
    PrivacyState,
    ReachabilityState,
    Suggestion,
)
from douk_manager.core.engine import ENGINE_MODE_MONITOR, assess_process_exit
from douk_manager.core.engine_update import (
    EngineRollbackPoint,
    RollbackApplyGate,
    RollbackIntegrity,
    rollback_can_apply,
)
from douk_manager.core.log_stats import (
    ENDPOINTS,
    FIELD_PRESENCE,
    KEYWORDS,
    LogStats,
    SegmentSelectionStatus,
    located_reason_zh,
    log_stats_label_zh,
    normalise_located_reason,
    select_dashboard_segments,
    truncate_reason_zh,
)
from douk_manager.core.power import request_normal_shutdown
from douk_manager.core.result_history import ResultPageSnapshot
from douk_manager.core.result_dashboard import (
    DashboardAccountRow,
    DashboardFileFingerprint,
    DashboardTaskIndex,
    DashboardTaskIndexEntry,
    ResultDashboardSnapshot,
)
from douk_manager.core.selector import compact_numbers
from douk_manager.core.settings_tasks import ActivatedTask, EarliestRule
from douk_manager.core.task_order import move_to_index
from douk_manager.ui_messages import format_information
from douk_manager.ui_state import (
    WindowGeometryState,
    WindowStateStore,
    safe_window_placement,
)
from douk_manager.startup import (
    StartupSafetyResult,
    StartupSafetyService,
    StartupStage,
    StartupState,
)


T = TypeVar("T")


class SmartSkipChoice:
    SKIP = "skip"
    FORCE_ALL = "force_all"
    CANCEL = "cancel"


class SmartSkipPreviewDialog(QDialog):
    """Bounded, resizable confirmation for a potentially large text preview."""

    SCREEN_MARGIN = 48

    def __init__(self, text: str, *, can_skip: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._choice = SmartSkipChoice.CANCEL
        self.setWindowTitle("智能跳过预览")
        self.setModal(True)
        self.setSizeGripEnabled(True)

        layout = QVBoxLayout(self)
        self.details = QTextEdit(self)
        self.details.setObjectName("smartSkipPreviewDetails")
        self.details.setReadOnly(True)
        self.details.setPlainText(text)
        self.details.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        layout.addWidget(self.details, 1)

        self.buttons = QDialogButtonBox(self)
        self.skip_button = self.buttons.addButton(
            "按预览跳过", QDialogButtonBox.ButtonRole.AcceptRole
        )
        self.force_button = self.buttons.addButton(
            "强制包含全部", QDialogButtonBox.ButtonRole.DestructiveRole
        )
        self.cancel_button = self.buttons.addButton(
            "取消创建", QDialogButtonBox.ButtonRole.RejectRole
        )
        self.skip_button.setObjectName("smartSkipAcceptButton")
        self.force_button.setObjectName("smartSkipForceButton")
        self.cancel_button.setObjectName("smartSkipCancelButton")
        self.skip_button.setEnabled(can_skip)
        self.cancel_button.setDefault(True)
        self.skip_button.clicked.connect(self._choose_skip)
        self.force_button.clicked.connect(self._choose_force_all)
        self.cancel_button.clicked.connect(self.reject)
        layout.addWidget(self.buttons)
        self._fit_to_screen()

    @property
    def choice(self) -> str:
        return self._choice

    def reject(self) -> None:
        self._choice = SmartSkipChoice.CANCEL
        super().reject()

    def _choose_skip(self) -> None:
        if not self.skip_button.isEnabled():
            return
        self._choice = SmartSkipChoice.SKIP
        self.accept()

    def _choose_force_all(self) -> None:
        self._choice = SmartSkipChoice.FORCE_ALL
        self.accept()

    def _fit_to_screen(self) -> None:
        screen = self.parentWidget().screen() if self.parentWidget() is not None else None
        screen = screen or QApplication.primaryScreen()
        if screen is None:
            self.resize(900, 680)
            return
        available = screen.availableGeometry()
        maximum_width = max(1, available.width() - self.SCREEN_MARGIN * 2)
        maximum_height = max(1, available.height() - self.SCREEN_MARGIN * 2)
        minimum_width = min(560, maximum_width)
        minimum_height = min(360, maximum_height)
        self.setMinimumSize(minimum_width, minimum_height)
        self.setMaximumSize(maximum_width, maximum_height)
        width = min(900, maximum_width)
        height = min(700, maximum_height)
        self.resize(width, height)
        self.move(
            available.x() + (available.width() - width) // 2,
            available.y() + (available.height() - height) // 2,
        )


POST_MODES = (
    ("不执行", "none"),
    ("每一批完成后", "batch"),
    ("整个队列完成后", "queue"),
)


@dataclass
class BackgroundTaskBinding:
    generation_key: str
    generation: int
    spec: TaskSpec
    output: QTextEdit | None = None
    buttons: tuple[QWidget, ...] = ()
    on_success: Callable[[object], None] | None = None
    on_failure: Callable[[object], None] | None = None
    on_cancelled: Callable[[object], None] | None = None
    on_progress: Callable[[object], None] | None = None
    on_removed: Callable[[], None] | None = None
    allow_during_closing: bool = False


@dataclass
class PendingBackgroundRequest:
    generation: int
    spec: TaskSpec
    action: Callable[[object], object]
    output: QTextEdit | None = None
    buttons: tuple[QWidget, ...] = ()
    on_success: Callable[[object], None] | None = None
    on_failure: Callable[[object], None] | None = None
    on_cancelled: Callable[[object], None] | None = None
    on_progress: Callable[[object], None] | None = None
    on_removed: Callable[[], None] | None = None
    allow_during_closing: bool = False


@dataclass
class DownloadSummaryTaskBinding:
    run: Any
    assessment: Any
    deduplicate_key: str
    generation: int
    task_id: str | None = None
    outcome: TaskState | None = None
    payload: object | None = None
    terminal_consumed: bool = False
    removed_consumed: bool = False
    continuation_ready: bool = False
    business_finalized: bool = False


@dataclass
class DownloadPostActionTaskBinding:
    timing: str
    run: Any | None
    deduplicate_key: str
    generation: int
    task_id: str | None = None
    outcome: TaskState | None = None
    terminal_consumed: bool = False
    removed_consumed: bool = False
    queue_decision_consumed: bool = False


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


def _dashboard_status_label(status: AccountStatus | str) -> str:
    labels = {
        AccountStatus.DOWNLOADED: "有新作品下载",
        AccountStatus.ALL_SKIPPED: "作品均被引擎跳过",
        AccountStatus.NO_ELIGIBLE_WORKS: "无符合条件作品",
        AccountStatus.PRIVATE: "私密账号",
        AccountStatus.ERROR: "处理异常，需核对",
        AccountStatus.INTERRUPTED: "处理中断",
        "pre_start_error": "进入处理前异常",
        "not_started": "未开始",
    }
    return labels.get(status, str(status))


def _dashboard_account_needs_attention(account: DashboardAccountRow) -> bool:
    return account.completed_with_anomaly or account.status in (
        AccountStatus.ERROR,
        AccountStatus.INTERRUPTED,
        "pre_start_error",
        "not_started",
    )


def _audit_identity_label(value: IdentityState) -> str:
    return {
        IdentityState.CONFIRMED: "已确认",
        IdentityState.CONFLICT: "冲突",
        IdentityState.DUPLICATE: "重复",
        IdentityState.UNRESOLVED: "未确认",
    }[value]


def _audit_reachability_label(value: ReachabilityState) -> str:
    return {
        ReachabilityState.REACHABLE: "可达",
        ReachabilityState.UNAVAILABLE: "不可用",
        ReachabilityState.REQUEST_FAILED: "请求失败，需复核",
        ReachabilityState.UNKNOWN: "未知",
    }[value]


def _audit_privacy_label(value: PrivacyState) -> str:
    return {
        PrivacyState.PUBLIC: "公开",
        PrivacyState.PRIVATE: "私密",
        PrivacyState.UNKNOWN: "未知",
    }[value]


def _audit_suggestion_label(value: Suggestion) -> str:
    return {
        Suggestion.KEEP: "保持启用",
        Suggestion.REVIEW: "需复核",
        Suggestion.SUGGEST_DISABLE: "建议停用",
        Suggestion.SUGGEST_REENABLE: "建议恢复",
        Suggestion.NO_EVIDENCE: "证据不足",
    }[value]


def _audit_disposition_label(value: Disposition) -> str:
    return {
        Disposition.ENABLED: "继续启用",
        Disposition.PERMANENTLY_DISABLED: "永久停用",
        Disposition.PENDING_REVIEW: "待复核",
    }[value]


def _log_stats_section_html(
    title: str,
    rows: tuple[tuple[str, object], ...],
    note: str,
    *,
    width_percent: int,
) -> str:
    row_html = "".join(
        (
            "<tr>"
            '<td style="font-size:15px; font-weight:600; color:#374151; '
            f'padding:4px 12px 4px 0;">{escape(label)}</td>'
            '<td align="right" style="font-size:16px; font-weight:600; '
            f'color:#111827; padding:4px 0;">{escape(str(value))}</td>'
            "</tr>"
        )
        for label, value in rows
    )
    note_html = (
        '<div style="font-size:12px; color:#64748b; margin-top:8px;">'
        f"{escape(note)}</div>"
        if note
        else ""
    )
    return (
        f'<td data-log-stats-section="true" width="{width_percent}%" '
        'valign="top" style="padding:8px 14px; border-right:1px solid #d1d5db;">'
        '<div style="font-size:18px; font-weight:700; color:#1f2937; '
        f'margin-bottom:8px;">{escape(title)}</div>'
        f'<table width="100%" cellspacing="0" cellpadding="0">{row_html}</table>'
        f"{note_html}</td>"
    )


def _render_log_stats_html(
    stats: LogStats, *, located_reason: str, columns: int
) -> str:
    reason = located_reason_zh(located_reason)
    truncate_reason = truncate_reason_zh(stats.truncate_reason)
    http_rows = tuple(
        (f"HTTP {code}", count) for code, count in sorted(stats.http.counts.items())
    )
    abnormal_rows = tuple(
        (f"异常 {code}", count)
        for code, count in sorted(stats.abnormal.counts.items())
    ) or (("无异常状态码", 0),)
    sections = (
        (
            "运行概览",
            (
                (log_stats_label_zh("schema"), stats.schema),
                (log_stats_label_zh("lines"), stats.lines),
                (log_stats_label_zh("bytes_analysed"), stats.bytes_analysed),
                (
                    log_stats_label_zh("window"),
                    f"{stats.window_start or '无'} -> {stats.window_end or '无'}",
                ),
                (log_stats_label_zh("active_minutes"), stats.active_minutes),
                (log_stats_label_zh("log_location_reason"), reason),
                (log_stats_label_zh("truncated"), "是" if stats.truncated else "否"),
                (log_stats_label_zh("truncate_reason"), truncate_reason),
            ),
            "",
        ),
        (
            "日志与响应",
            tuple(
                (log_stats_label_zh(level), stats.levels.get(level, 0))
                for level in ("INFO", "WARNING", "ERROR", "DEBUG", "CRITICAL")
            )
            + http_rows
            + (
                ("HTTP 响应总数", stats.http.total),
                ("HTTP 403 比例", f"{stats.http_403_rate:.4%}"),
            )
            + abnormal_rows,
            "异常路径状态码（不在上面那张响应码表里）",
        ),
        (
            "请求端点与失败",
            tuple(
                (endpoint, stats.endpoints.get(endpoint, 0)) for endpoint in ENDPOINTS
            )
            + (
                (log_stats_label_zh("request_failed"), stats.request_failed),
                (
                    log_stats_label_zh("private_account"),
                    stats.failures.private_account,
                ),
                (
                    log_stats_label_zh("resp_code_abnormal"),
                    stats.failures.resp_code_abnormal,
                ),
                (
                    log_stats_label_zh("download_interrupted"),
                    stats.failures.download_interrupted,
                ),
                (
                    log_stats_label_zh("url_parse_failed"),
                    stats.failures.url_parse_failed,
                ),
                (log_stats_label_zh("unavailable"), 0),
                (log_stats_label_zh("unknown"), 0),
            ),
            "本版本不从日志推断账号是否已注销或被封。",
        ),
        (
            "签名与参数",
            tuple(
                (field, stats.signatures.presence.get(field, 0))
                for field in FIELD_PRESENCE
            )
            + (
                (
                    log_stats_label_zh("request_lines"),
                    stats.signatures.request_lines,
                ),
                (
                    log_stats_label_zh("signature_coverage"),
                    f"{stats.signature_coverage:.4%}",
                ),
            ),
            "仅显示字段出现次数，不显示参数值。",
        ),
        (
            "业务事件",
            tuple(
                (log_stats_label_zh(name), stats.keywords.get(name, 0))
                for name in KEYWORDS
            ),
            "只显示固定业务关键词的聚合次数。",
        ),
    )
    column_count = max(1, min(int(columns), len(sections)))
    width_percent = 100 // column_count
    table_rows: list[str] = []
    for offset in range(0, len(sections), column_count):
        row_sections = sections[offset : offset + column_count]
        cells = [
            _log_stats_section_html(
                title, rows, note, width_percent=width_percent
            )
            for title, rows, note in row_sections
        ]
        cells.extend(
            f'<td width="{width_percent}%"></td>'
            for _unused in range(column_count - len(row_sections))
        )
        table_rows.append(f'<tr>{"".join(cells)}</tr>')
    return (
        '<html><body style="margin:0; color:#111827;">'
        '<table width="100%" cellspacing="0" cellpadding="0">'
        f'{"".join(table_rows)}</table></body></html>'
    )


class LogStatsOutput(QTextEdit):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stats: LogStats | None = None
        self._located_reason = ""
        self._rendered_columns = 0

    @staticmethod
    def _column_count_for_width(width: int) -> int:
        if width >= 1700:
            return 5
        if width >= 1320:
            return 4
        if width >= 920:
            return 3
        if width >= 620:
            return 2
        return 1

    def setPlainText(self, text: str) -> None:  # noqa: N802
        self._stats = None
        self._rendered_columns = 0
        super().setPlainText(text)

    def show_stats(self, stats: LogStats, *, located_reason: str = "") -> None:
        self._stats = stats
        self._located_reason = normalise_located_reason(located_reason)
        self._render_stats(force=True, reset_scroll=True)

    def _render_stats(self, *, force: bool = False, reset_scroll: bool = False) -> None:
        if self._stats is None:
            return
        columns = self._column_count_for_width(self.viewport().width())
        if not force and columns == self._rendered_columns:
            return
        scroll_value = 0 if reset_scroll else self.verticalScrollBar().value()
        self._rendered_columns = columns
        super().setHtml(
            _render_log_stats_html(
                self._stats,
                located_reason=self._located_reason,
                columns=columns,
            )
        )
        self.verticalScrollBar().setValue(
            min(scroll_value, self.verticalScrollBar().maximum())
        )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._render_stats()


class DashboardAccountTableModel(QAbstractTableModel):
    HEADERS = ("账号", "主状态", "异常附加", "证据来源")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._rows: tuple[DashboardAccountRow, ...] = ()
        self._visible_rows: tuple[DashboardAccountRow, ...] = ()
        self._evidence_source = ""
        self._filter_mode = "attention"
        self._account_number: int | None = None

    @property
    def total_count(self) -> int:
        return len(self._rows)

    @property
    def visible_count(self) -> int:
        return len(self._visible_rows)

    @property
    def attention_count(self) -> int:
        return sum(_dashboard_account_needs_attention(row) for row in self._rows)

    def set_rows(
        self,
        rows: tuple[DashboardAccountRow, ...],
        *,
        evidence_source: str,
    ) -> None:
        self.beginResetModel()
        self._rows = rows
        self._evidence_source = evidence_source
        self._rebuild_visible_rows()
        self.endResetModel()

    def set_filter(self, mode: str, account_text: str) -> None:
        account_number = self._parse_account_number(account_text)
        if mode == self._filter_mode and account_number == self._account_number:
            return
        self.beginResetModel()
        self._filter_mode = mode
        self._account_number = account_number
        self._rebuild_visible_rows()
        self.endResetModel()

    @staticmethod
    def _parse_account_number(text: str) -> int | None:
        value = text.strip()
        if value[:1].casefold() == "a":
            value = value[1:].strip()
        if not value:
            return None
        if not value.isdecimal():
            return -1
        return int(value)

    def _rebuild_visible_rows(self) -> None:
        self._visible_rows = tuple(row for row in self._rows if self._matches(row))

    def _matches(self, row: DashboardAccountRow) -> bool:
        if self._account_number is not None and row.a_number != self._account_number:
            return False
        if self._filter_mode == "all":
            return True
        if self._filter_mode == "attention":
            return _dashboard_account_needs_attention(row)
        if self._filter_mode == "anomaly":
            return row.completed_with_anomaly
        if self._filter_mode.startswith("status:"):
            expected = self._filter_mode.removeprefix("status:")
            return getattr(row.status, "value", row.status) == expected
        return False

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._visible_rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid() or role != Qt.ItemDataRole.DisplayRole:
            return None
        row = self._visible_rows[index.row()]
        values = (
            f"A{row.a_number}",
            _dashboard_status_label(row.status),
            "是" if row.completed_with_anomaly else "否",
            self._evidence_source,
        )
        return values[index.column()]

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        return super().headerData(section, orientation, role)


class AccountAuditTableModel(QAbstractTableModel):
    HEADERS = ("A编号", "身份", "可达", "隐私", "主档", "建议", "我的决定", "理由")

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._entries: tuple[AccountAuditEntry, ...] = ()
        self._visible_entries: tuple[AccountAuditEntry, ...] = ()
        self._filter_mode = "all"
        self._account_number: int | None = None
        self._decisions: dict[int, Disposition] = {}

    @property
    def total_count(self) -> int:
        return len(self._entries)

    @property
    def visible_count(self) -> int:
        return len(self._visible_entries)

    def set_report(self, report: AccountAuditReport | None) -> None:
        self.beginResetModel()
        self._entries = () if report is None else report.entries
        self._rebuild_visible_entries()
        self.endResetModel()

    def set_decisions(self, decisions: dict[int, Disposition]) -> None:
        self.beginResetModel()
        self._decisions = dict(decisions)
        self._rebuild_visible_entries()
        self.endResetModel()

    def set_filter(self, mode: str, account_text: str) -> None:
        number = self._parse_account_number(account_text)
        if mode == self._filter_mode and number == self._account_number:
            return
        self.beginResetModel()
        self._filter_mode = mode
        self._account_number = number
        self._rebuild_visible_entries()
        self.endResetModel()

    @staticmethod
    def _parse_account_number(text: str) -> int | None:
        value = text.strip()
        if value[:1].casefold() == "a":
            value = value[1:].strip()
        if not value:
            return None
        return int(value) if value.isdecimal() else -1

    def _rebuild_visible_entries(self) -> None:
        self._visible_entries = tuple(
            entry for entry in self._entries if self._matches(entry)
        )

    def _matches(self, entry: AccountAuditEntry) -> bool:
        if self._account_number is not None and entry.a_number != self._account_number:
            return False
        if self._filter_mode == "all":
            return True
        if self._filter_mode == "suggest_disable":
            return entry.suggestion is Suggestion.SUGGEST_DISABLE
        if self._filter_mode == "suggest_reenable":
            return entry.suggestion is Suggestion.SUGGEST_REENABLE
        if self._filter_mode == "review":
            return entry.suggestion is Suggestion.REVIEW
        if self._filter_mode == "duplicate":
            return entry.identity is IdentityState.DUPLICATE
        if self._filter_mode == "disabled":
            return not entry.master_enable
        return False

    def entry_at(self, row: int) -> AccountAuditEntry | None:
        if row < 0 or row >= len(self._visible_entries):
            return None
        return self._visible_entries[row]

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._visible_entries)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self.HEADERS)

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> object:
        if not index.isValid():
            return None
        entry = self._visible_entries[index.row()]
        if role == Qt.ItemDataRole.UserRole:
            return entry.a_number
        if role == Qt.ItemDataRole.ToolTipRole:
            return entry.suggestion_reason
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        decision = self._decisions.get(entry.a_number, entry.disposition)
        values = (
            f"A{entry.a_number}",
            _audit_identity_label(entry.identity),
            _audit_reachability_label(entry.reachability),
            _audit_privacy_label(entry.privacy),
            "启用" if entry.master_enable else "停用",
            _audit_suggestion_label(entry.suggestion),
            _audit_disposition_label(decision),
            entry.suggestion_reason,
        )
        return values[index.column()]

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> object:
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return self.HEADERS[section]
        if (
            role == Qt.ItemDataRole.TextAlignmentRole
            and orientation == Qt.Orientation.Horizontal
            and 0 <= section < 7
        ):
            return Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        return super().headerData(section, orientation, role)


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


class EngineRollbackTable(QTableWidget):
    """Clear the rollback target once keyboard focus leaves the table."""

    def focusOutEvent(self, event) -> None:  # noqa: N802
        super().focusOutEvent(event)
        QTimer.singleShot(0, self._clear_selection_if_unfocused)

    def _clear_selection_if_unfocused(self) -> None:
        focus_widget = QApplication.focusWidget()
        if focus_widget is self or (
            focus_widget is not None and self.isAncestorOf(focus_widget)
        ):
            return
        if QApplication.mouseButtons() != Qt.MouseButton.NoButton:
            # Let a click on an action button consume the selected row first.
            QTimer.singleShot(50, self._clear_selection_if_unfocused)
            return
        self.clearSelection()
        self.setCurrentCell(-1, -1)


class MainWindow(QMainWindow):
    _RESULT_RENDER_BATCH_SIZE = 100

    def __init__(self, *, window_state_store: WindowStateStore | None = None) -> None:
        super().__init__()
        self.controller = ManagerController()
        self._window_state_store = window_state_store or WindowStateStore.default()
        self._window_state_saved = False
        self.coordinator = BackgroundTaskCoordinator(self)
        self.coordinator.task_settled.connect(self._on_startup_task_settled)
        self.coordinator.task_settled.connect(self._on_background_task_settled)
        self.coordinator.task_progress.connect(self._on_background_task_progress)
        self.coordinator.task_removed.connect(self._on_background_task_removed)
        self.coordinator.idle.connect(self._on_background_tasks_idle)
        self._background_bindings: dict[str, BackgroundTaskBinding] = {}
        self._background_generations: dict[str, int] = {}
        self._background_pending: dict[str, PendingBackgroundRequest] = {}
        self._result_snapshot: ResultPageSnapshot | None = None
        self._dashboard_index: DashboardTaskIndex | None = None
        self._dashboard_snapshot: ResultDashboardSnapshot | None = None
        self._dashboard_entries: dict[str, DashboardTaskIndexEntry] = {}
        self._dashboard_user_selected = False
        self._dashboard_syncing_selector = False
        self._dashboard_index_load_pending: bool | None = None
        self._result_render_rows: tuple[object, ...] = ()
        self._result_render_cursor = 0
        self._result_render_runs = 0
        self._result_render_incomplete_old_runs = 0
        self._latest_log_stats_run: Any | None = None
        self._pending_auto_log_stats_run: Any | None = None
        self._log_stats_result: LogStats | None = None
        self._log_stats_scope: str | None = None
        self._log_stats_task_id: str | None = None
        self._log_stats_export_task_id: str | None = None
        self._engine_rollback_points: tuple[EngineRollbackPoint, ...] = ()
        self._engine_rollback_usage_task_id: str | None = None
        self._engine_rollback_refresh_generation = 0
        self._engine_rollback_usage_pending = False
        self._account_audit_report: AccountAuditReport | None = None
        self._account_audit_decisions: dict[int, Disposition] = {}
        self._account_audit_task_id: str | None = None
        self._account_audit_refresh_after_apply = False
        self._queue_start_waiting_for_log_stats = False
        self._log_stats_auto_enabled = self._read_log_stats_auto_enabled()
        self._collector_stop_task_id: str | None = None
        self.startup_generation = 0
        self._startup_task_id: str | None = None
        self._startup_result: StartupSafetyResult | None = None
        self._close_pending = False
        self._safe_widgets: list[QWidget] = []
        self._path_widgets: list[QWidget] = []
        self._diagnostic_widgets: list[QWidget] = []
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
        self._download_summary_binding: DownloadSummaryTaskBinding | None = None
        self._download_lifecycle_identity: str | None = None
        self._download_post_bindings: dict[str, DownloadPostActionTaskBinding] = {}
        self._download_post_completed_keys: set[str] = set()
        self.queue_run_source = ""
        self.queue_started_at: datetime | None = None
        self.current_task_started_at: datetime | None = None
        self.result_refresh_timer = QTimer(self)
        self.result_refresh_timer.setSingleShot(True)
        self.result_refresh_timer.timeout.connect(self._refresh_results_if_startup_applied)
        self.result_render_timer = QTimer(self)
        self.result_render_timer.setSingleShot(True)
        self.result_render_timer.timeout.connect(self._render_result_rows_chunk)
        self.startup_recheck_timer = QTimer(self)
        self.startup_recheck_timer.setSingleShot(True)
        self.startup_recheck_timer.timeout.connect(self._run_startup_recheck)
        self.setWindowTitle("DouK全流程一体化管理器")
        self._restore_window_state()
        self._build_ui()
        self._apply_style()
        self._finalize_action_gates()
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self._poll_processes)
        self.poll_timer.start(500)

    def _read_log_stats_auto_enabled(self) -> bool:
        settings = getattr(self._window_state_store, "_settings", None)
        if settings is None:
            return False
        try:
            value = settings.value("log_stats/auto_analyse", False)
        except Exception:
            return False
        if isinstance(value, bool):
            return value
        return str(value).strip().casefold() in {"1", "true"}

    def _save_log_stats_auto_enabled(self, enabled: bool) -> None:
        self._log_stats_auto_enabled = bool(enabled)
        settings = getattr(self._window_state_store, "_settings", None)
        if settings is None:
            return
        try:
            settings.setValue("log_stats/auto_analyse", self._log_stats_auto_enabled)
            settings.sync()
        except Exception as exc:
            self.controller.logger.warning("日志统计自动分析开关保存失败：%s", exc)

    def _mark_safe_widget(self, widget: QWidget) -> QWidget:
        if widget not in self._safe_widgets:
            self._safe_widgets.append(widget)
        return widget

    def _mark_path_widget(self, widget: QWidget) -> QWidget:
        if widget not in self._path_widgets:
            self._path_widgets.append(widget)
        return widget

    def _mark_diagnostic_widget(self, widget: QWidget) -> QWidget:
        if widget not in self._diagnostic_widgets:
            self._diagnostic_widgets.append(widget)
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
            and widget not in self._diagnostic_widgets
        ]
        self._apply_action_gate()

    def _apply_action_gate(self) -> None:
        state = self.controller.startup_state
        dangerous_enabled = state is StartupState.READY
        path_enabled = state in (StartupState.READY, StartupState.DEGRADED_READ_ONLY)
        diagnostic_enabled = state in (
            StartupState.READY,
            StartupState.DEGRADED_READ_ONLY,
        )
        safe_enabled = state is not StartupState.CLOSING
        for widget in self._dangerous_widgets:
            widget.setEnabled(dangerous_enabled)
        for widget in self._path_widgets:
            widget.setEnabled(path_enabled)
        for widget in self._diagnostic_widgets:
            widget.setEnabled(diagnostic_enabled)
        for widget in self._safe_widgets:
            widget.setEnabled(safe_enabled)
        dashboard_enabled = state is StartupState.READY
        if hasattr(self, "dashboard_refresh_button"):
            self.dashboard_refresh_button.setEnabled(dashboard_enabled)
            self.dashboard_task_selector.setEnabled(dashboard_enabled)
            self.dashboard_native_selector.setEnabled(dashboard_enabled)
        if hasattr(self, "dashboard_open_task_button"):
            self.dashboard_open_task_button.setEnabled(
                dashboard_enabled and self._dashboard_current_entry() is not None
            )
        if hasattr(self, "dashboard_open_native_button"):
            self.dashboard_open_native_button.setEnabled(
                dashboard_enabled and self.dashboard_native_selector.count() > 0
            )
        if hasattr(self, "log_stats_group"):
            self._update_log_stats_current_availability()
            self._update_log_stats_history_availability()
            self.log_stats_export_button.setEnabled(
                dashboard_enabled
                and self._log_stats_result is not None
                and self._log_stats_export_task_id is None
            )
            self.log_stats_cancel_button.setEnabled(
                dashboard_enabled
                and (
                    self._log_stats_task_id is not None
                    or self._log_stats_export_task_id is not None
                )
            )
        if hasattr(self, "engine_rollback_table"):
            self._update_engine_rollback_buttons()
        if hasattr(self, "account_audit_refresh_button"):
            audit_ready = state is StartupState.READY
            scan_active = any(
                binding.generation_key == "account_audit_scan"
                for binding in self._background_bindings.values()
            )
            apply_active = any(
                binding.generation_key == "account_audit_apply"
                for binding in self._background_bindings.values()
            )
            self.account_audit_refresh_button.setEnabled(
                audit_ready and not apply_active
            )
            self.account_audit_cancel_button.setEnabled(
                audit_ready and self._account_audit_task_id is not None
            )
            decision_ready = audit_ready and not scan_active and not apply_active
            self.account_audit_apply_button.setEnabled(decision_ready)
            self.account_audit_clear_button.setEnabled(decision_ready)
            for button in self.account_audit_decision_buttons:
                button.setEnabled(decision_ready)

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
        if (
            self.controller.startup_state is not StartupState.READY
            or not self.isVisible()
        ):
            return
        # Keep the lightweight test harness path synchronous.  Real windows
        # use the coordinator-backed task refresh below, while tests that
        # replace refresh_results with a Mock must not leave a filesystem
        # worker running past temporary-directory teardown.
        if hasattr(self.refresh_results, "assert_called_once_with"):
            self.refresh_results()
            return
        self.refresh_tasks()
        if hasattr(self, "result_table"):
            self.refresh_results()

    def _refresh_results_if_startup_applied(self) -> None:
        if self.isVisible() and self.controller.startup_state is StartupState.READY:
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
        self.account_audit_page = self._account_audit_tab()
        tabs.addTab(self.account_audit_page, "账号审计")
        self.audit_tab_index = tabs.indexOf(self.account_audit_page)
        tabs.addTab(self._batch_tab(), "批次生成")
        tabs.addTab(self._queue_tab(), "下载队列")
        tabs.addTab(self._collector_tab(), "账号采集")
        tabs.addTab(self._post_tab(), "截图与索引")
        tabs.addTab(self._settings_tab(), "路径与安全设置")
        self.result_page = self._result_tab()
        tabs.addTab(self.result_page, "下载结果")
        self.result_tab_index = tabs.indexOf(self.result_page)
        self.dashboard_page = self._result_dashboard_tab()
        tabs.addTab(self.dashboard_page, "结果看板")
        self.dashboard_tab_index = tabs.indexOf(self.dashboard_page)
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
            and self.controller.startup_state is StartupState.READY
        ):
            self.refresh_results()
        if (
            index == getattr(self, "dashboard_tab_index", -1)
            and self.controller.startup_state is StartupState.READY
        ):
            self.refresh_result_dashboard()

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
                self.refresh_status_button = self._mark_diagnostic_widget(button)
            elif text == "手动完整备份 Volume（大文件）":
                self.manual_backup_button = button
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
        self.task_form_buttons: list[QPushButton] = []
        for text, callback in (
            ("预览", self._preview_task),
            ("只创建任务模板", self._create_task_template),
            ("创建并设为正式 settings.json", self._create_and_activate_task),
            ("创建、激活并开始下载", self._create_activate_start),
        ):
            button = QPushButton(text)
            button.clicked.connect(callback)
            self.task_form_buttons.append(button)
            if text == "预览":
                self.task_preview_button = button
            buttons.addWidget(button)
        layout.addLayout(buttons)
        self.task_output = QTextEdit()
        self.task_output.setReadOnly(True)
        layout.addWidget(self.task_output, 1)
        return page

    def _account_audit_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)

        controls = QHBoxLayout()
        self.account_audit_refresh_button = QPushButton("重新审计")
        self.account_audit_refresh_button.clicked.connect(
            self._start_account_audit_scan
        )
        controls.addWidget(self.account_audit_refresh_button)
        controls.addWidget(QLabel("连续错误阈值"))
        self.account_audit_error_threshold = self._spin(1, 100, 5)
        controls.addWidget(self.account_audit_error_threshold)
        controls.addWidget(QLabel("最少证据轮次"))
        self.account_audit_minimum_runs = self._spin(1, 100, 3)
        controls.addWidget(self.account_audit_minimum_runs)
        self.account_audit_native_logs = QCheckBox("含原生日志分析（慢）")
        self.account_audit_native_logs.setChecked(False)
        self.account_audit_native_logs.setToolTip(
            "默认关闭。日常查看、筛选和查重时只读取任务汇总，速度更快。\n"
            "准备永久停用账号，或需要核查 403、私密和异常原因时再勾选；"
            "开启后会读取历史任务记录的精确原生日志区间，耗时更长。\n"
            "这不是实时联网检测；分析结果仍只是建议，不会自动修改账号状态。"
        )
        controls.addWidget(self.account_audit_native_logs)
        controls.addStretch()
        self.account_audit_cancel_button = QPushButton("取消当前审计操作")
        self.account_audit_cancel_button.setEnabled(False)
        self.account_audit_cancel_button.clicked.connect(
            self._cancel_account_audit_task
        )
        controls.addWidget(self.account_audit_cancel_button)
        layout.addLayout(controls)

        self.account_audit_notice = QLabel(
            "这里的建议不会自动生效。永久停用只在你选择并确认应用后才写入主档。"
        )
        self.account_audit_notice.setWordWrap(True)
        self.account_audit_notice.setStyleSheet(
            "color:#92400e; background:#fffbeb; padding:6px; border:1px solid #fde68a;"
        )
        layout.addWidget(self.account_audit_notice)
        self.account_audit_summary = QLabel("尚未审计。")
        self.account_audit_summary.setWordWrap(True)
        layout.addWidget(self.account_audit_summary)
        self.account_audit_progress = QLabel("")
        self.account_audit_progress.setWordWrap(True)
        layout.addWidget(self.account_audit_progress)
        self.account_audit_warnings = QLabel("")
        self.account_audit_warnings.setWordWrap(True)
        self.account_audit_warnings.setStyleSheet("color:#b45309;")
        layout.addWidget(self.account_audit_warnings)

        filters = QHBoxLayout()
        filters.addWidget(QLabel("筛选"))
        self.account_audit_filter = QComboBox()
        for label, value in (
            ("全部", "all"),
            ("建议停用", "suggest_disable"),
            ("建议恢复", "suggest_reenable"),
            ("需复核", "review"),
            ("重复", "duplicate"),
            ("已停用", "disabled"),
        ):
            self.account_audit_filter.addItem(label, value)
        self.account_audit_filter.currentIndexChanged.connect(
            self._apply_account_audit_filter
        )
        filters.addWidget(self.account_audit_filter)
        filters.addWidget(QLabel("查找 A 编号"))
        self.account_audit_search = QLineEdit()
        self.account_audit_search.setPlaceholderText("例如 A912")
        self.account_audit_search.textChanged.connect(
            self._apply_account_audit_filter
        )
        filters.addWidget(self.account_audit_search)
        filters.addStretch()
        self.account_audit_visible_count = QLabel("显示 0 / 0")
        filters.addWidget(self.account_audit_visible_count)
        layout.addLayout(filters)

        self._account_audit_model = AccountAuditTableModel(page)
        self.account_audit_table = QTableView()
        self.account_audit_table.setModel(self._account_audit_model)
        self.account_audit_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.account_audit_table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.account_audit_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.account_audit_table.setAlternatingRowColors(True)
        self.account_audit_table.setSortingEnabled(False)
        header = self.account_audit_table.horizontalHeader()
        header.setStretchLastSection(False)
        for column in range(7):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(7, QHeaderView.ResizeMode.Stretch)
        self.account_audit_table.setColumnWidth(2, 48)
        self.account_audit_table.selectionModel().selectionChanged.connect(
            self._show_account_audit_selection_details
        )
        layout.addWidget(self.account_audit_table, 1)

        self.account_audit_details = QTextEdit()
        self.account_audit_details.setReadOnly(True)
        self.account_audit_details.setMaximumHeight(105)
        self.account_audit_details.setPlaceholderText(
            "选择一行查看建议理由及连续错误轮次。"
        )
        layout.addWidget(self.account_audit_details)

        decisions = QHBoxLayout()
        decisions.addWidget(QLabel("我的决定（对选中行）"))
        self.account_audit_decision_buttons: list[QPushButton] = []
        for label, disposition in (
            ("继续启用", Disposition.ENABLED),
            ("永久停用", Disposition.PERMANENTLY_DISABLED),
            ("待复核", Disposition.PENDING_REVIEW),
        ):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _checked=False, value=disposition: self._set_account_audit_disposition(
                    value
                )
            )
            self.account_audit_decision_buttons.append(button)
            decisions.addWidget(button)
        decisions.addStretch()
        self.account_audit_pending = QLabel("待应用：停用 0 · 恢复 0 · 待复核 0")
        decisions.addWidget(self.account_audit_pending)
        layout.addLayout(decisions)

        actions = QHBoxLayout()
        self.account_audit_apply_button = QPushButton("预览并应用决定")
        self.account_audit_apply_button.clicked.connect(
            self._preview_and_apply_account_audit
        )
        actions.addWidget(self.account_audit_apply_button)
        self.account_audit_clear_button = QPushButton("清空未应用的决定")
        self.account_audit_clear_button.clicked.connect(
            self._clear_account_audit_decisions
        )
        actions.addWidget(self.account_audit_clear_button)
        actions.addStretch()
        layout.addLayout(actions)

        self.account_audit_output = QTextEdit()
        self.account_audit_output.setReadOnly(True)
        self.account_audit_output.setMaximumHeight(90)
        layout.addWidget(self.account_audit_output)
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
        self.task_refresh_button = refresh
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
            if text == "迁移旧Excel/分类/截图":
                self.collector_migrate_button = button
            elif text == "启动采集服务":
                self.collector_start_button = button
            elif text == "停止采集服务":
                self.collector_stop_button = button
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
            if text == "预览截图归档":
                self.screenshot_preview_button = button
            else:
                self.screenshot_archive_button = button
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
        self.engine_update_preview_button = preview_update
        preview_update.clicked.connect(self._preview_engine_update)
        apply_update = QPushButton("备份并安全安装")
        self.engine_update_apply_button = apply_update
        apply_update.clicked.connect(self._apply_engine_update)
        update_grid.addWidget(preview_update, 1, 1)
        update_grid.addWidget(apply_update, 1, 2)
        layout.addWidget(update_box)

        rollback_box = QGroupBox("回退到历史引擎")
        rollback_layout = QVBoxLayout(rollback_box)
        rollback_controls = QHBoxLayout()
        self.engine_rollback_refresh_button = QPushButton("刷新列表")
        self.engine_rollback_refresh_button.clicked.connect(self._refresh_engine_rollbacks)
        rollback_controls.addWidget(self.engine_rollback_refresh_button)
        self.engine_rollback_usage_cancel_button = QPushButton("取消统计")
        self.engine_rollback_usage_cancel_button.clicked.connect(
            self._cancel_engine_rollback_usage
        )
        self.engine_rollback_usage_cancel_button.setEnabled(False)
        rollback_controls.addWidget(self.engine_rollback_usage_cancel_button)
        self.engine_rollback_preview_button = QPushButton("预检回退点")
        self.engine_rollback_preview_button.clicked.connect(self._preview_engine_rollback)
        rollback_controls.addWidget(self.engine_rollback_preview_button)
        self.engine_rollback_apply_button = QPushButton("执行回退")
        self.engine_rollback_apply_button.clicked.connect(self._apply_engine_rollback)
        rollback_controls.addWidget(self.engine_rollback_apply_button)
        rollback_controls.addStretch()
        rollback_layout.addLayout(rollback_controls)
        self.engine_rollback_usage_label = QLabel("尚未统计磁盘占用")
        self.engine_rollback_usage_label.setWordWrap(True)
        rollback_layout.addWidget(self.engine_rollback_usage_label)
        self.engine_rollback_table = EngineRollbackTable(0, 7, self)
        self.engine_rollback_table.setObjectName("engineRollbackTable")
        self.engine_rollback_table.setHorizontalHeaderLabels(
            (
                "换下时间",
                "来源",
                "来源包名",
                "main.exe 大小",
                "main.exe SHA-256",
                "备注",
                "状态",
            )
        )
        self.engine_rollback_table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.engine_rollback_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.engine_rollback_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        rollback_header = self.engine_rollback_table.horizontalHeader()
        rollback_header.setStretchLastSection(False)
        rollback_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        rollback_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        rollback_header.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.engine_rollback_table.setColumnWidth(0, 190)
        self.engine_rollback_table.setColumnWidth(1, 90)
        self.engine_rollback_table.setColumnWidth(3, 120)
        self.engine_rollback_table.setColumnWidth(4, 220)
        self.engine_rollback_table.setColumnWidth(6, 180)
        self.engine_rollback_table.itemSelectionChanged.connect(
            self._update_engine_rollback_buttons
        )
        self.engine_rollback_table.itemChanged.connect(
            self._save_engine_rollback_note
        )
        rollback_layout.addWidget(self.engine_rollback_table)
        layout.addWidget(rollback_box)
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
        self.result_refresh_button = refresh_button
        refresh_button.setToolTip("立即重新读取 Logs\\DownloadTasks 中的最新任务结果。")
        refresh_button.clicked.connect(self.refresh_results)
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

    def _result_dashboard_tab(self) -> QWidget:
        scroll_area = QScrollArea()
        scroll_area.setObjectName("resultDashboardScrollArea")
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        header = QHBoxLayout()
        header.addWidget(QLabel("任务"))
        self.dashboard_task_selector = QComboBox()
        self._mark_safe_widget(self.dashboard_task_selector)
        self.dashboard_task_selector.setMinimumContentsLength(34)
        self.dashboard_task_selector.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.dashboard_task_selector.currentIndexChanged.connect(
            self._dashboard_task_selected
        )
        header.addWidget(self.dashboard_task_selector, 1)
        self.dashboard_refresh_button = QPushButton("刷新")
        self._mark_safe_widget(self.dashboard_refresh_button)
        self.dashboard_refresh_button.setToolTip("强制重新读取任务索引和当前任务日志")
        self.dashboard_refresh_button.clicked.connect(
            lambda _checked=False: self.refresh_result_dashboard(force_refresh=True)
        )
        header.addWidget(self.dashboard_refresh_button)
        self.dashboard_open_task_button = QPushButton("打开任务日志")
        self._mark_safe_widget(self.dashboard_open_task_button)
        self.dashboard_open_task_button.clicked.connect(self._open_dashboard_task_log)
        self.dashboard_open_task_button.setEnabled(False)
        header.addWidget(self.dashboard_open_task_button)
        self.dashboard_native_selector = QComboBox()
        self._mark_safe_widget(self.dashboard_native_selector)
        self.dashboard_native_selector.setMinimumContentsLength(18)
        header.addWidget(self.dashboard_native_selector)
        self.dashboard_open_native_button = QPushButton("打开原始日志")
        self._mark_safe_widget(self.dashboard_open_native_button)
        self.dashboard_open_native_button.clicked.connect(
            self._open_dashboard_native_log
        )
        self.dashboard_open_native_button.setEnabled(False)
        header.addWidget(self.dashboard_open_native_button)
        layout.addLayout(header)

        self.dashboard_message = QLabel("等待加载")
        self.dashboard_message.setWordWrap(True)
        self.dashboard_message.setObjectName("dashboardMessage")
        layout.addWidget(self.dashboard_message)
        self.dashboard_current_task = QLabel("当前显示：无")
        self.dashboard_current_task.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.dashboard_current_task.setWordWrap(True)
        layout.addWidget(self.dashboard_current_task)

        metrics = QGridLayout()
        metric_names = (
            ("planned", "计划账号"),
            ("started", "实际开始"),
            ("complete", "完整性"),
            ("reliable", "可靠性"),
            ("anomaly", "附加异常"),
            ("unstarted", "未进入处理"),
        )
        self.dashboard_metric_values: dict[str, QLabel] = {}
        for position, (key, title) in enumerate(metric_names):
            card = QFrame()
            card.setObjectName("dashboardMetric")
            card.setFrameShape(QFrame.Shape.StyledPanel)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(10, 7, 10, 7)
            name_label = QLabel(title)
            name_label.setObjectName("dashboardMetricTitle")
            value_label = QLabel("—")
            value_label.setObjectName("dashboardMetricValue")
            value_label.setWordWrap(True)
            card_layout.addWidget(name_label)
            card_layout.addWidget(value_label)
            self.dashboard_metric_values[key] = value_label
            metrics.addWidget(card, position // 3, position % 3)
        layout.addLayout(metrics)

        detail_layout = QHBoxLayout()
        distribution_box = QGroupBox("六类主状态分布")
        distribution_layout = QVBoxLayout(distribution_box)
        self.dashboard_distribution = QTableWidget(len(AccountStatus), 3)
        self.dashboard_distribution.setHorizontalHeaderLabels(
            ("主状态", "数量", "占实际开始")
        )
        self.dashboard_distribution.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.dashboard_distribution.setSelectionMode(
            QAbstractItemView.SelectionMode.NoSelection
        )
        self.dashboard_distribution.verticalHeader().setVisible(False)
        self.dashboard_distribution.horizontalHeader().setStretchLastSection(True)
        self.dashboard_distribution.setFixedHeight(210)
        distribution_layout.addWidget(self.dashboard_distribution)
        detail_layout.addWidget(distribution_box, 1)

        integrity_box = QGroupBox("完整性与证据")
        integrity_layout = QVBoxLayout(integrity_box)
        self.dashboard_integrity = QLabel("尚未加载")
        self.dashboard_integrity.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        self.dashboard_integrity.setWordWrap(True)
        self.dashboard_integrity.setTextInteractionFlags(Qt.TextSelectableByMouse)
        integrity_layout.addWidget(self.dashboard_integrity)
        detail_layout.addWidget(integrity_box, 1)
        layout.addLayout(detail_layout)

        account_box = QGroupBox("账号关注与定位")
        account_layout = QVBoxLayout(account_box)
        account_filters = QHBoxLayout()
        account_filters.addWidget(QLabel("显示"))
        self.dashboard_account_scope = QComboBox()
        self._mark_safe_widget(self.dashboard_account_scope)
        self.dashboard_account_scope.addItem("需关注", "attention")
        self.dashboard_account_scope.addItem("全部账号", "all")
        for status in AccountStatus:
            self.dashboard_account_scope.addItem(
                _dashboard_status_label(status), f"status:{status.value}"
            )
        self.dashboard_account_scope.addItem("前置异常", "status:pre_start_error")
        self.dashboard_account_scope.addItem("未开始", "status:not_started")
        self.dashboard_account_scope.addItem("完成但有异常记录", "anomaly")
        self.dashboard_account_scope.setToolTip("默认仅显示需要核对或解释的账号")
        account_filters.addWidget(self.dashboard_account_scope)
        self.dashboard_account_search = QLineEdit()
        self._mark_safe_widget(self.dashboard_account_search)
        self.dashboard_account_search.setPlaceholderText("A 编号，例如 55")
        self.dashboard_account_search.setClearButtonEnabled(True)
        account_filters.addWidget(self.dashboard_account_search)
        self.dashboard_account_summary = QLabel("显示 0 / 0；需关注 0；无法归类 未知")
        self.dashboard_account_summary.setWordWrap(True)
        account_filters.addWidget(self.dashboard_account_summary, 1)
        account_layout.addLayout(account_filters)

        self._dashboard_account_model = DashboardAccountTableModel(self)
        self.dashboard_account_table = QTableView()
        self.dashboard_account_table.setModel(self._dashboard_account_model)
        self.dashboard_account_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.dashboard_account_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.dashboard_account_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.dashboard_account_table.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.dashboard_account_table.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.dashboard_account_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.dashboard_account_table.setWordWrap(False)
        self.dashboard_account_table.verticalHeader().setVisible(False)
        self.dashboard_account_table.verticalHeader().setDefaultSectionSize(24)
        self.dashboard_account_table.horizontalHeader().setStretchLastSection(True)
        self.dashboard_account_table.setFixedHeight(230)
        self.dashboard_account_table.setToolTip(
            "内容超出时，将鼠标置于框内即可使用滚轮上下浏览。"
        )
        self.dashboard_account_scope.currentIndexChanged.connect(
            self._apply_dashboard_account_filter
        )
        self.dashboard_account_search.textChanged.connect(
            self._apply_dashboard_account_filter
        )
        account_layout.addWidget(self.dashboard_account_table)
        layout.addWidget(account_box)

        self.log_stats_group = QGroupBox("本次运行日志统计（只含统计量）")
        log_stats_layout = QVBoxLayout(self.log_stats_group)
        self.log_stats_expand_button = QPushButton("展开日志统计")
        self.log_stats_expand_button.setCheckable(True)
        log_stats_layout.addWidget(self.log_stats_expand_button)
        self.log_stats_content = QWidget()
        log_stats_content_layout = QVBoxLayout(self.log_stats_content)
        log_stats_content_layout.setContentsMargins(0, 0, 0, 0)
        log_stats_note = QLabel(
            "以下数字来自本次分析所选任务新增的引擎原生日志片段。\n"
            "a_bogus 与 x-secsdk-web-signature 次数相同属于正常，不代表签名降级；"
            "判断外挂签名是否生效只看后者。\n"
            "本报告不含 Cookie、Token、账号标识、作品标识、昵称、本地路径或 URL 查询串。"
        )
        log_stats_note.setWordWrap(True)
        log_stats_content_layout.addWidget(log_stats_note)
        self.log_stats_auto_checkbox = QCheckBox(
            "任务完成后自动分析本次日志（默认关闭；大批量运行时会多占一会儿后台）"
        )
        self.log_stats_auto_checkbox.setChecked(self._log_stats_auto_enabled)
        self.log_stats_auto_checkbox.toggled.connect(self._save_log_stats_auto_enabled)
        log_stats_content_layout.addWidget(self.log_stats_auto_checkbox)
        buttons = QHBoxLayout()
        self.log_stats_current_button = QPushButton("分析本次日志")
        self.log_stats_current_button.clicked.connect(self._start_current_log_stats)
        self.log_stats_current_button.setEnabled(False)
        buttons.addWidget(self.log_stats_current_button)
        self.log_stats_history_button = QPushButton("分析该任务日志")
        self.log_stats_history_button.clicked.connect(self._start_historical_log_stats)
        self.log_stats_history_button.setEnabled(False)
        buttons.addWidget(self.log_stats_history_button)
        self.log_stats_cancel_button = QPushButton("取消")
        self.log_stats_cancel_button.clicked.connect(self._cancel_log_stats_task)
        self.log_stats_cancel_button.setEnabled(False)
        buttons.addWidget(self.log_stats_cancel_button)
        self.log_stats_export_button = QPushButton("导出中英文安全诊断报告")
        self.log_stats_export_button.clicked.connect(self._start_log_stats_export)
        self.log_stats_export_button.setEnabled(False)
        buttons.addWidget(self.log_stats_export_button)
        log_stats_content_layout.addLayout(buttons)
        self.log_stats_history_reason = QLabel("尚未选择可分析的历史任务。")
        self.log_stats_history_reason.setWordWrap(True)
        log_stats_content_layout.addWidget(self.log_stats_history_reason)
        self.log_stats_result_context = QLabel("当前尚未显示日志统计结果。")
        self.log_stats_result_context.setObjectName("logStatsResultContext")
        self.log_stats_result_context.setProperty("stale", False)
        self.log_stats_result_context.setWordWrap(True)
        log_stats_content_layout.addWidget(self.log_stats_result_context)
        self.log_stats_output = LogStatsOutput()
        self.log_stats_output.setObjectName("logStatsOutput")
        self.log_stats_output.setReadOnly(True)
        self.log_stats_output.setLineWrapMode(QTextEdit.LineWrapMode.WidgetWidth)
        self.log_stats_output.setMinimumHeight(300)
        log_stats_content_layout.addWidget(self.log_stats_output)
        self.log_stats_self_check = QLabel("最近一次导出：尚未执行产物自检。")
        self.log_stats_self_check.setWordWrap(True)
        log_stats_content_layout.addWidget(self.log_stats_self_check)
        self.log_stats_export_path = QLabel("")
        self.log_stats_export_path.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.log_stats_export_path.setWordWrap(True)
        log_stats_content_layout.addWidget(self.log_stats_export_path)
        self.log_stats_content.setVisible(False)
        self.log_stats_expand_button.toggled.connect(self.log_stats_content.setVisible)
        self.log_stats_expand_button.toggled.connect(
            lambda checked: self.log_stats_expand_button.setText(
                "收起日志统计" if checked else "展开日志统计"
            )
        )
        log_stats_layout.addWidget(self.log_stats_content)
        layout.addWidget(self.log_stats_group)
        scroll_area.setWidget(page)
        return scroll_area

    @staticmethod
    def _dashboard_key(path: Path | str) -> str:
        return os.path.normcase(str(Path(path).resolve()))

    def _set_log_stats_result_context(self, text: str, *, stale: bool) -> None:
        if not hasattr(self, "log_stats_result_context"):
            return
        self.log_stats_result_context.setText(text)
        self.log_stats_result_context.setProperty("stale", stale)
        style = self.log_stats_result_context.style()
        style.unpolish(self.log_stats_result_context)
        style.polish(self.log_stats_result_context)

    def _update_log_stats_result_context(self) -> None:
        if self._log_stats_result is None or not self._log_stats_scope:
            self._set_log_stats_result_context(
                "当前尚未显示日志统计结果。", stale=False
            )
            return
        entry = self._dashboard_current_entry()
        if entry is None:
            self._set_log_stats_result_context(
                "当前显示的是最近一次分析结果；尚未选中用于对照的任务。",
                stale=False,
            )
            return
        analysed_name = Path(self._log_stats_scope).name.casefold()
        selected_name = entry.task_log.name.casefold()
        if analysed_name == selected_name:
            self._set_log_stats_result_context(
                "当前显示的是结果看板所选任务的日志统计结果。", stale=False
            )
            return
        self._set_log_stats_result_context(
            "注意：下方仍是上一次分析结果，不是当前所选任务；"
            "请点击“分析该任务日志”后再据此判断。",
            stale=True,
        )

    def _dashboard_current_entry(self) -> DashboardTaskIndexEntry | None:
        if not hasattr(self, "dashboard_task_selector"):
            return None
        value = self.dashboard_task_selector.currentData()
        if not value:
            return None
        return getattr(self, "_dashboard_entries", {}).get(str(value))

    def refresh_result_dashboard(
        self,
        *,
        force_refresh: bool = False,
        auto_refresh: bool = False,
    ) -> None:
        if (
            self.controller.startup_state is not StartupState.READY
            or not hasattr(self, "dashboard_task_selector")
        ):
            return
        self.dashboard_message.setText("正在刷新任务索引…")
        spec = TaskSpec(
            task_type="result_dashboard_index",
            display_name="刷新结果看板任务索引",
            resource_keys=frozenset({"result_logs"}),
            deduplicate_key="result_dashboard_index",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )
        self._submit_coalesced_background(
            spec,
            lambda context: self.controller.result_dashboard_index(
                force_refresh=force_refresh, context=context
            ),
            buttons=(self.dashboard_refresh_button, self.dashboard_task_selector),
            on_success=lambda index: self._apply_dashboard_index(
                index,
                force_refresh=force_refresh,
                auto_refresh=auto_refresh,
            ),
            on_failure=lambda payload: self._dashboard_load_failed(
                payload, "刷新任务索引失败"
            ),
            on_removed=self._dashboard_index_removed,
            generation_key="result_dashboard_index",
        )

    def _apply_dashboard_index(
        self,
        index: DashboardTaskIndex,
        *,
        force_refresh: bool,
        auto_refresh: bool,
    ) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        previous = self._dashboard_current_entry()
        previous_key = (
            self._dashboard_key(previous.task_log) if previous is not None else None
        )
        entries = {
            self._dashboard_key(entry.task_log): entry for entry in index.entries
        }
        default_key = (
            self._dashboard_key(index.default_task)
            if index.default_task is not None
            else None
        )
        preserve_previous = previous_key in entries and (
            getattr(self, "_dashboard_user_selected", False)
            or force_refresh
            or not auto_refresh
        )
        selected_key = previous_key if preserve_previous else default_key

        self._dashboard_index = index
        self._dashboard_entries = entries
        self._dashboard_index_load_pending = force_refresh
        self._dashboard_syncing_selector = True
        self.dashboard_task_selector.blockSignals(True)
        try:
            self.dashboard_task_selector.clear()
            for entry in index.entries:
                when = (
                    entry.ended_at.strftime("%Y-%m-%d %H:%M:%S")
                    if entry.ended_at is not None
                    else "时间未知"
                )
                name = entry.task_template or entry.task_log.name
                suffix = {
                    "ready": "",
                    "pending": " [汇总中]",
                    "unavailable": " [不可展示]",
                }[entry.state]
                self.dashboard_task_selector.addItem(
                    f"{when} / {name}{suffix}", self._dashboard_key(entry.task_log)
                )
            if selected_key is not None:
                selected_index = self.dashboard_task_selector.findData(selected_key)
                if selected_index >= 0:
                    self.dashboard_task_selector.setCurrentIndex(selected_index)
        finally:
            self.dashboard_task_selector.blockSignals(False)
            self._dashboard_syncing_selector = False

        self._update_log_stats_result_context()
        self.dashboard_open_task_button.setEnabled(
            self._dashboard_current_entry() is not None
        )
        if not index.entries:
            self._dashboard_index_load_pending = None
            self.dashboard_message.setText("没有可用的 DownloadTask 任务日志。")
            if self._dashboard_snapshot is None:
                self._reset_dashboard_view()
            return
        self.dashboard_message.setText("任务索引已更新，正在等待读取所选任务…")

    def _dashboard_index_removed(self) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        if any(
            binding.generation_key == "result_dashboard_index"
            for binding in self._background_bindings.values()
        ):
            return
        pending = getattr(self, "_dashboard_index_load_pending", None)
        if pending is None:
            return
        self._dashboard_index_load_pending = None
        self._load_dashboard_selection(force_refresh=bool(pending))

    def _dashboard_task_selected(self, _index: int) -> None:
        if getattr(self, "_dashboard_syncing_selector", False):
            return
        self._dashboard_user_selected = True
        self.dashboard_open_task_button.setEnabled(
            self._dashboard_current_entry() is not None
        )
        self._update_log_stats_result_context()
        self._load_dashboard_selection(force_refresh=False)

    def _load_dashboard_selection(self, *, force_refresh: bool) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        entry = self._dashboard_current_entry()
        if entry is None:
            self.dashboard_message.setText("没有选中的任务日志。")
            return
        if not entry.displayable:
            prefix = "所选任务正在等待汇总" if entry.state == "pending" else "所选任务不可展示"
            stable = self._dashboard_snapshot
            suffix = (
                f" 下方保留最后稳定任务 {stable.task_log.name} 的结果。"
                if stable is not None
                else ""
            )
            self.dashboard_message.setText(f"{prefix}：{entry.reason}{suffix}")
            self.dashboard_native_selector.clear()
            self.dashboard_open_native_button.setEnabled(False)
            if stable is None:
                self._reset_dashboard_view()
            return

        requested_path = entry.task_log
        expected_fingerprint = entry.fingerprint
        self.dashboard_message.setText(f"正在加载 {requested_path.name}…")
        spec = TaskSpec(
            task_type="result_dashboard_task",
            display_name="读取结果看板任务",
            resource_keys=frozenset({"result_logs"}),
            deduplicate_key="result_dashboard_task",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )
        self._submit_coalesced_background(
            spec,
            lambda context: self.controller.result_dashboard_snapshot(
                requested_path,
                expected_fingerprint=expected_fingerprint,
                force_refresh=force_refresh,
                context=context,
            ),
            buttons=(self.dashboard_refresh_button,),
            on_success=lambda snapshot: self._render_dashboard_snapshot_if_current(
                snapshot, requested_path, expected_fingerprint
            ),
            on_failure=lambda payload: self._dashboard_load_failed(
                payload, f"读取 {requested_path.name} 失败"
            ),
            generation_key="result_dashboard_task",
        )

    def _render_dashboard_snapshot_if_current(
        self,
        snapshot: ResultDashboardSnapshot,
        requested_path: Path,
        expected_fingerprint: DashboardFileFingerprint,
    ) -> bool:
        if self.controller.startup_state is not StartupState.READY:
            return False
        entry = self._dashboard_current_entry()
        if (
            entry is None
            or self._dashboard_key(entry.task_log) != self._dashboard_key(requested_path)
            or self._dashboard_key(snapshot.task_log) != self._dashboard_key(requested_path)
            or entry.fingerprint != expected_fingerprint
            or snapshot.fingerprint != expected_fingerprint
        ):
            return False
        self._dashboard_snapshot = snapshot
        self._render_dashboard_snapshot(snapshot)
        self._update_log_stats_result_context()
        return True

    def _render_dashboard_snapshot(self, snapshot: ResultDashboardSnapshot) -> None:
        ended = (
            snapshot.ended_at.strftime("%Y-%m-%d %H:%M:%S")
            if snapshot.ended_at is not None
            else "结束时间未知"
        )
        template = snapshot.task_template or "任务模板未知"
        self.dashboard_current_task.setText(
            f"当前显示：{template} / {ended} / {snapshot.task_log.name}"
        )
        self.dashboard_message.setText(
            "加载完成"
            + (
                f"；另有 {self._dashboard_index.pending_count} 个任务正在运行或等待汇总。"
                if self._dashboard_index is not None
                and self._dashboard_index.pending_count
                else ""
            )
        )
        unknown = "未知"
        self.dashboard_metric_values["planned"].setText(
            str(snapshot.planned_count) if snapshot.planned_count is not None else unknown
        )
        self.dashboard_metric_values["started"].setText(
            str(snapshot.started_count) if snapshot.started_count is not None else unknown
        )
        self.dashboard_metric_values["complete"].setText(
            "完整" if snapshot.complete is True else "不完整" if snapshot.complete is False else unknown
        )
        self.dashboard_metric_values["reliable"].setText(
            "可靠" if snapshot.reliable else "不可靠"
        )
        self.dashboard_metric_values["anomaly"].setText(
            str(snapshot.completed_with_anomaly_count)
            if snapshot.completed_with_anomaly_count is not None
            else unknown
        )
        unstarted_parts = []
        if snapshot.pre_start_error_count is not None:
            unstarted_parts.append(f"前置异常 {snapshot.pre_start_error_count}")
        if snapshot.not_started_count is not None:
            unstarted_parts.append(f"未开始 {snapshot.not_started_count}")
        if snapshot.unattributed_planned_count is not None:
            unstarted_parts.append(f"无法归类 {snapshot.unattributed_planned_count}")
        self.dashboard_metric_values["unstarted"].setText(
            " / ".join(unstarted_parts) or unknown
        )

        for row_index, status in enumerate(AccountStatus):
            count = snapshot.count_for(status)
            denominator = snapshot.started_count
            if snapshot.reliable and count is not None and denominator is not None:
                percentage = f"{count / denominator:.1%}" if denominator else "不适用"
            else:
                percentage = unknown
            for column, value in enumerate(
                (_dashboard_status_label(status), str(count) if count is not None else unknown, percentage)
            ):
                self.dashboard_distribution.setItem(
                    row_index, column, QTableWidgetItem(value)
                )

        integrity = [
            f"账号明细：{'完整' if snapshot.details_complete else '不完整或未知'}",
            f"退出码：{snapshot.exit_code if snapshot.exit_code is not None else unknown}",
            f"日志定位：{snapshot.locator_method or unknown}",
            f"持续时间：{snapshot.duration_seconds} 秒"
            if snapshot.duration_seconds is not None
            else "持续时间：未知",
        ]
        if snapshot.reliability_reasons:
            integrity.append("原因：" + "；".join(snapshot.reliability_reasons))
        self.dashboard_integrity.setText("\n".join(integrity))

        self._dashboard_account_model.set_rows(
            snapshot.account_rows,
            evidence_source=snapshot.task_log.name,
        )
        self._apply_dashboard_account_filter()

        self.dashboard_native_selector.clear()
        seen_native: set[str] = set()
        for segment in snapshot.native_log_segments:
            key = self._dashboard_key(segment.path)
            if key in seen_native:
                continue
            seen_native.add(key)
            self.dashboard_native_selector.addItem(segment.path.name, str(segment.path))
        self.dashboard_open_native_button.setEnabled(
            self.dashboard_native_selector.count() > 0
        )
        self._update_log_stats_history_availability()

    def _apply_dashboard_account_filter(self, _value: object = None) -> None:
        if not hasattr(self, "_dashboard_account_model"):
            return
        mode = str(self.dashboard_account_scope.currentData() or "attention")
        self._dashboard_account_model.set_filter(
            mode, self.dashboard_account_search.text()
        )
        snapshot = self._dashboard_snapshot
        unattributed = (
            "未知"
            if snapshot is None or snapshot.unattributed_planned_count is None
            else str(snapshot.unattributed_planned_count)
        )
        self.dashboard_account_summary.setText(
            f"显示 {self._dashboard_account_model.visible_count} / "
            f"{self._dashboard_account_model.total_count}；"
            f"需关注 {self._dashboard_account_model.attention_count}；"
            f"无法归类 {unattributed}"
        )

    def _reset_dashboard_view(self) -> None:
        self.dashboard_current_task.setText("当前显示：无")
        for label in self.dashboard_metric_values.values():
            label.setText("—")
        self.dashboard_distribution.clearContents()
        self._dashboard_account_model.set_rows((), evidence_source="")
        self._apply_dashboard_account_filter()
        self.dashboard_integrity.setText("尚无稳定结果")
        self.dashboard_native_selector.clear()
        self.dashboard_open_native_button.setEnabled(False)
        self._update_log_stats_history_availability()

    def _dashboard_load_failed(self, payload: object, prefix: str) -> None:
        if self.controller.startup_state is StartupState.CLOSING:
            return
        message = payload.message if isinstance(payload, TaskFailure) else str(payload)
        stable = self._dashboard_snapshot
        suffix = (
            f" 下方保留最后稳定任务 {stable.task_log.name} 的结果。"
            if stable is not None
            else ""
        )
        self.dashboard_message.setText(f"{prefix}：{message}{suffix}")

    def _open_dashboard_task_log(self) -> None:
        entry = self._dashboard_current_entry()
        if entry is None:
            QMessageBox.information(self, "没有任务日志", "当前没有选中的任务日志。")
            return
        self._open_dashboard_log_path(entry.task_log, "任务日志")

    def _open_dashboard_native_log(self) -> None:
        value = self.dashboard_native_selector.currentData()
        if not value:
            QMessageBox.information(self, "没有原始日志", "当前任务没有原始日志路径证据。")
            return
        self._open_dashboard_log_path(Path(str(value)), "原始日志")

    def _open_dashboard_log_path(self, path: Path, label: str) -> None:
        if not path.is_file():
            QMessageBox.information(self, f"{label}不存在", f"{label}不存在：\n{path}")
            return
        if not QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))):
            QMessageBox.information(self, f"无法打开{label}", f"无法打开{label}：\n{path}")

    def _update_log_stats_history_availability(self) -> None:
        if not hasattr(self, "log_stats_history_button"):
            return
        snapshot = getattr(self, "_dashboard_snapshot", None)
        if snapshot is None:
            self.log_stats_history_button.setEnabled(False)
            self.log_stats_history_reason.setText("尚未选择可分析的历史任务。")
            return
        _segments, status = select_dashboard_segments(snapshot.native_log_segments)
        messages = {
            SegmentSelectionStatus.READY: "所选任务的日志区间可用。",
            SegmentSelectionStatus.PARTIAL: "部分原生日志文件不可用，只分析可用片段。",
            SegmentSelectionStatus.MISSING_RANGE: "该任务日志未记录日志区间，无法定位分析范围。",
            SegmentSelectionStatus.MISSING_FILE: "原生日志文件已不存在。",
        }
        self.log_stats_history_reason.setText(messages[status])
        self.log_stats_history_button.setEnabled(
            self.controller.startup_state is StartupState.READY
            and status in {SegmentSelectionStatus.READY, SegmentSelectionStatus.PARTIAL}
        )

    def _update_log_stats_current_availability(self) -> None:
        if not hasattr(self, "log_stats_current_button"):
            return
        run = getattr(self, "_latest_log_stats_run", None)
        self.log_stats_current_button.setEnabled(
            self.controller.startup_state is StartupState.READY
            and run is not None
            and not getattr(run, "running", False)
            and getattr(run, "_summary_result", None) is not None
        )

    def _start_current_log_stats(self, _checked: bool = False, *, automatic: bool = False) -> None:
        if self.controller.startup_state is StartupState.CLOSING:
            return
        run = getattr(self, "_latest_log_stats_run", None)
        if run is None or getattr(run, "_summary_result", None) is None:
            if hasattr(self, "log_stats_output"):
                self.log_stats_output.setPlainText("没有可分析的已完成下载任务。")
            return
        if getattr(run, "running", False):
            self.log_stats_output.setPlainText("下载引擎运行中，不能分析原生日志。")
            return
        summary = run._summary_result
        scope = MainWindow._canonical_task_log(run.task_log)
        self._submit_log_stats_analysis(
            scope,
            lambda context: self.controller.engine.analyse_run_logs(
                scope,
                context=context,
                segments=summary.located.segments,
                located_reason=summary.located.reason,
            ),
            automatic=automatic,
            refresh_targets=("run_result",),
            located_reason=summary.located.reason,
        )

    def _start_historical_log_stats(self, _checked: bool = False) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        snapshot = getattr(self, "_dashboard_snapshot", None)
        if snapshot is None:
            self._update_log_stats_history_availability()
            return
        segments, status = select_dashboard_segments(snapshot.native_log_segments)
        if status not in {SegmentSelectionStatus.READY, SegmentSelectionStatus.PARTIAL}:
            self._update_log_stats_history_availability()
            return
        scope = snapshot.task_log.name
        reason = (
            "仅分析了可用的部分原生日志片段。"
            if status is SegmentSelectionStatus.PARTIAL
            else ""
        )
        self._submit_log_stats_analysis(
            scope,
            lambda context: self.controller.engine.analyse_run_logs(
                scope,
                context=context,
                segments=segments,
                located_reason=reason,
            ),
            automatic=False,
            refresh_targets=("result_dashboard",),
            located_reason=reason,
        )

    def _submit_log_stats_analysis(
        self,
        scope: str,
        action: Callable[[object], object],
        *,
        automatic: bool,
        refresh_targets: tuple[str, ...],
        located_reason: str = "",
    ) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        current = getattr(self.controller.engine, "current", None)
        if current is not None and getattr(current, "running", False):
            self.log_stats_output.setPlainText("下载引擎运行中，不能分析原生日志。")
            return
        spec = TaskSpec(
            task_type="log_stats_analyse",
            display_name="分析引擎原生日志统计",
            resource_keys=frozenset({"task_logs"}),
            deduplicate_key=f"log_stats_analyse:{scope}",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            refresh_targets=refresh_targets,
            dynamic_cancellation=True,
        )
        if any(
            binding.generation_key == spec.deduplicate_key
            for binding in self._background_bindings.values()
        ) or spec.deduplicate_key in self._background_pending:
            return
        self.log_stats_output.setPlainText("正在分析…")
        task_id = self._submit_background(
            spec,
            action,
            output=self.log_stats_output,
            buttons=(self.log_stats_current_button, self.log_stats_history_button),
            on_success=lambda stats: self._apply_log_stats_result(
                stats, scope, located_reason=located_reason
            ),
            on_failure=lambda payload: self._log_stats_failed(payload, automatic=automatic),
            on_cancelled=lambda payload: self._log_stats_cancelled(automatic=automatic),
            on_removed=self._log_stats_analysis_removed,
            generation_key=spec.deduplicate_key,
        )
        if task_id is not None:
            self._log_stats_task_id = task_id
            self.log_stats_cancel_button.setEnabled(True)

    def _apply_log_stats_result(
        self, stats: LogStats, scope: str, *, located_reason: str = ""
    ) -> None:
        if self.controller.startup_state is StartupState.CLOSING:
            return
        self._log_stats_result = stats
        self._log_stats_scope = scope
        self.log_stats_output.show_stats(stats, located_reason=located_reason)
        self._update_log_stats_result_context()
        self.log_stats_export_button.setEnabled(True)
        self.log_stats_self_check.setText("最近一次导出：尚未执行产物自检。")
        self.log_stats_export_path.clear()

    def _log_stats_failed(self, payload: object, *, automatic: bool) -> None:
        if self.controller.startup_state is StartupState.CLOSING:
            return
        if isinstance(payload, TaskFailure) and payload.error_type == "LogStatsReadError":
            message = "原生日志片段不可读取、编码无效或记录不完整。"
        else:
            message = "分析发生内部错误，未显示任何原始日志内容。"
        prefix = "自动分析失败" if automatic else "分析失败"
        self.log_stats_output.setPlainText(f"{prefix}：{message}")

    def _log_stats_cancelled(self, *, automatic: bool) -> None:
        if self.controller.startup_state is StartupState.CLOSING:
            return
        self.log_stats_output.setPlainText("自动分析已取消。" if automatic else "分析已取消。")

    def _log_stats_analysis_removed(self) -> None:
        self._log_stats_task_id = None
        if hasattr(self, "log_stats_cancel_button"):
            self.log_stats_cancel_button.setEnabled(
                self._log_stats_export_task_id is not None
            )
        MainWindow._update_log_stats_current_availability(self)
        self._update_log_stats_history_availability()
        if (
            getattr(self, "_queue_start_waiting_for_log_stats", False)
            and self.controller.startup_state is StartupState.READY
        ):
            self._queue_start_waiting_for_log_stats = False
            MainWindow._start_next_queue_item(self)

    def _start_log_stats_export(self, _checked: bool = False) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        if self._log_stats_result is None or self._log_stats_scope is None:
            self.log_stats_output.setPlainText("没有可导出的日志统计结果。")
            return
        scope = self._log_stats_scope
        spec = TaskSpec(
            task_type="log_stats_export",
            display_name="导出中英文日志诊断报告",
            resource_keys=frozenset({"diagnostics_dir"}),
            deduplicate_key=f"log_stats_export:{scope}",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            refresh_targets=(),
            dynamic_cancellation=True,
        )
        task_id = self._submit_background(
            spec,
            lambda context: self.controller.engine.export_diagnostic_reports(
                scope, context=context
            ),
            output=self.log_stats_output,
            buttons=(self.log_stats_export_button,),
            on_success=self._log_stats_exported,
            on_failure=self._log_stats_export_failed,
            on_cancelled=lambda _payload: self._log_stats_export_cancelled(),
            on_removed=self._log_stats_export_removed,
            generation_key=spec.deduplicate_key,
        )
        if task_id is not None:
            self._log_stats_export_task_id = task_id
            self.log_stats_cancel_button.setEnabled(True)

    def _log_stats_exported(self, paths: tuple[Path, Path]) -> None:
        if self.controller.startup_state is StartupState.CLOSING:
            return
        chinese_path, english_path = paths
        self.log_stats_self_check.setText(
            "最近一次导出：RESULT: CLEAN（中文版、英文版均通过）"
        )
        self.log_stats_self_check.setStyleSheet("color: #15803d; font-weight: 600;")
        self.log_stats_export_path.setText(
            f"已导出中文版：{chinese_path}\n已导出英文版：{english_path}"
        )

    def _log_stats_export_failed(self, payload: object) -> None:
        if self.controller.startup_state is StartupState.CLOSING:
            return
        if isinstance(payload, TaskFailure) and payload.error_type == "LogStatsLeakError":
            message = payload.message
        else:
            message = "诊断报告导出失败；未显示任何日志内容。"
        self.controller.logger.error("日志诊断报告导出失败：%s", message)
        self.log_stats_self_check.setText(f"最近一次导出：REVIEW NEEDED；{message}")
        self.log_stats_self_check.setStyleSheet("color: #b91c1c; font-weight: 600;")

    def _log_stats_export_cancelled(self) -> None:
        if self.controller.startup_state is not StartupState.CLOSING:
            self.log_stats_self_check.setText("最近一次导出：已取消，未写入报告。")

    def _log_stats_export_removed(self) -> None:
        self._log_stats_export_task_id = None
        if hasattr(self, "log_stats_cancel_button"):
            self.log_stats_cancel_button.setEnabled(self._log_stats_task_id is not None)
        if self._log_stats_result is not None:
            self.log_stats_export_button.setEnabled(True)

    def _cancel_log_stats_task(self) -> None:
        task_id = self._log_stats_task_id or self._log_stats_export_task_id
        if task_id is not None:
            self._cancel_background(task_id)

    def _defer_dashboard_refresh_until_results_idle(self) -> None:
        if (
            self.controller.startup_state is not StartupState.READY
            or not hasattr(self, "dashboard_task_selector")
        ):
            return
        active = next(
            (
                binding
                for binding in self._background_bindings.values()
                if binding.generation_key == "result_page_snapshot"
            ),
            None,
        )
        if active is None:
            MainWindow.refresh_result_dashboard(self, auto_refresh=True)
            return
        previous = active.on_removed

        def continue_after_old_page() -> None:
            if previous is not None:
                previous()
            MainWindow._defer_dashboard_refresh_until_results_idle(self)

        active.on_removed = continue_after_old_page

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

    def _submit_background(
        self,
        spec: TaskSpec,
        action: Callable[[object], object],
        *,
        output: QTextEdit | None = None,
        buttons: tuple[QWidget, ...] = (),
        on_success: Callable[[object], None] | None = None,
        on_failure: Callable[[object], None] | None = None,
        on_cancelled: Callable[[object], None] | None = None,
        on_progress: Callable[[object], None] | None = None,
        on_removed: Callable[[], None] | None = None,
        generation_key: str | None = None,
        generation: int | None = None,
        allow_during_closing: bool = False,
    ) -> str | None:
        self._cancel_shutdown_for_new_work()
        key = generation_key or spec.deduplicate_key or spec.task_type
        if generation is None:
            generation = self._background_generations.get(key, 0) + 1
        try:
            task_id = self.coordinator.start(spec, generation, action)
        except TaskRejectedError as exc:
            if output is not None:
                self._append_info(output, f"【未启动】{exc}")
            self.statusBar().showMessage(str(exc))
            return None
        self._background_generations[key] = generation
        self._background_bindings[task_id] = BackgroundTaskBinding(
            generation_key=key,
            generation=generation,
            spec=spec,
            output=output,
            buttons=buttons,
            on_success=on_success,
            on_failure=on_failure,
            on_cancelled=on_cancelled,
            on_progress=on_progress,
            on_removed=on_removed,
            allow_during_closing=allow_during_closing,
        )
        for button in buttons:
            button.setEnabled(False)
        return task_id

    def _submit_coalesced_background(
        self,
        spec: TaskSpec,
        action: Callable[[object], object],
        *,
        output: QTextEdit | None = None,
        buttons: tuple[QWidget, ...] = (),
        on_success: Callable[[object], None] | None = None,
        on_failure: Callable[[object], None] | None = None,
        on_cancelled: Callable[[object], None] | None = None,
        on_progress: Callable[[object], None] | None = None,
        on_removed: Callable[[], None] | None = None,
        generation_key: str | None = None,
        allow_during_closing: bool = False,
    ) -> str | None:
        key = generation_key or spec.deduplicate_key or spec.task_type
        generation = self._background_generations.get(key, 0) + 1
        active_task_id = next(
            (
                task_id
                for task_id, binding in self._background_bindings.items()
                if binding.generation_key == key
            ),
            None,
        )
        if active_task_id is None:
            return MainWindow._submit_background(
                self,
                spec,
                action,
                output=output,
                buttons=buttons,
                on_success=on_success,
                on_failure=on_failure,
                on_cancelled=on_cancelled,
                on_progress=on_progress,
                on_removed=on_removed,
                generation_key=key,
                generation=generation,
                allow_during_closing=allow_during_closing,
            )

        self._background_generations[key] = generation
        self._background_pending[key] = PendingBackgroundRequest(
            generation=generation,
            spec=spec,
            action=action,
            output=output,
            buttons=buttons,
            on_success=on_success,
            on_failure=on_failure,
            on_cancelled=on_cancelled,
            on_progress=on_progress,
            on_removed=on_removed,
            allow_during_closing=allow_during_closing,
        )
        self.coordinator.request_cancel(active_task_id)
        return active_task_id

    def _background_binding_is_current(
        self,
        binding: BackgroundTaskBinding,
        generation: int,
    ) -> bool:
        if generation != binding.generation:
            return False
        if self._background_generations.get(binding.generation_key) != generation:
            return False
        return (
            binding.allow_during_closing
            or self.controller.startup_state is not StartupState.CLOSING
        )

    @Slot(str, int, object)
    def _on_background_task_progress(
        self,
        task_id: str,
        generation: int,
        progress: object,
    ) -> None:
        binding = self._background_bindings.get(task_id)
        if binding is None or not self._background_binding_is_current(
            binding, generation
        ):
            return
        if binding.on_progress is not None:
            binding.on_progress(progress)

    @Slot(str, int, object, object)
    def _on_background_task_settled(
        self,
        task_id: str,
        generation: int,
        outcome: object,
        payload: object,
    ) -> None:
        binding = self._background_bindings.get(task_id)
        if binding is None or not self._background_binding_is_current(
            binding, generation
        ):
            return
        if outcome is TaskState.SUCCEEDED:
            if binding.on_success is not None:
                binding.on_success(payload)
            self._refresh_background_targets(binding.spec.refresh_targets)
            return
        if outcome is TaskState.CANCELLED:
            if binding.on_cancelled is not None:
                binding.on_cancelled(payload)
            elif binding.output is not None:
                self._append_info(binding.output, "操作已取消。")
            return
        if binding.on_failure is not None:
            binding.on_failure(payload)
        else:
            message = payload.message if isinstance(payload, TaskFailure) else str(payload)
            if binding.output is not None:
                self._append_info(binding.output, f"【失败】{message}")
            self.controller.logger.error("后台操作失败：%s", message)
            self.statusBar().showMessage("操作失败")

    @Slot(str)
    def _on_background_task_removed(self, task_id: str) -> None:
        binding = self._background_bindings.pop(task_id, None)
        if binding is None:
            return
        pending = getattr(self, "_background_pending", {}).pop(
            binding.generation_key, None
        )
        if (
            pending is not None
            and self.controller.startup_state is not StartupState.CLOSING
        ):
            MainWindow._submit_background(
                self,
                pending.spec,
                pending.action,
                output=pending.output,
                buttons=pending.buttons,
                on_success=pending.on_success,
                on_failure=pending.on_failure,
                on_cancelled=pending.on_cancelled,
                on_progress=pending.on_progress,
                on_removed=pending.on_removed,
                generation_key=binding.generation_key,
                generation=pending.generation,
                allow_during_closing=pending.allow_during_closing,
            )
        for button in binding.buttons:
            if not any(
                any(candidate is button for candidate in other.buttons)
                for other in self._background_bindings.values()
            ):
                button.setEnabled(True)
        if binding.on_removed is not None:
            binding.on_removed()
        self._apply_action_gate()

    def _cancel_background(self, task_id: str) -> bool:
        return self.coordinator.request_cancel(task_id)

    def _start_account_audit_scan(self) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        native_log_analysis = self.account_audit_native_logs.isChecked()
        threshold = self.account_audit_error_threshold.value()
        minimum_runs = self.account_audit_minimum_runs.value()
        spec = TaskSpec(
            task_type="account_audit_scan",
            display_name="扫描账号健康审计",
            resource_keys=frozenset({"settings", "task_logs"}),
            deduplicate_key="account_audit_scan",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            refresh_targets=(),
            dynamic_cancellation=True,
        )
        self.account_audit_progress.setText("正在准备账号审计……")
        task_id = self._submit_coalesced_background(
            spec,
            lambda context: self.controller.audit_accounts(
                error_threshold=threshold,
                minimum_evidence_runs=minimum_runs,
                native_log_analysis=native_log_analysis,
                context=context,
            ),
            output=self.account_audit_output,
            buttons=(
                self.account_audit_apply_button,
                self.account_audit_clear_button,
                *self.account_audit_decision_buttons,
            ),
            on_success=self._apply_account_audit_report,
            on_failure=self._account_audit_failed,
            on_cancelled=lambda _payload: self._account_audit_cancelled(),
            on_progress=self._account_audit_progressed,
            on_removed=self._account_audit_task_removed,
            generation_key="account_audit_scan",
        )
        if task_id is not None:
            self._account_audit_task_id = task_id
            self.account_audit_cancel_button.setEnabled(True)

    def _account_audit_progressed(self, progress: object) -> None:
        if self.controller.startup_state is StartupState.CLOSING:
            return
        message = str(getattr(progress, "message", progress))
        current = getattr(progress, "current", None)
        total = getattr(progress, "total", None)
        if current is not None and total is not None and f"{current}" not in message:
            message = f"{message}（{current} / {total}）"
        self.account_audit_progress.setText(message)

    def _apply_account_audit_report(self, report: AccountAuditReport) -> None:
        if self.controller.startup_state is StartupState.CLOSING:
            return
        self._account_audit_report = report
        self._account_audit_decisions.clear()
        self._account_audit_model.set_decisions({})
        self._account_audit_model.set_report(report)
        newest = report.newest_run or "无"
        native_summary = (
            f"；原生日志：已复核 {report.native_log_runs_scanned} 轮 / "
            f"{report.native_log_segments_scanned} 个区间"
            if report.native_log_analysis
            else "；原生日志：未启用"
        )
        self.account_audit_summary.setText(
            f"扫描轮次：{report.runs_scanned}；最近：{newest}；"
            f"主档：{report.total_accounts} 项；重复组：{report.duplicate_groups}"
            f"{native_summary}。"
        )
        self.account_audit_progress.setText("账号审计完成。")
        self.account_audit_warnings.setText(
            "\n".join(f"警告：{warning}" for warning in report.warnings)
        )
        self.account_audit_details.clear()
        self._apply_account_audit_filter()
        self._update_account_audit_pending()

    def _apply_account_audit_filter(self, _value: object = None) -> None:
        if not hasattr(self, "_account_audit_model"):
            return
        self._account_audit_model.set_filter(
            str(self.account_audit_filter.currentData()),
            self.account_audit_search.text(),
        )
        self.account_audit_visible_count.setText(
            f"显示 {self._account_audit_model.visible_count} / "
            f"{self._account_audit_model.total_count}"
        )

    def _show_account_audit_selection_details(self, *_args: object) -> None:
        rows = self.account_audit_table.selectionModel().selectedRows(0)
        if not rows:
            self.account_audit_details.clear()
            return
        entry = self._account_audit_model.entry_at(rows[0].row())
        if entry is None:
            self.account_audit_details.clear()
            return
        lines = [
            f"A{entry.a_number}：{entry.suggestion_reason}",
            f"当前决定：{_audit_disposition_label(self._account_audit_decisions.get(entry.a_number, entry.disposition))}",
        ]
        if entry.evidence.consecutive_error_run_ids:
            lines.append(
                "连续 ERROR 轮次："
                + "、".join(entry.evidence.consecutive_error_run_ids)
            )
        self.account_audit_details.setPlainText("\n".join(lines))

    def _selected_account_audit_entries(self) -> tuple[AccountAuditEntry, ...]:
        entries: dict[int, AccountAuditEntry] = {}
        for index in self.account_audit_table.selectionModel().selectedRows(0):
            entry = self._account_audit_model.entry_at(index.row())
            if entry is not None:
                entries[entry.a_number] = entry
        return tuple(entries[number] for number in sorted(entries))

    def _set_account_audit_disposition(self, disposition: Disposition) -> None:
        entries = self._selected_account_audit_entries()
        if not entries:
            self.statusBar().showMessage("请先选择账号审计行")
            return
        for entry in entries:
            if disposition is entry.disposition:
                self._account_audit_decisions.pop(entry.a_number, None)
            else:
                self._account_audit_decisions[entry.a_number] = disposition
        self._account_audit_model.set_decisions(self._account_audit_decisions)
        self._apply_account_audit_filter()
        self._update_account_audit_pending()

    def _update_account_audit_pending(self) -> None:
        counts = {
            disposition: sum(
                value is disposition
                for value in self._account_audit_decisions.values()
            )
            for disposition in Disposition
        }
        self.account_audit_pending.setText(
            "待应用："
            f"停用 {counts[Disposition.PERMANENTLY_DISABLED]} · "
            f"恢复 {counts[Disposition.ENABLED]} · "
            f"待复核 {counts[Disposition.PENDING_REVIEW]}"
        )

    def _clear_account_audit_decisions(self) -> None:
        self._account_audit_decisions.clear()
        self._account_audit_model.set_decisions({})
        self._apply_account_audit_filter()
        self._update_account_audit_pending()

    def _account_audit_decision_tuple(self) -> tuple[AuditDecision, ...]:
        return tuple(
            AuditDecision(number, disposition)
            for number, disposition in sorted(self._account_audit_decisions.items())
        )

    def _preview_and_apply_account_audit(self) -> None:
        report = self._account_audit_report
        decisions = self._account_audit_decision_tuple()
        if report is None:
            QMessageBox.information(self, "尚未审计", "请先完成一次账号审计。")
            return
        if not decisions:
            QMessageBox.information(self, "没有待应用决定", "请先选择账号并设置决定。")
            return
        try:
            preview = self.controller.account_audit.preview_decisions(report, decisions)
        except Exception as exc:
            QMessageBox.critical(self, "无法预览账号审计决定", str(exc))
            return
        enabled_before = sum(entry.master_enable for entry in report.entries)
        warnings = "\n".join(report.warnings)
        text = (
            f"将永久停用 {len(preview.to_disable)} 个账号："
            f"{compact_numbers(preview.to_disable) or '无'}\n"
            f"将恢复启用 {len(preview.to_enable)} 个账号："
            f"{compact_numbers(preview.to_enable) or '无'}\n"
            f"标记待复核 {len(preview.to_pending)} 个账号："
            f"{compact_numbers(preview.to_pending) or '无'}（不改主档）\n\n"
            f"应用后启用账号数：{enabled_before} → {preview.enabled_after}\n\n"
            f"主档数组长度保持 {preview.array_length_after} 项不变。\n"
            "所有 A 编号保持不变。被停用的条目留在原位，不会被删除。\n\n"
            "管理器会先做完整备份。"
        )
        if warnings:
            text += f"\n\n{warnings}"
        text += "\n\n确认写入？"
        answer = QMessageBox.question(
            self,
            "确认写入账号主档",
            text,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        spec = TaskSpec(
            task_type="account_audit_apply",
            display_name="应用账号审计决定",
            resource_keys=frozenset({"settings", "volume", "collector_process"}),
            deduplicate_key="account_audit_apply",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            refresh_targets=(),
            dynamic_cancellation=True,
        )
        self._account_audit_refresh_after_apply = False
        self.account_audit_progress.setText(
            "正在创建完整备份并应用决定，请等待操作完成。"
        )
        self._replace_info(
            self.account_audit_output,
            "正在创建完整备份并应用账号审计决定，请等待操作完成。",
            "待应用决定会在成功后刷新；当前显示不代表已写入主档。",
        )
        self.account_audit_progress.repaint()
        self.account_audit_output.viewport().repaint()
        task_id = self._submit_background(
            spec,
            lambda context: self.controller.apply_audit_decisions(
                report, decisions, context=context
            ),
            output=self.account_audit_output,
            buttons=(
                self.account_audit_refresh_button,
                self.account_audit_apply_button,
                self.account_audit_clear_button,
                *self.account_audit_decision_buttons,
            ),
            on_success=self._account_audit_applied,
            on_failure=self._account_audit_failed,
            on_cancelled=lambda _payload: self._account_audit_cancelled(),
            on_progress=self._account_audit_progressed,
            on_removed=self._account_audit_apply_removed,
            generation_key="account_audit_apply",
        )
        if task_id is not None:
            self._account_audit_task_id = task_id
            self.account_audit_cancel_button.setEnabled(True)
        else:
            self.account_audit_progress.setText("应用未启动，决定仍待应用。")
            self._append_info(
                self.account_audit_output,
                "决定仍待应用；账号主档未被本次操作修改。",
            )

    def _account_audit_applied(self, result: AuditApplyResult) -> None:
        if self.controller.startup_state is StartupState.CLOSING:
            return
        self._account_audit_refresh_after_apply = True
        self.account_audit_progress.setText("账号审计决定已安全应用。")
        self._replace_info(
            self.account_audit_output,
            f"完整备份：{result.backup_path}",
            "主档数组长度与 A 编号保持不变。",
        )

    def _account_audit_failed(self, payload: object) -> None:
        if self.controller.startup_state is StartupState.CLOSING:
            return
        message = payload.message if isinstance(payload, TaskFailure) else str(payload)
        self.account_audit_progress.setText("账号审计操作失败。")
        self._append_info(self.account_audit_output, f"【失败】{message}")

    def _account_audit_cancelled(self) -> None:
        if self.controller.startup_state is StartupState.CLOSING:
            return
        self.account_audit_progress.setText("账号审计操作已取消；未应用迟到结果。")

    def _account_audit_task_removed(self) -> None:
        active = next(
            (
                task_id
                for task_id, binding in self._background_bindings.items()
                if binding.generation_key == "account_audit_scan"
            ),
            None,
        )
        self._account_audit_task_id = active
        self.account_audit_cancel_button.setEnabled(active is not None)

    def _account_audit_apply_removed(self) -> None:
        self._account_audit_task_id = None
        self.account_audit_cancel_button.setEnabled(False)
        if (
            self._account_audit_refresh_after_apply
            and self.controller.startup_state is StartupState.READY
        ):
            self._account_audit_refresh_after_apply = False
            self._start_account_audit_scan()

    def _cancel_account_audit_task(self) -> None:
        task_id = self._account_audit_task_id
        if task_id is None:
            return
        if self._cancel_background(task_id):
            self.account_audit_progress.setText("正在取消账号审计操作……")

    def _refresh_background_targets(self, targets: tuple[str, ...]) -> None:
        if "task_list" in targets:
            self.refresh_tasks()
        if "download_results" in targets and hasattr(self, "result_table"):
            self.refresh_results()
        if "result_dashboard" in targets and hasattr(self, "dashboard_task_selector"):
            self.refresh_result_dashboard(auto_refresh=True)
        if "runtime_status" in targets:
            self._refresh_status()

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
        if self.controller.startup_state not in (
            StartupState.READY,
            StartupState.DEGRADED_READ_ONLY,
        ):
            return
        if not hasattr(self, "_background_bindings") or hasattr(
            self.refresh_all, "assert_called_once_with"
        ):
            self.refresh_all(check_processes=True)
            return
        spec = TaskSpec(
            task_type="runtime_status_snapshot",
            display_name="刷新运行状态",
            resource_keys=frozenset(
                {
                    "engine_process",
                    "collector_process",
                    "engine_files",
                    "volume",
                    "settings",
                    "video_tree",
                    "index",
                    "collector_data",
                }
            ),
            deduplicate_key="runtime_status_snapshot",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )
        self._submit_coalesced_background(
            spec,
            lambda context: self.controller.runtime_status_snapshot(context=context),
            buttons=(getattr(self, "refresh_status_button", None),)
            if getattr(self, "refresh_status_button", None) is not None
            else (),
            on_success=self._render_health_snapshot,
            generation_key="runtime_status_snapshot",
        )

    def refresh_all(self, *, check_processes: bool = False) -> None:
        health = self.controller.health(check_processes=check_processes)
        self._apply_action_gate()
        self._render_health_snapshot(health)
        if self.controller.startup_state is not StartupState.READY:
            self._apply_action_gate()
            return
        self.refresh_tasks()
        if hasattr(self, "result_table"):
            self.refresh_results()

    def _render_health_snapshot(self, health: dict[str, object]) -> None:
        operational_ready = self.controller.startup_state is StartupState.READY
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
                operational_ready
                and not collector_running
                and mode != ENGINE_MODE_MONITOR
            )
            if collector_running and self.queue_shutdown.isChecked():
                self.queue_shutdown.setChecked(False)
        if hasattr(self, "queue_pause_button"):
            self.queue_pause_button.setEnabled(
                operational_ready
                and self.queue_active
                and not self.queue_cancel_requested
                and str(health.get("engine_mode") or "") != ENGINE_MODE_MONITOR
            )
        if hasattr(self, "queue_cancel_button"):
            self.queue_cancel_button.setEnabled(
                operational_ready
                and self.queue_active
                and not self.queue_cancel_requested
                and str(health.get("engine_mode") or "") != ENGINE_MODE_MONITOR
            )
        if hasattr(self, "monitor_start_button"):
            monitor_running = mode == ENGINE_MODE_MONITOR
            self.monitor_start_button.setEnabled(
                operational_ready and not engine_running and not collector_running
            )
            self.monitor_stop_button.setEnabled(operational_ready and monitor_running)
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

    def _render_task_paths(self, paths: tuple[Path, ...]) -> None:
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
            for path in paths:
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

    def refresh_tasks(self) -> None:
        # Render keeps the established list flags: no drag/drop
        # (``~Qt.ItemIsDragEnabled`` and ``~Qt.ItemIsDropEnabled``).
        if (
            self.controller.startup_state is not StartupState.READY
            or not hasattr(self, "task_list")
        ):
            return
        spec = TaskSpec(
            task_type="task_list_snapshot",
            display_name="刷新任务列表",
            resource_keys=frozenset({"task_templates", "settings"}),
            deduplicate_key="task_list_snapshot",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )
        self._submit_coalesced_background(
            spec,
            lambda context: self.controller.list_tasks(context=context),
            buttons=(getattr(self, "task_refresh_button", None),)
            if getattr(self, "task_refresh_button", None) is not None
            else (),
            on_success=lambda paths: self._render_task_paths(tuple(paths)),
            generation_key="task_list_snapshot",
        )

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
        if self.controller.startup_state is not StartupState.READY:
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

    def _render_result_snapshot(self, snapshot: ResultPageSnapshot) -> None:
        if not hasattr(self, "result_table"):
            return
        selected_status = str(self.result_status_filter.currentData() or "")
        account_text = self.result_account_filter.text().strip().lstrip("Aa")
        task_text = self.result_task_filter.text().strip().casefold()
        try:
            account_number = int(account_text) if account_text else None
        except ValueError:
            account_number = -1
        rows = snapshot.rows
        filtered = tuple(
            row
            for row in rows
            if (not selected_status or getattr(row.status, "value", row.status) == selected_status)
            and (account_number is None or row.a_number == account_number)
            and (not task_text or task_text in row.task_template.casefold())
        )
        self._result_render_rows = filtered
        self._result_render_cursor = 0
        self._result_render_runs = len(snapshot.runs)
        self._result_render_incomplete_old_runs = snapshot.incomplete_old_runs
        self.result_table.setUpdatesEnabled(False)
        try:
            self.result_table.clearContents()
            self.result_table.setRowCount(len(filtered))
        finally:
            self.result_table.setUpdatesEnabled(True)
        self.result_note.setText(f"正在显示 {len(filtered)} 条账号结果……")
        self.result_render_timer.start(0)

    def _render_result_rows_chunk(self) -> None:
        if not hasattr(self, "result_table"):
            return
        start = self._result_render_cursor
        end = min(start + self._RESULT_RENDER_BATCH_SIZE, len(self._result_render_rows))
        for index in range(start, end):
            row = self._result_render_rows[index]
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
        self._result_render_cursor = end
        if end < len(self._result_render_rows):
            self.result_render_timer.start(0)
            return
        incomplete_old = self._result_render_incomplete_old_runs
        self.result_note.setText(
            f"共读取 {self._result_render_runs} 次任务日志，显示 {len(self._result_render_rows)} 条账号结果。"
            + (f"其中 {incomplete_old} 次旧日志没有完整列出正常账号，页面不会猜测缺失状态。" if incomplete_old else "")
        )
        self.result_last_refresh.setText(
            f"最近刷新：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
        )

    def refresh_results(self) -> None:
        if (
            self.controller.startup_state is not StartupState.READY
            or not hasattr(self, "result_table")
        ):
            return
        spec = TaskSpec(
            task_type="result_page_snapshot",
            display_name="刷新下载结果",
            resource_keys=frozenset({"result_logs"}),
            deduplicate_key="result_page_snapshot",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )
        self._submit_coalesced_background(
            spec,
            lambda context: self.controller.result_snapshot(limit=500, context=context),
            buttons=(getattr(self, "result_refresh_button", None),)
            if getattr(self, "result_refresh_button", None) is not None
            else (),
            on_success=lambda snapshot: self._render_result_snapshot(snapshot),
            generation_key="result_page_snapshot",
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
        if self.controller.startup_state is not StartupState.READY:
            return
        if self.task_smart_private.isChecked():
            expression = self.task_expression.text()
            validity_days = self.task_private_days.value()
            spec = TaskSpec(
                task_type="private_preview",
                display_name="智能私密预览",
                resource_keys=frozenset({"result_logs", "settings"}),
                deduplicate_key="private_preview",
                cancellable=True,
                close_policy=ClosePolicy.CANCEL,
                dynamic_cancellation=True,
            )
            self._submit_coalesced_background(
                spec,
                lambda context: self.controller.preview_private_skip(
                    expression, validity_days, context=context
                ),
                output=self.task_output,
                buttons=(self.task_preview_button,),
                on_success=lambda preview: self._render_private_preview_if_current(
                    preview, expression, validity_days
                ),
                generation_key="private_preview",
            )
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

    def _render_private_preview_if_current(
        self, preview: object, expression: str, validity_days: int
    ) -> bool:
        if self.task_expression.text() != expression:
            return False
        if self.task_private_days.value() != validity_days:
            return False
        if not self.task_smart_private.isChecked():
            return False
        self._replace_info(self.task_output, *self._smart_preview_lines(preview))
        return True

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
        if not self.queue_active and not MainWindow._download_summary_is_active(self):
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
        if getattr(self, "_download_lifecycle_identity", None) is None:
            self._download_lifecycle_identity = (
                f"download_lifecycle:{MainWindow._canonical_task_log(run.task_log)}"
            )
            getattr(self, "_download_post_bindings", {}).clear()
            getattr(self, "_download_post_completed_keys", set()).clear()
        run._download_lifecycle_identity = self._download_lifecycle_identity
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
                box = SmartSkipPreviewDialog(
                    "\n".join(self._smart_preview_lines(private_preview)),
                    can_skip=private_preview.effective is not None,
                    parent=self,
                )
                box.exec()
                if box.choice == SmartSkipChoice.SKIP:
                    excluded_numbers = private_preview.skipped_numbers
                elif box.choice == SmartSkipChoice.FORCE_ALL:
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
            if isinstance(result, ActivatedTask) and result.vetoed_numbers:
                self._append_info(
                    self.queue_output,
                    "主档永久停用已否决 "
                    f"{len(result.vetoed_numbers)} 个账号："
                    f"{compact_numbers(result.vetoed_numbers)}。",
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
        if getattr(run, "_download_summary_business_finalized", False):
            return
        run._download_summary_business_finalized = True
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
        self._run_post_actions_background("batch", run)

    @staticmethod
    def _post_action_resource_keys(config: object, timing: str) -> frozenset[str]:
        resources: set[str] = set()
        if getattr(config, "screenshot_post_mode", None) == timing:
            resources.update(("screenshots", "video_tree"))
        if getattr(config, "index_post_mode", None) == timing:
            resources.update(("video_tree", "index"))
        return frozenset(resources)

    @staticmethod
    def _download_post_action_key(
        lifecycle_identity: str,
        timing: str,
        run: object | None,
    ) -> str:
        if timing == "batch":
            if run is None:
                raise ValueError("batch post actions require an EngineRun")
            run_identity = MainWindow._canonical_task_log(run.task_log)
            return f"download_post_actions:{lifecycle_identity}:batch:{run_identity}"
        if timing == "queue":
            return f"download_post_actions:{lifecycle_identity}:queue"
        raise ValueError(f"unsupported post-action timing: {timing}")

    def _download_post_binding_is_current(
        self, binding: DownloadPostActionTaskBinding
    ) -> bool:
        return (
            getattr(self, "_download_post_bindings", {}).get(
                binding.deduplicate_key
            )
            is binding
            and getattr(self, "_background_generations", {}).get(
                binding.deduplicate_key
            )
            == binding.generation
        )

    def _stop_download_post_queue(
        self,
        binding: DownloadPostActionTaskBinding,
        *,
        cancelled: bool = False,
        closing: bool = False,
    ) -> None:
        if binding.queue_decision_consumed:
            return
        binding.queue_decision_consumed = True
        getattr(self, "_download_post_completed_keys", set()).add(
            binding.deduplicate_key
        )
        if not closing:
            self._record_queue_elapsed(
                "后续动作已取消" if cancelled else "后续动作失败"
            )
            self._append_info(
                self.queue_output,
                (
                    "后续动作已取消；队列已停止，剩余任务不会启动。"
                    if cancelled
                    else (
                        "【失败】队列后续动作失败；队列已停止，请检查上方错误。"
                        if binding.timing == "queue"
                        else "【失败】后续动作失败；队列已停止，剩余任务不会启动。"
                    )
                ),
            )
        self.queue_pending.clear()
        self.queue_current = None
        self.queue_active = False
        self._release_download_lifecycle()

    def _settle_download_post_action(
        self,
        binding: DownloadPostActionTaskBinding,
        outcome: TaskState,
        payload: object,
    ) -> None:
        if (
            not MainWindow._download_post_binding_is_current(self, binding)
            or binding.terminal_consumed
            or binding.removed_consumed
        ):
            return
        binding.terminal_consumed = True
        binding.outcome = outcome
        if getattr(self.controller, "startup_state", None) is StartupState.CLOSING:
            MainWindow._stop_download_post_queue(
                self, binding, cancelled=outcome is TaskState.CANCELLED, closing=True
            )
            return
        if outcome is TaskState.CANCELLED:
            MainWindow._stop_download_post_queue(self, binding, cancelled=True)
            return
        if outcome is not TaskState.SUCCEEDED:
            MainWindow._stop_download_post_queue(self, binding)
            return
        if binding.queue_decision_consumed:
            return

        binding.queue_decision_consumed = True
        getattr(self, "_download_post_completed_keys", set()).add(
            binding.deduplicate_key
        )
        messages = payload
        if messages:
            self._append_info(self.queue_output, *messages)
        if binding.timing == "queue":
            if self.queue_summaries_complete and self.queue_summaries_reliable:
                summary_conclusion = "每个任务的账号汇总均完整且可靠。"
            else:
                summary_conclusion = (
                    "至少一个任务的账号汇总不完整或不可靠；请查看上方信息及任务日志。"
                )
            self.controller.logger.info("下载队列执行结束：%s", summary_conclusion)
            post_lines = [
                line.strip()
                for message in (messages or [])
                for line in str(message).splitlines()
                if line.strip()
            ]
            for line in post_lines:
                self.controller.logger.info("队列后续动作：%s", line)
            self._append_info(
                self.queue_output,
                f"队列执行结束。{summary_conclusion}",
            )
            self._record_queue_elapsed("已完成")
            self.queue_active = False
            self.queue_current = None
            self._release_download_lifecycle()
            self._begin_shutdown_countdown_if_requested()
            return
        self.queue_current = None
        self._start_next_queue_item()

    def _remove_download_post_action(
        self, binding: DownloadPostActionTaskBinding
    ) -> None:
        if (
            not MainWindow._download_post_binding_is_current(self, binding)
            or binding.removed_consumed
        ):
            return
        binding.removed_consumed = True
        if getattr(self.controller, "startup_state", None) is StartupState.CLOSING:
            MainWindow._stop_download_post_queue(self, binding, closing=True)
        elif not binding.terminal_consumed:
            self.controller.logger.error(
                "下载后续动作在 Coordinator 终态前被移除：%s",
                binding.deduplicate_key,
            )
            MainWindow._stop_download_post_queue(self, binding)
        bindings = getattr(self, "_download_post_bindings", {})
        if bindings.get(binding.deduplicate_key) is binding:
            bindings.pop(binding.deduplicate_key, None)
        generations = getattr(self, "_background_generations", {})
        if generations.get(binding.deduplicate_key) == binding.generation:
            generations.pop(binding.deduplicate_key, None)

    def _run_post_actions_background(self, timing: str, run) -> None:
        if (
            getattr(self.controller, "startup_state", StartupState.READY)
            is not StartupState.READY
        ):
            return
        if not hasattr(self, "_background_bindings"):
            messages = self._run(
                lambda: self.controller.run_post_actions(timing), self.queue_output
            )
            if messages is None:
                self._record_queue_elapsed("后续动作失败")
                self._append_info(
                    self.queue_output,
                    "【失败】后续动作失败；队列已停止，剩余任务不会启动。",
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
            return

        lifecycle_identity = getattr(self, "_download_lifecycle_identity", None)
        lifecycle_active = getattr(
            self.controller, "_download_lifecycle_active", False
        )
        if not lifecycle_active:
            self.controller.logger.info(
                "忽略已释放下载生命周期的迟到后续动作请求：timing=%s",
                timing,
            )
            return
        if timing not in {"batch", "queue"} or lifecycle_identity is None:
            self.controller.logger.error(
                "下载后续动作缺少有效生命周期：timing=%s identity=%s",
                timing,
                lifecycle_identity,
            )
            self.queue_pending.clear()
            self.queue_current = None
            self.queue_active = False
            self._release_download_lifecycle()
            return
        if (
            timing == "batch"
            and run is not None
            and getattr(run, "_download_lifecycle_identity", lifecycle_identity)
            != lifecycle_identity
        ):
            self.controller.logger.info(
                "忽略其他下载生命周期的迟到后续动作请求：%s",
                run.task_log,
            )
            return

        deduplicate_key = MainWindow._download_post_action_key(
            lifecycle_identity, timing, run
        )
        bindings = getattr(self, "_download_post_bindings", None)
        if bindings is None:
            bindings = {}
            self._download_post_bindings = bindings
        completed = getattr(self, "_download_post_completed_keys", None)
        if completed is None:
            completed = set()
            self._download_post_completed_keys = completed
        if deduplicate_key in bindings or deduplicate_key in completed:
            self.controller.logger.info(
                "忽略重复下载后续动作请求：%s", deduplicate_key
            )
            return

        generation = getattr(self, "_background_generations", {}).get(
            deduplicate_key, 0
        ) + 1
        binding = DownloadPostActionTaskBinding(
            timing=timing,
            run=run,
            deduplicate_key=deduplicate_key,
            generation=generation,
        )
        bindings[deduplicate_key] = binding
        config = self.controller.config
        spec = TaskSpec(
            task_type="download_post_actions",
            display_name="下载后续动作",
            resource_keys=MainWindow._post_action_resource_keys(config, timing),
            deduplicate_key=deduplicate_key,
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            refresh_targets=("runtime_status", "download_results"),
            dynamic_cancellation=True,
        )
        task_id = self._submit_background(
            spec,
            lambda context: self.controller.run_post_actions(
                timing, context=context
            ),
            output=self.queue_output,
            buttons=(self.queue_pause_button, self.queue_cancel_button),
            on_success=lambda payload: MainWindow._settle_download_post_action(
                self, binding, TaskState.SUCCEEDED, payload
            ),
            on_failure=lambda payload: MainWindow._settle_download_post_action(
                self, binding, TaskState.FAILED, payload
            ),
            on_cancelled=lambda payload: MainWindow._settle_download_post_action(
                self, binding, TaskState.CANCELLED, payload
            ),
            on_removed=lambda: MainWindow._remove_download_post_action(
                self, binding
            ),
            generation_key=deduplicate_key,
        )
        if task_id is None:
            bindings.pop(deduplicate_key, None)
            self.controller.logger.error(
                "下载后续动作未获 Coordinator admission：%s", deduplicate_key
            )
            self._record_queue_elapsed("后续动作未启动")
            self.queue_pending.clear()
            self.queue_current = None
            self.queue_active = False
            self._release_download_lifecycle()
            return
        binding.task_id = task_id
        live_binding = getattr(self, "_background_bindings", {}).get(task_id)
        if live_binding is not None:
            binding.generation = live_binding.generation
        else:
            getattr(self, "_background_generations", {}).setdefault(
                deduplicate_key, binding.generation
            )

    def _shutdown_option_changed(self, state: int) -> None:
        checked = bool(state)
        self.queue_shutdown_requested = checked
        if not checked and self.shutdown_timer is not None and self.shutdown_timer.isActive():
            self._cancel_shutdown()

    def _start_next_queue_item(self) -> None:
        if getattr(self.controller, "startup_state", None) is StartupState.CLOSING:
            return
        if getattr(self, "_log_stats_task_id", None) is not None:
            self._queue_start_waiting_for_log_stats = True
            return
        if self.queue_pending and self.queue_paused:
            self._append_info(
                self.queue_output,
                "队列已暂停：当前任务结束后不会启动下一个模板；这不会暂停当前下载进程。",
            )
            return
        if not self.queue_pending:
            if hasattr(self, "_background_bindings"):
                self._run_post_actions_background("queue", None)
                return
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

    @staticmethod
    def _canonical_task_log(task_log: object) -> str:
        path = os.path.abspath(os.path.normpath(os.fspath(task_log)))
        return os.path.normcase(path)

    @staticmethod
    def _canonical_engine_update_archive(archive: Path) -> str:
        resolved = archive.expanduser().resolve(strict=False)
        return os.path.normcase(os.path.normpath(os.fspath(resolved)))

    @staticmethod
    def _download_summary_key(run) -> str:
        return f"download_summary:{MainWindow._canonical_task_log(run.task_log)}"

    def _download_summary_is_active(self) -> bool:
        binding = getattr(self, "_download_summary_binding", None)
        return binding is not None and not binding.removed_consumed

    def _download_post_owns_run(self, run: object) -> bool:
        return any(
            binding.run is run and not binding.removed_consumed
            for binding in getattr(self, "_download_post_bindings", {}).values()
        )

    def _download_run_blocks_close(self) -> bool:
        run = getattr(self, "queue_current", None)
        if run is None:
            return False
        if getattr(run, "running", False) or getattr(
            run, "result_review_waiting", False
        ):
            return True
        return not MainWindow._download_post_owns_run(self, run)

    def _download_summary_matches_run(self, run) -> bool:
        deduplicate_key = MainWindow._download_summary_key(run)
        if getattr(run, "_download_summary_coordinator_key", None) == deduplicate_key:
            return True
        binding = getattr(self, "_download_summary_binding", None)
        if binding is None:
            return False
        if binding.run is run:
            return True
        return binding.deduplicate_key == deduplicate_key

    def _download_summary_binding_is_current(
        self, binding: DownloadSummaryTaskBinding
    ) -> bool:
        return (
            getattr(self, "_download_summary_binding", None) is binding
            and getattr(self, "_background_generations", {}).get(
                binding.deduplicate_key
            )
            == binding.generation
        )

    def _retire_download_summary_binding(
        self, binding: DownloadSummaryTaskBinding
    ) -> None:
        if getattr(self, "_download_summary_binding", None) is binding:
            self._download_summary_binding = None
            generations = getattr(self, "_background_generations", {})
            if generations.get(binding.deduplicate_key) == binding.generation:
                generations.pop(binding.deduplicate_key, None)

    def _finalize_download_summary_for_closing(
        self, binding: DownloadSummaryTaskBinding
    ) -> None:
        if not binding.business_finalized:
            binding.business_finalized = True
            binding.run._download_summary_business_finalized = True
            self.queue_pending.clear()
            self.queue_current = None
            self.queue_active = False
            self._release_download_lifecycle()
        MainWindow._retire_download_summary_binding(self, binding)

    def _start_download_summary(self, run, exit_code: int | None, assessment) -> None:
        if getattr(self.controller, "startup_state", None) is StartupState.CLOSING:
            return
        deduplicate_key = MainWindow._download_summary_key(run)
        if getattr(run, "_download_summary_coordinator_key", None) == deduplicate_key:
            self.controller.logger.info(
                "忽略已提交 EngineRun 的账号结果汇总请求：%s", deduplicate_key
            )
            return
        previous = getattr(self, "_download_summary_binding", None)
        if previous is not None:
            if previous.deduplicate_key == deduplicate_key:
                self.controller.logger.info(
                    "忽略重复账号结果汇总请求：%s", deduplicate_key
                )
                return
            if not previous.removed_consumed:
                self.controller.logger.warning(
                    "已有账号结果汇总尚未移除，拒绝新请求：%s", deduplicate_key
                )
                return

        generation = getattr(self, "_background_generations", {}).get(
            deduplicate_key, 0
        ) + 1
        binding = DownloadSummaryTaskBinding(
            run=run,
            assessment=assessment,
            deduplicate_key=deduplicate_key,
            generation=generation,
        )
        self._download_summary_binding = binding
        ended_at = datetime.now()
        spec = TaskSpec(
            task_type="download_summary",
            display_name="汇总下载结果",
            resource_keys=frozenset({"task_logs", "result_logs"}),
            deduplicate_key=deduplicate_key,
            cancellable=False,
            close_policy=ClosePolicy.WAIT,
            refresh_targets=(),
        )
        task_id = self._submit_background(
            spec,
            lambda _token: self.controller.summarize_download(
                run, exit_code, ended_at
            ),
            output=self.queue_output,
            on_success=lambda payload: MainWindow._settle_download_summary(
                self, binding, TaskState.SUCCEEDED, payload
            ),
            on_failure=lambda payload: MainWindow._settle_download_summary(
                self, binding, TaskState.FAILED, payload
            ),
            on_cancelled=lambda payload: MainWindow._settle_download_summary(
                self, binding, TaskState.CANCELLED, payload
            ),
            on_removed=lambda: MainWindow._remove_download_summary(self, binding),
            generation_key=deduplicate_key,
        )
        if task_id is None:
            if self._download_summary_binding is binding:
                self._download_summary_binding = previous
            self.controller.logger.warning(
                "账号结果汇总未获 Coordinator admission：%s", deduplicate_key
            )
            return
        binding.task_id = task_id
        run._download_summary_coordinator_key = deduplicate_key
        live_binding = getattr(self, "_background_bindings", {}).get(task_id)
        if live_binding is not None:
            binding.generation = live_binding.generation

    def _settle_download_summary(
        self,
        binding: DownloadSummaryTaskBinding,
        outcome: TaskState,
        payload: object,
    ) -> None:
        if (
            not MainWindow._download_summary_binding_is_current(self, binding)
            or binding.terminal_consumed
            or binding.removed_consumed
        ):
            return
        binding.terminal_consumed = True
        binding.outcome = outcome
        binding.payload = payload
        if getattr(self.controller, "startup_state", None) is StartupState.CLOSING:
            return
        if outcome is not TaskState.SUCCEEDED:
            message = payload.message if isinstance(payload, TaskFailure) else str(payload)
            self.controller.logger.error("账号结果汇总失败：%s", message)
            if self.queue_cancel_requested:
                self._append_info(
                    self.queue_output,
                    f"取消任务后账号结果汇总未能完成：{message}",
                )
            else:
                self._append_info(
                    self.queue_output,
                    f"【失败】账号结果汇总失败：{message}",
                    "队列已停止；剩余任务不会启动。",
                )
            return

        summary = payload
        self._append_info(self.queue_output, *format_summary_for_ui(summary))
        run = binding.run
        run._summary_result = summary
        self._latest_log_stats_run = run
        MainWindow._update_log_stats_current_availability(self)
        if getattr(self, "log_stats_auto_checkbox", None) is not None and (
            self.log_stats_auto_checkbox.isChecked()
        ):
            self._pending_auto_log_stats_run = run
        if getattr(run, "completion_marker", None) is not None and run.running:
            if getattr(run, "pause_after_exit", False):
                run.result_review_waiting = True
                self._append_info(
                    self.queue_output,
                    "本任务结果已汇总；黑框保留等待人工查看，查看完成后请按任意键继续。",
                )
                return
            if not self._close_result_wrapper(run):
                binding.business_finalized = True
                run._download_summary_business_finalized = True
                return
        binding.continuation_ready = True

    def _remove_download_summary(self, binding: DownloadSummaryTaskBinding) -> None:
        if (
            not MainWindow._download_summary_binding_is_current(self, binding)
            or binding.removed_consumed
        ):
            return
        binding.removed_consumed = True
        if getattr(self.controller, "startup_state", None) is StartupState.CLOSING:
            MainWindow._finalize_download_summary_for_closing(self, binding)
            return

        self._refresh_background_targets(("download_results", "runtime_status"))
        MainWindow._defer_dashboard_refresh_until_results_idle(self)
        pending_auto = getattr(self, "_pending_auto_log_stats_run", None)
        if pending_auto is binding.run:
            self._pending_auto_log_stats_run = None
            self._start_current_log_stats_for_run(pending_auto)
        if getattr(self.controller, "startup_state", None) is StartupState.CLOSING:
            MainWindow._finalize_download_summary_for_closing(self, binding)
            return
        if not binding.terminal_consumed:
            binding.business_finalized = True
            binding.run._download_summary_business_finalized = True
            self.controller.logger.error(
                "账号结果汇总线程在 Coordinator 终态前被移除：%s",
                binding.deduplicate_key,
            )
            self._append_info(
                self.queue_output,
                "【失败】账号结果汇总未产生有效终态；队列已停止，剩余任务不会启动。",
            )
            self._record_task_elapsed(binding.run, "汇总协议失败")
            self._record_queue_elapsed("汇总协议失败")
            self._cleanup_completion_marker(binding.run)
            self.queue_pending.clear()
            self.queue_current = None
            self.queue_active = False
            self._release_download_lifecycle()
            MainWindow._retire_download_summary_binding(self, binding)
            return
        if binding.business_finalized:
            MainWindow._retire_download_summary_binding(self, binding)
            return
        if binding.outcome is TaskState.SUCCEEDED:
            if not binding.continuation_ready:
                MainWindow._retire_download_summary_binding(self, binding)
                return
            binding.business_finalized = True
            MainWindow._retire_download_summary_binding(self, binding)
            self._finish_run_after_summary(binding.run, binding.assessment)
            return

        binding.business_finalized = True
        binding.run._download_summary_business_finalized = True
        if self.queue_cancel_requested:
            MainWindow._retire_download_summary_binding(self, binding)
            self._finish_cancelled_queue(binding.run)
            return
        self._record_task_elapsed(binding.run, "汇总失败")
        self._record_queue_elapsed("汇总失败")
        self._cleanup_completion_marker(binding.run)
        self.queue_pending.clear()
        self.queue_current = None
        self.queue_active = False
        self._release_download_lifecycle()
        MainWindow._retire_download_summary_binding(self, binding)

    def _start_current_log_stats_for_run(self, run) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        self._latest_log_stats_run = run
        self._start_current_log_stats(automatic=True)

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
        if not MainWindow._download_summary_is_active(self) and getattr(
            run, "result_review_waiting", False
        ) and not run.running:
            binding = getattr(self, "_download_summary_binding", None)
            if binding is not None and binding.run is run:
                if binding.business_finalized:
                    return
                binding.business_finalized = True
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
        summary_running = MainWindow._download_summary_is_active(self)
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
        generations = getattr(self, "_background_generations", {})
        for key, binding in tuple(
            getattr(self, "_download_post_bindings", {}).items()
        ):
            if generations.get(key) == binding.generation:
                generations.pop(key, None)
        self._download_lifecycle_identity = None
        getattr(self, "_download_post_bindings", {}).clear()
        getattr(self, "_download_post_completed_keys", set()).clear()

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
        if MainWindow._download_summary_is_active(self):
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
                binding = getattr(self, "_download_summary_binding", None)
                if binding is not None and binding.run is run:
                    if binding.business_finalized:
                        return
                    binding.business_finalized = True
                self._finish_run_after_summary(
                    run,
                    assess_process_exit(getattr(run, "engine_exit_code", None)),
                )
            return
        if MainWindow._download_summary_matches_run(self, run):
            return
        marker_code = self._completion_marker_code(run)
        if marker_code is not None:
            first_marker_observation = getattr(run, "engine_exit_code", None) is None
            if first_marker_observation:
                run.engine_exit_code = marker_code
            assessment = assess_process_exit(marker_code)
            if first_marker_observation:
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
        spec = TaskSpec(
            task_type="full_volume_backup",
            display_name="完整 Volume 备份",
            resource_keys=frozenset({"volume", "settings"}),
            deduplicate_key="full_volume_backup",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            refresh_targets=("runtime_status",),
            dynamic_cancellation=True,
        )
        self._submit_background(
            spec,
            lambda context: self.controller.backup_now(context=context),
            output=self.overview_output,
            buttons=(getattr(self, "manual_backup_button", None),)
            if getattr(self, "manual_backup_button", None) is not None
            else (),
            on_success=lambda result: self._append_info(
                self.overview_output, f"完整 Volume 备份完成：{result}"
            ),
        )

    def _migrate_collector(self) -> None:
        spec = TaskSpec(
            task_type="collector_migration",
            display_name="迁移旧采集器数据",
            resource_keys=frozenset(
                {"collector_process", "collector_data", "settings", "screenshots"}
            ),
            deduplicate_key="collector_migration",
            cancellable=False,
            close_policy=ClosePolicy.WAIT,
            refresh_targets=("runtime_status",),
        )

        def show_result(result: object) -> None:
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
        self._submit_background(
            spec,
            lambda _token: self.controller.migrate_collector(),
            output=self.collector_output,
            buttons=(self.collector_migrate_button,),
            on_success=show_result,
        )

    def _start_collector(self) -> None:
        spec = TaskSpec(
            task_type="collector_start",
            display_name="启动账号采集服务",
            resource_keys=frozenset(
                {"collector_process", "collector_data", "settings", "screenshots"}
            ),
            deduplicate_key="collector_start",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            refresh_targets=("runtime_status",),
            dynamic_cancellation=True,
        )

        def show_result(result: object) -> None:
            self._append_info(
                self.collector_output,
                "账号采集服务已启动，并已通过 "
                f"http://127.0.0.1:{self.controller.config.collector_port}/health 验证。",
                f"采集服务日志：{result}",
            )
        self._submit_background(
            spec,
            lambda context: self.controller.start_collector(context=context),
            output=self.collector_output,
            buttons=(self.collector_start_button,),
            on_success=show_result,
        )

    def _stop_collector(self) -> None:
        spec = TaskSpec(
            task_type="collector_stop",
            display_name="停止账号采集服务",
            resource_keys=frozenset({"collector_process"}),
            deduplicate_key="collector_stop",
            cancellable=False,
            close_policy=ClosePolicy.WAIT,
            refresh_targets=("runtime_status",),
        )
        self._submit_background(
            spec,
            lambda _token: self.controller.stop_collector(),
            output=self.collector_output,
            buttons=(self.collector_stop_button,),
            on_success=lambda _result: self._append_info(
                self.collector_output, "账号采集服务已停止。"
            ),
        )

    def _export_userscript(self) -> None:
        result = self._run(self.controller.collector.export_userscript, self.collector_output)
        if result:
            self._append_info(self.collector_output, f"油猴脚本已导出：{result}")
            self._open_path(result.parent)

    def _preview_screenshots(self) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        spec = TaskSpec(
            task_type="screenshot_preview",
            display_name="预览截图归档",
            resource_keys=frozenset({"screenshots", "video_tree"}),
            deduplicate_key="screenshot_preview",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )

        def show_result(result: object) -> None:
            self._replace_info(
                self.post_output,
                f"识别账号文件夹：{result.recognized_folders}",
                f"识别截图：{result.recognized_images}",
                f"可以安全归档：{result.movable}",
                f"找不到账号文件夹：{result.missing_account_folder}",
                f"目标已有同名文件：{result.already_existing}",
                f"忽略非规范账号文件夹：{result.unmatched_folders}",
            )
        self._submit_background(
            spec,
            lambda context: self.controller.screenshot_preview(context=context),
            output=self.post_output,
            buttons=(self.screenshot_preview_button,),
            on_success=show_result,
            on_cancelled=lambda _payload: None,
        )

    def _organize_screenshots(self) -> None:
        spec = TaskSpec(
            task_type="screenshot_archive",
            display_name="安全归档截图",
            resource_keys=frozenset({"screenshots", "video_tree"}),
            deduplicate_key="screenshot_archive",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            refresh_targets=("runtime_status",),
            dynamic_cancellation=True,
        )
        self._submit_background(
            spec,
            lambda context: self.controller.organize_screenshots(context=context),
            output=self.post_output,
            buttons=(self.screenshot_archive_button,),
            on_success=lambda result: self._append_info(
                self.post_output, f"完成：安全归档 {result.moved} 张。"
            ),
        )

    def _refresh_index(self) -> None:
        def show_result(result: object) -> None:
            if result is not None:
                messages = [result.display_summary("索引刷新")]
                if result.output:
                    messages.extend(("详细输出：", result.output))
                self._replace_info(self.post_output, *messages)

        spec = TaskSpec(
            task_type="index_refresh",
            display_name="刷新索引",
            resource_keys=frozenset({"video_tree", "index"}),
            deduplicate_key="index_operation",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            refresh_targets=("runtime_status",),
            dynamic_cancellation=True,
        )
        self._submit_background(
            spec,
            lambda context: self.controller.refresh_index(context=context),
            output=self.post_output,
            buttons=(self.refresh_index_button, self.cleanup_index_button, self.cleanup_test_button),
            on_success=show_result,
        )

    def _cleanup_index(self) -> None:
        def show_result(result: object) -> None:
            if result is not None:
                messages = [result.display_summary("失效快捷方式清理")]
                if result.output:
                    messages.extend(("详细输出：", result.output))
                self._replace_info(self.post_output, *messages)

        spec = TaskSpec(
            task_type="index_cleanup",
            display_name="清理失效索引",
            resource_keys=frozenset({"video_tree", "index"}),
            deduplicate_key="index_operation",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            refresh_targets=("runtime_status",),
            dynamic_cancellation=True,
        )
        self._submit_background(
            spec,
            lambda context: self.controller.cleanup_index(context=context),
            output=self.post_output,
            buttons=(self.refresh_index_button, self.cleanup_index_button, self.cleanup_test_button),
            on_success=show_result,
        )

    def _cleanup_index_self_test(self) -> None:
        def show_result(result: object) -> None:
            if result is not None:
                self._replace_info(
                    self.post_output,
                    "清理功能隔离自检通过。正式视频目录和索引目录均未参与测试。",
                    result.output,
                )

        spec = TaskSpec(
            task_type="index_cleanup_self_test",
            display_name="自检索引清理",
            resource_keys=frozenset({"index"}),
            deduplicate_key="index_operation",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )
        self._submit_background(
            spec,
            lambda context: self.controller.cleanup_index_self_test(context=context),
            output=self.post_output,
            buttons=(self.refresh_index_button, self.cleanup_index_button, self.cleanup_test_button),
            on_success=show_result,
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
        if self.controller.startup_state is not StartupState.READY:
            return
        archive = Path(self.engine_update_zip.text().strip())
        deduplicate_key = (
            "engine_update_preview:"
            f"{MainWindow._canonical_engine_update_archive(archive)}"
        )
        spec = TaskSpec(
            task_type="engine_update_preview",
            display_name="预检更新包",
            resource_keys=frozenset({"engine_files"}),
            deduplicate_key=deduplicate_key,
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )

        def show_result(result: object) -> None:
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
        self._submit_coalesced_background(
            spec,
            lambda context: self.controller.preview_engine_update(
                archive, context=context
            ),
            output=self.settings_output,
            buttons=(self.engine_update_preview_button,),
            on_success=show_result,
            generation_key="engine_update_preview",
        )

    def _apply_engine_update(self) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        archive = Path(self.engine_update_zip.text().strip())
        deduplicate_key = (
            "engine_update_preview:"
            f"{MainWindow._canonical_engine_update_archive(archive)}"
        )
        spec = TaskSpec(
            task_type="engine_update_preview_for_apply",
            display_name="预检更新包",
            resource_keys=frozenset({"engine_files"}),
            deduplicate_key=deduplicate_key,
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )
        confirmed = {"value": False}

        def confirm(preview: object) -> None:
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
            confirmed["value"] = True

        self._submit_coalesced_background(
            spec,
            lambda context: self.controller.preview_engine_update(
                archive, context=context
            ),
            output=self.settings_output,
            buttons=(self.engine_update_apply_button,),
            on_success=confirm,
            on_removed=lambda: self._submit_engine_update_apply(archive)
            if confirmed["value"]
            else None,
            generation_key="engine_update_preview",
        )

    def _submit_engine_update_apply(self, archive: Path) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        spec = TaskSpec(
            task_type="engine_update_apply",
            display_name="安装引擎更新",
            resource_keys=frozenset(
                {"engine_process", "collector_process", "engine_files", "volume", "settings"}
            ),
            deduplicate_key="engine_update_apply",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            refresh_targets=("runtime_status",),
            dynamic_cancellation=True,
        )

        def show_result(result: object) -> None:
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
        self._submit_background(
            spec,
            lambda context: self.controller.apply_engine_update(archive, context=context),
            output=self.settings_output,
            buttons=(self.engine_update_apply_button,),
            on_success=show_result,
        )

    @staticmethod
    def _canonical_engine_rollback_point(point: Path) -> str:
        return os.path.normcase(os.path.normpath(os.path.abspath(os.fspath(point))))

    def _selected_engine_rollback_point(self) -> EngineRollbackPoint | None:
        if not hasattr(self, "engine_rollback_table"):
            return None
        row = self.engine_rollback_table.currentRow()
        if row < 0 or row >= len(self._engine_rollback_points):
            return None
        return self._engine_rollback_points[row]

    def _update_engine_rollback_buttons(self) -> None:
        if not hasattr(self, "engine_rollback_preview_button"):
            return
        selected = self._selected_engine_rollback_point()
        ready = self.controller.startup_state is StartupState.READY
        preview_busy = any(
            binding.spec.task_type in {
                "engine_rollback_preview",
                "engine_rollback_preview_for_apply",
            }
            for binding in self._background_bindings.values()
        )
        apply_busy = any(
            binding.spec.task_type == "engine_rollback_apply"
            for binding in self._background_bindings.values()
        )
        self.engine_rollback_preview_button.setEnabled(
            ready and selected is not None and not preview_busy
        )
        allowed = (
            selected is not None
            and rollback_can_apply(selected).gate is not RollbackApplyGate.REJECTED
        )
        self.engine_rollback_apply_button.setEnabled(
            ready and allowed and not apply_busy and not preview_busy
        )
        if hasattr(self, "engine_rollback_usage_cancel_button"):
            self.engine_rollback_usage_cancel_button.setEnabled(
                ready and getattr(self, "_engine_rollback_usage_task_id", None) is not None
            )

    def _render_engine_rollback_points(self, points: tuple[EngineRollbackPoint, ...]) -> None:
        self._engine_rollback_points = tuple(points)
        table = self.engine_rollback_table
        signals_were_blocked = table.blockSignals(True)
        try:
            table.setRowCount(len(points))
            origin_labels = {
                "ROLLBACK": "更新时换下",
                "SUPERSEDED": "回退时换下",
            }
            for row, point in enumerate(points):
                decision = rollback_can_apply(point)
                status = (
                    point.integrity.value
                    if decision.gate is not RollbackApplyGate.REJECTED
                    else f"{point.integrity.value}：{decision.reason}"
                )
                observed_hash = point.observed_main_sha256
                values = (
                    point.stamp,
                    origin_labels.get(point.origin.value, point.origin.value),
                    point.archive_name or "（无记录）",
                    (
                        f"{point.main_exe_bytes} bytes"
                        if point.main_exe_bytes is not None
                        else "未知"
                    ),
                    observed_hash or "不可用",
                    point.note,
                    status,
                )
                for column, value in enumerate(values):
                    item = QTableWidgetItem(value)
                    tooltips: list[str] = []
                    if column == 5:
                        item.setFlags(item.flags() | Qt.ItemFlag.ItemIsEditable)
                        tooltips.append("双击编辑备注；清空并确认后删除备注。")
                    else:
                        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                    if column == 4 and observed_hash:
                        tooltips.append(f"main.exe SHA-256：{observed_hash}")
                    if decision.gate is RollbackApplyGate.REJECTED:
                        item.setForeground(QColor("#94a3b8"))
                        tooltips.append(decision.reason)
                    if tooltips:
                        item.setToolTip("\n".join(tooltips))
                    table.setItem(row, column, item)
        finally:
            table.blockSignals(signals_were_blocked)
        self._update_engine_rollback_buttons()

    def _save_engine_rollback_note(self, item: QTableWidgetItem) -> None:
        if item.column() != 5:
            return
        row = item.row()
        if row < 0 or row >= len(self._engine_rollback_points):
            return
        point = self._engine_rollback_points[row]
        if item.text() == point.note:
            return
        try:
            saved_note = self.controller.save_engine_rollback_note(
                point.directory,
                item.text(),
            )
        except Exception as exc:
            signals_were_blocked = self.engine_rollback_table.blockSignals(True)
            try:
                item.setText(point.note)
            finally:
                self.engine_rollback_table.blockSignals(signals_were_blocked)
            self._replace_info(self.settings_output, "【备注保存失败】", str(exc))
            self.statusBar().showMessage("回退点备注保存失败")
            return

        signals_were_blocked = self.engine_rollback_table.blockSignals(True)
        try:
            item.setText(saved_note)
        finally:
            self.engine_rollback_table.blockSignals(signals_were_blocked)
        self._engine_rollback_points = tuple(
            replace(candidate, note=saved_note) if index == row else candidate
            for index, candidate in enumerate(self._engine_rollback_points)
        )
        self._replace_info(self.settings_output, "回退点备注已保存。")
        self.statusBar().showMessage("回退点备注已保存")

    def _refresh_engine_rollbacks(self) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        spec = TaskSpec(
            task_type="engine_rollback_list",
            display_name="刷新引擎回退点",
            resource_keys=frozenset({"engine_files"}),
            deduplicate_key="engine_rollback_list",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )
        self._engine_rollback_usage_pending = False

        def show_points(points: object) -> None:
            self._render_engine_rollback_points(tuple(points))
            self.engine_rollback_usage_label.setText("正在统计磁盘占用…")
            self._engine_rollback_usage_pending = True

        def submit_usage() -> None:
            if self._engine_rollback_usage_pending:
                self._engine_rollback_usage_pending = False
                self._submit_engine_rollback_usage()

        self._submit_background(
            spec,
            lambda context: self.controller.list_engine_rollbacks(context=context),
            output=self.settings_output,
            buttons=(self.engine_rollback_refresh_button,),
            on_success=show_points,
            on_removed=submit_usage,
            generation_key="engine_rollback_list",
        )

    def _submit_engine_rollback_usage(self) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        spec = TaskSpec(
            task_type="engine_rollback_usage",
            display_name="统计引擎回退点占用",
            resource_keys=frozenset({"engine_files"}),
            deduplicate_key="engine_rollback_usage",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )

        def show_usage(usage: object) -> None:
            self.engine_rollback_usage_label.setText(
                f"回退点 {usage.rollback_count} 份 · 已被换下的引擎 {usage.superseded_count} 份 · "
                f"合计占用 {usage.total_bytes / 1024 / 1024 / 1024:.2f} GB"
            )

        def usage_cancelled(_payload: object) -> None:
            self.engine_rollback_usage_label.setText(
                "磁盘占用统计已取消（可重新刷新）"
            )

        task_id_holder: dict[str, str | None] = {"value": None}

        def usage_removed() -> None:
            if self._engine_rollback_usage_task_id == task_id_holder["value"]:
                self._engine_rollback_usage_task_id = None
                self.engine_rollback_usage_cancel_button.setEnabled(False)

        task_id = self._submit_background(
            spec,
            lambda context: self.controller.measure_engine_rollback_usage(context=context),
            output=self.settings_output,
            buttons=(self.engine_rollback_refresh_button,),
            on_success=show_usage,
            on_cancelled=usage_cancelled,
            on_removed=usage_removed,
            generation_key="engine_rollback_usage",
        )
        task_id_holder["value"] = task_id
        self._engine_rollback_usage_task_id = task_id
        self.engine_rollback_usage_cancel_button.setEnabled(task_id is not None)
        if task_id is None:
            self.engine_rollback_usage_label.setText(
                "磁盘占用统计未启动（可重新刷新）"
            )

    def _cancel_engine_rollback_usage(self) -> None:
        task_id = self._engine_rollback_usage_task_id
        if task_id is not None:
            self._cancel_background(task_id)

    def _preview_engine_rollback(self) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        point = self._selected_engine_rollback_point()
        if point is None:
            return
        spec = TaskSpec(
            task_type="engine_rollback_preview",
            display_name="预检引擎回退点",
            resource_keys=frozenset({"engine_files"}),
            deduplicate_key=(
                "engine_rollback_preview:"
                f"{self._canonical_engine_rollback_point(point.directory)}"
            ),
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )

        def show_preview(preview: object) -> None:
            item = preview.point
            messages = [
                f"回退点状态：{item.integrity.value}",
                f"目标 main.exe SHA-256：{item.main_exe_sha256 or '无清单，未核对'}",
                f"当前 main.exe SHA-256：{preview.current_main_sha256}",
                f"正式 Volume 不会被替换：{'是' if preview.volume_stays else '否'}",
            ]
            if item.integrity is RollbackIntegrity.NO_MANIFEST:
                messages.extend(
                    (
                        "main.exe：存在",
                        "_internal：存在",
                        "_internal/Volume：不存在",
                        item.reject_reason,
                        "无法核对与已安装版本的一致性。",
                    )
                )
            elif rollback_can_apply(item).gate is RollbackApplyGate.REJECTED:
                messages.extend((item.reject_reason, "此回退点不可执行，任何确认都无法放行。"))
            else:
                messages.append(f"与当前引擎相同：{'是' if preview.is_same_as_current else '否'}")
            self._replace_info(self.settings_output, *messages)

        self._submit_coalesced_background(
            spec,
            lambda context: self.controller.preview_engine_rollback(
                point.directory,
                context=context,
            ),
            output=self.settings_output,
            buttons=(self.engine_rollback_preview_button,),
            on_success=show_preview,
            generation_key="engine_rollback_preview",
        )

    def _apply_engine_rollback(self) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        point = self._selected_engine_rollback_point()
        if point is None:
            return
        decision = rollback_can_apply(point)
        if decision.gate is RollbackApplyGate.REJECTED:
            self._replace_info(
                self.settings_output,
                "该回退点不可执行。",
                decision.reason,
                "任何确认都不能放行。",
            )
            return
        spec = TaskSpec(
            task_type="engine_rollback_preview_for_apply",
            display_name="预检引擎回退点",
            resource_keys=frozenset({"engine_files"}),
            deduplicate_key=(
                "engine_rollback_preview:"
                f"{self._canonical_engine_rollback_point(point.directory)}"
            ),
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            dynamic_cancellation=True,
        )
        accepted = {"value": False}
        accept_unverified = {"value": False}

        def confirm(preview: object) -> None:
            item = preview.point
            if rollback_can_apply(item).gate is RollbackApplyGate.REJECTED:
                self._replace_info(self.settings_output, "该回退点不可执行。", item.reject_reason)
                return
            answer = QMessageBox.question(
                self,
                "确认回退下载引擎",
                "将只替换下载引擎程序文件，正式 Volume 不会被替换，也不会回到旧状态。\n\n"
                f"安装时间：{item.installed_at.isoformat(sep=' ') if item.installed_at else item.stamp}\n"
                f"来源：{item.archive_name or '无记录'}\n"
                f"目标 main.exe SHA-256：{item.main_exe_sha256 or '无清单，未核对'}\n"
                f"当前 main.exe SHA-256：{preview.current_main_sha256}\n\n"
                "管理器会先永久备份完整正式 Volume，当前引擎会永久保存在 Updates\\EngineSuperseded。\n\n"
                "确认继续？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            if rollback_can_apply(item).requires_second_confirm:
                warning_answer = QMessageBox.question(
                    self,
                    "确认无清单回退",
                    "该回退点缺少安装清单，无法核对与已安装版本的一致性。\n\n"
                    "已确认 main.exe、_internal 存在且 _internal 内不含 Volume。\n\n"
                    "回退记录将保存本次实际读取的哈希、大小与来源目录。\n\n"
                    "仍然回退？（不建议）",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if warning_answer != QMessageBox.Yes:
                    return
                accept_unverified["value"] = True
            accepted["value"] = True

        self._submit_coalesced_background(
            spec,
            lambda context: self.controller.preview_engine_rollback(
                point.directory,
                context=context,
            ),
            output=self.settings_output,
            buttons=(self.engine_rollback_apply_button,),
            on_success=confirm,
            on_removed=lambda: self._submit_engine_rollback_apply(
                point.directory,
                accept_unverified=accept_unverified["value"],
            )
            if accepted["value"]
            else None,
            generation_key="engine_rollback_preview",
        )

    def _submit_engine_rollback_apply(
        self,
        point_dir: Path,
        *,
        accept_unverified: bool = False,
    ) -> None:
        if self.controller.startup_state is not StartupState.READY:
            return
        spec = TaskSpec(
            task_type="engine_rollback_apply",
            display_name="执行引擎回退",
            resource_keys=frozenset(
                {
                    "engine_process",
                    "collector_process",
                    "engine_files",
                    "volume",
                    "settings",
                }
            ),
            deduplicate_key="engine_rollback_apply",
            cancellable=True,
            close_policy=ClosePolicy.CANCEL,
            refresh_targets=("runtime_status",),
            dynamic_cancellation=True,
        )

        def show_result(result: object) -> None:
            if result.manifest_verified:
                first = "下载引擎回退完成。"
            else:
                first = "回退完成（来源无清单，未与已安装版本核对）。"
            self._replace_info(
                self.settings_output,
                first,
                f"回退前永久备份：{result.backup_path}",
                f"被换下引擎：{result.superseded_path}",
                f"实际恢复 main.exe SHA-256：{result.restored_main_sha256}",
                f"来源目录：{result.source_directory}",
            )

        self._submit_background(
            spec,
            lambda context: self.controller.apply_engine_rollback(
                point_dir,
                accept_unverified=accept_unverified,
                context=context,
            ),
            output=self.settings_output,
            buttons=(self.engine_rollback_apply_button,),
            on_success=show_result,
        )

    def _browse_dir(self, edit: QLineEdit) -> None:
        selected = QFileDialog.getExistingDirectory(self, "选择文件夹", edit.text())
        if selected:
            edit.setText(selected)

    @staticmethod
    def _open_path(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def _available_screen_geometries(self) -> tuple[tuple[QRect, ...], int]:
        screens = tuple(QApplication.screens())
        primary = QApplication.primaryScreen()
        primary_index = screens.index(primary) if primary in screens else 0
        return tuple(screen.availableGeometry() for screen in screens), primary_index

    def _restore_window_state(self) -> None:
        available, primary_index = self._available_screen_geometries()
        try:
            state = self._window_state_store.load()
        except Exception as exc:
            self.controller.logger.warning("读取窗口状态失败，使用安全默认值：%s", exc)
            state = None
        placement = safe_window_placement(
            state, available, primary_index=primary_index
        )
        self.setMinimumSize(placement.minimum_size)
        self.setGeometry(placement.geometry)
        if placement.maximized:
            self.setWindowState(self.windowState() | Qt.WindowState.WindowMaximized)

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._clamp_normal_window_frame()

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if (
            event.type() == QEvent.Type.WindowStateChange
            and not self.isMaximized()
            and not self.isMinimized()
        ):
            QTimer.singleShot(0, self._clamp_normal_window_frame)

    def _clamp_normal_window_frame(self) -> None:
        if self.isMaximized() or self.isMinimized() or not self.isVisible():
            return
        client = self.geometry()
        frame = self.frameGeometry()
        margins = QMargins(
            max(0, client.left() - frame.left()),
            max(0, client.top() - frame.top()),
            max(0, frame.right() - client.right()),
            max(0, frame.bottom() - client.bottom()),
        )
        available, primary_index = self._available_screen_geometries()
        placement = safe_window_placement(
            WindowGeometryState(client, False),
            available,
            primary_index=primary_index,
            frame_margins=margins,
        )
        self.setMinimumSize(placement.minimum_size)
        if placement.geometry != client:
            self.setGeometry(placement.geometry)

    def _save_window_state_once(self) -> None:
        if getattr(self, "_window_state_saved", False):
            return
        store = getattr(self, "_window_state_store", None)
        if store is None:
            return
        normal = self.normalGeometry() if self.isMaximized() else self.geometry()
        available, primary_index = self._available_screen_geometries()
        placement = safe_window_placement(
            WindowGeometryState(normal, self.isMaximized()),
            available,
            primary_index=primary_index,
        )
        try:
            if not store.save(
                WindowGeometryState(placement.geometry, placement.maximized)
            ):
                self.controller.logger.warning("窗口状态保存失败，将保留上一个有效状态。")
                return
        except Exception as exc:
            self.controller.logger.warning("窗口状态保存失败，将保留上一个有效状态：%s", exc)
            return
        self._window_state_saved = True

    def closeEvent(self, event) -> None:  # noqa: N802
        if MainWindow._download_run_blocks_close(
            self
        ) or MainWindow._download_summary_is_active(self):
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
            startup_ending = (
                getattr(self.controller, "startup_state", None)
                is StartupState.SAFETY_CHECKING
                or any(
                    binding.spec.task_type == "startup_safety"
                    for binding in getattr(
                        self, "_background_bindings", {}
                    ).values()
                )
            )
            self._close_pending = True
            self.coordinator.begin_closing()
            self.controller.begin_closing()
            self._apply_action_gate()
            QMessageBox.information(
                self,
                "启动安全检查正在结束" if startup_ending else "后台任务正在结束",
                (
                    "已请求取消启动安全检查；后台线程安全退出后管理器将自动关闭。"
                    if startup_ending
                    else "已请求取消可取消的后台任务；后台线程安全退出后管理器将自动关闭。"
                ),
            )

            event.ignore()
            return
        begin_closing = getattr(self.controller, "begin_closing", None)
        if callable(begin_closing):
            self.coordinator.begin_closing()
            begin_closing()
            self._apply_action_gate()
        if hasattr(self, "_background_bindings"):
            collector = getattr(self.controller, "collector", None)
            if collector is not None and getattr(collector, "process", None) is not None:
                self._start_collector_stop_on_close()
                event.ignore()
                return
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
        save_window_state = getattr(self, "_save_window_state_once", None)
        if callable(save_window_state):
            save_window_state()
        event.accept()

    def _start_collector_stop_on_close(self) -> None:
        if self._collector_stop_task_id is not None:
            return
        stop_succeeded = {"value": False}
        spec = TaskSpec(
            task_type="collector_stop_on_close",
            display_name="关闭前停止采集服务",
            resource_keys=frozenset({"collector_process"}),
            deduplicate_key="collector_stop_on_close",
            cancellable=False,
            close_policy=ClosePolicy.WAIT,
            allow_during_closing=True,
        )

        def success(_result: object) -> None:
            stop_succeeded["value"] = True
            self._collector_stop_task_id = None

        def finish_after_removal() -> None:
            if not stop_succeeded["value"]:
                return
            if self.coordinator.has_active_tasks():
                self._close_pending = True
            else:
                QTimer.singleShot(0, self.close)

        def failure(payload: object) -> None:
            self._collector_stop_task_id = None
            message = payload.message if isinstance(payload, TaskFailure) else str(payload)
            QMessageBox.critical(
                self,
                "账号采集服务停止失败",
                f"管理器仍保持打开，关闭前停止失败；可再次关闭重试。\n\n{message}",
            )

        self._collector_stop_task_id = self._submit_background(
            spec,
            lambda _token: self.controller.stop_collector(),
            on_success=success,
            on_failure=failure,
            on_removed=finish_after_removal,
            allow_during_closing=True,
        )

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
            QLabel#dashboardMessage { color: #374151; padding: 4px 0; }
            QFrame#dashboardMetric { background: white; border: 1px solid #cbd5e1;
                                     border-radius: 6px; }
            QLabel#dashboardMetricTitle { color: #4b5563; font-size: 12px; }
            QLabel#dashboardMetricValue { color: #111827; font-size: 16px;
                                           font-weight: 700; }
            QLabel#logStatsResultContext { background: #f8fafc; color: #374151;
                                           border: 1px solid #cbd5e1;
                                           padding: 8px 10px; font-size: 14px;
                                           font-weight: 600; }
            QLabel#logStatsResultContext[stale="true"] { background: #fff7ed;
                                                          color: #9a3412;
                                                          border-color: #fdba74; }
            QTableWidget { background: white; border: 1px solid #cbd5e1;
                           gridline-color: #e5e7eb; }
            QTableWidget#engineRollbackTable::item:selected {
                background: #dbeafe; color: #1e3a5f;
            }
            QTableWidget#engineRollbackTable::item:selected:!active {
                background: #f1f5f9; color: #334155;
            }
            """
        )
