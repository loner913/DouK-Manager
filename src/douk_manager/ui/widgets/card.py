"""Card container used by modern pages."""

from __future__ import annotations

from PySide6.QtWidgets import QFrame, QLayout, QVBoxLayout, QWidget

from ..theme.tokens import UiMetrics


class ModernCard(QFrame):
    """A styled surface with a predictable internal layout."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setProperty("modernUi", True)
        self.setProperty("modernCard", True)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_L,
        )
        layout.setSpacing(UiMetrics.SPACE_M)
        self._content_layout = layout

    @property
    def content_layout(self) -> QLayout:
        return self._content_layout
