"""Small dependency-free charts for the modern overview."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from ..theme.tokens import LIGHT_THEME, DARK_THEME


def _chart_theme(widget):
    return DARK_THEME if widget.property("darkTheme") else LIGHT_THEME


class TaskTrendChart(QWidget):
    """Seven-day task-count trend drawn from runtime or preview data."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(224)
        self._points: tuple[tuple[str, int], ...] = ()
        self._series: tuple[tuple[str, tuple[int, ...], str], ...] = ()
        self.setMouseTracking(True)

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._points and self._series:
            width = max(1, self.width() - 84)
            index = round((event.position().x() - 48) * (len(self._points) - 1) / width)
            index = max(0, min(len(self._points) - 1, index))
            self.setToolTip(self._points[index][0] + "\n" + "\n".join(
                f"{name}：{values[index]:,}" for name, values, _ in self._series if index < len(values)))
        super().mouseMoveEvent(event)

    def set_points(self, points: tuple[tuple[str, int], ...]) -> None:
        self._points = points
        self._series = (("已完成任务", tuple(value for _, value in points), "#19C37D"),)
        self.update()

    def set_series(
        self,
        labels: tuple[str, ...],
        series: tuple[tuple[str, tuple[int, ...], str], ...],
    ) -> None:
        """Set multiple named lines while keeping the one-line API above."""

        self._points = tuple((label, 0) for label in labels)
        self._series = tuple(series)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(48, 30, -36, -42)
        if rect.width() <= 0 or rect.height() <= 0:
            return

        painter.setPen(QPen(QColor(_chart_theme(self).border), 1))
        for row in range(5):
            y = rect.top() + rect.height() * row / 4
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))

        if not self._series:
            painter.setPen(QColor(_chart_theme(self).text_secondary))
            font = painter.font()
            font.setPointSize(10)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, self.property("emptyMessage") or "最近 7 天暂无已完成任务")
            return

        labels = tuple(label for label, _value in self._points)
        maximum = max(
            (value for _name, values, _colour in self._series for value in values),
            default=0,
        )
        if maximum <= 0:
            painter.setPen(QColor(_chart_theme(self).text_secondary))
            font = painter.font()
            font.setPointSize(10)
            painter.setFont(font)
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "最近 7 天暂无已完成任务")
            return
        usable_width = max(1.0, float(rect.width()))
        usable_height = max(1.0, float(rect.height()))
        count = len(labels)
        point_labels = []
        for series_index, (_name, values, colour) in enumerate(self._series):
            coords: list[QPointF] = []
            for index in range(count):
                value = values[index] if index < len(values) else 0
                x = rect.left() + usable_width * index / max(1, count - 1)
                y = rect.bottom() - usable_height * value / maximum
                coords.append(QPointF(x, y))
            if not coords:
                continue

            path = QPainterPath(coords[0])
            for point in coords[1:]:
                path.lineTo(point)
            if series_index < 2:
                fill = QPainterPath(path)
                fill.lineTo(coords[-1].x(), rect.bottom())
                fill.lineTo(coords[0].x(), rect.bottom())
                fill.closeSubpath()
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(QColor(colour + "22"))
                painter.drawPath(fill)

            line_pen = QPen(QColor(colour), 2.5 if series_index else 2.8)
            line_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            line_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(line_pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(path)

            painter.setBrush(QColor(_chart_theme(self).surface))
            painter.setPen(QPen(QColor(colour), 2.0))
            for point in coords:
                painter.drawEllipse(point, 3.8, 3.8)
            point_labels.extend((point, str(value), colour) for point, value in zip(coords, values))

        font = QFont(painter.font())
        font.setPixelSize(13)
        font.setBold(True)
        painter.setFont(font)
        occupied = []
        self._value_label_rects = []
        for point, value, colour in point_labels:
            width = painter.fontMetrics().horizontalAdvance(value) + 8
            x = max(1, min(self.width() - width - 1, point.x() - width / 2))
            box = QRectF(x, max(1, point.y() - 24), width, 20)
            for offset in (-24, 8, -45, 29, -66, 50):
                candidate = QRectF(x, max(1, min(rect.bottom() - 22, point.y() + offset)), width, 20)
                if not any(candidate.intersects(other) for other in occupied):
                    box = candidate
                    break
            occupied.append(box)
            self._value_label_rects.append(box)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(_chart_theme(self).surface))
            painter.drawRoundedRect(box, 3, 3)
            painter.setPen(QColor(_chart_theme(self).text_primary))
            painter.drawText(box, Qt.AlignmentFlag.AlignCenter, value)

        painter.setPen(QColor(_chart_theme(self).text_secondary))
        font = painter.font()
        font.setPointSize(9)
        painter.setFont(font)
        label_y = self.rect().bottom() - 7
        for index, label in enumerate(labels):
            x = rect.left() + usable_width * index / max(1, count - 1)
            painter.drawText(
                QRectF(x - 30, label_y - 18, 60, 18),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )

        for row in range(5):
            value = round(maximum * (4 - row) / 4)
            painter.drawText(
                QRectF(0, rect.top() + rect.height() * row / 4 - 9, 36, 18),
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                str(value),
            )


class DonutChart(QWidget):
    """Composition chart for real result-dashboard status counts."""

    _COLOURS = (
        "#20C997",
        "#FF5C68",
        "#4C8DFF",
        "#FFB547",
        "#8B5CF6",
        "#27C2EB",
        "#94A3B8",
    )

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumSize(188, 208)
        self._segments: tuple[tuple[str, int], ...] = ()

    def set_segments(self, segments: tuple[tuple[str, int], ...]) -> None:
        self._segments = tuple((label, max(0, int(value))) for label, value in segments)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        size = min(self.width(), self.height()) - 32
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
            painter.setBrush(QColor(_chart_theme(self).border))
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
        painter.setBrush(QColor(_chart_theme(self).surface))
        painter.drawEllipse(inner)
        painter.setPen(QColor(_chart_theme(self).text_secondary))
        font = QFont(painter.font())
        font.setPointSize(9)
        painter.setFont(font)
        painter.drawText(inner.adjusted(0, -18, 0, -2), Qt.AlignmentFlag.AlignCenter, "总计")
        painter.setPen(QColor(_chart_theme(self).text_primary))
        font.setPointSize(19)
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(
            inner.adjusted(0, 6, 0, 12),
            Qt.AlignmentFlag.AlignCenter,
            str(total) if total else "—",
        )


class HourlyThroughputChart(QWidget):
    """Compact hourly bar chart used by the populated overview preview."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setMinimumHeight(170)
        self._values: tuple[int, ...] = ()

    def set_values(self, values: tuple[int, ...]) -> None:
        self._values = tuple(max(0, int(value)) for value in values)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802 - Qt API
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        rect = self.rect().adjusted(28, 12, -12, -32)
        if rect.width() <= 0 or rect.height() <= 0:
            return
        maximum = max(self._values, default=0)
        if maximum <= 0:
            painter.setPen(QColor(_chart_theme(self).text_secondary))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, "暂无小时处理数据")
            return

        painter.setPen(QPen(QColor(_chart_theme(self).border), 1))
        for row in range(4):
            y = rect.top() + rect.height() * row / 3
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))

        count = len(self._values)
        slot = rect.width() / max(1, count)
        bar_width = max(3.0, slot * 0.56)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(LIGHT_THEME.primary))
        for index, value in enumerate(self._values):
            height = rect.height() * value / maximum
            x = rect.left() + slot * index + (slot - bar_width) / 2
            y = rect.bottom() - height
            painter.drawRoundedRect(QRectF(x, y, bar_width, height), 2, 2)

        painter.setPen(QColor(_chart_theme(self).text_secondary))
        for index, label in ((0, "00:00"), (6, "06:00"), (12, "12:00"), (18, "18:00")):
            if index >= count:
                continue
            x = rect.left() + slot * index
            painter.drawText(
                QRectF(x - 24, rect.bottom() + 8, 48, 18),
                Qt.AlignmentFlag.AlignCenter,
                label,
            )


__all__ = ["DonutChart", "HourlyThroughputChart", "TaskTrendChart"]
