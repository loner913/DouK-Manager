"""Modern overview page layered over the proven V0.1.6 controls."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from douk_manager.startup import StartupState

from ..theme.tokens import UiMetrics
from ..widgets.metric_card import MetricCard


def _text_or_dash(value: object | None) -> str:
    if value is None:
        return "—"
    text = str(value).strip()
    return text or "—"


def _format_datetime(value: datetime | None) -> str:
    return "—" if value is None else value.strftime("%Y-%m-%d %H:%M:%S")


class ModernOverviewPage(QWidget):
    """Read-only modern dashboard plus the untouched V0.1.6 safety controls.

    The legacy overview widget is reparented into the bottom compatibility
    section instead of being recreated. This keeps every original button,
    signal/slot, action gate, startup label, diagnostic field, and object
    identity alive while the new dashboard reads the same runtime state.
    """

    def __init__(
        self,
        host: Any,
        legacy_page: QWidget,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("modernUi", True)
        self.setObjectName("modernOverviewPage")
        self.setStyleSheet(
            """
QWidget#modernOverviewPage, QScrollArea#modernOverviewScroll,
QWidget#modernOverviewCanvas {
    background: transparent;
}
QLabel#modernPageTitle {
    font-size: 25px;
    font-weight: 700;
}
QLabel#modernPageSubtitle, QLabel#modernSectionNote,
QLabel#modernMetricTitle, QLabel#modernMetricNote,
QLabel#modernDetailLabel {
    font-size: 12px;
}
QLabel#modernMetricValue {
    font-size: 26px;
    font-weight: 700;
}
QLabel#modernMetricNote, QLabel#modernSectionNote,
QLabel#modernDetailLabel {
    opacity: 0.72;
}
QLabel#modernSectionTitle {
    font-size: 16px;
    font-weight: 700;
}
QLabel#modernDetailValue {
    font-size: 13px;
    font-weight: 600;
}
QLabel#modernOverviewStartupBadge {
    min-height: 30px;
    padding: 0 12px;
    border-radius: 8px;
    font-weight: 600;
}
"""
        )
        self._host = host
        self.legacy_page = legacy_page
        self._last_snapshot_identity: tuple[object, ...] | None = None
        self._dashboard_refresh_requested = False

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        scroll = QScrollArea(self)
        scroll.setObjectName("modernOverviewScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(scroll)

        canvas = QWidget(scroll)
        canvas.setProperty("modernUi", True)
        canvas.setObjectName("modernOverviewCanvas")
        scroll.setWidget(canvas)

        layout = QVBoxLayout(canvas)
        layout.setContentsMargins(
            UiMetrics.PAGE_MARGIN,
            UiMetrics.PAGE_MARGIN,
            UiMetrics.PAGE_MARGIN,
            UiMetrics.PAGE_MARGIN,
        )
        layout.setSpacing(UiMetrics.SPACE_XL)

        heading_row = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(UiMetrics.SPACE_XS)
        title = QLabel("总览", canvas)
        title.setObjectName("modernPageTitle")
        subtitle = QLabel("运行状态、最近任务结果与 V0.1.6 安全控制", canvas)
        subtitle.setObjectName("modernPageSubtitle")
        heading.addWidget(title)
        heading.addWidget(subtitle)
        heading_row.addLayout(heading)
        heading_row.addStretch(1)
        self.startup_badge = QLabel("启动检查中", canvas)
        self.startup_badge.setObjectName("modernOverviewStartupBadge")
        self.startup_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        heading_row.addWidget(self.startup_badge)
        layout.addLayout(heading_row)

        metric_grid = QGridLayout()
        metric_grid.setHorizontalSpacing(UiMetrics.SPACE_M)
        metric_grid.setVerticalSpacing(UiMetrics.SPACE_M)
        self.metrics = {
            "planned": MetricCard("计划账号", note="最近一次可用下载任务", parent=canvas),
            "started": MetricCard("实际开始", note="最近一次可用下载任务", parent=canvas),
            "complete": MetricCard("完整性", note="基于任务日志证据", parent=canvas),
            "reliable": MetricCard("可靠性", note="基于任务日志证据", parent=canvas),
            "anomaly": MetricCard("附加异常", note="完成但存在附加异常", parent=canvas),
            "not_started": MetricCard("未进入处理", note="计划内但没有开始", parent=canvas),
        }
        for index, card in enumerate(self.metrics.values()):
            metric_grid.addWidget(card, index // 3, index % 3)
        layout.addLayout(metric_grid)

        detail_row = QHBoxLayout()
        detail_row.setSpacing(UiMetrics.SPACE_M)
        self.task_card = self._make_detail_card("最近任务", canvas)
        self.runtime_card = self._make_detail_card("当前运行", canvas)
        detail_row.addWidget(self.task_card, 1)
        detail_row.addWidget(self.runtime_card, 1)
        layout.addLayout(detail_row)

        self.task_name_value = self._add_detail_row(
            self.task_card.layout(), "任务日志", "尚无可用任务结果"
        )
        self.task_time_value = self._add_detail_row(
            self.task_card.layout(), "结束时间", "—"
        )
        self.task_duration_value = self._add_detail_row(
            self.task_card.layout(), "耗时", "—"
        )

        self.queue_state_value = self._add_detail_row(
            self.runtime_card.layout(), "下载队列", "空闲"
        )
        self.account_summary_value = self._add_detail_row(
            self.runtime_card.layout(), "主档账号", "检查中"
        )
        self.startup_summary_value = self._add_detail_row(
            self.runtime_card.layout(), "启动安全", "检查中"
        )

        compatibility = QFrame(canvas)
        compatibility.setProperty("modernUi", True)
        compatibility.setProperty("modernCard", True)
        compatibility.setObjectName("modernCompatibilityCard")
        compatibility_layout = QVBoxLayout(compatibility)
        compatibility_layout.setContentsMargins(
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_L,
        )
        compatibility_layout.setSpacing(UiMetrics.SPACE_M)

        legacy_title = QLabel("系统状态与安全控制", compatibility)
        legacy_title.setObjectName("modernSectionTitle")
        legacy_note = QLabel(
            "这里直接承载 V0.1.6 原始总览控件；重新检查、日志、路径修复等行为仍由原代码执行。",
            compatibility,
        )
        legacy_note.setObjectName("modernSectionNote")
        legacy_note.setWordWrap(True)
        compatibility_layout.addWidget(legacy_title)
        compatibility_layout.addWidget(legacy_note)

        self._hide_duplicate_legacy_heading()
        legacy_page.setParent(compatibility)
        compatibility_layout.addWidget(legacy_page)
        layout.addWidget(compatibility)
        layout.addStretch(1)

        self._timer = QTimer(self)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self.refresh_from_host)
        self._timer.start()
        self.refresh_from_host()

    def _hide_duplicate_legacy_heading(self) -> None:
        for label in self.legacy_page.findChildren(QLabel):
            if label.objectName() == "title":
                label.hide()
                continue
            if label.text().strip() == (
                "统一管理账号采集、任务配置、5-1-1 下载、截图归档、快捷方式索引和分级备份。"
            ):
                label.hide()

    @staticmethod
    def _make_detail_card(title: str, parent: QWidget) -> QFrame:
        card = QFrame(parent)
        card.setProperty("modernUi", True)
        card.setProperty("modernCard", True)
        detail_layout = QVBoxLayout(card)
        detail_layout.setContentsMargins(
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_L,
        )
        detail_layout.setSpacing(UiMetrics.SPACE_S)
        label = QLabel(title, card)
        label.setObjectName("modernSectionTitle")
        detail_layout.addWidget(label)
        return card

    @staticmethod
    def _add_detail_row(layout, label: str, value: str) -> QLabel:
        row = QHBoxLayout()
        name = QLabel(label)
        name.setObjectName("modernDetailLabel")
        result = QLabel(value)
        result.setObjectName("modernDetailValue")
        result.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        result.setWordWrap(True)
        row.addWidget(name)
        row.addStretch(1)
        row.addWidget(result, 1)
        layout.addLayout(row)
        return result

    def activate(self) -> None:
        """Refresh visible state whenever the user enters the overview."""

        self._dashboard_refresh_requested = False
        self.refresh_from_host()
        self.request_real_dashboard_refresh()

    def request_real_dashboard_refresh(self) -> None:
        """Ask the existing V0.1.6 dashboard pipeline for real log evidence."""

        host = self._host
        if host.controller.startup_state is not StartupState.READY:
            return
        if self._dashboard_refresh_requested:
            return
        self._dashboard_refresh_requested = True
        # The legacy method already owns background-task deduplication and
        # result validation. Reuse it instead of opening logs from this page.
        host.refresh_result_dashboard(auto_refresh=True)

    def refresh_from_host(self) -> None:
        host = self._host
        if (
            host.controller.startup_state is StartupState.READY
            and getattr(host, "_dashboard_snapshot", None) is None
            and not self._dashboard_refresh_requested
        ):
            QTimer.singleShot(0, self.request_real_dashboard_refresh)
        self._refresh_startup(host)
        self._refresh_runtime(host)
        self._refresh_snapshot(getattr(host, "_dashboard_snapshot", None))

    def _refresh_startup(self, host: Any) -> None:
        state_label = getattr(host, "startup_state_label", None)
        summary_label = getattr(host, "startup_summary_label", None)
        state = state_label.text().strip() if state_label is not None else "检查中"
        summary = summary_label.text().strip() if summary_label is not None else "检查中"
        self.startup_badge.setText(state or "检查中")
        self.startup_badge.setProperty("startupState", state)
        self.startup_summary_value.setText(summary or "检查中")

    def _refresh_runtime(self, host: Any) -> None:
        if getattr(host, "queue_active", False):
            state = "已暂停" if getattr(host, "queue_paused", False) else "运行中"
            current = getattr(host, "queue_current", None)
            if current is not None:
                state = f"{state} · {_text_or_dash(getattr(current, 'name', current))}"
        else:
            pending = len(getattr(host, "queue_pending", ()) or ())
            state = f"等待 {pending} 项" if pending else "空闲"
        self.queue_state_value.setText(state)

        account_summary = getattr(host, "account_summary", None)
        self.account_summary_value.setText(
            account_summary.text().strip()
            if account_summary is not None and account_summary.text().strip()
            else "检查中"
        )

    def _refresh_snapshot(self, snapshot: Any | None) -> None:
        if snapshot is None:
            for key in ("planned", "started", "anomaly", "not_started"):
                self.metrics[key].set_metric("—")
            self.metrics["complete"].set_metric("—", note="等待真实任务日志")
            self.metrics["reliable"].set_metric("—", note="等待真实任务日志")
            self.task_name_value.setText("尚无可用任务结果")
            self.task_time_value.setText("—")
            self.task_duration_value.setText("—")
            return

        identity = (
            getattr(getattr(snapshot, "fingerprint", None), "normalized_path", None),
            getattr(getattr(snapshot, "fingerprint", None), "size", None),
            getattr(getattr(snapshot, "fingerprint", None), "mtime_ns", None),
        )
        self._last_snapshot_identity = identity

        planned = getattr(snapshot, "planned_count", None)
        started = getattr(snapshot, "started_count", None)
        anomaly = getattr(snapshot, "completed_with_anomaly_count", None)
        not_started = getattr(snapshot, "not_started_count", None)
        complete = getattr(snapshot, "complete", None)
        reliable = bool(getattr(snapshot, "reliable", False))

        self.metrics["planned"].set_metric(planned)
        self.metrics["started"].set_metric(started)
        self.metrics["anomaly"].set_metric(anomaly)
        self.metrics["not_started"].set_metric(not_started)

        if complete is True:
            self.metrics["complete"].set_metric("完整", note="任务证据完整")
        elif complete is False:
            self.metrics["complete"].set_metric("未完整", note="任务证据显示结果不完整")
        else:
            self.metrics["complete"].set_metric("未知", note="证据不足，未推断百分比")

        reliability_reasons = tuple(getattr(snapshot, "reliability_reasons", ()) or ())
        reliability_note = (
            "任务证据可信"
            if reliable
            else ("；".join(str(item) for item in reliability_reasons[:2]) or "需复核")
        )
        self.metrics["reliable"].set_metric(
            "可靠" if reliable else "需复核",
            note=reliability_note,
        )

        task_log = getattr(snapshot, "task_log", None)
        self.task_name_value.setText(_text_or_dash(getattr(task_log, "name", task_log)))
        self.task_time_value.setText(_format_datetime(getattr(snapshot, "ended_at", None)))
        duration = getattr(snapshot, "duration_seconds", None)
        self.task_duration_value.setText(
            "—" if duration is None else f"{int(duration)} 秒"
        )
