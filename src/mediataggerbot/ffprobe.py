from __future__ import annotations

import json
from pathlib import Path

from .utils import run_command, which


def ffprobe_available() -> bool:
    return which("ffprobe") is not None


def ffprobe_duration(path: Path, timeout_seconds: int = 45) -> float | None:
    if not ffprobe_available():
        return None
    args = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json",
        str(path),
    ]
    code, out, _err = run_command(args, timeout=timeout_seconds)
    if code != 0 or not out.strip():
        return None
    try:
        payload = json.loads(out)
        value = payload.get("format", {}).get("duration")
        return float(value) if value is not None else None
    except Exception:
        return None
