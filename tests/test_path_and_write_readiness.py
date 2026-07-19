from __future__ import annotations

from pathlib import Path

import pytest

from mediataggerbot.apply_readiness import probe_apply_readiness, readiness_blocks_apply
from mediataggerbot.cache import JsonCache
from mediataggerbot.config import load_config
from mediataggerbot.models import GenreResult, MatchResult
from mediataggerbot.rename import build_target_path

ROOT = Path(__file__).resolve().parents[1]


def _config():
    return load_config(project_root=ROOT, config_path=ROOT / "config" / "config.toml")


def _match() -> MatchResult:
    return MatchResult(
        matched=True,
        confidence=99.0,
        source="musicbrainz_recording_id_tag",
        artist="Very Long Artist Name " * 8,
        title="Very Long Recording Title " * 8,
    )


def _genre() -> GenreResult:
    return GenreResult(
        main_genre="Electronic Dance Music (EDM)",
        filename_main_genre="Electronic Dance Music (EDM)",
        subgenre="Progressive House",
        raw_terms=["progressive house"],
        source="database_terms",
        confidence=99.0,
    )


def test_target_path_respects_complete_path_budget(tmp_path: Path):
    cfg = _config()
    nested = tmp_path / ("deep-folder-" * 4)
    nested.mkdir()
    source = nested / "source.mp3"
    source.write_bytes(b"")
    budget = len(str(nested)) + 1 + len(source.suffix) + 48
    cfg.data["naming"]["max_full_path_length"] = budget

    target = build_target_path(source, _match(), _genre(), cfg)

    assert len(str(target)) <= budget
    assert target.suffix == ".mp3"
    assert target.parent == source.parent


def test_collision_suffix_still_respects_complete_path_budget(tmp_path: Path):
    cfg = _config()
    cfg.data["naming"]["max_full_path_length"] = 150
    source = tmp_path / "source.mp3"
    source.write_bytes(b"")
    first = build_target_path(source, _match(), _genre(), cfg)
    first.write_bytes(b"occupied")

    second = build_target_path(source, _match(), _genre(), cfg)

    assert second.name.endswith(" (2).mp3")
    assert len(str(second)) <= 150


def test_parent_that_consumes_budget_fails_with_clear_error(tmp_path: Path):
    cfg = _config()
    cfg.data["naming"]["max_full_path_length"] = 120
    parent = tmp_path / ("x" * 100)
    parent.mkdir()
    source = parent / "source.mp3"
    source.write_bytes(b"")

    with pytest.raises(RuntimeError, match="full-path budget"):
        build_target_path(source, _match(), _genre(), cfg)


def test_apply_readiness_reports_ready_for_normal_file(tmp_path: Path):
    cfg = _config()
    source = tmp_path / "source.mp3"
    source.write_bytes(b"abc")
    result = probe_apply_readiness(source, tmp_path / "target.mp3", cfg)

    assert result["status"] in {"ready", "warning_parent_write_not_confirmed"}
    assert readiness_blocks_apply(result) is False
    assert result["file_open_rw"] is True


def test_apply_readiness_blocks_permission_failure(tmp_path: Path, monkeypatch):
    cfg = _config()
    source = tmp_path / "source.mp3"
    source.write_bytes(b"abc")
    original_open = Path.open

    def blocked_open(self, *args, **kwargs):
        if self == source and args and args[0] == "r+b":
            raise PermissionError("locked")
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", blocked_open)
    result = probe_apply_readiness(source, tmp_path / "target.mp3", cfg)

    assert result["status"] == "blocked_permission_or_lock"
    assert readiness_blocks_apply(result) is True


def test_json_cache_reports_optimize_telemetry(tmp_path: Path):
    with JsonCache(tmp_path / "cache.sqlite3") as cache:
        cache.set("ns", "key", {"ok": True})
        snapshot = cache.snapshot()
    assert snapshot["optimize_open_errors"] == 0
    assert "optimize_close_errors" in cache.stats
