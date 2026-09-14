"""Modern application shell that preserves the complete V0.1.6 GUI implementation.

The migration strategy is intentionally conservative: the legacy ``QTabWidget`` and
all of its already-wired pages remain alive. The modern shell hides the native tab
bar, routes page selection through the Fluent navigation, and wraps every page in a
V0.1.7 presentation layer without reimplementing its business behaviour.
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QComboBox, QGroupBox, QLabel, QHBoxLayout, QTabWidget, QVBoxLayout, QWidget

from douk_manager.gui import MainWindow as LegacyMainWindow

from ..pages.legacy_page import ModernLegacyPage
from ..pages.overview_page import ModernOverviewPage
from ..theme.readability import feature_readability_stylesheet
from ..theme.theme_manager import ThemeManager, ThemeMode
from ..widgets.status_pill import StatusKind
from ..icons import vector_icon
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
        "观察名单": "◉",
        "截图与索引": "▧",
        "下载结果": "▰",
        "结果看板": "◔",
        "设置": "⚙",
    }
    _PAGE_DESCRIPTIONS = {
        "账号任务": "创建与执行账号任务；原有账号表达式、Earliest、智能跳过和任务动作完整保留。",
        "账号审计": "审计账号状态、筛选建议并应用决定；所有审计模型与原操作逻辑保持不变。",
        "批次生成": "按账号范围批量生成任务模板；继续使用 V0.1.6 的生成规则与文件写入流程。",
        "下载队列": "管理任务队列、顺序与本次运行控制；暂停、恢复、取消和日志行为保持原样。",
        "账号采集": "管理采集服务、Excel 与收集箱；继续复用原采集后台服务和现有操作入口。",
        "观察名单": "查看需要复查的 W 记录；严格打开主页，预览并确认 W→A 转正，保留显式恢复入口。",
        "截图与索引": "维护截图收集箱、视频目录、索引与快捷方式；原扫描和构建流程保持不变。",
        "下载结果": "浏览 DownloadTask 任务结果与筛选条件；继续使用原任务日志读取和刷新逻辑。",
        "结果看板": "汇总任务完整性、可靠性、状态分布与账号关注项；数据全部来自原结果模型。",
        "设置": "配置正式路径、Startup 观察数据恢复、下载引擎与回退历史；安全校验和正式数据边界保持原样。",
    }

    def __init__(self, *, window_state_store=None) -> None:
        super().__init__(window_state_store=window_state_store)
        self._modern_shell_install_error: Exception | None = None
        self._modern_status_timer: QTimer | None = None
        self._modern_theme = ThemeManager(parent=self, store=self._window_state_store,
                                          style_hints=QApplication.instance().styleHints())
        self.theme_strategy_combo.setCurrentIndex(self.theme_strategy_combo.findData(self._modern_theme.strategy))
        self.theme_strategy_combo.currentIndexChanged.connect(
            lambda: self._modern_theme.set_strategy(self.theme_strategy_combo.currentData()))
        self._modern_theme.strategy_changed.connect(self._sync_theme_strategy)
        self._modern_theme.save_failed.connect(
            lambda: self.theme_save_status.setText("主题已应用，但偏好保存失败；重启后可能无法保留。"))
        try:
            self._install_modern_shell()
            self._theme_settings_page.layout().insertWidget(0, self._theme_settings_box)
        except Exception as exc:  # pragma: no cover - defensive fallback.
            self._modern_shell_install_error = exc
            self.controller.logger.exception(
                "现代 UI 外壳安装失败，已保留 V0.1.6 原界面：%s", exc
            )

    def _settings_tab(self) -> QWidget:
        page = super()._settings_tab()
        box = QGroupBox("外观")
        layout = QHBoxLayout(box)
        layout.addWidget(QLabel("主题"))
        self.theme_strategy_combo = QComboBox()
        for label, value in (("跟随系统", "system"), ("浅色", "light"), ("深色", "dark")):
            self.theme_strategy_combo.addItem(label, value)
        layout.addWidget(self.theme_strategy_combo)
        self.theme_save_status = QLabel("更改后立即生效并保存。")
        self.theme_save_status.setWordWrap(True)
        layout.addWidget(self.theme_save_status, 1)
        box.setParent(page)
        self._theme_settings_page = page
        self._theme_settings_box = box
        return page

    def _sync_theme_strategy(self, strategy: str) -> None:
        self.theme_strategy_combo.blockSignals(True)
        self.theme_strategy_combo.setCurrentIndex(self.theme_strategy_combo.findData(strategy))
        self.theme_strategy_combo.blockSignals(False)
        self.theme_save_status.setText("更改后立即生效并保存。")

    def _apply_modern_theme(self, *_args) -> None:
        theme = self._modern_theme
        p = theme.palette
        self._modern_root.setStyleSheet(
            theme.stylesheet() + "\n\n" + feature_readability_stylesheet(p)
        )
        palette = self._modern_root.palette()
        for role, colour in (
            (QPalette.ColorRole.Window, p.app_background),
            (QPalette.ColorRole.Base, p.surface),
            (QPalette.ColorRole.Text, p.text_primary),
            (QPalette.ColorRole.WindowText, p.text_primary),
            (QPalette.ColorRole.Mid, p.border),
            (QPalette.ColorRole.PlaceholderText, p.text_secondary),
        ):
            palette.setColor(role, QColor(colour))
        self._modern_root.setPalette(palette)
        dark = theme.mode is ThemeMode.DARK
        for chart in (
            self.modern_overview.trend_chart,
            self.modern_overview.donut_chart,
            self.modern_overview.throughput_chart,
        ):
            chart.setProperty("darkTheme", dark)
            chart.update()
        button = self.modern_header.theme_button
        button.setIcon(vector_icon("sun" if dark else "moon", color=p.text_primary))
        button.setToolTip("切换到白色主题" if dark else "切换到深色主题")
        button.setAccessibleName(button.toolTip())
        self._modern_root.update()

    def _install_modern_shell(self) -> None:
        legacy_tabs = self.centralWidget()
        if not isinstance(legacy_tabs, QTabWidget):
            raise RuntimeError("V0.1.6 central widget is no longer QTabWidget")

        current_index = legacy_tabs.currentIndex()
        corner = legacy_tabs.cornerWidget(Qt.Corner.TopRightCorner)
        tab_bar = legacy_tabs.tabBar()
        previous_tab_bar_visible = tab_bar.isVisible()
        previous_corner_visible = bool(corner and corner.isVisible())
        original_pages = tuple(
            (legacy_tabs.widget(index), legacy_tabs.tabText(index))
            for index in range(legacy_tabs.count())
        )

        taken = self.takeCentralWidget()
        if taken is not legacy_tabs:
            if taken is not None:
                self.setCentralWidget(taken)
            raise RuntimeError("failed to detach the V0.1.6 tab container")

        modern_root: QWidget | None = None
        modern_overview: ModernOverviewPage | None = None
        modern_feature_pages: dict[str, ModernLegacyPage] = {}
        try:
            if not original_pages:
                raise RuntimeError("V0.1.6 pages are missing")

            previous_block = legacy_tabs.blockSignals(True)
            try:
                overview_page, overview_label = original_pages[0]
                legacy_tabs.removeTab(0)
                if os.environ.get("DOUK_MANAGER_PREVIEW_LOG_DATA") == "1":
                    from ..pages.completed_overview import CompletedOverviewPage
                    modern_overview = CompletedOverviewPage(self, overview_page)
                else:
                    modern_overview = ModernOverviewPage(self, overview_page)
                legacy_tabs.insertTab(0, modern_overview, overview_label)

                for index in range(1, len(original_pages)):
                    original_page, original_label = original_pages[index]
                    # The preceding replacement keeps all later indexes stable.
                    current_page = legacy_tabs.widget(index)
                    if current_page is not original_page:
                        raise RuntimeError(f"legacy page index changed during migration: {index}")
                    legacy_tabs.removeTab(index)
                    display_label = self._LABEL_OVERRIDES.get(original_label, original_label)
                    wrapper = ModernLegacyPage(
                        original_page,
                        title=display_label,
                        subtitle=self._PAGE_DESCRIPTIONS.get(
                            display_label,
                            "V0.1.6 原功能与数据流保持不变，仅升级页面视觉与布局。",
                        ),
                    )
                    legacy_tabs.insertTab(index, wrapper, original_label)
                    modern_feature_pages[display_label] = wrapper
                legacy_tabs.setCurrentIndex(min(current_index, legacy_tabs.count() - 1))
            finally:
                legacy_tabs.blockSignals(previous_block)

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
            self.modern_feature_pages = modern_feature_pages
            self._legacy_overview_page = original_pages[0][0]
            self._legacy_feature_pages = {
                self._LABEL_OVERRIDES.get(label, label): page
                for page, label in original_pages[1:]
            }
            self._legacy_tab_corner = corner

            sidebar.route_requested.connect(self._on_modern_route_requested)
            header.search_submitted.connect(self._on_header_search)
            legacy_tabs.currentChanged.connect(self._on_legacy_tab_changed_for_shell)
            self._on_legacy_tab_changed_for_shell(legacy_tabs.currentIndex())

            header.theme_button.clicked.connect(self._modern_theme.toggle)
            self._modern_theme.theme_changed.connect(self._apply_modern_theme)
            self._apply_modern_theme()

            self._modern_status_timer = QTimer(self)
            self._modern_status_timer.setInterval(250)
            self._modern_status_timer.timeout.connect(self._sync_modern_status)
            self._modern_status_timer.start()
            self._sync_modern_status()
            QTimer.singleShot(0, self._sync_responsive_shell)
        except Exception:
            if modern_root is not None and self.centralWidget() is modern_root:
                self.takeCentralWidget()
            previous_block = legacy_tabs.blockSignals(True)
            try:
                while legacy_tabs.count():
                    legacy_tabs.removeTab(0)
                for index, (page, label) in enumerate(original_pages):
                    page.setParent(legacy_tabs)
                    legacy_tabs.insertTab(index, page, label)
                if legacy_tabs.count():
                    legacy_tabs.setCurrentIndex(min(current_index, legacy_tabs.count() - 1))
            finally:
                legacy_tabs.blockSignals(previous_block)
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
        if hasattr(self, "watchlist_model"):
            summary = self.watchlist_model.reminder_summary()
            self.modern_sidebar.set_badge("观察名单", summary["due"],
                                          bool(summary["overdue"] or summary["recovery"]))
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
