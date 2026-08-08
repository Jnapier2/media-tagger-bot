from __future__ import annotations

import csv
import json
import sqlite3
import zipfile
from pathlib import Path

from _media_fixtures import write_minimal_asf_signature

from mediataggerbot.cache import JsonCache
from mediataggerbot.config import load_config
from mediataggerbot.databases import MusicBrainzClient
from mediataggerbot.diagnostics import write_diagnostics_export
from mediataggerbot.metadata import metadata_writer_plan
from mediataggerbot.models import GenreResult, MatchResult, MediaFile, PlanResult
from mediataggerbot.operation_journal import write_failed_operations_report
from mediataggerbot.project_repair import archive_stale_release_artifacts
from mediataggerbot.rename import TargetCollisionError, build_target_path
from mediataggerbot.reporting import build_summary, write_reports
from mediataggerbot.utils import write_json_atomic

ROOT = Path(__file__).resolve().parents[1]


def cfg(tmp_path: Path | None = None):
    value = load_config(project_root=ROOT, config_path=ROOT / "config" / "config.toml")
    if tmp_path is not None:
        value.data["paths"]["media_root"] = str(tmp_path)
        value.data["paths"]["logs_dir"] = str(tmp_path / "logs")
        value.data["paths"]["exports_dir"] = str(tmp_path / "exports")
        value.data["paths"]["state_dir"] = str(tmp_path / "state")
        value.data["paths"]["diagnostics_dir"] = str(tmp_path / "diagnostics")
        value.data["paths"]["temp_dir"] = str(tmp_path / "temp")
    return value


def match() -> MatchResult:
    return MatchResult(
        matched=True,
        confidence=99.0,
        source="musicbrainz_recording_id_tag",
        artist="Artist",
        title="Song",
        identity_tier="stable_identifier",
        musicbrainz_recording_id="recording-id",
    )


def genre() -> GenreResult:
    return GenreResult("Rock", "Rock", "Alternative Rock", ["alternative rock"], "database_terms", 95.0)


def media(path: Path) -> MediaFile:
    return MediaFile(
        path=path,
        rel_path=path.name,
        extension=path.suffix,
        size_bytes=path.stat().st_size,
        modified_ns=path.stat().st_mtime_ns,
        media_kind="audio",
        duration_seconds=180.0,
    )


def test_isrc_lookup_is_lean_then_enriched(tmp_path: Path):
    with JsonCache(tmp_path / "cache.sqlite3") as cache:
        client = MusicBrainzClient(cache=cache, namespace="mb", user_agent="test", timeout_seconds=1, min_interval_seconds=0)
        calls: list[tuple[str, dict]] = []

        def fake(_method, url, *, params=None, **_kwargs):
            calls.append((url, dict(params or {})))
            if "/isrc/" in url:
                return {"recordings": [{"id": "recording-id", "title": "Basic"}]}
            return {"id": "recording-id", "title": "Detailed", "artist-credit": []}

        client.request_json = fake  # type: ignore[method-assign]
        result = client.lookup_isrc("GBKQU1637552")
    assert result[0]["title"] == "Detailed"
    assert calls[0][1] == {"fmt": "json"}
    assert calls[1][0].endswith("/recording/recording-id")
    assert "artist-credits" in calls[1][1]["inc"]


def test_mislabeled_mp4_fails_closed(tmp_path: Path, monkeypatch):
    path = tmp_path / "not_really_mp4.mp4"
    path.write_bytes(b"not mp4")
    monkeypatch.setattr("mediataggerbot.metadata.ffprobe_media_info", lambda *_a, **_k: {"ok": True, "format_names": ["mp3"]})
    monkeypatch.setattr("mediataggerbot.metadata.classify_container", lambda _info: "mp3")
    plan = metadata_writer_plan(path, cfg(tmp_path))
    assert plan["supported"] is False
    assert plan["reason"] == "extension_container_mismatch:mp3"


def test_mov_requires_exiftool(tmp_path: Path, monkeypatch):
    path = tmp_path / "clip.mov"
    path.write_bytes(b"mov")
    monkeypatch.setattr("mediataggerbot.metadata.ffprobe_media_info", lambda *_a, **_k: {"ok": True, "format_names": ["mov,mp4"]})
    monkeypatch.setattr("mediataggerbot.metadata.classify_container", lambda _info: "mp4_family")
    monkeypatch.setattr("mediataggerbot.metadata.exiftool_available", lambda: False)
    plan = metadata_writer_plan(path, cfg(tmp_path))
    assert plan["supported"] is False
    assert plan["reason"] == "mov_requires_exiftool_for_verified_apply"


def test_asf_writer_selected_for_wma(tmp_path: Path):
    path = tmp_path / "track.wma"
    write_minimal_asf_signature(path)
    plan = metadata_writer_plan(path, cfg(tmp_path))
    assert plan["supported"] is True
    assert plan["writer"] == "asf"


def test_apply_safe_collision_blocks_suffix_but_apply_all_can_suffix(tmp_path: Path):
    source = tmp_path / "old.mp3"
    source.write_bytes(b"source")
    target = tmp_path / "Artist - Song - Rock - Alternative Rock.mp3"
    target.write_bytes(b"target")
    config = cfg(tmp_path)
    try:
        build_target_path(source, match(), genre(), config, allow_collision_suffix=False)
    except TargetCollisionError as exc:
        assert exc.target == target
    else:
        raise AssertionError("Apply-safe collision must fail closed")
    suffixed = build_target_path(source, match(), genre(), config, allow_collision_suffix=True)
    assert suffixed.name.endswith("(2).mp3")


def test_summary_reports_total_included_and_omitted_errors(tmp_path: Path):
    plans = []
    for index in range(105):
        path = tmp_path / f"{index}.mp3"
        path.write_bytes(b"")
        plan = PlanResult(
            media=media(path), match=match(), genre=genre(), proposed_path=None, proposed_filename=None,
            action="apply_safe", should_apply=True, status="embedded_metadata_write_failed", error="denied"
        )
        plans.append(plan)
    summary = build_summary(plans, "run", "apply-safe")
    assert summary["errors_total"] == 105
    assert summary["errors_included"] == 100
    assert summary["errors_omitted"] == 5
    assert summary["error_status_counts"] == {"embedded_metadata_write_failed": 105}


def test_failed_operation_report_is_read_only_and_actionable(tmp_path: Path):
    db = tmp_path / "journal.sqlite3"
    source = tmp_path / "source.mp3"
    source.write_bytes(b"x")
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE operations (operation_id TEXT, run_id TEXT, source_path TEXT, target_path TEXT, stage TEXT, status TEXT, created_utc TEXT, updated_utc TEXT, details_json TEXT)")
        conn.execute("INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,?)", ("op", "old", str(source), str(tmp_path / "target.mp3"), "metadata_write_failed", "failed", "a", "b", json.dumps({"error": "denied"})))
    output = write_failed_operations_report(db, tmp_path / "out", "new")
    assert output is not None
    rows = list(csv.DictReader(output.open(encoding="utf-8-sig")))
    assert rows[0]["retry_class"] == "source_present_revalidate_before_retry"
    assert rows[0]["error"] == "denied"


def test_diagnostics_prioritizes_new_safety_reports_and_labels_capture_phase(tmp_path: Path):
    config = cfg(tmp_path)
    for directory in [config.logs_dir, config.exports_dir, config.state_dir, config.diagnostics_dir, config.temp_dir]:
        directory.mkdir(parents=True, exist_ok=True)
    report_dir = config.exports_dir / "run"
    report_dir.mkdir(parents=True)
    summary = report_dir / "summary.json"
    failures = report_dir / "failures.csv"
    collisions = report_dir / "collisions.csv"
    failed_ops = report_dir / "failed_ops.csv"
    write_json_atomic(summary, {"errors_total": 107, "errors_included": 100, "errors_omitted": 7})
    for path in [failures, collisions, failed_ops]:
        path.write_text("a,b\n1,2\n", encoding="utf-8")
    zip_path = write_diagnostics_export(config, "run", "apply-safe", report_paths={
        "summary_json": summary,
        "run_failures_csv": failures,
        "target_collisions_csv": collisions,
        "failed_operations_csv": failed_ops,
        "run_exit_report": summary,
    })
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
        assert any("run_failures_csv" in name for name in names)
        assert any("target_collisions_csv" in name for name in names)
        assert any("failed_operations_csv" in name for name in names)
        diag = json.loads(archive.read("diagnostic_summary.json"))
        assert diag["run_error_counts"]["errors_total"] == 107
        assert diag["capture_phase"] in {"finalization_before_unlock", "post_run_or_idle_snapshot"}


def test_repair_archives_only_stale_root_release_files_and_marker(tmp_path: Path):
    stale = tmp_path / "FULL_BATCH_OUTPUT_v0.5.5.txt"
    current = tmp_path / "FULL_BATCH_OUTPUT_v0.5.6.txt"
    stale.write_text("old", encoding="utf-8")
    current.write_text("new", encoding="utf-8")
    marker = tmp_path / ".venv" / ".deps_checked_v0.5.5"
    marker.parent.mkdir()
    marker.write_text("ok", encoding="utf-8")
    result = archive_stale_release_artifacts(tmp_path, "0.5.6")
    assert result["status"] == "completed"
    assert not stale.exists()
    assert not marker.exists()
    assert current.exists()
    assert Path(result["manifest"]).exists()
