"""Top application bar for the modern DouK Manager shell."""

from __future__ import annotations

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from ..theme.tokens import UiMetrics
from ..widgets.status_pill import StatusKind, StatusPill


class GlobalHeader(QWidget):
    """Presentation-only header with a real page-navigation search box."""

    search_submitted = Signal(str)

    def __init__(
        self,
        *,
        product_name: str = "DouK Manager",
        workspace_label: str = "全流程工作区 · V0.1.7",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setProperty("modernUi", True)
        self.setObjectName("modernHeader")
        self.setFixedHeight(UiMetrics.HEADER_HEIGHT)
        self._compact = False

        root = QHBoxLayout(self)
        root.setContentsMargins(
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_S,
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_S,
        )
        root.setSpacing(UiMetrics.SPACE_M)

        self.brand_icon = QLabel("D", self)
        self.brand_icon.setObjectName("modernBrandIcon")
        self.brand_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.brand_icon.setFixedSize(42, 42)
        root.addWidget(self.brand_icon)

        self.brand_block = QWidget(self)
        brand = QVBoxLayout(self.brand_block)
        brand.setContentsMargins(0, 0, 0, 0)
        brand.setSpacing(0)
        self.product_label = QLabel(product_name, self.brand_block)
        self.product_label.setObjectName("modernProductName")
        self.workspace_label = QLabel(workspace_label, self.brand_block)
        self.workspace_label.setObjectName("modernWorkspaceLabel")
        brand.addWidget(self.product_label)
        brand.addWidget(self.workspace_label)
        root.addWidget(self.brand_block)

        self.search = QLineEdit(self)
        self.search.setProperty("modernControl", True)
        self.search.setObjectName("modernGlobalSearch")
        self.search.setPlaceholderText("搜索页面或功能…")
        self.search.setMinimumWidth(180)
        self.search.setMaximumWidth(430)
        self.search.returnPressed.connect(
            lambda: self.search_submitted.emit(self.search.text().strip())
        )
        root.addWidget(self.search, 1)

        self.collector_status = StatusPill(
            "采集服务", "未知", kind=StatusKind.NEUTRAL, parent=self
        )
        self.download_status = StatusPill(
            "下载进程", "未知", kind=StatusKind.NEUTRAL, parent=self
        )
        self.database_status = StatusPill(
            "数据库", "未知", kind=StatusKind.NEUTRAL, parent=self
        )
        self.volume_status = StatusPill(
            "Volume", "未知", kind=StatusKind.NEUTRAL, parent=self
        )
        self.status_pills = (
            self.collector_status,
            self.download_status,
            self.database_status,
            self.volume_status,
        )
        for pill in self.status_pills:
            root.addWidget(pill)

    def set_compact(self, compact: bool) -> None:
        if self._compact == compact:
            return
        self._compact = compact
        self.workspace_label.setVisible(not compact)
        self.product_label.setVisible(not compact)
        self.search.setVisible(not compact)
        for pill in self.status_pills:
            pill.set_compact(compact)
