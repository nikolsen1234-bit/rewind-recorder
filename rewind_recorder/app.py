from __future__ import annotations

import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from pathlib import Path

from PySide6.QtWidgets import QApplication, QMessageBox

from rewind_recorder.config import APP_NAME
from rewind_recorder.main_window import MainWindow
from rewind_recorder.windows_api import enable_windows_dpi_awareness


def _log_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return base / "RewindRecorder" / "logs"


def _setup_logging() -> Path:
    log_dir = _log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "rewind_recorder.log"

    handler = RotatingFileHandler(log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    return log_path


def _install_excepthook() -> None:
    log = logging.getLogger("rewind_recorder")

    def hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, KeyboardInterrupt):
            sys.__excepthook__(exc_type, exc, tb)
            return
        log.critical("Unhandled exception", exc_info=(exc_type, exc, tb))
        message = "".join(traceback.format_exception_only(exc_type, exc)).strip()
        try:
            QMessageBox.critical(
                None, APP_NAME,
                f"An unexpected error occurred and was logged:\n\n{message}\n\n"
                f"Logs: {_log_dir() / 'rewind_recorder.log'}",
            )
        except Exception:
            pass

    sys.excepthook = hook


def main() -> int:
    log_path = _setup_logging()
    _install_excepthook()
    logging.getLogger("rewind_recorder").info("Starting %s, log file %s", APP_NAME, log_path)

    enable_windows_dpi_awareness()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
