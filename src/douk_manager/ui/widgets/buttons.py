"""Button variants shared across modern pages."""

from __future__ import annotations

from PySide6.QtWidgets import QPushButton, QWidget


class _ModernButton(QPushButton):
    role = ""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self.setProperty("modernUi", True)
        self.setProperty("buttonRole", self.role)


class PrimaryButton(_ModernButton):
    role = "primary"


class SecondaryButton(_ModernButton):
    role = "secondary"
