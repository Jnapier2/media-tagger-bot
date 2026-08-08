from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import AppConfig
from .timeutil import now_utc
from .utils import write_json_atomic


def write_run_status(
    config: AppConfig,
    run_id: str,
    mode: str,
    status: str,
    last_step: str,
    processed_files: int = 0,
    total_files: int | None = None,
    shutdown_reason: str = "",
    extra: dict[str, Any] | None = None,
) -> Path:
    payload: dict[str, Any] = {
        "schema": "MediaTaggerBot.run_status.v2",
        "updated_utc": now_utc().isoformat(),
        "run_id": run_id,
        "mode": mode,
        "status": status,
        "last_step": last_step,
        "processed_files": processed_files,
        "total_files": total_files,
        "shutdown_reason": shutdown_reason,
    }
    if extra:
        payload["extra"] = extra
    path = config.state_dir / "last_run_status.json"
    write_json_atomic(path, payload)
    return path


def write_run_exit_report(
    config: AppConfig,
    run_id: str,
    mode: str,
    *,
    exit_code: int,
    terminal_status: str,
    completion_class: str,
    completed_verified: list[str] | None = None,
    completed_not_fully_verified: list[str] | None = None,
    partial_or_rushed: list[str] | None = None,
    skipped_deferred_blocked: list[str] | None = None,
    actual_timeouts_errors: list[str] | None = None,
    exact_outputs: dict[str, str | Path] | None = None,
    safest_next_action: str = "Review the run status and diagnostics before the next mutating mode.",
    details: dict[str, Any] | None = None,
    update_last: bool = True,
) -> Path:
    """Write a truthful, machine-readable end-of-run/early-exit record.

    The field names intentionally mirror the project's long-form triage/exit rules:
    verified work is kept separate from unverified, partial, blocked, and actual errors.
    """
    outputs = {str(key): str(value) for key, value in (exact_outputs or {}).items() if value}
    payload: dict[str, Any] = {
        "schema": "MediaTaggerBot.run_exit_report.v1",
        "created_utc": now_utc().isoformat(),
        "run_id": run_id,
        "mode": mode,
        "exit_code": int(exit_code),
        "terminal_status": terminal_status,
        "completion_class": completion_class,
        "completed_verified": list(completed_verified or []),
        "completed_not_fully_verified": list(completed_not_fully_verified or []),
        "partial_or_rushed": list(partial_or_rushed or []),
        "skipped_deferred_blocked": list(skipped_deferred_blocked or []),
        "actual_timeouts_errors": list(actual_timeouts_errors or []),
        "exact_outputs": outputs,
        "safest_next_action": safest_next_action,
    }
    if details:
        payload["details"] = details

    if update_last:
        state_path = config.state_dir / "last_run_exit.json"
        write_json_atomic(state_path, payload)
    output_dir = config.exports_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / f"run_exit_report_{run_id}.json"
    write_json_atomic(report_path, payload)
    return report_path


def last_exit_matches_run(config: AppConfig, run_id: str) -> bool:
    path = config.state_dir / "last_run_exit.json"
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(payload, dict) and str(payload.get("run_id") or "") == run_id
    except (OSError, ValueError, TypeError):
        return False


def finalize_abandoned_run_from_lock(
    config: AppConfig,
    recovery_event: dict[str, Any],
    *,
    current_run_id: str,
    current_mode: str,
) -> dict[str, Path]:
    """Preserve truthful evidence for a same-host run whose owner process died.

    The function never touches media and never retries journal operations. It records
    that the prior process failed to reach normal finalization and, for interrupted
    mutating modes, creates a review marker that a complete dry-run can later clear.
    """
    prior_run_id = str(recovery_event.get("prior_run_id") or "").strip()
    prior_mode = str(recovery_event.get("prior_mode") or "unknown").strip() or "unknown"
    outputs: dict[str, Path] = {}
    config.state_dir.mkdir(parents=True, exist_ok=True)

    status_snapshot: dict[str, Any] = {}
    status_path = config.state_dir / "last_run_status.json"
    try:
        import json

        loaded = json.loads(status_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict) and (not prior_run_id or str(loaded.get("run_id") or "") == prior_run_id):
            status_snapshot = loaded
    except (OSError, ValueError, TypeError):
        status_snapshot = {}

    evidence = {
        "schema": "MediaTaggerBot.abandoned_run_recovery.v1",
        "created_utc": now_utc().isoformat(),
        "prior_run_id": prior_run_id,
        "prior_mode": prior_mode,
        "current_run_id": current_run_id,
        "current_mode": current_mode,
        "lock_recovery": recovery_event,
        "prior_status_snapshot": status_snapshot,
        "operation_journal_path": str(config.state_dir / "operation_journal.sqlite3"),
        "media_files_mutated_by_recovery": False,
        "automatic_retry_performed": False,
        "safest_next_action": (
            "Run Repair/check and a complete Dry-run, then review failed_operations and needs_review before another mutating mode."
        ),
    }
    recovery_state = config.state_dir / "last_abandoned_run_recovery.json"
    write_json_atomic(recovery_state, evidence)
    outputs["abandoned_run_recovery_state"] = recovery_state

    if prior_run_id:
        prior_dir = config.exports_dir / prior_run_id
        prior_dir.mkdir(parents=True, exist_ok=True)
        receipt = prior_dir / f"abandoned_run_recovery_{prior_run_id}.json"
        write_json_atomic(receipt, evidence)
        outputs["abandoned_run_recovery_receipt"] = receipt
        exit_path = prior_dir / f"run_exit_report_{prior_run_id}.json"
        if not exit_path.exists():
            exit_path = write_run_exit_report(
                config,
                prior_run_id,
                prior_mode,
                exit_code=76,
                terminal_status="abandoned_run_recovered",
                completion_class="partial_not_fully_verified",
                completed_verified=[
                    "The same-host owner PID was no longer running; stale lock evidence was preserved before recovery.",
                    "Lock recovery itself did not read or mutate media files.",
                ],
                completed_not_fully_verified=[
                    "Any operations completed before interruption are represented only by the operation journal and partial run outputs."
                ],
                partial_or_rushed=["The prior process did not reach its normal finalization path."],
                actual_timeouts_errors=[
                    "Abandoned process/stale lock: "
                    f"pid={recovery_event.get('prior_pid')} reason={recovery_event.get('stale_reason')} "
                    f"heartbeat_age_seconds={recovery_event.get('heartbeat_age_seconds')}"
                ],
                exact_outputs={
                    "recovery_receipt": receipt,
                    "archived_lock": recovery_event.get("archived_lock_path", ""),
                    "operation_journal": config.state_dir / "operation_journal.sqlite3",
                },
                safest_next_action=evidence["safest_next_action"],
                details={"prior_status_snapshot": status_snapshot},
                update_last=False,
            )
        outputs["prior_run_exit_report"] = exit_path

    if prior_mode in {"apply-safe", "apply-all", "rollback"}:
        marker = config.state_dir / "mutation_recovery_review_required.json"
        marker_payload = {
            "schema": "MediaTaggerBot.mutation_recovery_review.v1",
            "created_utc": now_utc().isoformat(),
            "status": "dry_run_required_before_next_mutating_mode",
            "prior_run_id": prior_run_id,
            "prior_mode": prior_mode,
            "recovery_receipt": str(outputs.get("abandoned_run_recovery_receipt", recovery_state)),
            "failed_operations_report_required": True,
            "clear_condition": "complete dry-run finalization",
            "media_files_mutated_by_marker": False,
        }
        write_json_atomic(marker, marker_payload)
        outputs["mutation_recovery_review_marker"] = marker
    return outputs


def mutation_recovery_review_status(config: AppConfig) -> dict[str, Any]:
    path = config.state_dir / "mutation_recovery_review_required.json"
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, ValueError, TypeError):
        payload = {}
    return {"path": str(path), "exists": path.exists(), "required": bool(payload), "payload": payload}


def clear_mutation_recovery_review(config: AppConfig, run_id: str) -> Path | None:
    path = config.state_dir / "mutation_recovery_review_required.json"
    if not path.exists():
        return None
    status = mutation_recovery_review_status(config)
    receipt = config.exports_dir / run_id / f"mutation_recovery_review_cleared_{run_id}.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        receipt,
        {
            "schema": "MediaTaggerBot.mutation_recovery_review_clearance.v1",
            "cleared_utc": now_utc().isoformat(),
            "cleared_by_run_id": run_id,
            "cleared_by_mode": "dry-run",
            "prior_marker": status.get("payload", {}),
            "media_files_mutated_by_clearance": False,
        },
    )
    path.unlink(missing_ok=True)
    return receipt
