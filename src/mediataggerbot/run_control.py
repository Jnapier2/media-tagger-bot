from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .single_instance import read_lock_payload, read_lock_status
from .timeutil import now_utc
from .utils import write_json_atomic

STOP_REQUEST_FILENAME = "graceful_stop_request.json"


def request_graceful_stop(state_dir: Path, lock_path: Path, stale_after_seconds: int) -> dict[str, Any]:
    """Request that the active owner finalize after its current bounded operation.

    This function never kills a process and never touches media. The request is tied to
    the active lock's random owner token so a stale request cannot stop a future run.
    """
    status = read_lock_status(lock_path, stale_after_seconds)
    payload = read_lock_payload(lock_path)
    result: dict[str, Any] = {
        "schema": "MediaTaggerBot.graceful_stop_request.v1",
        "requested_utc": now_utc().isoformat(),
        "state_dir": str(state_dir),
        "lock_path": str(lock_path),
        "active_run_found": bool(status.get("active")),
        "request_written": False,
        "media_files_mutated": False,
        "lock_status": status,
    }
    owner_token = str(payload.get("owner_token") or "")
    if not status.get("active") or not owner_token:
        result["status"] = "no_active_run"
        result["message"] = "No active owner-aware MediaTaggerBot run was found."
        return result

    request = {
        "schema": "MediaTaggerBot.graceful_stop_request.v1",
        "requested_utc": result["requested_utc"],
        "owner_token": owner_token,
        "run_id": str(payload.get("run_id") or ""),
        "mode": str(payload.get("mode") or ""),
        "request": "finish_current_bounded_step_then_finalize_partial_outputs",
        "media_files_mutated": False,
    }
    state_dir.mkdir(parents=True, exist_ok=True)
    request_path = state_dir / STOP_REQUEST_FILENAME
    write_json_atomic(request_path, request)
    result.update(
        {
            "status": "requested",
            "request_written": True,
            "request_path": str(request_path),
            "target_run_id": request["run_id"],
            "target_mode": request["mode"],
            "message": "Graceful stop requested; the active run will finalize at its next safe checkpoint.",
        }
    )
    return result


def check_graceful_stop(state_dir: Path, owner_token: str) -> tuple[bool, dict[str, Any]]:
    request_path = state_dir / STOP_REQUEST_FILENAME
    payload = _read_json(request_path)
    if not payload:
        return False, {}
    if str(payload.get("owner_token") or "") != str(owner_token or ""):
        return False, {**payload, "status": "stale_owner_mismatch"}
    return True, {**payload, "status": "matched_active_owner", "request_path": str(request_path)}


def clear_graceful_stop(state_dir: Path, owner_token: str | None = None) -> bool:
    request_path = state_dir / STOP_REQUEST_FILENAME
    if not request_path.exists():
        return False
    if owner_token:
        payload = _read_json(request_path)
        if str(payload.get("owner_token") or "") != str(owner_token):
            return False
    request_path.unlink(missing_ok=True)
    return True


def graceful_stop_status(state_dir: Path) -> dict[str, Any]:
    request_path = state_dir / STOP_REQUEST_FILENAME
    payload = _read_json(request_path)
    return {
        "schema": "MediaTaggerBot.graceful_stop_status.v1",
        "path": str(request_path),
        "exists": request_path.exists(),
        "request": payload,
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}
