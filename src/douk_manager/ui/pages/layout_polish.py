"""Windows geometry polish for reflowed V0.1.6 feature cards.

The reflow layer intentionally keeps the original V0.1.6 widgets.  On very tall
windows Qt can still give ``Preferred`` widgets part of the unused vertical space,
which makes compact forms look vertically centred inside otherwise empty cards.
This module changes only layout alignment/size-policy hints so compact content stays
at the top while genuine data views and text outputs remain free to expand.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
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


def _cap_vertical_growth(widget: QWidget) -> None:
    policy = widget.sizePolicy()
    widget.setSizePolicy(policy.horizontalPolicy(), QSizePolicy.Policy.Maximum)


def polish_feature_card_geometry(page: QWidget) -> None:
    """Keep compact section-card content top-aligned on tall Windows displays.

    This is deliberately presentation-only.  It does not replace widgets, alter
    enabled state, disconnect signals, change models, or touch controller logic.
    """

    for card in page.findChildren(QFrame):
        if not card.property("legacySectionCard"):
            continue
        layout = card.layout()
        if layout is None:
            continue

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
                    _cap_vertical_growth(widget)
                    layout.setAlignment(widget, Qt.AlignmentFlag.AlignTop)
                continue

            child_layout = item.layout()
            if child_layout is not None and not _layout_has_expanding_content(child_layout):
                layout.setAlignment(child_layout, Qt.AlignmentFlag.AlignTop)

        card.setProperty("legacyGeometryPolished", True)


__all__ = ["polish_feature_card_geometry"]
