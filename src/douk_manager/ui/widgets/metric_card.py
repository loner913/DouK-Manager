"""Dashboard metric card used by the V0.1.7 overview."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..theme.tokens import UiMetrics


class MetricCard(QFrame):
    """Compact read-only KPI card with a Fluent-style icon tile."""

    def __init__(
        self,
        title: str,
        *,
        value: str = "—",
        note: str = "",
        icon_text: str = "•",
        tone: str = "primary",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("modernUi", True)
        self.setProperty("modernCard", True)
        self.setProperty("metricCard", True)
        self.setMinimumHeight(116)

        root = QHBoxLayout(self)
        root.setContentsMargins(
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_L,
        )
        root.setSpacing(UiMetrics.SPACE_M)

        self.icon_label = QLabel(icon_text, self)
        self.icon_label.setObjectName("modernMetricIcon")
        self.icon_label.setProperty("tone", tone)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setFixedSize(52, 52)
        root.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignTop)

        content = QVBoxLayout()
        content.setSpacing(2)
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

        content.addWidget(self.title_label)
        content.addWidget(self.value_label)
        content.addWidget(self.note_label)
        content.addStretch(1)
        root.addLayout(content, 1)

    def set_metric(self, value: object, *, note: str = "") -> None:
        self.value_label.setText("—" if value is None else str(value))
        self.note_label.setText(note)
