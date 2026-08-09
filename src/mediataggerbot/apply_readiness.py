from __future__ import annotations

import os
import stat
import uuid
from pathlib import Path
from typing import Any

from .config import AppConfig
from .metadata import metadata_writer_plan, probe_metadata_writer_readiness, sidecar_destination_readiness
from .rename import build_sidecar_path
from .utils import windows_utf16_units

_PARENT_MUTATION_CACHE: dict[str, dict[str, Any]] = {}


def clear_readiness_probe_cache() -> None:
    _PARENT_MUTATION_CACHE.clear()


def _probe_parent_mutation(parent: Path) -> dict[str, Any]:
    key = os.path.normcase(os.path.abspath(str(parent)))
    cached = _PARENT_MUTATION_CACHE.get(key)
    if cached is not None:
        return {**cached, "cache_hit": True}
    token = uuid.uuid4().hex[:12]
    first = parent / f".mediataggerbot_write_probe_{os.getpid()}_{token}.tmp"
    second = parent / f".mediataggerbot_write_probe_{os.getpid()}_{token}.renamed.tmp"
    result: dict[str, Any] = {
        "status": "unknown",
        "create_ok": False,
        "rename_ok": False,
        "cleanup_ok": False,
        "cache_hit": False,
        "error": "",
    }
    try:
        with first.open("xb") as handle:
            handle.write(b"")
            handle.flush()
        result["create_ok"] = True
        first.replace(second)
        result["rename_ok"] = True
        second.unlink()
        result["cleanup_ok"] = True
        result["status"] = "ready"
    except Exception as exc:
        result["status"] = "blocked_parent_create_or_rename"
        result["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        for candidate in (first, second):
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                result["cleanup_ok"] = False
    _PARENT_MUTATION_CACHE[key] = dict(result)
    return result


def probe_apply_readiness(path: Path, proposed_path: Path | None, config: AppConfig) -> dict[str, Any]:
    """Perform a bounded readiness probe without changing media bytes.

    The probe now mirrors the real workflow more closely: it validates the selected
    metadata parser and performs one cached create/rename/delete probe per parent
    directory.  The temporary probe file is project-identifiable and always cleaned
    up best-effort; no media file is edited.
    """
    writer_plan = metadata_writer_plan(path, config)
    result: dict[str, Any] = {
        "schema": "MediaTaggerBot.apply_readiness.v2",
        "status": "unknown",
        "file_open_rw": False,
        "parent_exists": path.parent.is_dir(),
        "parent_write_hint": os.access(path.parent, os.W_OK),
        "readonly_attribute": False,
        "repairable_readonly": False,
        "proposed_path_length": windows_utf16_units(str(proposed_path)) if proposed_path else None,
        "metadata_writer_plan": writer_plan,
        "metadata_writer_parse_ready": False,
        "metadata_writer_parse_error": "",
        "metadata_sidecar_only": False,
        "sidecar_readiness": {},
        "parent_mutation_probe": {},
        "error": "",
    }
    try:
        mode = path.stat().st_mode
        result["readonly_attribute"] = not bool(mode & stat.S_IWUSR)
    except OSError as exc:
        result["status"] = "blocked_stat_failed"
        result["error"] = f"{type(exc).__name__}: {exc}"
        return result

    writer_supported = bool(writer_plan.get("supported"))
    sidecar_fallback = bool(config.get("processing.write_sidecar_for_unsupported_metadata", True))
    if not writer_supported and not sidecar_fallback:
        result["status"] = "blocked_metadata_writer_unavailable"
        result["error"] = str(writer_plan.get("reason") or "No verified embedded metadata writer")
        return result

    if writer_supported:
        parse_ready, parse_error = probe_metadata_writer_readiness(path, writer_plan)
        result["metadata_writer_parse_ready"] = parse_ready
        result["metadata_writer_parse_error"] = parse_error
        if not parse_ready:
            result["status"] = "blocked_metadata_writer_parse_failed"
            result["error"] = parse_error
            return result
    else:
        result["metadata_sidecar_only"] = True
        result["metadata_writer_parse_ready"] = False
        result["metadata_writer_parse_error"] = str(writer_plan.get("reason") or "embedded writer unavailable")

    sidecar_requested = bool(config.get("processing.create_sidecar_for_every_apply", False)) or (
        sidecar_fallback and not writer_supported
    )
    if sidecar_requested:
        try:
            current_sidecar = build_sidecar_path(path, config)
            final_media = proposed_path or path
            final_sidecar = build_sidecar_path(final_media, config)
        except Exception as exc:
            result["status"] = "blocked_sidecar_path_invalid"
            result["error"] = f"{type(exc).__name__}: {exc}"
            return result
        checks = []
        for media_candidate, sidecar_candidate, phase in (
            (path, current_sidecar, "current"),
            (final_media, final_sidecar, "final"),
        ):
            ready, reason = sidecar_destination_readiness(media_candidate, sidecar_candidate)
            checks.append({"phase": phase, "path": str(sidecar_candidate), "ready": ready, "reason": reason})
            if not ready:
                result["sidecar_readiness"] = {"checks": checks}
                result["status"] = "blocked_sidecar_destination"
                result["error"] = reason
                return result
        result["sidecar_readiness"] = {"checks": checks}

    try:
        mode = "r+b" if writer_supported else "rb"
        with path.open(mode) as handle:
            handle.seek(0, os.SEEK_END)
        result["file_open_rw"] = writer_supported
        result["file_open_read"] = True
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
        return result

    if bool(config.get("processing.probe_parent_directory_mutation", True)):
        parent_probe = _probe_parent_mutation(path.parent)
        result["parent_mutation_probe"] = parent_probe
        if str(parent_probe.get("status") or "").startswith("blocked_"):
            result["status"] = str(parent_probe["status"])
            result["error"] = str(parent_probe.get("error") or "")
            return result

    if not result["parent_write_hint"]:
        result["status"] = "warning_parent_write_not_confirmed"
    else:
        result["status"] = "ready"
    return result


def readiness_blocks_apply(result: dict[str, Any]) -> bool:
    return str(result.get("status") or "").startswith("blocked_")
