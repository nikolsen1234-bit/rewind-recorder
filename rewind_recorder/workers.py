from __future__ import annotations

import logging
import threading
from pathlib import Path

import cv2
from PySide6.QtCore import QThread, Signal

from rewind_recorder.export import ExportResult, VideoExporter
from rewind_recorder.project import FrameProject
from rewind_recorder.types import AudioSegment

_log = logging.getLogger(__name__)

_IMPORT_PROGRESS_EVERY = 30


class ImportClipWorker(QThread):
    progress = Signal(int, int)
    finished_ok = Signal(int, float)
    failed = Signal(str)

    def __init__(self, project: FrameProject, clip_path: Path, max_frames: int) -> None:
        super().__init__()
        self.project = project
        self.clip_path = clip_path
        self.max_frames = max_frames
        self._stop_event = threading.Event()

    def cancel(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        cap: cv2.VideoCapture | None = None
        try:
            cap = cv2.VideoCapture(str(self.clip_path))
            if not cap.isOpened():
                self.failed.emit(f"Could not open video file: {self.clip_path}")
                return

            source_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
            total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            target = total if total > 0 else self.max_frames
            if target > self.max_frames:
                target = self.max_frames

            imported = 0
            while not self._stop_event.is_set():
                ok, frame = cap.read()
                if not ok:
                    break
                self.project.add_frame(frame)
                imported += 1
                if imported % _IMPORT_PROGRESS_EVERY == 0:
                    self.progress.emit(imported, target)
                if imported >= self.max_frames:
                    break

            self.progress.emit(imported, target)
            self.finished_ok.emit(imported, source_fps)
        except Exception as exc:
            _log.exception("import worker failed")
            self.failed.emit(str(exc))
        finally:
            if cap is not None:
                cap.release()


class ExportWorker(QThread):
    progress = Signal(str)
    finished_ok = Signal(object)
    failed = Signal(str)

    def __init__(
        self,
        exporter: VideoExporter,
        frame_paths: list[Path],
        segments: list[AudioSegment],
        output_path: Path,
    ) -> None:
        super().__init__()
        self.exporter = exporter
        self.frame_paths = frame_paths
        self.segments = segments
        self.output_path = output_path

    def run(self) -> None:
        try:
            self.progress.emit("Rendering video...")
            result: ExportResult = self.exporter.export(
                self.frame_paths, self.segments, self.output_path,
            )
            self.finished_ok.emit(result)
        except Exception as exc:
            _log.exception("export worker failed")
            self.failed.emit(str(exc))
