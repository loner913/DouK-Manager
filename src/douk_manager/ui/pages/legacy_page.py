"""Modern presentation wrapper for untouched V0.1.6 feature pages.

The wrapper changes presentation only: every original widget instance remains alive,
with the same signals, slots, models, delegates and controller references. This lets
V0.1.7 modernise all feature pages without re-implementing behaviour.
"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QShowEvent
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
    QPushButton,
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
from .feature_reflow import reflow_feature_page
from .layout_polish import polish_feature_card_geometry


_PRIMARY_HINTS = (
    "创建并",
    "创建、激活",
    "开始下载",
    "开始执行",
    "生成全部批次",
    "重新审计",
    "按顺序运行已选",
    "启动采集服务",
    "立即安全归档截图",
    "立即刷新索引",
    "立即刷新结果",
    "保存全部设置",
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


def _button_role(text: str) -> str:
    normalised = "".join(text.split())
    if any(hint in normalised for hint in _DANGER_HINTS):
        return "danger"
    if any(hint in normalised for hint in _PRIMARY_HINTS):
        return "primary"
    return "secondary"


def mark_legacy_descendants(root: QWidget) -> None:
    """Attach the shared modern presentation properties to one legacy page."""

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
            widget.setProperty("legacyRole", _button_role(widget.text()))
        # Re-polish because many V0.1.6 widgets already existed before the
        # modern stylesheet was installed.
        widget.style().unpolish(widget)
        widget.style().polish(widget)

    root.style().unpolish(root)
    root.style().polish(root)


class ModernLegacyPage(QWidget):
    """Place one original feature page inside the V0.1.7 visual system.

    ``QTabWidget`` explicitly hides every non-current page. Reparenting one of those
    pages preserves that explicit hidden state, so a wrapper can otherwise appear
    completely blank even though every original control is still present. The
    wrapper therefore explicitly restores visibility after reparenting and again
    when the wrapper itself is shown.
    """

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
        self.layout_profile: str | None = None
        self.layout_reflow_error: Exception | None = None

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
        text.addWidget(title_label)
        heading.addLayout(text, 1)
        layout.addLayout(heading)

        self.surface = QFrame(canvas)
        self.surface.setProperty("modernUi", True)
        self.surface.setProperty("modernFeatureSurface", True)
        self.surface.setObjectName("modernFeatureSurface")
        surface_layout = QVBoxLayout(self.surface)
        surface_layout.setContentsMargins(0, 0, 0, 0)
        surface_layout.setSpacing(12)

        legacy_page.setParent(self.surface)
        legacy_page.setProperty("legacyRoot", True)
        if legacy_page.objectName() == "resultDashboardScrollArea":
            legacy_page.viewport().setObjectName("modernDashboardViewport")
            legacy_page.widget().setObjectName("modernDashboardCanvas")
        legacy_page.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self._mark_descendants(legacy_page)
        try:
            self.layout_profile = reflow_feature_page(legacy_page, title)
            polish_feature_card_geometry(legacy_page)
            legacy_page.setProperty("legacyReflowed", True)
            # Keep optional usage guidance accessible without stacking another
            # full-width text band between the title and the working controls.
            content = legacy_page.widget() if isinstance(legacy_page, QScrollArea) else legacy_page
            content_layout = content.layout()
            guidance = []
            for index in range(content_layout.count()):
                widget = content_layout.itemAt(index).widget()
                if (isinstance(widget, QLabel) and widget.property("legacyBanner")
                        and widget.property("legacyBannerKind") == "info"):
                    guidance.append(widget)
                    widget.hide()
            if guidance:
                help_button = QPushButton("使用说明", canvas)
                help_button.setObjectName("modernPageHelp")
                help_button.setCheckable(True)
                for widget in guidance:
                    help_button.toggled.connect(widget.setVisible)
                heading.addWidget(help_button, 0, Qt.AlignmentFlag.AlignVCenter)
        except Exception as exc:  # Defensive: visual reflow must never remove V0.1.6 access.
            self.layout_reflow_error = exc
            legacy_page.setProperty("legacyReflowed", False)
        surface_layout.addWidget(legacy_page, 1)
        if title == "设置":
            self.surface.setMaximumWidth(1380)

        # QTabWidget hides inactive pages explicitly. That hidden flag survives
        # setParent(), so every page except the tab that happened to be current at
        # migration time would otherwise remain invisible inside its new wrapper.
        legacy_page.show()

        layout.addWidget(self.surface, 1)

    @staticmethod
    def _button_role(text: str) -> str:
        return _button_role(text)

    def _mark_descendants(self, root: QWidget) -> None:
        """Attach presentation properties only; do not change behaviour/state."""
        mark_legacy_descendants(root)

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt API
        super().showEvent(event)
        # Defensive repeat: keep the legacy root visible whenever this wrapper is
        # the active page, without touching any child control's own visibility.
        if self.legacy_page.isHidden():
            self.legacy_page.show()


__all__ = ["ModernLegacyPage", "mark_legacy_descendants"]
