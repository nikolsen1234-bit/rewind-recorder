from pathlib import Path
from typing import Any

import cv2
import qtawesome as qta
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from rewind_recorder.audio_manager import AudioManager
from rewind_recorder.autosave import AutosaveManager
from rewind_recorder.capture import CaptureWorker
from rewind_recorder.config import APP_NAME, DEFAULT_FPS, RecorderState
from rewind_recorder.export import VideoExporter
from rewind_recorder.preview import PreviewController
from rewind_recorder.project import FrameProject
from rewind_recorder.timecode import format_seconds
from rewind_recorder.types import CaptureArea
from rewind_recorder.widgets import AreaSelector, CaptureAreaOverlay, FloatingRecorderControl, TrimTimeline
from rewind_recorder.windows_api import exclude_widget_from_capture, force_widget_topmost

_BUTTON_STYLE = """
QPushButton {
    background: #2a2a2a;
    color: #e0e0e0;
    border: 1px solid #444;
    border-radius: 6px;
    padding: 10px 16px;
    font-size: 14px;
    font-weight: 600;
}
QPushButton:hover {
    background: #383838;
    border-color: #666;
}
QPushButton:pressed {
    background: #1a1a1a;
    border-color: #555;
}
QPushButton:disabled {
    background: #1e1e1e;
    color: #555;
    border-color: #333;
}
"""

_SECTION_STYLE = "font-weight: 700; font-size: 14px; color: #ccc;"
_SUBLABEL_STYLE = "color: #888; font-size: 12px;"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.project = FrameProject(DEFAULT_FPS)
        self.audio = AudioManager(DEFAULT_FPS)
        self.exporter = VideoExporter(DEFAULT_FPS)
        self.autosaver = AutosaveManager(self.project, self.audio)

        self.capture_worker: CaptureWorker | None = None
        self.selector: AreaSelector | None = None
        self.area_overlay = CaptureAreaOverlay()
        self.floating_control = FloatingRecorderControl()
        self.locked_capture_area: CaptureArea | None = None

        self._updating_timeline = False
        self._timeline_dragging = False
        self._state_transitioning = False
        self._overlay_area_key: CaptureArea | None = None
        self._floating_area_key: CaptureArea | None = None

        self.topmost_timer = QTimer(self)
        self.topmost_timer.setInterval(1000)
        self.topmost_timer.timeout.connect(self._maintain_topmost)
        self.topmost_timer.start()

        self.setWindowTitle(APP_NAME)
        self.resize(1100, 750)
        self._build_ui()

        self.preview_ctrl = PreviewController(self.project, self.audio, self.exporter, parent=self)
        self.preview_ctrl.frame_changed.connect(self._on_preview_frame)
        self.preview_ctrl.playback_stopped.connect(self._on_preview_stopped)
        self.preview_ctrl.status_message.connect(self._update_status)

        result = self.autosaver.restore()
        if result.restored:
            self.locked_capture_area = result.locked_area
            self._update_status("Restored previous recording")
            self._sync_overlay(apply_geometry=True)
        elif result.error:
            QMessageBox.warning(
                self, APP_NAME,
                f"Could not restore previous recording.\n\n{result.error}",
            )

        self._refresh_timeline()
        self._update_controls()

    def _icon(self, name: str) -> "QIcon":
        return qta.icon(name, color=QColor(200, 200, 200))

    def _build_ui(self) -> None:
        central = QWidget()
        central.setStyleSheet("background: #181818;")
        layout = QVBoxLayout(central)
        layout.setSpacing(6)
        layout.setContentsMargins(16, 12, 16, 12)

        self.time_label = QLabel("00:00.000 / 00:00.000")
        self.time_label.setStyleSheet(_SUBLABEL_STYLE)
        self.time_label.setAlignment(Qt.AlignCenter)
        self.cut_label = QLabel("Trim range: none")
        self.cut_label.setStyleSheet(_SUBLABEL_STYLE)

        self.preview_label = QLabel("Preview appears here after frames are recorded")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(320, 180)
        self.preview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_label.setScaledContents(False)
        self.preview_label.setStyleSheet(
            "QLabel { background: #0a0a0a; color: #666; border: 1px solid #333; border-radius: 6px; }"
        )
        self._last_preview_frame: QImage | None = None

        self.timeline = TrimTimeline()
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.valueChanged.connect(self._on_timeline_changed)
        self.timeline.selectionChanged.connect(self._on_trim_selection_changed)
        self.timeline.sliderPressed.connect(self._on_timeline_pressed)
        self.timeline.sliderReleased.connect(self._on_timeline_released)

        self.audio_input_combo = QComboBox()
        self.audio_output_combo = QComboBox()
        self.refresh_audio_button = QPushButton(self._icon("mdi6.refresh"), " Refresh")
        self.refresh_audio_button.setStyleSheet(_BUTTON_STYLE)
        self.refresh_audio_button.clicked.connect(self._refresh_audio_devices)

        self.select_button = QPushButton(self._icon("mdi6.target"), " Select Area")
        self.import_button = QPushButton(self._icon("mdi6.file-import-outline"), " Import")
        self.play_preview_button = QPushButton(self._icon("mdi6.play"), " Play")
        self.stop_button = QPushButton(self._icon("mdi6.stop"), " Stop")
        self.save_button = QPushButton(self._icon("mdi6.content-save"), " Save")
        self.cut_start_button = QPushButton(self._icon("mdi6.format-vertical-align-top"), " Mark In")
        self.cut_end_button = QPushButton(self._icon("mdi6.format-vertical-align-bottom"), " Mark Out")
        self.delete_cut_button = QPushButton(self._icon("mdi6.delete-outline"), " Delete Range")
        self.clear_cut_button = QPushButton(self._icon("mdi6.close-circle-outline"), " Clear Range")

        for btn in (
            self.select_button, self.import_button,
            self.play_preview_button, self.stop_button, self.save_button,
            self.cut_start_button, self.cut_end_button,
            self.delete_cut_button, self.clear_cut_button,
        ):
            btn.setMinimumHeight(40)
            btn.setStyleSheet(_BUTTON_STYLE)

        self.select_button.clicked.connect(self._select_area)
        self.floating_control.primary_clicked.connect(self._record_or_resume)
        self.import_button.clicked.connect(self._import_clip)
        self.play_preview_button.clicked.connect(self._toggle_preview)
        self.stop_button.clicked.connect(self._stop_recording)
        self.save_button.clicked.connect(self._save_as)
        self.cut_start_button.clicked.connect(self._set_cut_start)
        self.cut_end_button.clicked.connect(self._set_cut_end)
        self.delete_cut_button.clicked.connect(self._delete_selected_range)
        self.clear_cut_button.clicked.connect(self._clear_cut_selection)

        input_col = QVBoxLayout()
        input_col.setSpacing(4)
        input_title = QLabel("Input device")
        input_title.setStyleSheet(_SECTION_STYLE)
        input_col.addWidget(input_title)
        input_col.addWidget(self.audio_input_combo)

        output_col = QVBoxLayout()
        output_col.setSpacing(4)
        output_title = QLabel("Output device")
        output_title.setStyleSheet(_SECTION_STYLE)
        output_col.addWidget(output_title)
        output_col.addWidget(self.audio_output_combo)

        refresh_col = QVBoxLayout()
        refresh_col.addStretch(1)
        refresh_col.addWidget(self.refresh_audio_button)

        audio_row = QHBoxLayout()
        audio_row.setSpacing(12)
        audio_row.addLayout(input_col, 1)
        audio_row.addLayout(output_col, 1)
        audio_row.addLayout(refresh_col)

        timeline_header = QHBoxLayout()
        tl_label = QLabel("Timeline")
        tl_label.setStyleSheet(_SECTION_STYLE)
        timeline_header.addWidget(tl_label)
        timeline_header.addStretch(1)

        trim_col = QVBoxLayout()
        trim_col.setSpacing(6)
        trim_header = QHBoxLayout()
        trim_title = QLabel("Trim")
        trim_title.setStyleSheet(_SECTION_STYLE)
        trim_header.addWidget(trim_title)
        trim_header.addSpacing(10)
        trim_header.addWidget(self.cut_label)
        trim_header.addStretch(1)
        trim_col.addLayout(trim_header)
        trim_btns = QHBoxLayout()
        trim_btns.setSpacing(6)
        trim_btns.addWidget(self.cut_start_button)
        trim_btns.addWidget(self.cut_end_button)
        trim_btns.addWidget(self.delete_cut_button)
        trim_btns.addWidget(self.clear_cut_button)
        trim_col.addLayout(trim_btns)

        playback_col = QVBoxLayout()
        playback_col.setSpacing(6)
        pb_title = QLabel("Playback")
        pb_title.setStyleSheet(_SECTION_STYLE)
        playback_col.addWidget(pb_title)
        pb_btns = QHBoxLayout()
        pb_btns.setSpacing(6)
        pb_btns.addWidget(self.select_button)
        pb_btns.addWidget(self.play_preview_button)
        pb_btns.addWidget(self.stop_button)
        playback_col.addLayout(pb_btns)

        actions_col = QVBoxLayout()
        actions_col.setSpacing(6)
        act_title = QLabel("Actions")
        act_title.setStyleSheet(_SECTION_STYLE)
        actions_col.addWidget(act_title)
        act_btns = QHBoxLayout()
        act_btns.setSpacing(6)
        act_btns.addWidget(self.import_button)
        act_btns.addWidget(self.save_button)
        actions_col.addLayout(act_btns)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(24)
        bottom_row.addLayout(trim_col, 2)
        bottom_row.addLayout(playback_col, 1)
        bottom_row.addLayout(actions_col, 1)

        layout.addLayout(audio_row)
        layout.addWidget(self.preview_label, 1)
        layout.addLayout(timeline_header)
        layout.addWidget(self.timeline)
        layout.addWidget(self.time_label)
        layout.addSpacing(8)
        layout.addLayout(bottom_row)

        self.setCentralWidget(central)
        self._refresh_audio_devices()

    def _refresh_audio_devices(self) -> None:
        prev_input = self.audio_input_combo.currentData()
        prev_output = self.audio_output_combo.currentData()

        self.audio_input_combo.blockSignals(True)
        self.audio_output_combo.blockSignals(True)
        self.audio_input_combo.clear()
        self.audio_output_combo.clear()

        try:
            import sounddevice as sd
            for index, name in self._wasapi_input_devices(sd):
                self.audio_input_combo.addItem(name, index)
            if self.audio_input_combo.count() == 0:
                self.audio_input_combo.addItem("No input devices found", None)
        except Exception as exc:
            self.audio_input_combo.addItem(f"No input devices found: {exc}", None)

        try:
            import soundcard as sc
            speakers = list(sc.all_speakers())
            if not speakers:
                self.audio_output_combo.addItem("No output devices found", None)
            for speaker in speakers:
                self.audio_output_combo.addItem(speaker.name, speaker.id)
        except Exception as exc:
            self.audio_output_combo.addItem(f"No output devices found: {exc}", None)

        self._restore_combo(self.audio_input_combo, prev_input)
        self._restore_combo(self.audio_output_combo, prev_output)
        self.audio_input_combo.blockSignals(False)
        self.audio_output_combo.blockSignals(False)

    def _wasapi_input_devices(self, sd: Any) -> list[tuple[int, str]]:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        wasapi = {i for i, h in enumerate(hostapis) if str(h.get("name", "")).lower() == "windows wasapi"}

        candidates: list[tuple[int, str]] = []
        for i, d in enumerate(devices):
            if int(d.get("max_input_channels", 0)) <= 0:
                continue
            if wasapi and int(d.get("hostapi", -1)) not in wasapi:
                continue
            name = " ".join(str(d.get("name", f"Input {i}")).replace("\r", " ").replace("\n", " ").split())
            candidates.append((i, name))

        if not candidates:
            for i, d in enumerate(devices):
                if int(d.get("max_input_channels", 0)) > 0:
                    name = " ".join(str(d.get("name", f"Input {i}")).replace("\r", " ").replace("\n", " ").split())
                    candidates.append((i, name))

        seen: set[str] = set()
        unique: list[tuple[int, str]] = []
        for index, name in candidates:
            key = name.lower()
            if key not in seen:
                seen.add(key)
                unique.append((index, name))
        return unique

    @staticmethod
    def _restore_combo(combo: QComboBox, value: object) -> None:
        for i in range(combo.count()):
            if combo.itemData(i) == value:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def _selected_input_device(self) -> int | None:
        v = self.audio_input_combo.currentData()
        return int(v) if v is not None else None

    def _selected_output_device(self) -> str | None:
        v = self.audio_output_combo.currentData()
        return str(v) if v is not None else None

    def _select_area(self) -> None:
        self.preview_ctrl.stop()
        if self.project.state is RecorderState.RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause or stop recording before selecting a new area.")
            return

        if self.project.has_frames():
            reply = QMessageBox.question(
                self, APP_NAME,
                "Selecting a new area clears the current temporary recording. Continue?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            self.audio.clear(delete_files=True)
            self.project.clear_frames()
            self.autosaver.clear()
            self.project.state = RecorderState.IDLE
            self.locked_capture_area = self.project.area
            self._refresh_timeline()
            self._update_status("Idle")
            self.area_overlay.hide()

        self.selector = AreaSelector()
        self.selector.area_selected.connect(self._on_area_selected)
        self.selector.show()
        force_widget_topmost(self.selector)
        self.selector.activateWindow()

    def _on_area_selected(self, raw: dict) -> None:
        area = CaptureArea.normalized(raw["x"], raw["y"], raw["width"], raw["height"])
        self.project.set_area(area)
        self.locked_capture_area = area
        self._update_status(f"Area selected: {area.width}×{area.height}")
        self._sync_overlay(apply_geometry=True)
        self._update_controls()

    def _record_or_resume(self) -> None:
        self.preview_ctrl.stop()
        if self.project.state is RecorderState.RECORDING:
            self._pause_recording()
        elif self.project.state is RecorderState.PAUSED:
            self._resume_from_here()
        else:
            self._start_recording()

    def _with_transition(self, expected: RecorderState, action: callable) -> None:
        if self.project.state is not expected:
            return
        self.preview_ctrl.stop()
        if self._state_transitioning:
            return
        self._state_transitioning = True
        self._update_controls()
        QApplication.processEvents()
        try:
            action()
        finally:
            self._state_transitioning = False
            self._update_controls()

    def _start_recording(self) -> None:
        if self.project.area is None:
            QMessageBox.warning(self, APP_NAME, "Select a screen area before recording.")
            return
        if self.project.state is RecorderState.RECORDING:
            return
        if self._state_transitioning:
            return
        self._state_transitioning = True
        self._update_controls()
        QApplication.processEvents()
        try:
            if self.project.has_frames():
                reply = QMessageBox.question(
                    self, APP_NAME,
                    "Start a new recording and delete the current temporary frames?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
                self.audio.clear(delete_files=True)
                self.project.clear_frames()
                self.autosaver.clear()
                self.locked_capture_area = self.project.area

            self.project.set_timeline_index(0)
            self.project.reset_cut_marks()
            self._begin_capture()
        finally:
            self._state_transitioning = False
            self._update_controls()

    def _pause_recording(self) -> None:
        def action():
            self._stop_worker()
            self.audio.stop_recording(end_frame=self.project.frame_count())
            self.project.state = RecorderState.PAUSED
            self.project.set_timeline_index(self.project.frame_count())
            self._refresh_timeline()
            self._update_status("Paused")
            self._sync_overlay()
        self._with_transition(RecorderState.RECORDING, action)

    def _resume_from_here(self) -> None:
        def action():
            index = self.timeline.value()
            self.audio.discard_after(index)
            removed = self.project.truncate_after(index)
            self._refresh_timeline()
            if removed:
                self._update_status(
                    f"Deleted {removed} frame(s); recording from {format_seconds(index / self.project.fps)}"
                )
            self._begin_capture()
        if self.project.area is None:
            QMessageBox.warning(self, APP_NAME, "Select a screen area before recording.")
            return
        self._with_transition(RecorderState.PAUSED, action)

    def _stop_recording(self) -> None:
        def action():
            self.project.state = RecorderState.STOPPED
            self.project.set_timeline_index(self.project.frame_count())
            self._refresh_timeline()
            self._update_status("Stopped")
            self.area_overlay.hide()
            self.floating_control.hide()
        self._with_transition(RecorderState.PAUSED, action)

    def _begin_capture(self) -> None:
        if self.project.area is None:
            return
        if self.capture_worker is not None and self.capture_worker.isRunning():
            return

        area = self.locked_capture_area or self.project.area
        self.locked_capture_area = area
        self.project.set_area(area)
        self.capture_worker = CaptureWorker(self.project, area, self.project.fps)
        self.capture_worker.frame_saved.connect(self._on_frame_saved)
        self.capture_worker.capture_error.connect(self._on_capture_error)
        self.capture_worker.finished.connect(self._on_worker_finished)
        self.project.state = RecorderState.RECORDING
        self._sync_overlay()

        started, errors = self.audio.start_recording(
            self.project.ensure_temp_dir(),
            self.project.frame_count(),
            self._selected_input_device(),
            self._selected_output_device(),
        )
        self.capture_worker.start()

        if started:
            if errors:
                self._update_status(f"Recording with partial audio - {'; '.join(errors)}")
            else:
                self._update_status("Recording + audio")
        else:
            self._update_status(f"Recording video only - audio unavailable: {'; '.join(errors)}")
        self._update_controls()

    def _stop_worker(self) -> None:
        worker = self.capture_worker
        if worker is None:
            return
        worker.stop()
        worker.wait(3000)
        if worker.isRunning():
            QMessageBox.warning(self, APP_NAME, "Recording thread did not stop within 3 seconds.")
        self.capture_worker = None

    def _on_worker_finished(self) -> None:
        if self.capture_worker is self.sender():
            self.capture_worker = None
        if self.project.state is RecorderState.RECORDING:
            self.audio.stop_recording(end_frame=self.project.frame_count())
            self.project.state = RecorderState.PAUSED if self.project.has_frames() else RecorderState.IDLE
            self._refresh_timeline()
            self._update_status("Recording stopped")
            self._sync_overlay()
            self._update_controls()

    def _on_capture_error(self, message: str) -> None:
        self.audio.stop_recording(end_frame=self.project.frame_count())
        self.project.state = RecorderState.PAUSED if self.project.has_frames() else RecorderState.IDLE
        self._refresh_timeline()
        self._update_status("Capture error")
        self._sync_overlay()
        self._update_controls()
        QMessageBox.critical(self, APP_NAME, f"Screen capture failed:\n\n{message}")

    def _on_frame_saved(self, frame_count: int) -> None:
        if self.project.state is not RecorderState.RECORDING:
            return
        self.project.set_timeline_index(frame_count)
        self._refresh_timeline()
        self._update_status(f"Recording - {format_seconds(frame_count / self.project.fps)}")

    def _on_timeline_changed(self, value: int) -> None:
        if self._updating_timeline or self.project.state is RecorderState.RECORDING:
            return
        if self.preview_ctrl.is_playing:
            self.preview_ctrl.stop()
        self.project.set_timeline_index(value)
        self._update_timeline_labels()
        self._update_preview()

    def _on_timeline_pressed(self) -> None:
        if self.preview_ctrl.is_playing:
            self.preview_ctrl.stop()
        self._timeline_dragging = True

    def _on_timeline_released(self) -> None:
        self._timeline_dragging = False
        if self.project.state is RecorderState.RECORDING:
            return
        self.project.set_timeline_index(self.timeline.value())
        self._refresh_timeline()

    def _on_trim_selection_changed(self, start: int | None, end: int | None) -> None:
        if self.project.state is RecorderState.RECORDING:
            return
        self.project.cut_start = start
        self.project.cut_end = end
        self._update_timeline_labels()
        self.timeline.set_selection(start, end)
        self._update_controls()

    def _set_cut_start(self) -> None:
        if self.project.state is RecorderState.RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause or stop recording before setting Mark In.")
            return
        if not self.project.has_frames():
            QMessageBox.warning(self, APP_NAME, "Record frames before setting Mark In.")
            return
        self.project.cut_start = self.timeline.value()
        self._refresh_timeline()
        self._update_status(f"Mark In set at {format_seconds(self.project.cut_start / self.project.fps)}")
        self._update_controls()

    def _set_cut_end(self) -> None:
        if self.project.state is RecorderState.RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause or stop recording before setting Mark Out.")
            return
        if not self.project.has_frames():
            QMessageBox.warning(self, APP_NAME, "Record frames before setting Mark Out.")
            return
        self.project.cut_end = self.timeline.value()
        self._refresh_timeline()
        self._update_status(f"Mark Out set at {format_seconds(self.project.cut_end / self.project.fps)}")
        self._update_controls()

    def _clear_cut_selection(self) -> None:
        self.project.reset_cut_marks()
        self._refresh_timeline()
        self._update_status("Trim range cleared")
        self._update_controls()

    def _delete_selected_range(self) -> None:
        if self.project.state is RecorderState.RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause or stop recording before deleting a trim range.")
            return
        if self.project.cut_start is None or self.project.cut_end is None:
            QMessageBox.warning(self, APP_NAME, "Set both Mark In and Mark Out before deleting the trim range.")
            return

        start, end = self.project.cut_start, self.project.cut_end
        if start == end:
            QMessageBox.warning(self, APP_NAME, "Mark In and Mark Out are the same position, so nothing is selected.")
            return

        lo, hi = sorted((start, end))
        duration = (hi - lo) / self.project.fps
        reply = QMessageBox.question(
            self, APP_NAME,
            f"Delete the marked trim range?\n\n"
            f"{format_seconds(lo / self.project.fps)} to "
            f"{format_seconds(hi / self.project.fps)} "
            f"({format_seconds(duration)})",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.audio.delete_range(start, end)
        removed = self.project.delete_range(start, end)
        self._refresh_timeline()
        self._update_status(f"Deleted trim range ({removed} frame(s))")
        self._update_controls()

    def _toggle_preview(self) -> None:
        if self.preview_ctrl.is_playing:
            self.preview_ctrl.stop()
            return
        if self.project.state is RecorderState.RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause recording before playing the preview.")
            return
        if not self.project.has_frames():
            QMessageBox.warning(self, APP_NAME, "Record frames before playing the preview.")
            return
        self.preview_ctrl.start(speaker_id=self._selected_output_device())
        self._update_controls()

    def _on_preview_frame(self, index: int) -> None:
        self.timeline.setValue(index, emit=False)
        self._update_timeline_labels()
        self._update_preview()

    def _on_preview_stopped(self) -> None:
        self.play_preview_button.setIcon(self._icon("mdi6.play"))
        self.play_preview_button.setText(" Play")
        state = self.project.state
        self._update_status("Paused" if state is RecorderState.PAUSED else state.value.title())
        self._update_controls()

    def _import_clip(self) -> None:
        self.preview_ctrl.stop()
        if self.project.state is RecorderState.RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause or stop recording before importing.")
            return

        filename, _ = QFileDialog.getOpenFileName(
            self, "Import Video Clip", str(Path.home() / "Videos"),
            "Video Files (*.mp4 *.avi *.mov *.mkv *.webm);;All Files (*)",
        )
        if not filename:
            return

        self._update_status("Importing...")
        QApplication.processEvents()

        try:
            cap = cv2.VideoCapture(filename)
            if not cap.isOpened():
                raise RuntimeError(f"Could not open video file: {filename}")

            imported = 0
            while True:
                ok, frame = cap.read()
                if not ok:
                    break
                self.project.add_frame(frame)
                imported += 1

            cap.release()

            if imported == 0:
                QMessageBox.warning(self, APP_NAME, "No frames could be read from the selected video.")
                return

            if self.project.state is RecorderState.IDLE:
                self.project.state = RecorderState.PAUSED
            self.project.set_timeline_index(self.project.frame_count())
            self._refresh_timeline()
            self._update_status(f"Imported {imported} frames from {Path(filename).name}")
            self._update_controls()
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not import video:\n\n{exc}")
            self._update_status("Import failed")

    def _save_as(self) -> None:
        self.preview_ctrl.stop()
        if self.project.state is RecorderState.RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause or stop recording before saving.")
            return
        if not self.project.has_frames():
            QMessageBox.warning(self, APP_NAME, "There are no recorded frames to save.")
            return

        default_dir = Path.home() / "Videos"
        if not default_dir.exists():
            default_dir = Path.home()

        filename, _ = QFileDialog.getSaveFileName(
            self, "Save Recording As", str(default_dir / "rewind_recording.mp4"), "MP4 Video (*.mp4)",
        )
        if not filename:
            return

        output_path = Path(filename)
        if output_path.suffix.lower() != ".mp4":
            output_path = output_path.with_suffix(".mp4")

        try:
            self._update_status("Saving...")
            QApplication.processEvents()
            result = self.exporter.export(
                self.project.snapshot_frame_paths(),
                self.audio.mix_segments(),
                output_path,
            )
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not save recording:\n\n{exc}")
            self._update_status("Save failed")
            return

        self.audio.clear(delete_files=True)
        self.project.clear_frames()
        self.autosaver.clear()
        self.project.state = RecorderState.IDLE
        self._refresh_timeline()
        self._update_controls()
        self.area_overlay.hide()
        self.floating_control.hide()

        notes = []
        if result.used_fallback_codec:
            notes.append(
                "OpenCV could not open the MP4 writer, so a local AVI intermediate "
                "was rendered and converted to MP4 with embedded audio."
            )
        if result.audio_muxed:
            notes.append("Audio was embedded inside the MP4.")

        msg = f"Saved video to:\n{result.path}"
        if notes:
            msg += "\n\n" + "\n\n".join(notes)
        QMessageBox.information(self, APP_NAME, msg)
        self._update_status("Saved")

    def _sync_overlay(self, apply_geometry: bool = False) -> None:
        if self.project.area is None:
            self.area_overlay.hide()
            self.floating_control.hide()
            self._overlay_area_key = None
            self._floating_area_key = None
            return

        area = self.project.area
        if apply_geometry or not self.area_overlay.isVisible() or self._overlay_area_key != area:
            self.area_overlay.set_area(area)
            self._overlay_area_key = area
        self.area_overlay.set_status(self.project.state)

        if apply_geometry or not self.floating_control.isVisible() or self._floating_area_key != area:
            self.floating_control.set_area(area)
            self._floating_area_key = area
        self.floating_control.set_state(self.project.state, True)

    def _maintain_topmost(self) -> None:
        if self.area_overlay.isVisible():
            force_widget_topmost(self.area_overlay)
            exclude_widget_from_capture(self.area_overlay)
        if self.floating_control.isVisible():
            force_widget_topmost(self.floating_control)
            exclude_widget_from_capture(self.floating_control)

    def _refresh_timeline(self) -> None:
        count = self.project.frame_count()
        index = self.project.get_timeline_index()

        self._updating_timeline = True
        self.timeline.setRange(0, count)
        self.timeline.set_selection(self.project.cut_start, self.project.cut_end)
        if not self._timeline_dragging:
            self.timeline.setValue(index, emit=False)
        self._updating_timeline = False

        self._update_timeline_labels()
        self._update_preview()

    def _update_timeline_labels(self) -> None:
        count = self.project.frame_count()
        index = self.project.get_timeline_index()
        current = format_seconds(index / self.project.fps)
        total = format_seconds(count / self.project.fps)
        self.time_label.setText(f"{current} / {total}")
        self.cut_label.setText(self._cut_label_text())

    def _update_preview(self) -> None:
        count = self.project.frame_count()
        index = self.project.get_timeline_index()

        if count == 0:
            self._last_preview_frame = None
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("Preview appears here after frames are recorded")
            return

        path = self.project.preview_frame_path(index)
        if path is None:
            self._last_preview_frame = None
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("Preview unavailable")
            return

        frame = cv2.imread(str(path))
        if frame is None:
            self._last_preview_frame = None
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("Could not read preview frame")
            return

        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        self._last_preview_frame = QImage(rgb.data, w, h, ch * w, QImage.Format_RGB888).copy()
        self._scale_preview_to_label()

    def _scale_preview_to_label(self) -> None:
        if self._last_preview_frame is None:
            return
        pixmap = QPixmap.fromImage(self._last_preview_frame).scaled(
            self.preview_label.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
        self.preview_label.setPixmap(pixmap)

    def _cut_label_text(self) -> str:
        start, end = self.project.cut_start, self.project.cut_end
        if start is None and end is None:
            return "Trim range: none"
        if start is None:
            return f"Trim range: Mark Out set at {format_seconds(end / self.project.fps)}. Set Mark In next."
        if end is None:
            return f"Trim range: Mark In set at {format_seconds(start / self.project.fps)}. Set Mark Out next."

        lo, hi = sorted((start, end))
        duration = (hi - lo) / self.project.fps
        if lo == hi:
            return f"Trim range: empty at {format_seconds(lo / self.project.fps)}"
        return (
            f"Trim range: {format_seconds(lo / self.project.fps)} → "
            f"{format_seconds(hi / self.project.fps)} ({format_seconds(duration)})"
        )

    def _update_status(self, text: str) -> None:
        self.setWindowTitle(f"{APP_NAME} — {text}")

    def _update_controls(self) -> None:
        state = self.project.state
        has_area = self.project.area is not None
        has_frames = self.project.has_frames()
        recording = state is RecorderState.RECORDING
        busy = self._state_transitioning
        playing = self.preview_ctrl.is_playing
        can_edit = not recording and has_frames

        self.select_button.setEnabled(not recording and not busy and not playing)
        self.play_preview_button.setEnabled(can_edit and not busy)
        if playing:
            self.play_preview_button.setIcon(self._icon("mdi6.stop"))
            self.play_preview_button.setText(" Stop")
        else:
            self.play_preview_button.setIcon(self._icon("mdi6.play"))
            self.play_preview_button.setText(" Play")
        self.stop_button.setEnabled(state is RecorderState.PAUSED and not busy and not playing)
        self.save_button.setEnabled(can_edit and not busy and not playing)
        self.import_button.setEnabled(not recording and not busy and not playing)

        for combo in (self.audio_input_combo, self.audio_output_combo):
            combo.setEnabled(not recording and not busy and not playing)
        self.refresh_audio_button.setEnabled(not recording and not busy and not playing)

        self.timeline.setEnabled(can_edit and not busy)

        for btn in (self.cut_start_button, self.cut_end_button):
            btn.setEnabled(can_edit and not busy and not playing)

        has_marks = self.project.cut_start is not None or self.project.cut_end is not None
        has_full_selection = (
            self.project.cut_start is not None
            and self.project.cut_end is not None
            and self.project.cut_start != self.project.cut_end
        )
        self.delete_cut_button.setEnabled(can_edit and has_full_selection and not busy and not playing)
        self.clear_cut_button.setEnabled(can_edit and has_marks and not busy and not playing)

        if self.floating_control.isVisible():
            self.floating_control.primary_button.setEnabled(
                has_area and state is not RecorderState.STOPPED and not busy and not playing
            )

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._scale_preview_to_label()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.preview_ctrl.stop()
        self._stop_worker()
        self.audio.stop_recording(end_frame=self.project.frame_count())
        if self.project.state is RecorderState.RECORDING:
            self.project.state = RecorderState.PAUSED if self.project.has_frames() else RecorderState.IDLE
        self.project.set_timeline_index(self.project.frame_count())

        try:
            if self.project.has_frames() or self.project.area is not None:
                self.autosaver.save(self.locked_capture_area)
            else:
                self.autosaver.clear()
        except Exception as exc:
            QMessageBox.critical(
                self, APP_NAME,
                f"Could not save the local recording project for restore.\n\n"
                f"{exc}\n\nThe app will stay open so the recording is not lost.",
            )
            event.ignore()
            return

        self.area_overlay.close()
        self.floating_control.close()
        event.accept()
