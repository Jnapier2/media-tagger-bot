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
