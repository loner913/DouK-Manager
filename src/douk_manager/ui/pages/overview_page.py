"""Modern overview page layered over the proven V0.1.6 controls."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from douk_manager.startup import StartupState

from ..theme.tokens import UiMetrics
from ..widgets.metric_card import MetricCard
from ..widgets.overview_charts import DonutChart, TaskTrendChart


def _text_or_dash(value: object | None) -> str:
    if value is None:
        return "—"
    text = str(value).strip()
    return text or "—"


def _format_datetime(value: datetime | None) -> str:
    return "—" if value is None else value.strftime("%Y-%m-%d %H:%M:%S")


def _status_name(status: object) -> str:
    name = getattr(status, "name", "")
    if name:
        return str(name)
    return str(status).rsplit(".", 1)[-1]


_STATUS_LABELS = {
    "DOWNLOADED": "下载成功",
    "ALL_SKIPPED": "全部跳过",
    "NO_ELIGIBLE_WORKS": "无符合条件作品",
    "PRIVATE": "私密账号",
    "ERROR": "处理异常",
    "INTERRUPTED": "处理中断",
}


class ModernOverviewPage(QWidget):
    """Modern dashboard that reads real V0.1.6 runtime state.

    The legacy overview widget is retained verbatim in the expandable diagnostics
    area, keeping every original button, signal/slot, action gate, startup label,
    diagnostic field, and object identity alive.
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
        self._host = host
        self.legacy_page = legacy_page
        self._last_snapshot_identity: tuple[object, ...] | None = None
        self._dashboard_refresh_requested = False
        self._diagnostics_expanded = False
        self._auto_expanded_for_degraded = False
        self._layout_width = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("modernOverviewScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        root.addWidget(self.scroll)

        self.canvas = QWidget(self.scroll)
        self.canvas.setProperty("modernUi", True)
        self.canvas.setObjectName("modernOverviewCanvas")
        self.scroll.setWidget(self.canvas)

        self.layout = QVBoxLayout(self.canvas)
        self.layout.setContentsMargins(
            UiMetrics.PAGE_MARGIN,
            20,
            UiMetrics.PAGE_MARGIN,
            UiMetrics.PAGE_MARGIN,
        )
        self.layout.setSpacing(14)

        self._build_heading()
        self._build_metrics()
        self._build_analytics()
        self._build_work_area()
        self._build_diagnostics()
        self.layout.addStretch(1)

        self._timer = QTimer(self)
        self._timer.setInterval(350)
        self._timer.timeout.connect(self.refresh_from_host)
        self._timer.start()
        self._clock_timer = QTimer(self)
        self._clock_timer.setInterval(30_000)
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start()
        self._update_clock()
        self.refresh_from_host()
        QTimer.singleShot(0, lambda: self.set_viewport_width(self.width()))

    def _build_heading(self) -> None:
        row = QHBoxLayout()
        heading = QVBoxLayout()
        heading.setSpacing(2)
        self.title = QLabel("运行总览", self.canvas)
        self.title.setObjectName("modernPageTitle")
        self.subtitle = QLabel(
            "DouK Manager 的实时状态与最近任务概览 · 所有数值均来自现有运行数据",
            self.canvas,
        )
        self.subtitle.setObjectName("modernPageSubtitle")
        self.subtitle.setWordWrap(True)
        heading.addWidget(self.title)
        heading.addWidget(self.subtitle)
        row.addLayout(heading, 1)

        self.startup_badge = QLabel("安全检查中", self.canvas)
        self.startup_badge.setObjectName("modernOverviewStartupBadge")
        self.startup_badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        row.addWidget(self.startup_badge, 0, Qt.AlignmentFlag.AlignTop)

        clock = QVBoxLayout()
        clock.setSpacing(0)
        self.clock_date = QLabel(self.canvas)
        self.clock_date.setObjectName("modernClockDate")
        self.clock_date.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.clock_time = QLabel(self.canvas)
        self.clock_time.setObjectName("modernClockTime")
        self.clock_time.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock.addWidget(self.clock_date)
        clock.addWidget(self.clock_time)
        row.addLayout(clock)
        self.layout.addLayout(row)

    def _build_metrics(self) -> None:
        self.metric_grid = QGridLayout()
        self.metric_grid.setHorizontalSpacing(10)
        self.metric_grid.setVerticalSpacing(10)
        self.metrics = {
            "planned": MetricCard(
                "计划账号", note="最近一次可用任务", icon_text="◎", tone="primary", parent=self.canvas
            ),
            "started": MetricCard(
                "实际开始", note="最近一次可用任务", icon_text="▶", tone="success", parent=self.canvas
            ),
            "complete": MetricCard(
                "完整性", note="基于任务日志证据", icon_text="✓", tone="info", parent=self.canvas
            ),
            "reliable": MetricCard(
                "可靠性", note="基于任务日志证据", icon_text="◆", tone="violet", parent=self.canvas
            ),
            "anomaly": MetricCard(
                "附加异常", note="完成后发现的异常", icon_text="!", tone="danger", parent=self.canvas
            ),
            "not_started": MetricCard(
                "未进入处理", note="计划内但未开始", icon_text="◷", tone="warning", parent=self.canvas
            ),
        }
        self.layout.addLayout(self.metric_grid)

    def _build_analytics(self) -> None:
        self.analytics_grid = QGridLayout()
        self.analytics_grid.setHorizontalSpacing(10)
        self.analytics_grid.setVerticalSpacing(10)

        self.trend_card = self._make_card("最近 7 天任务趋势", "按真实任务日志结束时间统计")
        self.trend_chart = TaskTrendChart(self.trend_card)
        self.trend_card.layout().addWidget(self.trend_chart, 1)

        self.composition_card = self._make_card("结果构成", "最近一次可用任务")
        composition_body = QHBoxLayout()
        self.donut_chart = DonutChart(self.composition_card)
        composition_body.addWidget(self.donut_chart, 1)
        self.composition_legend = QLabel("暂无可用任务结果", self.composition_card)
        self.composition_legend.setObjectName("modernDetailLabel")
        self.composition_legend.setWordWrap(True)
        composition_body.addWidget(self.composition_legend, 1)
        self.composition_card.layout().addLayout(composition_body)

        self.throughput_card = self._make_card("下载吞吐", "最近一次任务的真实处理效率")
        self.throughput_value = QLabel("—", self.throughput_card)
        self.throughput_value.setObjectName("modernMetricValue")
        self.throughput_note = QLabel("等待真实任务日志", self.throughput_card)
        self.throughput_note.setObjectName("modernSectionNote")
        self.throughput_note.setWordWrap(True)
        self.throughput_bar = QProgressBar(self.throughput_card)
        self.throughput_bar.setObjectName("modernThroughputBar")
        self.throughput_bar.setTextVisible(False)
        self.throughput_bar.setRange(0, 100)
        self.throughput_bar.setValue(0)
        self.throughput_card.layout().addWidget(self.throughput_value)
        self.throughput_card.layout().addWidget(self.throughput_note)
        self.throughput_card.layout().addSpacing(8)
        self.throughput_card.layout().addWidget(self.throughput_bar)
        self.throughput_card.layout().addStretch(1)

        self.layout.addLayout(self.analytics_grid)

    def _build_work_area(self) -> None:
        self.work_grid = QGridLayout()
        self.work_grid.setHorizontalSpacing(10)
        self.work_grid.setVerticalSpacing(10)

        self.task_card = self._make_card("账号任务", "快速进入原有 V0.1.6 账号任务功能")
        self.task_summary = QLabel("账号表达式、Earliest、智能跳过等行为保持原样。", self.task_card)
        self.task_summary.setObjectName("modernDetailLabel")
        self.task_summary.setWordWrap(True)
        self.task_card.layout().addWidget(self.task_summary)
        task_button = self._action_button("打开账号任务", "primary")
        task_button.clicked.connect(lambda: self._navigate("账号任务"))
        self.task_card.layout().addWidget(task_button)

        self.queue_card = self._make_card("下载队列", "当前队列状态")
        self.queue_state_value = QLabel("空闲", self.queue_card)
        self.queue_state_value.setObjectName("modernDetailValue")
        self.queue_state_value.setWordWrap(True)
        self.queue_card.layout().addWidget(self.queue_state_value)
        queue_button = self._action_button("打开下载队列", "secondary")
        queue_button.clicked.connect(lambda: self._navigate("下载队列"))
        self.queue_card.layout().addWidget(queue_button)

        self.attention_card = self._make_card("最近异常", "只展示已有证据，不推断未知状态")
        self.attention_value = QLabel("尚无可用任务结果", self.attention_card)
        self.attention_value.setObjectName("modernDetailLabel")
        self.attention_value.setWordWrap(True)
        self.attention_card.layout().addWidget(self.attention_value)
        dashboard_button = self._action_button("查看结果看板", "secondary")
        dashboard_button.clicked.connect(lambda: self._navigate("结果看板"))
        self.attention_card.layout().addWidget(dashboard_button)

        self.layout.addLayout(self.work_grid)

    def _build_diagnostics(self) -> None:
        self.compatibility = QFrame(self.canvas)
        self.compatibility.setProperty("modernUi", True)
        self.compatibility.setProperty("modernCard", True)
        compatibility_layout = QVBoxLayout(self.compatibility)
        compatibility_layout.setContentsMargins(16, 14, 16, 16)
        compatibility_layout.setSpacing(10)

        heading = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel("系统状态与安全控制", self.compatibility)
        title.setObjectName("modernSectionTitle")
        note = QLabel(
            "V0.1.6 原始安全控件完整保留；只读保护时自动展开，正常状态下可按需查看。",
            self.compatibility,
        )
        note.setObjectName("modernSectionNote")
        note.setWordWrap(True)
        text.addWidget(title)
        text.addWidget(note)
        heading.addLayout(text, 1)
        self.diagnostic_toggle = self._action_button("展开诊断", "secondary")
        self.diagnostic_toggle.clicked.connect(self._toggle_diagnostics)
        heading.addWidget(self.diagnostic_toggle, 0, Qt.AlignmentFlag.AlignTop)
        compatibility_layout.addLayout(heading)

        self._hide_duplicate_legacy_heading()
        self.legacy_page.setParent(self.compatibility)
        self.legacy_page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        compatibility_layout.addWidget(self.legacy_page)
        self.legacy_page.hide()
        self.layout.addWidget(self.compatibility)

    @staticmethod
    def _make_card(title: str, note: str = "") -> QFrame:
        card = QFrame()
        card.setProperty("modernUi", True)
        card.setProperty("modernCard", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        label = QLabel(title, card)
        label.setObjectName("modernSectionTitle")
        layout.addWidget(label)
        if note:
            helper = QLabel(note, card)
            helper.setObjectName("modernSectionNote")
            helper.setWordWrap(True)
            layout.addWidget(helper)
        return card

    @staticmethod
    def _action_button(text: str, role: str) -> QPushButton:
        button = QPushButton(text)
        button.setProperty("overviewAction", role)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        return button

    def _hide_duplicate_legacy_heading(self) -> None:
        for label in self.legacy_page.findChildren(QLabel):
            if label.objectName() == "title":
                label.hide()
                continue
            if label.text().strip() == (
                "统一管理账号采集、任务配置、5-1-1 下载、截图归档、快捷方式索引和分级备份。"
            ):
                label.hide()

    def _navigate(self, label: str) -> None:
        navigate = getattr(self._host, "navigate_to_page_label", None)
        if callable(navigate):
            navigate(label)

    def _toggle_diagnostics(self) -> None:
        self.set_diagnostics_expanded(not self._diagnostics_expanded)

    def set_diagnostics_expanded(self, expanded: bool) -> None:
        self._diagnostics_expanded = expanded
        self.legacy_page.setVisible(expanded)
        self.diagnostic_toggle.setText("收起诊断" if expanded else "展开诊断")

    def activate(self) -> None:
        self._dashboard_refresh_requested = False
        self.refresh_from_host()
        self.request_real_dashboard_refresh()

    def request_real_dashboard_refresh(self) -> None:
        host = self._host
        if host.controller.startup_state is not StartupState.READY:
            return
        if self._dashboard_refresh_requested:
            return
        self._dashboard_refresh_requested = True
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
        self._refresh_trend(host)
        self._refresh_snapshot(getattr(host, "_dashboard_snapshot", None))

    def _refresh_startup(self, host: Any) -> None:
        state = host.controller.startup_state
        summary_label = getattr(host, "startup_summary_label", None)
        summary = summary_label.text().strip() if summary_label is not None else "检查中"
        labels = {
            StartupState.BOOTSTRAPPING: ("正在启动", "info"),
            StartupState.SAFETY_CHECKING: ("安全检查中", "info"),
            StartupState.READY: ("运行正常", "success"),
            StartupState.DEGRADED_READ_ONLY: ("只读保护", "warning"),
            StartupState.CLOSING: ("正在关闭", "info"),
        }
        text, kind = labels.get(state, ("状态未知", "info"))
        self.startup_badge.setText(text)
        self.startup_badge.setProperty("startupKind", kind)
        self.startup_badge.setToolTip(summary or text)
        self.startup_badge.style().unpolish(self.startup_badge)
        self.startup_badge.style().polish(self.startup_badge)
        if state is StartupState.DEGRADED_READ_ONLY and not self._auto_expanded_for_degraded:
            self._auto_expanded_for_degraded = True
            self.set_diagnostics_expanded(True)

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

    def _refresh_trend(self, host: Any) -> None:
        index = getattr(host, "_dashboard_index", None)
        today = datetime.now().date()
        dates = [today - timedelta(days=offset) for offset in range(6, -1, -1)]
        counts = {day: 0 for day in dates}
        if index is not None:
            for entry in getattr(index, "entries", ()):
                ended = getattr(entry, "ended_at", None)
                if ended is not None and ended.date() in counts:
                    counts[ended.date()] += 1
        points = tuple((day.strftime("%m-%d"), counts[day]) for day in dates)
        self.trend_chart.set_points(points)

    def _refresh_snapshot(self, snapshot: Any | None) -> None:
        if snapshot is None:
            for key in ("planned", "started", "anomaly", "not_started"):
                self.metrics[key].set_metric("—")
            self.metrics["complete"].set_metric("—", note="等待真实任务日志")
            self.metrics["reliable"].set_metric("—", note="等待真实任务日志")
            self.donut_chart.set_segments(())
            self.composition_legend.setText("暂无可用任务结果")
            self.throughput_value.setText("—")
            self.throughput_note.setText("等待真实任务日志")
            self.throughput_bar.setValue(0)
            self.attention_value.setText("尚无可用任务结果")
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
            self.metrics["complete"].set_metric("未知", note="证据不足，不推断百分比")

        reasons = tuple(getattr(snapshot, "reliability_reasons", ()) or ())
        self.metrics["reliable"].set_metric(
            "可靠" if reliable else "需复核",
            note="任务证据可信" if reliable else ("；".join(map(str, reasons[:2])) or "证据不足"),
        )

        segments: list[tuple[str, int]] = []
        legend: list[str] = []
        for status, value in tuple(getattr(snapshot, "main_status_counts", ()) or ()):
            count = 0 if value is None else int(value)
            label = _STATUS_LABELS.get(_status_name(status), _status_name(status))
            segments.append((label, count))
            if count:
                legend.append(f"{label}  {count}")
        pre_start = getattr(snapshot, "pre_start_error_count", None)
        if pre_start:
            segments.append(("进入处理前异常", int(pre_start)))
            legend.append(f"进入处理前异常  {pre_start}")
        if not_started:
            segments.append(("未开始", int(not_started)))
            legend.append(f"未开始  {not_started}")
        self.donut_chart.set_segments(tuple(segments))
        self.composition_legend.setText("\n".join(legend[:7]) or "当前任务暂无分类计数")

        duration = getattr(snapshot, "duration_seconds", None)
        if started is not None and duration not in (None, 0):
            per_minute = float(started) * 60.0 / float(duration)
            self.throughput_value.setText(f"{per_minute:.1f} 账号/分钟")
            self.throughput_note.setText(f"本次任务耗时 {int(duration)} 秒")
        else:
            self.throughput_value.setText("—")
            self.throughput_note.setText("当前证据不足以计算处理速度")
        if planned and started is not None:
            coverage = max(0, min(100, round(100 * int(started) / int(planned))))
            self.throughput_bar.setValue(coverage)
            self.throughput_bar.setToolTip(f"已开始 / 计划账号：{coverage}%")
        else:
            self.throughput_bar.setValue(0)
            self.throughput_bar.setToolTip("暂无处理覆盖率")

        attention_lines = []
        if anomaly:
            attention_lines.append(f"完成后附加异常：{anomaly}")
        error_count = next(
            (
                value
                for status, value in tuple(getattr(snapshot, "main_status_counts", ()) or ())
                if _status_name(status) == "ERROR"
            ),
            None,
        )
        interrupted_count = next(
            (
                value
                for status, value in tuple(getattr(snapshot, "main_status_counts", ()) or ())
                if _status_name(status) == "INTERRUPTED"
            ),
            None,
        )
        if error_count:
            attention_lines.append(f"处理异常：{error_count}")
        if interrupted_count:
            attention_lines.append(f"处理中断：{interrupted_count}")
        if pre_start:
            attention_lines.append(f"进入处理前异常：{pre_start}")
        if not_started:
            attention_lines.append(f"未开始：{not_started}")
        self.attention_value.setText("\n".join(attention_lines) or "最近任务没有需要关注的异常证据")

    def _update_clock(self) -> None:
        now = datetime.now()
        self.clock_date.setText(now.strftime("%Y年%m月%d日"))
        self.clock_time.setText(now.strftime("%H:%M"))

    def set_viewport_width(self, width: int) -> None:
        width = max(1, int(width))
        if self._layout_width == width:
            return
        self._layout_width = width
        if width >= 1180:
            metric_columns = 6
        elif width >= 760:
            metric_columns = 3
        else:
            metric_columns = 2
        self._relayout_metrics(metric_columns)
        self._relayout_analytics(width)
        self._relayout_work(width)
        self.clock_date.setVisible(width >= 720)
        self.clock_time.setVisible(width >= 720)

    def _relayout_metrics(self, columns: int) -> None:
        for card in self.metrics.values():
            self.metric_grid.removeWidget(card)
        for index, card in enumerate(self.metrics.values()):
            self.metric_grid.addWidget(card, index // columns, index % columns)

    def _relayout_analytics(self, width: int) -> None:
        for card in (self.trend_card, self.composition_card, self.throughput_card):
            self.analytics_grid.removeWidget(card)
        if width >= 1080:
            self.analytics_grid.addWidget(self.trend_card, 0, 0, 1, 2)
            self.analytics_grid.addWidget(self.composition_card, 0, 2)
            self.analytics_grid.addWidget(self.throughput_card, 0, 3)
            for column in range(4):
                self.analytics_grid.setColumnStretch(column, 1)
        elif width >= 720:
            self.analytics_grid.addWidget(self.trend_card, 0, 0, 1, 2)
            self.analytics_grid.addWidget(self.composition_card, 1, 0)
            self.analytics_grid.addWidget(self.throughput_card, 1, 1)
        else:
            self.analytics_grid.addWidget(self.trend_card, 0, 0)
            self.analytics_grid.addWidget(self.composition_card, 1, 0)
            self.analytics_grid.addWidget(self.throughput_card, 2, 0)

    def _relayout_work(self, width: int) -> None:
        cards = (self.task_card, self.queue_card, self.attention_card)
        for card in cards:
            self.work_grid.removeWidget(card)
        if width >= 900:
            for index, card in enumerate(cards):
                self.work_grid.addWidget(card, 0, index)
                self.work_grid.setColumnStretch(index, 1)
        else:
            for index, card in enumerate(cards):
                self.work_grid.addWidget(card, index, 0)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self.set_viewport_width(event.size().width())
