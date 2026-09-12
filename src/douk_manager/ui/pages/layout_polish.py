"""Windows geometry polish for reflowed V0.1.6 feature cards.

The modern shell keeps the original V0.1.6 widgets. Some legacy layouts contain
vertical stretches that made sense in the old full-page design, but look awkward
inside the new card layout on 1440p/4K displays. This module removes only those
presentation spacers, constrains compact cards, and keeps empty output areas from
turning into giant blank panels. Real tables and populated outputs still expand.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QLabel,
    QLayout,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QTextEdit,
    QWidget,
)


_EXPANDING_TYPES = (
    QAbstractItemView,
    QTextEdit,
    QPlainTextEdit,
    QScrollArea,
)
_MAX_WIDGET_HEIGHT = 16777215
_EMPTY_OUTPUT_HEIGHT = 230
_POPULATED_OUTPUT_MINIMUM = 180


def _is_empty_adaptive_output(widget: QWidget) -> bool:
    return bool(
        isinstance(widget, QTextEdit)
        and widget.property("legacyOutput")
        and not widget.toPlainText().strip()
    )


def _widget_has_expanding_content(widget: QWidget) -> bool:
    if _is_empty_adaptive_output(widget):
        return False
    if isinstance(widget, _EXPANDING_TYPES):
        return True
    for child in widget.findChildren(_EXPANDING_TYPES):
        if _is_empty_adaptive_output(child):
            continue
        return True
    return False


def _layout_has_expanding_content(layout: QLayout) -> bool:
    for index in range(layout.count()):
        item = layout.itemAt(index)
        if item is None:
            continue
        widget = item.widget()
        if widget is not None and _widget_has_expanding_content(widget):
            return True
        child = item.layout()
        if child is not None and _layout_has_expanding_content(child):
            return True
    return False


def _strip_legacy_vertical_spacers(layout: QLayout) -> None:
    """Remove only expanding vertical spacer items from a compact legacy layout."""

    for index in range(layout.count() - 1, -1, -1):
        item = layout.itemAt(index)
        if item is None:
            continue

        spacer = item.spacerItem()
        if spacer is not None:
            if spacer.expandingDirections() & Qt.Orientation.Vertical:
                layout.takeAt(index)
            continue

        child = item.layout()
        if child is not None:
            _strip_legacy_vertical_spacers(child)
            continue

        widget = item.widget()
        if widget is not None and widget.layout() is not None:
            if not _widget_has_expanding_content(widget):
                _strip_legacy_vertical_spacers(widget.layout())


def _set_vertical_policy(widget: QWidget, policy: QSizePolicy.Policy) -> None:
    current = widget.sizePolicy()
    widget.setSizePolicy(current.horizontalPolicy(), policy)


def _cap_vertical_growth(widget: QWidget) -> None:
    widget.setMinimumHeight(0)
    _set_vertical_policy(widget, QSizePolicy.Policy.Maximum)

    # Wrapped legacy labels sometimes inherited a large minimum height from the
    # former full-page layout. Let Qt recompute them from their real text again.
    if isinstance(widget, QLabel):
        widget.setMaximumHeight(_MAX_WIDGET_HEIGHT)
        widget.adjustSize()


def _bind_adaptive_output(editor: QTextEdit, card: QFrame) -> None:
    """Keep an empty output compact, but let it grow as soon as content arrives."""

    if editor.property("legacyAdaptiveOutputBound"):
        return
    editor.setProperty("legacyAdaptiveOutputBound", True)

    def refresh() -> None:
        has_content = bool(editor.toPlainText().strip())
        if has_content:
            editor.setMinimumHeight(_POPULATED_OUTPUT_MINIMUM)
            editor.setMaximumHeight(_MAX_WIDGET_HEIGHT)
            _set_vertical_policy(editor, QSizePolicy.Policy.Expanding)
            _set_vertical_policy(card, QSizePolicy.Policy.Expanding)
            card.setProperty("legacyCompactGeometry", False)
        else:
            editor.setMinimumHeight(150)
            editor.setMaximumHeight(_EMPTY_OUTPUT_HEIGHT)
            _set_vertical_policy(editor, QSizePolicy.Policy.Preferred)
            # If the output is the only potentially expanding child, the whole
            # card can stay compact instead of filling half the screen with blank.
            if not _layout_has_expanding_content(card.layout()):
                _set_vertical_policy(card, QSizePolicy.Policy.Maximum)
                card.setProperty("legacyCompactGeometry", True)
        editor.updateGeometry()
        card.updateGeometry()

    editor.textChanged.connect(refresh)
    refresh()


def polish_feature_card_geometry(page: QWidget) -> None:
    """Keep feature content dense and top-aligned on tall Windows screens.

    This is presentation-only: no control is replaced, no signal is disconnected,
    and no model/controller state or enabled/disabled gate is changed.
    """

    for card in page.findChildren(QFrame):
        if not card.property("legacySectionCard"):
            continue
        layout = card.layout()
        if layout is None:
            continue

        for editor in card.findChildren(QTextEdit):
            if editor.property("legacyOutput"):
                _bind_adaptive_output(editor, card)

        card_has_expanding_content = _layout_has_expanding_content(layout)
        if not card_has_expanding_content:
            _strip_legacy_vertical_spacers(layout)
            _cap_vertical_growth(card)

        for index in range(layout.count()):
            item = layout.itemAt(index)
            if item is None:
                continue

            widget = item.widget()
            if widget is not None:
                is_heading = bool(
                    widget.property("legacySectionTitle")
                    or widget.property("legacySectionNote")
                )
                if is_heading or not _widget_has_expanding_content(widget):
                    if widget.layout() is not None:
                        _strip_legacy_vertical_spacers(widget.layout())
                    _cap_vertical_growth(widget)
                    layout.setAlignment(widget, Qt.AlignmentFlag.AlignTop)
                continue

            child_layout = item.layout()
            if child_layout is not None and not _layout_has_expanding_content(child_layout):
                _strip_legacy_vertical_spacers(child_layout)
                layout.setAlignment(child_layout, Qt.AlignmentFlag.AlignTop)

        layout.invalidate()
        layout.activate()
        card.setProperty("legacyGeometryPolished", True)
        card.setProperty("legacyCompactGeometry", not card_has_expanding_content)


__all__ = ["polish_feature_card_geometry"]
