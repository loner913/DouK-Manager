"""Windows geometry polish for reflowed V0.1.6 feature cards.

The modern shell keeps the original V0.1.6 widgets.  Some legacy layouts contain
vertical stretches that made sense in the old full-page design, but look awkward
inside the new card layout on 1440p/4K displays.  This module removes only those
presentation spacers and constrains compact cards; data views and output editors
remain free to expand.
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


def _widget_has_expanding_content(widget: QWidget) -> bool:
    if isinstance(widget, _EXPANDING_TYPES):
        return True
    return bool(widget.findChildren(_EXPANDING_TYPES))


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


def _cap_vertical_growth(widget: QWidget) -> None:
    policy = widget.sizePolicy()
    widget.setMinimumHeight(0)
    widget.setSizePolicy(policy.horizontalPolicy(), QSizePolicy.Policy.Maximum)

    # Wrapped legacy labels sometimes inherited a large minimum height from the
    # former full-page layout.  Let Qt recompute them from their real text again.
    if isinstance(widget, QLabel):
        widget.setMaximumHeight(16777215)
        widget.adjustSize()


def polish_feature_card_geometry(page: QWidget) -> None:
    """Keep compact feature content dense and top-aligned on tall Windows screens.

    This is presentation-only: no control is replaced, no signal is disconnected,
    and no model/controller state or enabled/disabled gate is changed.
    """

    for card in page.findChildren(QFrame):
        if not card.property("legacySectionCard"):
            continue
        layout = card.layout()
        if layout is None:
            continue

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
