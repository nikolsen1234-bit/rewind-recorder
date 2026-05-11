# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.2] - 2026-05-11

### Fixed

- App-wide unhandled exceptions no longer abort the process silently. They are now logged to `%LOCALAPPDATA%/RewindRecorder/logs/rewind_recorder.log` and shown in a dialog.
- `closeEvent` stops the topmost-maintenance timer first, preventing a "C++ object already deleted" crash when the timer fires after overlay widgets are destroyed.
- A capture thread that does not stop within 3 seconds is no longer dropped while still running (which previously caused "QThread: Destroyed while thread is still running" aborts). It is now parked and reaped when it exits.
- Audio playback worker now blocks until clean exit when stopped, so the `QThread` is never destroyed mid-`run()`.
- A single bad frame no longer kills an active recording. Capture now tolerates up to 10 consecutive frame errors before aborting.
- Cursor overlay errors are caught per-frame instead of stopping recording.
- Microphone callback queue is now bounded; over-pressure drops blocks instead of leaking memory until the OS kills the process.
- Microphone writer thread no longer dies silently on disk errors; failures are logged and surfaced via `last_error`.
- `stream.stop()` and `stream.close()` are no longer in the same try block, so a failure in `stop()` no longer leaks the WASAPI handle.
- Export ffmpeg subprocess `TimeoutExpired` is now caught instead of bubbling out and crashing the export thread.
- Export tolerates occasional unreadable frames (skips with a log warning) instead of aborting the whole export and deleting the partial file.
- Preview no longer crashes when `cv2.imread` raises on a partially-flushed frame file written by the active capture thread.
- Autosave restore clamps `fps` to >=1, preventing `ZeroDivisionError` later in export.
- `format_seconds` clamps negative input, preventing garbled timecode display from rewind math.
- "Save As" can no longer be triggered re-entrantly via double-click during `processEvents`.
- `PreviewController` exposes `set_fps()` instead of being poked at private `_timer`.

### Changed

- Cached the resolved ffmpeg path on `VideoExporter` so it is not re-resolved per export.
- Added structured logging across the capture, audio, export, and main window subsystems for post-mortem diagnosis.

## [0.1.1] - 2026-04-28

### Fixed

- Preview now scales correctly when the window is resized.
- Record button restored in UI so users can start recording after stopping.
- FPS now synced across exporter, audio manager, and preview after autosave restore.

### Changed

- Deduplicated identical `_primary_action` / `_record_or_resume` methods.
- Merged `autosave.save()` and `save_with_locked_area()` into one method.
- Cleaned up layout spacing for consistency.
- Removed unused imports and unnecessary docstrings.

## [0.1.0] - 2026-04-28

### Added

- First public release.
- Record any selected part of your screen at 60 FPS.
- Pause, go back on the timeline, and record again from there. The new recording replaces everything after that point.
- Trim the start and end of a recording.
- Record microphone and system audio.
- Play back your recording with the edited audio.
- Export as MP4.
- Autosave while recording.

[Unreleased]: https://github.com/nikolsen1234-bit/rewind-recorder/compare/v0.1.2...HEAD
[0.1.2]: https://github.com/nikolsen1234-bit/rewind-recorder/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/nikolsen1234-bit/rewind-recorder/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/nikolsen1234-bit/rewind-recorder/releases/tag/v0.1.0
