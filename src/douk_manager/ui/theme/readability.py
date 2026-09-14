"""High-DPI readability and feature-page presentation overrides."""

from __future__ import annotations

from .tokens import ThemePalette


def feature_readability_stylesheet(p: ThemePalette) -> str:
    """Return presentation-only typography and layout styles for migrated pages.

    These rules deliberately target only the migrated legacy surface. They do not
    replace controls, signals, models, validators or enabled/disabled state.
    """

    warning_bg = "#FFF8E8" if p.surface == "#FFFFFF" else "#3B2B12"
    warning_border = "#F5D98A" if p.surface == "#FFFFFF" else "#76571D"
    info_bg = "#F4F8FF" if p.surface == "#FFFFFF" else p.surface_alt

    return f"""
QLabel#modernPageSubtitle {{
    color: {p.text_secondary};
    font-size: 13px;
    font-weight: 500;
}}

/* The page canvas is already the Fluent background.  Avoid the previous giant
   white panel around every feature page; individual section cards provide the
   hierarchy instead. */
QFrame[modernFeatureSurface="true"] {{
    background: transparent;
    border: none;
}}

QWidget[legacyRoot="true"] {{
    font-size: 13px;
}}
QWidget[legacyRoot="true"] QLabel {{
    color: {p.text_secondary};
    font-size: 13px;
    font-weight: 500;
}}
QWidget[legacyRoot="true"] QGroupBox[legacyCard="true"] {{
    font-size: 13px;
    font-weight: 700;
}}

/* Section cards are now the primary visual containers. */
QWidget[legacyRoot="true"] QFrame[legacySectionCard="true"] {{
    background: {p.surface};
    border: 1px solid {p.border};
    border-radius: 12px;
}}
QWidget[legacyRoot="true"] QFrame[legacyCompactCard="true"] {{
    min-height: 0px;
}}
QWidget[legacyRoot="true"] QLabel[legacySectionTitle="true"] {{
    color: {p.text_primary};
    font-size: 17px;
    font-weight: 700;
}}
QWidget[legacyRoot="true"] QLabel[legacySectionNote="true"] {{
    color: {p.text_secondary};
    font-size: 12px;
    font-weight: 500;
}}
QWidget[legacyRoot="true"] QLabel[legacyBanner="true"] {{
    padding: 10px 13px;
    color: {p.text_secondary};
    background: {info_bg};
    border: 1px solid {p.border};
    border-radius: 9px;
    font-size: 12px;
    font-weight: 500;
}}
QWidget[legacyRoot="true"] QLabel[legacyBanner="true"][legacyBannerKind="warning"] {{
    color: {p.text_primary};
    background: {warning_bg};
    border-color: {warning_border};
}}
QWidget[legacyRoot="true"] QLabel[legacyPathSummary="true"] {{
    padding: 10px 12px;
    color: {p.text_secondary};
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 8px;
    font-family: "Cascadia Mono", "Consolas", "Microsoft YaHei UI";
    font-size: 11px;
}}
QWidget[legacyRoot="true"] QWidget[legacyLayoutHolder="true"] {{
    background: transparent;
}}
QWidget[legacyRoot="true"] QTextEdit[legacyOutput="true"] {{
    background: {p.surface_alt};
}}

/* Legacy group boxes used to be page-sized panels.  Inside a modern section card
   they become flat sub-groups, eliminating the boxes-inside-boxes look. */
QWidget[legacyRoot="true"][legacyReflowed="true"] QGroupBox[legacyCard="true"] {{
    margin-top: 14px;
    padding: 16px 0 0 0;
    background: transparent;
    border: none;
    border-radius: 0px;
}}
QWidget[legacyRoot="true"] QPushButton[legacyModernized="true"][queueActionActive="true"],
QWidget[legacyRoot="true"] QPushButton[legacyModernized="true"][queueActionActive="true"]:disabled {{
    background: {p.primary};
    border-color: {p.primary};
    color: white;
}}
QWidget[legacyRoot="true"][legacyReflowed="true"] QGroupBox[compactSettingsGroup="true"] {{
    margin-top: 0px;
    padding-top: 0px;
}}
QWidget[legacyRoot="true"][legacyReflowed="true"] QGroupBox[legacyCard="true"]::title {{
    subcontrol-origin: margin;
    left: 0px;
    padding: 0 4px 0 0;
    color: {p.text_primary};
    background: transparent;
    font-size: 13px;
    font-weight: 700;
}}

QWidget[legacyRoot="true"] QLineEdit[legacyModernized="true"],
QWidget[legacyRoot="true"] QComboBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QSpinBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QDoubleSpinBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QDateEdit[legacyModernized="true"],
QWidget[legacyRoot="true"] QDateTimeEdit[legacyModernized="true"],
QWidget[legacyRoot="true"] QTextEdit[legacyModernized="true"],
QWidget[legacyRoot="true"] QPlainTextEdit[legacyModernized="true"],
QWidget[legacyRoot="true"] QTableView[legacyModernized="true"],
QWidget[legacyRoot="true"] QTableWidget[legacyModernized="true"],
QWidget[legacyRoot="true"] QListView[legacyModernized="true"],
QWidget[legacyRoot="true"] QListWidget[legacyModernized="true"],
QWidget[legacyRoot="true"] QTreeView[legacyModernized="true"] {{
    font-size: 13px;
}}

QWidget[legacyRoot="true"] QHeaderView::section {{
    color: {p.text_secondary};
    font-size: 12px;
    font-weight: 600;
}}

QWidget[legacyRoot="true"] QPushButton[legacyModernized="true"] {{
    font-size: 13px;
    font-weight: 600;
}}
QWidget[legacyRoot="true"] QCheckBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QRadioButton[legacyModernized="true"] {{
    color: {p.text_primary};
    font-size: 13px;
    font-weight: 500;
}}

/* Read-only protection disables many valid actions. Disabled must still be
   legible; use secondary text instead of a nearly invisible placeholder tone. */
QWidget[legacyRoot="true"] QPushButton[legacyModernized="true"]:disabled {{
    color: {p.text_secondary};
}}
QWidget[legacyRoot="true"] QLineEdit[legacyModernized="true"]:disabled,
QWidget[legacyRoot="true"] QComboBox[legacyModernized="true"]:disabled,
QWidget[legacyRoot="true"] QSpinBox[legacyModernized="true"]:disabled,
QWidget[legacyRoot="true"] QDoubleSpinBox[legacyModernized="true"]:disabled,
QWidget[legacyRoot="true"] QDateEdit[legacyModernized="true"]:disabled,
QWidget[legacyRoot="true"] QDateTimeEdit[legacyModernized="true"]:disabled {{
    color: {p.text_secondary};
}}
/* Consistent desktop typography across the shell and migrated controls. */
QPushButton#modernPageHelp {{
    color: {p.primary}; background: transparent;
    border: 1px solid {p.border}; border-radius: 8px;
    padding: 6px 12px; font-size: 13px;
}}
QPushButton#modernPageHelp:checked {{ background: {p.surface_alt}; }}
QLabel#modernWorkspaceLabel,
QLabel#modernMetricNote, QLabel#modernSectionNote,
QLabel#modernDetailLabel {{ font-size: 12px; }}
QLabel#modernMetricTitle, QLabel#modernDetailValue,
QLabel#modernPageSubtitle, QLineEdit#modernGlobalSearch {{ font-size: 14px; }}
QLabel#modernSectionTitle {{ font-size: 18px; }}
QLabel#modernMetricValue {{ font-size: 30px; }}
QPushButton[navigationItem="true"] {{ font-size: 15px; min-height: 50px; }}
QWidget[legacyRoot="true"] QLabel,
QWidget[legacyRoot="true"] QLineEdit[legacyModernized="true"],
QWidget[legacyRoot="true"] QComboBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QSpinBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QDoubleSpinBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QDateEdit[legacyModernized="true"],
QWidget[legacyRoot="true"] QDateTimeEdit[legacyModernized="true"],
QWidget[legacyRoot="true"] QTextEdit[legacyModernized="true"],
QWidget[legacyRoot="true"] QPlainTextEdit[legacyModernized="true"],
QWidget[legacyRoot="true"] QAbstractItemView,
QWidget[legacyRoot="true"] QPushButton[legacyModernized="true"],
QWidget[legacyRoot="true"] QCheckBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QRadioButton[legacyModernized="true"] {{ font-size: 14px; }}
QWidget[legacyRoot="true"] QLabel[legacySectionTitle="true"] {{ font-size: 18px; }}
QWidget[legacyRoot="true"] QLabel[legacySectionNote="true"],
QWidget[legacyRoot="true"] QLabel[legacyBanner="true"],
QWidget[legacyRoot="true"] QLabel[legacyPathSummary="true"],
QWidget[legacyRoot="true"] QHeaderView::section {{ font-size: 12px; }}
QWidget[legacyRoot="true"] QLineEdit[legacyModernized="true"],
QWidget[legacyRoot="true"] QComboBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QSpinBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QPushButton[legacyModernized="true"] {{ min-height: 40px; }}
""".strip()


__all__ = ["feature_readability_stylesheet"]
