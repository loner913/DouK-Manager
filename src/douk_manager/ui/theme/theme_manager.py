"""Application theme coordinator for the V0.1.7 modern UI."""

from __future__ import annotations

from enum import Enum

from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QApplication

from .tokens import DARK_THEME, LIGHT_THEME, ThemePalette, UiMetrics


class ThemeMode(str, Enum):
    LIGHT = "light"
    DARK = "dark"


class ThemeManager(QObject):
    """Own the active palette and apply a shared stylesheet.

    The manager is deliberately opt-in. Creating the modern UI package does not
    mutate the existing V0.1.6 application stylesheet; callers must explicitly
    invoke :meth:`apply`.
    """

    theme_changed = Signal(str)

    def __init__(
        self,
        mode: ThemeMode = ThemeMode.LIGHT,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._mode = mode

    @property
    def mode(self) -> ThemeMode:
        return self._mode

    @property
    def palette(self) -> ThemePalette:
        return DARK_THEME if self._mode is ThemeMode.DARK else LIGHT_THEME

    def set_mode(self, mode: ThemeMode) -> None:
        if mode is self._mode:
            return
        self._mode = mode
        self.theme_changed.emit(mode.value)

    def toggle(self) -> ThemeMode:
        mode = ThemeMode.DARK if self._mode is ThemeMode.LIGHT else ThemeMode.LIGHT
        self.set_mode(mode)
        return mode

    def apply(self, app: QApplication) -> None:
        """Apply the modern stylesheet to ``app``.

        This remains separate from :meth:`set_mode` so migration code can update
        state without unexpectedly replacing a legacy application stylesheet.
        """

        app.setStyleSheet(self.stylesheet())

    def stylesheet(self) -> str:
        p = self.palette
        m = UiMetrics
        return f"""
QWidget[modernUi="true"] {{
    color: {p.text_primary};
    font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
}}

QWidget#modernRoot {{
    background: {p.app_background};
}}

QWidget#modernHeader {{
    background: {p.surface};
    border-bottom: 1px solid {p.border};
}}
QLabel#modernBrandIcon {{
    color: white;
    background: {p.primary};
    border-radius: 10px;
    font-size: 20px;
    font-weight: 700;
}}
QLabel#modernProductName {{
    color: {p.text_primary};
    font-size: 16px;
    font-weight: 700;
}}
QLabel#modernWorkspaceLabel {{
    color: {p.text_secondary};
    font-size: 11px;
}}

QWidget#modernSidebar {{
    background: {p.surface};
    border-right: 1px solid {p.border};
}}
QPushButton[navigationItem="true"] {{
    min-height: 42px;
    padding: 0 14px;
    border: none;
    border-radius: {m.RADIUS_CONTROL}px;
    color: {p.text_secondary};
    background: transparent;
    text-align: left;
    font-size: 14px;
}}
QPushButton[navigationItem="true"]:hover {{
    color: {p.text_primary};
    background: {p.surface_hover};
}}
QPushButton[navigationItem="true"]:checked {{
    color: {p.primary};
    background: {p.surface_hover};
    font-weight: 600;
}}

QFrame[modernCard="true"] {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: {m.RADIUS_CARD}px;
}}

QFrame[statusPill="true"] {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: {m.RADIUS_CONTROL}px;
}}

QPushButton[buttonRole="primary"] {{
    min-height: {m.PRIMARY_CONTROL_HEIGHT}px;
    padding: 0 16px;
    border: none;
    border-radius: {m.RADIUS_CONTROL}px;
    color: white;
    background: {p.primary};
    font-weight: 600;
}}
QPushButton[buttonRole="primary"]:hover {{
    background: {p.primary_hover};
}}
QPushButton[buttonRole="primary"]:disabled {{
    background: {p.border_strong};
    color: {p.text_muted};
}}

QPushButton[buttonRole="secondary"] {{
    min-height: {m.CONTROL_HEIGHT}px;
    padding: 0 14px;
    border: 1px solid {p.border};
    border-radius: {m.RADIUS_CONTROL}px;
    color: {p.text_primary};
    background: {p.surface};
}}
QPushButton[buttonRole="secondary"]:hover {{
    background: {p.surface_hover};
}}

QLineEdit[modernControl="true"] {{
    min-height: {m.CONTROL_HEIGHT}px;
    padding: 0 12px;
    border: 1px solid {p.border};
    border-radius: {m.RADIUS_CONTROL}px;
    color: {p.text_primary};
    background: {p.surface_alt};
    selection-background-color: {p.primary};
}}
QLineEdit[modernControl="true"]:focus {{
    border-color: {p.primary};
}}
""".strip()
