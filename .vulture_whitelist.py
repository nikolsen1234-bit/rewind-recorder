"""Whitelist for vulture: members that look unused but are reached via Qt
event dispatch or ctypes struct layout."""

from rewind_recorder import audio, main_window, widgets, windows_api

# PortAudio callback contract — must accept these positional args even if unused.
audio.LocalMicrophoneRecorder._on_audio_block

# Qt event handlers, dispatched by the framework, not by Python code.
main_window.MainWindow.closeEvent
widgets.AreaSelector.paintEvent
widgets.AreaSelector.keyPressEvent
widgets.CaptureAreaOverlay.paintEvent
widgets.FloatingRecorderControl.paintEvent

# Win32 struct layout — fields are read by user32/gdi32 via memory layout.
windows_api.CURSORINFO.cbSize
windows_api.BITMAPINFOHEADER.biSize
windows_api.BITMAPINFOHEADER.biWidth
windows_api.BITMAPINFOHEADER.biHeight
windows_api.BITMAPINFOHEADER.biPlanes
windows_api.BITMAPINFOHEADER.biBitCount
windows_api.BITMAPINFOHEADER.biCompression
