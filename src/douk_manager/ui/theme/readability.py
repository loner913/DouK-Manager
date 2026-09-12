"""High-DPI readability overrides for legacy V0.1.6 feature pages."""

from __future__ import annotations

from .tokens import ThemePalette


def feature_readability_stylesheet(p: ThemePalette) -> str:
    """Return presentation-only typography overrides for migrated feature pages.

    These rules deliberately target only widgets under ``legacyRoot``.  They do
    not replace controls, signals, models, validators or enabled/disabled state;
    they only make the preserved V0.1.6 UI easier to read on real Windows
    1440p/4K displays.
    """

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

/* Read-only protection disables many valid actions.  Disabled must still be
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
