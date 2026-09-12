"""Central design tokens for the modern DouK Manager interface.

All modern widgets should consume these values rather than embedding ad-hoc
colours or spacing constants.  This keeps light and dark themes visually
consistent and makes later tuning low risk.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ThemePalette:
    app_background: str
    surface: str
    surface_alt: str
    surface_hover: str
    border: str
    border_strong: str
    primary: str
    primary_hover: str
    success: str
    info: str
    warning: str
    danger: str
    text_primary: str
    text_secondary: str
    text_muted: str


class UiMetrics:
    """Shared sizing constants for the modern shell."""

    SIDEBAR_WIDTH = 230
    SIDEBAR_COLLAPSED_WIDTH = 64
    HEADER_HEIGHT = 72

    PAGE_MARGIN = 24
    SPACE_XS = 4
    SPACE_S = 8
    SPACE_M = 12
    SPACE_L = 16
    SPACE_XL = 24
    SPACE_XXL = 32

    RADIUS_SMALL = 6
    RADIUS_CONTROL = 8
    RADIUS_CARD = 12

    CONTROL_HEIGHT = 36
    PRIMARY_CONTROL_HEIGHT = 40


LIGHT_THEME = ThemePalette(
    app_background="#F5F8FC",
    surface="#FFFFFF",
    surface_alt="#F8FAFD",
    surface_hover="#F2F6FC",
    border="#E3EAF3",
    border_strong="#CFDAE8",
    primary="#1677FF",
    primary_hover="#0E68E8",
    success="#19C37D",
    info="#20B8E5",
    warning="#FFA940",
    danger="#FF4D5E",
    text_primary="#0F172A",
    text_secondary="#64748B",
    text_muted="#94A3B8",
)

DARK_THEME = ThemePalette(
    app_background="#081321",
    surface="#0E1B2B",
    surface_alt="#122235",
    surface_hover="#172A40",
    border="#22344B",
    border_strong="#31465F",
    primary="#3287FF",
    primary_hover="#4A96FF",
    success="#20D585",
    info="#27C2EB",
    warning="#FFB347",
    danger="#FF5967",
    text_primary="#F4F7FB",
    text_secondary="#90A2BA",
    text_muted="#6F839E",
)
