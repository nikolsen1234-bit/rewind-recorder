from __future__ import annotations


def safe_disconnect(signal, slot) -> None:
    try:
        signal.disconnect(slot)
    except (RuntimeError, TypeError):
        pass
