"""Whitelist for vulture: members that look unused but are reached via Qt
event dispatch, ctypes struct layout, or dataclass-by-name access."""

from rewind_recorder import audio, main_window, preview, widgets, windows_api

# Dataclass fields populated by info() and consumed by callers via attribute access.
audio.AudioRecordingInfo.active
audio.AudioRecordingInfo.started_at
audio.AudioRecordingInfo.stopped_at
audio.AudioRecordingInfo.last_status

# PortAudio callback contract — must accept these positional args.
audio.LocalMicrophoneRecorder._on_audio_block

# Qt event handlers, dispatched by the framework, not by Python code.
main_window.MainWindow.closeEvent
widgets.AreaSelector.paintEvent
widgets.AreaSelector.keyPressEvent
widgets.CaptureAreaOverlay.paintEvent
widgets.FloatingRecorderControl.paintEvent

# Public controller API kept for clarity even if not currently invoked.
preview.PreviewController.toggle

# Win32 struct layout — fields are read by user32/gdi32 via memory layout.
windows_api.CURSORINFO.cbSize
windows_api.BITMAPINFOHEADER.biSize
windows_api.BITMAPINFOHEADER.biWidth
windows_api.BITMAPINFOHEADER.biHeight
windows_api.BITMAPINFOHEADER.biPlanes
windows_api.BITMAPINFOHEADER.biBitCount
windows_api.BITMAPINFOHEADER.biCompression
