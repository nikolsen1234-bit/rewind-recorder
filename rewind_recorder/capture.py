import logging
import threading
import time

import cv2
import mss
import numpy as np
from PySide6.QtCore import QThread, Signal

from rewind_recorder.project import FrameProject
from rewind_recorder.types import CaptureArea
from rewind_recorder.windows_api import draw_cursor_overlay

_log = logging.getLogger(__name__)
_MAX_CONSECUTIVE_ERRORS = 10


class CaptureWorker(QThread):
    frame_saved = Signal(int)
    capture_error = Signal(str)

    def __init__(self, project: FrameProject, area: CaptureArea, fps: int) -> None:
        super().__init__()
        self.project = project
        self.area = area
        self.fps = fps
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        monitor = self.area.to_monitor()
        frame_interval = 1.0 / max(1, self.fps)
        consecutive_errors = 0

        try:
            with mss.mss() as screen_capture:
                while not self._stop_event.is_set():
                    started = time.perf_counter()
                    try:
                        raw_frame = screen_capture.grab(monitor)
                        bgra_frame = np.array(raw_frame)
                        if bgra_frame.ndim < 3 or bgra_frame.shape[0] == 0 or bgra_frame.shape[1] == 0:
                            raise ValueError(f"Capture returned empty frame shape={bgra_frame.shape}")
                        bgr_frame = cv2.cvtColor(bgra_frame, cv2.COLOR_BGRA2BGR)
                        try:
                            draw_cursor_overlay(bgr_frame, self.area)
                        except Exception as overlay_exc:
                            _log.debug("Cursor overlay failed (skipping): %s", overlay_exc)
                        frame_count = self.project.add_frame(bgr_frame)
                        self.frame_saved.emit(frame_count)
                        consecutive_errors = 0
                    except Exception as frame_exc:
                        consecutive_errors += 1
                        _log.warning(
                            "Frame capture error %d/%d: %s",
                            consecutive_errors, _MAX_CONSECUTIVE_ERRORS, frame_exc,
                        )
                        if consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                            self.capture_error.emit(
                                f"Capture failed {consecutive_errors} frames in a row: {frame_exc}"
                            )
                            return

                    elapsed = time.perf_counter() - started
                    remaining = frame_interval - elapsed
                    if remaining > 0:
                        self._stop_event.wait(remaining)
        except Exception as exc:
            _log.exception("Capture loop terminated by fatal error")
            self.capture_error.emit(str(exc))
