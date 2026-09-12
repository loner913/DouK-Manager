"""Modern presentation wrapper for untouched V0.1.6 feature pages.

The wrapper changes presentation only: every original widget instance remains alive,
with the same signals, slots, models, delegates and controller references.  This lets
V0.1.7 modernise all feature pages in one pass without re-implementing behaviour.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractButton,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDateTimeEdit,
    QDoubleSpinBox,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListView,
    QListWidget,
    QPlainTextEdit,
    QProgressBar,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTableView,
    QTableWidget,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from ..theme.tokens import UiMetrics


_PRIMARY_HINTS = (
    "创建并",
    "创建、激活",
    "开始下载",
    "开始执行",
    "执行回退",
    "保存各路径",
    "保存配置",
    "应用",
)
_DANGER_HINTS = (
    "取消全部",
    "取消当前",
    "停止采集",
    "停止任务",
    "删除",
    "清空",
)


class ModernLegacyPage(QWidget):
    """Place one original feature page inside the V0.1.7 visual system."""

    def __init__(
        self,
        legacy_page: QWidget,
        *,
        title: str,
        subtitle: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("modernUi", True)
        self.setObjectName("modernLegacyPage")
        self.legacy_page = legacy_page
        self.page_title = title

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.scroll = QScrollArea(self)
        self.scroll.setObjectName("modernLegacyScroll")
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        root.addWidget(self.scroll)

        canvas = QWidget(self.scroll)
        canvas.setProperty("modernUi", True)
        canvas.setObjectName("modernLegacyCanvas")
        self.scroll.setWidget(canvas)

        layout = QVBoxLayout(canvas)
        layout.setContentsMargins(
            UiMetrics.PAGE_MARGIN,
            22,
            UiMetrics.PAGE_MARGIN,
            UiMetrics.PAGE_MARGIN,
        )
        layout.setSpacing(14)

        heading = QHBoxLayout()
        heading.setSpacing(12)
        text = QVBoxLayout()
        text.setSpacing(3)
        title_label = QLabel(title, canvas)
        title_label.setObjectName("modernPageTitle")
        subtitle_label = QLabel(subtitle, canvas)
        subtitle_label.setObjectName("modernPageSubtitle")
        subtitle_label.setWordWrap(True)
        text.addWidget(title_label)
        text.addWidget(subtitle_label)
        heading.addLayout(text, 1)
        layout.addLayout(heading)

        self.surface = QFrame(canvas)
        self.surface.setProperty("modernUi", True)
        self.surface.setProperty("modernFeatureSurface", True)
        self.surface.setObjectName("modernFeatureSurface")
        surface_layout = QVBoxLayout(self.surface)
        surface_layout.setContentsMargins(18, 18, 18, 18)
        surface_layout.setSpacing(12)

        legacy_page.setParent(self.surface)
        legacy_page.setProperty("legacyRoot", True)
        legacy_page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._mark_descendants(legacy_page)
        surface_layout.addWidget(legacy_page, 1)
        layout.addWidget(self.surface, 1)

    @staticmethod
    def _button_role(text: str) -> str:
        normalised = "".join(text.split())
        if any(hint in normalised for hint in _DANGER_HINTS):
            return "danger"
        if any(hint in normalised for hint in _PRIMARY_HINTS):
            return "primary"
        return "secondary"

    def _mark_descendants(self, root: QWidget) -> None:
        """Attach presentation properties only; do not change behaviour/state."""

        control_types = (
            QLineEdit,
            QComboBox,
            QSpinBox,
            QDoubleSpinBox,
            QDateEdit,
            QDateTimeEdit,
            QTextEdit,
            QPlainTextEdit,
            QTableView,
            QTableWidget,
            QListView,
            QListWidget,
            QTreeView,
            QProgressBar,
            QCheckBox,
            QRadioButton,
        )
        for widget in root.findChildren(QWidget):
            if isinstance(widget, QGroupBox):
                widget.setProperty("legacyCard", True)
            if isinstance(widget, control_types):
                widget.setProperty("legacyModernized", True)
            if isinstance(widget, QAbstractButton):
                widget.setProperty("legacyModernized", True)
                if isinstance(widget, (QCheckBox, QRadioButton)):
                    continue
                widget.setProperty("legacyRole", self._button_role(widget.text()))
            # Re-polish because many V0.1.6 widgets already existed before the
            # modern stylesheet was installed.
            widget.style().unpolish(widget)
            widget.style().polish(widget)

        root.style().unpolish(root)
        root.style().polish(root)


__all__ = ["ModernLegacyPage"]
