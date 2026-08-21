from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from PySide6.QtCore import QMargins, QSettings, QRect, QSize


UI_STATE_VERSION = 1
UI_STATE_PATH_ENV = "DOUK_MANAGER_UI_STATE_PATH"
DEFAULT_WINDOW_SIZE = QSize(1260, 820)
DEFAULT_WINDOW_FRACTION = 0.9
DEFAULT_MINIMUM_SIZE = QSize(1080, 700)
WINDOW_SCREEN_MARGIN = 16


@dataclass(frozen=True)
class WindowGeometryState:
    normal_geometry: QRect
    maximized: bool = False


@dataclass(frozen=True)
class SafeWindowPlacement:
    geometry: QRect
    minimum_size: QSize
    maximized: bool


class WindowStateStore:
    """Versioned UI-only state, separate from all downloader configuration."""

    def __init__(self, settings: QSettings) -> None:
        self._settings = settings
        self._settings.setAtomicSyncRequired(True)

    @classmethod
    def default(cls) -> "WindowStateStore":
        override = os.environ.get(UI_STATE_PATH_ENV)
        if override:
            path = Path(override).expanduser()
            path.parent.mkdir(parents=True, exist_ok=True)
            return cls(QSettings(os.fspath(path), QSettings.Format.IniFormat))

        isolated_home = os.environ.get("DOUK_MANAGER_HOME")
        if isolated_home:
            path = Path(isolated_home).expanduser() / "Data" / "UI" / "window_state.ini"
            path.parent.mkdir(parents=True, exist_ok=True)
            return cls(QSettings(os.fspath(path), QSettings.Format.IniFormat))

        return cls(
            QSettings(
                QSettings.Format.IniFormat,
                QSettings.Scope.UserScope,
                "loner913",
                "DouK-Manager-UI",
            )
        )

    def load(self) -> WindowGeometryState | None:
        try:
            version = _as_int(self._settings.value("state/version"))
            if version != UI_STATE_VERSION:
                return None
            x = _as_int(self._settings.value("main_window/x"))
            y = _as_int(self._settings.value("main_window/y"))
            width = _as_int(self._settings.value("main_window/width"))
            height = _as_int(self._settings.value("main_window/height"))
            maximized = _as_bool(self._settings.value("main_window/maximized", False))
            if None in (x, y, width, height) or maximized is None:
                return None
            if width <= 0 or height <= 0:
                return None
            return WindowGeometryState(QRect(x, y, width, height), maximized)
        except (OverflowError, TypeError, ValueError):
            return None

    def save(self, state: WindowGeometryState) -> bool:
        geometry = state.normal_geometry
        if geometry.width() <= 0 or geometry.height() <= 0:
            return False
        self._settings.setValue("state/version", UI_STATE_VERSION)
        self._settings.setValue("main_window/x", geometry.x())
        self._settings.setValue("main_window/y", geometry.y())
        self._settings.setValue("main_window/width", geometry.width())
        self._settings.setValue("main_window/height", geometry.height())
        self._settings.setValue("main_window/maximized", state.maximized)
        self._settings.sync()
        return self._settings.status() == QSettings.Status.NoError


def safe_window_placement(
    state: WindowGeometryState | None,
    available_geometries: Iterable[QRect],
    *,
    primary_index: int = 0,
    frame_margins: QMargins | None = None,
) -> SafeWindowPlacement:
    screens = tuple(
        QRect(rect)
        for rect in available_geometries
        if rect.width() > 0 and rect.height() > 0
    )
    if not screens:
        screens = (QRect(0, 0, DEFAULT_WINDOW_SIZE.width(), DEFAULT_WINDOW_SIZE.height()),)
    primary_index = min(max(0, primary_index), len(screens) - 1)
    candidate = state.normal_geometry if state is not None else None
    target, has_intersection = _target_screen(candidate, screens, primary_index)
    working_target = _inset_screen(target)
    margins = frame_margins or QMargins()
    frame_left = max(0, margins.left())
    frame_top = max(0, margins.top())
    frame_right = max(0, margins.right())
    frame_bottom = max(0, margins.bottom())
    client_area_width = max(1, working_target.width() - frame_left - frame_right)
    client_area_height = max(1, working_target.height() - frame_top - frame_bottom)

    minimum = QSize(
        min(DEFAULT_MINIMUM_SIZE.width(), client_area_width),
        min(DEFAULT_MINIMUM_SIZE.height(), client_area_height),
    )
    if candidate is None:
        requested_width = round(working_target.width() * DEFAULT_WINDOW_FRACTION)
        requested_height = round(working_target.height() * DEFAULT_WINDOW_FRACTION)
    else:
        requested_width = candidate.width()
        requested_height = candidate.height()
    width = min(max(requested_width, minimum.width()), client_area_width)
    height = min(max(requested_height, minimum.height()), client_area_height)

    client_left = working_target.left() + frame_left
    client_top = working_target.top() + frame_top
    client_right = working_target.right() - frame_right
    client_bottom = working_target.bottom() - frame_bottom

    if candidate is None or not has_intersection:
        x = client_left + (client_area_width - width) // 2
        y = client_top + (client_area_height - height) // 2
    else:
        x = min(
            max(candidate.x(), client_left),
            client_right - width + 1,
        )
        y = min(
            max(candidate.y(), client_top),
            client_bottom - height + 1,
        )

    return SafeWindowPlacement(
        QRect(x, y, width, height),
        minimum,
        state.maximized if state is not None else False,
    )


def _target_screen(
    candidate: QRect | None, screens: tuple[QRect, ...], primary_index: int
) -> tuple[QRect, bool]:
    if candidate is None:
        return screens[primary_index], False
    intersections = tuple(candidate.intersected(screen) for screen in screens)
    areas = tuple(rect.width() * rect.height() if not rect.isEmpty() else 0 for rect in intersections)
    largest = max(areas, default=0)
    if largest <= 0:
        return screens[primary_index], False
    return screens[areas.index(largest)], True


def _inset_screen(screen: QRect) -> QRect:
    if (
        screen.width() <= WINDOW_SCREEN_MARGIN * 2
        or screen.height() <= WINDOW_SCREEN_MARGIN * 2
    ):
        return QRect(screen)
    return screen.adjusted(
        WINDOW_SCREEN_MARGIN,
        WINDOW_SCREEN_MARGIN,
        -WINDOW_SCREEN_MARGIN,
        -WINDOW_SCREEN_MARGIN,
    )


def _as_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        return int(value.strip())
    return None


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    return None
