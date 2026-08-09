from __future__ import annotations

import json
import os
import sqlite3
import zipfile
from pathlib import Path

import pytest

import mediataggerbot.diagnostics as diagnostics_module
import mediataggerbot.main as main_module
from mediataggerbot.cache import JsonCache
from mediataggerbot.config import AppConfig, load_config
from mediataggerbot.diagnostics import write_diagnostics_export
from mediataggerbot.main import run_processing_mode, run_rollback
from mediataggerbot.models import MediaFile, ScanCoverage
from mediataggerbot.operation_journal import OperationJournal
from mediataggerbot.run_control import (
    check_graceful_stop,
    clear_graceful_stop,
    request_graceful_stop,
)
from mediataggerbot.runtime_state import write_run_exit_report
from mediataggerbot.scanner import scan_media_root
from mediataggerbot.single_instance import SingleInstanceLock

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def isolated_config(tmp_path: Path) -> AppConfig:
    cfg = load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")
    media_root = tmp_path / "Private Music Library"
    media_root.mkdir(parents=True, exist_ok=True)
    cfg.data["paths"].update(
        {
            "media_root": str(media_root),
            "logs_dir": str(tmp_path / "logs"),
            "exports_dir": str(tmp_path / "exports"),
            "state_dir": str(tmp_path / "state"),
            "diagnostics_dir": str(tmp_path / "diagnostics"),
            "temp_dir": str(tmp_path / "temp"),
        }
    )
    for directory in [cfg.logs_dir, cfg.exports_dir, cfg.state_dir, cfg.diagnostics_dir, cfg.temp_dir]:
        directory.mkdir(parents=True, exist_ok=True)
    return cfg


def test_owner_bound_graceful_stop_request_does_not_match_future_owner(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    lock_path = state_dir / "mediataggerbot.lock"
    lock = SingleInstanceLock(lock_path, run_id="active-run", mode="dry-run")
    lock.acquire()
    try:
        result = request_graceful_stop(state_dir, lock_path, 3600)
        assert result["status"] == "requested"
        assert result["target_run_id"] == "active-run"
        matched, payload = check_graceful_stop(state_dir, lock.owner_token)
        assert matched is True
        assert payload["status"] == "matched_active_owner"
        wrong, stale = check_graceful_stop(state_dir, "different-owner-token")
        assert wrong is False
        assert stale["status"] == "stale_owner_mismatch"
        assert clear_graceful_stop(state_dir, "different-owner-token") is False
        assert clear_graceful_stop(state_dir, lock.owner_token) is True
    finally:
        lock.release()


def test_recursive_scanner_honors_graceful_stop_and_writes_partial_proof(tmp_path: Path) -> None:
    cfg = isolated_config(tmp_path)
    root = cfg.media_root
    assert root is not None
    for index in range(5):
        folder = root / f"folder_{index}" / "nested"
        folder.mkdir(parents=True)
        (folder / f"song_{index}.mp3").write_bytes(b"")
    cfg.data["processing"]["scan_progress_every_directories"] = 1
    stop = {"requested": False}
    progress: list[tuple[str, int]] = []

    def callback(phase: str, processed: int, total: int | None, relative: str) -> None:
        progress.append((phase, processed))
        if phase == "directory_discovery" and processed >= 1:
            stop["requested"] = True

    files, coverage = scan_media_root(
        root,
        cfg,
        progress_callback=callback,
        stop_check=lambda: stop["requested"],
    )

    assert progress
    assert coverage.graceful_stop_requested is True
    assert coverage.status == "partial_graceful_stop"
    assert coverage.all_reachable_subfolders_checked is False
    assert coverage.stopped_phase == "directory_discovery"
    assert len(files) == 0


def test_apply_fails_closed_before_cache_or_mutation_when_scan_is_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = isolated_config(tmp_path)
    root = cfg.media_root
    assert root is not None
    media_path = root / "Artist - Song.mp3"
    media_path.write_bytes(b"unchanged")
    media = MediaFile(
        path=media_path,
        rel_path=media_path.name,
        extension=".mp3",
        size_bytes=media_path.stat().st_size,
        modified_ns=media_path.stat().st_mtime_ns,
        relative_depth=0,
        media_kind="audio",
    )
    coverage = ScanCoverage(
        root=str(root),
        recursive=True,
        require_recursive_scan=True,
        follow_directory_symlinks=False,
        started_utc="2026-01-01T00:00:00+00:00",
        finished_utc="2026-01-01T00:00:01+00:00",
        status="partial_file_limit",
        all_reachable_subfolders_checked=False,
        limit_reached=True,
        media_files_found=1,
        media_files_scanned=1,
        directories_visited=1,
        subdirectories_discovered=1,
    )
    monkeypatch.setattr(main_module, "scan_media_root", lambda *args, **kwargs: ([media], coverage))

    def mutation_must_not_run(*args, **kwargs):
        raise AssertionError("apply_plan must not run after partial traversal")

    monkeypatch.setattr(main_module, "apply_plan", mutation_must_not_run)
    diag = cfg.diagnostics_dir / "dummy.zip"
    diag.write_bytes(b"diagnostic")
    monkeypatch.setattr(main_module, "write_diagnostics_export", lambda *args, **kwargs: diag)
    log = cfg.logs_dir / "run.log"
    log.write_text("test", encoding="utf-8")

    code = run_processing_mode(cfg, "apply-safe", "partial_apply_test", log, lock=None)

    assert code == 3
    assert media_path.read_bytes() == b"unchanged"
    assert not (cfg.state_dir / "api_cache.sqlite3").exists()
    exit_report = json.loads((cfg.state_dir / "last_run_exit.json").read_text(encoding="utf-8"))
    assert exit_report["completion_class"] == "blocked_before_mutation"
    assert exit_report["exit_code"] == 3


def test_rollback_blocks_entire_manifest_when_any_path_is_outside_media_root(tmp_path: Path) -> None:
    cfg = isolated_config(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    new_path = outside / "renamed.mp3"
    original_path = outside / "original.mp3"
    new_path.write_bytes(b"do-not-move")
    manifest = tmp_path / "rollback_manifest_bad.json"
    manifest.write_text(
        json.dumps([{"original_path": str(original_path), "new_path": str(new_path)}]),
        encoding="utf-8",
    )
    log = cfg.logs_dir / "rollback.log"
    log.write_text("rollback test", encoding="utf-8")

    code = run_rollback(cfg, str(manifest), "rollback_bad_path", log)

    assert code == 2
    assert new_path.exists()
    assert not original_path.exists()
    result = json.loads((cfg.exports_dir / "rollback_result_rollback_bad_path.json").read_text(encoding="utf-8"))
    assert result["status"] == "blocked_manifest_validation"
    assert result["media_files_mutated"] is False
    assert result["results"][0]["error"] == "path_outside_configured_media_root"


def test_api_cache_corruption_is_quarantined_and_rebuilt(tmp_path: Path) -> None:
    path = tmp_path / "cache.sqlite3"
    path.write_bytes(b"not-a-sqlite-database")

    with JsonCache(path, auto_recover=True) as cache:
        assert cache.recovery["recovered"] is True
        assert cache.recovery["quarantined_files"]
        cache.set("provider", "key", {"ok": True})
        assert cache.get("provider", "key") == {"ok": True}


def test_api_cache_write_failure_is_nonfatal_for_provider_result(tmp_path: Path) -> None:
    cache = JsonCache(tmp_path / "cache.sqlite3")
    assert cache.conn is not None
    cache.conn.close()
    cache.set("provider", "key", {"ok": True})
    assert cache.disabled is True
    assert cache.stats["write_errors"] == 1
    cache.close()


def test_corrupt_operation_journal_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "operation_journal.sqlite3"
    path.write_bytes(b"corrupt")
    with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
        OperationJournal(path, "run")


def test_diagnostics_redacts_media_and_user_paths_and_enforces_final_manifest_count(tmp_path: Path) -> None:
    cfg = isolated_config(tmp_path)
    report = cfg.exports_dir / "needs_review.csv"
    assert cfg.media_root is not None
    private_media_path = cfg.media_root / "Secret Artist" / "Secret Song.mp3"
    report.write_text(
        f'path,error\n"{private_media_path}","tool at C:\\Users\\Alice\\bin\\ffprobe.exe"\n',
        encoding="utf-8",
    )
    log = cfg.logs_dir / "run.log"
    log.write_text(f"Scanning {private_media_path}\nHome C:\\Users\\Alice\\Documents\n", encoding="utf-8")

    path = write_diagnostics_export(
        cfg,
        "redaction_test",
        "diagnostics",
        log_path=log,
        report_paths={"needs_review_csv": report},
    )

    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        names = archive.namelist()
        manifest = json.loads(archive.read("diagnostic_export_manifest.json"))
        assert manifest["file_count_in_zip"] == len(names)
        assert sum(info.file_size for info in archive.infolist()) <= cfg.get(
            "processing.diagnostic_max_total_bytes"
        )
        for name in names:
            data = archive.read(name).decode("utf-8", errors="ignore")
            assert str(cfg.media_root) not in data
            assert "C:\\Users\\Alice" not in data
        report_name = next(name for name in names if name.startswith("reports/needs_review_csv_"))
        report_text = archive.read(report_name).decode("utf-8")
        assert "<MEDIA_ROOT>" in report_text
        assert "<USER_HOME>" in report_text


def test_diagnostics_primary_failure_produces_integrity_checked_minimal_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = isolated_config(tmp_path)

    def fail_primary(*args, **kwargs):
        raise RuntimeError("forced primary export failure")

    monkeypatch.setattr(diagnostics_module, "_write_diagnostics_export_primary", fail_primary)
    path = diagnostics_module.write_diagnostics_export(cfg, "fallback_test", "diagnostics")

    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
        assert archive.namelist() == ["diagnostic_failure.json"]
        payload = json.loads(archive.read("diagnostic_failure.json"))
        assert payload["status"] == "minimal_fallback_after_primary_export_failure"
        assert payload["media_files_mutated"] is False


def test_run_exit_report_can_avoid_overwriting_active_last_exit(tmp_path: Path) -> None:
    cfg = isolated_config(tmp_path)
    sentinel = {"run_id": "active-run", "status": "running"}
    (cfg.state_dir / "last_run_exit.json").write_text(json.dumps(sentinel), encoding="utf-8")

    report = write_run_exit_report(
        cfg,
        "control-run",
        "request-stop",
        exit_code=0,
        terminal_status="completed",
        completion_class="completed_verified",
        update_last=False,
    )

    assert report.exists()
    assert json.loads((cfg.state_dir / "last_run_exit.json").read_text(encoding="utf-8")) == sentinel
