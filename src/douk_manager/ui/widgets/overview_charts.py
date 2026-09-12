"""Small dependency-free charts for the modern overview.

These widgets are presentation-only. Callers push already-derived values into
these views; the widgets never open logs, query databases, or start services.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

from ..theme.tokens import LIGHT_THEME


class TaskTrendChart(QWidget):
    """Seven-day task-count trend drawn from real task-index timestamps."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(178)
        self._points: tuple[tuple[str, int], ...] = ()

    def set_points(self, points: tuple[tuple[str, int], ...]) -> None:
        self._points = points
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(12, 14, -12, -24)
        if rect.width() <= 0 or rect.height() <= 0:
            return

        grid_pen = QPen(QColor("#E9EFF7"), 1)
        painter.setPen(grid_pen)
        for row in range(5):
            y = rect.top() + rect.height() * row / 4
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))

        if not self._points or max((value for _, value in self._points), default=0) <= 0:
            painter.setPen(QColor(LIGHT_THEME.text_muted))
            painter.drawText(
                rect,
                Qt.AlignmentFlag.AlignCenter,
                "最近 7 天暂无已完成任务",
            )
            return

        maximum = max(value for _, value in self._points)
        usable_width = max(1.0, float(rect.width()))
        usable_height = max(1.0, float(rect.height()))
        count = len(self._points)
        coords: list[QPointF] = []
        for index, (_label, value) in enumerate(self._points):
            x = rect.left() + usable_width * index / max(1, count - 1)
            y = rect.bottom() - usable_height * value / maximum
            coords.append(QPointF(x, y))

        line_pen = QPen(QColor(LIGHT_THEME.primary), 2.4)
        line_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        line_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(line_pen)
        for index in range(1, len(coords)):
            painter.drawLine(coords[index - 1], coords[index])

        painter.setBrush(QColor("#FFFFFF"))
        painter.setPen(QPen(QColor(LIGHT_THEME.primary), 2))
        for point in coords:
            painter.drawEllipse(point, 4, 4)

        painter.setPen(QColor(LIGHT_THEME.text_muted))
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        label_y = self.rect().bottom() - 5
        for index, (label, _value) in enumerate(self._points):
            x = rect.left() + usable_width * index / max(1, count - 1)
            painter.drawText(
                QRectF(x - 24, label_y - 14, 48, 14),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )


class DonutChart(QWidget):
    """Compact composition chart for real result-dashboard status counts."""

    _COLOURS = (
        "#20C997",
        "#4C8DFF",
        "#FF5C68",
        "#FFB547",
        "#8B5CF6",
        "#27C2EB",
        "#94A3B8",
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(176, 176)
        self._segments: tuple[tuple[str, int], ...] = ()

    def set_segments(self, segments: tuple[tuple[str, int], ...]) -> None:
        self._segments = tuple((label, max(0, int(value))) for label, value in segments)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        size = min(self.width(), self.height()) - 28
        if size <= 0:
            return
        ring = QRectF(
            (self.width() - size) / 2,
            (self.height() - size) / 2,
            size,
            size,
        )
        total = sum(value for _, value in self._segments)
        painter.setPen(Qt.PenStyle.NoPen)
        if total <= 0:
            painter.setBrush(QColor("#E8EEF6"))
            painter.drawEllipse(ring)
        else:
            start = 90 * 16
            for index, (_label, value) in enumerate(self._segments):
                if value <= 0:
                    continue
                span = -int(round(360 * 16 * value / total))
                painter.setBrush(QColor(self._COLOURS[index % len(self._COLOURS)]))
                painter.drawPie(ring, start, span)
                start += span

        inner = ring.adjusted(size * 0.22, size * 0.22, -size * 0.22, -size * 0.22)
        painter.setBrush(QColor("#FFFFFF"))
        painter.drawEllipse(inner)
        painter.setPen(QColor(LIGHT_THEME.text_secondary))
        font = QFont(painter.font())
        font.setPointSize(8)
        painter.setFont(font)
        painter.drawText(
            inner.adjusted(0, -16, 0, -2),
            Qt.AlignmentFlag.AlignCenter,
            "总计",
        )
        painter.setPen(QColor(LIGHT_THEME.text_primary))
        font.setPointSize(17)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            inner.adjusted(0, 5, 0, 10),
            Qt.AlignmentFlag.AlignCenter,
            str(total) if total else "—",
        )
