import shutil
import tempfile
import threading
from pathlib import Path

import cv2
import numpy as np

from rewind_recorder.config import DEFAULT_FPS, JPEG_QUALITY, RecorderState
from rewind_recorder.types import CaptureArea


class FrameProject:
    def __init__(self, fps: int = DEFAULT_FPS) -> None:
        self.fps = fps
        self.area: CaptureArea | None = None
        self.temp_dir: Path | None = None
        self.frames: list[Path] = []
        self.timeline_index = 0
        self.state = RecorderState.IDLE
        self.cut_start: int | None = None
        self.cut_end: int | None = None
        self.next_frame_id = 0
        self._lock = threading.RLock()

    def set_area(self, area: CaptureArea) -> None:
        with self._lock:
            self.area = area

    def ensure_temp_dir(self) -> Path:
        with self._lock:
            if self.temp_dir is None:
                self.temp_dir = Path(tempfile.mkdtemp(prefix="rewind_recorder_"))
            return self.temp_dir

    def frame_count(self) -> int:
        with self._lock:
            return len(self.frames)

    def has_frames(self) -> bool:
        return self.frame_count() > 0

    def snapshot_frame_paths(self) -> list[Path]:
        with self._lock:
            return list(self.frames)

    def preview_frame_path(self, timeline_index: int) -> Path | None:
        with self._lock:
            if not self.frames:
                return None
            if timeline_index <= 0:
                return self.frames[0]
            return self.frames[min(timeline_index - 1, len(self.frames) - 1)]

    def get_timeline_index(self) -> int:
        with self._lock:
            return self.timeline_index

    def set_timeline_index(self, index: int) -> int:
        with self._lock:
            self.timeline_index = max(0, min(index, len(self.frames)))
            return self.timeline_index

    def reset_cut_marks(self) -> None:
        with self._lock:
            self.cut_start = None
            self.cut_end = None

    def add_frame(self, frame: np.ndarray) -> int:
        temp_dir = self.ensure_temp_dir()

        with self._lock:
            frame_path = temp_dir / f"frame_{self.next_frame_id:09d}.jpg"
            self.next_frame_id += 1

        ok = cv2.imwrite(
            str(frame_path),
            frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY],
        )
        if not ok:
            raise RuntimeError(f"Could not write frame to {frame_path}")

        with self._lock:
            self.frames.append(frame_path)
            self.timeline_index = len(self.frames)
            return self.timeline_index

    def truncate_after(self, index: int) -> int:
        with self._lock:
            index = max(0, min(index, len(self.frames)))
            removed = self.frames[index:]
            self.frames = self.frames[:index]
            self.timeline_index = index
            self.cut_start = None
            self.cut_end = None

        for path in removed:
            path.unlink(missing_ok=True)

        return len(removed)

    def delete_range(self, start: int, end: int) -> int:
        with self._lock:
            count = len(self.frames)
            start = max(0, min(start, count))
            end = max(0, min(end, count))
            if start > end:
                start, end = end, start
            if start == end:
                return 0

            removed = self.frames[start:end]
            self.frames = self.frames[:start] + self.frames[end:]
            self.timeline_index = min(start, len(self.frames))
            self.cut_start = None
            self.cut_end = None

        for path in removed:
            path.unlink(missing_ok=True)

        return len(removed)

    def clear_frames(self) -> None:
        with self._lock:
            temp_dir = self.temp_dir
            self.temp_dir = None
            self.frames = []
            self.timeline_index = 0
            self.cut_start = None
            self.cut_end = None
            self.next_frame_id = 0

        if temp_dir is not None:
            shutil.rmtree(temp_dir, ignore_errors=True)
