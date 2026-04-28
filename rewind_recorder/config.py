from enum import Enum


class RecorderState(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    PAUSED = "paused"
    STOPPED = "stopped"


APP_NAME = "Rewind Recorder"
DEFAULT_FPS = 60
DEFAULT_OUTPUT_WIDTH = 1920
DEFAULT_OUTPUT_HEIGHT = 1080
JPEG_QUALITY = 90
AUDIO_SAMPLE_RATE = 48_000
AUDIO_CHANNELS = 1
AUTOSAVE_FILENAME = "autosave_project.json"
