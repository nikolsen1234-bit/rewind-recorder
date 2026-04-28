import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from rewind_recorder.audio_manager import AudioManager
from rewind_recorder.config import AUTOSAVE_FILENAME, DEFAULT_FPS, RecorderState
from rewind_recorder.project import FrameProject
from rewind_recorder.types import AudioSegment, CaptureArea


@dataclass
class RestoreResult:
    restored: bool
    area: CaptureArea | None = None
    locked_area: CaptureArea | None = None
    error: str | None = None


class AutosaveManager:
    def __init__(self, project: FrameProject, audio: AudioManager) -> None:
        self.project = project
        self.audio = audio

    def save(self, locked_area: CaptureArea | None = None) -> None:
        path = self._autosave_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        data = {
            "version": 1,
            "fps": self.project.fps,
            "area": self._area_to_dict(self.project.area),
            "locked_capture_area": self._area_to_dict(locked_area),
            "temp_dir": str(self.project.temp_dir) if self.project.temp_dir is not None else None,
            "frames": [str(p) for p in self.project.snapshot_frame_paths()],
            "timeline_index": self.project.get_timeline_index(),
            "cut_start": self.project.cut_start,
            "cut_end": self.project.cut_end,
            "next_frame_id": self.project.next_frame_id,
            "audio_segments": [seg.to_json() for seg in self.audio.segments],
        }

        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        temp.replace(path)

    def clear(self) -> None:
        path = self._autosave_path()
        path.unlink(missing_ok=True)
        path.with_suffix(".tmp").unlink(missing_ok=True)

    def restore(self) -> RestoreResult:
        path = self._autosave_path()
        if not path.exists():
            return RestoreResult(restored=False)

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return self._apply_restore(data)
        except Exception as exc:
            return RestoreResult(restored=False, error=str(exc))

    def _apply_restore(self, data: dict) -> RestoreResult:
        frame_paths = [Path(v) for v in data.get("frames", [])]
        frame_paths = [p for p in frame_paths if p.exists()]
        raw_area = data.get("area")

        if raw_area is None and not frame_paths:
            self.clear()
            return RestoreResult(restored=False)

        area = CaptureArea.normalized(**raw_area) if raw_area else None

        self.project.fps = int(data.get("fps", DEFAULT_FPS))
        self.project.area = area
        self.project.temp_dir = Path(data["temp_dir"]) if data.get("temp_dir") else None
        self.project.frames = frame_paths
        self.project.timeline_index = max(0, min(int(data.get("timeline_index", len(frame_paths))), len(frame_paths)))
        self.project.cut_start = self._clamp_optional(data.get("cut_start"), len(frame_paths))
        self.project.cut_end = self._clamp_optional(data.get("cut_end"), len(frame_paths))
        self.project.next_frame_id = max(
            int(data.get("next_frame_id", 0)),
            self._next_id_from_frames(frame_paths),
        )
        self.project.state = RecorderState.PAUSED if frame_paths else RecorderState.IDLE

        raw_locked = data.get("locked_capture_area") or raw_area
        locked_area = CaptureArea.normalized(**raw_locked) if raw_locked else None

        self.audio.segments = self._restore_segments(data.get("audio_segments", []), len(frame_paths))
        self.audio.current_start_frame = None

        return RestoreResult(restored=True, area=area, locked_area=locked_area)

    def _restore_segments(self, raw_segments: object, frame_count: int) -> list[AudioSegment]:
        if not isinstance(raw_segments, list):
            return []

        restored: list[AudioSegment] = []
        for raw in raw_segments:
            if not isinstance(raw, dict):
                continue
            path = Path(str(raw.get("path", "")))
            if not path.exists():
                continue

            start = max(0, min(int(raw.get("timeline_start_frame", 0)), frame_count))
            end = max(start, min(int(raw.get("timeline_end_frame", 0)), frame_count))
            if end == start:
                continue

            restored.append(AudioSegment(
                path=path,
                source_name=str(raw.get("source_name", "Audio")),
                record_start_frame=int(raw.get("record_start_frame", 0)),
                source_start_frame=int(raw.get("source_start_frame", 0)),
                source_end_frame=int(raw.get("source_end_frame", 0)),
                timeline_start_frame=start,
                timeline_end_frame=end,
                sample_rate=int(raw.get("sample_rate", 0)),
                channels=int(raw.get("channels", 0)),
            ))

        return restored

    def _area_to_dict(self, area: CaptureArea | None) -> dict | None:
        if area is None:
            return None
        return {"x": area.x, "y": area.y, "width": area.width, "height": area.height}

    @staticmethod
    def _clamp_optional(value: object, frame_count: int) -> int | None:
        if value is None:
            return None
        return max(0, min(int(value), frame_count))

    @staticmethod
    def _next_id_from_frames(frame_paths: list[Path]) -> int:
        highest = -1
        for p in frame_paths:
            try:
                highest = max(highest, int(p.stem.rsplit("_", 1)[1]))
            except (IndexError, ValueError):
                continue
        return highest + 1

    @staticmethod
    def _app_data_dir() -> Path:
        if sys.platform == "win32":
            base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        else:
            base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        return base / "RewindRecorder"

    def _autosave_path(self) -> Path:
        return self._app_data_dir() / AUTOSAVE_FILENAME
