from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from .config import AppConfig


def probe_apply_readiness(path: Path, proposed_path: Path | None, config: AppConfig) -> dict[str, Any]:
    """Perform a bounded, non-mutating snapshot of write/rename readiness.

    Opening a file in ``r+b`` mode does not write bytes, but it catches many
    read-only, ACL, sharing, and active-lock failures before an apply run reaches
    the metadata writer.  This is a snapshot, not a guarantee that another
    process will not lock the file later.
    """
    result: dict[str, Any] = {
        "schema": "MediaTaggerBot.apply_readiness.v1",
        "status": "unknown",
        "file_open_rw": False,
        "parent_exists": path.parent.is_dir(),
        "parent_write_hint": os.access(path.parent, os.W_OK),
        "readonly_attribute": False,
        "repairable_readonly": False,
        "proposed_path_length": len(str(proposed_path)) if proposed_path else None,
        "error": "",
    }
    try:
        mode = path.stat().st_mode
        result["readonly_attribute"] = not bool(mode & stat.S_IWUSR)
    except OSError as exc:
        result["status"] = "blocked_stat_failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    try:
        with path.open("r+b") as handle:
            handle.seek(0, os.SEEK_END)
        result["file_open_rw"] = True
    except PermissionError as exc:
        if result["readonly_attribute"] and bool(config.get("processing.repair_readonly_attribute_on_apply", True)):
            result["repairable_readonly"] = True
            result["status"] = "readonly_repairable"
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        result["status"] = "blocked_permission_or_lock"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result
    except OSError as exc:
        result["status"] = "blocked_open_failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    if not result["parent_exists"]:
        result["status"] = "blocked_parent_missing"
    elif not result["parent_write_hint"]:
        result["status"] = "warning_parent_write_not_confirmed"
    else:
        result["status"] = "ready"
    return result


def readiness_blocks_apply(result: dict[str, Any]) -> bool:
    return str(result.get("status") or "").startswith("blocked_")
