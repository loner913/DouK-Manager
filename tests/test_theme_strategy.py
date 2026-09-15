import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from PySide6.QtCore import QObject, QSettings, Qt, Signal
from douk_manager.ui_state import WindowStateStore
from douk_manager.ui.theme.theme_manager import ThemeManager, ThemeMode


class Hints(QObject):
    colorSchemeChanged = Signal(object)
    scheme = Qt.ColorScheme.Unknown

    def colorScheme(self):
        return self.scheme

    def change(self, scheme):
        self.scheme = scheme
        self.colorSchemeChanged.emit(scheme)


class ThemeStrategyTests(unittest.TestCase):
    def test_system_fixed_toggle_and_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "ui.ini")
            store = WindowStateStore(QSettings(path, QSettings.IniFormat))
            hints = Hints()
            theme = ThemeManager(store=store, style_hints=hints)
            self.assertEqual(theme.strategy, "system")
            self.assertEqual(theme.mode, ThemeMode.LIGHT)
            hints.change(Qt.ColorScheme.Dark)
            self.assertEqual(theme.mode, ThemeMode.DARK)
            self.assertEqual(store.load_theme_strategy(), "system")
            theme.toggle()
            self.assertEqual(theme.strategy, "light")
            hints.change(Qt.ColorScheme.Dark)
            self.assertEqual(theme.mode, ThemeMode.LIGHT)
            restarted = ThemeManager(store=WindowStateStore(QSettings(path, QSettings.IniFormat)), style_hints=hints)
            self.assertEqual(restarted.strategy, "light")
            restarted.set_strategy("system")
            self.assertEqual(restarted.mode, ThemeMode.DARK)

    def test_save_failure_is_reported_and_unknown_setting_falls_back(self):
        store = Mock()
        store.load_theme_strategy.return_value = "light"
        store.save_theme_strategy.return_value = False
        theme = ThemeManager(store=store)
        errors = []
        theme.save_failed.connect(lambda: errors.append(True))
        theme.set_strategy("dark")
        self.assertEqual(errors, [True])
        self.assertEqual(theme.mode, ThemeMode.DARK)
        with tempfile.TemporaryDirectory() as directory:
            settings = QSettings(str(Path(directory) / "ui.ini"), QSettings.IniFormat)
            settings.setValue("ui/theme/strategy", "invalid")
            self.assertEqual(WindowStateStore(settings).load_theme_strategy(), "system")

    def test_dark_table_corner_uses_the_scoped_header_surface(self):
        stylesheet = ThemeManager(ThemeMode.DARK).stylesheet()

        self.assertIn(
            'QWidget[legacyRoot="true"] QTableCornerButton::section',
            stylesheet,
        )
        corner_rule = stylesheet.split("QTableCornerButton::section", 1)[1].split(
            "}", 1
        )[0]
        self.assertIn("background: #122235;", corner_rule)
