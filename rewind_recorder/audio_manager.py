from pathlib import Path

from rewind_recorder.audio import (
    BaseAudioRecorder,
    LocalMicrophoneRecorder,
    LocalSystemAudioRecorder,
)
from rewind_recorder.config import AUDIO_CHANNELS, AUDIO_SAMPLE_RATE
from rewind_recorder.types import AudioSegment


class AudioManager:
    def __init__(self, fps: int) -> None:
        self.fps = fps
        self.segments: list[AudioSegment] = []
        self.recorders: list[tuple[str, BaseAudioRecorder]] = []
        self.current_start_frame: int | None = None

    def start_recording(
        self,
        temp_dir: Path,
        frame_count: int,
        input_device: int | None,
        output_device: str | None,
        input_sample_rate: int | None = None,
    ) -> tuple[bool, list[str]]:
        self.stop_recording(end_frame=frame_count)
        self.current_start_frame = frame_count

        mic = LocalMicrophoneRecorder(
            output_dir=temp_dir,
            sample_rate=input_sample_rate or self._default_input_sample_rate(input_device),
            channels=AUDIO_CHANNELS,
            device=input_device,
        )
        system = LocalSystemAudioRecorder(
            output_dir=temp_dir,
            sample_rate=AUDIO_SAMPLE_RATE,
            channels=2,
            speaker_id=output_device,
        )

        self.recorders = []
        errors: list[str] = []

        if mic.start():
            self.recorders.append(("Microphone", mic))
        else:
            errors.append(f"microphone: {mic.last_error}")
            mic.cleanup(delete_file=True)

        if system.start():
            self.recorders.append(("System audio", system))
        else:
            errors.append(f"system audio: {system.last_error}")
            system.cleanup(delete_file=True)

        if not self.recorders:
            self.current_start_frame = None
            return False, errors

        return True, errors

    def stop_recording(self, end_frame: int | None = None, *, keep: bool = True) -> None:
        recorders = list(self.recorders)
        if not recorders:
            return

        start_frame = self.current_start_frame
        self.recorders = []
        self.current_start_frame = None

        for source_name, recorder in recorders:
            audio_info = recorder.stop()
            if not keep:
                recorder.cleanup(delete_file=True)
                continue

            if audio_info.output_path is None or audio_info.frames_written <= 0:
                recorder.cleanup(delete_file=True)
                continue

            start = max(0, int(start_frame or 0))
            end = max(start, int(end_frame or 0))
            if end == start:
                recorder.cleanup(delete_file=True)
                continue

            self.segments.append(AudioSegment(
                path=Path(audio_info.output_path),
                source_name=source_name,
                record_start_frame=start,
                source_start_frame=start,
                source_end_frame=end,
                timeline_start_frame=start,
                timeline_end_frame=end,
                sample_rate=audio_info.sample_rate,
                channels=audio_info.channels,
            ))

    def discard_after(self, frame_index: int) -> None:
        frame_index = max(0, frame_index)
        old_paths = {seg.path for seg in self.segments}
        kept: list[AudioSegment] = []

        for seg in self.segments:
            if seg.timeline_start_frame >= frame_index:
                continue
            if seg.timeline_end_frame > frame_index:
                trimmed = AudioSegment(
                    path=seg.path,
                    source_name=seg.source_name,
                    record_start_frame=seg.record_start_frame,
                    source_start_frame=seg.source_start_frame,
                    source_end_frame=seg.source_start_frame + (frame_index - seg.timeline_start_frame),
                    timeline_start_frame=seg.timeline_start_frame,
                    timeline_end_frame=frame_index,
                    sample_rate=seg.sample_rate,
                    channels=seg.channels,
                )
                kept.append(trimmed)
            else:
                kept.append(seg)

        self.segments = kept
        self._delete_unreferenced(old_paths)

    def delete_range(self, start: int, end: int) -> None:
        start, end = sorted((max(0, start), max(0, end)))
        if start == end:
            return

        removed_frames = end - start
        updated: list[AudioSegment] = []

        for seg in self.segments:
            if seg.timeline_end_frame <= start:
                updated.append(seg)
                continue

            if seg.timeline_start_frame >= end:
                shifted = AudioSegment(
                    path=seg.path,
                    source_name=seg.source_name,
                    record_start_frame=seg.record_start_frame,
                    source_start_frame=seg.source_start_frame,
                    source_end_frame=seg.source_end_frame,
                    timeline_start_frame=seg.timeline_start_frame - removed_frames,
                    timeline_end_frame=seg.timeline_end_frame - removed_frames,
                    sample_rate=seg.sample_rate,
                    channels=seg.channels,
                )
                updated.append(shifted)
                continue

            if seg.timeline_start_frame < start:
                left = AudioSegment(
                    path=seg.path,
                    source_name=seg.source_name,
                    record_start_frame=seg.record_start_frame,
                    source_start_frame=seg.source_start_frame,
                    source_end_frame=seg.source_start_frame + (start - seg.timeline_start_frame),
                    timeline_start_frame=seg.timeline_start_frame,
                    timeline_end_frame=start,
                    sample_rate=seg.sample_rate,
                    channels=seg.channels,
                )
                updated.append(left)

            if seg.timeline_end_frame > end:
                right_frames = seg.timeline_end_frame - end
                right = AudioSegment(
                    path=seg.path,
                    source_name=seg.source_name,
                    record_start_frame=seg.record_start_frame,
                    source_start_frame=seg.source_start_frame + (end - seg.timeline_start_frame),
                    source_end_frame=seg.source_end_frame,
                    timeline_start_frame=start,
                    timeline_end_frame=start + right_frames,
                    sample_rate=seg.sample_rate,
                    channels=seg.channels,
                )
                updated.append(right)

        self.segments = updated

    def clear(self, *, delete_files: bool = False) -> None:
        self.stop_recording(end_frame=0, keep=not delete_files)
        for _name, recorder in list(self.recorders):
            recorder.cleanup(delete_file=delete_files)
        self.recorders = []

        if delete_files:
            for seg in self.segments:
                seg.path.unlink(missing_ok=True)

        self.segments = []
        self.current_start_frame = None

    def mix_segments(self) -> list[AudioSegment]:
        return [s for s in self.segments if s.path.exists() and s.duration_frames > 0]

    def source_names(self, segments: list[AudioSegment] | None = None) -> list[str]:
        names: list[str] = []
        for seg in (segments or self.segments):
            if seg.source_name not in names:
                names.append(seg.source_name)
        return names

    def has_microphone(self, segments: list[AudioSegment] | None = None) -> bool:
        return any(s.source_name.lower() == "microphone" for s in (segments or self.segments))

    def _default_input_sample_rate(self, device: int | None) -> int:
        try:
            import sounddevice as sd
            info = sd.query_devices(device, kind="input")
            rate = int(round(float(info.get("default_samplerate", AUDIO_SAMPLE_RATE))))
            if rate > 0:
                return rate
        except Exception:
            pass
        return AUDIO_SAMPLE_RATE

    def _delete_unreferenced(self, old_paths: set[Path]) -> None:
        current = {seg.path for seg in self.segments}
        for path in old_paths - current:
            path.unlink(missing_ok=True)
