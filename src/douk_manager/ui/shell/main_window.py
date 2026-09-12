"""Modern application shell that preserves the complete V0.1.6 GUI implementation.

The migration strategy is intentionally conservative: the legacy ``QTabWidget`` and
all of its already-wired pages remain alive and keep their original object identity.
This class only hides the tab bar and routes page selection through the new Fluent
navigation shell.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QTabWidget, QVBoxLayout, QWidget

from douk_manager.gui import MainWindow as LegacyMainWindow

from ..theme.theme_manager import ThemeManager, ThemeMode
from ..widgets.status_pill import StatusKind
from .global_header import GlobalHeader
from .navigation import NavigationItem, NavigationSidebar


class ModernMainWindow(LegacyMainWindow):
    """V0.1.7 shell layered around the proven V0.1.6 window.

    No controller, background-task, startup-safety, queue, collector, audit,
    result, or persistence behaviour is reimplemented here. Every existing
    page remains the exact widget created by :class:`LegacyMainWindow`.
    """

    _LABEL_OVERRIDES = {
        "路径与安全设置": "设置",
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
        try:
            modern_root = QWidget(self)
            modern_root.setProperty("modernUi", True)
            modern_root.setObjectName("modernRoot")

            root_layout = QVBoxLayout(modern_root)
            root_layout.setContentsMargins(0, 0, 0, 0)
            root_layout.setSpacing(0)

            header = GlobalHeader(parent=modern_root)
            # A global search was present in the visual concept, but V0.1.6 has
            # no equivalent contract. Keep it hidden until it can be backed by
            # real behaviour instead of shipping a decorative dead control.
            header.search.hide()
            root_layout.addWidget(header)

            body = QWidget(modern_root)
            body.setProperty("modernUi", True)
            body_layout = QHBoxLayout(body)
            body_layout.setContentsMargins(0, 0, 0, 0)
            body_layout.setSpacing(0)

            navigation_items = tuple(
                NavigationItem(
                    route=self._route_for_index(index),
                    label=self._LABEL_OVERRIDES.get(
                        legacy_tabs.tabText(index), legacy_tabs.tabText(index)
                    ),
                )
                for index in range(legacy_tabs.count())
            )
            sidebar = NavigationSidebar(navigation_items, body)
            body_layout.addWidget(sidebar)

            # This is the key compatibility guarantee: the original QTabWidget
            # itself is retained, with all page instances and signal wiring
            # untouched. Only its native tab strip is hidden.
            legacy_tabs.setParent(body)
            tab_bar.hide()
            if corner is not None:
                corner.hide()
            body_layout.addWidget(legacy_tabs, 1)
            root_layout.addWidget(body, 1)

            self.setCentralWidget(modern_root)
            self._modern_root = modern_root
            self.modern_header = header
            self.modern_sidebar = sidebar
            self._legacy_tab_corner = corner

            sidebar.route_requested.connect(self._on_modern_route_requested)
            legacy_tabs.currentChanged.connect(self._on_legacy_tab_changed_for_shell)
            self._on_legacy_tab_changed_for_shell(current_index)

            # Apply only selectors scoped to the modern shell. The V0.1.6 page
            # stylesheet remains in force for the legacy pages during migration.
            modern_root.setStyleSheet(self._modern_theme.stylesheet())

            self._modern_status_timer = QTimer(self)
            self._modern_status_timer.setInterval(250)
            self._modern_status_timer.timeout.connect(self._sync_modern_status)
            self._modern_status_timer.start()
            self._sync_modern_status()
        except Exception:
            if modern_root is not None and self.centralWidget() is modern_root:
                self.takeCentralWidget()
            legacy_tabs.setParent(self)
            self.setCentralWidget(legacy_tabs)
            tab_bar.setVisible(previous_tab_bar_visible)
            if corner is not None:
                corner.setVisible(previous_corner_visible)
            raise

    @staticmethod
    def _route_for_index(index: int) -> str:
        return f"legacy-tab:{index}"

    def _on_modern_route_requested(self, route: str) -> None:
        prefix = "legacy-tab:"
        if not route.startswith(prefix):
            return
        try:
            index = int(route.removeprefix(prefix))
        except ValueError:
            return
        if 0 <= index < self.tabs.count():
            # Switching the existing QTabWidget intentionally triggers the
            # original V0.1.6 currentChanged -> _tab_changed path.
            self.tabs.setCurrentIndex(index)

    def _on_legacy_tab_changed_for_shell(self, index: int) -> None:
        if hasattr(self, "modern_sidebar") and 0 <= index < self.tabs.count():
            self.modern_sidebar.set_current_route(self._route_for_index(index))

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
