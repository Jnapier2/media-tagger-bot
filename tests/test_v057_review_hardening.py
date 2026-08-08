from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

from _media_fixtures import write_minimal_mp3
from mediataggerbot.config import load_config, validate_sidecar_extension
from mediataggerbot.diagnostics import sanitize_diagnostic_value
from mediataggerbot.main import run_preflight
from mediataggerbot.matcher import Matcher, deduplicate_identity_candidates
from mediataggerbot.metadata import metadata_writer_plan, verify_metadata_write, write_metadata
from mediataggerbot.models import GenreResult, MatchResult, MediaFile
from mediataggerbot.operation_journal import OperationJournal
from mediataggerbot.rename import build_sidecar_path, windows_path_identity_key
from mediataggerbot.run_control import request_graceful_stop
from mediataggerbot.scanner import _first_flat
from mediataggerbot.single_instance import read_lock_status
from mediataggerbot.timeutil import new_run_id

ROOT = Path(__file__).resolve().parents[1]


def cfg(tmp_path: Path | None = None):
    value = load_config(project_root=ROOT, config_path=ROOT / "config" / "config.toml")
    if tmp_path is not None:
        value.data["paths"].update(
            media_root=str(tmp_path / "media"),
            logs_dir=str(tmp_path / "logs"),
            exports_dir=str(tmp_path / "exports"),
            state_dir=str(tmp_path / "state"),
            diagnostics_dir=str(tmp_path / "diagnostics"),
            temp_dir=str(tmp_path / "temp"),
        )
        for key in ("media", "logs", "exports", "state", "diagnostics", "temp"):
            (tmp_path / key).mkdir(parents=True, exist_ok=True)
    return value


def match(**updates) -> MatchResult:
    values = dict(
        matched=True,
        confidence=99.0,
        source="musicbrainz_recording_id_tag",
        artist="Artist",
        title="Song",
        musicbrainz_recording_id="11111111-1111-1111-1111-111111111111",
        identity_tier="stable_identifier",
        ambiguity_status="exact_stable_identifier",
    )
    values.update(updates)
    return MatchResult(**values)


def genre() -> GenreResult:
    return GenreResult("Rock", "Rock", "Alternative Rock", ["alternative rock"], "database_terms", 95.0)


def media(path: Path, **updates) -> MediaFile:
    values = dict(
        path=path,
        rel_path=path.name,
        extension=path.suffix,
        size_bytes=path.stat().st_size,
        modified_ns=path.stat().st_mtime_ns,
        media_kind="audio",
        duration_seconds=180.0,
    )
    values.update(updates)
    return MediaFile(**values)


@pytest.mark.parametrize(
    "suffix",
    [".", "..", ".metadata.json.", ".metadata.json ", ".meta:data.json", ".meta/data.json", ".meta\\data.json", ".meta\x00.json"],
)
def test_sidecar_suffix_rejects_windows_aliases_and_invalid_characters(suffix: str):
    assert validate_sidecar_extension(suffix)


def test_sidecar_path_cannot_alias_media_under_windows_semantics(tmp_path: Path):
    config = cfg(tmp_path)
    config.data["metadata"]["sidecar_extension"] = "."
    with pytest.raises(RuntimeError, match="Unsafe metadata.sidecar_extension"):
        build_sidecar_path(tmp_path / "song.mp3", config)
    assert windows_path_identity_key(tmp_path / "song.mp3.") == windows_path_identity_key(tmp_path / "song.mp3")


def test_existing_unowned_sidecar_is_never_replaced(tmp_path: Path):
    config = cfg(tmp_path)
    source = tmp_path / "song.wav"
    source.write_bytes(b"not a supported embedded container")
    sidecar = Path(str(source) + ".metadata.json")
    sidecar.write_text('{"owner":"someone-else"}\n', encoding="utf-8")
    before = sidecar.read_bytes()
    with pytest.raises(RuntimeError, match="not recognized as MediaTaggerBot-owned"):
        write_metadata(source, match(), genre(), config, sidecar_path=sidecar)
    assert sidecar.read_bytes() == before
    assert source.read_bytes() == b"not a supported embedded container"


def test_bot_owned_sidecar_is_backed_up_before_update(tmp_path: Path):
    config = cfg(tmp_path)
    source = tmp_path / "song.wav"
    source.write_bytes(b"unsupported")
    sidecar = Path(str(source) + ".metadata.json")
    sidecar.write_text('{"schema":"MediaTaggerBot.sidecar.v5","app_version":"0.5.6"}\n', encoding="utf-8")
    wrote, _error, written = write_metadata(source, match(), genre(), config, sidecar_path=sidecar)
    assert wrote is False and written == sidecar
    backups = list(tmp_path.glob("song.wav.metadata.json.bak_*"))
    assert len(backups) == 1
    assert "MediaTaggerBot.sidecar.v5" in backups[0].read_text(encoding="utf-8")


def test_acoustid_duplicate_rows_do_not_hide_competing_identity(tmp_path: Path):
    class FakeAcoustID:
        enabled = True

        def lookup_fingerprint(self, _duration, _fingerprint):
            a = {"id": "A", "title": "Song", "artists": [{"name": "Artist"}], "duration": 180}
            b = {"id": "B", "title": "Song", "artists": [{"name": "Artist"}], "duration": 180}
            return {
                "status": "ok",
                "results": [
                    {"id": "ac-a1", "score": 0.95, "recordings": [a]},
                    {"id": "ac-a2", "score": 0.95, "recordings": [a]},
                    {"id": "ac-a3", "score": 0.95, "recordings": [a]},
                    {"id": "ac-b", "score": 0.95, "recordings": [b]},
                ],
            }

    path = tmp_path / "Artist - Song.mp3"
    write_minimal_mp3(path)
    item = media(path, fingerprint="fingerprint", fingerprint_duration=180, existing_artist="Artist", existing_title="Song")
    matcher = Matcher(cfg(tmp_path), FakeAcoustID(), None, None, None, None)  # type: ignore[arg-type]
    result = matcher._match_acoustid(item)
    assert result is not None
    assert result.evidence["acoustid_raw_candidate_count"] == 4
    assert result.evidence["acoustid_unique_candidate_count"] == 2
    assert result.ambiguity_status == "ambiguous_fingerprint_candidates"
    assert "ambiguous_fingerprint_candidates" in result.apply_blockers


def test_candidate_dedup_retains_best_score_per_identity():
    unique, counts = deduplicate_identity_candidates([
        match(musicbrainz_recording_id="A", confidence=91.0),
        match(musicbrainz_recording_id="A", confidence=95.0),
        match(musicbrainz_recording_id="B", confidence=94.0),
    ])
    by_id = {item.musicbrainz_recording_id: item.confidence for item in unique}
    assert by_id == {"A": 95.0, "B": 94.0}
    assert counts["mbid:a"] == 2


def test_missing_expected_mbid_fails_native_readback(monkeypatch, tmp_path: Path):
    path = tmp_path / "song.mp3"
    write_minimal_mp3(path)
    monkeypatch.setattr(
        "mediataggerbot.metadata.read_existing_tags",
        lambda _path: {"artist": "Artist", "title": "Song", "genre": "Rock"},
    )
    verified, details = verify_metadata_write(path, match(), genre(), embedded_written=True)
    assert verified is False
    assert "musicbrainz_recording_id_missing" in details["mismatches"]


def test_tag_lookup_does_not_map_album_or_custom_suffixes_to_track_identity():
    flat = {
        "albumartist": "Wrong Artist",
        "albumtitle": "Wrong Title",
        "mediataggerbotsubgenre": "Wrong Genre",
    }
    assert _first_flat(flat, "artist") is None
    assert _first_flat(flat, "title") is None
    assert _first_flat(flat, "genre") is None
    namespaced = {"comappleitunesmusicbrainzrecordingid": "recording-id"}
    assert _first_flat(namespaced, "musicbrainzrecordingid") == "recording-id"


def test_tag_derived_identity_is_not_cross_seeded_under_fingerprint(tmp_path: Path):
    from mediataggerbot.cache import JsonCache

    path = tmp_path / "song.mp3"
    write_minimal_mp3(path)
    item = media(path, fingerprint="abc", fingerprint_duration=180)
    with JsonCache(tmp_path / "cache.sqlite3") as cache:
        matcher = Matcher(cfg(tmp_path), None, None, None, None, cache)
        result = match(source="musicbrainz_recording_id_tag", identity_tier="stable_identifier")
        matcher._store_identity_memory(result, item)
        keys = matcher._identity_memory_keys(item, result, for_store=True)
        assert all(not key.startswith("fingerprint:") for key in keys)
        fingerprint_key = matcher._identity_memory_keys(item)[0]
        if fingerprint_key.startswith("fingerprint:"):
            assert cache.get("identity_memory_v3", fingerprint_key) is None


def test_mislabeled_mp3_is_not_selected_for_id3_writer(tmp_path: Path):
    path = tmp_path / "fake.mp3"
    path.write_bytes(b"plain text, not MPEG audio")
    plan = metadata_writer_plan(path, cfg(tmp_path))
    assert plan["supported"] is False
    assert plan["writer"] == "none"


def test_journal_target_presence_without_durable_identity_is_conflict(tmp_path: Path):
    journal_path = tmp_path / "journal.sqlite3"
    source = tmp_path / "source.mp3"
    target = tmp_path / "target.mp3"
    with OperationJournal(journal_path, "old") as old:
        operation = old.start(source, target)
        target.write_bytes(b"unrelated")
        old.update(operation, "renamed")
    with OperationJournal(journal_path, "new") as current:
        result = current.reconcile_prior_incomplete()
    assert result["completed_after_crash"] == 0
    assert result["conflict"] == 1


def test_journal_reconciliation_reports_truncation(tmp_path: Path):
    journal_path = tmp_path / "journal.sqlite3"
    with OperationJournal(journal_path, "old") as old:
        for index in range(3):
            source = tmp_path / f"source-{index}.mp3"
            source.write_bytes(b"x")
            old.start(source, tmp_path / f"target-{index}.mp3")
    with OperationJournal(journal_path, "new") as current:
        result = current.reconcile_prior_incomplete(limit=2)
    assert result["eligible_total"] == 3
    assert result["checked"] == 2
    assert result["truncated"] is True


def test_diagnostic_redaction_is_key_aware_for_misspelled_secret_key(tmp_path: Path):
    config = cfg(tmp_path)
    payload = {
        "acoustid_cllent_key": "SECRET-VALUE",
        "Authorization": "Bearer abc",
        "nested": {"password": "hunter2"},
        "normal": "safe",
    }
    cleaned = sanitize_diagnostic_value(payload, config)
    assert cleaned["acoustid_cllent_key"] == "<redacted>"
    assert cleaned["Authorization"] == "<redacted>"
    assert cleaned["nested"]["password"] == "<redacted>"
    assert cleaned["normal"] == "safe"


def test_run_ids_are_collision_resistant():
    values = {new_run_id("dry-run") for _ in range(100)}
    assert len(values) == 100
    assert all("dry_run" in value for value in values)


def test_foreign_host_graceful_stop_is_rejected(tmp_path: Path):
    lock = tmp_path / "foreign.lock.json"
    lock.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "hostname": "foreign-computer",
                "owner_token": "owner",
                "run_id": "run",
                "mode": "apply-safe",
                "heartbeat_epoch": time.time(),
            }
        ),
        encoding="utf-8",
    )
    result = request_graceful_stop(tmp_path, lock, 86400, request_path=tmp_path / "stop.json")
    assert result["request_written"] is False
    assert result["status"] == "no_active_run"
    assert not (tmp_path / "stop.json").exists()


def test_malformed_lock_has_short_grace_then_becomes_stale(tmp_path: Path):
    lock = tmp_path / "lock.json"
    lock.write_text("", encoding="utf-8")
    recent = read_lock_status(lock, stale_after_seconds=86400)
    assert recent["active"] is True
    old = time.time() - 20
    os.utime(lock, (old, old))
    stale = read_lock_status(lock, stale_after_seconds=86400)
    assert stale["active"] is False
    assert stale["stale"] is True
    assert stale["reason"] == "malformed_lock_grace_expired"


def test_preflight_fails_when_media_root_is_unset(tmp_path: Path, monkeypatch):
    config = cfg(tmp_path)
    config.data["paths"]["media_root"] = ""
    monkeypatch.setattr("mediataggerbot.main.write_diagnostics_export", lambda *_a, **_k: tmp_path / "diag.zip")
    monkeypatch.setattr(
        "mediataggerbot.main.build_environment_summary",
        lambda _c, _r, _m: {
            "computer_context": {},
            "launcher_status": {"legacy_powershell_launcher_present": False, "attestation": {"safe_to_process": True, "status": "direct_python", "confirmed": True}},
            "scan_policy": {"recursive": True, "require_recursive_scan": True, "follow_directory_symlinks": False, "exclude_dir_names": [], "max_files_per_run": 0, "complete_signal": "all_reachable_subfolders_checked=true", "inventory_cache_enabled": True, "inventory_cache_ttl_days": 3650},
            "tools": {"fpcalc": None, "ffprobe": None, "exiftool": None, "fingerprint_backend": {}},
        },
    )
    assert run_preflight(config, "run", tmp_path / "run.log") == 2


def test_bat_prerequisite_failure_captures_error_outside_parenthesized_block():
    text = (ROOT / "Start_MediaTaggerBot.bat").read_text(encoding="utf-8")
    assert "call :ensure_runtime\nif errorlevel 1 (" not in text
    assert "call :find_control_python\nif errorlevel 1 (" not in text
    assert ":execute_mode_prerequisite_failed" in text
    assert ":execute_control_prerequisite_failed" in text
    assert ":execute_stop_prerequisite_failed" in text


def test_csv_formula_cells_are_neutralized_without_changing_non_strings(tmp_path: Path):
    from mediataggerbot.utils import csv_safe_cell, csv_safe_mapping

    assert csv_safe_cell("=HYPERLINK('x')") == "'=HYPERLINK('x')"
    assert csv_safe_cell("  @cmd") == "'  @cmd"
    assert csv_safe_cell("normal") == "normal"
    assert csv_safe_cell(-5) == -5
    assert csv_safe_mapping({"artist": "+SUM(A1:A2)", "count": 2}) == {"artist": "'+SUM(A1:A2)", "count": 2}


def test_windows_path_budget_counts_utf16_code_units(tmp_path: Path):
    from mediataggerbot.rename import fit_stem_to_full_path_budget
    from mediataggerbot.utils import windows_utf16_units

    # Emoji is one Python character but two UTF-16 code units on Windows.
    stem = "Artist " + ("🎵" * 40)
    budget = windows_utf16_units(str(tmp_path)) + 1 + windows_utf16_units(".mp3") + 50
    fitted = fit_stem_to_full_path_budget(tmp_path, stem, ".mp3", budget)
    full = str(tmp_path / f"{fitted}.mp3")
    assert windows_utf16_units(full) <= budget
    assert len(fitted) < len(stem)


def test_cache_prunes_expired_rows_and_checkpoints(tmp_path: Path):
    from mediataggerbot.cache import JsonCache

    path = tmp_path / "cache.sqlite3"
    cache = JsonCache(path, ttl_days=1)
    cache.set("test", "old", {"value": 1})
    assert cache.conn is not None
    cache.conn.execute("UPDATE cache SET created=? WHERE namespace='test' AND cache_key='old'", (time.time() - 172800,))
    cache.conn.commit()
    assert cache.get("test", "old") is None
    assert cache.stats["pruned_rows"] >= 1
    cache.close()


def test_file_identity_fields_invalidate_fingerprint_cache(tmp_path: Path):
    from mediataggerbot.cache import JsonCache
    import mediataggerbot.fingerprint as fingerprint_module

    path = tmp_path / "song.mp3"
    write_minimal_mp3(path)
    calls = {"count": 0}

    def fake(_path, timeout_seconds=120, duration_hint_seconds=None):
        calls["count"] += 1
        return 180, "fingerprint", None

    original = path.stat()
    first = media(path, changed_ns=original.st_ctime_ns, file_id=getattr(original, "st_ino", None))
    second = media(path, changed_ns=original.st_ctime_ns, file_id=getattr(original, "st_ino", None))
    replacement_identity = media(path, changed_ns=original.st_ctime_ns + 1, file_id=getattr(original, "st_ino", 0) + 1)
    with JsonCache(tmp_path / "fp.sqlite3") as cache:
        from pytest import MonkeyPatch
        patch = MonkeyPatch()
        patch.setattr(fingerprint_module, "fingerprint_file", fake)
        try:
            fingerprint_module.fingerprint_media(first, cache=cache)
            fingerprint_module.fingerprint_media(second, cache=cache)
            fingerprint_module.fingerprint_media(replacement_identity, cache=cache)
        finally:
            patch.undo()
    assert second.fingerprint_cache_hit is True
    assert replacement_identity.fingerprint_cache_hit is False
    assert calls["count"] == 2


def test_api_wait_is_interrupted_by_graceful_stop(tmp_path: Path):
    from mediataggerbot.cache import JsonCache
    from mediataggerbot.databases import ApiClientBase

    with JsonCache(tmp_path / "api.sqlite3") as cache:
        client = ApiClientBase(
            cache=cache,
            namespace="test",
            user_agent="test",
            timeout_seconds=5,
            min_interval_seconds=0,
            max_retries=0,
            stop_check=lambda: True,
        )
        class NeverCallSession:
            def request(self, **_kwargs):
                raise AssertionError("network request should not start after graceful stop")
        client.session = NeverCallSession()  # type: ignore[assignment]
        assert client.request_json("GET", "https://example.invalid") is None
        assert client.metrics["graceful_stop_interrupts"] == 1
        assert client.metrics["requests_sent"] == 0


def test_repair_manifest_is_written_before_first_move(tmp_path: Path, monkeypatch):
    from mediataggerbot.project_repair import archive_stale_release_artifacts

    stale = tmp_path / "FULL_BATCH_OUTPUT_v0.5.6.txt"
    stale.write_text("old", encoding="utf-8")
    observed: dict[str, object] = {}
    original_replace = Path.replace

    def checked_replace(self: Path, target: Path):
        manifest = target.parent / "archive_manifest.json"
        observed["manifest_exists_before_move"] = manifest.exists()
        if manifest.exists():
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            observed["status_before_move"] = payload.get("status")
            observed["planned_count_before_move"] = len(payload.get("planned", []))
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", checked_replace)
    result = archive_stale_release_artifacts(tmp_path, "0.5.7")
    assert result["status"] == "completed"
    assert observed == {
        "manifest_exists_before_move": True,
        "status_before_move": "planned",
        "planned_count_before_move": 1,
    }


def test_repair_detects_and_archives_stale_dependency_attestation(tmp_path: Path):
    from mediataggerbot.project_repair import archive_stale_release_artifacts, build_project_drift_status

    venv = tmp_path / ".venv"
    venv.mkdir()
    stale = venv / ".deps_attestation_v0.5.6.json"
    current = venv / ".deps_attestation_v0.5.7.json"
    stale.write_text("{}", encoding="utf-8")
    current.write_text("{}", encoding="utf-8")
    drift = build_project_drift_status(tmp_path, "0.5.7")
    assert str(stale) in drift["stale_dependency_markers"]
    assert str(current) not in drift["stale_dependency_markers"]
    result = archive_stale_release_artifacts(tmp_path, "0.5.7")
    assert result["status"] == "completed"
    assert not stale.exists()
    assert current.exists()


def test_bat_dependency_attestation_failure_is_not_silently_successful():
    text = (ROOT / "Start_MediaTaggerBot.bat").read_text(encoding="utf-8")
    assert "scripts\\verify_runtime_environment.py" in text
    assert "file-hash attestation; rebuilding from the hash-locked public-source contract" in text
    assert "Dependency environment verified earlier but its attestation marker could not be finalized" in text
    assert text.count("exit /b 16") >= 2


def test_unsupported_container_sidecar_fallback_is_reachable_without_media_write(tmp_path: Path):
    from mediataggerbot.apply_readiness import probe_apply_readiness

    config = cfg(tmp_path)
    source = tmp_path / "song.wav"
    source.write_bytes(b"unsupported container bytes")
    proposed = tmp_path / "Artist - Song - Rock.wav"
    result = probe_apply_readiness(source, proposed, config)
    assert result["metadata_sidecar_only"] is True
    assert result["status"] == "ready"
    assert result["file_open_read"] is True
    assert result["file_open_rw"] is False
    assert not Path(str(source) + ".metadata.json").exists()


def test_unowned_final_sidecar_blocks_before_metadata_or_rename(tmp_path: Path):
    from mediataggerbot.apply_readiness import probe_apply_readiness

    config = cfg(tmp_path)
    source = tmp_path / "song.wav"
    source.write_bytes(b"unsupported container bytes")
    proposed = tmp_path / "Artist - Song - Rock.wav"
    final_sidecar = Path(str(proposed) + ".metadata.json")
    final_sidecar.write_text('{"owner":"other"}\n', encoding="utf-8")
    result = probe_apply_readiness(source, proposed, config)
    assert result["status"] == "blocked_sidecar_destination"
    assert result["error"] == "sidecar_destination_exists_and_is_not_mediataggerbot_owned"
    assert source.read_bytes() == b"unsupported container bytes"


def test_synchronized_project_path_warning_is_advisory_only():
    from mediataggerbot.pathing import synchronized_runtime_hint

    hint = synchronized_runtime_hint(Path(r"C:\Users\User\OneDrive\Bots\MediaTaggerBot"))
    assert hint["detected"] is True
    assert hint["provider_hint"] == "OneDrive"
    assert hint["advisory_only"] is True

def test_runtime_environment_verifier_allows_index_installed_dependencies_without_wheels(tmp_path, monkeypatch):
    from scripts import verify_runtime_environment as verifier

    source_lock = Path(__file__).resolve().parents[1] / "requirements.lock.txt"
    (tmp_path / "requirements.lock.txt").write_bytes(source_lock.read_bytes())
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.5.9"\n', encoding="utf-8")
    monkeypatch.setattr(verifier.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(verifier, "verify_distribution", lambda name, version: {"name": name, "version": version, "record_hashes_checked": 1})

    result = verifier.verify(tmp_path)

    assert result["dependency_source"] == "package_index_with_hash_locked_requirements"
    assert result["wheels"] == []
    assert result["status"] == "verified"


def test_atomic_replace_retries_transient_permission_error(tmp_path, monkeypatch):
    from mediataggerbot import utils

    source = tmp_path / "source.tmp"
    destination = tmp_path / "destination.json"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")
    real_replace = os.replace
    calls = {"count": 0}

    def flaky_replace(left, right):
        calls["count"] += 1
        if calls["count"] < 3:
            raise PermissionError("transient sharing violation")
        return real_replace(left, right)

    monkeypatch.setattr(utils.os, "replace", flaky_replace)
    monkeypatch.setattr(utils.time, "sleep", lambda _seconds: None)

    utils._replace_with_retry(source, destination)

    assert calls["count"] == 3
    assert destination.read_text(encoding="utf-8") == "new"

