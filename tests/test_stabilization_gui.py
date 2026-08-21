from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, QRect, QSize, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QWidget

from douk_manager import gui as gui_module
from douk_manager.core.engine import ENGINE_MODE_MONITOR
from douk_manager.gui import (
    MainWindow,
    SmartSkipChoice,
    SmartSkipPreviewDialog,
)
from douk_manager.ui_state import (
    DEFAULT_WINDOW_FRACTION,
    DEFAULT_WINDOW_SIZE,
    UI_STATE_VERSION,
    WindowGeometryState,
    WindowStateStore,
    safe_window_placement,
)


class SmartSkipPreviewDialogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def _preview_text(count: int) -> str:
        return "\n".join(
            (
                "智能跳过已启用：扫描最近 3 天内全部可解析任务日志",
                f"输入账号：A1-A{count}（共 {count} 个，有效URL {count} 个）",
                "近期明确私密（将跳过）：A1、A10、A100",
                "近期明确非私密（纳入）：" + "、".join(f"A{i}" for i in range(1, count + 1)),
                f"没有可靠历史记录（纳入）：A{count}",
                "创建时可选择：按预览跳过、强制包含全部或取消创建。",
            )
        )

    def _dialog(self, count: int = 1370, *, can_skip: bool = True) -> SmartSkipPreviewDialog:
        dialog = SmartSkipPreviewDialog(self._preview_text(count), can_skip=can_skip)
        dialog.show()
        self.app.processEvents()
        self.addCleanup(dialog.deleteLater)
        return dialog

    def test_1370_account_preview_is_bounded_scrollable_complete_and_resizable(self) -> None:
        text = self._preview_text(1370)
        dialog = self._dialog()
        available = dialog.screen().availableGeometry()

        self.assertTrue(available.contains(dialog.frameGeometry()))
        self.assertEqual(dialog.details.toPlainText(), text)
        self.assertTrue(dialog.details.isReadOnly())
        self.assertGreater(dialog.details.verticalScrollBar().maximum(), 0)
        self.assertTrue(dialog.skip_button.isVisible())
        self.assertTrue(dialog.force_button.isVisible())
        self.assertTrue(dialog.cancel_button.isVisible())

        button_geometry = QRect(dialog.buttons.geometry())
        dialog.details.verticalScrollBar().setValue(
            dialog.details.verticalScrollBar().maximum()
        )
        self.app.processEvents()
        self.assertEqual(dialog.buttons.geometry(), button_geometry)
        self.assertIn("A1-A1370", dialog.details.toPlainText())
        self.assertIn("A1370", dialog.details.toPlainText())

        before = dialog.size()
        dialog.resize(max(dialog.minimumWidth(), before.width() - 50), before.height())
        self.app.processEvents()
        self.assertNotEqual(dialog.size(), before)

    def test_real_preview_formatter_preserves_all_1370_account_decisions(self) -> None:
        decisions = tuple(
            SimpleNamespace(
                a_number=number,
                category="recent_private" if number % 10 == 0 else "recent_non_private",
                source_text=f"synthetic-log-{number}",
            )
            for number in range(1, 1371)
        )
        preview = SimpleNamespace(
            validity_days=3,
            requested=SimpleNamespace(
                compact="A1-A1370",
                selected_positions=1370,
                selected_valid_urls=1370,
            ),
            effective=SimpleNamespace(compact="A1-A9,A11-A19"),
            decisions=decisions,
            private_matches=(),
        )

        text = "\n".join(MainWindow._smart_preview_lines(preview))

        self.assertIn("输入账号：A1-A1370（共 1370 个，有效URL 1370 个）", text)
        self.assertIn("近期明确非私密（纳入）：A1、A2", text)
        self.assertIn("A1369", text)
        self.assertIn("A1370：synthetic-log-1370", text)
        dialog = SmartSkipPreviewDialog(text, can_skip=True)
        self.assertEqual(dialog.details.toPlainText(), text)
        dialog.deleteLater()

    def test_account_volume_does_not_create_per_account_widgets(self) -> None:
        small = self._dialog(10)
        large = self._dialog(1370)
        self.assertEqual(len(small.findChildren(QWidget)), len(large.findChildren(QWidget)))

    def test_buttons_escape_and_window_close_return_exact_choices(self) -> None:
        cases = (
            ("skip_button", SmartSkipChoice.SKIP),
            ("force_button", SmartSkipChoice.FORCE_ALL),
            ("cancel_button", SmartSkipChoice.CANCEL),
        )
        for button_name, expected in cases:
            with self.subTest(button=button_name):
                dialog = self._dialog(20)
                getattr(dialog, button_name).click()
                self.assertEqual(dialog.choice, expected)

        escape_dialog = self._dialog(20)
        QTest.keyClick(escape_dialog, Qt.Key.Key_Escape)
        self.app.processEvents()
        self.assertEqual(escape_dialog.choice, SmartSkipChoice.CANCEL)

        close_dialog = self._dialog(20)
        close_dialog.close()
        self.app.processEvents()
        self.assertEqual(close_dialog.choice, SmartSkipChoice.CANCEL)

        disabled = self._dialog(20, can_skip=False)
        self.assertFalse(disabled.skip_button.isEnabled())


class SmartSkipTaskChoiceTests(unittest.TestCase):
    def _window(self, choice: str) -> SimpleNamespace:
        preview = SimpleNamespace(
            private_matches=(object(),),
            effective=object(),
            skipped_numbers=(3, 9),
        )
        controller = SimpleNamespace(
            preview_private_skip=Mock(return_value=preview),
            create_task=Mock(return_value=None),
        )
        return SimpleNamespace(
            task_smart_private=SimpleNamespace(isChecked=lambda: True),
            task_expression=SimpleNamespace(text=lambda: "A1-A10"),
            task_private_days=SimpleNamespace(value=lambda: 3),
            task_earliest_mode=SimpleNamespace(currentData=lambda: "keep"),
            task_earliest_value=SimpleNamespace(text=lambda: ""),
            task_persist_master=SimpleNamespace(isChecked=lambda: False),
            task_name=SimpleNamespace(text=lambda: ""),
            task_output=object(),
            controller=controller,
            _earliest_rule=Mock(return_value=object()),
            _run=lambda action, _output: action(),
            _smart_preview_lines=Mock(return_value=("full preview",)),
            dialog_choice=choice,
        )

    def test_task_creation_maps_skip_force_and_cancel_without_business_changes(self) -> None:
        class FakeDialog:
            def __init__(self, _text, *, can_skip, parent) -> None:
                self.choice = parent.dialog_choice
                self.can_skip = can_skip

            def exec(self) -> None:
                return None

        for choice, expected in (
            (SmartSkipChoice.SKIP, (3, 9)),
            (SmartSkipChoice.FORCE_ALL, ()),
            (SmartSkipChoice.CANCEL, None),
        ):
            with self.subTest(choice=choice):
                window = self._window(choice)
                with patch.object(gui_module, "SmartSkipPreviewDialog", FakeDialog):
                    MainWindow._create_task(window, activate=False, start=False)
                if expected is None:
                    window.controller.create_task.assert_not_called()
                else:
                    self.assertEqual(
                        window.controller.create_task.call_args.args[-1], expected
                    )


class WindowStateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _store(self, directory: str) -> tuple[WindowStateStore, QSettings]:
        settings = QSettings(
            str(Path(directory) / "window_state.ini"), QSettings.Format.IniFormat
        )
        settings.clear()
        return WindowStateStore(settings), settings

    def test_store_round_trip_and_corrupt_values_fall_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store, settings = self._store(directory)
            state = WindowGeometryState(QRect(25, 35, 1400, 900), True)
            self.assertTrue(store.save(state))
            self.assertEqual(store.load(), state)

            settings.setValue("main_window/width", "broken")
            settings.sync()
            self.assertIsNone(store.load())

            settings.setValue("main_window/width", 1400)
            settings.setValue("state/version", UI_STATE_VERSION + 1)
            settings.sync()
            self.assertIsNone(store.load())

    def test_geometry_is_clamped_for_missing_screen_damage_and_small_work_area(self) -> None:
        primary = QRect(0, 0, 1920, 1040)
        secondary = QRect(1920, 0, 1600, 900)
        default = safe_window_placement(None, (primary, secondary))
        expected_work_area = primary.adjusted(16, 16, -16, -16)
        self.assertEqual(
            default.geometry.size(),
            QSize(
                round(expected_work_area.width() * DEFAULT_WINDOW_FRACTION),
                round(expected_work_area.height() * DEFAULT_WINDOW_FRACTION),
            ),
        )
        self.assertTrue(primary.contains(default.geometry))
        self.assertEqual(default.geometry.center(), expected_work_area.center())

        large_screen = QRect(0, 0, 2560, 1392)
        large_default = safe_window_placement(None, (large_screen,))
        self.assertEqual(large_default.geometry.size(), QSize(2275, 1224))
        self.assertTrue(large_screen.contains(large_default.geometry))
        self.assertGreater(large_default.geometry.width(), DEFAULT_WINDOW_SIZE.width())
        self.assertGreater(large_default.geometry.height(), DEFAULT_WINDOW_SIZE.height())

        on_secondary = safe_window_placement(
            WindowGeometryState(QRect(2100, 100, 1300, 760), True),
            (primary, secondary),
        )
        self.assertTrue(secondary.contains(on_secondary.geometry))
        self.assertTrue(on_secondary.maximized)

        removed = safe_window_placement(
            WindowGeometryState(QRect(2500, 200, 5000, 4000), False), (primary,)
        )
        self.assertTrue(primary.contains(removed.geometry))

        small = QRect(50, 70, 800, 600)
        fallback = safe_window_placement(
            WindowGeometryState(QRect(-99999, -99999, 99999, 99999), False),
            (small,),
        )
        self.assertTrue(small.contains(fallback.geometry))
        self.assertEqual(fallback.geometry.size(), fallback.minimum_size)
        self.assertLess(fallback.geometry.width(), small.width())
        self.assertLess(fallback.geometry.height(), small.height())

    def test_real_main_window_saves_only_accepted_close_and_restores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "manager"
            store, _settings = self._store(directory)
            with patch.dict(os.environ, {"DOUK_MANAGER_HOME": str(root)}):
                first = MainWindow(window_state_store=store)
                first.poll_timer.stop()
                first.setGeometry(120, 140, 1180, 730)
                event = SimpleNamespace(accept=Mock(), ignore=Mock())
                first.closeEvent(event)
                event.accept.assert_called_once_with()
                event.ignore.assert_not_called()
                saved = store.load()
                self.assertIsNotNone(saved)
                for handler in list(first.controller.logger.handlers):
                    first.controller.logger.removeHandler(handler)
                    handler.close()

                restored = MainWindow(window_state_store=store)
                restored.poll_timer.stop()
                self.assertEqual(restored.geometry(), saved.normal_geometry)
                self.assertTrue(
                    any(
                        screen.availableGeometry().contains(restored.geometry())
                        for screen in QApplication.screens()
                    )
                )
                for handler in list(restored.controller.logger.handlers):
                    restored.controller.logger.removeHandler(handler)
                    handler.close()
                first.deleteLater()
                restored.deleteLater()
                self.app.processEvents()

    def test_real_default_window_stays_inside_available_screen_after_show(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "manager"
            store, _settings = self._store(directory)
            with patch.dict(os.environ, {"DOUK_MANAGER_HOME": str(root)}):
                window = MainWindow(window_state_store=store)
                try:
                    window.poll_timer.stop()
                    window.show()
                    self.app.processEvents()
                    available = window.screen().availableGeometry()
                    self.assertTrue(
                        available.contains(window.geometry()),
                        f"available={available}; geometry={window.geometry()}; "
                        f"minimum={window.minimumSize()}; hint={window.sizeHint()}",
                    )
                finally:
                    for handler in list(window.controller.logger.handlers):
                        window.controller.logger.removeHandler(handler)
                        handler.close()
                    window.hide()
                    window.deleteLater()
                    self.app.processEvents()

    def test_real_main_window_restores_maximized_and_saves_only_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "manager"
            settings = QSettings(
                str(Path(directory) / "maximized.ini"), QSettings.Format.IniFormat
            )
            store = WindowStateStore(settings)
            self.assertTrue(
                store.save(WindowGeometryState(QRect(40, 50, 720, 700), True))
            )
            original_save = store.save
            store.save = Mock(side_effect=original_save)
            with patch.dict(os.environ, {"DOUK_MANAGER_HOME": str(root)}):
                window = MainWindow(window_state_store=store)
                window.poll_timer.stop()
                self.assertTrue(
                    bool(window.windowState() & Qt.WindowState.WindowMaximized)
                )
                first = SimpleNamespace(accept=Mock(), ignore=Mock())
                second = SimpleNamespace(accept=Mock(), ignore=Mock())
                window.closeEvent(first)
                window.closeEvent(second)
                self.assertEqual(store.save.call_count, 1)
                for handler in list(window.controller.logger.handlers):
                    window.controller.logger.removeHandler(handler)
                    handler.close()
                window.deleteLater()
                self.app.processEvents()

    def test_default_store_is_isolated_by_synthetic_manager_home(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "manager"
            with patch.dict(
                os.environ,
                {"DOUK_MANAGER_HOME": str(root)},
                clear=False,
            ):
                with patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("DOUK_MANAGER_UI_STATE_PATH", None)
                    store = WindowStateStore.default()
                    self.assertTrue(
                        store.save(
                            WindowGeometryState(QRect(20, 30, 1100, 720), False)
                        )
                    )
            self.assertTrue((root / "Data" / "UI" / "window_state.ini").is_file())

    def test_every_rejected_close_path_leaves_state_unsaved(self) -> None:
        def base_window() -> SimpleNamespace:
            controller = SimpleNamespace(
                engine=SimpleNamespace(current=None),
                collector=SimpleNamespace(process=None),
                begin_closing=Mock(),
                stop_collector=Mock(),
            )
            return SimpleNamespace(
                controller=controller,
                background_thread=None,
                coordinator=SimpleNamespace(
                    has_active_tasks=Mock(return_value=False),
                    begin_closing=Mock(),
                ),
                _close_pending=False,
                _apply_action_gate=Mock(),
                _background_bindings={},
                _start_collector_stop_on_close=Mock(),
                _save_window_state_once=Mock(),
            )

        def invoke(window: SimpleNamespace, *, run=False, summary=False) -> None:
            event = SimpleNamespace(accept=Mock(), ignore=Mock())
            with patch.object(MainWindow, "_download_run_blocks_close", return_value=run), patch.object(
                MainWindow, "_download_summary_is_active", return_value=summary
            ), patch.object(gui_module.QMessageBox, "information"), patch.object(
                gui_module.QMessageBox, "critical"
            ):
                MainWindow.closeEvent(window, event)
            event.ignore.assert_called_once_with()
            event.accept.assert_not_called()
            window._save_window_state_once.assert_not_called()

        invoke(base_window(), run=True)
        invoke(base_window(), summary=True)

        monitor = base_window()
        monitor.controller.engine.current = SimpleNamespace(
            mode=ENGINE_MODE_MONITOR, running=True
        )
        invoke(monitor)

        index = base_window()
        index.background_thread = SimpleNamespace(isRunning=lambda: True)
        invoke(index)

        background = base_window()
        background.coordinator.has_active_tasks.return_value = True
        invoke(background)

        collector = base_window()
        collector.controller.collector.process = object()
        invoke(collector)

        failed_stop = base_window()
        del failed_stop._background_bindings
        failed_stop.controller.collector.process = object()
        failed_stop.controller.stop_collector.side_effect = RuntimeError("stop failed")
        invoke(failed_stop)


if __name__ == "__main__":
    unittest.main()
