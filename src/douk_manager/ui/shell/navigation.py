"""Left-side navigation shell for the modern interface."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QButtonGroup, QPushButton, QVBoxLayout, QWidget

from ..theme.tokens import UiMetrics


@dataclass(frozen=True, slots=True)
class NavigationItem:
    route: str
    label: str
    icon_text: str = ""


class NavigationSidebar(QWidget):
    """Pure navigation view.

    It emits route identifiers only and has no dependency on controllers,
    database access, background tasks, or legacy page implementations.
    """

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

        self._buttons: dict[str, QPushButton] = {}
        self._group = QButtonGroup(self)
        self._group.setExclusive(True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(UiMetrics.SPACE_S, UiMetrics.SPACE_L, UiMetrics.SPACE_S, UiMetrics.SPACE_L)
        layout.setSpacing(UiMetrics.SPACE_XS)

        for item in items:
            button = QPushButton(self._button_text(item), self)
            button.setCheckable(True)
            button.setProperty("navigationItem", True)
            button.clicked.connect(lambda checked=False, route=item.route: self.route_requested.emit(route))
            self._group.addButton(button)
            self._buttons[item.route] = button
            layout.addWidget(button)

        layout.addStretch(1)

    @staticmethod
    def _button_text(item: NavigationItem) -> str:
        return f"{item.icon_text}  {item.label}".strip()

    def set_current_route(self, route: str) -> None:
        button = self._buttons.get(route)
        if button is not None:
            button.setChecked(True)
