"""Left-side navigation shell for the modern interface."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QSize, Signal, Qt
from PySide6.QtWidgets import QButtonGroup, QFrame, QPushButton, QVBoxLayout, QWidget

from ..icons import vector_icon
from ..theme.tokens import UiMetrics


@dataclass(frozen=True, slots=True)
class NavigationItem:
    route: str
    label: str
    icon_text: str = ""
    placement: str = "main"


_ICON_BY_LABEL = {
    "总览": "home",
    "账号任务": "clipboard",
    "账号审计": "shield",
    "批次生成": "plus-square",
    "下载队列": "download",
    "账号采集": "users",
    "截图与索引": "image",
    "下载结果": "folder",
    "结果看板": "chart",
    "设置": "settings",
}


class NavigationSidebar(QWidget):
    """Pure navigation view with font-independent vector icons."""

    route_requested = Signal(str)

    def __init__(
        self,
        items: tuple[NavigationItem, ...],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("modernUi", True)
        self.setObjectName("modernSidebar")
        self.setFixedWidth(UiMetrics.SIDEBAR_WIDTH)
        self._collapsed = False
        self._items = {item.route: item for item in items}

        self._buttons: dict[str, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 18, 10, 18)
        layout.setSpacing(5)

        main_items = tuple(item for item in items if item.placement != "bottom")
        bottom_items = tuple(item for item in items if item.placement == "bottom")
        for item in main_items:
            layout.addWidget(self._make_button(item))

        layout.addStretch(1)
        if bottom_items:
            divider = QFrame(self)
            divider.setObjectName("modernSidebarDivider")
            divider.setFrameShape(QFrame.Shape.HLine)
            layout.addWidget(divider)
            layout.addSpacing(5)
            for item in bottom_items:
                layout.addWidget(self._make_button(item))

    def _make_button(self, item: NavigationItem) -> QPushButton:
        button = QPushButton(item.label, self)
        button.setCheckable(True)
        button.setProperty("navigationItem", True)
        button.setProperty("placement", item.placement)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.setToolTip(item.label)
        icon_name = _ICON_BY_LABEL.get(item.label, "home")
        button.setIcon(
            vector_icon(
                icon_name,
                color="#60738E",
                checked_color="#1677FF",
                size=21,
            )
        )
        button.setIconSize(QSize(21, 21))
        button.clicked.connect(
            lambda checked=False, route=item.route: self.route_requested.emit(route)
        )
        self._group.addButton(button)
        self._buttons[item.route] = button
        return button

    def set_collapsed(self, collapsed: bool) -> None:
        if self._collapsed == collapsed:
            return
        self._collapsed = collapsed
        self.setFixedWidth(
            UiMetrics.SIDEBAR_COLLAPSED_WIDTH
            if collapsed
            else UiMetrics.SIDEBAR_WIDTH
        )
        for route, button in self._buttons.items():
            item = self._items[route]
            button.setText("" if collapsed else item.label)
            button.setIconSize(QSize(23 if collapsed else 21, 23 if collapsed else 21))
            button.setProperty("collapsed", collapsed)
            button.style().unpolish(button)
            button.style().polish(button)

    def set_current_route(self, route: str) -> None:
        button = self._buttons.get(route)
        if button is not None:
            button.setChecked(True)

    def route_for_query(self, query: str) -> str | None:
        needle = query.strip().casefold()
        if not needle:
            return None
        exact = next(
            (
                item.route
                for item in self._items.values()
                if item.label.casefold() == needle
            ),
            None,
        )
        if exact is not None:
            return exact
        return next(
            (
                item.route
                for item in self._items.values()
                if needle in item.label.casefold()
            ),
            None,
        )
