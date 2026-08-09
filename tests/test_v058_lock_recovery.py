from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

import pytest

import mediataggerbot.computer_context as computer_module
import mediataggerbot.main as main_module
import mediataggerbot.single_instance as lock_module
from mediataggerbot.computer_context import local_lock_path
from mediataggerbot.config import load_config
from mediataggerbot.models import MediaFile, ScanCoverage
from mediataggerbot.operation_journal import OperationJournal, read_operation_journal_summary
from mediataggerbot.runtime_state import (
    finalize_abandoned_run_from_lock,
    mutation_recovery_review_status,
)
from mediataggerbot.single_instance import LockBusyError, SingleInstanceLock, read_lock_status

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def isolated_config(tmp_path: Path):
    cfg = load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")
    media_root = tmp_path / "Media"
    media_root.mkdir(parents=True)
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
    for path in (cfg.logs_dir, cfg.exports_dir, cfg.state_dir, cfg.diagnostics_dir, cfg.temp_dir):
        path.mkdir(parents=True, exist_ok=True)
    return cfg


def test_same_host_dead_pid_recovers_after_short_grace_even_with_24h_stale_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "state" / "mediataggerbot.pc-alpha.lock"
    lock_path.parent.mkdir(parents=True)
    payload = {
        "schema": "MediaTaggerBot.single_instance_lock.v3",
        "pid": 99999999,
        "process_start_epoch": 100.0,
        "hostname": socket.gethostname(),
        "owner_token": "dead-owner",
        "run_id": "prior-apply",
        "mode": "apply-safe",
        "computer_id": "PC-ALPHA-01",
        "heartbeat_epoch": time.time() - 6 * 3600,
    }
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(lock_module, "_pid_alive", lambda _pid: False)

    status = read_lock_status(lock_path, stale_after_seconds=86400, dead_owner_grace_seconds=30)
    assert status["active"] is False
    assert status["stale"] is True
    assert status["recovery_eligible"] is True
    assert status["reason"] == "owner_pid_not_alive_recovery_eligible"

    lock = SingleInstanceLock(
        lock_path,
        stale_after_seconds=86400,
        heartbeat_seconds=30,
        dead_owner_grace_seconds=30,
        run_id="current-scan",
        mode="scan-only",
        computer_id="PC-ALPHA-01",
    )
    lock.acquire()
    try:
        assert lock.acquired is True
        assert len(lock.recovery_events) == 1
        event = lock.recovery_events[0]
        assert event["prior_run_id"] == "prior-apply"
        assert Path(event["archived_lock_path"]).exists()
        assert Path(event["receipt_path"]).exists()
        current = json.loads(lock_path.read_text(encoding="utf-8"))
        assert current["run_id"] == "current-scan"
    finally:
        lock.release()
    assert not lock_path.exists()


def test_live_same_host_owner_still_blocks(tmp_path: Path) -> None:
    lock_path = tmp_path / "bot.lock"
    first = SingleInstanceLock(lock_path, run_id="live", mode="dry-run")
    second = SingleInstanceLock(lock_path, run_id="blocked", mode="scan-only")
    first.acquire()
    try:
        with pytest.raises(LockBusyError) as captured:
            second.acquire()
        assert captured.value.status["active"] is True
        assert captured.value.status["same_host"] is True
    finally:
        first.release()


def test_foreign_lock_is_preserved_and_uses_host_scoped_fallback(tmp_path: Path) -> None:
    lock_path = tmp_path / "bot.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 1234,
                "hostname": "FOREIGN-PC",
                "owner_token": "foreign-owner",
                "run_id": "foreign-run",
                "mode": "apply-safe",
                "heartbeat_epoch": time.time(),
            }
        ),
        encoding="utf-8",
    )
    original = lock_path.read_bytes()
    local = SingleInstanceLock(lock_path, run_id="local", mode="scan-only")
    local.acquire()
    fallback = local.lock_path
    try:
        assert fallback != lock_path
        assert "host-" in fallback.name
        assert lock_path.read_bytes() == original
    finally:
        local.release()
    assert lock_path.read_bytes() == original
    assert not fallback.exists()


def test_known_profile_lock_path_is_host_scoped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    context = {"canonical_id": "PC-ALPHA-01"}
    monkeypatch.setattr(computer_module.socket, "gethostname", lambda: "ALPHA")
    first = local_lock_path(tmp_path, context)
    monkeypatch.setattr(computer_module.socket, "gethostname", lambda: "OTHER-ALPHA")
    second = local_lock_path(tmp_path, context)
    assert first != second
    assert first.name.startswith("mediataggerbot.pc-alpha-01-")
    assert second.name.startswith("mediataggerbot.pc-alpha-01-")


def test_abandoned_mutating_run_writes_review_marker_and_prior_exit(tmp_path: Path) -> None:
    cfg = isolated_config(tmp_path)
    event = {
        "prior_run_id": "prior-run",
        "prior_mode": "apply-safe",
        "prior_pid": 9999,
        "stale_reason": "owner_pid_not_alive_recovery_eligible",
        "heartbeat_age_seconds": 20000,
        "archived_lock_path": str(cfg.state_dir / "lock_recovery" / "old.json"),
    }
    outputs = finalize_abandoned_run_from_lock(
        cfg, event, current_run_id="current-run", current_mode="scan-only"
    )
    assert outputs["prior_run_exit_report"].exists()
    prior_exit = json.loads(outputs["prior_run_exit_report"].read_text(encoding="utf-8"))
    assert prior_exit["terminal_status"] == "abandoned_run_recovered"
    marker = mutation_recovery_review_status(cfg)
    assert marker["required"] is True
    assert marker["payload"]["prior_run_id"] == "prior-run"


def test_operation_journal_summary_reports_truncation_counts(tmp_path: Path) -> None:
    path = tmp_path / "journal.sqlite3"
    with OperationJournal(path, "run") as journal:
        for index in range(5):
            source = tmp_path / f"source-{index}.mp3"
            target = tmp_path / f"target-{index}.mp3"
            source.write_bytes(b"x")
            operation = journal.start(source, target)
            journal.fail(operation, "embedded_metadata_write_failed", "test failure")
    summary = read_operation_journal_summary(path, limit=2)
    assert summary["schema"] == "MediaTaggerBot.operation_journal_summary.v2"
    assert summary["incomplete_total"] == 5
    assert summary["incomplete_included"] == 2
    assert summary["incomplete_omitted"] == 3
    assert summary["truncated"] is True


def test_scan_only_with_media_inspection_errors_is_not_completed_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = isolated_config(tmp_path)
    media = cfg.media_root / "broken.mp3"
    media.write_bytes(b"not real media")
    stat_result = media.stat()
    item = MediaFile(
        path=media,
        rel_path="broken.mp3",
        extension=".mp3",
        size_bytes=stat_result.st_size,
        media_kind="audio",
        modified_ns=stat_result.st_mtime_ns,
        relative_depth=1,
        scan_error="parser failed",
    )
    coverage = ScanCoverage(
        root=str(cfg.media_root),
        recursive=True,
        require_recursive_scan=True,
        follow_directory_symlinks=False,
        started_utc="2026-08-02T00:00:00+00:00",
        status="complete_all_subfolders_with_media_errors",
        all_reachable_subfolders_checked=True,
        directories_visited=1,
        subdirectories_discovered=0,
        media_files_found=1,
        media_files_scanned=1,
        media_scan_errors=1,
    )

    def fake_scan(*_args, **_kwargs):
        return [item], coverage

    monkeypatch.setattr(main_module, "scan_media_root", fake_scan)
    monkeypatch.setattr(
        main_module,
        "write_diagnostics_export",
        lambda *_args, **_kwargs: cfg.diagnostics_dir / "diag.zip",
    )
    (cfg.diagnostics_dir / "diag.zip").write_bytes(b"diag")
    code = main_module.run_processing_mode(cfg, "scan-only", "scan-errors", cfg.logs_dir / "run.log")
    assert code == 2
    exit_report = json.loads(
        (cfg.exports_dir / "scan-errors" / "run_exit_report_scan-errors.json").read_text(encoding="utf-8")
    )
    assert exit_report["completion_class"] == "partial_not_fully_verified"
    assert exit_report["terminal_status"] == "completed_with_errors"


def test_diagnostic_action_summary_prioritizes_recovery_and_scan_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import mediataggerbot.diagnostics as diagnostics_module
    from mediataggerbot.diagnostics import build_diagnostic_action_summary
    from mediataggerbot.utils import write_json_atomic

    cfg = isolated_config(tmp_path)
    monkeypatch.setattr(
        diagnostics_module,
        "fingerprint_backend_status",
        lambda *_args, **_kwargs: {"available": False, "selected_backend": "none"},
    )
    marker = cfg.state_dir / "mutation_recovery_review_required.json"
    write_json_atomic(marker, {"prior_run_id": "prior", "prior_mode": "apply-safe"})
    write_json_atomic(
        cfg.state_dir / "last_scan_coverage.json",
        {
            "status": "complete_all_subfolders_with_media_errors",
            "media_scan_errors": 484,
            "all_reachable_subfolders_checked": True,
        },
    )
    cfg.data["project"]["contact"] = "local-user@example.invalid"
    summary = build_diagnostic_action_summary(
        cfg,
        {
            "active": False,
            "stale": True,
            "recovery_eligible": True,
            "run_id": "prior",
            "reason": "owner_pid_not_alive_recovery_eligible",
        },
        {"status_counts": {"failed": 178}, "retryable_count": 0},
    )
    codes = {item["code"] for item in summary["items"]}
    assert summary["highest_priority"] == "High"
    assert "stale_lock_recovery_available" in codes
    assert "operation_journal_requires_review" in codes
    assert "interrupted_mutation_dry_run_required" in codes
    assert "media_inspection_errors" in codes
    assert "fingerprint_backend_unavailable" in codes
    assert "musicbrainz_contact_placeholder" in codes


def test_complete_dry_run_clearance_receipt_removes_recovery_marker(tmp_path: Path) -> None:
    from mediataggerbot.runtime_state import clear_mutation_recovery_review
    from mediataggerbot.utils import write_json_atomic

    cfg = isolated_config(tmp_path)
    marker = cfg.state_dir / "mutation_recovery_review_required.json"
    write_json_atomic(
        marker,
        {
            "schema": "MediaTaggerBot.mutation_recovery_review.v1",
            "prior_run_id": "prior-run",
            "prior_mode": "apply-safe",
        },
    )
    receipt = clear_mutation_recovery_review(cfg, "review-dry-run")
    assert receipt is not None and receipt.exists()
    assert not marker.exists()
    payload = json.loads(receipt.read_text(encoding="utf-8"))
    assert payload["cleared_by_mode"] == "dry-run"
    assert payload["media_files_mutated_by_clearance"] is False


def test_alive_owner_never_auto_recovers_when_start_identity_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "alive.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 43210,
                "hostname": socket.gethostname(),
                "owner_token": "alive-owner",
                "run_id": "alive-run",
                "mode": "apply-safe",
                "heartbeat_epoch": time.time() - 172800,
                "process_start_epoch": 123.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(lock_module, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(lock_module, "_process_start_epoch", lambda _pid: None)
    status = read_lock_status(lock_path, stale_after_seconds=86400, dead_owner_grace_seconds=30)
    assert status["active"] is True
    assert status["stale"] is False
    assert status["recovery_eligible"] is False
    assert status["reason"] == "owner_pid_alive_identity_unavailable"


def test_reused_pid_recovers_after_short_grace_not_full_stale_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "reused.lock"
    lock_path.write_text(
        json.dumps(
            {
                "pid": 43210,
                "hostname": socket.gethostname(),
                "owner_token": "old-owner",
                "run_id": "old-run",
                "mode": "apply-safe",
                "heartbeat_epoch": time.time() - 3600,
                "process_start_epoch": 100.0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(lock_module, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(lock_module, "_process_start_epoch", lambda _pid: 200.0)
    status = read_lock_status(lock_path, stale_after_seconds=86400, dead_owner_grace_seconds=30)
    assert status["active"] is False
    assert status["stale"] is True
    assert status["recovery_eligible"] is True
    assert status["reason"] == "pid_reused_recovery_eligible"
