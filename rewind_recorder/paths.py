from __future__ import annotations

import os
import sys
from pathlib import Path

LOG_FILENAME = "rewind_recorder.log"


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "RewindRecorder"


def log_dir() -> Path:
    return app_data_dir() / "logs"


def log_path() -> Path:
    return log_dir() / LOG_FILENAME
