import abc
import queue
import tempfile
import threading
import time
import wave
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import Any, Self


class AudioRecorderError(RuntimeError):
    """Raised when audio recording cannot start or stop cleanly."""


@dataclass(frozen=True)
class AudioRecordingInfo:
    output_path: Path | None
    active: bool
    started_at: float | None
    stopped_at: float | None
    duration_seconds: float
    sample_rate: int
    channels: int
    frames_written: int
    last_error: str | None = None
    last_status: str | None = None


class BaseAudioRecorder(abc.ABC):
    """Abstract base class providing shared state and helpers for audio recorders."""

    def __init__(
        self,
        output_path: str | Path | None = None,
        *,
        output_dir: str | Path | None = None,
        sample_rate: int = 48_000,
        channels: int = 1,
        auto_cleanup_empty: bool = True,
    ) -> None:
        self.requested_output_path = Path(output_path) if output_path is not None else None
        self.output_dir = Path(output_dir) if output_dir is not None else None
        self.sample_rate = int(sample_rate)
        self.channels = int(channels)
        self.auto_cleanup_empty = auto_cleanup_empty

        self._lock = threading.RLock()
        self._output_path: Path | None = None
        self._started_at: float | None = None
        self._stopped_at: float | None = None
        self._frames_written = 0
        self._last_error: str | None = None
        self._last_status: str | None = None

    @property
    def output_path(self) -> Path | None:
        return self._output_path

    @property
    @abc.abstractmethod
    def is_recording(self) -> bool:
        ...

    @property
    def last_error(self) -> str | None:
        return self._last_error

    @property
    def duration_seconds(self) -> float:
        if self._started_at is None:
            return 0.0
        end = self._stopped_at if self._stopped_at is not None else time.perf_counter()
        return max(0.0, end - self._started_at)

    @property
    def frames_written(self) -> int:
        return self._frames_written

    @abc.abstractmethod
    def start(self, *, raise_on_error: bool = False) -> bool:
        ...

    @abc.abstractmethod
    def stop(self, *, timeout: float = 5.0, raise_on_error: bool = False) -> AudioRecordingInfo:
        ...

    @abc.abstractmethod
    def cleanup(self, *, delete_file: bool = False, timeout: float = 5.0) -> None:
        ...

    def info(self) -> AudioRecordingInfo:
        return AudioRecordingInfo(
            output_path=self._output_path,
            active=self.is_recording,
            started_at=self._started_at,
            stopped_at=self._stopped_at,
            duration_seconds=self.duration_seconds,
            sample_rate=self.sample_rate,
            channels=self.channels,
            frames_written=self._frames_written,
            last_error=self._last_error,
            last_status=self._last_status,
        )

    def _resolve_output_path(self, prefix: str) -> Path:
        if self.requested_output_path is not None:
            return self.requested_output_path

        output_dir = self.output_dir if self.output_dir is not None else Path(tempfile.gettempdir())
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        return output_dir / f"{prefix}_{stamp}.wav"

    def _delete_output_file(self) -> None:
        if self._output_path is None:
            return
        try:
            self._output_path.unlink(missing_ok=True)
        except OSError as exc:
            self._last_error = str(exc)

    def __enter__(self) -> Self:
        self.start(raise_on_error=True)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.cleanup()


class LocalMicrophoneRecorder(BaseAudioRecorder):
    """Record a selected local microphone/input endpoint to a PCM WAV file.

    The class intentionally depends only on ``sounddevice`` plus Python's
    standard ``wave`` module. ``sounddevice`` is imported lazily so the main
    application can still launch on systems where microphone recording is not
    installed or available.
    """

    def __init__(
        self,
        output_path: str | Path | None = None,
        *,
        output_dir: str | Path | None = None,
        sample_rate: int = 48_000,
        channels: int = 1,
        device: int | str | None = None,
        blocksize: int = 0,
        auto_cleanup_empty: bool = True,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be greater than zero")
        if channels <= 0:
            raise ValueError("channels must be greater than zero")
        if blocksize < 0:
            raise ValueError("blocksize cannot be negative")

        super().__init__(
            output_path=output_path,
            output_dir=output_dir,
            sample_rate=sample_rate,
            channels=channels,
            auto_cleanup_empty=auto_cleanup_empty,
        )

        self.requested_channels = int(channels)
        self.device = device
        self.blocksize = int(blocksize)

        self._stream: Any | None = None
        self._wave_file: wave.Wave_write | None = None
        self._writer_thread: threading.Thread | None = None
        self._writer_queue: queue.SimpleQueue[bytes | None] | None = None
        self._sd: Any | None = None

    @property
    def is_recording(self) -> bool:
        return self._stream is not None

    def start(self, *, raise_on_error: bool = False) -> bool:
        """Start recording.

        Returns ``True`` when audio capture starts. Returns ``False`` and stores
        a human-readable ``last_error`` when dependencies, devices, or streams
        are unavailable. Pass ``raise_on_error=True`` to raise instead.
        """

        with self._lock:
            if self.is_recording:
                return True

            self._last_error = None
            self._last_status = None
            self._stopped_at = None
            self._frames_written = 0
            self._output_path = self._resolve_output_path("rewind_mic")

            try:
                sd = self._import_sounddevice()
                self._sd = sd
                self.channels = self._available_channel_count(sd)

                self._output_path.parent.mkdir(parents=True, exist_ok=True)
                self._wave_file = wave.open(str(self._output_path), "wb")
                self._wave_file.setnchannels(self.channels)
                self._wave_file.setsampwidth(2)
                self._wave_file.setframerate(self.sample_rate)

                self._writer_queue = queue.SimpleQueue()
                self._writer_thread = threading.Thread(
                    target=self._writer_loop,
                    name="LocalMicrophoneRecorderWriter",
                    daemon=True,
                )
                self._writer_thread.start()

                self._stream = sd.RawInputStream(
                    samplerate=self.sample_rate,
                    blocksize=self.blocksize,
                    device=self.device,
                    channels=self.channels,
                    dtype="int16",
                    callback=self._on_audio_block,
                )
                self._stream.start()
                self._started_at = time.perf_counter()
                return True
            except Exception as exc:
                self._last_error = str(exc)
                self._reset_after_failed_start()
                if raise_on_error:
                    raise AudioRecorderError(self._last_error) from exc
                return False

    def stop(self, *, timeout: float = 5.0, raise_on_error: bool = False) -> AudioRecordingInfo:
        """Stop recording and close the WAV file."""

        with self._lock:
            stream = self._stream
            self._stream = None

        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception as exc:
                self._last_error = str(exc)
                if raise_on_error:
                    raise AudioRecorderError(self._last_error) from exc

        self._stopped_at = time.perf_counter() if self._started_at is not None else None
        self._close_writer(timeout=timeout)

        if self.auto_cleanup_empty and self._frames_written == 0 and self._output_path is not None:
            self._delete_output_file()

        return self.info()

    def cleanup(self, *, delete_file: bool = False, timeout: float = 5.0) -> None:
        """Release resources, optionally deleting the recorded WAV file."""

        if self.is_recording:
            self.stop(timeout=timeout)
        else:
            self._close_writer(timeout=timeout)

        if delete_file:
            self._delete_output_file()

    def _import_sounddevice(self) -> Any:
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise AudioRecorderError(
                "Microphone recording requires the 'sounddevice' package. "
                "Install it with: python -m pip install sounddevice"
            ) from exc
        return sd

    def _available_channel_count(self, sd: Any) -> int:
        try:
            device_info = sd.query_devices(self.device, "input")
        except Exception as exc:
            raise AudioRecorderError(f"No usable microphone input device found: {exc}") from exc

        max_channels = int(device_info.get("max_input_channels", 0))
        if max_channels < 1:
            raise AudioRecorderError("The selected input device has no microphone channels.")

        return min(self.requested_channels, max_channels)

    def _on_audio_block(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        if status:
            self._last_status = str(status)

        writer_queue = self._writer_queue
        if writer_queue is None:
            return

        try:
            writer_queue.put(bytes(indata))
        except Exception as exc:
            self._last_error = str(exc)

    def _writer_loop(self) -> None:
        writer_queue = self._writer_queue
        if writer_queue is None:
            return

        while True:
            chunk = writer_queue.get()
            if chunk is None:
                break

            wave_file = self._wave_file
            if wave_file is None:
                continue

            wave_file.writeframes(chunk)
            bytes_per_frame = self.channels * 2
            if bytes_per_frame > 0:
                self._frames_written += len(chunk) // bytes_per_frame

    def _close_writer(self, *, timeout: float) -> None:
        writer_queue = self._writer_queue
        writer_thread = self._writer_thread

        if writer_queue is not None:
            writer_queue.put(None)

        if writer_thread is not None:
            writer_thread.join(timeout=max(0.0, timeout))
            if writer_thread.is_alive():
                self._last_error = "Timed out while closing the microphone WAV writer."

        wave_file = self._wave_file
        self._writer_queue = None
        self._writer_thread = None
        self._wave_file = None

        if wave_file is not None:
            try:
                wave_file.close()
            except Exception as exc:
                self._last_error = str(exc)

    def _reset_after_failed_start(self) -> None:
        stream = self._stream
        self._stream = None
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass

        self._close_writer(timeout=1.0)
        if self._output_path is not None:
            self._delete_output_file()


class LocalSystemAudioRecorder(BaseAudioRecorder):
    """Record a selected Windows speaker/output device through local loopback."""

    def __init__(
        self,
        output_path: str | Path | None = None,
        *,
        output_dir: str | Path | None = None,
        sample_rate: int = 48_000,
        channels: int = 2,
        speaker_id: str | None = None,
        block_frames: int = 2048,
        auto_cleanup_empty: bool = True,
    ) -> None:
        if sample_rate <= 0:
            raise ValueError("sample_rate must be greater than zero")
        if channels <= 0:
            raise ValueError("channels must be greater than zero")
        if block_frames <= 0:
            raise ValueError("block_frames must be greater than zero")

        super().__init__(
            output_path=output_path,
            output_dir=output_dir,
            sample_rate=sample_rate,
            channels=channels,
            auto_cleanup_empty=auto_cleanup_empty,
        )

        self.speaker_id = speaker_id
        self.block_frames = int(block_frames)

        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_recording(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self, *, raise_on_error: bool = False) -> bool:
        with self._lock:
            if self.is_recording:
                return True

            self._last_error = None
            self._last_status = None
            self._stopped_at = None
            self._frames_written = 0
            self._stop_event.clear()
            self._ready_event.clear()
            self._output_path = self._resolve_output_path("rewind_system")

            try:
                self._output_path.parent.mkdir(parents=True, exist_ok=True)
                self._select_speaker()
                self._thread = threading.Thread(
                    target=self._record_loop,
                    name="LocalSystemAudioRecorder",
                    daemon=True,
                )
                self._thread.start()
                if not self._ready_event.wait(timeout=5.0):
                    self._last_error = "Timed out while opening the selected system audio recorder."
                    self.cleanup(delete_file=True, timeout=1.0)
                    if raise_on_error:
                        raise AudioRecorderError(self._last_error)
                    return False
                if self._last_error is not None:
                    error = self._last_error
                    self.cleanup(delete_file=True, timeout=1.0)
                    if raise_on_error:
                        raise AudioRecorderError(error)
                    return False
                if self._thread is None or not self._thread.is_alive():
                    self._last_error = "Selected system audio recorder closed before recording started."
                    self.cleanup(delete_file=True, timeout=1.0)
                    if raise_on_error:
                        raise AudioRecorderError(self._last_error)
                    return False
                self._started_at = time.perf_counter()
                return True
            except Exception as exc:
                self._last_error = str(exc)
                self.cleanup(delete_file=True, timeout=1.0)
                if raise_on_error:
                    raise AudioRecorderError(self._last_error) from exc
                return False

    def stop(self, *, timeout: float = 5.0, raise_on_error: bool = False) -> AudioRecordingInfo:
        self._stop_event.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=max(0.0, timeout))
            if thread.is_alive():
                self._last_error = "Timed out while closing the system audio recorder."
                if raise_on_error:
                    raise AudioRecorderError(self._last_error)
        self._thread = None
        self._stopped_at = time.perf_counter() if self._started_at is not None else None

        if self.auto_cleanup_empty and self._frames_written == 0 and self._output_path is not None:
            self._delete_output_file()

        return self.info()

    def cleanup(self, *, delete_file: bool = False, timeout: float = 5.0) -> None:
        if self.is_recording:
            self.stop(timeout=timeout)
        if delete_file:
            self._delete_output_file()

    def _record_loop(self) -> None:
        try:
            import numpy as np
            import soundcard as sc

            speaker = self._select_speaker(sc)
            assert self._output_path is not None

            with wave.open(str(self._output_path), "wb") as wave_file:
                wave_file.setnchannels(self.channels)
                wave_file.setsampwidth(2)
                wave_file.setframerate(self.sample_rate)

                with sc.get_microphone(speaker.id, include_loopback=True).recorder(
                    samplerate=self.sample_rate,
                    channels=self.channels,
                ) as recorder:
                    self._last_status = "recording"
                    self._ready_event.set()
                    while not self._stop_event.is_set():
                        data = recorder.record(numframes=self.block_frames)
                        if data is None or len(data) == 0:
                            continue
                        if data.ndim == 1:
                            data = data.reshape(-1, 1)
                        if data.shape[1] != self.channels:
                            if data.shape[1] > self.channels:
                                data = data[:, : self.channels]
                            else:
                                data = np.repeat(data[:, :1], self.channels, axis=1)
                        pcm = np.clip(data, -1.0, 1.0)
                        pcm = (pcm * 32767.0).astype("<i2", copy=False)
                        wave_file.writeframes(pcm.tobytes())
                        self._frames_written += int(pcm.shape[0])
        except Exception as exc:
            self._last_error = str(exc)
            self._ready_event.set()
        finally:
            self._ready_event.set()

    def _select_speaker(self, sc: Any | None = None) -> Any:
        if sc is None:
            import soundcard as sc

        if self.speaker_id is None:
            return sc.default_speaker()

        for speaker in sc.all_speakers():
            if speaker.id == self.speaker_id or speaker.name == self.speaker_id:
                return speaker
        raise AudioRecorderError("Selected output/system audio device was not found.")
