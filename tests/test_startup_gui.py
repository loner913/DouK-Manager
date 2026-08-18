from __future__ import annotations

import inspect
import os
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEventLoop, QTimer
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QPushButton

from douk_manager import app as app_module
from douk_manager.gui import MainWindow
from douk_manager.startup import StartupSafetyResult, StartupStage, StartupState


def make_health() -> dict[str, object]:
    return {
        "engine_exe": True,
        "volume": True,
        "master_settings": True,
        "active_settings": True,
        "database": True,
        "video_root": True,
        "index_root": True,
        "collector_running": False,
        "engine_running": False,
        "engine_mode": "",
        "master_positions": 4,
        "master_valid_urls": 4,
    }


def make_result(
    generation: int,
    *,
    success: bool = True,
    state: StartupState = StartupState.READY,
    summary: str = "启动安全检查通过。",
    details: str = "synthetic startup result",
) -> StartupSafetyResult:
    return StartupSafetyResult(
        generation=generation,
        success=success,
        state=state,
        stage=StartupStage.SNAPSHOT,
        summary=summary,
        details=details,
        health=make_health(),
        startup_backup=Path("synthetic-backup.json") if success else None,
    )


class StartupGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def _window(self, root: Path) -> MainWindow:
        self._home_patch = patch.dict(os.environ, {"DOUK_MANAGER_HOME": str(root)})
        self._home_patch.start()
        window = MainWindow()
        window.show()
        self.app.processEvents()
        return window

    def _dispose_window(self, window: MainWindow) -> None:
        window.hide()
        window.deleteLater()
        self.app.processEvents()
        logger = getattr(window.controller, "logger", None)
        if logger is not None:
            for handler in list(logger.handlers):
                logger.removeHandler(handler)
                handler.close()
        self._home_patch.stop()

    def _run_until(self, condition, *, timeout_ms: int = 3000) -> None:
        if condition():
            return
        loop = QEventLoop()
        timed_out: list[bool] = []
        poll = QTimer()
        poll.setInterval(1)

        def check() -> None:
            if condition():
                poll.stop()
                loop.quit()

        def timeout() -> None:
            timed_out.append(True)
            loop.quit()

        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(timeout)
        poll.timeout.connect(check)
        poll.start()
        timer.start(timeout_ms)
        loop.exec()
        poll.stop()
        timer.stop()
        self.assertFalse(timed_out, "bounded GUI event loop timed out")
        self.assertTrue(condition())

    def test_constructor_is_show_first_and_app_schedules_startup_after_show(self) -> None:
        constructor_source = inspect.getsource(MainWindow.__init__)
        app_source = inspect.getsource(app_module.main)

        self.assertNotIn("try_startup_backup", constructor_source)
        self.assertNotIn("refresh_all", constructor_source)
        self.assertLess(app_source.index("window.show()"), app_source.index("QTimer.singleShot"))
        self.assertIn("window.begin_startup_check", app_source)

    def test_real_offscreen_window_stays_responsive_while_safety_action_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            entered = threading.Event()
            release = threading.Event()
            heartbeat = [0]
            service = Mock()

            def blocked_run(generation: int, _token: object) -> StartupSafetyResult:
                entered.set()
                if not release.wait(2.0):
                    raise RuntimeError("test release event timed out")
                return make_result(generation)

            service.run.side_effect = blocked_run
            with patch("douk_manager.gui.StartupSafetyService", return_value=service):
                window = self._window(root)
                heartbeat_timer = QTimer()
                heartbeat_timer.setInterval(1)
                heartbeat_timer.timeout.connect(lambda: heartbeat.__setitem__(0, heartbeat[0] + 1))
                heartbeat_timer.start()
                QTimer.singleShot(0, window.begin_startup_check)

                self._run_until(lambda: entered.is_set() and heartbeat[0] >= 5)
                window.tabs.setCurrentIndex(1)
                self.assertEqual(window.tabs.currentIndex(), 1)
                self.assertTrue(window.coordinator.has_active_tasks())
                release.set()
                self._run_until(
                    lambda: window.controller.startup_state is StartupState.READY
                    and not window.coordinator.has_active_tasks()
                )
                heartbeat_timer.stop()
                self.assertGreaterEqual(heartbeat[0], 5)
                self._dispose_window(window)

    def test_ready_result_renders_snapshot_and_enables_normal_gate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            self.assertTrue(window.controller.begin_startup_check(1))

            window.apply_startup_result(make_result(1))

            self.assertIs(window.controller.startup_state, StartupState.READY)
            self.assertEqual(window.startup_state_label.text(), "READY")
            self.assertEqual(window.status_labels["volume"].text(), "正常")
            self.assertIn("有效URL 4", window.account_summary.text())
            self.assertTrue(any(widget.isEnabled() for widget in window._dangerous_widgets))
            self._dispose_window(window)

    def test_result_scan_waits_until_startup_result_is_applied(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            window.refresh_results = Mock()

            window.tabs.setCurrentIndex(window.result_tab_index)
            self.app.processEvents()

            window.refresh_results.assert_not_called()
            self.assertTrue(window.controller.begin_startup_check(1))
            window.apply_startup_result(make_result(1))
            self._run_until(lambda: window.refresh_results.called)
            self._dispose_window(window)

    def test_degraded_result_disables_dangerous_controls_but_keeps_path_repair_and_diagnostics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            self.assertTrue(window.controller.begin_startup_check(2))

            window.apply_startup_result(
                make_result(
                    2,
                    success=False,
                    state=StartupState.DEGRADED_READ_ONLY,
                    summary="路径检查失败",
                    details="synthetic details",
                )
            )

            self.assertIs(
                window.controller.startup_state,
                StartupState.DEGRADED_READ_ONLY,
            )
            self.assertTrue(all(not widget.isEnabled() for widget in window._dangerous_widgets))
            self.assertTrue(window.engine_edit.isEnabled())
            self.assertTrue(window.video_edit.isEnabled())
            self.assertTrue(window.path_save_button.isEnabled())
            self.assertFalse(window.setting_batch_accounts.isEnabled())
            self.assertTrue(window.startup_copy_button.isEnabled())
            self.assertTrue(window.startup_log_button.isEnabled())
            self.assertTrue(window.startup_recheck_button.isEnabled())
            buttons = {button.text(): button for button in window.findChildren(QPushButton)}
            for text in (
                "创建并设为正式 settings.json",
                "运行当前 setting",
                "备份并安全安装",
                "手动完整备份 Volume（大文件）",
                "启动采集服务",
                "立即安全归档截图",
                "立即刷新索引",
            ):
                self.assertIn(text, buttons)
                self.assertFalse(buttons[text].isEnabled(), text)
            self._dispose_window(window)

    def test_stale_result_is_ignored_and_recheck_increments_generation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            self.assertTrue(window.controller.begin_startup_check(3))
            window.apply_startup_result(make_result(2))
            self.assertIs(window.controller.startup_state, StartupState.SAFETY_CHECKING)
            self.assertEqual(window.startup_summary_label.text(), "检查中")

            window.coordinator.start = Mock(return_value="startup-task")
            window.controller.startup_state = StartupState.READY
            window.startup_generation = 3
            window.begin_startup_check()

            self.assertEqual(window.startup_generation, 4)
            self.assertIs(window.controller.startup_state, StartupState.SAFETY_CHECKING)
            window.coordinator.start.assert_called_once()
            self._dispose_window(window)

    def test_copy_error_and_open_log_use_diagnostics_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            self.assertTrue(window.controller.begin_startup_check(5))
            window.apply_startup_result(
                make_result(
                    5,
                    success=False,
                    state=StartupState.DEGRADED_READ_ONLY,
                    summary="copy summary",
                    details="copy details",
                )
            )
            window.controller.reconfigure = Mock()
            QApplication.clipboard().clear()

            window.startup_copy_button.click()
            self.assertIn("copy details", QApplication.clipboard().text())
            with patch.object(QDesktopServices, "openUrl", return_value=True) as open_url:
                window.startup_log_button.click()
            open_url.assert_called_once()
            self.assertEqual(
                Path(open_url.call_args.args[0].toLocalFile()),
                window.controller.log_path,
            )
            window.controller.reconfigure.assert_not_called()
            self._dispose_window(window)

    def test_degraded_path_save_submits_only_path_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = self._window(Path(directory))
            window.controller.reconfigure = Mock(return_value="")
            window.refresh_all = Mock()
            window.engine_edit.setText(str(Path(directory) / "replacement.exe"))
            window.video_edit.setText(str(Path(directory) / "videos"))
            window.index_edit.setText(str(Path(directory) / "index"))
            window.old_screenshot_edit.setText(str(Path(directory) / "old"))

            window._save_paths()

            window.controller.reconfigure.assert_called_once_with(
                {
                    "engine_exe": str(Path(directory) / "replacement.exe"),
                    "video_root": str(Path(directory) / "videos"),
                    "index_root": str(Path(directory) / "index"),
                    "old_screenshot_dir": str(Path(directory) / "old"),
                }
            )
            self._dispose_window(window)


if __name__ == "__main__":
    unittest.main()
