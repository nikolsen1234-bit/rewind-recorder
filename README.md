# Rewind Recorder

[![CI](https://github.com/nikolsen1234-bit/rewind-recorder/actions/workflows/ci.yml/badge.svg)](https://github.com/nikolsen1234-bit/rewind-recorder/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Rewind Recorder is a local Windows screen recorder with rewind-and-overwrite editing.

Record a screen area, pause, scrub back to an earlier point, and continue recording from there. Everything after the playhead is replaced, so short mistakes can be fixed without starting over. The app also supports trimming, audio preview, and MP4 export.

## Highlights

- Area-based screen recording.
- 60 FPS capture by default.
- Visible mouse cursor capture.
- Microphone and system-audio recording.
- Rewind while paused, then record from that point.
- Mark In / Mark Out trimming.
- Preview playback with the edited audio mix.
- MP4 export with embedded audio.
- Local autosave for unfinished recordings.
- No account, cloud upload, watermark, browser extension, or Electron shell.

## Requirements

- Windows 10 or Windows 11.
- Python 3.11 or newer.

## Install

```powershell
python -m pip install git+https://github.com/nikolsen1234-bit/rewind-recorder.git
```

## Run

```powershell
rewind-recorder
```

Alternative launch commands:

```powershell
python -m rewind_recorder
python main.py
```

## Workflow

1. Select a recording area.
2. Choose input and output audio devices.
3. Start recording.
4. Pause when you want to review or edit.
5. Move the timeline backward to rewind.
6. Resume recording to replace everything after the playhead.
7. Use Mark In / Mark Out to remove unwanted sections.
8. Preview the result.
9. Stop and save as MP4.

## How Rewind Works

Rewind Recorder stores frames and audio in a temporary local project while recording. Moving the timeline does not delete anything by itself. When recording resumes from a paused timeline position, all later frames and matching audio are removed, then new capture is appended from that point.

## Audio

The input selector records microphone audio. The output selector records system audio through local loopback and is also used for preview playback.

Preview uses the same edited audio mix as export. If the project has no recorded audio, preview shows a warning instead of silently playing a muted result.

## Export

Exports are written as MP4 with embedded audio. Video is rendered at 1920x1080; recordings with a different aspect ratio are centered on a black canvas instead of being stretched.

If OpenCV cannot write MP4 directly on a system, the app renders a temporary AVI file and converts it to MP4 using the local FFmpeg binary provided by `imageio-ffmpeg`.

## Privacy

Rewind Recorder does not upload or transmit recordings. Temporary frames, audio segments, and autosave metadata stay on the local machine and are cleaned up after a successful save.

## Development

```powershell
git clone https://github.com/nikolsen1234-bit/rewind-recorder.git
cd rewind-recorder
python -m pip install -e .
python -m compileall -q main.py rewind_recorder
```

## Project Structure

```text
rewind_recorder/
  app.py          application entrypoint
  main_window.py  main GUI and workflow controller
  widgets.py      area selector, overlay, floating control, timeline
  capture.py      screen capture worker
  audio.py        microphone and system audio recorders
  playback.py     preview audio playback
  project.py      temporary frame project storage
  windows_api.py  Windows DPI, cursor, and overlay helpers
```

`main.py` is kept as a compatibility launcher. `pyproject.toml` contains package metadata, dependencies, and the `rewind-recorder` command.

## License

MIT License. See [LICENSE](LICENSE).
