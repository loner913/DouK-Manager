"""Log-backed overview using the accepted completed-task composition."""

from datetime import date

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QLabel, QPushButton, QStyle, QComboBox, QHBoxLayout, QVBoxLayout, QGridLayout, QWidget

from douk_manager.background import TaskSpec, ClosePolicy, TaskState
from douk_manager.core.download_summary import AccountStatus
from douk_manager.core.result_dashboard import DashboardTaskIndexEntry
from douk_manager.startup import StartupState
from .completed_data import read_completed
from .overview_page import ModernOverviewPage, _STATUS_LABELS, _status_name


class CompletedOverviewPage(ModernOverviewPage):
    def __init__(self, host, legacy_page, parent=None):
        self._completed = None
        self._initial_refresh_requested = False
        self._summary_waiting = set()
        self._summary_failed = False
        self._attention_page = 0
        self._all_attention_rows = ()
        self._result_caption = "尚无可用任务结果"
        super().__init__(host, legacy_page, parent, completed_layout=True)
        self.refresh_button = QPushButton(self.canvas)
        self.refresh_button.setObjectName("modernOverviewFilter")
        self.refresh_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh_button.setFixedSize(36, 36)
        self.refresh_button.setToolTip("刷新上次任务结果")
        self.refresh_button.setAccessibleName("刷新上次任务结果")
        self.refresh_button.clicked.connect(self.request_real_dashboard_refresh)
        self.layout.itemAt(0).layout().addWidget(self.refresh_button)
        for card in (self.trend_card, self.composition_card):
            for button in card.findChildren(QPushButton, "modernOverviewFilter"):
                button.hide()
        self.trend_note = self.trend_card.findChild(QLabel, "modernSectionNote")
        self.attention_note = self.attention_card.findChild(QLabel, "modernSectionNote")
        self.donut_chart.setMinimumWidth(150)
        self.composition_legend.setMinimumWidth(240)
        self.composition_legend.setWordWrap(False)
        self.composition_legend.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        self.composition_legend.setContentsMargins(0, 10, 0, 0)
        # Let the legend start beside the heading while preserving the chart row.
        composition_layout = self.composition_card.layout()
        heading = composition_layout.takeAt(0).layout()
        note = composition_layout.takeAt(0).widget()
        chart_row = composition_layout.takeAt(0).layout()
        chart_row.removeWidget(self.donut_chart)
        chart_row.removeWidget(self.composition_legend)
        composition_grid = QGridLayout()
        composition_grid.setContentsMargins(0, 0, 0, 0)
        composition_grid.setHorizontalSpacing(8)
        composition_grid.setVerticalSpacing(composition_layout.spacing())
        composition_grid.addLayout(heading, 0, 0)
        composition_grid.addWidget(note, 1, 0)
        composition_grid.addWidget(self.donut_chart, 2, 0)
        composition_grid.addWidget(self.composition_legend, 0, 1, 3, 1)
        composition_grid.setColumnStretch(0, 1)
        composition_grid.setColumnStretch(1, 1)
        composition_grid.setRowStretch(2, 1)
        composition_layout.addLayout(composition_grid, 1)
        # Keep the body expanding even when filtering removes every account row.
        card_layout = self.attention_card.layout()
        body_index = card_layout.indexOf(self.attention_rows)
        card_layout.takeAt(body_index)
        self.attention_body = QWidget(self.attention_card)
        body_layout = QVBoxLayout(self.attention_body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(3)
        body_layout.addWidget(self.attention_value)
        body_layout.addLayout(self.attention_rows)
        body_layout.addStretch(1)
        self.attention_body.setMinimumHeight(self.attention_rows.sizeHint().height())
        card_layout.insertWidget(body_index, self.attention_body, 1)
        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.attention_filter = QComboBox(self.attention_card)
        self.attention_filter.setObjectName("modernAttentionFilter")
        self.attention_filter.setProperty("modernControl", True)
        for label, key in (("全部异常", "all"), ("处理异常", "error"), ("处理中断", "interrupted"),
                           ("附加异常", "anomaly"), ("处理前异常", "pre_start_error")):
            self.attention_filter.addItem(label, key)
        self.attention_filter.setMinimumWidth(100)
        self.attention_filter.currentIndexChanged.connect(self._filter_attention)
        controls.addWidget(self.attention_filter, 1)
        self.attention_page_label = QLabel(self.attention_card)
        self.attention_page_label.setObjectName("modernAttentionPager")
        controls.addWidget(self.attention_page_label)
        self.attention_previous = QPushButton(self.attention_card)
        self.attention_next = QPushButton(self.attention_card)
        for button, tip, step in (
            (self.attention_previous, "上一页异常", -1),
            (self.attention_next, "下一页异常", 1),
        ):
            button.setObjectName("modernOverviewFilter")
            button.setProperty("attentionPager", True)
            button.setFixedSize(30, 30)
            button.setText("‹" if step < 0 else "›")
            button.setToolTip(tip)
            button.setAccessibleName(tip)
            button.clicked.connect(lambda _checked=False, delta=step: self._turn_attention(delta))
            controls.addWidget(button)
        self.attention_card.layout().insertLayout(2, controls)
        for button in (self.queue_button, self.dashboard_button):
            button.clicked.disconnect()
            button.clicked.connect(self._open_completed)
        self._apply_empty()
        host.coordinator.task_settled.connect(self._summary_settled)
        host.coordinator.task_removed.connect(self._summary_removed)

    def _summary_settled(self, task_id, generation, outcome, _payload):
        binding = self._host._background_bindings.get(task_id)
        if (binding is not None and binding.spec.task_type == "download_summary"
                and self._host._background_binding_is_current(binding, generation)):
            self._summary_waiting.add(task_id)
            self._summary_failed = outcome is not TaskState.SUCCEEDED

    def _summary_removed(self, task_id):
        if task_id in self._summary_waiting:
            self._summary_waiting.remove(task_id)
            QTimer.singleShot(0, self.request_real_dashboard_refresh)

    def refresh_from_host(self):
        # The inherited 350ms timer mirrors status only; disk reads are event driven.
        self.refresh_watchlist_reminder()
        self._refresh_startup(self._host)
        if (
            self._host.controller.startup_state is StartupState.READY
            and not self._initial_refresh_requested
        ):
            self._initial_refresh_requested = True
            QTimer.singleShot(0, self.request_real_dashboard_refresh)

    def _refresh_preview(self):
        pass

    def request_real_dashboard_refresh(self):
        if self._host.controller.startup_state is not StartupState.READY:
            return
        self.subtitle.setText(self._result_caption + " · 正在刷新")
        service = self._host.controller.result_dashboard
        today = date.today()
        task_id = self._host._submit_coalesced_background(
            TaskSpec(task_type="completed_overview", display_name="刷新总览任务结果",
                     resource_keys=frozenset({"result_logs"}), deduplicate_key="completed_overview",
                     dynamic_cancellation=True, close_policy=ClosePolicy.CANCEL),
            lambda context: read_completed(service, today, context),
            on_success=self._apply_completed,
            on_failure=lambda _: self.subtitle.setText(self._result_caption + " · 刷新失败，请重试"),
            generation_key="completed_overview",
        )
        if task_id is None:
            self.subtitle.setText(self._result_caption + " · 暂时忙碌，请重试")

    def _clear_rows(self):
        if hasattr(self, "attention_body"):
            self.attention_body.setMinimumHeight(max(
                self.attention_body.minimumHeight(), self.attention_rows.sizeHint().height()))
        while self.attention_rows.count():
            widget = self.attention_rows.takeAt(0).widget()
            if widget:
                widget.hide()
                widget.deleteLater()

    def _apply_empty(self):
        self._completed = None
        self.title.setText("运行总览")
        self._refresh_snapshot(None)
        self._clear_rows()
        self._all_attention_rows = ()
        self._attention_page = 0
        self._render_attention_page()
        self.attention_value.show()
        for fields in (self.summary_fields, self.efficiency_fields):
            for label in fields.values():
                label.setText("—")
        self.queue_state_value.setText("尚无可用任务")
        self.attention_note.setText("尚无已结束任务")
        self.queue_card.findChild(QLabel, "modernSectionNote").setText("等待任务日志")
        self.queue_button.setEnabled(False)
        self.dashboard_button.setEnabled(False)

    def _apply_completed(self, result):
        if self._host.controller.startup_state is StartupState.CLOSING:
            return
        snapshot = result.snapshot
        if snapshot is None and result.notice and self._completed is not None:
            self.subtitle.setText(self._result_caption + " · " + result.notice + "，保留上次结果")
            return
        previous_page = self._attention_page
        previous_path = self._completed.task_log if self._completed else None
        self._apply_empty()
        self.trend_chart.set_series(result.labels, result.series)
        self.trend_chart.setProperty("emptyMessage", "历史证据不完整" if result.trend_incomplete else "最近7天暂无已完成任务")
        self.trend_note.setText("历史证据不完整，暂不绘制趋势" if result.trend_incomplete
                               else "近7天 · 按任务结束日期统计账号次数")
        if snapshot is None:
            self._result_caption = result.notice or "尚无已结束且汇总可用的任务"
            self.subtitle.setText(self._result_caption)
            return
        self._completed = snapshot
        self._refresh_snapshot(snapshot)
        for key in ("planned", "started", "anomaly", "not_started"):
            value = self.metrics[key].value_label.text()
            if value.isdigit():
                self.metrics[key].value_label.setText(f"{int(value):,}")
            self.metrics[key].note_label.setText("上次已结束任务")
        ended = snapshot.ended_at.strftime("%Y-%m-%d %H:%M:%S")
        name = snapshot.task_template or "未命名任务"
        self._result_caption = f"上次任务：{name} ｜ 结束于 {ended}"
        if self._summary_failed:
            self._result_caption += " · 新任务汇总失败，保留可用结果"
        self.subtitle.setText(self._result_caption + (" · " + result.notice if result.notice else ""))
        self.queue_state_value.setText(name)
        self.queue_card.findChild(QLabel, "modernSectionNote").setText("任务已结束 · 日志汇总可用")
        self.summary_fields["账号范围"].setText("日志未提供明确范围")
        self.summary_fields["开始时间"].setText(snapshot.started_at.strftime("%Y-%m-%d %H:%M:%S")
                                                if snapshot.started_at else "未知")
        self.summary_fields["结束时间"].setText(ended)
        exit_text = "正常退出" if snapshot.exit_code == 0 else (
            "退出状态未知" if snapshot.exit_code is None else f"退出码 {snapshot.exit_code}")
        self.summary_fields["执行结果"].setText(exit_text + " · " + self.metrics["complete"].value_label.text())
        duration = snapshot.duration_seconds
        self.efficiency_fields["任务总用时"].setText(
            f"{duration // 3600}小时 {(duration % 3600) // 60}分钟 {duration % 60}秒"
            if duration is not None else "未知")
        self.efficiency_fields["实际开始"].setText(self._count(snapshot.started_count))
        self.efficiency_fields["有新作品下载"].setText(self._count(snapshot.count_for(AccountStatus.DOWNLOADED)))
        self.throughput_note.setText("按任务总用时计算，含等待和暂停" if duration and duration > 0 else "时长不足，无法计算平均效率")
        counts = snapshot.main_status_counts
        valid = all(v is not None for _, v in counts) and sum(v for _, v in counts) == snapshot.started_count
        self.donut_chart.set_segments(tuple((_STATUS_LABELS[_status_name(s)], v) for s, v in counts) if valid else ())
        self.composition_legend.setTextFormat(Qt.TextFormat.RichText)
        self.composition_legend.setObjectName("modernWorkValue")
        self.composition_legend.style().unpolish(self.composition_legend)
        self.composition_legend.style().polish(self.composition_legend)
        legend_rows = []
        names = ("有新作品", "全部跳过", "无符合条件", "私密账号", "处理异常", "处理中断")
        for index, ((status, value), name) in enumerate(zip(counts, names)):
            color = self.donut_chart._COLOURS[index]
            number = f"{value:,}" if value is not None else "未知"
            ratio = f"{value / snapshot.started_count:.1%}" if valid and snapshot.started_count else "—"
            legend_rows.append(f'<tr><td style="white-space:nowrap; padding-right:12px; padding-bottom:12px"><span style="color:{color}">■</span> {name}</td>'
                               f'<td align="right" style="padding-right:12px; padding-bottom:12px">{number}</td>'
                               f'<td align="right" style="padding-bottom:12px">{ratio}</td></tr>')
        self.composition_legend.setText('<table width="100%" cellspacing="0" cellpadding="0" style="font-size:16px">'
            '<tr><td style="padding-right:12px; padding-bottom:12px">状态</td>'
            '<td align="right" style="padding-right:12px; padding-bottom:12px">账号数</td>'
            '<td align="right" style="padding-bottom:12px">占比</td></tr>' + ''.join(legend_rows) + '</table>'
            + ("" if valid else "<br>部分证据，占比不可确认"))
        self._clear_rows()
        self._all_attention_rows = tuple(sorted((row for row in snapshot.account_rows
            if row.status in (AccountStatus.ERROR, AccountStatus.INTERRUPTED, "pre_start_error")
            or row.completed_with_anomaly), key=lambda r: r.a_number))
        self._attention_page = previous_page if previous_path == snapshot.task_log else 0
        self.attention_note.setText(f"任务结束于 {ended}")
        self._render_attention_page()
        if not snapshot.details_complete:
            self.attention_value.setText("账号明细不完整，请查看任务汇总")
        elif snapshot.pre_start_error_count:
            self.attention_value.setText(f"进入处理前异常：{snapshot.pre_start_error_count}")
        self.queue_button.setEnabled(True)
        self.dashboard_button.setEnabled(True)

    def _filter_attention(self, _index):
        self._attention_page = 0
        self._render_attention_page()

    def _turn_attention(self, delta):
        self._attention_page += delta
        self._render_attention_page()

    def _render_attention_page(self):
        self._clear_rows()
        key = self.attention_filter.currentData()
        rows = [row for row in self._all_attention_rows if key == "all"
                or (key == "anomaly" and row.completed_with_anomaly) or row.status == key]
        self._attention_page = max(0, min(self._attention_page, max(0, (len(rows)-1)//5)))
        start = self._attention_page * 5
        for row in rows[start:start+5]:
            parts = []
            if row.status in (AccountStatus.ERROR, AccountStatus.INTERRUPTED):
                parts.append(_STATUS_LABELS[_status_name(row.status)])
            elif row.status == "pre_start_error":
                parts.append("进入处理前异常")
            if row.completed_with_anomaly:
                parts.append("完成后附加异常")
            self._add_preview_attention_row(self.attention_rows, f"A{row.a_number}", " · ".join(parts), "",
                "warning" if row.completed_with_anomaly and row.status not in (AccountStatus.ERROR, AccountStatus.INTERRUPTED) else "danger")
        self.attention_page_label.setText(f"{start+1}–{min(start+5,len(rows))} / {len(rows)}" if rows else "0 / 0")
        self.attention_previous.setEnabled(start > 0)
        self.attention_next.setEnabled(start+5 < len(rows))
        incomplete = self._completed is not None and not self._completed.details_complete
        self.attention_value.setVisible(not rows or incomplete)
        if incomplete:
            self.attention_value.setText("账号明细不完整，请查看任务汇总")
        elif not rows:
            self.attention_value.setText("当前类别没有异常记录" if self._completed else "尚无可用任务结果")

    @staticmethod
    def _count(value):
        return f"{value:,} 个账号" if value is not None else "未知"

    def _open_completed(self):
        snapshot = self._completed
        if snapshot is None:
            return
        service = self._host.controller.result_dashboard
        self._host._submit_coalesced_background(
            TaskSpec(task_type="completed_detail", display_name="打开任务结果",
                     resource_keys=frozenset({"result_logs"}), dynamic_cancellation=True),
            lambda context: service.selected_task(snapshot.task_log,
                expected_fingerprint=snapshot.fingerprint, context=context),
            on_success=self._show_detail,
            on_failure=lambda _: self.subtitle.setText(self._result_caption + " · 日志已变化或不可用，请刷新"),
            generation_key="completed_detail",
        )

    def _show_detail(self, snapshot):
        if self._completed is None or self._completed.fingerprint != snapshot.fingerprint:
            return
        # Reuse the existing presenter without changing the overview's selection.
        host = self._host
        key = host._dashboard_key(snapshot.task_log)
        entry = DashboardTaskIndexEntry(snapshot.task_log, snapshot.fingerprint,
                                        snapshot.task_template, snapshot.ended_at, "ready", "")
        host._dashboard_entries[key] = entry
        selector = host.dashboard_task_selector
        selector.blockSignals(True)
        try:
            index = selector.findData(key)
            if index < 0:
                selector.addItem(snapshot.task_template or snapshot.task_log.name, key)
                index = selector.count() - 1
            selector.setCurrentIndex(index)
        finally:
            selector.blockSignals(False)
        host._dashboard_snapshot = snapshot
        host._render_dashboard_snapshot(snapshot)
        self._navigate("结果看板")
