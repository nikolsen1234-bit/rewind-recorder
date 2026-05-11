# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] - 2026-05-11

### Fixed

- Importing a clip no longer freezes the UI. Runs on a background `QThread` with live progress in the status bar. Window stays responsive; close-during-import cleanly cancels.
- Exporting no longer freezes the UI. Same background-thread pattern; status bar shows "Saving…" while ffmpeg works.
- Exported MP4 files are now H.264 (libx264 + AAC + `+faststart` moov) on every path, not just the AVI fallback. Previously the common path used the cv2 `mp4v` codec which produces non-baseline-profile MP4s that some browsers, Slack previews, and quick-look players refuse to play.
- Imported clip's source FPS is now read and the user is warned if it differs from the project FPS by more than 0.5 (previously the timeline silently played at the wrong speed).
- Cap on imported frame count (36 000 ≈ 10 min @ 60 fps) prevents the user from accidentally filling temp disk with a multi-GB 4K source.
- `_render_video`'s first-frame-search no longer re-iterates skipped frames in the main write loop (small perf, but correctness too — skipped paths were tried twice).
- `_mux_audio` `temp_output.replace(final_path)` now catches `OSError` (file locked by a media player) and reports it instead of crashing.
- Audio mix WAV file is now reliably cleaned up on every export path (success, mux failure, render failure) via a single `try/finally` in `export()`.
- Re-entrant import/export blocked: the buttons are disabled while a worker runs.

### Changed

- Pre-allocated and reused the letterbox canvas in `VideoExporter._frame_to_canvas` (was allocating a fresh 1920×1080×3 numpy array per frame; for a 10-min 60 fps render that was ~36 000 × 6 MB = ~220 GB of allocator churn).
- New `rewind_recorder/workers.py` with `ImportClipWorker` and `ExportWorker`. Both expose `progress`, `finished_ok`, `failed` signals.
- Bump to 0.3.0 (minor — visible behavior changes around imported-clip warnings and the new H.264 export default).

## [0.2.0] - 2026-05-11

### Added

- Real status bar at the bottom of the window. Status no longer hijacks the title bar.
- Menu bar: **File** (Save As, Import Clip, Quit) and **Help** (Open Logs Folder, About).
- About dialog showing version, log-file path, and the keyboard-shortcut cheat sheet.
- Tooltips on every button, combo, and the timeline. Shortcuts shown inline in button labels.
- Keyboard shortcuts: `F9` Record/Pause, `F10` Stop, `Space` Play preview, `[` Trim Start, `]` Trim End, `Ctrl+S` Save As, `Ctrl+Shift+L` open logs folder, `Ctrl+Q` Quit.
- First-run guidance in the empty preview area: "Step 1 — Select Area", then "Step 2 — hit Record" once an area is locked in.

### Changed

- Record button is visually promoted (red accent, larger height) so the primary action stands out.
- Time display is now a large monospace counter centred above the timeline, instead of a tiny grey label.
- "Mark In / Mark Out / Delete Range / Clear Range" renamed to "Trim Start / Trim End / Cut Out Section / Clear Marks" — friendlier vocabulary.
- "Input device / Output device" relabelled to "Microphone / System audio (loopback)" so the purpose is obvious.
- Bottom toolbar regrouped into three sections: **Record** (Select Area / Record / Stop), **Trim** (cut out a section), **Review & save** (Play / Import / Save As).

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

[Unreleased]: https://github.com/nikolsen1234-bit/rewind-recorder/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/nikolsen1234-bit/rewind-recorder/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/nikolsen1234-bit/rewind-recorder/compare/v0.1.2...v0.2.0
[0.1.2]: https://github.com/nikolsen1234-bit/rewind-recorder/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/nikolsen1234-bit/rewind-recorder/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/nikolsen1234-bit/rewind-recorder/releases/tag/v0.1.0
