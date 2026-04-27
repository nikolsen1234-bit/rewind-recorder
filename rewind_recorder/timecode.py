from __future__ import annotations


def format_seconds(seconds: float) -> str:
    milliseconds = int(round((seconds - int(seconds)) * 1000))
    whole_seconds = int(seconds)

    if milliseconds == 1000:
        whole_seconds += 1
        milliseconds = 0

    hours = whole_seconds // 3600
    minutes = (whole_seconds % 3600) // 60
    secs = whole_seconds % 60

    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"
    return f"{minutes:02d}:{secs:02d}.{milliseconds:03d}"
