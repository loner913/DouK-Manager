"""Modern application shell that preserves the complete V0.1.6 GUI implementation.

The migration strategy is intentionally conservative: the legacy ``QTabWidget`` and
all of its already-wired pages remain alive. The modern shell hides the native tab
bar, routes page selection through the Fluent navigation, and progressively wraps
individual pages without reimplementing their business behaviour.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QTabWidget, QVBoxLayout, QWidget

from douk_manager.gui import MainWindow as LegacyMainWindow

from ..pages.overview_page import ModernOverviewPage
from ..theme.theme_manager import ThemeManager, ThemeMode
from ..widgets.status_pill import StatusKind
from .global_header import GlobalHeader
from .navigation import NavigationItem, NavigationSidebar


class ModernMainWindow(LegacyMainWindow):
    """V0.1.7 shell layered around the proven V0.1.6 window."""

    _LABEL_OVERRIDES = {
        "路径与安全设置": "设置",
    }
    _NAV_ICONS = {
        "总览": "⌂",
        "账号任务": "▤",
        "账号审计": "◇",
        "批次生成": "⊞",
        "下载队列": "⇩",
        "账号采集": "●",
        "截图与索引": "▧",
        "下载结果": "▰",
        "结果看板": "◔",
        "设置": "⚙",
    }

    def __init__(self, *, window_state_store=None) -> None:
        super().__init__(window_state_store=window_state_store)
        self._modern_shell_install_error: Exception | None = None
        self._modern_status_timer: QTimer | None = None
        self._modern_theme = ThemeManager(ThemeMode.LIGHT, self)
        try:
            self._install_modern_shell()
        except Exception as exc:  # pragma: no cover - defensive fallback.
            self._modern_shell_install_error = exc
            self.controller.logger.exception(
                "现代 UI 外壳安装失败，已保留 V0.1.6 原界面：%s", exc
            )

    def _install_modern_shell(self) -> None:
        legacy_tabs = self.centralWidget()
        if not isinstance(legacy_tabs, QTabWidget):
            raise RuntimeError("V0.1.6 central widget is no longer QTabWidget")

        current_index = legacy_tabs.currentIndex()
        corner = legacy_tabs.cornerWidget(Qt.Corner.TopRightCorner)
        tab_bar = legacy_tabs.tabBar()
        previous_tab_bar_visible = tab_bar.isVisible()
        previous_corner_visible = bool(corner and corner.isVisible())

        taken = self.takeCentralWidget()
        if taken is not legacy_tabs:
            if taken is not None:
                self.setCentralWidget(taken)
            raise RuntimeError("failed to detach the V0.1.6 tab container")

        modern_root: QWidget | None = None
        legacy_overview: QWidget | None = None
        modern_overview: ModernOverviewPage | None = None
        overview_label = ""
        overview_replaced = False
        try:
            if legacy_tabs.count() < 1:
                raise RuntimeError("V0.1.6 overview tab is missing")
            legacy_overview = legacy_tabs.widget(0)
            overview_label = legacy_tabs.tabText(0)
            legacy_tabs.removeTab(0)
            modern_overview = ModernOverviewPage(self, legacy_overview)
            legacy_tabs.insertTab(0, modern_overview, overview_label)
            overview_replaced = True
            legacy_tabs.setCurrentIndex(min(current_index, legacy_tabs.count() - 1))

            modern_root = QWidget(self)
            modern_root.setProperty("modernUi", True)
            modern_root.setObjectName("modernRoot")
            root_layout = QVBoxLayout(modern_root)
            root_layout.setContentsMargins(0, 0, 0, 0)
            root_layout.setSpacing(0)

            header = GlobalHeader(parent=modern_root)
            root_layout.addWidget(header)

            body = QWidget(modern_root)
            body.setProperty("modernUi", True)
            body_layout = QHBoxLayout(body)
            body_layout.setContentsMargins(0, 0, 0, 0)
            body_layout.setSpacing(0)

            navigation_items = tuple(
                NavigationItem(
                    route=self._route_for_index(index),
                    label=self._display_label_for_index(index),
                    icon_text=self._NAV_ICONS.get(self._display_label_for_index(index), "•"),
                    placement=(
                        "bottom" if self._display_label_for_index(index) == "设置" else "main"
                    ),
                )
                for index in range(legacy_tabs.count())
            )
            sidebar = NavigationSidebar(navigation_items, body)
            body_layout.addWidget(sidebar)

            legacy_tabs.setParent(body)
            legacy_tabs.setObjectName("modernPageHost")
            legacy_tabs.setDocumentMode(True)
            tab_bar.hide()
            if corner is not None:
                corner.hide()
            body_layout.addWidget(legacy_tabs, 1)
            root_layout.addWidget(body, 1)

            self.setCentralWidget(modern_root)
            self._modern_root = modern_root
            self.modern_header = header
            self.modern_sidebar = sidebar
            self.modern_overview = modern_overview
            self._legacy_overview_page = legacy_overview
            self._legacy_tab_corner = corner

            sidebar.route_requested.connect(self._on_modern_route_requested)
            header.search_submitted.connect(self._on_header_search)
            legacy_tabs.currentChanged.connect(self._on_legacy_tab_changed_for_shell)
            self._on_legacy_tab_changed_for_shell(legacy_tabs.currentIndex())

            modern_root.setStyleSheet(self._modern_theme.stylesheet())

            self._modern_status_timer = QTimer(self)
            self._modern_status_timer.setInterval(250)
            self._modern_status_timer.timeout.connect(self._sync_modern_status)
            self._modern_status_timer.start()
            self._sync_modern_status()
            QTimer.singleShot(0, self._sync_responsive_shell)
        except Exception:
            if modern_root is not None and self.centralWidget() is modern_root:
                self.takeCentralWidget()
            if overview_replaced and modern_overview is not None and legacy_overview is not None:
                index = legacy_tabs.indexOf(modern_overview)
                if index >= 0:
                    legacy_tabs.removeTab(index)
                legacy_overview.setParent(legacy_tabs)
                legacy_tabs.insertTab(0, legacy_overview, overview_label)
                legacy_tabs.setCurrentIndex(min(current_index, legacy_tabs.count() - 1))
            legacy_tabs.setParent(self)
            self.setCentralWidget(legacy_tabs)
            tab_bar.setVisible(previous_tab_bar_visible)
            if corner is not None:
                corner.setVisible(previous_corner_visible)
            raise

    def _display_label_for_index(self, index: int) -> str:
        label = self.tabs.tabText(index)
        return self._LABEL_OVERRIDES.get(label, label)

    @staticmethod
    def _route_for_index(index: int) -> str:
        return f"legacy-tab:{index}"

    def navigate_to_page_label(self, label: str) -> bool:
        target = label.strip()
        for index in range(self.tabs.count()):
            if self._display_label_for_index(index) == target:
                self.tabs.setCurrentIndex(index)
                return True
        return False

    def _on_header_search(self, query: str) -> None:
        route = self.modern_sidebar.route_for_query(query)
        if route is None:
            self.modern_header.search.selectAll()
            self.modern_header.search.setToolTip("未找到匹配页面；可搜索总览、下载队列、结果看板等。")
            return
        self.modern_header.search.setToolTip("")
        self._on_modern_route_requested(route)
        self.modern_header.search.clear()

    def _on_modern_route_requested(self, route: str) -> None:
        prefix = "legacy-tab:"
        if not route.startswith(prefix):
            return
        try:
            index = int(route.removeprefix(prefix))
        except ValueError:
            return
        if 0 <= index < self.tabs.count():
            self.tabs.setCurrentIndex(index)

    def _on_legacy_tab_changed_for_shell(self, index: int) -> None:
        if hasattr(self, "modern_sidebar") and 0 <= index < self.tabs.count():
            self.modern_sidebar.set_current_route(self._route_for_index(index))
        if index == 0 and hasattr(self, "modern_overview"):
            self.modern_overview.activate()

    def _sync_responsive_shell(self) -> None:
        if not hasattr(self, "modern_sidebar"):
            return
        width = self.width()
        self.modern_sidebar.set_collapsed(width < 1050)
        self.modern_header.set_compact(width < 1120)
        if hasattr(self, "modern_overview"):
            self.modern_overview.set_viewport_width(self.tabs.width())

    def resizeEvent(self, event) -> None:  # noqa: N802 - Qt API
        super().resizeEvent(event)
        if hasattr(self, "modern_sidebar"):
            self._sync_responsive_shell()

    def _sync_modern_status(self) -> None:
        """Mirror existing V0.1.6 status labels without querying backends again."""

        if not hasattr(self, "modern_header"):
            return
        collector = getattr(self, "global_collector_status", None)
        engine = getattr(self, "global_engine_status", None)
        status_labels = getattr(self, "status_labels", {})
        self._set_header_status(
            self.modern_header.collector_status,
            collector.text() if collector is not None else "检查中",
        )
        self._set_header_status(
            self.modern_header.download_status,
            engine.text() if engine is not None else "检查中",
        )
        database = status_labels.get("database")
        volume = status_labels.get("volume")
        self._set_header_status(
            self.modern_header.database_status,
            database.text() if database is not None else "检查中",
        )
        self._set_header_status(
            self.modern_header.volume_status,
            volume.text() if volume is not None else "检查中",
        )

    @staticmethod
    def _set_header_status(pill, value: str) -> None:
        normalised = value.strip()
        if normalised in {"运行中", "后台监听", "批量下载", "正常"}:
            kind = StatusKind.SUCCESS
        elif "失败" in normalised or "异常" in normalised:
            kind = StatusKind.DANGER
        elif "缺失" in normalised or "未配置" in normalised:
            kind = StatusKind.WARNING
        elif normalised in {"检查中", "未知", "未运行", ""}:
            kind = StatusKind.NEUTRAL
        else:
            kind = StatusKind.INFO
        pill.set_status(value=normalised or "未知", kind=kind)
