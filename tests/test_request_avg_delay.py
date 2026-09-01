from __future__ import annotations

import ast
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox

from douk_manager.config import (
    DEFAULT_REQUEST_AVG_DELAY,
    AppConfig,
    update_config,
    validate_request_avg_delay,
)
from douk_manager.controller import ManagerController
from douk_manager.core.backup import BackupService
from douk_manager.core.engine import BATCH_RUN_COMMAND, EngineService
from douk_manager.gui import MainWindow
from douk_manager.startup import StartupState
from tests.helpers import make_test_paths


TOOLTIP = (
    "默认6秒，实际等待按对数正态分布随机变化，最短0.5秒。保存后无需重启管理程序，"
    "从下一次启动下载任务生效，当前任务保持原值。此设置仅调整正常数据请求成功后的等待；"
    "失败重试等待和每批账号暂停规则保持不变。"
)


class ConfigTests(unittest.TestCase):
    def test_missing_field_defaults_to_six(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps({"batch_accounts": 50}), encoding="utf-8")
            self.assertEqual(AppConfig.load(path).request_avg_delay, DEFAULT_REQUEST_AVG_DELAY)

    def test_save_and_reload_supports_six_five_and_four(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            for value in (6, 5, 4):
                config = AppConfig(request_avg_delay=value)
                config.save(path)
                self.assertEqual(AppConfig.load(path).request_avg_delay, float(value))

    def test_invalid_values_are_rejected(self) -> None:
        for value in ("", "abc", "nan", "inf", 0, -1):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_request_avg_delay(value)

    def test_update_rejects_invalid_value_before_save(self) -> None:
        with self.assertRaisesRegex(ValueError, "大于0的有限数字"):
            update_config(AppConfig(), {"request_avg_delay": float("nan")})


class EngineBindingTests(unittest.TestCase):
    def test_legacy_config_without_field_still_starts_with_six(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 1)
            active = json.loads(paths.active_settings.read_text(encoding="utf-8"))
            active["run_command"] = BATCH_RUN_COMMAND
            active["accounts_urls"][0]["enable"] = True
            paths.active_settings.write_text(json.dumps(active), encoding="utf-8")
            backup = BackupService(paths)
            backup.create_critical_snapshot = Mock(return_value=paths.backups / "snapshot")
            config = SimpleNamespace(
                engine_exe=str(paths.engine_exe), batch_accounts=50, rest_seconds=150
            )
            service = EngineService(paths, config, backup)
            service.external_running = Mock(return_value=False)
            process = Mock(pid=1234)
            with patch("douk_manager.core.engine.subprocess.Popen", return_value=process) as popen:
                run = service.start()
            self.assertEqual(popen.call_args.kwargs["env"]["DOUK_REQUEST_AVG_DELAY"], "6.0")
            self.assertEqual(run.request_avg_delay, 6.0)


class HotSaveTests(unittest.TestCase):
    def test_running_task_keeps_old_value_while_next_start_uses_new_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = make_test_paths(Path(directory), 1)
            controller = ManagerController.__new__(ManagerController)
            controller.root = paths.root
            controller.paths = paths
            controller.config = AppConfig(
                engine_exe=str(paths.engine_exe),
                video_root=str(paths.video_root),
                index_root=str(paths.index_root),
                request_avg_delay=6,
            )
            current = Mock(request_avg_delay=6.0)
            controller.engine = Mock(current=current)
            controller.engine.external_running.return_value = True
            controller.logger = Mock()
            controller.startup_state = StartupState.READY
            controller._download_lifecycle_active = True

            values = dict(controller.config.__dict__)
            values["request_avg_delay"] = 5
            message = controller.reconfigure(values)

            self.assertIn("下一次启动下载任务", message)
            self.assertEqual(current.request_avg_delay, 6.0)
            self.assertEqual(controller.config.request_avg_delay, 5.0)
            self.assertIs(controller.engine.config, controller.config)
            self.assertEqual(AppConfig.load(paths.config_file).request_avg_delay, 5.0)


class TooltipContractTests(unittest.TestCase):
    def test_tooltip_contract_is_kept_in_gui_source(self) -> None:
        source = Path(__file__).parents[1] / "src" / "douk_manager" / "gui.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        constants = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
        }
        self.assertIn(TOOLTIP, constants)


class SettingsGuiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_runtime_tooltips_and_invalid_empty_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, {"DOUK_MANAGER_HOME": directory}
        ):
            window = MainWindow()
            try:
                self.assertEqual(window.setting_request_avg_delay.toolTip(), TOOLTIP)
                form = window.setting_request_avg_delay.parentWidget().layout()
                label = form.labelForField(window.setting_request_avg_delay)
                self.assertEqual(label.toolTip(), TOOLTIP)
                window.setting_request_avg_delay.lineEdit().setText("")
                window.controller.reconfigure = Mock()
                with patch.object(QMessageBox, "warning") as warning:
                    window._save_settings()
                window.controller.reconfigure.assert_not_called()
                self.assertIn("大于0的有限数字", warning.call_args.args[2])
            finally:
                window.hide()
                window.deleteLater()
                self.app.processEvents()
                for handler in list(window.controller.logger.handlers):
                    window.controller.logger.removeHandler(handler)
                    handler.close()


if __name__ == "__main__":
    unittest.main()
