import time
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from rewind_recorder.audio_manager import AudioManager
from rewind_recorder.export import VideoExporter
from rewind_recorder.playback import AudioPlaybackWorker
from rewind_recorder.project import FrameProject


class PreviewController(QObject):
    frame_changed = Signal(int)
    playback_stopped = Signal()
    status_message = Signal(str)

    def __init__(
        self,
        project: FrameProject,
        audio: AudioManager,
        exporter: VideoExporter,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.project = project
        self.audio = audio
        self.exporter = exporter

        self._timer = QTimer(self)
        self._timer.setInterval(max(1, int(round(1000 / self.project.fps))))
        self._timer.timeout.connect(self._advance)

        self._audio_worker: AudioPlaybackWorker | None = None
        self._audio_path: Path | None = None
        self._index = 0
        self._start_index = 0
        self._started_at = 0.0

    @property
    def is_playing(self) -> bool:
        return self._timer.isActive()

    def toggle(self) -> None:
        if self.is_playing:
            self.stop()
        else:
            self.start()

    def start(self, speaker_id: str | None = None) -> None:
        self.stop()

        frame_count = self.project.frame_count()
        start_index = self.project.get_timeline_index()
        if start_index >= frame_count:
            start_index = 0
            self.project.set_timeline_index(0)

        self._index = start_index
        self._start_index = start_index
        self._started_at = time.perf_counter()

        segments = self.audio.mix_segments()
        if not segments:
            self.status_message.emit("No recorded audio available for preview.")
            return

        try:
            self._audio_path = self.exporter.build_audio_mix(
                self.project.ensure_temp_dir() / "preview_audio_mix.wav",
                segments,
                frame_count,
                allow_silence=False,
            )
        except Exception as exc:
            self._audio_path = None
            self.status_message.emit(f"Preview audio unavailable: {exc}")
            return

        if self._audio_path is None:
            self.status_message.emit("Could not build preview audio from recorded segments.")
            return

        self._audio_worker = AudioPlaybackWorker(
            self._audio_path,
            start_index / self.project.fps,
            speaker_id,
        )
        self._audio_worker.playback_error.connect(self._on_audio_error)
        self._audio_worker.finished.connect(self._on_audio_finished)
        self._audio_worker.start()

        sources = ", ".join(self.audio.source_names(segments))
        if self.audio.has_microphone(segments):
            self.status_message.emit(f"Preview playing with audio: {sources}")
        else:
            self.status_message.emit(f"Preview playing without microphone audio: {sources}")

        self._timer.start()

    def stop(self) -> None:
        was_playing = self._timer.isActive()
        self._timer.stop()

        worker = self._audio_worker
        self._audio_worker = None
        if worker is not None:
            worker.stop()
            if worker.isRunning():
                worker.wait(1000)

        if was_playing:
            self.playback_stopped.emit()

    def _advance(self) -> None:
        frame_count = self.project.frame_count()
        if frame_count <= 0:
            self.stop()
            return

        elapsed = max(0.0, time.perf_counter() - self._started_at)
        target = self._start_index + int(round(elapsed * self.project.fps))
        self._index = max(self._index, target)

        if self._index >= frame_count:
            self.project.set_timeline_index(frame_count)
            self.frame_changed.emit(frame_count)
            self.stop()
            return

        self.project.set_timeline_index(self._index)
        self.frame_changed.emit(self._index)
        self._index += 1

    def _on_audio_error(self, message: str) -> None:
        self.status_message.emit(f"Preview audio error: {message}")

    def _on_audio_finished(self) -> None:
        if self.sender() is self._audio_worker:
            self._audio_worker = None
