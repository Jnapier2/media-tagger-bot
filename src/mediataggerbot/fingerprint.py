from __future__ import annotations

import hashlib
import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from .cache import JsonCache
from .ffprobe import ffprobe_duration
from .models import MediaFile
from .utils import run_command, which


@lru_cache(maxsize=1)
def ffmpeg_chromaprint_available() -> bool:
    """Return whether the installed FFmpeg exposes the Chromaprint muxer.

    Many Windows FFmpeg "full" builds already include ``--enable-chromaprint``.
    Probing once per process lets the bot use that existing binary when ``fpcalc``
    is absent, avoiding another mandatory tool installation.
    """
    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        return False
    code, out, err = run_command([ffmpeg, "-hide_banner", "-muxers"], timeout=15)
    if code != 0:
        return False
    text = f"{out}\n{err}".casefold()
    return any("chromaprint" in line and " e " in f" {line.casefold()} " for line in text.splitlines())


def fingerprint_backend_status() -> dict[str, Any]:
    fpcalc = which("fpcalc")
    ffmpeg = which("ffmpeg")
    ffmpeg_capable = ffmpeg_chromaprint_available() if ffmpeg else False
    selected = "fpcalc" if fpcalc else ("ffmpeg_chromaprint" if ffmpeg_capable else "none")
    return {
        "selected": selected,
        "available": selected != "none",
        "fpcalc": fpcalc,
        "ffmpeg": ffmpeg,
        "ffmpeg_chromaprint": ffmpeg_capable,
        "fingerprint_audio_window_seconds": 120,
    }


def fingerprint_backend_available() -> bool:
    return bool(fingerprint_backend_status()["available"])


def fingerprint_file(
    path: Path,
    timeout_seconds: int = 120,
    duration_hint_seconds: float | int | None = None,
) -> tuple[int | None, str | None, str | None]:
    """Return ``(whole_file_duration_seconds, fingerprint, error)``.

    ``fpcalc`` remains the preferred implementation.  When it is unavailable,
    FFmpeg's official Chromaprint muxer is used if the installed build exposes
    it.  The fallback fingerprints the same first 120 seconds used by modern
    ``fpcalc`` defaults and emits the base64-compressed fingerprint expected by
    the AcoustID lookup API.
    """
    backend = fingerprint_backend_status()["selected"]
    if backend == "none":
        return None, None, "No fingerprint backend available (fpcalc missing; FFmpeg Chromaprint muxer unavailable)"
    if backend == "ffmpeg_chromaprint":
        return _fingerprint_with_ffmpeg(path, timeout_seconds, duration_hint_seconds)

    args = ["fpcalc", "-json", str(path)]
    code, out, err = run_command(args, timeout=timeout_seconds)
    if code != 0:
        return None, None, err.strip() or f"fpcalc exit code {code}"
    try:
        payload = json.loads(out)
        duration = int(round(float(payload.get("duration")))) if payload.get("duration") is not None else None
        fingerprint = payload.get("fingerprint")
        if not fingerprint:
            return duration, None, "fpcalc returned no fingerprint"
        return duration, str(fingerprint), None
    except Exception as exc:
        return None, None, f"Unable to parse fpcalc JSON: {exc}"


def _fingerprint_with_ffmpeg(
    path: Path,
    timeout_seconds: int,
    duration_hint_seconds: float | int | None,
) -> tuple[int | None, str | None, str | None]:
    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        return None, None, "FFmpeg disappeared after the Chromaprint capability probe"
    args = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-vn",
        "-t",
        "120",
        "-ac",
        "2",
        "-ar",
        "11025",
        "-f",
        "chromaprint",
        "-algorithm",
        "1",
        "-fp_format",
        "base64",
        "-",
    ]
    code, out, err = run_command(args, timeout=timeout_seconds)
    if code != 0:
        return None, None, err.strip() or f"FFmpeg Chromaprint exit code {code}"
    fingerprint = out.strip()
    if not fingerprint:
        return None, None, "FFmpeg Chromaprint returned no fingerprint"

    duration = _optional_int(duration_hint_seconds)
    if duration is None:
        probed = ffprobe_duration(path, timeout_seconds=min(45, max(5, int(timeout_seconds))))
        duration = _optional_int(round(probed)) if probed is not None else None
    if duration is None:
        return None, None, "FFmpeg generated a fingerprint but whole-file duration could not be determined"
    return duration, fingerprint, None


def fingerprint_cache_key(media: MediaFile) -> str:
    """Return a fail-safe key that invalidates when full path, size, or mtime changes."""
    try:
        full_path = os.path.normcase(str(media.path.resolve()))
    except OSError:
        full_path = os.path.normcase(os.path.abspath(str(media.path)))
    signature = f"{full_path}\0{media.size_bytes}\0{media.modified_ns or 0}"
    return hashlib.sha256(signature.encode("utf-8", errors="surrogatepass")).hexdigest()


def fingerprint_media(
    media: MediaFile,
    timeout_seconds: int = 120,
    cache: JsonCache | None = None,
    use_cache: bool = True,
) -> MediaFile:
    """Fingerprint one file, reusing a successful prior result when its signature is unchanged."""
    cache_key = fingerprint_cache_key(media)
    if cache is not None and use_cache:
        cached = cache.get("fingerprint_v1", cache_key)
        if isinstance(cached, dict) and cached.get("fingerprint"):
            media.fingerprint_duration = _optional_int(cached.get("duration"))
            media.fingerprint = str(cached["fingerprint"])
            media.fingerprint_error = None
            media.fingerprint_cache_hit = True
            return media

    duration, fingerprint, error = fingerprint_file(
        media.path,
        timeout_seconds=timeout_seconds,
        duration_hint_seconds=media.duration_seconds,
    )
    media.fingerprint_duration = duration
    media.fingerprint = fingerprint
    media.fingerprint_error = error
    media.fingerprint_cache_hit = False
    # Cache only successful fingerprints. Timeouts, missing tools, and corrupt files are retried later.
    if cache is not None and use_cache and fingerprint:
        cache.set("fingerprint_v1", cache_key, {"duration": duration, "fingerprint": fingerprint})
    return media


def _optional_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
