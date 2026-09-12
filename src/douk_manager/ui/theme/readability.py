"""High-DPI readability and feature-page presentation overrides."""

from __future__ import annotations

from .tokens import ThemePalette


def feature_readability_stylesheet(p: ThemePalette) -> str:
    """Return presentation-only typography and layout styles for migrated pages.

    These rules deliberately target only widgets under ``legacyRoot``. They do not
    replace controls, signals, models, validators or enabled/disabled state; they
    make the preserved V0.1.6 UI readable and visually grouped on real Windows
    1440p/4K displays.
    """

    warning_bg = "#FFF8E8" if p.surface == "#FFFFFF" else "#3B2B12"
    warning_border = "#F5D98A" if p.surface == "#FFFFFF" else "#76571D"
    info_bg = "#F4F8FF" if p.surface == "#FFFFFF" else p.surface_alt

    return f"""
QLabel#modernPageSubtitle {{
    color: {p.text_secondary};
    font-size: 14px;
    font-weight: 500;
}}

QWidget[legacyRoot="true"] {{
    font-size: 14px;
}}
QWidget[legacyRoot="true"] QLabel {{
    color: {p.text_secondary};
    font-size: 14px;
    font-weight: 500;
}}
QWidget[legacyRoot="true"] QGroupBox[legacyCard="true"] {{
    font-size: 14px;
    font-weight: 700;
}}

/* V0.1.7 reflow cards.  These containers are presentation-only; their children
   are the original V0.1.6 widget instances. */
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
    font-size: 13px;
    font-weight: 500;
}}
QWidget[legacyRoot="true"] QLabel[legacyBanner="true"] {{
    padding: 11px 13px;
    color: {p.text_secondary};
    background: {info_bg};
    border: 1px solid {p.border};
    border-radius: 9px;
    font-size: 13px;
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
    font-size: 12px;
}}
QWidget[legacyRoot="true"] QWidget[legacyLayoutHolder="true"] {{
    background: transparent;
}}
QWidget[legacyRoot="true"] QTextEdit[legacyOutput="true"] {{
    background: {p.surface_alt};
}}

/* Reflowed group boxes become inner control groups rather than giant page-level
   rectangles.  Keep their titles but reduce the visual weight inside cards. */
QWidget[legacyRoot="true"][legacyReflowed="true"] QGroupBox[legacyCard="true"] {{
    margin-top: 16px;
    padding: 17px 14px 13px 14px;
    background: {p.surface_alt};
    border: 1px solid {p.border};
    border-radius: 9px;
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
    font-size: 14px;
}}

QWidget[legacyRoot="true"] QHeaderView::section {{
    color: {p.text_secondary};
    font-size: 13px;
    font-weight: 600;
}}

QWidget[legacyRoot="true"] QPushButton[legacyModernized="true"] {{
    font-size: 14px;
    font-weight: 600;
}}
QWidget[legacyRoot="true"] QCheckBox[legacyModernized="true"],
QWidget[legacyRoot="true"] QRadioButton[legacyModernized="true"] {{
    color: {p.text_primary};
    font-size: 14px;
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
""".strip()


__all__ = ["feature_readability_stylesheet"]
