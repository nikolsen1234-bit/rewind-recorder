import threading
import time

import cv2
import mss
import numpy as np
from PySide6.QtCore import QThread, Signal

from rewind_recorder.project import FrameProject
from rewind_recorder.types import CaptureArea
from rewind_recorder.windows_api import draw_cursor_overlay


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

        try:
            with mss.mss() as screen_capture:
                while not self._stop_event.is_set():
                    started = time.perf_counter()
                    raw_frame = screen_capture.grab(monitor)
                    bgra_frame = np.array(raw_frame)
                    bgr_frame = cv2.cvtColor(bgra_frame, cv2.COLOR_BGRA2BGR)
                    draw_cursor_overlay(bgr_frame, self.area)
                    frame_count = self.project.add_frame(bgr_frame)
                    self.frame_saved.emit(frame_count)

                    elapsed = time.perf_counter() - started
                    remaining = frame_interval - elapsed
                    if remaining > 0:
                        self._stop_event.wait(remaining)
        except Exception as exc:
            self.capture_error.emit(str(exc))
