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
        canvas = "#F4F8FD" if self._mode is ThemeMode.LIGHT else p.app_background
        subtle = "#F9FBFE" if self._mode is ThemeMode.LIGHT else p.surface_alt
        success_bg = "#ECFDF5" if self._mode is ThemeMode.LIGHT else "#103529"
        warning_bg = "#FFF8E8" if self._mode is ThemeMode.LIGHT else "#3B2B12"
        danger_bg = "#FFF1F3" if self._mode is ThemeMode.LIGHT else "#3A1820"
        return f"""
QWidget#modernRoot {{
    background: {canvas};
    color: {p.text_primary};
    font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
    font-size: 13px;
}}
QPushButton#modernThemeToggle {{
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 8px;
}}
QPushButton#modernThemeToggle:hover {{ background: {p.surface_hover}; }}

QWidget#modernHeader {{
    background: {p.surface};
    border-bottom: 1px solid {p.border};
}}
QLabel#modernBrandIcon {{
    color: white;
    background: {p.primary};
    border-radius: 12px;
    font-size: 21px;
    font-weight: 700;
}}
QLabel#modernProductName {{
    color: {p.text_primary};
    font-size: 17px;
    font-weight: 700;
}}
QLabel#modernWorkspaceLabel {{
    color: {p.text_secondary};
    font-size: 11px;
}}
QLineEdit#modernGlobalSearch {{
    min-height: 42px;
    padding: 0 14px;
    border: 1px solid {p.border};
    border-radius: 11px;
    color: {p.text_primary};
    background: {subtle};
    font-size: 12px;
    selection-background-color: {p.primary};
}}
QLineEdit#modernGlobalSearch:focus {{
    border-color: {p.primary};
    background: {p.surface};
}}

QFrame[statusPill="true"] {{
    min-height: 42px;
    background: {subtle};
    border: 1px solid {p.border};
    border-radius: 12px;
}}
QFrame[statusPill="true"] QLabel {{ background: transparent; }}
QLabel#modernStatusLabel {{
    color: {p.text_primary};
    font-size: 14px;
    font-weight: 600;
}}
QLabel#modernStatusValue {{
    color: {p.text_secondary};
    font-size: 14px;
    font-weight: 600;
    padding: 3px 6px;
    border-radius: 5px;
}}
QLabel#modernStatusValue[statusKind="success"] {{ color: {p.success}; background: {success_bg}; }}
QLabel#modernStatusValue[statusKind="info"] {{ color: {p.info}; background: {selected}; }}
QLabel#modernStatusValue[statusKind="warning"] {{ color: {p.warning}; background: {warning_bg}; }}
QLabel#modernStatusValue[statusKind="danger"] {{ color: {p.danger}; background: {danger_bg}; }}
QLabel#modernStatusDot {{ font-size: 11px; }}
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
    min-height: 50px;
    padding: 0 16px;
    border: none;
    border-radius: 10px;
    color: {p.text_primary};
    background: transparent;
    text-align: left;
    font-size: 13px;
    font-weight: 500;
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
}}
QFrame#modernSidebarDivider {{
    color: {p.border};
    background: {p.border};
    max-height: 1px;
    border: none;
}}

QTabWidget#modernPageHost::pane {{ border: 0; background: transparent; }}
QTabWidget#modernPageHost > QWidget {{ background: transparent; }}
QWidget#modernOverviewPage,
QScrollArea#modernOverviewScroll,
QWidget#modernOverviewCanvas,
QWidget#modernLegacyPage,
QScrollArea#modernLegacyScroll,
QWidget#modernLegacyCanvas {{ background: transparent; border: none; }}

QScrollArea#resultDashboardScrollArea,
QWidget#modernDashboardViewport,
QWidget#modernDashboardCanvas {{ background: {p.app_background}; border: none; }}
QFrame#dashboardMetric {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 8px;
}}
QLabel#dashboardMetricTitle {{ color: {p.text_secondary}; }}
QLabel#dashboardMetricValue {{ color: {p.text_primary}; }}

QLabel#modernPageTitle {{
    color: {p.text_primary};
    font-size: 26px;
    font-weight: 700;
}}
QLabel#modernPageSubtitle {{
    color: {p.text_secondary};
    font-size: 12px;
}}
QLabel#modernClockDate {{ color: {p.text_primary}; font-size: 14px; font-weight: 700; }}
QLabel#modernClockTime {{
    color: {p.text_primary};
    font-size: 24px;
    font-weight: 800;
}}

QFrame[modernCard="true"] {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: {m.RADIUS_CARD}px;
}}
QFrame[metricCard="true"] {{ background: {p.surface}; }}
QLabel#modernMetricTitle {{
    color: {p.text_secondary};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#modernMetricValue {{
    color: {p.text_primary};
    font-size: 26px;
    font-weight: 700;
}}
QLabel#modernMetricNote {{ color: {p.text_muted}; font-size: 10px; }}
QLabel#modernMetricIcon {{
    color: white;
    border-radius: 13px;
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
QPushButton#modernOverviewFilter {{
    min-height: 28px;
    padding: 0 10px;
    border: 1px solid {p.border};
    border-radius: 8px;
    color: {p.text_secondary};
    background: {subtle};
    font-size: 10px;
}}
QPushButton#modernOverviewFilter:hover {{
    color: {p.primary};
    border-color: {p.primary};
    background: {selected};
}}
QLabel#modernChartLegend {{
    color: {p.text_muted};
    font-size: 10px;
}}
QWidget#modernPreviewTaskForm {{
    padding: 5px 0;
}}
QLabel#modernPreviewFieldLabel {{
    color: {p.text_secondary};
    font-size: 11px;
}}
QLineEdit#modernPreviewField {{
    min-height: 32px;
    padding: 0 10px;
    border: 1px solid {p.border};
    border-radius: 8px;
    color: {p.text_primary};
    background: {subtle};
    font-size: 11px;
}}
QLabel#modernPreviewSmartNote {{ color: {p.text_muted}; font-size: 10px; }}
QLabel#modernWorkValue {{
    color: {p.text_primary};
    font-size: 14px;
    font-weight: 700;
}}
QComboBox#modernAttentionFilter {{
    background: {p.surface}; color: {p.text_primary};
    border: 1px solid {p.border}; border-radius: 6px;
    padding: 5px 6px; font-size: 12px; min-height: 20px;
}}
QComboBox#modernAttentionFilter QAbstractItemView {{
    background: {p.surface}; color: {p.text_primary};
    selection-background-color: {p.primary}; selection-color: white;
}}
QLabel#modernAttentionPager {{ color: {p.text_secondary}; font-size: 12px; }}
QPushButton[attentionPager="true"] {{ padding: 0; font-size: 20px; font-weight: 700; color: {p.text_primary}; }}
QPushButton[attentionPager="true"]:disabled {{ color: {p.text_muted}; }}
QFrame#modernQueueRow,
QFrame#modernAttentionRow {{
    background: transparent;
    border: none;
    border-bottom: 1px solid {p.border};
}}
QLabel#modernQueueIndex,
QLabel#modernQueueAccount,
QLabel#modernQueuePercentage,
QLabel#modernQueueSpeed,
QLabel#modernQueueTime {{
    color: {p.text_primary};
    font-size: 13px;
}}
QLabel#modernQueueIndex {{ color: {p.text_secondary}; }}
QLabel#modernQueueStatus {{ font-size: 13px; font-weight: 600; }}
QLabel#modernQueueStatus[queueKind="success"] {{ color: {p.success}; }}
QLabel#modernQueueStatus[queueKind="info"] {{ color: {p.info}; }}
QLabel#modernQueueStatus[queueKind="neutral"] {{ color: {p.text_muted}; }}
QProgressBar#modernQueueProgress {{
    min-height: 7px;
    max-height: 7px;
    border: none;
    border-radius: 4px;
    background: {p.border};
}}
QProgressBar#modernQueueProgress::chunk {{
    border-radius: 4px;
    background: {p.primary};
}}
QLabel#modernAttentionDot {{ font-size: 13px; }}
QLabel#modernAttentionDot[attentionKind="danger"] {{ color: {p.danger}; }}
QLabel#modernAttentionDot[attentionKind="warning"] {{ color: {p.warning}; }}
QLabel#modernAttentionDot[attentionKind="info"] {{ color: {p.info}; }}
QLabel#modernAttentionTitle {{ color: {p.text_primary}; font-size: 13px; font-weight: 600; }}
QLabel#modernAttentionNote {{ color: {p.text_primary}; font-size: 12px; }}
QLabel#modernAttentionTime {{ color: {p.text_secondary}; font-size: 12px; }}
QLabel#modernDetailValue {{
    color: {p.text_primary};
    font-size: 12px;
    font-weight: 600;
}}
QLabel#modernOverviewStartupBadge {{
    min-height: 30px;
    padding: 0 12px;
    color: {p.text_secondary};
    background: {subtle};
    border: 1px solid {p.border};
    border-radius: 15px;
    font-size: 11px;
    font-weight: 700;
}}
QLabel#modernOverviewStartupBadge[startupKind="success"] {{
    color: {p.success};
    background: {success_bg};
    border-color: #C7F3DF;
}}
QLabel#modernOverviewStartupBadge[startupKind="warning"] {{
    color: #B56A00;
    background: {warning_bg};
    border-color: #FBE4AD;
}}
QLabel#modernOverviewStartupBadge[startupKind="info"] {{
    color: {p.primary};
    background: {selected};
    border-color: #CFE2FF;
}}

QPushButton[overviewAction="primary"] {{
    min-height: 40px;
    padding: 0 16px;
    border: none;
    border-radius: 9px;
    color: white;
    background: {p.primary};
    font-size: 12px;
    font-weight: 600;
}}
QPushButton[overviewAction="primary"]:hover {{ background: {p.primary_hover}; }}
QPushButton[overviewAction="secondary"] {{
    min-height: 38px;
    padding: 0 14px;
    border: 1px solid {p.border};
    border-radius: 9px;
    color: {p.text_primary};
    background: {subtle};
    font-size: 12px;
}}
QPushButton[overviewAction="secondary"]:hover {{ background: {p.surface_hover}; }}

QProgressBar#modernThroughputBar {{
    min-height: 9px;
    max-height: 9px;
    border: none;
    border-radius: 4px;
    background: #E7EEF7;
    text-align: center;
}}
QProgressBar#modernThroughputBar::chunk {{
    border-radius: 4px;
    background: {p.primary};
}}

/* ---- All remaining V0.1.6 feature pages: presentation-only modernisation. ---- */
QFrame[modernFeatureSurface="true"] {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: {m.RADIUS_CARD}px;
}}
QWidget[legacyRoot="true"] {{
    background: transparent;
    color: {p.text_primary};
}}
QWidget[legacyRoot="true"] QLabel {{
    color: {p.text_secondary};
    background: transparent;
    font-size: 12px;
}}
QWidget[legacyRoot="true"] QGroupBox[legacyCard="true"] {{
    margin-top: 15px;
    padding: 18px 14px 14px 14px;
    border: 1px solid {p.border};
    border-radius: 10px;
    background: {subtle};
    color: {p.text_primary};
    font-size: 12px;
    font-weight: 700;
}}
QWidget[legacyRoot="true"] QGroupBox[legacyCard="true"]::title {{
    subcontrol-origin: margin;
    left: 12px;
    padding: 0 7px;
    color: {p.text_primary};
    background: {p.surface};
}}
QWidget[legacyRoot="true"] QLineEdit[legacyModernized="true"],
QWidget[legacyRoot="true"] QComboBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QSpinBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QDoubleSpinBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QDateEdit[legacyModernized="true"],
QWidget[legacyRoot="true"] QDateTimeEdit[legacyModernized="true"] {{
    min-height: 36px;
    padding: 0 10px;
    border: 1px solid {p.border};
    border-radius: 8px;
    background: {p.surface};
    color: {p.text_primary};
    selection-background-color: {p.primary};
}}
QWidget[legacyRoot="true"] QLineEdit[legacyModernized="true"]:focus,
QWidget[legacyRoot="true"] QComboBox[legacyModernized="true"]:focus,
QWidget[legacyRoot="true"] QSpinBox[legacyModernized="true"]:focus,
QWidget[legacyRoot="true"] QDoubleSpinBox[legacyModernized="true"]:focus,
QWidget[legacyRoot="true"] QDateEdit[legacyModernized="true"]:focus,
QWidget[legacyRoot="true"] QDateTimeEdit[legacyModernized="true"]:focus {{
    border-color: {p.primary};
}}
QWidget[legacyRoot="true"] QComboBox[legacyModernized="true"]::drop-down,
QWidget[legacyRoot="true"] QDateEdit[legacyModernized="true"]::drop-down,
QWidget[legacyRoot="true"] QDateTimeEdit[legacyModernized="true"]::drop-down {{
    width: 28px;
    border: none;
}}
QWidget[legacyRoot="true"] QTextEdit[legacyModernized="true"],
QWidget[legacyRoot="true"] QPlainTextEdit[legacyModernized="true"] {{
    padding: 9px;
    border: 1px solid {p.border};
    border-radius: 9px;
    background: {p.surface};
    color: {p.text_primary};
    selection-background-color: {p.primary};
}}
QWidget[legacyRoot="true"] QTableView[legacyModernized="true"],
QWidget[legacyRoot="true"] QTableWidget[legacyModernized="true"],
QWidget[legacyRoot="true"] QListView[legacyModernized="true"],
QWidget[legacyRoot="true"] QListWidget[legacyModernized="true"],
QWidget[legacyRoot="true"] QTreeView[legacyModernized="true"] {{
    border: 1px solid {p.border};
    border-radius: 9px;
    background: {p.surface};
    alternate-background-color: {subtle};
    color: {p.text_primary};
    gridline-color: {p.border};
    selection-background-color: {selected};
    selection-color: {p.text_primary};
}}
QWidget[legacyRoot="true"] QHeaderView::section {{
    min-height: 34px;
    padding: 0 9px;
    border: none;
    border-right: 1px solid {p.border};
    border-bottom: 1px solid {p.border};
    background: {subtle};
    color: {p.text_secondary};
    font-size: 11px;
    font-weight: 600;
}}
QWidget[legacyRoot="true"] QPushButton[legacyModernized="true"] {{
    min-height: 38px;
    padding: 0 14px;
    border: 1px solid {p.border};
    border-radius: 8px;
    background: {subtle};
    color: {p.text_primary};
    font-size: 12px;
    font-weight: 600;
}}
QWidget[legacyRoot="true"] QPushButton[legacyModernized="true"]:hover {{
    background: {p.surface_hover};
    border-color: {p.border_strong};
}}
QWidget[legacyRoot="true"] QPushButton[legacyRole="primary"] {{
    border-color: {p.primary};
    background: {p.primary};
    color: white;
}}
QWidget[legacyRoot="true"] QPushButton[legacyRole="primary"]:hover {{
    border-color: {p.primary_hover};
    background: {p.primary_hover};
}}
QWidget[legacyRoot="true"] QPushButton[legacyRole="danger"] {{
    border-color: #FFD2D8;
    background: {danger_bg};
    color: {p.danger};
}}
QWidget[legacyRoot="true"] QPushButton[legacyModernized="true"]:disabled {{
    border-color: {p.border};
    background: {subtle};
    color: {p.text_muted};
}}
QWidget[legacyRoot="true"] QCheckBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QRadioButton[legacyModernized="true"] {{
    spacing: 8px;
    color: {p.text_primary};
    font-size: 12px;
}}
QWidget[legacyRoot="true"] QProgressBar[legacyModernized="true"] {{
    min-height: 10px;
    max-height: 10px;
    border: none;
    border-radius: 5px;
    background: {p.border};
    color: transparent;
}}
QWidget[legacyRoot="true"] QProgressBar[legacyModernized="true"]::chunk {{
    border-radius: 5px;
    background: {p.primary};
}}

QScrollBar:vertical {{
    width: 11px;
    margin: 2px;
    background: transparent;
}}
QScrollBar::handle:vertical {{
    min-height: 32px;
    border-radius: 4px;
    background: {p.border_strong};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
""".strip()
