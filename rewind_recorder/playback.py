from __future__ import annotations

import threading
import wave
from pathlib import Path
from typing import Optional

import numpy as np
from PySide6.QtCore import QThread, Signal


class AudioPlaybackWorker(QThread):
    playback_error = Signal(str)

    def __init__(self, audio_path: Path, start_seconds: float, speaker_id: Optional[str]) -> None:
        super().__init__()
        self.audio_path = audio_path
        self.start_seconds = max(0.0, start_seconds)
        self.speaker_id = speaker_id
        self._stop_event = threading.Event()

    def stop(self) -> None:
        self._stop_event.set()

    def run(self) -> None:
        try:
            import soundcard as sc

            speaker = self.selected_speaker(sc)
            with wave.open(str(self.audio_path), "rb") as reader:
                sample_rate = reader.getframerate()
                channels = reader.getnchannels()
                sample_width = reader.getsampwidth()
                if sample_width != 2:
                    raise RuntimeError("Preview playback supports 16-bit PCM WAV audio.")

                start_sample = min(int(round(self.start_seconds * sample_rate)), reader.getnframes())
                reader.setpos(start_sample)

                with speaker.player(samplerate=sample_rate, channels=channels) as player:
                    while not self._stop_event.is_set():
                        data = reader.readframes(2048)
                        if not data:
                            break
                        samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
                        if channels > 1:
                            samples = samples.reshape(-1, channels)
                        else:
                            samples = samples.reshape(-1, 1)
                        player.play(samples)
        except Exception as exc:
            self.playback_error.emit(str(exc))

    def selected_speaker(self, sc) -> object:
        if self.speaker_id is None:
            return sc.default_speaker()
        for speaker in sc.all_speakers():
            if speaker.id == self.speaker_id or speaker.name == self.speaker_id:
                return speaker
        raise RuntimeError("Selected playback output device was not found.")
