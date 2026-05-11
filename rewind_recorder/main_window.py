import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import cv2
import qtawesome as qta
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QColor, QImage, QKeySequence, QPixmap, QShortcut
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
    QStatusBar,
    QVBoxLayout,
    QWidget,
)

_log = logging.getLogger(__name__)

from rewind_recorder import __version__
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

_PRIMARY_BUTTON_STYLE = """
QPushButton {
    background: #b8302d;
    color: #ffffff;
    border: 1px solid #d04040;
    border-radius: 6px;
    padding: 10px 16px;
    font-size: 14px;
    font-weight: 700;
}
QPushButton:hover {
    background: #c93a37;
    border-color: #e25555;
}
QPushButton:pressed {
    background: #8f2522;
    border-color: #a02d2a;
}
QPushButton:disabled {
    background: #3a1c1c;
    color: #7a5757;
    border-color: #4a2424;
}
"""

_SECTION_STYLE = "font-weight: 700; font-size: 14px; color: #ccc;"
_SUBLABEL_STYLE = "color: #888; font-size: 12px;"
_TIME_STYLE = (
    "font-family: 'Consolas', 'Cascadia Mono', 'Courier New', monospace; "
    "font-size: 22px; font-weight: 600; color: #e8e8e8; "
    "padding: 4px 12px; background: #0e0e0e; border: 1px solid #2a2a2a; "
    "border-radius: 6px; letter-spacing: 1px;"
)
_HINT_STYLE = "color: #999; font-size: 13px; line-height: 1.5em;"


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.project = FrameProject(DEFAULT_FPS)
        self.audio = AudioManager(DEFAULT_FPS)
        self.exporter = VideoExporter(DEFAULT_FPS)
        self.autosaver = AutosaveManager(self.project, self.audio)

        self.capture_worker: CaptureWorker | None = None
        self._zombie_workers: list[CaptureWorker] = []
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
            self.audio.fps = self.project.fps
            self.exporter.fps = self.project.fps
            self.preview_ctrl.set_fps(self.project.fps)
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
        self._build_menu_bar()

        central = QWidget()
        central.setStyleSheet("background: #181818;")
        layout = QVBoxLayout(central)
        layout.setSpacing(6)
        layout.setContentsMargins(16, 12, 16, 12)

        self.time_label = QLabel("00:00.000 / 00:00.000")
        self.time_label.setStyleSheet(_TIME_STYLE)
        self.time_label.setAlignment(Qt.AlignCenter)
        self.time_label.setToolTip("Current position / total recording length")

        self.cut_label = QLabel("Trim: none")
        self.cut_label.setStyleSheet(_SUBLABEL_STYLE)

        self.preview_label = QLabel(self._preview_hint_text())
        self.preview_label.setAlignment(Qt.AlignCenter)
        self.preview_label.setMinimumSize(320, 180)
        self.preview_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.preview_label.setScaledContents(False)
        self.preview_label.setWordWrap(True)
        self.preview_label.setStyleSheet(
            "QLabel { background: #0a0a0a; color: #888; border: 1px solid #333; "
            "border-radius: 6px; font-size: 14px; padding: 24px; }"
        )
        self._last_preview_frame: QImage | None = None

        self.timeline = TrimTimeline()
        self.timeline.setRange(0, 0)
        self.timeline.setEnabled(False)
        self.timeline.setToolTip(
            "Drag to scrub. While paused, click Record to overwrite from this point."
        )
        self.timeline.valueChanged.connect(self._on_timeline_changed)
        self.timeline.selectionChanged.connect(self._on_trim_selection_changed)
        self.timeline.sliderPressed.connect(self._on_timeline_pressed)
        self.timeline.sliderReleased.connect(self._on_timeline_released)

        self.audio_input_combo = QComboBox()
        self.audio_input_combo.setToolTip("Microphone used while recording (WASAPI). Pick \"none\" by leaving empty.")
        self.audio_output_combo = QComboBox()
        self.audio_output_combo.setToolTip("System audio source (loopback) — captures whatever your speakers play.")
        self.refresh_audio_button = QPushButton(self._icon("mdi6.refresh"), " Refresh")
        self.refresh_audio_button.setStyleSheet(_BUTTON_STYLE)
        self.refresh_audio_button.setToolTip("Re-scan audio devices (after plugging in a mic, etc.)")
        self.refresh_audio_button.clicked.connect(self._refresh_audio_devices)

        self.select_button = QPushButton(self._icon("mdi6.target"), " Select Area")
        self.select_button.setToolTip("Drag a box around the part of the screen you want to capture.")

        self.record_button = QPushButton(self._icon("mdi6.record-circle-outline"), " Record  (F9)")
        self.record_button.setToolTip(
            "Start recording. While recording, click again (or press F9) to pause.\n"
            "While paused, scrub the timeline back and click Record to overwrite from there."
        )

        self.import_button = QPushButton(self._icon("mdi6.file-import-outline"), " Import Clip")
        self.import_button.setToolTip("Load an existing video file (mp4/avi/mov/mkv/webm) into the editor.")

        self.play_preview_button = QPushButton(self._icon("mdi6.play"), " Play  (Space)")
        self.play_preview_button.setToolTip("Preview the recording with audio. Press Space to toggle.")

        self.stop_button = QPushButton(self._icon("mdi6.stop"), " Stop  (F10)")
        self.stop_button.setToolTip("Stop the current recording session and finalize it.")

        self.save_button = QPushButton(self._icon("mdi6.content-save"), " Save As...  (Ctrl+S)")
        self.save_button.setToolTip("Export the recording to MP4 with embedded audio.")

        self.cut_start_button = QPushButton(self._icon("mdi6.format-vertical-align-top"), " Trim Start  ([)")
        self.cut_start_button.setToolTip("Set the start of a section to cut out, at the current playhead.")

        self.cut_end_button = QPushButton(self._icon("mdi6.format-vertical-align-bottom"), " Trim End  (])")
        self.cut_end_button.setToolTip("Set the end of a section to cut out, at the current playhead.")

        self.delete_cut_button = QPushButton(self._icon("mdi6.delete-outline"), " Cut Out Section")
        self.delete_cut_button.setToolTip("Permanently remove the marked section from the recording.")

        self.clear_cut_button = QPushButton(self._icon("mdi6.close-circle-outline"), " Clear Marks")
        self.clear_cut_button.setToolTip("Forget the current trim start/end marks (does not delete frames).")

        for btn in (
            self.select_button, self.import_button,
            self.play_preview_button, self.stop_button, self.save_button,
            self.cut_start_button, self.cut_end_button,
            self.delete_cut_button, self.clear_cut_button,
        ):
            btn.setMinimumHeight(40)
            btn.setStyleSheet(_BUTTON_STYLE)

        self.record_button.setMinimumHeight(46)
        self.record_button.setStyleSheet(_PRIMARY_BUTTON_STYLE)

        self.select_button.clicked.connect(self._select_area)
        self.record_button.clicked.connect(self._record_or_resume)
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
        input_title = QLabel("Microphone")
        input_title.setStyleSheet(_SECTION_STYLE)
        input_col.addWidget(input_title)
        input_col.addWidget(self.audio_input_combo)

        output_col = QVBoxLayout()
        output_col.setSpacing(4)
        output_title = QLabel("System audio (loopback)")
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

        time_row = QHBoxLayout()
        time_row.addStretch(1)
        time_row.addWidget(self.time_label)
        time_row.addStretch(1)

        timeline_header = QHBoxLayout()
        tl_label = QLabel("Timeline")
        tl_label.setStyleSheet(_SECTION_STYLE)
        timeline_header.addWidget(tl_label)
        timeline_header.addSpacing(12)
        timeline_header.addWidget(self.cut_label)
        timeline_header.addStretch(1)

        trim_col = QVBoxLayout()
        trim_col.setSpacing(6)
        trim_title = QLabel("Trim (cut out a section)")
        trim_title.setStyleSheet(_SECTION_STYLE)
        trim_col.addWidget(trim_title)
        trim_btns = QHBoxLayout()
        trim_btns.setSpacing(6)
        trim_btns.addWidget(self.cut_start_button)
        trim_btns.addWidget(self.cut_end_button)
        trim_btns.addWidget(self.delete_cut_button)
        trim_btns.addWidget(self.clear_cut_button)
        trim_col.addLayout(trim_btns)

        record_col = QVBoxLayout()
        record_col.setSpacing(6)
        rc_title = QLabel("Record")
        rc_title.setStyleSheet(_SECTION_STYLE)
        record_col.addWidget(rc_title)
        rc_btns = QHBoxLayout()
        rc_btns.setSpacing(6)
        rc_btns.addWidget(self.select_button)
        rc_btns.addWidget(self.record_button, 1)
        rc_btns.addWidget(self.stop_button)
        record_col.addLayout(rc_btns)

        review_col = QVBoxLayout()
        review_col.setSpacing(6)
        rv_title = QLabel("Review & save")
        rv_title.setStyleSheet(_SECTION_STYLE)
        review_col.addWidget(rv_title)
        rv_btns = QHBoxLayout()
        rv_btns.setSpacing(6)
        rv_btns.addWidget(self.play_preview_button)
        rv_btns.addWidget(self.import_button)
        rv_btns.addWidget(self.save_button)
        review_col.addLayout(rv_btns)

        bottom_row = QHBoxLayout()
        bottom_row.setSpacing(24)
        bottom_row.addLayout(record_col, 2)
        bottom_row.addLayout(trim_col, 2)
        bottom_row.addLayout(review_col, 2)

        layout.addLayout(audio_row)
        layout.addWidget(self.preview_label, 1)
        layout.addLayout(time_row)
        layout.addLayout(timeline_header)
        layout.addWidget(self.timeline)
        layout.addSpacing(8)
        layout.addLayout(bottom_row)

        self.setCentralWidget(central)

        self._build_status_bar()
        self._install_shortcuts()
        self._refresh_audio_devices()

    def _build_menu_bar(self) -> None:
        menu = self.menuBar()
        file_menu = menu.addMenu("&File")
        save_action = QAction("&Save As...", self)
        save_action.setShortcut(QKeySequence("Ctrl+S"))
        save_action.triggered.connect(self._save_as)
        file_menu.addAction(save_action)
        import_action = QAction("&Import Clip...", self)
        import_action.triggered.connect(self._import_clip)
        file_menu.addAction(import_action)
        file_menu.addSeparator()
        quit_action = QAction("&Quit", self)
        quit_action.setShortcut(QKeySequence("Ctrl+Q"))
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        help_menu = menu.addMenu("&Help")
        logs_action = QAction("Open &Logs Folder", self)
        logs_action.setShortcut(QKeySequence("Ctrl+Shift+L"))
        logs_action.triggered.connect(self._open_logs_folder)
        help_menu.addAction(logs_action)
        help_menu.addSeparator()
        about_action = QAction("&About Rewind Recorder", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _build_status_bar(self) -> None:
        self.status_bar = QStatusBar(self)
        self.status_bar.setStyleSheet(
            "QStatusBar { background: #141414; color: #bbb; border-top: 1px solid #2a2a2a; } "
            "QStatusBar::item { border: none; }"
        )
        self.setStatusBar(self.status_bar)
        self.status_bar.showMessage("Ready — pick a screen area to start.")

    def _install_shortcuts(self) -> None:
        for key, slot in (
            ("F9", self._record_or_resume),
            ("F10", self._stop_recording),
            ("Space", self._toggle_preview),
            ("[", self._set_cut_start),
            ("]", self._set_cut_end),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(slot)

    def _preview_hint_text(self) -> str:
        if self.project.area is None:
            return (
                "Step 1 — click \"Select Area\" to drag a box around what you want to record.\n\n"
                "Then hit Record (F9). Pause anytime, scrub the timeline back, and Record\n"
                "again to overwrite. Use Trim Start/End to cut out a section, then Save As."
            )
        if not self.project.has_frames():
            return (
                f"Area locked: {self.project.area.width}×{self.project.area.height}\n\n"
                "Step 2 — hit Record (F9) to start capturing.\n"
                "Press F9 again to pause. F10 to stop. Space to preview."
            )
        return "Preview unavailable"

    def _open_logs_folder(self) -> None:
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        else:
            base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        log_dir = base / "RewindRecorder" / "logs"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            if sys.platform == "win32":
                os.startfile(str(log_dir))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(log_dir)])
            else:
                subprocess.Popen(["xdg-open", str(log_dir)])
        except Exception as exc:
            _log.exception("Could not open logs folder")
            QMessageBox.warning(self, APP_NAME, f"Could not open logs folder:\n{log_dir}\n\n{exc}")

    def _show_about(self) -> None:
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        else:
            base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        log_path = base / "RewindRecorder" / "logs" / "rewind_recorder.log"
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<h3>{APP_NAME}</h3>"
            f"<p>Version {__version__}</p>"
            f"<p>Local Windows screen recorder with rewind-and-overwrite editing. "
            f"Nothing leaves your machine.</p>"
            f"<p><b>Logs:</b><br><code>{log_path}</code></p>"
            f"<p><a href=\"https://github.com/nikolsen1234-bit/rewind-recorder\">"
            f"github.com/nikolsen1234-bit/rewind-recorder</a></p>"
            f"<p><b>Shortcuts</b><br>"
            f"F9 Record / Pause &nbsp; F10 Stop &nbsp; Space Play preview<br>"
            f"[ Trim Start &nbsp; ] Trim End &nbsp; Ctrl+S Save As &nbsp; Ctrl+Q Quit</p>"
        )

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
            self._reap_zombie_workers()
            return

        try:
            worker.frame_saved.disconnect(self._on_frame_saved)
        except (RuntimeError, TypeError):
            pass
        try:
            worker.capture_error.disconnect(self._on_capture_error)
        except (RuntimeError, TypeError):
            pass
        try:
            worker.finished.disconnect(self._on_worker_finished)
        except (RuntimeError, TypeError):
            pass

        worker.stop()
        worker.wait(3000)
        self.capture_worker = None

        if worker.isRunning():
            _log.warning("Capture thread did not stop within 3s; parking as zombie")
            self._zombie_workers.append(worker)
            worker.finished.connect(lambda w=worker: self._zombie_workers.remove(w) if w in self._zombie_workers else None)
            QMessageBox.warning(
                self, APP_NAME,
                "Recording thread did not stop within 3 seconds. "
                "It will keep running in the background until it exits cleanly.",
            )

        self._reap_zombie_workers()

    def _reap_zombie_workers(self) -> None:
        if not self._zombie_workers:
            return
        self._zombie_workers = [w for w in self._zombie_workers if w.isRunning()]

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
            QMessageBox.warning(self, APP_NAME, "Pause or stop recording before setting a trim start.")
            return
        if not self.project.has_frames():
            QMessageBox.warning(self, APP_NAME, "Record some frames before setting a trim start.")
            return
        self.project.cut_start = self.timeline.value()
        self._refresh_timeline()
        self._update_status(f"Trim start at {format_seconds(self.project.cut_start / self.project.fps)}")
        self._update_controls()

    def _set_cut_end(self) -> None:
        if self.project.state is RecorderState.RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause or stop recording before setting a trim end.")
            return
        if not self.project.has_frames():
            QMessageBox.warning(self, APP_NAME, "Record some frames before setting a trim end.")
            return
        self.project.cut_end = self.timeline.value()
        self._refresh_timeline()
        self._update_status(f"Trim end at {format_seconds(self.project.cut_end / self.project.fps)}")
        self._update_controls()

    def _clear_cut_selection(self) -> None:
        self.project.reset_cut_marks()
        self._refresh_timeline()
        self._update_status("Trim marks cleared")
        self._update_controls()

    def _delete_selected_range(self) -> None:
        if self.project.state is RecorderState.RECORDING:
            QMessageBox.warning(self, APP_NAME, "Pause or stop recording before cutting out a section.")
            return
        if self.project.cut_start is None or self.project.cut_end is None:
            QMessageBox.warning(self, APP_NAME, "Set both Trim Start and Trim End before cutting out a section.")
            return

        start, end = self.project.cut_start, self.project.cut_end
        if start == end:
            QMessageBox.warning(self, APP_NAME, "Trim Start and Trim End are the same position, so nothing is selected.")
            return

        lo, hi = sorted((start, end))
        duration = (hi - lo) / self.project.fps
        reply = QMessageBox.question(
            self, APP_NAME,
            f"Cut out this section?\n\n"
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
        self._update_status(f"Cut out section ({removed} frame(s))")
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
        self.play_preview_button.setText(" Play  (Space)")
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
        if self._state_transitioning:
            return
        self._state_transitioning = True
        self.save_button.setEnabled(False)
        try:
            self._save_as_impl()
        finally:
            self._state_transitioning = False
            self._update_controls()

    def _save_as_impl(self) -> None:
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
            _log.exception("Export failed")
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
        try:
            if self.area_overlay.isVisible():
                force_widget_topmost(self.area_overlay)
                exclude_widget_from_capture(self.area_overlay)
            if self.floating_control.isVisible():
                force_widget_topmost(self.floating_control)
                exclude_widget_from_capture(self.floating_control)
        except RuntimeError as exc:
            _log.debug("Skipping topmost maintenance on deleted widget: %s", exc)
            self.topmost_timer.stop()

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
            self.preview_label.setText(self._preview_hint_text())
            return

        path = self.project.preview_frame_path(index)
        if path is None:
            self._last_preview_frame = None
            self.preview_label.setPixmap(QPixmap())
            self.preview_label.setText("Preview unavailable")
            return

        try:
            frame = cv2.imread(str(path))
        except Exception as exc:
            _log.warning("Preview cv2.imread raised on %s: %s", path, exc)
            frame = None
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
            return "Trim: none"
        if start is None:
            return f"Trim: end at {format_seconds(end / self.project.fps)} — set start next"
        if end is None:
            return f"Trim: start at {format_seconds(start / self.project.fps)} — set end next"

        lo, hi = sorted((start, end))
        duration = (hi - lo) / self.project.fps
        if lo == hi:
            return f"Trim: empty at {format_seconds(lo / self.project.fps)}"
        return (
            f"Trim: {format_seconds(lo / self.project.fps)} → "
            f"{format_seconds(hi / self.project.fps)} ({format_seconds(duration)})"
        )

    def _update_status(self, text: str) -> None:
        if hasattr(self, "status_bar"):
            self.status_bar.showMessage(text)
        else:
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
        self.record_button.setEnabled(has_area and not busy and not playing and state is not RecorderState.RECORDING)
        self.play_preview_button.setEnabled(can_edit and not busy)
        if playing:
            self.play_preview_button.setIcon(self._icon("mdi6.stop"))
            self.play_preview_button.setText(" Stop  (Space)")
        else:
            self.play_preview_button.setIcon(self._icon("mdi6.play"))
            self.play_preview_button.setText(" Play  (Space)")
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

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        exclude_widget_from_capture(self)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.topmost_timer.stop()

        try:
            self.preview_ctrl.stop()
        except Exception:
            _log.exception("preview stop failed during close")

        try:
            if self.project.state is RecorderState.RECORDING and self.capture_worker is not None:
                self.capture_worker.stop()
                self.capture_worker.wait(3000)
        except Exception:
            _log.exception("capture stop failed during close")

        try:
            self.audio.stop_recording(end_frame=self.project.frame_count())
        except Exception:
            _log.exception("audio stop failed during close")

        if self.project.state is RecorderState.RECORDING:
            self.project.state = RecorderState.PAUSED if self.project.has_frames() else RecorderState.IDLE
        self.project.set_timeline_index(self.project.frame_count())

        try:
            if self.project.has_frames() or self.project.area is not None:
                self.autosaver.save(self.locked_capture_area)
            else:
                self.autosaver.clear()
        except Exception as exc:
            _log.exception("autosave failed during close")
            self.topmost_timer.start()
            QMessageBox.critical(
                self, APP_NAME,
                f"Could not save the local recording project for restore.\n\n"
                f"{exc}\n\nThe app will stay open so the recording is not lost.",
            )
            event.ignore()
            return

        try:
            self._stop_worker()
        except Exception:
            _log.exception("_stop_worker failed during close")

        try:
            self.area_overlay.close()
        except Exception:
            pass
        try:
            self.floating_control.close()
        except Exception:
            pass
        event.accept()
