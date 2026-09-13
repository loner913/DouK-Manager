"""Modern overview page layered over the proven V0.1.6 controls."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from typing import Any

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
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
from ..widgets.overview_charts import (
    DonutChart,
    HourlyThroughputChart,
    TaskTrendChart,
)
from .legacy_page import mark_legacy_descendants


def _text_or_dash(value: object | None) -> str:
    if value is None:
        return "—"
    text = str(value).strip()
    return text or "—"


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


_PREVIEW_TREND_LABELS = (
    "09-06",
    "09-07",
    "09-08",
    "09-09",
    "09-10",
    "09-11",
    "09-12",
)
_PREVIEW_TREND_SERIES = (
    ("计划账号", (800, 1050, 900, 1100, 1000, 1150, 1200), "#3287FF"),
    ("有新作品", (640, 880, 720, 900, 850, 920, 960), "#19C37D"),
    ("处理异常", (15, 28, 22, 30, 18, 32, 23), "#FF5967"),
)
_PREVIEW_RESULT_SEGMENTS = (
    ("下载成功", 712),
    ("下载失败", 45),
    ("超时", 68),
    ("解析失败", 32),
    ("其他", 35),
)
_PREVIEW_RESULT_DETAILS = (
    ("下载成功", 712, "79.8%", "#20C997"),
    ("下载失败", 45, "5.0%", "#FF5C68"),
    ("超时", 68, "7.6%", "#4C8DFF"),
    ("解析失败", 32, "3.6%", "#FFB547"),
    ("其他", 35, "3.9%", "#8B5CF6"),
)
_PREVIEW_THROUGHPUT = (
    18, 55, 62, 21, 45, 30, 70, 80, 92, 64, 38, 42,
    55, 60, 37, 26, 50, 58, 35, 20, 12, 6, 0, 0,
)


class ModernOverviewPage(QWidget):
    """Modern dashboard that reads real V0.1.6 runtime state.

    The legacy overview widget is retained verbatim in the expandable diagnostics
    area.  The modern layer only presents already-existing data and navigation;
    original buttons, signals, action gates and startup safety logic remain alive.
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
        self._preview_mock_data = os.environ.get("DOUK_MANAGER_PREVIEW_MOCK_DATA") == "1"
        self.legacy_page = legacy_page
        self._last_snapshot_identity: tuple[object, ...] | None = None
        self._dashboard_refresh_requested = False
        self._diagnostics_expanded = False
        self._layout_width = 0

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("modernOverviewScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        root.addWidget(self.scroll)

        self.canvas = QWidget(self.scroll)
        self.canvas.setProperty("modernUi", True)
        self.canvas.setObjectName("modernOverviewCanvas")
        self.canvas.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
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
        reminder_row = QHBoxLayout()
        self.watchlist_reminder_text = QLabel("观察提醒待加载", self.canvas)
        self.watchlist_reminder_text.setWordWrap(True)
        reminder_row.addWidget(self.watchlist_reminder_text, 1)
        self.watchlist_reminder = QPushButton("查看观察名单", self.canvas)
        self.watchlist_reminder.setObjectName("modernOverviewFilter")
        self.watchlist_reminder.setMinimumHeight(36)
        self.watchlist_reminder.clicked.connect(self._open_watchlist_due)
        reminder_row.addWidget(self.watchlist_reminder)
        self.layout.addLayout(reminder_row)
        self._build_metrics()
        self._build_analytics()
        self._build_work_area()
        self._build_diagnostics()

        # Do not put a catch-all stretch at the bottom.  On a maximized Windows
        # desktop that made the whole dashboard hug the top edge while hundreds
        # of pixels of the viewport remained empty. Give the chart and work bands
        # equal height, matching the balanced rows in the overview reference.
        self.layout.setStretchFactor(self.analytics_grid, 1)
        self.layout.setStretchFactor(self.work_grid, 1)

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
        row.setSpacing(14)
        heading = QVBoxLayout()
        heading.setSpacing(2)
        self.title = QLabel(
            "晚上好，Jackson 👋" if self._preview_mock_data else "运行总览",
            self.canvas,
        )
        self.title.setObjectName("modernPageTitle")
        self.subtitle = QLabel(
            (
                "这是 DouK Manager 的运行概览，今天也是高效工作的一天！"
                if self._preview_mock_data
                else "DouK Manager 的实时状态与最近任务概览 · 所有数值均来自现有运行数据"
            ),
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
        if self._preview_mock_data:
            self.startup_badge.hide()
        row.addWidget(self.startup_badge, 0, Qt.AlignmentFlag.AlignTop)

        clock = QVBoxLayout()
        clock.setContentsMargins(0, 0, 0, 0)
        clock.setSpacing(0)
        self.clock_date = QLabel(self.canvas)
        self.clock_date.setObjectName("modernClockDate")
        self.clock_date.setAlignment(Qt.AlignmentFlag.AlignRight)
        self.clock_time = QLabel(self.canvas)
        self.clock_time.setObjectName("modernClockTime")
        self.clock_time.setAlignment(Qt.AlignmentFlag.AlignRight)
        clock.addWidget(self.clock_date)
        clock.addWidget(self.clock_time)
        clock.setAlignment(Qt.AlignmentFlag.AlignTop)
        row.addLayout(clock)
        self.layout.addLayout(row)

    def _build_metrics(self) -> None:
        self.metric_grid = QGridLayout()
        self.metric_grid.setHorizontalSpacing(10)
        self.metric_grid.setVerticalSpacing(10)
        self.metrics = {
            "planned": MetricCard(
                "计划账号", note="最近一次可用任务", tone="primary", parent=self.canvas
            ),
            "started": MetricCard(
                "实际开始", note="最近一次可用任务", tone="success", parent=self.canvas
            ),
            "complete": MetricCard(
                "完整性", note="基于任务日志证据", tone="info", parent=self.canvas
            ),
            "reliable": MetricCard(
                "可靠性", note="基于任务日志证据", tone="violet", parent=self.canvas
            ),
            "anomaly": MetricCard(
                "附加异常", note="完成后发现的异常", tone="danger", parent=self.canvas
            ),
            "not_started": MetricCard(
                "未进入处理", note="计划内但未开始", tone="warning", parent=self.canvas
            ),
        }
        self.layout.addLayout(self.metric_grid)

    def _build_analytics(self) -> None:
        self.analytics_grid = QGridLayout()
        self.analytics_grid.setHorizontalSpacing(10)
        self.analytics_grid.setVerticalSpacing(10)

        self.trend_card = self._make_card(
            "最近 7 天任务趋势", "按真实任务日志结束时间统计", "近 7 天⌄"
        )
        self.trend_card.setMinimumHeight(260)
        self.trend_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.trend_chart = TaskTrendChart(self.trend_card)
        self._add_chart_legend(
            self.trend_card,
            (("计划账号", "#3287FF"), ("有新作品", "#19C37D"), ("处理异常", "#FF5967")),
        )
        self.trend_card.layout().addWidget(self.trend_chart, 1)

        self.composition_card = self._make_card(
            "结果构成", "上次已结束任务" if self._preview_mock_data else "最近一次可用任务",
            "" if self._preview_mock_data else "全部任务⌄"
        )
        self.composition_card.setMinimumHeight(260)
        self.composition_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        composition_body = QHBoxLayout()
        composition_body.setContentsMargins(0, 0, 0, 0)
        composition_body.setSpacing(8)
        self.donut_chart = DonutChart(self.composition_card)
        composition_body.addWidget(self.donut_chart, 1)
        self.composition_legend = QLabel("暂无可用任务结果", self.composition_card)
        self.composition_legend.setObjectName("modernDetailLabel")
        self.composition_legend.setWordWrap(True)
        composition_body.addWidget(self.composition_legend, 1)
        self.composition_card.layout().addLayout(composition_body, 1)

        self.throughput_card = self._make_card(
            "处理效率" if self._preview_mock_data else "下载吞吐",
            "上次已结束任务" if self._preview_mock_data else "最近一次任务的真实处理效率",
            "" if self._preview_mock_data else "近 24 小时⌄"
        )
        self.throughput_card.setMinimumHeight(260)
        self.throughput_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
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
        self.throughput_bar.hide()
        self.throughput_chart = HourlyThroughputChart(self.throughput_card)
        self.throughput_card.layout().addWidget(self.throughput_value)
        self.throughput_card.layout().addWidget(self.throughput_note)
        self.throughput_card.layout().addWidget(self.throughput_chart, 1)
        self.throughput_card.layout().addWidget(self.throughput_bar)
        if self._preview_mock_data:
            self.throughput_chart.hide()
            self.throughput_card.layout().addSpacing(20)
            self.efficiency_fields = {}
            for name, value in (("任务总用时", "2 小时 30 分钟"), ("实际开始", "1,200 个账号"), ("有新作品下载", "960 个账号")):
                self.efficiency_fields[name] = self._add_summary_field(self.throughput_card.layout(), name, value)
            self.throughput_card.layout().addStretch(1)

        self.layout.addLayout(self.analytics_grid)

    def _build_work_area(self) -> None:
        self.work_grid = QGridLayout()
        self.work_grid.setHorizontalSpacing(10)
        self.work_grid.setVerticalSpacing(10)

        self.task_card = self._make_card("＋ 账号任务", "快速进入原有账号任务功能")
        self.task_card.layout().setAlignment(Qt.AlignmentFlag.AlignTop)
        self.task_card.setProperty("workCard", True)
        self.task_card.setMinimumHeight(190 if not self._preview_mock_data else 250)
        self.task_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.task_summary = QLabel(
            "账号表达式、Earliest、智能跳过与执行入口继续使用原有逻辑。",
            self.task_card,
        )
        self.task_summary.setObjectName("modernDetailLabel")
        self.task_summary.setWordWrap(True)
        self.task_button = self._action_button("打开账号任务", "primary")
        self.task_button.clicked.connect(lambda: self._navigate("账号任务"))
        if self._preview_mock_data:
            self.task_summary.hide()
            self._build_preview_task_form()
            task_actions = QHBoxLayout()
            task_actions.setSpacing(8)
            self.task_button.setText("开始执行")
            task_actions.addWidget(self.task_button, 2)
            batch_button = self._action_button("批次生成", "secondary")
            batch_button.clicked.connect(lambda: self._navigate("批次生成"))
            task_actions.addWidget(batch_button)
            audit_button = self._action_button("账号审计", "secondary")
            audit_button.clicked.connect(lambda: self._navigate("账号审计"))
            task_actions.addWidget(audit_button)
            result_button = self._action_button("查看结果", "secondary")
            result_button.clicked.connect(lambda: self._navigate("下载结果"))
            task_actions.addWidget(result_button)
            self.task_card.layout().addLayout(task_actions)
        else:
            self.task_card.layout().addWidget(self.task_summary)
            self.task_card.layout().addStretch(1)
            self.task_card.layout().addWidget(self.task_button)

        self.queue_card = self._make_card(
            "上次任务摘要" if self._preview_mock_data else "下载队列",
            "任务已结束 · 日志汇总完成" if self._preview_mock_data else "当前队列状态",
        )
        self.queue_card.setProperty("workCard", True)
        self.queue_card.setMinimumHeight(190 if not self._preview_mock_data else 250)
        self.queue_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.queue_state_value = QLabel("空闲", self.queue_card)
        self.queue_state_value.setObjectName("modernWorkValue")
        self.queue_state_value.setWordWrap(True)
        self.queue_card.layout().addWidget(self.queue_state_value)
        queue_note = QLabel(
            "队列、暂停、继续、取消和完成处理仍由 V0.1.6 原流程负责。",
            self.queue_card,
        )
        queue_note.setObjectName("modernDetailLabel")
        queue_note.setWordWrap(True)
        self.queue_button = self._action_button("打开下载队列", "secondary")
        self.queue_button.clicked.connect(lambda: self._navigate("下载队列"))
        if self._preview_mock_data:
            queue_note.hide()
            self.summary_fields = {}
            for name, value in (("账号范围", "A1–A1200"), ("开始时间", "2026-09-12 17:50"), ("结束时间", "2026-09-12 20:20"), ("执行结果", "进程正常结束 · 23 个账号处理异常")):
                self.summary_fields[name] = self._add_summary_field(self.queue_card.layout(), name, value)
            self.queue_card.layout().addStretch(1)
            self.queue_button.setText("查看任务结果")
            self.queue_button.clicked.disconnect()
            self.queue_button.clicked.connect(lambda: self._navigate("结果看板"))
            self.queue_card.layout().addWidget(self.queue_button)
        else:
            self.queue_card.layout().addWidget(queue_note)
            self.queue_card.layout().addStretch(1)
            self.queue_card.layout().addWidget(self.queue_button)

        self.attention_card = self._make_card(
            "上次任务异常" if self._preview_mock_data else "最近异常",
            "任务结束于 09-12 20:20" if self._preview_mock_data else "只展示已有证据",
            "" if self._preview_mock_data else "查看更多 ›"
        )
        self.attention_card.setProperty("workCard", True)
        self.attention_card.setMinimumHeight(190 if not self._preview_mock_data else 250)
        self.attention_card.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.attention_value = QLabel("尚无可用任务结果", self.attention_card)
        self.attention_value.setObjectName("modernDetailLabel")
        self.attention_value.setWordWrap(True)
        self.dashboard_button = self._action_button("查看结果看板", "secondary")
        self.dashboard_button.clicked.connect(lambda: self._navigate("结果看板"))
        if self._preview_mock_data:
            self.attention_value.hide()
            attention_rows = QVBoxLayout()
            self.attention_rows = attention_rows
            attention_rows.setSpacing(3)
            self.attention_card.layout().addLayout(attention_rows, 1)
            for row in (
                ("A37", "处理异常", "", "danger"),
                ("A128", "处理异常", "", "danger"),
                ("A406", "处理异常", "", "danger"),
                ("A752", "完成后附加异常", "", "warning"),
                ("A1036", "完成后附加异常", "", "warning"),
            ):
                self._add_preview_attention_row(attention_rows, *row)
            self.attention_card.layout().addWidget(self.dashboard_button)
        else:
            self.attention_card.layout().addWidget(self.attention_value)
            self.attention_card.layout().addStretch(1)
            self.attention_card.layout().addWidget(self.dashboard_button)

        self.layout.addLayout(self.work_grid)

    @staticmethod
    def _add_summary_field(layout: QVBoxLayout, name: str, value: str) -> QLabel:
        row = QHBoxLayout()
        label = QLabel(name)
        label.setObjectName("modernDetailLabel")
        label.setMinimumWidth(84)
        detail = QLabel(value)
        detail.setObjectName("modernWorkValue")
        detail.setWordWrap(True)
        row.addWidget(label)
        row.addWidget(detail, 1)
        layout.addLayout(row)
        return detail

    def _build_diagnostics(self) -> None:
        self.compatibility = QFrame(self.canvas)
        self.compatibility.setProperty("modernUi", True)
        self.compatibility.setProperty("modernCard", True)
        self.compatibility.setProperty("diagnosticCard", True)
        self.compatibility.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum
        )
        compatibility_layout = QVBoxLayout(self.compatibility)
        compatibility_layout.setContentsMargins(16, 14, 16, 16)
        compatibility_layout.setSpacing(10)

        heading = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(2)
        title = QLabel("系统状态与安全控制", self.compatibility)
        title.setObjectName("modernSectionTitle")
        note = QLabel(
            "V0.1.6 原始安全控件完整保留。只读保护属于受支持的安全运行模式，诊断与路径状态按需展开查看。",
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
        self.legacy_page.setProperty("legacyRoot", True)
        mark_legacy_descendants(self.legacy_page)
        self.legacy_page.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred
        )
        compatibility_layout.addWidget(self.legacy_page)
        self.legacy_page.hide()
        self.layout.addWidget(self.compatibility)

    @staticmethod
    def _make_card(
        title: str,
        note: str = "",
        action_text: str = "",
    ) -> QFrame:
        card = QFrame()
        card.setProperty("modernUi", True)
        card.setProperty("modernCard", True)
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(7)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        header.setSpacing(8)
        label = QLabel(title, card)
        label.setObjectName("modernSectionTitle")
        header.addWidget(label, 1)
        if action_text:
            action = QPushButton(action_text, card)
            action.setObjectName("modernOverviewFilter")
            action.setProperty("overviewFilter", True)
            action.setCursor(Qt.CursorShape.PointingHandCursor)
            header.addWidget(action, 0, Qt.AlignmentFlag.AlignTop)
        layout.addLayout(header)
        if note:
            helper = QLabel(note, card)
            helper.setObjectName("modernSectionNote")
            helper.setWordWrap(True)
            layout.addWidget(helper)
        return card

    @staticmethod
    def _add_chart_legend(
        card: QFrame,
        entries: tuple[tuple[str, str], ...],
    ) -> None:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(9)
        row.addStretch(1)
        for label, colour in entries:
            legend = QLabel(
                f'<span style="color:{colour}">●</span>&nbsp;{label}',
                card,
            )
            legend.setObjectName("modernChartLegend")
            legend.setTextFormat(Qt.TextFormat.RichText)
            row.addWidget(legend)
        card.layout().addLayout(row)

    def _build_preview_task_form(self) -> None:
        form = QWidget(self.task_card)
        form.setObjectName("modernPreviewTaskForm")
        grid = QGridLayout(form)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(7)
        grid.setColumnMinimumWidth(0, 74)
        grid.setColumnStretch(1, 1)

        fields = (
            ("账号表达式", "@美食探店主_*"),
            ("Earliest", "2024-03-01 00:00"),
        )
        for row_index, (label_text, value) in enumerate(fields):
            label = QLabel(label_text, form)
            label.setObjectName("modernPreviewFieldLabel")
            field = QLineEdit(value, form)
            field.setObjectName("modernPreviewField")
            field.setReadOnly(True)
            grid.addWidget(label, row_index, 0)
            grid.addWidget(field, row_index, 1)

        smart_note = QLabel(
            '<span style="color:#19C37D">●</span>&nbsp;智能跳过&nbsp;&nbsp;'
            "跳过已完成的账号，避免重复处理",
            form,
        )
        smart_note.setObjectName("modernPreviewSmartNote")
        smart_note.setTextFormat(Qt.TextFormat.RichText)
        grid.addWidget(smart_note, len(fields), 1)
        self.task_card.layout().addWidget(form)

    @staticmethod
    def _add_preview_queue_row(
        layout: QVBoxLayout,
        account: str,
        status: str,
        progress: int,
        speed: str,
        updated: str,
        kind: str,
    ) -> None:
        row = QFrame()
        row.setObjectName("modernQueueRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(5)

        index = QLabel(str(layout.count() + 1), row)
        index.setObjectName("modernQueueIndex")
        index.setFixedWidth(16)
        account_label = QLabel(account, row)
        account_label.setObjectName("modernQueueAccount")
        account_label.setMinimumWidth(86)
        status_label = QLabel(status, row)
        status_label.setObjectName("modernQueueStatus")
        status_label.setProperty("queueKind", kind)
        status_label.setFixedWidth(48)
        progress_bar = QProgressBar(row)
        progress_bar.setObjectName("modernQueueProgress")
        progress_bar.setRange(0, 100)
        progress_bar.setValue(max(0, min(100, int(progress))))
        progress_bar.setTextVisible(False)
        progress_bar.setFixedWidth(62)
        percentage = QLabel(f"{progress}%", row)
        percentage.setObjectName("modernQueuePercentage")
        percentage.setFixedWidth(40)
        speed_label = QLabel(speed, row)
        speed_label.setObjectName("modernQueueSpeed")
        speed_label.setFixedWidth(76)
        time_label = QLabel(updated, row)
        time_label.setObjectName("modernQueueTime")
        time_label.setFixedWidth(72)

        row_layout.addWidget(index)
        row_layout.addWidget(account_label, 1)
        row_layout.addWidget(status_label)
        row_layout.addWidget(progress_bar)
        row_layout.addWidget(percentage)
        row_layout.addWidget(speed_label)
        row_layout.addWidget(time_label)
        layout.addWidget(row)

    @staticmethod
    def _add_preview_attention_row(
        layout: QVBoxLayout,
        title: str,
        note: str,
        updated: str,
        kind: str,
    ) -> None:
        row = QFrame()
        row.setObjectName("modernAttentionRow")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 2, 0, 2)
        row_layout.setSpacing(6)
        dot = QLabel("●", row)
        dot.setObjectName("modernAttentionDot")
        dot.setProperty("attentionKind", kind)
        dot.setFixedWidth(15)

        text = QVBoxLayout()
        text.setContentsMargins(0, 0, 0, 0)
        text.setSpacing(1)
        title_label = QLabel(title, row)
        title_label.setObjectName("modernAttentionTitle")
        note_label = QLabel(note, row)
        note_label.setObjectName("modernAttentionNote")
        note_label.setWordWrap(True)
        text.addWidget(title_label)
        text.addWidget(note_label)

        time_label = QLabel(updated, row)
        time_label.setObjectName("modernAttentionTime")
        time_label.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignTop
        )
        time_label.setFixedWidth(46)
        row_layout.addWidget(dot)
        row_layout.addLayout(text, 1)
        row_layout.addWidget(time_label)
        layout.addWidget(row)

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
        self.compatibility.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred if expanded else QSizePolicy.Policy.Maximum,
        )
        self.diagnostic_toggle.setText("收起诊断" if expanded else "展开诊断")
        self.compatibility.updateGeometry()

    def activate(self) -> None:
        self._dashboard_refresh_requested = False
        self.refresh_from_host()
        self.request_real_dashboard_refresh()

    def request_real_dashboard_refresh(self) -> None:
        if self._preview_mock_data:
            return
        host = self._host
        if host.controller.startup_state is not StartupState.READY:
            return
        if self._dashboard_refresh_requested:
            return
        self._dashboard_refresh_requested = True
        host.refresh_result_dashboard(auto_refresh=True)

    def _open_watchlist_due(self) -> None:
        host = self._host
        host.watchlist_state_filter.setCurrentIndex(host.watchlist_state_filter.findData("watching"))
        host.watchlist_due_filter.setCurrentIndex(host.watchlist_due_filter.findData("due"))
        host.watchlist_search_edit.clear()
        host.tabs.setCurrentIndex(host.watchlist_tab_index)

    def refresh_watchlist_reminder(self) -> None:
        model = getattr(self._host, "watchlist_model", None)
        if model is None or getattr(self._host, "_watchlist_snapshot", None) is None:
            self.watchlist_reminder_text.setText("观察提醒待加载")
            return
        summary = model.reminder_summary()
        self.watchlist_reminder_text.setText(
            f"观察名单 · 待处理 {summary['due']} · 超期 {summary['overdue']} · "
            f"待恢复 {summary['recovery']} · 最早加入 {summary['oldest']}")
        urgent = summary["overdue"] or summary["recovery"]
        color = "#e05260" if urgent else "#c77d16" if summary["due"] else "#60809c"
        self.watchlist_reminder_text.setStyleSheet(
            f"color: {color}; font-weight: {700 if urgent else 500}; text-align: left; padding: 6px;")

    def refresh_from_host(self) -> None:
        host = self._host
        self.refresh_watchlist_reminder()
        self._refresh_startup(host)
        if self._preview_mock_data:
            self._refresh_preview()
            return
        if (
            host.controller.startup_state is StartupState.READY
            and getattr(host, "_dashboard_snapshot", None) is None
            and not self._dashboard_refresh_requested
        ):
            QTimer.singleShot(0, self.request_real_dashboard_refresh)
        self._refresh_runtime(host)
        self._refresh_trend(host)
        self._refresh_snapshot(getattr(host, "_dashboard_snapshot", None))

    def _refresh_preview(self) -> None:
        """Refresh the isolated visual fixture without reading or writing runtime data."""

        preview_metrics = (
            ("planned", "1,200", "+12%", "#19C37D"),
            ("started", "1,200", "", "#19C37D"),
            ("complete", "完整", "", "#19C37D"),
            ("reliable", "可靠", "", "#19C37D"),
            ("anomaly", "8", "", "#FF5967"),
            ("not_started", "0", "", "#FF5967"),
        )
        for key, value, delta, colour in preview_metrics:
            card = self.metrics[key]
            card.set_metric(value)
            card.note_label.setText("上次已结束任务")

        self.title.setText("运行总览")
        self.subtitle.setText("上次任务：日常下载 · A1–A1200　｜　结束于 2026-09-12 20:20")
        self.trend_chart.set_series(_PREVIEW_TREND_LABELS, _PREVIEW_TREND_SERIES)
        results = (("有新作品", 960), ("全部跳过", 160), ("无符合条件", 45), ("私密账号", 12), ("处理异常", 23), ("处理中断", 0))
        self.donut_chart.set_segments(results)
        self.composition_legend.setText(
            "<br>".join(f'{label}　{count}' for label, count in results)
        )
        self.composition_legend.setTextFormat(Qt.TextFormat.RichText)
        self.throughput_value.setText("8.0 账号/分钟")
        self.throughput_note.setText("按任务总用时计算的平均处理效率")
        self.throughput_chart.set_values(())
        self.throughput_bar.setValue(74)
        self.queue_state_value.setText("日常下载 · A1–A1200")
        self.attention_value.setText("23 个处理异常 · 8 个附加异常")

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
        if state is StartupState.DEGRADED_READ_ONLY:
            tooltip = "当前处于只读保护：写操作已禁用。这是受支持的安全运行模式。"
            if summary:
                tooltip += f"\n{summary}"
            self.startup_badge.setToolTip(tooltip)
        else:
            self.startup_badge.setToolTip(summary or text)
        self.startup_badge.style().unpolish(self.startup_badge)
        self.startup_badge.style().polish(self.startup_badge)

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
            self.throughput_chart.set_values(())
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
            note="任务证据可信"
            if reliable
            else ("；".join(map(str, reasons[:2])) or "证据不足"),
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
        self.throughput_chart.set_values(())

        attention_lines: list[str] = []
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
        self.attention_value.setText(
            "\n".join(attention_lines) or "最近任务没有需要关注的异常证据"
        )

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
            self.metric_grid.setColumnStretch(index % columns, 1)

    def _relayout_analytics(self, width: int) -> None:
        for card in (self.trend_card, self.composition_card, self.throughput_card):
            self.analytics_grid.removeWidget(card)
        for column in range(4):
            self.analytics_grid.setColumnStretch(column, 0)
        if width >= 1080:
            self.analytics_grid.addWidget(self.trend_card, 0, 0, 1, 2)
            self.analytics_grid.addWidget(self.composition_card, 0, 2)
            self.analytics_grid.addWidget(self.throughput_card, 0, 3)
            self.analytics_grid.setColumnStretch(0, 5)
            self.analytics_grid.setColumnStretch(1, 5)
            self.analytics_grid.setColumnStretch(2, 6)
            self.analytics_grid.setColumnStretch(3, 4)
        elif width >= 720:
            self.analytics_grid.addWidget(self.trend_card, 0, 0, 1, 2)
            self.analytics_grid.addWidget(self.composition_card, 1, 0)
            self.analytics_grid.addWidget(self.throughput_card, 1, 1)
            self.analytics_grid.setColumnStretch(0, 1)
            self.analytics_grid.setColumnStretch(1, 1)
        else:
            self.analytics_grid.addWidget(self.trend_card, 0, 0)
            self.analytics_grid.addWidget(self.composition_card, 1, 0)
            self.analytics_grid.addWidget(self.throughput_card, 2, 0)
            self.analytics_grid.setColumnStretch(0, 1)

    def _relayout_work(self, width: int) -> None:
        cards = (self.task_card, self.queue_card, self.attention_card)
        for card in cards:
            self.work_grid.removeWidget(card)
        for column in range(3):
            self.work_grid.setColumnStretch(column, 0)
        if width >= 900:
            for index, card in enumerate(cards):
                self.work_grid.addWidget(card, 0, index)
            # Match the approved dashboard composition: the task and queue areas
            # carry more information than the attention summary.
            self.work_grid.setColumnStretch(0, 5)
            self.work_grid.setColumnStretch(1, 5)
            self.work_grid.setColumnStretch(2, 3)
        else:
            for index, card in enumerate(cards):
                self.work_grid.addWidget(card, index, 0)
            self.work_grid.setColumnStretch(0, 1)

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        self.set_viewport_width(event.size().width())
