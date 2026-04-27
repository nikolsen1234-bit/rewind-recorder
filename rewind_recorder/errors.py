from __future__ import annotations


class VideoWriterOpenError(RuntimeError):
    """Raised when OpenCV cannot open or finish a requested video writer."""
