from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from rewind_recorder.config import APP_NAME
from rewind_recorder.main_window import MainWindow
from rewind_recorder.windows_api import enable_windows_dpi_awareness


def main() -> int:
    enable_windows_dpi_awareness()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
