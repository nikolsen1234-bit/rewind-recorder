import shutil
import subprocess
import sys
import wave
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from rewind_recorder.config import AUDIO_SAMPLE_RATE, DEFAULT_OUTPUT_HEIGHT, DEFAULT_OUTPUT_WIDTH
from rewind_recorder.errors import VideoWriterOpenError
from rewind_recorder.types import AudioSegment


@dataclass(frozen=True)
class ExportResult:
    path: Path
    used_fallback_codec: bool
    audio_muxed: bool


class VideoExporter:
    def __init__(self, fps: int) -> None:
        self.fps = fps
        self.output_width = DEFAULT_OUTPUT_WIDTH
        self.output_height = DEFAULT_OUTPUT_HEIGHT

    def export(
        self,
        frame_paths: list[Path],
        segments: list[AudioSegment],
        output_path: Path,
    ) -> ExportResult:
        video_path, used_fallback = self._render_video(frame_paths, output_path)
        audio_path = self.build_audio_mix(
            video_path.with_suffix(".wav"),
            segments,
            len(frame_paths),
        )

        if audio_path is None:
            return ExportResult(path=video_path, used_fallback_codec=used_fallback, audio_muxed=False)

        final_path, muxed, detail = self._mux_audio(video_path, audio_path)
        if not muxed:
            raise RuntimeError(
                "This recording has audio, but the audio could not be embedded into the video.\n\n"
                f"{detail}"
            )
        return ExportResult(path=final_path, used_fallback_codec=used_fallback, audio_muxed=True)

    def build_audio_mix(
        self,
        output_path: Path,
        segments: list[AudioSegment],
        frame_count: int,
        *,
        allow_silence: bool = True,
    ) -> Path | None:
        if frame_count <= 0:
            return None

        total_duration = frame_count / self.fps
        valid = [s for s in segments if s.path.exists() and s.duration_frames > 0]

        if not valid:
            if not allow_silence:
                return None
            return self._build_silent_audio(output_path, total_duration)

        ffmpeg = self._find_ffmpeg()
        if ffmpeg is None:
            raise RuntimeError("Embedding audio requires local FFmpeg from imageio-ffmpeg.")

        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.unlink(missing_ok=True)
        valid.sort(key=lambda s: s.timeline_start_frame)

        command = [ffmpeg, "-y"]
        filters: list[str] = []
        labels: list[str] = []

        for i, seg in enumerate(valid):
            command.extend(["-i", str(seg.path)])
            source_offset = max(0.0, (seg.source_start_frame - seg.record_start_frame) / self.fps)
            duration = max(0.0, (seg.source_end_frame - seg.source_start_frame) / self.fps)
            delay_ms = max(0, int(round((seg.timeline_start_frame / self.fps) * 1000)))
            label = f"a{i}"
            labels.append(f"[{label}]")
            filters.append(
                f"[{i}:a]"
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

        command.extend([
            "-filter_complex", ";".join(filters),
            "-map", "[mixed]",
            "-ac", "2",
            "-ar", "48000",
            "-c:a", "pcm_s16le",
            str(output_path),
        ])

        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            command, capture_output=True, text=True,
            creationflags=creationflags, timeout=3600,
        )
        if result.returncode != 0 or not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            detail = result.stderr.strip() or result.stdout.strip() or "FFmpeg did not create mixed audio."
            raise RuntimeError(f"Could not build embedded audio mix. {detail}")

        return output_path

    def _render_video(self, frame_paths: list[Path], requested_path: Path) -> tuple[Path, bool]:
        if not frame_paths:
            raise RuntimeError("No frames are available.")

        first_frame = cv2.imread(str(frame_paths[0]))
        if first_frame is None:
            raise RuntimeError(f"Could not read first frame: {frame_paths[0]}")

        height, width = first_frame.shape[:2]
        fps = float(self.fps)

        if requested_path.suffix.lower() == ".avi":
            return self._write_video(requested_path, "XVID", frame_paths, fps, width, height), False

        mp4_path = requested_path.with_suffix(".mp4")
        try:
            return self._write_video(mp4_path, "mp4v", frame_paths, fps, width, height), False
        except VideoWriterOpenError:
            avi_path = self._unique_path(requested_path.with_suffix(".avi"))
            return self._write_video(avi_path, "XVID", frame_paths, fps, width, height), True

    def _write_video(
        self,
        output_path: Path,
        codec: str,
        frame_paths: list[Path],
        fps: float,
        source_width: int,
        source_height: int,
    ) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        fourcc = cv2.VideoWriter_fourcc(*codec)
        writer = cv2.VideoWriter(str(output_path), fourcc, fps, (self.output_width, self.output_height))

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
                writer.write(self._frame_to_canvas(frame))
            completed = True
        finally:
            writer.release()

        if not completed:
            output_path.unlink(missing_ok=True)

        if not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            raise VideoWriterOpenError(f"OpenCV did not create a valid output file: {output_path}")

        return output_path

    def _frame_to_canvas(self, frame: np.ndarray) -> np.ndarray:
        src_h, src_w = frame.shape[:2]
        scale = min(self.output_width / src_w, self.output_height / src_h)
        scaled_w = max(2, int(round(src_w * scale)))
        scaled_h = max(2, int(round(src_h * scale)))
        scaled_w -= scaled_w % 2
        scaled_h -= scaled_h % 2

        resized = cv2.resize(frame, (scaled_w, scaled_h), interpolation=cv2.INTER_AREA)
        canvas = np.zeros((self.output_height, self.output_width, 3), dtype=np.uint8)
        x = (self.output_width - scaled_w) // 2
        y = (self.output_height - scaled_h) // 2
        canvas[y : y + scaled_h, x : x + scaled_w] = resized
        return canvas

    def _mux_audio(self, video_path: Path, audio_path: Path) -> tuple[Path, bool, str]:
        ffmpeg = self._find_ffmpeg()
        if ffmpeg is None:
            return video_path, False, "Embedding audio needs local FFmpeg. Install imageio-ffmpeg."

        final_path = video_path.with_suffix(".mp4")
        temp_output = final_path.with_name(f"{final_path.stem}_with_audio_tmp{final_path.suffix}")
        temp_output.unlink(missing_ok=True)

        if video_path.suffix.lower() == ".mp4":
            video_codec_args = ["-c:v", "copy"]
        else:
            video_codec_args = ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p"]

        command = [
            ffmpeg, "-y",
            "-i", str(video_path),
            "-i", str(audio_path),
            *video_codec_args,
            "-c:a", "aac", "-b:a", "192k",
            str(temp_output),
        ]
        creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        result = subprocess.run(
            command, capture_output=True, text=True,
            creationflags=creationflags, timeout=3600,
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

    def _build_silent_audio(self, output_path: Path, duration_seconds: float) -> Path | None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        sample_rate = AUDIO_SAMPLE_RATE
        channels = 2
        total_samples = max(1, int(round(duration_seconds * sample_rate)))
        bytes_per_frame = channels * 2

        with wave.open(str(output_path), "wb") as writer:
            writer.setnchannels(channels)
            writer.setsampwidth(2)
            writer.setframerate(sample_rate)
            remaining = total_samples
            while remaining > 0:
                chunk = min(remaining, 48_000)
                writer.writeframes(b"\x00" * chunk * bytes_per_frame)
                remaining -= chunk

        if not output_path.exists() or output_path.stat().st_size == 0:
            output_path.unlink(missing_ok=True)
            return None
        return output_path

    def _find_ffmpeg(self) -> str | None:
        path = shutil.which("ffmpeg")
        if path:
            return path
        try:
            import imageio_ffmpeg
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return None

    def _unique_path(self, path: Path) -> Path:
        if not path.exists():
            return path
        for i in range(1, 1000):
            candidate = path.with_name(f"{path.stem}_{i}{path.suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError(f"Could not find a free fallback filename near {path}")
