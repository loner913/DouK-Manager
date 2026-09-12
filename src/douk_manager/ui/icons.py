"""Self-contained vector icons for the modern DouK Manager UI.

The icon set is drawn with QPainter primitives instead of font glyphs. This keeps
Windows rendering stable across Segoe UI / Microsoft YaHei installations and DPI
settings, while avoiding a runtime dependency on external icon files.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap


def _pixmap(name: str, color: str, size: int) -> QPixmap:
    dpr = 2.0
    px = max(1, int(size * dpr))
    canvas = QPixmap(px, px)
    canvas.setDevicePixelRatio(dpr)
    canvas.fill(Qt.GlobalColor.transparent)

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.scale(size / 24.0, size / 24.0)
    pen = QPen(QColor(color), 1.9)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    def line(x1: float, y1: float, x2: float, y2: float) -> None:
        painter.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    if name == "home":
        path = QPainterPath(QPointF(3.5, 10.5))
        path.lineTo(12, 3.5)
        path.lineTo(20.5, 10.5)
        painter.drawPath(path)
        painter.drawRoundedRect(QRectF(6.2, 9.5, 11.6, 10.2), 1.5, 1.5)
        line(10, 19.7, 10, 14.2)
        line(14, 14.2, 14, 19.7)
    elif name == "clipboard":
        painter.drawRoundedRect(QRectF(5.5, 5, 13, 15.5), 2, 2)
        painter.drawRoundedRect(QRectF(8.5, 2.8, 7, 4.2), 1.4, 1.4)
        line(8.5, 11, 15.5, 11)
        line(8.5, 15, 15.5, 15)
    elif name == "shield":
        path = QPainterPath(QPointF(12, 3))
        path.lineTo(19, 6)
        path.lineTo(18, 13.2)
        path.cubicTo(17.5, 16.6, 15.2, 19, 12, 21)
        path.cubicTo(8.8, 19, 6.5, 16.6, 6, 13.2)
        path.lineTo(5, 6)
        path.closeSubpath()
        painter.drawPath(path)
        line(9.2, 12, 11.2, 14)
        line(11.2, 14, 15.2, 9.6)
    elif name == "plus-square":
        painter.drawRoundedRect(QRectF(4, 4, 16, 16), 3, 3)
        line(12, 8.5, 12, 15.5)
        line(8.5, 12, 15.5, 12)
    elif name == "download":
        line(12, 3.5, 12, 14.5)
        line(8, 10.5, 12, 14.5)
        line(16, 10.5, 12, 14.5)
        painter.drawRoundedRect(QRectF(4.5, 17, 15, 3.5), 1.2, 1.2)
    elif name == "users":
        painter.drawEllipse(QRectF(8.5, 4, 7, 7))
        painter.drawArc(QRectF(5.2, 10.5, 13.6, 10), 15 * 16, 150 * 16)
        painter.drawArc(QRectF(2.6, 7.5, 7.2, 7.2), 45 * 16, 105 * 16)
        painter.drawArc(QRectF(14.2, 7.5, 7.2, 7.2), 30 * 16, 105 * 16)
    elif name == "image":
        painter.drawRoundedRect(QRectF(3.5, 5, 17, 14), 2.2, 2.2)
        painter.drawEllipse(QRectF(7, 8, 2.8, 2.8))
        path = QPainterPath(QPointF(5.2, 17))
        path.lineTo(10.2, 12)
        path.lineTo(13.2, 15)
        path.lineTo(16, 12.2)
        path.lineTo(19, 15.2)
        painter.drawPath(path)
    elif name == "folder":
        path = QPainterPath(QPointF(3.5, 7.2))
        path.lineTo(9.2, 7.2)
        path.lineTo(11.2, 9.2)
        path.lineTo(20.5, 9.2)
        path.lineTo(20.5, 18.8)
        path.lineTo(3.5, 18.8)
        path.closeSubpath()
        painter.drawPath(path)
    elif name == "chart":
        painter.drawEllipse(QRectF(4, 4, 16, 16))
        line(12, 4, 12, 12)
        line(12, 12, 18.8, 16)
        line(12, 12, 7.3, 18.5)
    elif name == "settings":
        painter.drawEllipse(QRectF(8.2, 8.2, 7.6, 7.6))
        for x1, y1, x2, y2 in (
            (12, 2.8, 12, 6.2), (12, 17.8, 12, 21.2),
            (2.8, 12, 6.2, 12), (17.8, 12, 21.2, 12),
            (5.5, 5.5, 7.8, 7.8), (16.2, 16.2, 18.5, 18.5),
            (18.5, 5.5, 16.2, 7.8), (7.8, 16.2, 5.5, 18.5),
        ):
            line(x1, y1, x2, y2)
    elif name == "search":
        painter.drawEllipse(QRectF(4.5, 4.5, 10.5, 10.5))
        line(14, 14, 20, 20)
    elif name == "play":
        path = QPainterPath(QPointF(8.2, 5.2))
        path.lineTo(18.2, 12)
        path.lineTo(8.2, 18.8)
        path.closeSubpath()
        painter.setBrush(QColor(color))
        painter.drawPath(path)
    elif name == "check":
        line(5.2, 12.5, 9.7, 17)
        line(9.7, 17, 19, 7)
    elif name == "warning":
        path = QPainterPath(QPointF(12, 3.5))
        path.lineTo(21, 19.2)
        path.lineTo(3, 19.2)
        path.closeSubpath()
        painter.drawPath(path)
        line(12, 8, 12, 13.5)
        painter.drawPoint(QPointF(12, 16.5))
    elif name == "clock":
        painter.drawEllipse(QRectF(4, 4, 16, 16))
        line(12, 7.2, 12, 12)
        line(12, 12, 15.5, 14)
    else:
        painter.drawEllipse(QRectF(8.5, 8.5, 7, 7))

    painter.end()
    return canvas


def vector_icon(
    name: str,
    *,
    color: str = "#60738E",
    checked_color: str | None = None,
    size: int = 20,
) -> QIcon:
    """Return a crisp checkable icon suitable for QPushButton/QAction usage."""

    icon = QIcon()
    icon.addPixmap(_pixmap(name, color, size), QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(
        _pixmap(name, checked_color or color, size),
        QIcon.Mode.Normal,
        QIcon.State.On,
    )
    return icon


def icon_pixmap(name: str, color: str, size: int) -> QPixmap:
    return _pixmap(name, color, size)


__all__ = ["vector_icon", "icon_pixmap"]
