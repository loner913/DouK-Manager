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
    """Own the active palette and apply the scoped modern stylesheet."""

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
        app.setStyleSheet(self.stylesheet())

    def stylesheet(self) -> str:
        p = self.palette
        m = UiMetrics
        selected = "#EAF3FF" if self._mode is ThemeMode.LIGHT else "#173457"
        canvas = "#F6F9FD" if self._mode is ThemeMode.LIGHT else p.app_background
        subtle = "#F9FBFE" if self._mode is ThemeMode.LIGHT else p.surface_alt
        return f"""
QWidget#modernRoot {{
    background: {canvas};
    color: {p.text_primary};
    font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 13px;
}}

QWidget#modernHeader {{
    background: {p.surface};
    border-bottom: 1px solid {p.border};
}}
QLabel#modernBrandIcon {{
    color: white;
    background: {p.primary};
    border-radius: 11px;
    font-size: 21px;
    font-weight: 700;
}}
QLabel#modernProductName {{
    color: {p.text_primary};
    font-size: 16px;
    font-weight: 700;
}}
QLabel#modernWorkspaceLabel {{
    color: {p.text_secondary};
    font-size: 10px;
}}
QLineEdit#modernGlobalSearch {{
    min-height: 38px;
    padding: 0 13px;
    border: 1px solid {p.border};
    border-radius: 10px;
    color: {p.text_primary};
    background: {subtle};
    selection-background-color: {p.primary};
}}
QLineEdit#modernGlobalSearch:focus {{
    border-color: {p.primary};
    background: {p.surface};
}}

QFrame[statusPill="true"] {{
    min-height: 36px;
    background: {subtle};
    border: 1px solid {p.border};
    border-radius: 18px;
}}
QFrame[statusPill="true"] QLabel {{
    background: transparent;
}}
QLabel#modernStatusLabel {{
    color: {p.text_primary};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#modernStatusValue {{
    color: {p.text_secondary};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#modernStatusDot {{ font-size: 10px; }}
QLabel#modernStatusDot[statusKind="success"] {{ color: {p.success}; }}
QLabel#modernStatusDot[statusKind="info"] {{ color: {p.info}; }}
QLabel#modernStatusDot[statusKind="warning"] {{ color: {p.warning}; }}
QLabel#modernStatusDot[statusKind="danger"] {{ color: {p.danger}; }}
QLabel#modernStatusDot[statusKind="neutral"] {{ color: {p.text_muted}; }}

QWidget#modernSidebar {{
    background: {p.surface};
    border-right: 1px solid {p.border};
}}
QPushButton[navigationItem="true"] {{
    min-height: 46px;
    padding: 0 14px;
    border: none;
    border-radius: 9px;
    color: {p.text_secondary};
    background: transparent;
    text-align: left;
    font-size: 13px;
}}
QPushButton[navigationItem="true"]:hover {{
    color: {p.text_primary};
    background: {p.surface_hover};
}}
QPushButton[navigationItem="true"]:checked {{
    color: {p.primary};
    background: {selected};
    font-weight: 700;
}}
QPushButton[navigationItem="true"][collapsed="true"] {{
    padding: 0;
    text-align: center;
    font-size: 18px;
}}
QFrame#modernSidebarDivider {{
    color: {p.border};
    background: {p.border};
    max-height: 1px;
    border: none;
}}

QTabWidget#modernPageHost::pane {{
    border: 0;
    background: transparent;
}}
QTabWidget#modernPageHost > QWidget {{
    background: transparent;
}}

QWidget#modernOverviewPage,
QScrollArea#modernOverviewScroll,
QWidget#modernOverviewCanvas {{
    background: transparent;
    border: none;
}}
QLabel#modernPageTitle {{
    color: {p.text_primary};
    font-size: 25px;
    font-weight: 700;
}}
QLabel#modernPageSubtitle {{
    color: {p.text_secondary};
    font-size: 12px;
}}
QLabel#modernClockDate {{
    color: {p.text_secondary};
    font-size: 10px;
}}
QLabel#modernClockTime {{
    color: {p.text_primary};
    font-size: 22px;
    font-weight: 600;
}}

QFrame[modernCard="true"] {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: {m.RADIUS_CARD}px;
}}
QFrame[metricCard="true"] {{
    background: {p.surface};
}}
QLabel#modernMetricTitle {{
    color: {p.text_secondary};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#modernMetricValue {{
    color: {p.text_primary};
    font-size: 24px;
    font-weight: 700;
}}
QLabel#modernMetricNote {{
    color: {p.text_muted};
    font-size: 10px;
}}
QLabel#modernMetricIcon {{
    color: white;
    border-radius: 12px;
    font-size: 22px;
    font-weight: 700;
}}
QLabel#modernMetricIcon[tone="primary"] {{ background: {p.primary}; }}
QLabel#modernMetricIcon[tone="success"] {{ background: {p.success}; }}
QLabel#modernMetricIcon[tone="info"] {{ background: {p.info}; }}
QLabel#modernMetricIcon[tone="violet"] {{ background: #7C4DFF; }}
QLabel#modernMetricIcon[tone="danger"] {{ background: {p.danger}; }}
QLabel#modernMetricIcon[tone="warning"] {{ background: {p.warning}; }}

QLabel#modernSectionTitle {{
    color: {p.text_primary};
    font-size: 15px;
    font-weight: 700;
}}
QLabel#modernSectionNote,
QLabel#modernDetailLabel {{
    color: {p.text_muted};
    font-size: 10px;
}}
QLabel#modernDetailValue {{
    color: {p.text_primary};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#modernOverviewStartupBadge {{
    min-height: 28px;
    padding: 0 11px;
    color: {p.text_secondary};
    background: {subtle};
    border: 1px solid {p.border};
    border-radius: 14px;
    font-size: 11px;
    font-weight: 700;
}}
QLabel#modernOverviewStartupBadge[startupKind="success"] {{
    color: {p.success};
    background: #ECFDF5;
    border-color: #C7F3DF;
}}
QLabel#modernOverviewStartupBadge[startupKind="warning"] {{
    color: #B56A00;
    background: #FFF8E8;
    border-color: #FBE4AD;
}}
QLabel#modernOverviewStartupBadge[startupKind="info"] {{
    color: {p.primary};
    background: {selected};
    border-color: #CFE2FF;
}}

QPushButton[overviewAction="primary"] {{
    min-height: 36px;
    padding: 0 14px;
    border: none;
    border-radius: 8px;
    color: white;
    background: {p.primary};
    font-weight: 600;
}}
QPushButton[overviewAction="primary"]:hover {{ background: {p.primary_hover}; }}
QPushButton[overviewAction="secondary"] {{
    min-height: 34px;
    padding: 0 12px;
    border: 1px solid {p.border};
    border-radius: 8px;
    color: {p.text_primary};
    background: {subtle};
}}
QPushButton[overviewAction="secondary"]:hover {{ background: {p.surface_hover}; }}

QProgressBar#modernThroughputBar {{
    min-height: 8px;
    max-height: 8px;
    border: none;
    border-radius: 4px;
    background: #E7EEF7;
    text-align: center;
}}
QProgressBar#modernThroughputBar::chunk {{
    border-radius: 4px;
    background: {p.primary};
}}

QScrollBar:vertical {{
    width: 10px;
    margin: 2px;
    background: transparent;
}}
QScrollBar::handle:vertical {{
    min-height: 30px;
    border-radius: 4px;
    background: {p.border_strong};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
""".strip()
