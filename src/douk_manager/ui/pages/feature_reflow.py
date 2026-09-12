"""Presentation-only reflow profiles for the V0.1.6 feature pages.

The functions in this module deliberately move existing ``QLayoutItem`` objects
instead of recreating controls.  Every button, editor, model-backed view and signal
connection therefore remains the original V0.1.6 instance; only its visual grouping
changes for the V0.1.7 Fluent shell.
"""

from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QScrollArea,
    QSizePolicy,
    QTableView,
    QTableWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class FeatureReflowError(RuntimeError):
    """Raised when a known V0.1.6 page no longer has the expected top-level shape."""


def _content_widget(page: QWidget) -> QWidget:
    if isinstance(page, QScrollArea) and page.widget() is not None:
        return page.widget()
    return page


def _root_layout(page: QWidget) -> QLayout:
    content = _content_widget(page)
    layout = content.layout()
    if layout is None:
        raise FeatureReflowError("feature page has no root layout")
    return layout


def _take_all(layout: QLayout) -> list[QLayoutItem]:
    items: list[QLayoutItem] = []
    while layout.count():
        item = layout.takeAt(0)
        if item is not None:
            items.append(item)
    return items


def _restore(layout: QLayout, items: Iterable[QLayoutItem]) -> None:
    for item in items:
        _add_item(layout, item)


def _add_item(layout: QLayout, item: QLayoutItem, stretch: int = 0) -> None:
    widget = item.widget()
    child_layout = item.layout()
    spacer = item.spacerItem()
    if widget is not None:
        if isinstance(layout, (QVBoxLayout, QHBoxLayout)):
            layout.addWidget(widget, stretch)
        else:
            layout.addWidget(widget)
        return
    if child_layout is not None:
        if isinstance(layout, (QVBoxLayout, QHBoxLayout)):
            layout.addLayout(child_layout, stretch)
        else:
            layout.addItem(item)
        return
    if spacer is not None:
        layout.addItem(spacer)


def _item_widget(item: QLayoutItem) -> QWidget | None:
    return item.widget()


def _mark_banner(item: QLayoutItem, kind: str = "info") -> None:
    widget = item.widget()
    if isinstance(widget, QLabel):
        widget.setProperty("legacyBanner", True)
        widget.setProperty("legacyBannerKind", kind)
        widget.setWordWrap(True)
        widget.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Maximum)


def _prepare_output(
    item: QLayoutItem,
    *,
    placeholder: str,
    minimum_height: int = 220,
) -> None:
    widget = item.widget()
    if isinstance(widget, QTextEdit):
        widget.setProperty("legacyOutput", True)
        widget.setPlaceholderText(placeholder)
        widget.setMinimumHeight(minimum_height)
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)


def _prepare_table(item: QLayoutItem, *, minimum_height: int = 360) -> None:
    widget = item.widget()
    if isinstance(widget, (QTableView, QTableWidget)):
        widget.setProperty("legacyDataView", True)
        widget.setMinimumHeight(minimum_height)
        widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)


def _card(
    parent: QWidget,
    title: str,
    items: Iterable[QLayoutItem],
    *,
    note: str = "",
    match_row_height: bool = False,
) -> QFrame:
    frame = QFrame(parent)
    frame.setProperty("legacySectionCard", True)
    if match_row_height:
        frame.setProperty("legacyMatchRowHeight", True)
        frame.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
    layout = QVBoxLayout(frame)
    layout.setContentsMargins(18, 16, 18, 18)
    layout.setSpacing(12)

    if title:
        title_label = QLabel(title, frame)
        title_label.setProperty("legacySectionTitle", True)
        layout.addWidget(title_label)
    if note:
        note_label = QLabel(note, frame)
        note_label.setProperty("legacySectionNote", True)
        note_label.setWordWrap(True)
        layout.addWidget(note_label)

    for item in items:
        _add_item(layout, item, 1 if item.widget() and _is_expanding_widget(item.widget()) else 0)
    return frame


def _is_expanding_widget(widget: QWidget) -> bool:
    return isinstance(widget, (QTextEdit, QTableView, QTableWidget))


def _configure_root(layout: QLayout) -> None:
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(14)
    # Compact rows must start below the heading, even in a maximized window.
    layout.setAlignment(Qt.AlignmentFlag.AlignTop)


def _require(items: list[QLayoutItem], minimum: int, title: str) -> None:
    if len(items) < minimum:
        raise FeatureReflowError(
            f"{title} top-level layout changed: expected >= {minimum}, got {len(items)}"
        )


def _reflow_task(page: QWidget, root: QLayout, items: list[QLayoutItem]) -> None:
    _require(items, 4, "账号任务")
    intro, form, actions, output, *tail = items
    output.widget().setProperty("legacyFillAvailable", True)
    root.setAlignment(Qt.AlignmentFlag(0))
    _mark_banner(intro)
    _prepare_output(output, placeholder="任务预览、创建结果和运行信息会显示在这里。", minimum_height=420)

    content = _content_widget(page)
    _add_item(root, intro)
    root.addWidget(_card(content, "任务配置", (form,)))
    root.addWidget(
        _card(
            content,
            "任务操作",
            (actions,),
        )
    )
    root.addWidget(_card(content, "预览与执行结果", (output,)), 1)
    _restore(root, tail)


def _reflow_audit(page: QWidget, root: QLayout, items: list[QLayoutItem]) -> None:
    _require(items, 11, "账号审计")
    controls, notice, summary, progress, warnings, filters, table, details, decisions, actions, output, *tail = items
    _mark_banner(notice, "warning")
    _prepare_table(table, minimum_height=440)
    _prepare_output(output, placeholder="审计执行与应用结果会显示在这里。", minimum_height=120)

    status_labels = tuple(
        item.widget() for item in (progress, warnings)
        if isinstance(item.widget(), QLabel)
    )

    def sync_status_visibility() -> None:
        for label in status_labels:
            label.setVisible(bool(label.text().strip()))
        cards = (top.itemAt(0).widget(), top.itemAt(1).widget())
        height = max(card.layout().sizeHint().height() for card in cards)
        for card in cards:
            card.setFixedHeight(height)
            card.layout().setAlignment(Qt.AlignmentFlag.AlignTop)

    # Legacy callbacks update these labels directly; preserve those instances.
    status_timer = QTimer(page)
    status_timer.setInterval(150)
    status_timer.timeout.connect(sync_status_visibility)
    status_timer.start()

    content = _content_widget(page)
    top = QHBoxLayout()
    top.setSpacing(14)
    top.addWidget(
        _card(content, "审计参数", (controls,)),
        5,
    )
    top.addWidget(
        _card(
            content,
            "审计状态",
            (notice, summary, progress, warnings),
        ),
        7,
    )
    root.addLayout(top)
    sync_status_visibility()
    root.addWidget(_card(content, "筛选与查找", (filters,)))
    root.addWidget(_card(content, "账号审计结果", (table,)), 1)

    bottom = QHBoxLayout()
    bottom.setSpacing(14)
    bottom.addWidget(
        _card(content, "选中账号详情", (details,)),
        4,
    )
    bottom.addWidget(
        _card(
            content,
            "决定与应用",
            (decisions, actions, output),
            note="确认预览后应用决定。",
        ),
        6,
    )
    root.addLayout(bottom)
    _restore(root, tail)


def _reflow_batch(page: QWidget, root: QLayout, items: list[QLayoutItem]) -> None:
    _require(items, 4, "批次生成")
    intro, form, actions, output, *tail = items
    output.widget().setProperty("legacyFillAvailable", True)
    root.setAlignment(Qt.AlignmentFlag(0))
    _mark_banner(intro)
    _prepare_output(output, placeholder="批次生成结果与任务文件列表会显示在这里。", minimum_height=420)
    content = _content_widget(page)
    _add_item(root, intro)
    root.addWidget(_card(content, "批次参数", (form, actions)))
    root.addWidget(_card(content, "生成结果", (output,)), 1)
    _restore(root, tail)


def _reflow_queue(page: QWidget, root: QLayout, items: list[QLayoutItem]) -> None:
    _require(items, 5, "下载队列")
    intro, task_area, options, actions, output, *tail = items
    _mark_banner(intro)
    _prepare_output(output, placeholder="队列运行、下载结果汇总和后续动作信息会显示在这里。", minimum_height=220)
    content = _content_widget(page)
    _add_item(root, intro)

    columns = QHBoxLayout()
    columns.setSpacing(14)
    columns.addWidget(
        _card(content, "任务模板与顺序", (task_area,), match_row_height=True),
        7,
    )
    columns.addWidget(
        _card(content, "本次运行策略", (options,)),
        4,
    )
    root.addLayout(columns, 1)
    root.addWidget(_card(content, "快捷操作", (actions,)))
    root.addWidget(_card(content, "队列运行日志", (output,)))
    _restore(root, tail)


def _reflow_collector(page: QWidget, root: QLayout, items: list[QLayoutItem]) -> None:
    _require(items, 4, "账号采集")
    intro, paths, actions, output, *tail = items
    output.widget().setProperty("legacyFillAvailable", True)
    root.setAlignment(Qt.AlignmentFlag(0))
    _mark_banner(intro)
    if isinstance(paths.widget(), QLabel):
        paths.widget().setProperty("legacyPathSummary", True)
        paths.widget().setWordWrap(True)
    _prepare_output(output, placeholder="采集服务启动、迁移与导出结果会显示在这里。", minimum_height=420)
    content = _content_widget(page)
    root.addWidget(
        _card(
            content,
            "采集服务与正式路径",
            (intro, paths, actions),
        ),
    )
    root.addWidget(_card(content, "服务输出", (output,)), 1)
    _restore(root, tail)


def _reflow_post(page: QWidget, root: QLayout, items: list[QLayoutItem]) -> None:
    _require(items, 3, "截图与索引")
    paths, actions, output, *tail = items
    output.widget().setProperty("legacyFillAvailable", True)
    root.setAlignment(Qt.AlignmentFlag(0))
    _prepare_output(output, placeholder="截图归档、索引刷新与清理结果会显示在这里。", minimum_height=440)
    content = _content_widget(page)
    root.addWidget(_card(content, "路径与维护操作", (paths, actions)))
    root.addWidget(_card(content, "维护结果", (output,)), 1)
    _restore(root, tail)


def _reflow_results(page: QWidget, root: QLayout, items: list[QLayoutItem]) -> None:
    _require(items, 5, "下载结果")
    intro, filters, table, note, refreshed, *tail = items
    _mark_banner(intro)
    _prepare_table(table, minimum_height=520)
    content = _content_widget(page)
    _add_item(root, intro)
    root.addWidget(_card(content, "筛选条件", (filters,)))
    root.addWidget(_card(content, "账号结果", (table,)), 1)
    status = _card(content, "结果状态", (note, refreshed))
    status.setProperty("legacyCompactCard", True)
    root.addWidget(status)
    _restore(root, tail)


def _reflow_settings(page: QWidget, root: QLayout, items: list[QLayoutItem]) -> None:
    _require(items, 9, "设置")
    warning, paths, defaults, note, save_paths, save_all, update, rollback, output, *tail = items
    _mark_banner(warning, "warning")
    _mark_banner(note)
    _prepare_output(output, placeholder="保存、更新、回退和安全验证结果会显示在这里。", minimum_height=180)
    content = _content_widget(page)
    _add_item(root, warning)

    config = QHBoxLayout()
    config.setSpacing(14)
    config.addWidget(
        _card(content, "正式路径", (paths,), match_row_height=True),
        6,
    )
    config.addWidget(
        _card(content, "任务默认值", (defaults, note), match_row_height=True),
        4,
    )
    root.addLayout(config)

    # Keep the action layout on a real, parented holder. The previous temporary
    # QWidgetItem wrapper could be collected with its holder and destroy these
    # original buttons even though MainWindow still referenced them.
    action_holder = QWidget(content)
    action_holder.setProperty("legacyLayoutHolder", True)
    action_layout = QHBoxLayout(action_holder)
    action_layout.setContentsMargins(0, 0, 0, 0)
    action_layout.addStretch(1)
    action_layout.addItem(save_paths)
    action_layout.addItem(save_all)
    action_card = _card(
        content,
        "保存与重新验证",
        (),
        note="保存后重新检查路径与运行环境。",
    )
    action_card.layout().addWidget(action_holder)
    root.addWidget(action_card)

    maintenance = QHBoxLayout()
    maintenance.setSpacing(14)
    maintenance.addWidget(
        _card(content, "下载引擎更新", (update,), match_row_height=True),
        4,
    )
    maintenance.addWidget(
        _card(content, "历史引擎回退", (rollback,), match_row_height=True),
        7,
    )
    root.addLayout(maintenance, 1)
    root.addWidget(_card(content, "操作结果", (output,)))
    _restore(root, tail)


def _layout_item(layout: QLayout) -> QLayoutItem:
    """Wrap a layout in a temporary item accepted by the common card helper."""

    holder = QWidget()
    holder.setProperty("legacyLayoutHolder", True)
    holder.setLayout(layout)
    # QWidgetItem is created by QLayout when adding the holder.  Returning a tiny
    # ad-hoc layout item is unnecessary; use a one-widget layout and take it back.
    container = QVBoxLayout()
    container.setContentsMargins(0, 0, 0, 0)
    container.addWidget(holder)
    item = container.takeAt(0)
    if item is None:  # pragma: no cover - Qt guarantees the item above.
        raise FeatureReflowError("failed to wrap layout")
    return item


def _reflow_dashboard(page: QWidget, root: QLayout, items: list[QLayoutItem]) -> None:
    _require(items, 7, "结果看板")
    controls, message, current, metrics, details, accounts, log_stats, *tail = items
    content = _content_widget(page)
    root.addWidget(_card(content, "任务与日志来源", (controls, message, current)))
    root.addWidget(_card(content, "任务概览", (metrics,)))
    root.addWidget(_card(content, "状态与完整性", (details,)))
    root.addWidget(_card(content, "账号关注与定位", (accounts,)), 1)
    root.addWidget(_card(content, "运行日志统计", (log_stats,)))
    _restore(root, tail)


def _generic_reflow(page: QWidget, root: QLayout, items: list[QLayoutItem]) -> None:
    content = _content_widget(page)
    for index, item in enumerate(items, start=1):
        widget = _item_widget(item)
        if isinstance(widget, QLabel) and index == 1:
            _mark_banner(item)
            _add_item(root, item)
            continue
        root.addWidget(_card(content, "" if len(items) == 1 else f"功能区 {index}", (item,)))


_REFLOWERS = {
    "账号任务": _reflow_task,
    "账号审计": _reflow_audit,
    "批次生成": _reflow_batch,
    "下载队列": _reflow_queue,
    "账号采集": _reflow_collector,
    "截图与索引": _reflow_post,
    "下载结果": _reflow_results,
    "结果看板": _reflow_dashboard,
    "设置": _reflow_settings,
}


def reflow_feature_page(page: QWidget, title: str) -> str:
    """Apply one visual profile and return the profile name.

    Only top-level layout ownership changes.  The original controls themselves are
    reused, so V0.1.6 state, models, signal/slot connections and controller gates
    remain intact.
    """

    root = _root_layout(page)
    items = _take_all(root)
    _configure_root(root)
    if not items:
        return "empty"

    reflower = _REFLOWERS.get(title, _generic_reflow)
    try:
        reflower(page, root, items)
    except Exception:
        # A future V0.1.6 layout change must not make a feature page disappear.
        while root.count():
            root.takeAt(0)
        _restore(root, items)
        raise
    return title


__all__ = ["FeatureReflowError", "reflow_feature_page"]
