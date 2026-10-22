from __future__ import annotations

import math
from typing import Callable, List, Sequence, Tuple

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QStyle, QToolButton, QWidget

from .models import MeasurementSample


def _nice_number(value: float, round_result: bool) -> float:
    if value <= 0 or not math.isfinite(value):
        return 1.0
    exponent = math.floor(math.log10(value))
    fraction = value / (10 ** exponent)
    if round_result:
        if fraction < 1.5:
            nice_fraction = 1.0
        elif fraction < 3.0:
            nice_fraction = 2.0
        elif fraction < 7.0:
            nice_fraction = 5.0
        else:
            nice_fraction = 10.0
    else:
        if fraction <= 1.0:
            nice_fraction = 1.0
        elif fraction <= 2.0:
            nice_fraction = 2.0
        elif fraction <= 5.0:
            nice_fraction = 5.0
        else:
            nice_fraction = 10.0
    return nice_fraction * (10 ** exponent)


def axis_ticks(
    values: Sequence[float],
    target_count: int = 6,
    include_zero: bool = True,
) -> tuple[float, float, List[float]]:
    finite = [float(value) for value in values if math.isfinite(value)]
    if not finite:
        return -1.0, 1.0, [-1.0, -0.5, 0.0, 0.5, 1.0]

    minimum = min(finite)
    maximum = max(finite)
    if include_zero:
        minimum = min(minimum, 0.0)
        maximum = max(maximum, 0.0)

    if math.isclose(minimum, maximum, rel_tol=0.0, abs_tol=1e-15):
        scale = max(abs(minimum), 1.0)
        minimum -= scale * 0.5
        maximum += scale * 0.5

    span = _nice_number(maximum - minimum, round_result=False)
    step = max(_nice_number(span / max(2, target_count - 1), round_result=True),
               math.ulp(minimum), math.ulp(maximum))
    axis_min = math.floor(minimum / step) * step
    axis_max = math.ceil(maximum / step) * step
    if include_zero:
        axis_min = min(axis_min, 0.0)
        axis_max = max(axis_max, 0.0)

    ticks: List[float] = []
    current = axis_min
    while current <= axis_max + step * 1e-9 and len(ticks) < 20:
        ticks.append(0.0 if abs(current) < step * 1e-10 else current)
        current += step

    if 0.0 not in ticks and axis_min <= 0.0 <= axis_max:
        ticks.append(0.0)
        ticks.sort()
    return axis_min, axis_max, ticks


def range_ticks(minimum: float, maximum: float, target_count: int = 6) -> List[float]:
    if not all(math.isfinite(value) for value in (minimum, maximum)) or maximum <= minimum:
        return []
    span = maximum - minimum
    step = max(_nice_number(span / max(2, target_count - 1), round_result=True),
               math.ulp(minimum), math.ulp(maximum))
    first = math.ceil(minimum / step) * step
    ticks: List[float] = []
    for index in range(20):
        current = first + index * step
        if current > maximum + step * 1e-9:
            break
        if not ticks or current > ticks[-1]:
            ticks.append(0.0 if abs(current) < step * 1e-10 else current)
    return ticks


def _format_tick(value: float, scientific: bool = True, step: float | None = None) -> str:
    """Match precision to tick spacing instead of rounding zoomed axes to 0.1."""
    if not math.isfinite(value):
        return "--"
    valid_step = step is not None and math.isfinite(step) and step > 0
    if valid_step and abs(value) < step * 1e-9:
        value = 0.0
    if value == 0:
        value = 0.0  # Suppress negative zero.
    magnitude = abs(value)
    step_exponent = math.floor(math.log10(step) + 1e-9) if valid_step else -6
    decimals = max(1, -step_exponent)
    if magnitude and ((scientific and (magnitude >= 10000 or magnitude < 0.1)) or decimals > 12):
        exponent = math.floor(math.log10(magnitude))
        digits = min(15, max(1, exponent - step_exponent)) if valid_step else 6
        return f"{value:.{digits}e}"
    return f"{value:.{min(12, decimals)}f}"


def _tick_labels(ticks: Sequence[float], scientific: bool = True) -> list[str]:
    steps = [b - a for a, b in zip(ticks, ticks[1:]) if b > a]
    step = min(steps) if steps else None
    labels = [_format_tick(value, scientific, step) for value in ticks]
    # At the floating-point resolution limit, use enough digits to distinguish
    # every representable tick instead of showing several identical labels.
    if len(set(labels)) != len(labels):
        labels = [format(value, ".17g") for value in ticks]
    return labels


def _draw_x_tick_labels(painter: QPainter, ticks, labels, map_x, plot, outer) -> None:
    """Use measured label widths and omit overlaps on narrow/zoomed time axes."""
    last_right = -float("inf")
    metrics = painter.fontMetrics()
    for tick, label in zip(ticks, labels):
        width = metrics.horizontalAdvance(label) + 8
        left = max(outer.left() + 4, min(map_x(tick) - width / 2, outer.right() - width - 4))
        if left < last_right + 6:
            continue
        painter.drawText(int(left), int(plot.bottom() + 5), width, 20,
                         Qt.AlignmentFlag.AlignHCenter, label)
        last_right = left + width


def _zoom_range(minimum: float, maximum: float, anchor: float, factor: float) -> tuple[float, float]:
    factor = min(5.0, max(0.05, factor))
    return anchor - (anchor - minimum) * factor, anchor + (maximum - anchor) * factor


class _InteractivePlot(QWidget):
    """Base plot with drag-to-pan, selectable wheel zoom, and reset controls."""

    ZOOM_MODES = ("XY", "X", "Y")

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(260)
        self.setMinimumWidth(430)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMouseTracking(True)
        self._drag_start: QPoint | None = None
        self._drag_ranges = None
        self._last_plot_rect = None
        self._last_ranges = None
        self._zoom_mode_index = 0

        self.reset_button = QToolButton(self)
        self.reset_button.setObjectName("plotResetButton")
        self.reset_button.setAutoRaise(True)
        self.reset_button.setToolTip("Reset plot view")
        self.reset_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.reset_button.setFixedSize(26, 26)
        self.reset_button.clicked.connect(self.reset_view)

        self.zoom_mode_button = QToolButton(self)
        self.zoom_mode_button.setObjectName("plotResetButton")
        self.zoom_mode_button.setAutoRaise(True)
        self.zoom_mode_button.setFixedSize(38, 26)
        self.zoom_mode_button.clicked.connect(self.cycle_zoom_mode)
        self._update_zoom_button()

        self.reset_button.raise_()
        self.zoom_mode_button.raise_()

    @property
    def zoom_mode(self) -> str:
        return self.ZOOM_MODES[self._zoom_mode_index]

    def cycle_zoom_mode(self) -> None:
        self._zoom_mode_index = (self._zoom_mode_index + 1) % len(self.ZOOM_MODES)
        self._update_zoom_button()

    def _update_zoom_button(self) -> None:
        mode = self.zoom_mode
        self.zoom_mode_button.setText(mode)
        descriptions = {
            "XY": "Mouse wheel zooms horizontal and vertical axes",
            "X": "Mouse wheel zooms the horizontal axis only",
            "Y": "Mouse wheel zooms the vertical axis only",
        }
        self.zoom_mode_button.setToolTip(descriptions[mode])

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        y = max(8, self.height() - 38)
        self.reset_button.move(16, y)
        self.zoom_mode_button.move(48, y)
        self.reset_button.raise_()
        self.zoom_mode_button.raise_()

    def reset_view(self) -> None:
        raise NotImplementedError

    def _can_interact(self, position: QPointF) -> bool:
        return self._last_plot_rect is not None and self._last_plot_rect.contains(position.toPoint())

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self._can_interact(event.position()):
            self._drag_start = event.position().toPoint()
            self._drag_ranges = self._last_ranges
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:
        if self._drag_start is not None and event.button() == Qt.MouseButton.LeftButton:
            self._drag_start = None
            self._drag_ranges = None
            self.unsetCursor()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class LinePlot(_InteractivePlot):
    def __init__(
        self,
        title: str,
        x_label: str,
        y_label: str,
        selector: Callable[[MeasurementSample], Tuple[float, float]],
        reverse_y: bool = False,
        scientific_ticks: bool = True,
        primary_label: str = "Result 1",
        secondary_label: str = "Result 2",
    ):
        super().__init__()
        self.title = title
        self.x_label = x_label
        self.y_label = y_label
        self.selector = selector
        self.reverse_y = reverse_y
        self.scientific_ticks = scientific_ticks
        self.primary_label = primary_label
        self.secondary_label = secondary_label
        self.points: List[Tuple[float, float]] = []
        self.secondary_points: List[Tuple[float, float]] = []
        self._view_x: tuple[float, float] | None = None
        self._view_y: tuple[float, float] | None = None

    def add_sample(self, sample: MeasurementSample) -> None:
        x, y = self.selector(sample)
        if math.isfinite(x) and math.isfinite(y):
            self.points.append((x, y))
            if len(self.points) > 3000:
                self.points = self.points[-3000:]
            self.update()

    @staticmethod
    def _clean_points(points) -> List[Tuple[float, float]]:
        cleaned: List[Tuple[float, float]] = []
        for x, y in points:
            x_value = float(x)
            y_value = float(y)
            if math.isfinite(x_value) and math.isfinite(y_value):
                cleaned.append((x_value, y_value))
        return cleaned

    def set_points(self, points) -> None:
        self.points = self._clean_points(points)
        self.reset_view()

    def set_secondary_points(self, points, label: str = "Result 2") -> None:
        self.secondary_points = self._clean_points(points)
        self.secondary_label = label
        self.reset_view()

    def clear_secondary_points(self) -> None:
        self.secondary_points.clear()
        self.reset_view()

    def clear(self) -> None:
        self.points.clear()
        self.secondary_points.clear()
        self.reset_view()

    def reset_view(self) -> None:
        self._view_x = None
        self._view_y = None
        self.update()

    def _data_ranges(self) -> tuple[tuple[float, float], tuple[float, float]]:
        all_points = self.points + self.secondary_points
        xs = [point[0] for point in all_points] or [0.0, 1.0]
        ys = [point[1] for point in all_points] or [0.0, 1.0]
        xmin, xmax, _ = axis_ticks(xs, target_count=6, include_zero=True)
        ymin, ymax, _ = axis_ticks(ys, target_count=6, include_zero=True)
        return (xmin, xmax), (ymin, ymax)

    def _ranges(self) -> tuple[float, float, float, float]:
        data_x, data_y = self._data_ranges()
        xmin, xmax = self._view_x or data_x
        ymin, ymax = self._view_y or data_y
        return xmin, xmax, ymin, ymax

    def mouseMoveEvent(self, event) -> None:
        if self._drag_start is None or self._drag_ranges is None or self._last_plot_rect is None:
            super().mouseMoveEvent(event)
            return
        xmin, xmax, ymin, ymax = self._drag_ranges
        dx = event.position().x() - self._drag_start.x()
        dy = event.position().y() - self._drag_start.y()
        x_shift = -dx / max(1.0, self._last_plot_rect.width()) * (xmax - xmin)
        y_sign = -1.0 if self.reverse_y else 1.0
        y_shift = y_sign * dy / max(1.0, self._last_plot_rect.height()) * (ymax - ymin)
        self._view_x = (xmin + x_shift, xmax + x_shift)
        self._view_y = (ymin + y_shift, ymax + y_shift)
        self.update()
        event.accept()

    def wheelEvent(self, event) -> None:
        if not self._can_interact(event.position()) or self._last_ranges is None or self._last_plot_rect is None:
            super().wheelEvent(event)
            return
        xmin, xmax, ymin, ymax = self._last_ranges
        px = (event.position().x() - self._last_plot_rect.left()) / max(1.0, self._last_plot_rect.width())
        py = (event.position().y() - self._last_plot_rect.top()) / max(1.0, self._last_plot_rect.height())
        px = min(1.0, max(0.0, px))
        py = min(1.0, max(0.0, py))
        anchor_x = xmin + px * (xmax - xmin)
        anchor_y = ymin + py * (ymax - ymin) if self.reverse_y else ymax - py * (ymax - ymin)
        factor = 0.8 if event.angleDelta().y() > 0 else 1.25
        if self.zoom_mode in {"XY", "X"}:
            self._view_x = _zoom_range(xmin, xmax, anchor_x, factor)
        if self.zoom_mode in {"XY", "Y"}:
            self._view_y = _zoom_range(ymin, ymax, anchor_y, factor)
        self.update()
        event.accept()

    def _draw_series(self, painter: QPainter, points, xmin, xmax, map_x, map_y, color: QColor) -> bool:
        visible = [(x, y) for x, y in points if xmin <= x <= xmax]
        if len(visible) < 2:
            return False
        path = QPainterPath(QPointF(map_x(visible[0][0]), map_y(visible[0][1])))
        for x, y in visible[1:]:
            path.lineTo(QPointF(map_x(x), map_y(y)))
        painter.setPen(QPen(color, 2))
        painter.drawPath(path)
        return True

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(10, 8, -10, -10)
        painter.fillRect(rect, QColor(16, 18, 24))
        painter.setPen(QPen(QColor(80, 88, 105), 1))
        painter.drawRoundedRect(rect, 6, 6)

        xmin, xmax, ymin, ymax = self._ranges()
        self._last_ranges = (xmin, xmax, ymin, ymax)
        x_ticks = range_ticks(xmin, xmax)
        y_ticks = range_ticks(ymin, ymax)
        x_labels = _tick_labels(x_ticks)
        y_labels = _tick_labels(y_ticks, self.scientific_ticks)
        label_width = max((painter.fontMetrics().horizontalAdvance(label) for label in y_labels), default=0)
        left_margin = max(66, label_width + 36)
        plot = rect.adjusted(left_margin, 24, -18, -42)
        self._last_plot_rect = plot
        painter.setPen(QColor(238, 241, 247))
        painter.drawText(rect.adjusted(10, 2, -10, -2), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, self.title)
        painter.setPen(QColor(168, 176, 190))
        painter.drawText(rect.adjusted(0, 0, -12, -5), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, self.x_label)
        painter.save()
        painter.translate(rect.left() + 14, rect.center().y() + 36)
        painter.rotate(-90)
        painter.drawText(0, 0, self.y_label)
        painter.restore()

        primary_color = QColor(96, 232, 184)
        secondary_color = QColor(112, 169, 255)
        if self.secondary_points:
            painter.setPen(primary_color)
            painter.drawText(rect.adjusted(12, 3, -126, -3), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight, self.primary_label)
            painter.setPen(secondary_color)
            painter.drawText(rect.adjusted(12, 3, -42, -3), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight, self.secondary_label)

        def map_x(value: float) -> float:
            return plot.left() + (value - xmin) / (xmax - xmin) * plot.width()

        def map_y(value: float) -> float:
            if self.reverse_y:
                return plot.top() + (value - ymin) / (ymax - ymin) * plot.height()
            return plot.bottom() - (value - ymin) / (ymax - ymin) * plot.height()

        grid_pen = QPen(QColor(45, 51, 64), 1)
        axis_pen = QPen(QColor(95, 103, 122), 1)
        zero_pen = QPen(QColor(105, 118, 140), 1.4)
        label_color = QColor(168, 176, 190)

        for tick in x_ticks:
            x = map_x(tick)
            painter.setPen(zero_pen if tick == 0.0 else grid_pen)
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.setPen(label_color)
        _draw_x_tick_labels(painter, x_ticks, x_labels, map_x, plot, rect)

        for tick, label in zip(y_ticks, y_labels):
            y = map_y(tick)
            painter.setPen(zero_pen if tick == 0.0 else grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(label_color)
            painter.drawText(
                int(rect.left() + 28),
                int(y - 10),
                left_margin - 34,
                20,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label,
            )

        painter.setPen(axis_pen)
        painter.drawRect(plot)

        painter.save()
        painter.setClipRect(plot)
        drew_primary = self._draw_series(painter, self.points, xmin, xmax, map_x, map_y, primary_color)
        drew_secondary = self._draw_series(painter, self.secondary_points, xmin, xmax, map_x, map_y, secondary_color)
        painter.restore()
        if not drew_primary and not drew_secondary:
            painter.setPen(QColor(140, 148, 163))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "Waiting for data...")


class DualAxisTimePlot(_InteractivePlot):
    """Time plot with drain current on the left and Gate voltage on the right."""

    def __init__(
        self,
        title: str = "Drain I and Gate V vs Time",
        drain_scale: float = 1.0,
        drain_axis_label: str = "drain I (A)",
        scientific_drain_ticks: bool = True,
        scientific_gate_ticks: bool = True,
    ):
        super().__init__()
        self.title = title
        self.drain_scale = float(drain_scale)
        self.drain_axis_label = drain_axis_label
        self.scientific_drain_ticks = scientific_drain_ticks
        self.scientific_gate_ticks = scientific_gate_ticks
        self.points: List[Tuple[float, float, float]] = []
        self.secondary_drain_points: List[Tuple[float, float]] = []
        self._view_x: tuple[float, float] | None = None
        self._view_y1: tuple[float, float] | None = None
        self._view_y2: tuple[float, float] | None = None

    def add_sample(self, sample: MeasurementSample) -> None:
        values = (sample.elapsed_s, sample.drain_i * self.drain_scale, sample.gate_v)
        if all(math.isfinite(value) for value in values):
            self.points.append(values)
            if len(self.points) > 3000:
                self.points = self.points[-3000:]
            self.update()

    def set_points(self, points) -> None:
        cleaned: List[Tuple[float, float, float]] = []
        for x, y1, y2 in points:
            values = (float(x), float(y1) * self.drain_scale, float(y2))
            if all(math.isfinite(value) for value in values):
                cleaned.append(values)
        self.points = cleaned
        self.reset_view()

    def set_secondary_drain_points(self, points) -> None:
        cleaned: List[Tuple[float, float]] = []
        for x, y in points:
            values = (float(x), float(y) * self.drain_scale)
            if all(math.isfinite(value) for value in values):
                cleaned.append(values)
        self.secondary_drain_points = cleaned
        self.reset_view()

    def clear_secondary_drain_points(self) -> None:
        self.secondary_drain_points.clear()
        self.reset_view()

    def clear(self) -> None:
        self.points.clear()
        self.secondary_drain_points.clear()
        self.reset_view()

    def reset_view(self) -> None:
        self._view_x = None
        self._view_y1 = None
        self._view_y2 = None
        self.update()

    def _ranges(self) -> tuple[float, float, float, float, float, float]:
        xs = [point[0] for point in self.points] + [point[0] for point in self.secondary_drain_points]
        y1s = [point[1] for point in self.points] + [point[1] for point in self.secondary_drain_points]
        y2s = [point[2] for point in self.points]
        dx = axis_ticks(xs or [0.0, 1.0], include_zero=True)[:2]
        dy1 = axis_ticks(y1s or [0.0, 1.0], include_zero=True)[:2]
        dy2 = axis_ticks(y2s or [0.0, 1.0], include_zero=True)[:2]
        xmin, xmax = self._view_x or dx
        y1min, y1max = self._view_y1 or dy1
        y2min, y2max = self._view_y2 or dy2
        return xmin, xmax, y1min, y1max, y2min, y2max

    def mouseMoveEvent(self, event) -> None:
        if self._drag_start is None or self._drag_ranges is None or self._last_plot_rect is None:
            super().mouseMoveEvent(event)
            return
        xmin, xmax, y1min, y1max, y2min, y2max = self._drag_ranges
        dx = event.position().x() - self._drag_start.x()
        dy = event.position().y() - self._drag_start.y()
        x_shift = -dx / max(1.0, self._last_plot_rect.width()) * (xmax - xmin)
        y1_shift = -dy / max(1.0, self._last_plot_rect.height()) * (y1max - y1min)
        y2_shift = -dy / max(1.0, self._last_plot_rect.height()) * (y2max - y2min)
        self._view_x = (xmin + x_shift, xmax + x_shift)
        self._view_y1 = (y1min + y1_shift, y1max + y1_shift)
        self._view_y2 = (y2min + y2_shift, y2max + y2_shift)
        self.update()
        event.accept()

    def wheelEvent(self, event) -> None:
        if not self._can_interact(event.position()) or self._last_ranges is None or self._last_plot_rect is None:
            super().wheelEvent(event)
            return
        xmin, xmax, y1min, y1max, y2min, y2max = self._last_ranges
        px = (event.position().x() - self._last_plot_rect.left()) / max(1.0, self._last_plot_rect.width())
        py = (event.position().y() - self._last_plot_rect.top()) / max(1.0, self._last_plot_rect.height())
        px = min(1.0, max(0.0, px))
        py = min(1.0, max(0.0, py))
        factor = 0.8 if event.angleDelta().y() > 0 else 1.25
        anchor_x = xmin + px * (xmax - xmin)
        anchor_y1 = y1min + py * (y1max - y1min)
        anchor_y2 = y2min + py * (y2max - y2min)
        if self.zoom_mode in {"XY", "X"}:
            self._view_x = _zoom_range(xmin, xmax, anchor_x, factor)
        if self.zoom_mode in {"XY", "Y"}:
            self._view_y1 = _zoom_range(y1min, y1max, anchor_y1, factor)
            self._view_y2 = _zoom_range(y2min, y2max, anchor_y2, factor)
        self.update()
        event.accept()

    @staticmethod
    def _path(points, xmin, xmax, map_x, map_y) -> QPainterPath | None:
        visible = [point for point in points if xmin <= point[0] <= xmax]
        if len(visible) < 2:
            return None
        path = QPainterPath(QPointF(map_x(visible[0][0]), map_y(visible[0][1])))
        for x, y in visible[1:]:
            path.lineTo(QPointF(map_x(x), map_y(y)))
        return path

    def paintEvent(self, event) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self.rect().adjusted(10, 8, -10, -10)
        painter.fillRect(rect, QColor(16, 18, 24))
        painter.setPen(QPen(QColor(80, 88, 105), 1))
        painter.drawRoundedRect(rect, 6, 6)

        xmin, xmax, y1min, y1max, y2min, y2max = self._ranges()
        self._last_ranges = (xmin, xmax, y1min, y1max, y2min, y2max)
        x_ticks = range_ticks(xmin, xmax)
        y1_ticks = range_ticks(y1min, y1max)
        y2_ticks = range_ticks(y2min, y2max)
        x_labels = _tick_labels(x_ticks)
        y1_labels = _tick_labels(y1_ticks, self.scientific_drain_ticks)
        y2_labels = _tick_labels(y2_ticks, self.scientific_gate_ticks)
        metrics = painter.fontMetrics()
        left_margin = max(66, max((metrics.horizontalAdvance(label) for label in y1_labels), default=0) + 36)
        right_margin = max(66, max((metrics.horizontalAdvance(label) for label in y2_labels), default=0) + 36)
        plot = rect.adjusted(left_margin, 24, -right_margin, -42)
        self._last_plot_rect = plot
        painter.setPen(QColor(238, 241, 247))
        painter.drawText(rect.adjusted(10, 2, -10, -2), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft, self.title)
        painter.setPen(QColor(168, 176, 190))
        painter.drawText(rect.adjusted(0, 0, -12, -5), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom, "time (s)")

        drain1_color = QColor(96, 232, 184)
        drain2_color = QColor(112, 169, 255)
        gate_color = QColor(255, 209, 102)
        if self.secondary_drain_points:
            painter.setPen(drain1_color)
            painter.drawText(rect.adjusted(12, 3, -178, -3), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight, "Drain I 1")
            painter.setPen(drain2_color)
            painter.drawText(rect.adjusted(12, 3, -101, -3), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight, "Drain I 2")
        else:
            painter.setPen(drain1_color)
            painter.drawText(rect.adjusted(12, 3, -130, -3), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight, "Drain I")
        painter.setPen(gate_color)
        painter.drawText(rect.adjusted(12, 3, -34, -3), Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight, "Gate V")

        painter.save()
        painter.setPen(drain1_color)
        painter.translate(rect.left() + 14, rect.center().y() + 36)
        painter.rotate(-90)
        painter.drawText(0, 0, self.drain_axis_label)
        painter.restore()
        painter.save()
        painter.setPen(gate_color)
        painter.translate(rect.right() - 10, rect.center().y() - 36)
        painter.rotate(90)
        painter.drawText(0, 0, "gate V (V)")
        painter.restore()

        def map_x(value: float) -> float:
            return plot.left() + (value - xmin) / (xmax - xmin) * plot.width()

        def map_y1(value: float) -> float:
            return plot.top() + (value - y1min) / (y1max - y1min) * plot.height()

        def map_y2(value: float) -> float:
            return plot.top() + (value - y2min) / (y2max - y2min) * plot.height()

        grid_pen = QPen(QColor(45, 51, 64), 1)
        zero_pen = QPen(QColor(105, 118, 140), 1.4)
        axis_pen = QPen(QColor(95, 103, 122), 1)

        for tick in x_ticks:
            x = map_x(tick)
            painter.setPen(zero_pen if tick == 0.0 else grid_pen)
            painter.drawLine(QPointF(x, plot.top()), QPointF(x, plot.bottom()))
            painter.setPen(QColor(168, 176, 190))
        _draw_x_tick_labels(painter, x_ticks, x_labels, map_x, plot, rect)

        for tick, label in zip(y1_ticks, y1_labels):
            y = map_y1(tick)
            painter.setPen(zero_pen if tick == 0.0 else grid_pen)
            painter.drawLine(QPointF(plot.left(), y), QPointF(plot.right(), y))
            painter.setPen(drain1_color)
            painter.drawText(
                int(rect.left() + 28),
                int(y - 10),
                left_margin - 34,
                20,
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                label,
            )

        for tick, label in zip(y2_ticks, y2_labels):
            y = map_y2(tick)
            painter.setPen(gate_color)
            painter.drawText(
                int(plot.right() + 6),
                int(y - 10),
                right_margin - 34,
                20,
                Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                label,
            )

        painter.setPen(axis_pen)
        painter.drawRect(plot)

        painter.save()
        painter.setClipRect(plot)
        primary_points = [(x, y1) for x, y1, _y2 in self.points]
        gate_points = [(x, y2) for x, _y1, y2 in self.points]
        drain1_path = self._path(primary_points, xmin, xmax, map_x, map_y1)
        drain2_path = self._path(self.secondary_drain_points, xmin, xmax, map_x, map_y1)
        gate_path = self._path(gate_points, xmin, xmax, map_x, map_y2)

        if drain1_path is not None:
            painter.setPen(QPen(drain1_color, 2))
            painter.drawPath(drain1_path)
        if drain2_path is not None:
            painter.setPen(QPen(drain2_color, 2))
            painter.drawPath(drain2_path)
        if gate_path is not None:
            painter.setPen(QPen(gate_color, 2))
            painter.drawPath(gate_path)
        painter.restore()
        if drain1_path is None and drain2_path is None and gate_path is None:
            painter.setPen(QColor(140, 148, 163))
            painter.drawText(plot, Qt.AlignmentFlag.AlignCenter, "Waiting for data...")
