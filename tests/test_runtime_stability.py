from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from mediataggerbot.config import load_config
from mediataggerbot.main import apply_plan, rename_path_with_case_support, verify_source_unchanged
from mediataggerbot.metadata import verify_metadata_write, write_metadata
from mediataggerbot.models import GenreResult, MatchResult, MediaFile, PlanResult
from mediataggerbot.operation_journal import OperationJournal, read_operation_journal_summary
from mediataggerbot.pathing import update_media_root_in_config
from mediataggerbot.rename import build_target_path
from mediataggerbot.single_instance import SingleInstanceLock

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def config(tmp_path: Path):
    cfg = load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")
    cfg.data["paths"]["state_dir"] = str(tmp_path / "state")
    cfg.data["paths"]["exports_dir"] = str(tmp_path / "exports")
    return cfg


def match() -> MatchResult:
    return MatchResult(
        matched=True,
        confidence=99.0,
        source="musicbrainz_recording_id_tag",
        artist="Artist",
        source_artist_credit="Artist",
        title="Song",
        musicbrainz_recording_id="11111111-1111-1111-1111-111111111111",
        identity_tier="stable_identifier",
        ambiguity_status="exact_stable_identifier",
        raw_genres=["pop"],
    )


def genre() -> GenreResult:
    return GenreResult(
        main_genre="Pop",
        filename_main_genre="Pop",
        subgenre=None,
        raw_terms=["pop"],
        source="database_terms",
        confidence=90.0,
    )


def media(path: Path) -> MediaFile:
    stat = path.stat()
    return MediaFile(
        path=path,
        rel_path=path.name,
        extension=path.suffix,
        size_bytes=stat.st_size,
        modified_ns=stat.st_mtime_ns,
        media_kind="audio",
    )


def test_source_change_guard_detects_change_after_scan(tmp_path: Path):
    path = tmp_path / "song.mp3"
    path.write_bytes(b"first")
    item = media(path)
    assert verify_source_unchanged(item)[0] is True

    time.sleep(0.002)
    path.write_bytes(b"second-content")
    ok, details = verify_source_unchanged(item)
    assert ok is False
    assert "changed" in details["reason"].lower()


def test_run_wide_target_reservation_prevents_dry_run_collisions(tmp_path: Path):
    cfg = config(tmp_path)
    first = tmp_path / "a.mp3"
    second = tmp_path / "b.mp3"
    first.write_bytes(b"")
    second.write_bytes(b"")
    reserved: set[str] = set()

    target_one = build_target_path(first, match(), genre(), cfg, reserved_paths=reserved)
    target_two = build_target_path(second, match(), genre(), cfg, reserved_paths=reserved)

    assert target_one.name == "Artist - Song - Pop.mp3"
    assert target_two.name == "Artist - Song - Pop (2).mp3"


def test_apply_safe_writes_verifies_and_renames_with_journal(tmp_path: Path):
    cfg = config(tmp_path)
    source = tmp_path / "old.mp3"
    source.write_bytes(b"")
    item = media(source)
    target = build_target_path(source, match(), genre(), cfg)
    plan = PlanResult(
        media=item,
        match=match(),
        genre=genre(),
        proposed_path=target,
        proposed_filename=target.name,
        action="apply_safe",
        should_apply=True,
    )
    journal_path = cfg.state_dir / "operation_journal.sqlite3"
    with OperationJournal(journal_path, "run") as journal:
        apply_plan(plan, cfg, "run", mode="apply-safe", journal=journal)

    assert plan.status == "applied"
    assert plan.metadata_written is True
    assert plan.metadata_verified is True
    assert plan.renamed is True
    assert plan.rename_verified is True
    assert target.exists()
    assert not source.exists()
    summary = read_operation_journal_summary(journal_path)
    assert summary["status_counts"]["completed"] == 1


def test_metadata_verification_rereads_id3(tmp_path: Path):
    cfg = config(tmp_path)
    path = tmp_path / "song.mp3"
    path.write_bytes(b"")
    wrote, error, sidecar = write_metadata(path, match(), genre(), cfg)
    verified, details = verify_metadata_write(path, match(), genre(), embedded_written=wrote, sidecar_path=sidecar)

    assert error is None
    assert verified is True
    assert details["method"] == "mutagen_reread"


def test_operation_journal_reconciles_retryable_and_completed_paths(tmp_path: Path):
    journal_path = tmp_path / "journal.sqlite3"
    retry_source = tmp_path / "retry.mp3"
    retry_source.write_bytes(b"")
    completed_source = tmp_path / "gone.mp3"
    completed_target = tmp_path / "done.mp3"

    with OperationJournal(journal_path, "old") as old:
        old.start(retry_source, tmp_path / "retry-target.mp3")
        op = old.start(completed_source, completed_target)
        completed_target.write_bytes(b"")
        old.update(op, "renamed")

    with OperationJournal(journal_path, "new") as current:
        result = current.reconcile_prior_incomplete()

    assert result["retryable"] == 1
    assert result["completed_after_crash"] == 1
    summary = read_operation_journal_summary(journal_path)
    assert summary["status_counts"]["retryable"] == 1
    assert summary["status_counts"]["completed"] == 1


def test_single_instance_lock_blocks_live_owner_and_recovers_stale_dead_owner(tmp_path: Path):
    lock_path = tmp_path / "bot.lock"
    first = SingleInstanceLock(lock_path, stale_after_seconds=60, heartbeat_seconds=5)
    second = SingleInstanceLock(lock_path, stale_after_seconds=60, heartbeat_seconds=5)
    first.acquire()
    try:
        with pytest.raises(RuntimeError, match="appears active"):
            second.acquire()
    finally:
        first.release()

    lock_path.write_text(
        json.dumps({
            "pid": 99999999,
            "hostname": os.environ.get("COMPUTERNAME", "definitely-other-host"),
            "owner_token": "old",
            "heartbeat_epoch": time.time() - 3600,
        }),
        encoding="utf-8",
    )
    os.utime(lock_path, (time.time() - 3600, time.time() - 3600))
    recovered = SingleInstanceLock(lock_path, stale_after_seconds=60, heartbeat_seconds=5)
    recovered.acquire()
    assert recovered.acquired is True
    recovered.release()



def test_case_only_canonical_spelling_uses_safe_two_step_rename(tmp_path: Path):
    source = tmp_path / "artist - song - pop.mp3"
    target = tmp_path / "Artist - Song - Pop.mp3"
    source.write_bytes(b"content")

    rename_path_with_case_support(source, target)

    assert target.exists()
    assert target.read_bytes() == b"content"
    assert not any(entry.name == source.name for entry in tmp_path.iterdir())
    assert not list(tmp_path.glob(".mtb_case_*.tmp"))



def test_set_root_validates_toml_and_preserves_path_metacharacters(tmp_path: Path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[paths]\nmedia_root = ""\n', encoding="utf-8")
    requested = r"D:\R&B Music #1"

    result = update_media_root_in_config(config_path, requested, backup=True)

    import tomllib

    with config_path.open("rb") as handle:
        parsed = tomllib.load(handle)
    assert parsed["paths"]["media_root"] == requested
    assert Path(result["backup_path"]).exists()
    assert not config_path.with_suffix(".toml.tmp").exists()


def test_operation_journal_read_only_summary_supports_spaces_and_hash(tmp_path: Path):
    special_dir = tmp_path / "Moved Bot Folder #1" / "state"
    journal_path = special_dir / "operation journal.sqlite3"
    source = tmp_path / "source.mp3"
    target = tmp_path / "target.mp3"
    source.write_bytes(b"content")

    with OperationJournal(journal_path, "special-path-run") as journal:
        operation_id = journal.start(source, target)
        journal.complete(operation_id)

    summary = read_operation_journal_summary(journal_path)

    assert "read_error" not in summary
    assert summary["status_counts"]["completed"] == 1
    assert summary["path"] == str(journal_path)
