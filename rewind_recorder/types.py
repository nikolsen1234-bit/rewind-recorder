from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CaptureArea:
    x: int
    y: int
    width: int
    height: int

    @staticmethod
    def normalized(x: int, y: int, width: int, height: int) -> "CaptureArea":
        w = max(2, int(width))
        h = max(2, int(height))
        w -= w % 2
        h -= h % 2
        return CaptureArea(x=int(x), y=int(y), width=w, height=h)

    def to_monitor(self) -> dict[str, int]:
        return {"left": self.x, "top": self.y, "width": self.width, "height": self.height}


@dataclass
class AudioSegment:
    path: Path
    source_name: str
    record_start_frame: int
    source_start_frame: int
    source_end_frame: int
    timeline_start_frame: int
    timeline_end_frame: int
    sample_rate: int
    channels: int

    @property
    def duration_frames(self) -> int:
        return self.timeline_end_frame - self.timeline_start_frame

    def to_json(self) -> dict:
        return {
            "path": str(self.path),
            "source_name": self.source_name,
            "record_start_frame": self.record_start_frame,
            "source_start_frame": self.source_start_frame,
            "source_end_frame": self.source_end_frame,
            "timeline_start_frame": self.timeline_start_frame,
            "timeline_end_frame": self.timeline_end_frame,
            "sample_rate": self.sample_rate,
            "channels": self.channels,
        }
