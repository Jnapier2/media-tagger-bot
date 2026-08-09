from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .utils import run_command, which


MP4_CONTAINER_NAMES = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}
ASF_CONTAINER_NAMES = {"asf"}
OGG_CONTAINER_NAMES = {"ogg", "oga", "ogv"}


def ffprobe_available() -> bool:
    return which("ffprobe") is not None


def ffprobe_media_info(path: Path, timeout_seconds: int = 45) -> dict[str, Any]:
    """Return a bounded, read-only container/stream probe.

    Extension-only writer selection caused real production failures when files were
    mislabeled or when MOV video was handled as a generic MP4.  ffprobe is used only
    for container families where the extension alone is not reliable.
    """
    result: dict[str, Any] = {
        "schema": "MediaTaggerBot.ffprobe_media_info.v1",
        "available": ffprobe_available(),
        "ok": False,
        "format_name": "",
        "format_names": [],
        "format_long_name": "",
        "duration_seconds": None,
        "stream_types": [],
        "stream_codecs": [],
        "error": "",
    }
    if not result["available"]:
        result["error"] = "ffprobe unavailable"
        return result
    args = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=format_name,format_long_name,duration:stream=codec_type,codec_name",
        "-of", "json",
        str(path),
    ]
    code, out, err = run_command(args, timeout=timeout_seconds)
    if code != 0 or not out.strip():
        result["error"] = (err or out or f"ffprobe exited with code {code}").strip()
        return result
    try:
        payload = json.loads(out)
        fmt = payload.get("format", {}) if isinstance(payload, dict) else {}
        streams = payload.get("streams", []) if isinstance(payload, dict) else []
        format_name = str(fmt.get("format_name") or "")
        result["format_name"] = format_name
        result["format_names"] = [item.strip().casefold() for item in format_name.split(",") if item.strip()]
        result["format_long_name"] = str(fmt.get("format_long_name") or "")
        duration = fmt.get("duration")
        result["duration_seconds"] = float(duration) if duration not in (None, "") else None
        if isinstance(streams, list):
            result["stream_types"] = sorted({str(item.get("codec_type") or "") for item in streams if isinstance(item, dict) and item.get("codec_type")})
            result["stream_codecs"] = sorted({str(item.get("codec_name") or "") for item in streams if isinstance(item, dict) and item.get("codec_name")})
        result["ok"] = bool(result["format_names"])
        if not result["ok"]:
            result["error"] = "ffprobe returned no container format"
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {exc}"
    return result


def classify_container(info: dict[str, Any]) -> str:
    names = {str(item).casefold() for item in (info.get("format_names") or [])}
    if names & MP4_CONTAINER_NAMES:
        return "mp4_family"
    if names & ASF_CONTAINER_NAMES:
        return "asf"
    if "mp3" in names:
        return "mp3"
    if "flac" in names:
        return "flac"
    if names & OGG_CONTAINER_NAMES:
        return "ogg_family"
    if "wav" in names:
        return "wav"
    if "aiff" in names:
        return "aiff"
    if names:
        return sorted(names)[0]
    return "unknown"


def ffprobe_duration(path: Path, timeout_seconds: int = 45) -> float | None:
    info = ffprobe_media_info(path, timeout_seconds=timeout_seconds)
    value = info.get("duration_seconds")
    return float(value) if value is not None else None
