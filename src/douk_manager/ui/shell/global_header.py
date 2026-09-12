"""Top application bar for the modern DouK Manager shell."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QVBoxLayout, QWidget

from ..theme.tokens import UiMetrics
from ..widgets.status_pill import StatusKind, StatusPill


class GlobalHeader(QWidget):
    """Presentation-only header.

    Runtime service state is pushed in by callers so this widget never owns or
    starts collector/downloader/database processes.
    """

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

        root = QHBoxLayout(self)
        root.setContentsMargins(
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_S,
            UiMetrics.SPACE_L,
            UiMetrics.SPACE_S,
        )
        root.setSpacing(UiMetrics.SPACE_L)

        self.brand_icon = QLabel("D", self)
        self.brand_icon.setObjectName("modernBrandIcon")
        self.brand_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.brand_icon.setFixedSize(42, 42)
        root.addWidget(self.brand_icon)

        brand = QVBoxLayout()
        brand.setSpacing(0)
        self.product_label = QLabel(product_name, self)
        self.product_label.setObjectName("modernProductName")
        self.workspace_label = QLabel(workspace_label, self)
        self.workspace_label.setObjectName("modernWorkspaceLabel")
        brand.addWidget(self.product_label)
        brand.addWidget(self.workspace_label)
        root.addLayout(brand)

        self.search = QLineEdit(self)
        self.search.setProperty("modernControl", True)
        self.search.setPlaceholderText("搜索账号、任务、视频或链接…")
        self.search.setMaximumWidth(470)
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

        root.addWidget(self.collector_status)
        root.addWidget(self.download_status)
        root.addWidget(self.database_status)
        root.addWidget(self.volume_status)
