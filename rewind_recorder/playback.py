import logging
import threading
import wave
from pathlib import Path

import numpy as np
from PySide6.QtCore import QThread, Signal

_log = logging.getLogger(__name__)
_PLAYBACK_CHUNK_FRAMES = 1024


class AudioPlaybackWorker(QThread):
    playback_error = Signal(str)

    def __init__(self, audio_path: Path, start_seconds: float, speaker_id: str | None) -> None:
        super().__init__()
        self.audio_path = audio_path
        self.start_seconds = max(0.0, start_seconds)
        self.speaker_id = speaker_id
        self._stop_event = threading.Event()

    def stop(self, *, wait_ms: int = 2000) -> None:
        self._stop_event.set()
        if self.isRunning():
            if not self.wait(wait_ms):
                _log.warning("AudioPlaybackWorker did not stop within %d ms", wait_ms)

    def run(self) -> None:
        try:
            import soundcard as sc

            speaker = self._find_speaker(sc)
            with wave.open(str(self.audio_path), "rb") as reader:
                sample_rate = reader.getframerate()
                channels = reader.getnchannels()
                if reader.getsampwidth() != 2:
                    raise RuntimeError("Preview playback supports 16-bit PCM WAV audio.")

                start_sample = min(int(round(self.start_seconds * sample_rate)), reader.getnframes())
                reader.setpos(start_sample)

                with speaker.player(samplerate=sample_rate, channels=channels) as player:
                    while not self._stop_event.is_set():
                        data = reader.readframes(_PLAYBACK_CHUNK_FRAMES)
                        if not data:
                            break
                        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
                        if channels > 1:
                            samples = samples.reshape(-1, channels)
                        else:
                            samples = samples.reshape(-1, 1)
                        player.play(samples)
        except Exception as exc:
            _log.exception("Audio playback worker failed")
            self.playback_error.emit(str(exc))

    def _find_speaker(self, sc) -> object:
        if self.speaker_id is None:
            return sc.default_speaker()
        for speaker in sc.all_speakers():
            if speaker.id == self.speaker_id or speaker.name == self.speaker_id:
                return speaker
        raise RuntimeError("Selected playback output device was not found.")
