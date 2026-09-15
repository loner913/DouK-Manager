"""Dashboard metric card used by the V0.1.7 overview."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from ..icons import icon_pixmap
from ..theme.tokens import UiMetrics


_METRIC_ICONS = {
    "计划账号": "users",
    "实际开始": "play",
    "完整性": "check",
    "可靠性": "shield",
    "附加异常": "warning",
    "未进入处理": "clock",
}


class MetricCard(QFrame):
    """Read-only KPI card with a crisp vector icon tile."""

    def __init__(
        self,
        title: str,
        *,
        value: str = "—",
        note: str = "",
        icon_text: str = "",
        tone: str = "primary",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("modernUi", True)
        self.setProperty("modernCard", True)
        self.setProperty("metricCard", True)
        self.setMinimumHeight(132)

        root = QHBoxLayout(self)
        root.setContentsMargins(18, 17, 18, 17)
        root.setSpacing(14)

        self.icon_label = QLabel(self)
        self.icon_label.setObjectName("modernMetricIcon")
        self.icon_label.setProperty("tone", tone)
        self.icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.icon_label.setFixedSize(58, 58)
        icon_name = _METRIC_ICONS.get(title, "chart")
        self.icon_label.setPixmap(icon_pixmap(icon_name, "#FFFFFF", 27))
        root.addWidget(self.icon_label, 0, Qt.AlignmentFlag.AlignVCenter)

        content = QVBoxLayout()
        content.setSpacing(3)
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

        content.addStretch(1)
        content.addWidget(self.title_label)
        content.addWidget(self.value_label)
        content.addWidget(self.note_label)
        content.addStretch(1)
        root.addLayout(content, 1)

    def set_metric(self, value: object, *, note: str = "") -> None:
        self.value_label.setText("—" if value is None else str(value))
        self.note_label.setText(note)
