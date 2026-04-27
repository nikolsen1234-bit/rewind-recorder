from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path
from typing import Optional

import cv2
import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QImage, QPixmap
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

from rewind_recorder.audio import LocalMicrophoneRecorder, LocalSystemAudioRecorder
from rewind_recorder.capture import CaptureWorker
from rewind_recorder.config import (
    APP_NAME,
    AUDIO_CHANNELS,
    AUDIO_SAMPLE_RATE,
    AUTOSAVE_FILENAME,
    DEFAULT_FPS,
    DEFAULT_OUTPUT_HEIGHT,
    DEFAULT_OUTPUT_WIDTH,
    STATE_IDLE,
    STATE_PAUSED,
    STATE_RECORDING,
    STATE_STOPPED,
)
from rewind_recorder.errors import VideoWriterOpenError
from rewind_recorder.playback import AudioPlaybackWorker
from rewind_recorder.project import FrameProject
from rewind_recorder.timecode import format_seconds
from rewind_recorder.widgets import AreaSelector, CaptureAreaOverlay, FloatingRecorderControl, TrimTimeline
from rewind_recorder.windows_api import exclude_widget_from_capture, force_widget_topmost


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.project = FrameProject(DEFAULT_FPS)
        self.capture_worker: Optional[CaptureWorker] = None
        self.selector: Optional[AreaSelector] = None
        self.area_overlay = CaptureAreaOverlay()
        self.floating_control = FloatingRecorderControl()
        self._updating_timeline = False
        self._timeline_dragging = False
        self._state_transitioning = False
        self._overlay_area_key: Optional[tuple[int, int, int, int]] = None
        self._floating_area_key: Optional[tuple[int, int, int, int]] = None
        self.locked_capture_area: Optional[dict[str, int]] = None
        self.audio_recorders: list[tuple[str, object]] = []
        self.audio_segments: list[dict[str, object]] = []
        self.current_audio_start_frame: Optional[int] = None
        self.preview_audio_worker: Optional[AudioPlaybackWorker] = None
        self.preview_playback_timer = QTimer(self)
        self.preview_playback_timer.setInterval(max(1, int(round(1000 / self.project.fps))))
        self.preview_playback_timer.timeout.connect(self.advance_preview_playback)
        self.preview_playback_index = 0
        self.preview_playback_start_index = 0
        self.preview_playback_started_at = 0.0
        self.preview_audio_path: Optional[Path] = None
        self.topmost_timer = QTimer(self)
        self.topmost_timer.setInterval(1000)
        self.topmost_timer.timeout.connect(self.maintain_recorder_windows)
        self.topmost_timer.start()

        self.setWindowTitle(APP_NAME)
        self.resize(900, 660)
        self.build_ui()
        restored = self.restore_autosave_project()
        self.refresh_timeline()
        self.update_controls()
        if restored:
            self.update_status("Restored previous recording")
            self.sync_area_overlay(apply_geometry=True)

    def build_ui(self) -> None:
        central = QWidget()
        layout = QVBoxLayout(central)

        self.area_label = QLabel("No area selected")
        self.status_label = QLabel("Status: Idle")
        self.time_label = QLabel("00:00.000 / 00:00.000")
        self.cut_label = QLabel("Trim range: none")
        self.preview_caption = QLabel("Preview: no frames yet")

        self.preview_label = QLabel("Preview appears here after frames are recorded")
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setFixedSize(860, 390)
        self.preview_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.preview_label.setStyleSheet(
            "QLabel { background: #111; color: #ddd; border: 1px solid #555; padding: 12px; }"
        )

        self.timeline = TrimTimeline()
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.valueChanged.connect(self.on_timeline_changed)
        self.timeline.selectionChanged.connect(self.on_trim_selection_changed)
        self.timeline.sliderPressed.connect(self.on_timeline_pressed)
        self.timeline.sliderReleased.connect(self.on_timeline_released)

        timeline_header = QHBoxLayout()
        timeline_title = QLabel("Timeline")
        timeline_title.setStyleSheet("font-weight: 700;")
        timeline_header.addWidget(timeline_title)
        timeline_header.addStretch(1)
        timeline_header.addWidget(self.time_label)

        self.audio_input_combo = QComboBox()
        self.audio_output_combo = QComboBox()
        self.refresh_audio_button = QPushButton("Refresh Audio")
        self.refresh_audio_button.setFixedWidth(110)
        self.refresh_audio_button.clicked.connect(self.refresh_audio_devices)

        self.select_button = QPushButton("Select Area")
        self.record_button = QPushButton("Start Recording")
        self.play_preview_button = QPushButton("Play")
        self.stop_button = QPushButton("Stop")
        self.save_button = QPushButton("Save")
        self.cut_start_button = QPushButton("Mark In")
        self.cut_end_button = QPushButton("Mark Out")
        self.delete_cut_button = QPushButton("Delete Range")
        self.clear_cut_button = QPushButton("Clear Range")

        self.select_button.setFixedWidth(90)
        self.record_button.setFixedWidth(135)
        self.play_preview_button.setFixedWidth(90)
        self.stop_button.setFixedWidth(90)
        self.save_button.setFixedWidth(90)
        self.cut_start_button.setFixedWidth(90)
        self.cut_end_button.setFixedWidth(90)
        self.delete_cut_button.setFixedWidth(120)
        self.clear_cut_button.setFixedWidth(110)

        self.select_button.clicked.connect(self.select_area)
        self.floating_control.primary_clicked.connect(self.primary_action)
        self.floating_control.stop_clicked.connect(self.stop_recording)
        self.record_button.clicked.connect(self.record_or_resume)
        self.play_preview_button.clicked.connect(self.toggle_preview_playback)
        self.stop_button.clicked.connect(self.stop_recording)
        self.save_button.clicked.connect(self.save_as)
        self.cut_start_button.clicked.connect(self.set_cut_start)
        self.cut_end_button.clicked.connect(self.set_cut_end)
        self.delete_cut_button.clicked.connect(self.delete_selected_range)
        self.clear_cut_button.clicked.connect(self.clear_cut_selection)

        audio_row = QHBoxLayout()
        audio_row.addWidget(QLabel("Input device"))
        audio_row.addWidget(self.audio_input_combo, 1)
        audio_row.addWidget(QLabel("Output device"))
        audio_row.addWidget(self.audio_output_combo, 1)
        audio_row.addWidget(self.refresh_audio_button)

        transport_row = QHBoxLayout()
        transport_row.addWidget(self.select_button)
        transport_row.addWidget(self.record_button)
        transport_row.addWidget(self.play_preview_button)
        transport_row.addWidget(self.stop_button)
        transport_row.addWidget(self.save_button)
        transport_row.addStretch(1)

        self.edit_controls = QWidget()
        edit_layout = QVBoxLayout(self.edit_controls)
        edit_layout.setContentsMargins(0, 0, 0, 0)

        edit_row = QHBoxLayout()
        trim_title = QLabel("Trim")
        trim_title.setStyleSheet("font-weight: 700;")
        edit_row.addWidget(trim_title)
        edit_row.addWidget(self.cut_start_button)
        edit_row.addWidget(self.cut_end_button)
        edit_row.addWidget(self.delete_cut_button)
        edit_row.addWidget(self.clear_cut_button)
        edit_row.addStretch(1)
        edit_layout.addLayout(edit_row)

        layout.addWidget(self.area_label)
        layout.addLayout(audio_row)
        layout.addWidget(self.preview_label, 0, Qt.AlignHCenter)
        layout.addWidget(self.preview_caption)
        layout.addLayout(timeline_header)
        layout.addWidget(self.timeline)
        layout.addWidget(self.cut_label)
        layout.addWidget(self.edit_controls)
        layout.addWidget(self.status_label)
        layout.addLayout(transport_row)

        self.setCentralWidget(central)
        self.refresh_audio_devices()

    def normalize_area(self, area: dict[str, int]) -> dict[str, int]:
        width = max(2, int(area["width"]))
        height = max(2, int(area["height"]))

        if width % 2:
            width -= 1
        if height % 2:
            height -= 1

        return {
            "x": int(area["x"]),
            "y": int(area["y"]),
            "width": max(2, width),
            "height": max(2, height),
        }

    def refresh_audio_devices(self) -> None:
        current_input = self.audio_input_combo.currentData() if hasattr(self, "audio_input_combo") else None
        current_output = self.audio_output_combo.currentData() if hasattr(self, "audio_output_combo") else None

        self.audio_input_combo.blockSignals(True)
        self.audio_output_combo.blockSignals(True)
        self.audio_input_combo.clear()
        self.audio_output_combo.clear()

        try:
            import sounddevice as sd

            input_devices = self.windows_input_devices(sd)
            if not input_devices:
                self.audio_input_combo.addItem("No input devices found", None)
            for index, name in input_devices:
                self.audio_input_combo.addItem(name, index)
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

        self.restore_combo_selection(self.audio_input_combo, current_input)
        self.restore_combo_selection(self.audio_output_combo, current_output)
        self.audio_input_combo.blockSignals(False)
        self.audio_output_combo.blockSignals(False)

    def windows_input_devices(self, sd) -> list[tuple[int, str]]:
        devices = sd.query_devices()
        hostapis = sd.query_hostapis()
        wasapi_indexes = {
            index for index, hostapi in enumerate(hostapis) if str(hostapi.get("name", "")).lower() == "windows wasapi"
        }

        candidates: list[tuple[int, str]] = []
        for index, device in enumerate(devices):
            if int(device.get("max_input_channels", 0)) <= 0:
                continue
            if wasapi_indexes and int(device.get("hostapi", -1)) not in wasapi_indexes:
                continue
            candidates.append((index, self.clean_audio_device_name(str(device.get("name", f"Input {index}")))))

        if not candidates:
            for index, device in enumerate(devices):
                if int(device.get("max_input_channels", 0)) > 0:
                    candidates.append((index, self.clean_audio_device_name(str(device.get("name", f"Input {index}")))))

        seen: set[str] = set()
        unique: list[tuple[int, str]] = []
        for index, name in candidates:
            key = name.lower()
            if key in seen:
                continue
            seen.add(key)
            unique.append((index, name))
        return unique

    def clean_audio_device_name(self, name: str) -> str:
        return " ".join(name.replace("\r", " ").replace("\n", " ").split())

    def restore_combo_selection(self, combo: QComboBox, value: object) -> None:
        for index in range(combo.count()):
            if combo.itemData(index) == value:
                combo.setCurrentIndex(index)
                return
        combo.setCurrentIndex(0)

    def selected_audio_input_device(self) -> Optional[int]:
        value = self.audio_input_combo.currentData()
        return int(value) if value is not None else None

    def selected_audio_output_device(self) -> Optional[str]:
        value = self.audio_output_combo.currentData()
        return str(value) if value is not None else None

    def app_data_dir(self) -> Path:
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        else:
            base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        return base / "RewindRecorder"

    def autosave_path(self) -> Path:
        return self.app_data_dir() / AUTOSAVE_FILENAME

    def serialize_audio_segment(self, segment: dict[str, object]) -> dict[str, object]:
        return {
            key: str(value) if key == "path" else value
            for key, value in segment.items()
        }

    def autosave_project(self) -> None:
        autosave_path = self.autosave_path()
        autosave_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": 1,
            "fps": self.project.fps,
            "area": self.project.area,
            "locked_capture_area": self.locked_capture_area,
            "temp_dir": str(self.project.temp_dir) if self.project.temp_dir is not None else None,
            "frames": [str(path) for path in self.project.snapshot_frame_paths()],
            "timeline_index": self.project.get_timeline_index(),
            "cut_start": self.project.cut_start,
            "cut_end": self.project.cut_end,
            "next_frame_id": self.project.next_frame_id,
            "audio_segments": [self.serialize_audio_segment(segment) for segment in self.audio_segments],
        }
        temp_path = autosave_path.with_suffix(".tmp")
        temp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp_path.replace(autosave_path)

    def clear_autosave_project(self) -> None:
        path = self.autosave_path()
        path.unlink(missing_ok=True)
        tmp_path = path.with_suffix(".tmp")
        tmp_path.unlink(missing_ok=True)

    def restore_autosave_project(self) -> bool:
        path = self.autosave_path()
        if not path.exists():
            return False

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            frame_paths = [Path(value) for value in data.get("frames", [])]
            frame_paths = [frame_path for frame_path in frame_paths if frame_path.exists()]
            area = data.get("area")

            if area is None and not frame_paths:
                self.clear_autosave_project()
                return False

            self.project.fps = int(data.get("fps", DEFAULT_FPS))
            self.project.area = self.normalize_area(area) if area is not None else None
            self.project.temp_dir = Path(data["temp_dir"]) if data.get("temp_dir") else None
            self.project.frames = frame_paths
            self.project.timeline_index = max(0, min(int(data.get("timeline_index", len(frame_paths))), len(frame_paths)))
            self.project.cut_start = self.restore_optional_frame_index(data.get("cut_start"), len(frame_paths))
            self.project.cut_end = self.restore_optional_frame_index(data.get("cut_end"), len(frame_paths))
            self.project.next_frame_id = max(int(data.get("next_frame_id", 0)), self.next_frame_id_from_frames(frame_paths))
            self.project.state = STATE_PAUSED if frame_paths else STATE_IDLE
            locked_area = data.get("locked_capture_area") or self.project.area
            self.locked_capture_area = self.normalize_area(locked_area) if locked_area is not None else None
            self.audio_segments = self.restore_audio_segments(data.get("audio_segments", []), len(frame_paths))
            self.current_audio_start_frame = None

            if self.project.area is not None:
                self.area_label.setText(
                    "Capture area: "
                    f"x={self.project.area['x']}, y={self.project.area['y']}, "
                    f"width={self.project.area['width']}, height={self.project.area['height']}"
                )
            return True
        except Exception as exc:
            self.update_status("Could not restore previous recording")
            QMessageBox.warning(
                self,
                APP_NAME,
                "Could not restore the previous local recording project.\n\n"
                f"{exc}\n\nThe autosave file was kept here:\n{path}",
            )
            return False

    def restore_optional_frame_index(self, value: object, frame_count: int) -> Optional[int]:
        if value is None:
            return None
        return max(0, min(int(value), frame_count))

    def next_frame_id_from_frames(self, frame_paths: list[Path]) -> int:
        highest = -1
        for frame_path in frame_paths:
            try:
                highest = max(highest, int(frame_path.stem.rsplit("_", 1)[1]))
            except (IndexError, ValueError):
                continue
        return highest + 1

    def restore_audio_segments(self, segments: object, frame_count: int) -> list[dict[str, object]]:
        restored: list[dict[str, object]] = []
        if not isinstance(segments, list):
            return restored

        for segment in segments:
            if not isinstance(segment, dict):
                continue
            path = Path(str(segment.get("path", "")))
            if not path.exists():
                continue

            restored_segment: dict[str, object] = {"path": path}
            restored_segment["source_name"] = str(segment.get("source_name", "Audio"))
            for key in (
                "record_start_frame",
                "source_start_frame",
                "source_end_frame",
                "timeline_start_frame",
                "timeline_end_frame",
                "sample_rate",
                "channels",
            ):
                restored_segment[key] = int(segment.get(key, 0))

            start = max(0, min(int(restored_segment["timeline_start_frame"]), frame_count))
            end = max(start, min(int(restored_segment["timeline_end_frame"]), frame_count))
            if end == start:
                continue
            restored_segment["timeline_start_frame"] = start
            restored_segment["timeline_end_frame"] = end
            restored.append(restored_segment)

        return restored

    def record_or_resume(self) -> None:
        self.stop_preview_playback()
        if self.project.state == STATE_RECORDING:
            self.pause_recording()
        elif self.project.state == STATE_PAUSED:
            self.resume_from_here()
        elif self.project.state == STATE_STOPPED:
            self.start_recording()
        else:
            self.start_recording()

    def primary_action(self) -> None:
        self.stop_preview_playback()
        if self.project.state == STATE_RECORDING:
            self.pause_recording()
        elif self.project.state == STATE_PAUSED:
            self.resume_from_here()
        else:
            self.start_recording()

    def area_key(self, area: dict[str, int]) -> tuple[int, int, int, int]:
        return (area["x"], area["y"], area["width"], area["height"])

    def begin_state_transition(self) -> bool:
        if self._state_transitioning:
            return False
        self._state_transitioning = True
        self.update_controls()
        QApplication.processEvents()
        return True

    def end_state_transition(self) -> None:
        self._state_transitioning = False
        self.update_controls()

    def sync_area_overlay(self, apply_geometry: bool = False) -> None:
        if self.project.area is None:
            self.area_overlay.hide()
            self.floating_control.hide()
            self._overlay_area_key = None
            self._floating_area_key = None
            return

        area_key = self.area_key(self.project.area)
        if apply_geometry or not self.area_overlay.isVisible() or self._overlay_area_key != area_key:
            self.area_overlay.set_area(self.project.area)
            self._overlay_area_key = area_key
        self.area_overlay.set_status(self.project.state)

        if apply_geometry or not self.floating_control.isVisible() or self._floating_area_key != area_key:
            self.floating_control.set_area(self.project.area)
            self._floating_area_key = area_key
        self.floating_control.set_state(self.project.state, True)

    def maintain_recorder_windows(self) -> None:
        if self.area_overlay.isVisible():
            force_widget_topmost(self.area_overlay)
            exclude_widget_from_capture(self.area_overlay)
        if self.floating_control.isVisible():
            force_widget_topmost(self.floating_control)
            exclude_widget_from_capture(self.floating_control)

    def select_area(self) -> None:
        self.stop_preview_playback()
        if self.project.state == STATE_RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause or stop recording before selecting a new area.")
            return

        if self.project.has_frames():
            reply = QMessageBox.question(
                self,
                APP_NAME,
                "Selecting a new area clears the current temporary recording. Continue?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if reply != QMessageBox.Yes:
                return
            self.clear_audio(delete_files=True)
            self.project.clear_frames()
            self.clear_autosave_project()
            self.project.state = STATE_IDLE
            self.locked_capture_area = self.project.area.copy() if self.project.area is not None else None
            self.refresh_timeline()
            self.update_status("Idle")
            self.area_overlay.hide()

        self.selector = AreaSelector()
        self.selector.area_selected.connect(self.on_area_selected)
        self.selector.show()
        force_widget_topmost(self.selector)
        self.selector.activateWindow()

    def on_area_selected(self, area: dict) -> None:
        normalized = self.normalize_area(area)
        self.project.set_area(normalized)
        self.locked_capture_area = normalized.copy()
        self.area_label.setText(
            "Capture area: "
            f"x={normalized['x']}, y={normalized['y']}, "
            f"width={normalized['width']}, height={normalized['height']}"
        )
        self.update_status("Area selected")
        self.sync_area_overlay(apply_geometry=True)
        self.update_controls()

    def start_recording(self) -> None:
        if self.project.area is None:
            QMessageBox.warning(self, APP_NAME, "Select a screen area before recording.")
            return
        if self.project.state == STATE_RECORDING:
            return
        if not self.begin_state_transition():
            return

        try:
            if self.project.has_frames():
                reply = QMessageBox.question(
                    self,
                    APP_NAME,
                    "Start a new recording and delete the current temporary frames?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if reply != QMessageBox.Yes:
                    return
                self.clear_audio(delete_files=True)
                self.project.clear_frames()
                self.clear_autosave_project()
                self.locked_capture_area = self.project.area.copy() if self.project.area is not None else None

            self.project.set_timeline_index(0)
            self.project.reset_cut_marks()
            self.begin_capture()
        finally:
            self.end_state_transition()

    def begin_capture(self) -> None:
        if self.project.area is None:
            QMessageBox.warning(self, APP_NAME, "Select a screen area before recording.")
            return
        if self.capture_worker is not None and self.capture_worker.isRunning():
            return

        capture_area = (self.locked_capture_area or self.project.area).copy()
        self.locked_capture_area = capture_area.copy()
        self.project.set_area(capture_area)
        self.capture_worker = CaptureWorker(self.project, capture_area, self.project.fps)
        self.capture_worker.frame_saved.connect(self.on_frame_saved)
        self.capture_worker.capture_error.connect(self.on_capture_error)
        self.capture_worker.finished.connect(self.on_worker_finished)
        self.project.state = STATE_RECORDING
        self.sync_area_overlay()
        audio_started = self.start_audio_recording()
        self.capture_worker.start()
        self.update_status("Recording + audio" if audio_started else "Recording video only")
        self.update_controls()

    def pause_recording(self) -> None:
        if self.project.state != STATE_RECORDING:
            return
        self.stop_preview_playback()
        if not self.begin_state_transition():
            return

        try:
            self.stop_worker()
            self.stop_audio_recording(end_frame=self.project.frame_count())
            self.project.state = STATE_PAUSED
            self.project.set_timeline_index(self.project.frame_count())
            self.refresh_timeline()
            self.update_status("Paused")
            self.sync_area_overlay()
        finally:
            self.end_state_transition()

    def resume_from_here(self) -> None:
        if self.project.state != STATE_PAUSED:
            return
        self.stop_preview_playback()
        if self.project.area is None:
            QMessageBox.warning(self, APP_NAME, "Select a screen area before recording.")
            return
        if not self.begin_state_transition():
            return

        try:
            index = self.timeline.value()
            self.discard_audio_after(index)
            removed = self.project.truncate_after(index)
            self.refresh_timeline()
            if removed:
                self.update_status(
                    f"Deleted {removed} frame(s); recording from {format_seconds(index / self.project.fps)}"
                )
            self.begin_capture()
        finally:
            self.end_state_transition()

    def stop_recording(self) -> None:
        if self.project.state != STATE_PAUSED:
            return
        self.stop_preview_playback()
        if not self.begin_state_transition():
            return

        try:
            self.project.state = STATE_STOPPED
            self.project.set_timeline_index(self.project.frame_count())
            self.refresh_timeline()
            self.update_status("Stopped")
            self.area_overlay.hide()
            self.floating_control.hide()
        finally:
            self.end_state_transition()

    def stop_worker(self) -> None:
        worker = self.capture_worker
        if worker is None:
            return

        worker.stop()
        worker.wait(3000)
        if worker.isRunning():
            QMessageBox.warning(self, APP_NAME, "Recording thread did not stop within 3 seconds.")
        self.capture_worker = None

    def start_audio_recording(self) -> bool:
        self.stop_audio_recording(end_frame=self.project.frame_count())

        temp_dir = self.project.ensure_temp_dir()
        input_device = self.selected_audio_input_device()
        output_device = self.selected_audio_output_device()
        self.current_audio_start_frame = self.project.frame_count()

        microphone = LocalMicrophoneRecorder(
            output_dir=temp_dir,
            sample_rate=self.default_input_sample_rate(input_device),
            channels=AUDIO_CHANNELS,
            device=input_device,
        )
        system_audio = LocalSystemAudioRecorder(
            output_dir=temp_dir,
            sample_rate=AUDIO_SAMPLE_RATE,
            channels=2,
            speaker_id=output_device,
        )

        self.audio_recorders = []
        errors: list[str] = []
        if microphone.start():
            self.audio_recorders.append(("Microphone", microphone))
        else:
            errors.append(f"microphone: {microphone.last_error}")
            microphone.cleanup(delete_file=True)

        if system_audio.start():
            self.audio_recorders.append(("System audio", system_audio))
        else:
            errors.append(f"system audio: {system_audio.last_error}")
            system_audio.cleanup(delete_file=True)

        if self.audio_recorders:
            if errors:
                self.update_status(f"Recording with partial audio - {'; '.join(errors)}")
            return True

        self.current_audio_start_frame = None
        self.update_status(f"Recording video only - audio unavailable: {'; '.join(errors)}")
        return False

    def default_input_sample_rate(self, device: Optional[int]) -> int:
        try:
            import sounddevice as sd

            device_info = sd.query_devices(device, kind="input")
            sample_rate = int(round(float(device_info.get("default_samplerate", AUDIO_SAMPLE_RATE))))
            if sample_rate > 0:
                return sample_rate
        except Exception:
            pass
        return AUDIO_SAMPLE_RATE

    def stop_audio_recording(self, end_frame: Optional[int] = None, keep_segment: bool = True) -> None:
        recorders = list(self.audio_recorders)
        if not recorders:
            return

        start_frame = self.current_audio_start_frame
        self.audio_recorders = []
        self.current_audio_start_frame = None

        for source_name, recorder in recorders:
            audio_info = recorder.stop()
            if not keep_segment:
                recorder.cleanup(delete_file=True)
                continue

            if audio_info.output_path is None or audio_info.frames_written <= 0:
                recorder.cleanup(delete_file=True)
                continue

            start = max(0, int(start_frame or 0))
            end = self.project.frame_count() if end_frame is None else max(0, int(end_frame))
            end = max(start, end)
            if end == start:
                recorder.cleanup(delete_file=True)
                continue

            self.audio_segments.append(
                {
                    "path": Path(audio_info.output_path),
                    "source_name": source_name,
                    "record_start_frame": start,
                    "source_start_frame": start,
                    "source_end_frame": end,
                    "timeline_start_frame": start,
                    "timeline_end_frame": end,
                    "sample_rate": audio_info.sample_rate,
                    "channels": audio_info.channels,
                }
            )

    def discard_audio_after(self, frame_index: int) -> None:
        frame_index = max(0, int(frame_index))
        old_paths = {Path(segment["path"]) for segment in self.audio_segments}
        kept_segments: list[dict[str, object]] = []

        for segment in self.audio_segments:
            start = int(segment["timeline_start_frame"])
            end = int(segment["timeline_end_frame"])
            if start >= frame_index:
                continue
            if end > frame_index:
                kept = dict(segment)
                kept["timeline_end_frame"] = frame_index
                kept["source_end_frame"] = int(segment["source_start_frame"]) + (frame_index - start)
                kept_segments.append(kept)
            else:
                kept_segments.append(segment)

        self.audio_segments = kept_segments
        self.delete_unreferenced_audio_files(old_paths)

    def delete_audio_range(self, start: int, end: int) -> None:
        start, end = sorted((max(0, int(start)), max(0, int(end))))
        if start == end:
            return

        removed_frames = end - start
        updated_segments: list[dict[str, object]] = []

        for segment in self.audio_segments:
            seg_start = int(segment["timeline_start_frame"])
            seg_end = int(segment["timeline_end_frame"])
            source_start = int(segment["source_start_frame"])
            source_end = int(segment["source_end_frame"])

            if seg_end <= start:
                updated_segments.append(segment)
                continue

            if seg_start >= end:
                shifted = dict(segment)
                shifted["timeline_start_frame"] = seg_start - removed_frames
                shifted["timeline_end_frame"] = seg_end - removed_frames
                updated_segments.append(shifted)
                continue

            if seg_start < start:
                left = dict(segment)
                left["timeline_end_frame"] = start
                left["source_end_frame"] = source_start + (start - seg_start)
                updated_segments.append(left)

            if seg_end > end:
                right = dict(segment)
                right_frames = seg_end - end
                right["timeline_start_frame"] = start
                right["timeline_end_frame"] = start + right_frames
                right["source_start_frame"] = source_start + (end - seg_start)
                right["source_end_frame"] = source_end
                updated_segments.append(right)

        self.audio_segments = updated_segments

    def clear_audio(self, delete_files: bool = False) -> None:
        self.stop_audio_recording(end_frame=self.project.frame_count(), keep_segment=not delete_files)
        for _source_name, recorder in list(self.audio_recorders):
            recorder.cleanup(delete_file=delete_files)
        self.audio_recorders = []
        paths = {Path(segment["path"]) for segment in self.audio_segments}
        self.audio_segments = []
        self.current_audio_start_frame = None

        if delete_files:
            for path in paths:
                path.unlink(missing_ok=True)

    def delete_unreferenced_audio_files(self, old_paths: set[Path]) -> None:
        current_paths = {Path(segment["path"]) for segment in self.audio_segments}
        for path in old_paths - current_paths:
            path.unlink(missing_ok=True)

    def on_worker_finished(self) -> None:
        if self.capture_worker is self.sender():
            self.capture_worker = None

        if self.project.state == STATE_RECORDING:
            self.stop_audio_recording(end_frame=self.project.frame_count())
            self.project.state = STATE_PAUSED if self.project.has_frames() else STATE_IDLE
            self.refresh_timeline()
            self.update_status("Recording stopped")
            self.sync_area_overlay()
            self.update_controls()

    def on_capture_error(self, message: str) -> None:
        self.stop_audio_recording(end_frame=self.project.frame_count())
        self.project.state = STATE_PAUSED if self.project.has_frames() else STATE_IDLE
        self.refresh_timeline()
        self.update_status("Capture error")
        self.sync_area_overlay()
        self.update_controls()
        QMessageBox.critical(self, APP_NAME, f"Screen capture failed:\n\n{message}")

    def on_frame_saved(self, frame_count: int) -> None:
        if self.project.state != STATE_RECORDING:
            return

        self.project.set_timeline_index(frame_count)
        self.refresh_timeline()
        self.update_status(f"Recording - {format_seconds(frame_count / self.project.fps)}")

    def on_timeline_changed(self, value: int) -> None:
        if self._updating_timeline or self.project.state == STATE_RECORDING:
            return
        if self.is_preview_playing():
            self.stop_preview_playback()

        self.project.set_timeline_index(value)
        self.update_timeline_labels()
        self.update_preview()

    def on_timeline_pressed(self) -> None:
        if self.is_preview_playing():
            self.stop_preview_playback()
        self._timeline_dragging = True

    def on_timeline_released(self) -> None:
        self._timeline_dragging = False
        if self.project.state == STATE_RECORDING:
            return

        self.project.set_timeline_index(self.timeline.value())
        self.refresh_timeline()

    def on_trim_selection_changed(self, start: Optional[int], end: Optional[int]) -> None:
        if self.project.state == STATE_RECORDING:
            return

        self.project.cut_start = start
        self.project.cut_end = end
        self.update_timeline_labels()
        self.timeline.set_selection(start, end)
        self.update_controls()

    def set_cut_start(self) -> None:
        if self.project.state == STATE_RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause or stop recording before setting Mark In.")
            return
        if not self.project.has_frames():
            QMessageBox.warning(self, APP_NAME, "Record frames before setting Mark In.")
            return

        self.project.cut_start = self.timeline.value()
        self.refresh_timeline()
        self.update_status(f"Mark In set at {format_seconds(self.project.cut_start / self.project.fps)}")
        self.update_controls()

    def set_cut_end(self) -> None:
        if self.project.state == STATE_RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause or stop recording before setting Mark Out.")
            return
        if not self.project.has_frames():
            QMessageBox.warning(self, APP_NAME, "Record frames before setting Mark Out.")
            return

        self.project.cut_end = self.timeline.value()
        self.refresh_timeline()
        self.update_status(f"Mark Out set at {format_seconds(self.project.cut_end / self.project.fps)}")
        self.update_controls()

    def clear_cut_selection(self) -> None:
        self.project.reset_cut_marks()
        self.refresh_timeline()
        self.update_status("Trim range cleared")
        self.update_controls()

    def delete_selected_range(self) -> None:
        if self.project.state == STATE_RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause or stop recording before deleting a trim range.")
            return

        if self.project.cut_start is None or self.project.cut_end is None:
            QMessageBox.warning(self, APP_NAME, "Set both Mark In and Mark Out before deleting the trim range.")
            return

        start = self.project.cut_start
        end = self.project.cut_end
        if start == end:
            QMessageBox.warning(self, APP_NAME, "Mark In and Mark Out are the same position, so nothing is selected.")
            return

        start_for_message, end_for_message = sorted((start, end))
        duration = (end_for_message - start_for_message) / self.project.fps
        reply = QMessageBox.question(
            self,
            APP_NAME,
            "Delete the marked trim range?\n\n"
            f"{format_seconds(start_for_message / self.project.fps)} to "
            f"{format_seconds(end_for_message / self.project.fps)} "
            f"({format_seconds(duration)})",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        self.delete_audio_range(start, end)
        removed = self.project.delete_range(start, end)
        self.refresh_timeline()
        self.update_status(f"Deleted trim range ({removed} frame(s))")
        self.update_controls()

    def save_as(self) -> None:
        self.stop_preview_playback()
        if self.project.state == STATE_RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause or stop recording before saving.")
            return
        if not self.project.has_frames():
            QMessageBox.warning(self, APP_NAME, "There are no recorded frames to save.")
            return

        default_dir = Path.home() / "Videos"
        if not default_dir.exists():
            default_dir = Path.home()
        default_path = default_dir / "rewind_recording.mp4"

        filename, _selected_filter = QFileDialog.getSaveFileName(
            self,
            "Save Recording As",
            str(default_path),
            "MP4 Video (*.mp4)",
        )
        if not filename:
            return

        output_path = Path(filename)
        if output_path.suffix == "":
            output_path = output_path.with_suffix(".mp4")
        elif output_path.suffix.lower() != ".mp4":
            output_path = output_path.with_suffix(".mp4")

        try:
            self.update_status("Saving...")
            QApplication.processEvents()
            saved_path, used_fallback = self.render_video(output_path)
            audio_path = self.build_audio_mix(saved_path.with_suffix(".wav"))
            audio_muxed = False
            audio_note = ""
            if audio_path is not None:
                saved_path, audio_muxed, audio_note = self.mux_audio_into_video(saved_path, audio_path)
                if not audio_muxed:
                    raise RuntimeError(
                        "This recording has audio, but the audio could not be embedded into the video.\n\n"
                        f"{audio_note}"
                    )
        except Exception as exc:
            QMessageBox.critical(self, APP_NAME, f"Could not save recording:\n\n{exc}")
            self.update_status("Save failed")
            return

        self.clear_audio(delete_files=True)
        self.project.clear_frames()
        self.clear_autosave_project()
        self.project.state = STATE_IDLE
        self.refresh_timeline()
        self.update_controls()
        self.area_overlay.hide()
        self.floating_control.hide()

        fallback_note = (
            "\n\nOpenCV could not open the MP4 writer, so the app rendered a local AVI intermediate "
            "and converted it to MP4 with embedded audio."
            if used_fallback
            else ""
        )
        audio_save_note = "\n\nAudio was embedded inside the MP4." if audio_muxed else ""

        QMessageBox.information(self, APP_NAME, f"Saved video to:\n{saved_path}{fallback_note}{audio_save_note}")
        self.update_status("Saved")

    def render_video(self, requested_path: Path) -> tuple[Path, bool]:
        frame_paths = self.project.snapshot_frame_paths()
        if not frame_paths:
            raise RuntimeError("No frames are available.")

        first_frame = cv2.imread(str(frame_paths[0]))
        if first_frame is None:
            raise RuntimeError(f"Could not read first frame: {frame_paths[0]}")

        height, width = first_frame.shape[:2]
        fps = float(self.project.fps)
        output_width = DEFAULT_OUTPUT_WIDTH
        output_height = DEFAULT_OUTPUT_HEIGHT

        if requested_path.suffix.lower() == ".avi":
            return self.write_video(
                requested_path,
                "XVID",
                frame_paths,
                fps,
                width,
                height,
                output_width,
                output_height,
            ), False

        mp4_path = requested_path.with_suffix(".mp4")
        try:
            return self.write_video(
                mp4_path,
                "mp4v",
                frame_paths,
                fps,
                width,
                height,
                output_width,
                output_height,
            ), False
        except VideoWriterOpenError:
            avi_path = self.unique_fallback_path(requested_path.with_suffix(".avi"))
            return self.write_video(
                avi_path,
                "XVID",
                frame_paths,
                fps,
                width,
                height,
                output_width,
                output_height,
            ), True

    def audio_mix_segments(self) -> list[dict[str, object]]:
        return [
            dict(segment)
            for segment in self.audio_segments
            if Path(segment["path"]).exists()
            and int(segment["timeline_end_frame"]) > int(segment["timeline_start_frame"])
        ]

    def audio_source_names(self, segments: list[dict[str, object]]) -> list[str]:
        names: list[str] = []
        for segment in segments:
            name = str(segment.get("source_name", "Audio"))
            if name not in names:
                names.append(name)
        return names

    def has_microphone_audio(self, segments: list[dict[str, object]]) -> bool:
        return any(str(segment.get("source_name", "")).lower() == "microphone" for segment in segments)

    def build_audio_mix(
        self,
        output_path: Path,
        segments: Optional[list[dict[str, object]]] = None,
        *,
        allow_silence: bool = True,
    ) -> Optional[Path]:
        frame_count = self.project.frame_count()
        if frame_count <= 0:
            return None

        total_duration = frame_count / self.project.fps
        segments = self.audio_mix_segments() if segments is None else [dict(segment) for segment in segments]
        if not segments:
            if not allow_silence:
                return None
            return self.build_silent_audio(output_path, total_duration)

        ffmpeg = self.find_ffmpeg_executable()
        if ffmpeg is None:
            raise RuntimeError("Embedding audio requires local FFmpeg from imageio-ffmpeg.")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
        segments.sort(key=lambda segment: int(segment["timeline_start_frame"]))

        command = [ffmpeg, "-y"]
        filters: list[str] = []
        labels: list[str] = []
        for index, segment in enumerate(segments):
            command.extend(["-i", str(Path(segment["path"]))])
            record_start = int(segment["record_start_frame"])
            source_start = int(segment["source_start_frame"])
            source_end = int(segment["source_end_frame"])
            timeline_start = int(segment["timeline_start_frame"])
            source_offset = max(0.0, (source_start - record_start) / self.project.fps)
            duration = max(0.0, (source_end - source_start) / self.project.fps)
            delay_ms = max(0, int(round((timeline_start / self.project.fps) * 1000)))
            label = f"a{index}"
            labels.append(f"[{label}]")
            filters.append(
                f"[{index}:a]"
                f"atrim=start={source_offset:.6f}:duration={duration:.6f},"
                "asetpts=PTS-STARTPTS,"
                "aresample=48000,"
                "aformat=sample_fmts=s16:channel_layouts=stereo,"
                f"adelay={delay_ms}:all=1"
                f"[{label}]"
            )

        if len(labels) == 1:
            filters.append(
                f"{labels[0]}apad=whole_dur={total_duration:.6f},"
                f"atrim=0:{total_duration:.6f}[mixed]"
            )
        else:
            filters.append(
                f"{''.join(labels)}"
                f"amix=inputs={len(labels)}:duration=longest:normalize=0,"
                f"apad=whole_dur={total_duration:.6f},"
                f"atrim=0:{total_duration:.6f}[mixed]"
            )

        command.extend(
            [
                "-filter_complex",
                ";".join(filters),
                "-map",
                "[mixed]",
                "-ac",
                "2",
                "-ar",
                "48000",
                "-c:a",
                "pcm_s16le",
                str(output_path),
            ]
        )

        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            creationflags=creationflags,
            timeout=3600,
        )
        if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            detail = result.stderr.strip() or result.stdout.strip() or "FFmpeg did not create mixed audio."
            raise RuntimeError(f"Could not build embedded audio mix. {detail}")
        return output_path

    def build_silent_audio(self, output_path: Path, duration_seconds: float) -> Optional[Path]:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sample_rate = AUDIO_SAMPLE_RATE
        channels = 2
        sample_width = 2
        total_samples = max(1, int(round(duration_seconds * sample_rate)))
        with wave.open(str(output_path), "wb") as writer:
            writer.setnchannels(channels)
            writer.setsampwidth(sample_width)
            writer.setframerate(sample_rate)
            self.write_audio_silence(writer, total_samples, channels, sample_width)
        if not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            return None
        return output_path

    def read_audio_segment(
        self,
        segment: dict[str, object],
        sample_rate: int,
        channels: int,
        sample_width: int,
        skip_samples: int,
        wanted_samples: int,
    ) -> tuple[bytes, int]:
        path = Path(segment["path"])
        record_start = int(segment["record_start_frame"])
        source_start = int(segment["source_start_frame"])
        source_offset_frames = max(0, source_start - record_start)
        source_sample_start = int(round((source_offset_frames / self.project.fps) * sample_rate)) + skip_samples

        with wave.open(str(path), "rb") as reader:
            if (
                reader.getframerate() != sample_rate
                or reader.getnchannels() != channels
                or reader.getsampwidth() != sample_width
            ):
                raise RuntimeError("Microphone audio format changed during the recording.")

            source_sample_start = min(max(0, source_sample_start), reader.getnframes())
            reader.setpos(source_sample_start)
            samples_to_read = min(wanted_samples, reader.getnframes() - source_sample_start)
            data = reader.readframes(samples_to_read)
            return data, samples_to_read

    def write_audio_silence(
        self,
        writer: wave.Wave_write,
        sample_count: int,
        channels: int,
        sample_width: int,
    ) -> None:
        bytes_per_sample_frame = channels * sample_width
        remaining = max(0, int(sample_count))
        while remaining:
            chunk_samples = min(remaining, 48_000)
            writer.writeframes(b"\x00" * chunk_samples * bytes_per_sample_frame)
            remaining -= chunk_samples

    def mux_audio_into_video(self, video_path: Path, audio_path: Path) -> tuple[Path, bool, str]:
        ffmpeg = self.find_ffmpeg_executable()
        if ffmpeg is None:
            return (
                video_path,
                False,
                "Embedding audio into MP4 needs local FFmpeg. Install requirements.txt so imageio-ffmpeg is available.",
            )

        final_path = video_path.with_suffix(".mp4")
        temp_output = final_path.with_name(f"{final_path.stem}_with_audio_tmp{final_path.suffix}")
        temp_output.unlink(missing_ok=True)
        if video_path.suffix.lower() == ".mp4":
            video_codec_args = ["-c:v", "copy"]
        else:
            video_codec_args = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]

        command = [
            ffmpeg,
            "-y",
            "-i",
            str(video_path),
            "-i",
            str(audio_path),
            *video_codec_args,
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(temp_output),
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            creationflags=creationflags,
            timeout=3600,
        )
        if result.returncode != 0 or not temp_output.exists() or temp_output.stat().st_size == 0:
            temp_output.unlink(missing_ok=True)
            detail = result.stderr.strip() or result.stdout.strip() or "FFmpeg did not create an output file."
            return video_path, False, f"MP4 audio embedding failed. {detail}"

        if final_path.exists() and final_path != video_path:
            final_path.unlink(missing_ok=True)
        if final_path == video_path:
            video_path.unlink(missing_ok=True)
        temp_output.replace(final_path)
        if video_path != final_path:
            video_path.unlink(missing_ok=True)
        audio_path.unlink(missing_ok=True)
        return final_path, True, ""

    def find_ffmpeg_executable(self) -> Optional[str]:
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            return ffmpeg

        try:
            import imageio_ffmpeg
        except Exception:
            return None

        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None

    def write_video(
        self,
        output_path: Path,
        codec: str,
        frame_paths: list[Path],
        fps: float,
        source_width: int,
        source_height: int,
        output_width: int,
        output_height: int,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fourcc = cv2.VideoWriter_fourcc(*codec)
        writer = cv2.VideoWriter(str(output_path), fourcc, fps, (output_width, output_height))

        if not writer.isOpened():
            writer.release()
            raise VideoWriterOpenError(f"OpenCV could not open a {codec} writer for {output_path}")

        completed = False
        try:
            for frame_path in frame_paths:
                frame = cv2.imread(str(frame_path))
                if frame is None:
                    raise RuntimeError(f"Could not read frame: {frame_path}")
                if frame.shape[1] != source_width or frame.shape[0] != source_height:
                    frame = cv2.resize(frame, (source_width, source_height), interpolation=cv2.INTER_AREA)
                writer.write(self.frame_to_1080p_canvas(frame, output_width, output_height))
            completed = True
        finally:
            writer.release()

        if not completed:
            output_path.unlink(missing_ok=True)

        if not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            raise VideoWriterOpenError(f"OpenCV did not create a valid output file: {output_path}")

        return output_path

    def frame_to_1080p_canvas(self, frame: np.ndarray, output_width: int, output_height: int) -> np.ndarray:
        source_height, source_width = frame.shape[:2]
        scale = min(output_width / source_width, output_height / source_height)
        scaled_width = max(2, int(round(source_width * scale)))
        scaled_height = max(2, int(round(source_height * scale)))

        if scaled_width % 2:
            scaled_width -= 1
        if scaled_height % 2:
            scaled_height -= 1

        resized = cv2.resize(frame, (scaled_width, scaled_height), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((output_height, output_width, 3), dtype=np.uint8)
        x = (output_width - scaled_width) // 2
        y = (output_height - scaled_height) // 2
        canvas[y : y + scaled_height, x : x + scaled_width] = resized
        return canvas

    def unique_fallback_path(self, path: Path) -> Path:
        if not path.exists():
            return path

        for index in range(1, 1000):
            candidate = path.with_name(f"{path.stem}_{index}{path.suffix}")
            if not candidate.exists():
                return candidate

        raise RuntimeError(f"Could not find a free fallback filename near {path}")

    def refresh_timeline(self) -> None:
        frame_count = self.project.frame_count()
        timeline_index = self.project.get_timeline_index()

        self._updating_timeline = True
        self.timeline.setRange(0, frame_count)
        self.timeline.set_selection(self.project.cut_start, self.project.cut_end)
        if not self._timeline_dragging:
            self.timeline.setValue(timeline_index, emit=False)
        self._updating_timeline = False

        self.update_timeline_labels()
        self.update_preview()

    def is_preview_playing(self) -> bool:
        return self.preview_playback_timer.isActive()

    def toggle_preview_playback(self) -> None:
        if self.is_preview_playing():
            self.stop_preview_playback()
            return
        self.start_preview_playback()

    def start_preview_playback(self) -> None:
        if self.project.state == STATE_RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause recording before playing the preview.")
            return
        if not self.project.has_frames():
            QMessageBox.warning(self, APP_NAME, "Record frames before playing the preview.")
            return

        self.stop_preview_playback()

        frame_count = self.project.frame_count()
        start_index = self.project.get_timeline_index()
        if start_index >= frame_count:
            start_index = 0
            self.project.set_timeline_index(start_index)
            self.timeline.setValue(start_index, emit=False)

        self.preview_playback_index = start_index
        self.preview_playback_start_index = start_index
        self.preview_playback_started_at = time.perf_counter()

        try:
            segments = self.audio_mix_segments()
            if not segments:
                QMessageBox.warning(
                    self,
                    APP_NAME,
                    "No recorded audio is available for preview yet.\n\n"
                    "Record with the selected input/output devices, then pause before pressing Play.",
                )
                return

            self.preview_audio_path = self.build_audio_mix(
                self.project.ensure_temp_dir() / "preview_audio_mix.wav",
                segments,
                allow_silence=False,
            )
            if self.preview_audio_path is None:
                QMessageBox.warning(self, APP_NAME, "Could not build preview audio from the recorded segments.")
                return

            self.preview_audio_worker = AudioPlaybackWorker(
                self.preview_audio_path,
                start_index / self.project.fps,
                self.selected_audio_output_device(),
            )
            self.preview_audio_worker.playback_error.connect(self.on_preview_playback_error)
            self.preview_audio_worker.finished.connect(self.on_preview_audio_finished)
            self.preview_audio_worker.start()

            sources = ", ".join(self.audio_source_names(segments))
            if self.has_microphone_audio(segments):
                self.update_status(f"Preview playing with audio: {sources}")
            else:
                self.update_status(f"Preview playing without microphone audio: {sources}")
        except Exception as exc:
            self.preview_audio_path = None
            self.preview_audio_worker = None
            self.update_status(f"Preview audio unavailable: {exc}")
            return

        self.preview_playback_timer.start()
        self.update_controls()

    def advance_preview_playback(self) -> None:
        frame_count = self.project.frame_count()
        if frame_count <= 0:
            self.stop_preview_playback()
            return

        elapsed = max(0.0, time.perf_counter() - self.preview_playback_started_at)
        target_index = self.preview_playback_start_index + int(round(elapsed * self.project.fps))
        self.preview_playback_index = max(self.preview_playback_index, target_index)

        if self.preview_playback_index >= frame_count:
            self.project.set_timeline_index(frame_count)
            self.timeline.setValue(frame_count, emit=False)
            self.update_timeline_labels()
            self.update_preview()
            self.stop_preview_playback()
            return

        self.project.set_timeline_index(self.preview_playback_index)
        self.timeline.setValue(self.preview_playback_index, emit=False)
        self.update_timeline_labels()
        self.update_preview()
        self.preview_playback_index += 1

    def stop_preview_playback(self) -> None:
        was_playing = self.preview_playback_timer.isActive()
        if was_playing:
            self.preview_playback_timer.stop()

        worker = self.preview_audio_worker
        self.preview_audio_worker = None
        if worker is not None:
            worker.stop()
            if worker.isRunning():
                worker.wait(1000)

        if hasattr(self, "play_preview_button"):
            self.play_preview_button.setText("Play")
        if was_playing and hasattr(self, "status_label"):
            self.update_status("Paused" if self.project.state == STATE_PAUSED else self.project.state.title())
        if hasattr(self, "select_button"):
            self.update_controls()

    def on_preview_playback_error(self, message: str) -> None:
        self.update_status(f"Preview audio error: {message}")

    def on_preview_audio_finished(self) -> None:
        if self.sender() is self.preview_audio_worker:
            self.preview_audio_worker = None

    def update_timeline_labels(self) -> None:
        frame_count = self.project.frame_count()
        timeline_index = self.project.get_timeline_index()

        self.time_label.setText(
            f"{format_seconds(timeline_index / self.project.fps)} / "
            f"{format_seconds(frame_count / self.project.fps)}"
        )
        self.cut_label.setText(self.cut_label_text())

    def update_preview(self) -> None:
        frame_count = self.project.frame_count()
        timeline_index = self.project.get_timeline_index()

        if frame_count == 0:
            self.set_preview_placeholder("Preview appears here after frames are recorded")
            self.preview_caption.setText("Preview: no frames yet")
            return

        frame_path = self.project.preview_frame_path(timeline_index)
        if frame_path is None:
            self.set_preview_placeholder("Preview unavailable")
            self.preview_caption.setText("Preview: no frame selected")
            return

        frame = cv2.imread(str(frame_path))
        if frame is None:
            self.set_preview_placeholder("Could not read preview frame")
            self.preview_caption.setText(f"Preview: failed to load {frame_path.name}")
            return

        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        height, width, channels = rgb_frame.shape
        qimage = QImage(rgb_frame.data, width, height, channels * width, QImage.Format_RGB888).copy()
        pixmap = QPixmap.fromImage(qimage).scaled(
            self.preview_label.size(),
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
        self.preview_label.setPixmap(pixmap)

        if timeline_index <= 0:
            frame_number = 1
            preview_time = 0
        else:
            frame_number = min(timeline_index, frame_count)
            preview_time = frame_number / self.project.fps

        self.preview_caption.setText(
            f"Preview: frame {frame_number} of {frame_count} at {format_seconds(preview_time)}"
        )

    def set_preview_placeholder(self, text: str) -> None:
        self.preview_label.setPixmap(QPixmap())
        self.preview_label.setText(text)

    def cut_label_text(self) -> str:
        start = self.project.cut_start
        end = self.project.cut_end
        if start is None and end is None:
            return "Trim range: none"

        if start is None:
            return f"Trim range: Mark Out set at {format_seconds(end / self.project.fps)}. Set Mark In next."
        if end is None:
            return f"Trim range: Mark In set at {format_seconds(start / self.project.fps)}. Set Mark Out next."

        range_start, range_end = sorted((start, end))
        duration = (range_end - range_start) / self.project.fps
        if range_start == range_end:
            return f"Trim range: empty at {format_seconds(range_start / self.project.fps)}"

        return (
            "Trim range: "
            f"{format_seconds(range_start / self.project.fps)} -> "
            f"{format_seconds(range_end / self.project.fps)} "
            f"({format_seconds(duration)})"
        )

    def update_status(self, text: str) -> None:
        self.status_label.setText(f"Status: {text}")

    def update_controls(self) -> None:
        state = self.project.state
        has_area = self.project.area is not None
        has_frames = self.project.has_frames()
        is_recording = state == STATE_RECORDING
        is_busy = self._state_transitioning
        is_playing = self.is_preview_playing() if hasattr(self, "preview_playback_timer") else False
        can_edit = not is_recording and has_frames

        self.select_button.setEnabled(not is_recording and not is_busy and not is_playing)
        self.record_button.setEnabled(has_area and not is_busy and not is_playing)
        if state == STATE_PAUSED:
            self.record_button.setText("Resume Recording")
        elif state == STATE_STOPPED:
            self.record_button.setText("Start New Recording")
        elif is_recording:
            self.record_button.setText("Pause Recording")
        else:
            self.record_button.setText("Start Recording")
        self.play_preview_button.setEnabled(can_edit and not is_busy)
        self.play_preview_button.setText("Stop" if is_playing else "Play")
        self.stop_button.setEnabled(state == STATE_PAUSED and not is_busy and not is_playing)
        self.save_button.setEnabled(can_edit and not is_busy and not is_playing)
        self.audio_input_combo.setEnabled(not is_recording and not is_busy and not is_playing)
        self.audio_output_combo.setEnabled(not is_recording and not is_busy and not is_playing)
        self.refresh_audio_button.setEnabled(not is_recording and not is_busy and not is_playing)
        has_selection_marks = self.project.cut_start is not None or self.project.cut_end is not None
        self.timeline.setEnabled(can_edit and not is_busy)
        self.cut_start_button.setEnabled(can_edit and not is_busy and not is_playing)
        self.cut_end_button.setEnabled(can_edit and not is_busy and not is_playing)
        has_delete_selection = (
            self.project.cut_start is not None
            and self.project.cut_end is not None
            and self.project.cut_start != self.project.cut_end
        )
        self.delete_cut_button.setEnabled(can_edit and has_delete_selection and not is_busy and not is_playing)
        self.clear_cut_button.setEnabled(can_edit and has_selection_marks and not is_busy and not is_playing)

        if self.floating_control.isVisible():
            self.floating_control.primary_button.setEnabled(
                has_area and state != STATE_STOPPED and not is_busy and not is_playing
            )
            self.floating_control.stop_button.setEnabled(state == STATE_PAUSED and not is_busy and not is_playing)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.stop_preview_playback()
        self.stop_worker()
        self.stop_audio_recording(end_frame=self.project.frame_count())
        if self.project.state == STATE_RECORDING:
            self.project.state = STATE_PAUSED if self.project.has_frames() else STATE_IDLE
        self.project.set_timeline_index(self.project.frame_count())

        try:
            if self.project.has_frames() or self.project.area is not None:
                self.autosave_project()
            else:
                self.clear_autosave_project()
        except Exception as exc:
            QMessageBox.critical(
                self,
                APP_NAME,
                "Could not save the local recording project for restore.\n\n"
                f"{exc}\n\nThe app will stay open so the recording is not lost.",
            )
            event.ignore()
            return

        self.area_overlay.close()
        self.floating_control.close()
        event.accept()
