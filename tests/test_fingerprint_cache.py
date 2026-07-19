from pathlib import Path

from mediataggerbot.cache import JsonCache
from mediataggerbot.models import MediaFile
import mediataggerbot.fingerprint as fingerprint_module


def _media(path: Path, modified_ns: int) -> MediaFile:
    return MediaFile(
        path=path,
        rel_path="nested/song.mp3",
        extension=".mp3",
        size_bytes=12345,
        media_kind="audio",
        modified_ns=modified_ns,
        relative_depth=1,
    )


def test_successful_fingerprint_is_cached_and_signature_invalidates(tmp_path, monkeypatch):
    path = tmp_path / "song.mp3"
    path.write_bytes(b"not-real-audio")
    calls = {"count": 0}

    def fake_fingerprint_file(_path, timeout_seconds=120, duration_hint_seconds=None):
        calls["count"] += 1
        return 201, "cached-fingerprint", None

    monkeypatch.setattr(fingerprint_module, "fingerprint_file", fake_fingerprint_file)
    with JsonCache(tmp_path / "cache.sqlite3", ttl_days=365) as cache:
        first = fingerprint_module.fingerprint_media(_media(path, 100), cache=cache)
        second = fingerprint_module.fingerprint_media(_media(path, 100), cache=cache)
        changed = fingerprint_module.fingerprint_media(_media(path, 101), cache=cache)

    assert first.fingerprint_cache_hit is False
    assert second.fingerprint_cache_hit is True
    assert second.fingerprint == "cached-fingerprint"
    assert changed.fingerprint_cache_hit is False
    assert calls["count"] == 2


def test_ffmpeg_chromaprint_is_used_when_fpcalc_is_missing(tmp_path, monkeypatch):
    path = tmp_path / "song.wav"
    path.write_bytes(b"placeholder")
    commands: list[list[str]] = []

    def fake_which(name: str):
        if name == "fpcalc":
            return None
        if name == "ffmpeg":
            return "ffmpeg"
        return None

    def fake_run_command(args, timeout, cwd=None):
        commands.append(list(args))
        if "-muxers" in args:
            return 0, "  E  chromaprint     Chromaprint\n", ""
        return 0, "AQAA-test-fingerprint\n", ""

    monkeypatch.setattr(fingerprint_module, "which", fake_which)
    monkeypatch.setattr(fingerprint_module, "run_command", fake_run_command)
    fingerprint_module.ffmpeg_chromaprint_available.cache_clear()

    duration, fingerprint, error = fingerprint_module.fingerprint_file(
        path,
        timeout_seconds=30,
        duration_hint_seconds=203.2,
    )

    assert error is None
    assert duration == 203
    assert fingerprint == "AQAA-test-fingerprint"
    command = commands[-1]
    assert command[0] == "ffmpeg"
    assert command[command.index("-t") + 1] == "120"
    assert command[command.index("-algorithm") + 1] == "1"
    assert command[command.index("-fp_format") + 1] == "base64"
    fingerprint_module.ffmpeg_chromaprint_available.cache_clear()
