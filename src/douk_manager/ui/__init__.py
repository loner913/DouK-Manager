"""Modern UI foundation for DouK Manager.

The package is intentionally isolated from the legacy GUI until each migration
phase is explicitly wired into ``douk_manager.gui``.  Keeping it isolated lets
V0.1.6 behaviour remain the regression baseline while the V0.1.7 shell is built.
"""

from .theme import DARK_THEME, LIGHT_THEME, ThemeManager, ThemeMode

__all__ = ["DARK_THEME", "LIGHT_THEME", "ThemeManager", "ThemeMode"]
