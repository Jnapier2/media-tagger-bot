from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Mapping

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.11+ is required
    tomllib = None  # type: ignore

from .launcher_attestation import build_launcher_attestation
from .run_control import request_graceful_stop
from .timeutil import now_utc, timestamp_for_filename
from .utils import write_json_atomic


def run_portable_stop(
    project_root: Path,
    *,
    app_version: str,
    env: Mapping[str, str] | None = None,
) -> tuple[int, dict[str, Any], Path]:
    """Request a safe stop without constructing or repairing the application runtime.

    This path intentionally depends only on the Python standard library and small
    project modules that also use only the standard library.  It never creates,
    deletes, installs into, or validates ``.venv`` and never reads media files.
    """
    project_root = project_root.resolve()
    config_path = project_root / "config" / "config.toml"
    raw_config, config_status = _load_config_best_effort(config_path)
    paths = raw_config.get("paths") if isinstance(raw_config.get("paths"), dict) else {}
    processing = raw_config.get("processing") if isinstance(raw_config.get("processing"), dict) else {}
    state_dir, state_dir_status = _runtime_dir(project_root, paths.get("state_dir"), "state")
    exports_dir, exports_dir_status = _runtime_dir(project_root, paths.get("exports_dir"), "exports")
    stale_after = _bounded_int(processing.get("single_instance_stale_after_seconds"), 86400, 60, 31_536_000)

    result = request_graceful_stop(
        state_dir,
        state_dir / "mediataggerbot.lock",
        stale_after,
    )
    run_id = f"{timestamp_for_filename()}_request_stop_control"
    exports_dir.mkdir(parents=True, exist_ok=True)
    evidence_path = exports_dir / f"graceful_stop_request_{run_id}.json"
    result.update(
        {
            "control_path": "portable_stdlib_no_runtime_setup",
            "app_version": app_version,
            "project_root": str(project_root),
            "config_path": str(config_path),
            "config_read_status": config_status,
            "runtime_path_status": {
                "state_dir": state_dir_status,
                "exports_dir": exports_dir_status,
            },
            "resolved_runtime_dirs": {
                "state_dir": str(state_dir),
                "exports_dir": str(exports_dir),
            },
            "runtime_setup_attempted": False,
            "virtual_environment_modified": False,
            "dependency_install_attempted": False,
            "media_files_read": False,
            "launcher_attestation": build_launcher_attestation(project_root, app_version, env),
            "evidence_created_utc": now_utc().isoformat(),
        }
    )
    write_json_atomic(evidence_path, result)
    return 0, result, evidence_path


def _load_config_best_effort(path: Path) -> tuple[dict[str, Any], str]:
    if not path.exists():
        return {}, "missing_fallback_defaults"
    if tomllib is None:
        return {}, "tomllib_unavailable_fallback_defaults"
    try:
        with path.open("rb") as handle:
            value = tomllib.load(handle)
        return (value if isinstance(value, dict) else {}), "loaded"
    except (OSError, ValueError, TypeError):
        return {}, "invalid_fallback_defaults"


def _runtime_dir(project_root: Path, raw: object, default: str) -> tuple[Path, str]:
    """Resolve a runtime-owned directory without ever escaping the project root.

    This function is deliberately independent of the full configuration loader so
    request-stop remains available when media dependencies or the active config are
    broken.  Absolute, drive-qualified, UNC, and parent-traversal values fail closed
    to the project-local default.
    """
    raw_text = str(raw or "").strip()
    text = raw_text or default
    expanded = os.path.expandvars(os.path.expanduser(text)).strip() or default

    windows = PureWindowsPath(expanded)
    posix = PurePosixPath(expanded)
    normalized_parts = [part for part in expanded.replace("\\", "/").split("/") if part not in {"", "."}]
    unsafe = (
        windows.is_absolute()
        or bool(windows.drive)
        or posix.is_absolute()
        or expanded.startswith(("\\\\", "//"))
        or any(part == ".." for part in normalized_parts)
    )
    if unsafe:
        return (project_root / default).resolve(), "fallback_rejected_absolute_or_traversal"

    candidate = project_root.joinpath(*normalized_parts).resolve()
    try:
        candidate.relative_to(project_root)
    except ValueError:
        return (project_root / default).resolve(), "fallback_resolved_outside_project"

    status = "configured_project_relative" if raw_text else "default_project_relative"
    return candidate, status


def _bounded_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(minimum, min(maximum, parsed))
