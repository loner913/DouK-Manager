"""Compact service-status indicator used in the global header."""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget, QSizePolicy

from ..theme.tokens import UiMetrics


class StatusKind(str, Enum):
    SUCCESS = "success"
    INFO = "info"
    WARNING = "warning"
    DANGER = "danger"
    NEUTRAL = "neutral"


class StatusPill(QFrame):
    """Label + coloured state dot with responsive compact presentation."""

    _COMPACT_LABELS = {
        "采集服务": "采集",
        "下载进程": "下载",
        "数据库": "DB",
        "Volume": "Vol",
    }

    def __init__(
        self,
        label: str,
        value: str,
        *,
        kind: StatusKind = StatusKind.NEUTRAL,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("modernUi", True)
        self.setProperty("statusPill", True)
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self._label_text = label
        self._compact = False

        layout = QHBoxLayout(self)
        layout.setContentsMargins(
            UiMetrics.SPACE_M,
            7,
            UiMetrics.SPACE_M,
            7,
        )
        layout.setSpacing(6)

        self.dot = QLabel(self)
        self.dot.setFixedSize(8, 8)
        self.dot.setObjectName("modernStatusDot")
        self.dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel(label, self)
        self.label.setObjectName("modernStatusLabel")
        self.value = QLabel(value, self)
        self.value.setObjectName("modernStatusValue")

        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        layout.addWidget(self.value)
        self.set_status(value=value, kind=kind)

    def set_compact(self, compact: bool) -> None:
        if self._compact == compact:
            return
        self._compact = compact
        self.label.setText(
            self._COMPACT_LABELS.get(self._label_text, self._label_text)
            if compact
            else self._label_text
        )
        self.setProperty("compact", compact)
        self.style().unpolish(self)
        self.style().polish(self)

    def set_status(self, *, value: str, kind: StatusKind) -> None:
        self.value.setText(value)
        self.setToolTip(f"{self._label_text}：{value}")
        self.setProperty("statusKind", kind.value)
        self.dot.setProperty("statusKind", kind.value)
        self.value.setProperty("statusKind", kind.value)
        colours = {
            StatusKind.SUCCESS: "#19A86B",
            StatusKind.INFO: "#168BC2",
            StatusKind.WARNING: "#E89020",
            StatusKind.DANGER: "#E04455",
            StatusKind.NEUTRAL: "#71839B",
        }
        self.dot.setStyleSheet(
            f"background-color: {colours[kind]}; border: none; border-radius: 4px;"
        )
        self.style().unpolish(self)
        self.style().polish(self)
        self.dot.style().unpolish(self.dot)
        self.dot.style().polish(self.dot)
        self.value.style().unpolish(self.value)
        self.value.style().polish(self.value)
        self.updateGeometry()
