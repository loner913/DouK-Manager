"""Compact service-status indicator used in the global header."""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QWidget

from ..theme.tokens import DARK_THEME, LIGHT_THEME, UiMetrics


class StatusKind(str, Enum):
    SUCCESS = "success"
    INFO = "info"
    WARNING = "warning"
    DANGER = "danger"
    NEUTRAL = "neutral"


_STATUS_COLOURS = {
    StatusKind.SUCCESS: (LIGHT_THEME.success, DARK_THEME.success),
    StatusKind.INFO: (LIGHT_THEME.info, DARK_THEME.info),
    StatusKind.WARNING: (LIGHT_THEME.warning, DARK_THEME.warning),
    StatusKind.DANGER: (LIGHT_THEME.danger, DARK_THEME.danger),
    StatusKind.NEUTRAL: (LIGHT_THEME.text_muted, DARK_THEME.text_muted),
}


class StatusPill(QFrame):
    """Label + coloured state dot with no domain-specific behaviour."""

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

        layout = QHBoxLayout(self)
        layout.setContentsMargins(UiMetrics.SPACE_M, UiMetrics.SPACE_S, UiMetrics.SPACE_M, UiMetrics.SPACE_S)
        layout.setSpacing(UiMetrics.SPACE_S)

        self.dot = QLabel("●", self)
        self.dot.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label = QLabel(label, self)
        self.value = QLabel(value, self)

        layout.addWidget(self.dot)
        layout.addWidget(self.label)
        layout.addWidget(self.value)

        self.set_status(value=value, kind=kind)

    def set_status(self, *, value: str, kind: StatusKind) -> None:
        self.value.setText(value)
        self.setProperty("statusKind", kind.value)
        light, _dark = _STATUS_COLOURS[kind]
        self.dot.setStyleSheet(f"color: {light};")
