# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/nikolsen1234-bit/rewind-recorder/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/nikolsen1234-bit/rewind-recorder/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/nikolsen1234-bit/rewind-recorder/releases/tag/v0.1.0
