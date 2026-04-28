from PySide6.QtCore import QEvent, QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from rewind_recorder.config import RecorderState
from rewind_recorder.types import CaptureArea
from rewind_recorder.windows_api import exclude_widget_from_capture, force_widget_topmost


class AreaSelector(QWidget):
    area_selected = Signal(dict)

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_DeleteOnClose)
        self.setCursor(Qt.CrossCursor)
        self.setFocusPolicy(Qt.StrongFocus)

        self.origin: QPoint | None = None
        self.current: QPoint | None = None

        self.setGeometry(self.virtual_desktop_geometry())

    @staticmethod
    def virtual_desktop_geometry() -> QRect:
        screens = QApplication.screens()
        if not screens:
            return QRect(0, 0, 800, 600)
        geometry = screens[0].geometry()
        for screen in screens[1:]:
            geometry = geometry.united(screen.geometry())
        return geometry

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(0, 0, 0, 90))

        if self.origin is not None and self.current is not None:
            selection = self.selection_rect()
            painter.fillRect(selection, QColor(255, 255, 255, 35))
            painter.setPen(QPen(QColor(255, 255, 255), 2, Qt.SolidLine))
            painter.drawRect(selection)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self.origin = event.position().toPoint()
            self.current = self.origin
            self.update()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self.origin is not None:
            self.current = event.position().toPoint()
            self.update()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or self.origin is None:
            return
        self.current = event.position().toPoint()
        selection = self.selection_rect()
        if selection.width() >= 8 and selection.height() >= 8:
            top_left = selection.topLeft() + self.geometry().topLeft()
            self.area_selected.emit(
                {
                    "x": top_left.x(),
                    "y": top_left.y(),
                    "width": selection.width(),
                    "height": selection.height(),
                }
            )
        self.close()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close()

    def selection_rect(self) -> QRect:
        assert self.origin is not None
        assert self.current is not None
        return QRect(self.origin, self.current).normalized()


class CaptureAreaOverlay(QWidget):
    def __init__(self) -> None:
        flags = Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        transparent_input = getattr(Qt, "WindowTransparentForInput", None)
        if transparent_input is not None:
            flags |= transparent_input

        super().__init__(None, flags)
        self.border = 6
        self.is_recording = False
        self._pulse_step = 0
        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(120)
        self._pulse_timer.timeout.connect(self._advance_pulse)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setFocusPolicy(Qt.NoFocus)
        self.hide()

    def set_area(self, area: CaptureArea) -> None:
        self.setGeometry(
            area.x - self.border,
            area.y - self.border,
            area.width + (self.border * 2),
            area.height + (self.border * 2),
        )
        self.show()
        force_widget_topmost(self)
        exclude_widget_from_capture(self)

    def set_status(self, state: RecorderState) -> None:
        if state is RecorderState.RECORDING:
            self.is_recording = True
            if not self._pulse_timer.isActive():
                self._pulse_timer.start()
        else:
            self.is_recording = False
            self._stop_pulse()
        self.update()

    def _advance_pulse(self) -> None:
        self._pulse_step = (self._pulse_step + 1) % 12
        self.update()

    def _stop_pulse(self) -> None:
        self._pulse_timer.stop()
        self._pulse_step = 0

    def paintEvent(self, event) -> None:  # noqa: N802
        if not self.is_recording:
            return
        painter = QPainter(self)
        pulse = abs(6 - self._pulse_step) / 6
        color = QColor(235, 35, 35, 150 + int(90 * pulse))
        painter.fillRect(0, 0, self.width(), self.border, color)
        painter.fillRect(0, self.height() - self.border, self.width(), self.border, color)
        painter.fillRect(0, 0, self.border, self.height(), color)
        painter.fillRect(self.width() - self.border, 0, self.border, self.height(), color)


class FloatingRecorderControl(QWidget):
    primary_clicked = Signal()

    _IDLE_COLOR = "#3a3a3a"
    _RECORDING_COLOR = "#c62828"
    _RECORDING_PULSE_COLOR = "#e53935"

    def __init__(self) -> None:
        super().__init__(
            None,
            Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool,
        )
        self._capture_area: CaptureArea | None = None
        self._drag_offset: QPoint | None = None
        self._drag_start: QPoint | None = None
        self._drag_started = False
        self._is_recording = False
        self._pulse_on = False

        self._pulse_timer = QTimer(self)
        self._pulse_timer.setInterval(600)
        self._pulse_timer.timeout.connect(self._animate_pulse)

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setFocusPolicy(Qt.StrongFocus)

        self.drag_handle = QLabel("☰")
        self.drag_handle.setAlignment(Qt.AlignCenter)
        self.drag_handle.setCursor(Qt.OpenHandCursor)
        self.drag_handle.setMinimumHeight(28)
        self.drag_handle.setStyleSheet(
            """
            QLabel {
                background: rgba(255, 255, 255, 22);
                color: #d8d8d8;
                font-size: 14px;
                padding: 4px 0;
                border-top-left-radius: 14px;
                border-top-right-radius: 14px;
            }
            """
        )

        self.primary_button = QPushButton("●  Start Recording")
        self.primary_button.setMinimumSize(210, 54)
        self.primary_button.clicked.connect(self.primary_clicked.emit)

        for w in (self.drag_handle, self.primary_button):
            w.installEventFilter(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 10)
        layout.setSpacing(6)
        layout.addWidget(self.drag_handle)

        button_container = QWidget()
        button_container.setStyleSheet("background: transparent;")
        button_row = QHBoxLayout()
        button_row.setContentsMargins(10, 0, 10, 0)
        button_row.addWidget(self.primary_button)
        button_container.setLayout(button_row)
        layout.addWidget(button_container)

        self.setStyleSheet(
            """
            FloatingRecorderControl {
                background: rgba(20, 20, 20, 230);
                border-radius: 14px;
            }
            QPushButton {
                border: none;
                border-radius: 10px;
                color: white;
                font-weight: 700;
                font-size: 18px;
                padding: 10px 18px;
            }
            """
        )
        self._apply_button_style(self._IDLE_COLOR)
        self.hide()

    def set_area(self, area: CaptureArea) -> None:
        area_changed = self._capture_area != area
        self._capture_area = area

        self.adjustSize()
        if area_changed or not self.isVisible():
            self._move_to_default_position(area)

        self.show()
        force_widget_topmost(self)
        exclude_widget_from_capture(self)

    def _move_to_default_position(self, area: CaptureArea) -> None:
        desktop = AreaSelector.virtual_desktop_geometry()
        preferred_x = area.x + (area.width - self.width()) // 2
        preferred_y = area.y + area.height

        if preferred_y + self.height() > desktop.bottom():
            preferred_y = area.y - self.height()

        x = max(desktop.left(), min(preferred_x, desktop.right() - self.width()))
        y = max(desktop.top(), min(preferred_y, desktop.bottom() - self.height()))
        self.move(x, y)

    def set_state(self, state: RecorderState, has_area: bool) -> None:
        self.setVisible(has_area)
        self.primary_button.setEnabled(has_area)

        if state is RecorderState.RECORDING:
            self.primary_button.setText("⏸  Pause")
            self._is_recording = True
            if not self._pulse_timer.isActive():
                self._pulse_timer.start()
            self._apply_button_style(self._RECORDING_COLOR)
        elif state is RecorderState.PAUSED:
            self.primary_button.setText("●  Resume")
            self.primary_button.setEnabled(True)
            self._stop_pulse()
            self._apply_button_style(self._IDLE_COLOR)
        elif state is RecorderState.STOPPED:
            self.hide()
            self.primary_button.setEnabled(False)
            self._stop_pulse()
            self._apply_button_style(self._IDLE_COLOR)
            return
        else:
            self.primary_button.setText("●  Start Recording")
            self.primary_button.setEnabled(has_area)
            self._stop_pulse()
            self._apply_button_style(self._IDLE_COLOR)

        self.adjustSize()
        if has_area:
            force_widget_topmost(self)
            exclude_widget_from_capture(self)

    def _animate_pulse(self) -> None:
        self._pulse_on = not self._pulse_on
        color = self._RECORDING_PULSE_COLOR if self._pulse_on else self._RECORDING_COLOR
        self._apply_button_style(color)

    def _stop_pulse(self) -> None:
        self._pulse_timer.stop()
        self._pulse_on = False
        self._is_recording = False

    def _apply_button_style(self, color: str) -> None:
        self.primary_button.setStyleSheet(
            f"""
            QPushButton {{
                background: {color};
                color: white;
                font-size: 18px;
            }}
            QPushButton:hover {{
                background: {color}; filter: brightness(1.2);
            }}
            QPushButton:disabled {{
                background: #555;
                color: #999;
            }}
            """
        )

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._begin_drag(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._drag_offset is not None:
            self._update_drag(event.globalPosition().toPoint(), force=True)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._drag_offset is not None:
            self._end_drag()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched in {self.drag_handle, self.primary_button}:
            if event.type() == QEvent.MouseButtonPress and event.button() == Qt.LeftButton:
                self._begin_drag(event.globalPosition().toPoint())
                return watched is self.drag_handle
            if event.type() == QEvent.MouseMove and self._drag_offset is not None:
                return self._update_drag(event.globalPosition().toPoint(), force=watched is self.drag_handle)
            if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton:
                was_dragging = self._drag_started
                self._end_drag()
                self.drag_handle.setCursor(Qt.OpenHandCursor)
                return watched is self.drag_handle or was_dragging
        return super().eventFilter(watched, event)

    def _begin_drag(self, global_position: QPoint) -> None:
        self._drag_start = global_position
        self._drag_offset = global_position - self.frameGeometry().topLeft()
        self._drag_started = False

    def _update_drag(self, global_position: QPoint, force: bool = False) -> bool:
        if self._drag_offset is None or self._drag_start is None:
            return False
        moved_far_enough = (global_position - self._drag_start).manhattanLength() >= 4
        if not force and not self._drag_started and not moved_far_enough:
            return False
        self._drag_started = True
        self.drag_handle.setCursor(Qt.ClosedHandCursor)
        desired = global_position - self._drag_offset
        self.move(self._clamp_to_desktop(desired))
        force_widget_topmost(self)
        return True

    def _end_drag(self) -> None:
        self._drag_offset = None
        self._drag_start = None
        self._drag_started = False

    def _clamp_to_desktop(self, position: QPoint) -> QPoint:
        desktop = AreaSelector.virtual_desktop_geometry()
        return QPoint(
            max(desktop.left(), min(position.x(), desktop.right() - self.width())),
            max(desktop.top(), min(position.y(), desktop.bottom() - self.height())),
        )


class TrimTimeline(QWidget):
    valueChanged = Signal(int)
    selectionChanged = Signal(object, object)
    sliderPressed = Signal()
    sliderReleased = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._minimum = 0
        self._maximum = 0
        self._value = 0
        self._selection_start: int | None = None
        self._selection_end: int | None = None
        self._drag_mode: str | None = None
        self.setMinimumHeight(32)
        self.setMouseTracking(True)
        self.setCursor(Qt.PointingHandCursor)

    def setRange(self, minimum: int, maximum: int) -> None:  # noqa: N802
        self._minimum = minimum
        self._maximum = max(minimum, maximum)
        self.setValue(self._value, emit=False)
        self.update()

    def setValue(self, value: int, emit: bool = True) -> None:  # noqa: N802
        value = max(self._minimum, min(value, self._maximum))
        if value == self._value:
            self.update()
            return
        self._value = value
        self.update()
        if emit:
            self.valueChanged.emit(self._value)

    def value(self) -> int:
        return self._value

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802
        super().setEnabled(enabled)
        self._update_cursor()
        self.update()

    def set_selection(self, start: int | None, end: int | None) -> None:
        self._selection_start = start
        self._selection_end = end
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        track_rect = QRect(12, 10, max(1, self.width() - 24), 10)
        base_color = QColor(80, 80, 80) if self.isEnabled() else QColor(45, 45, 45)
        painter.fillRect(track_rect, base_color)

        if self._maximum > self._minimum:
            progress_width = max(0, self._x_for_value(self._value) - track_rect.left())
            painter.fillRect(
                QRect(track_rect.left(), track_rect.top(), progress_width, track_rect.height()),
                QColor(95, 145, 255),
            )

        if (
            self._selection_start is not None
            and self._selection_end is not None
            and self._selection_start != self._selection_end
        ):
            range_start, range_end = sorted((self._selection_start, self._selection_end))
            sel_left = self._x_for_value(range_start)
            sel_right = self._x_for_value(range_end)
            painter.fillRect(
                QRect(sel_left, track_rect.top() - 6, max(2, sel_right - sel_left), track_rect.height() + 12),
                QColor(255, 88, 68, 115),
            )

        if self._selection_start is not None:
            sx = self._x_for_value(self._selection_start)
            painter.setPen(QPen(QColor(255, 120, 90), 3))
            painter.drawLine(sx, track_rect.top() - 10, sx, track_rect.bottom() + 10)

        if self._selection_end is not None:
            ex = self._x_for_value(self._selection_end)
            painter.setPen(QPen(QColor(255, 120, 90), 3))
            painter.drawLine(ex, track_rect.top() - 10, ex, track_rect.bottom() + 10)

        playhead_x = self._x_for_value(self._value)
        painter.setBrush(QColor(255, 255, 255))
        painter.setPen(QPen(QColor(50, 50, 50), 2))
        painter.drawEllipse(QPoint(playhead_x, track_rect.center().y()), 9, 9)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if not self.isEnabled() or event.button() != Qt.LeftButton:
            return
        x = event.position().toPoint().x()
        handle = self._handle_at_x(x)
        if handle is not None:
            self._drag_mode = handle
            self.setCursor(Qt.SizeHorCursor)
            return
        self._drag_mode = "playhead"
        self.sliderPressed.emit()
        self.setValue(self._value_for_x(x))

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self.isEnabled():
            return
        x = event.position().toPoint().x()
        if self._drag_mode == "playhead":
            self.setValue(self._value_for_x(x))
        elif self._drag_mode in {"start", "end"}:
            value = self._value_for_x(x)
            if self._drag_mode == "start":
                self._selection_start = value
            else:
                self._selection_end = value
            self.selectionChanged.emit(self._selection_start, self._selection_end)
            self.update()
        else:
            self._update_cursor(x)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._drag_mode is not None:
            mode = self._drag_mode
            self._drag_mode = None
            if mode == "playhead":
                self.setValue(self._value_for_x(event.position().toPoint().x()))
            elif mode in {"start", "end"}:
                self.selectionChanged.emit(self._selection_start, self._selection_end)
            self.sliderReleased.emit()
            self._update_cursor()

    def _x_for_value(self, value: int) -> int:
        track_left = 12
        track_width = max(1, self.width() - 24)
        if self._maximum <= self._minimum:
            return track_left
        ratio = (max(self._minimum, min(value, self._maximum)) - self._minimum) / (self._maximum - self._minimum)
        return track_left + int(round(ratio * track_width))

    def _value_for_x(self, x: int) -> int:
        track_left = 12
        track_width = max(1, self.width() - 24)
        ratio = (max(track_left, min(x, track_left + track_width)) - track_left) / track_width
        return int(round(self._minimum + ratio * (self._maximum - self._minimum)))

    def _handle_at_x(self, x: int) -> str | None:
        hit_radius = 10
        hits: list[tuple[str, int]] = []
        if self._selection_start is not None:
            hits.append(("start", abs(x - self._x_for_value(self._selection_start))))
        if self._selection_end is not None:
            hits.append(("end", abs(x - self._x_for_value(self._selection_end))))
        hits = [h for h in hits if h[1] <= hit_radius]
        if not hits:
            return None
        hits.sort(key=lambda h: h[1])
        return hits[0][0]

    def _update_cursor(self, x: int | None = None) -> None:
        if not self.isEnabled():
            self.setCursor(Qt.ArrowCursor)
            return
        if self._drag_mode in {"start", "end"}:
            self.setCursor(Qt.SizeHorCursor)
            return
        if x is not None and self._handle_at_x(x) is not None:
            self.setCursor(Qt.SizeHorCursor)
            return
        self.setCursor(Qt.PointingHandCursor)
