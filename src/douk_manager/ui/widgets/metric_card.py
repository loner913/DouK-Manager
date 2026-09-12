"""Dashboard metric card used by the V0.1.7 overview."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout, QWidget

from ..theme.tokens import UiMetrics


class MetricCard(QFrame):
    """Compact read-only KPI card.

    The card owns presentation only. Values are pushed in by presenters/pages;
    it never reads controllers, logs, databases, or process state itself.
    """

    def __init__(
        self,
        title: str,
        *,
        value: str = "—",
        note: str = "",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("modernUi", True)
        self.setProperty("modernCard", True)
        self.setProperty("metricCard", True)
        self.setMinimumHeight(112)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_M,
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_M,
        )
        layout.setSpacing(UiMetrics.SPACE_XS)

        self.title_label = QLabel(title, self)
        self.title_label.setObjectName("modernMetricTitle")
        self.value_label = QLabel(value, self)
        self.value_label.setObjectName("modernMetricValue")
        self.value_label.setAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
        )
        self.note_label = QLabel(note, self)
        self.note_label.setObjectName("modernMetricNote")
        self.note_label.setWordWrap(True)

        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.note_label)
        layout.addStretch(1)

    def set_metric(self, value: object, *, note: str = "") -> None:
        self.value_label.setText("—" if value is None else str(value))
        self.note_label.setText(note)
