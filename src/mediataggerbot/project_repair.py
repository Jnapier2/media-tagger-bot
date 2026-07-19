from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from .timeutil import timestamp_for_filename
from .utils import sha256_file, write_json_atomic

LEGACY_LAUNCHER_NAMES = ("Launch_MediaTaggerBot.ps1",)
_VERSIONED_ARTIFACT_RE = re.compile(
    r"^(?:FULL_BATCH_OUTPUT|OMISSION_COVERAGE_LEDGER|MediaTaggerBot)_v(?P<version>\d+\.\d+\.\d+)",
    re.IGNORECASE,
)


def build_project_drift_status(project_root: Path, current_version: str) -> dict[str, Any]:
    """Inspect project-only release drift without changing files."""
    project_root = project_root.resolve()
    legacy = [str(project_root / name) for name in LEGACY_LAUNCHER_NAMES if (project_root / name).exists()]

    stale_versioned_artifacts: list[dict[str, str]] = []
    try:
        children = sorted(project_root.iterdir(), key=lambda p: p.name.casefold())
    except OSError as exc:
        children = []
        inspection_error = f"{type(exc).__name__}: {exc}"
    else:
        inspection_error = ""
    for child in children:
        match = _VERSIONED_ARTIFACT_RE.match(child.name)
        if match and match.group("version") != current_version:
            stale_versioned_artifacts.append(
                {"path": str(child), "declared_version": match.group("version"), "kind": "file" if child.is_file() else "directory"}
            )

    nested_project_roots: list[str] = []
    for child in children:
        if not child.is_dir() or child.name in {".venv", "archive", "config", "diagnostics", "docs", "exports", "logs", "src", "state", "temp", "tests", "tools", "wheels"}:
            continue
        if (child / "Start_MediaTaggerBot.bat").exists():
            nested_project_roots.append(str(child))

    stale_dependency_markers: list[str] = []
    venv = project_root / ".venv"
    if venv.exists():
        for marker in sorted(venv.glob(".deps_checked_v*")):
            if marker.name != f".deps_checked_v{current_version}":
                stale_dependency_markers.append(str(marker))

    findings = len(legacy) + len(stale_versioned_artifacts) + len(nested_project_roots) + len(stale_dependency_markers)
    return {
        "schema": "MediaTaggerBot.project_drift_status.v1",
        "project_root": str(project_root),
        "current_version": current_version,
        "status": "pass" if findings == 0 and not inspection_error else "warning",
        "finding_count": findings,
        "legacy_launchers": legacy,
        "stale_versioned_artifacts": stale_versioned_artifacts,
        "nested_project_roots": nested_project_roots,
        "stale_dependency_markers": stale_dependency_markers,
        "inspection_error": inspection_error,
        "active_launcher": str(project_root / "Start_MediaTaggerBot.bat"),
        "notes": [
            "Only the BAT launcher in the current project root is active.",
            "Versioned documentation from older releases is reported but never moved automatically.",
            "Repair may quarantine only exact known legacy launcher filenames; it never deletes them.",
        ],
    }


def quarantine_legacy_launchers(project_root: Path, current_version: str) -> dict[str, Any]:
    """Reversibly move exact obsolete launcher files out of the active project root."""
    project_root = project_root.resolve()
    candidates = [project_root / name for name in LEGACY_LAUNCHER_NAMES if (project_root / name).exists()]
    result: dict[str, Any] = {
        "schema": "MediaTaggerBot.legacy_launcher_quarantine.v1",
        "project_root": str(project_root),
        "current_version": current_version,
        "status": "nothing_to_quarantine" if not candidates else "completed",
        "moved": [],
        "errors": [],
        "media_files_mutated": False,
    }
    if not candidates:
        return result

    archive_root = project_root / "archive" / "legacy_launchers"
    archive_dir = archive_root / timestamp_for_filename()
    suffix = 2
    while archive_dir.exists():
        archive_dir = archive_root / f"{timestamp_for_filename()}_{suffix}"
        suffix += 1
    archive_dir.mkdir(parents=True, exist_ok=False)
    for source in candidates:
        destination = archive_dir / source.name
        try:
            before_hash = sha256_file(source)
            try:
                source.replace(destination)
            except OSError:
                shutil.move(str(source), str(destination))
            after_hash = sha256_file(destination)
            if before_hash != after_hash:
                # Preserve recoverability even for an unexpected storage/copy fault.
                # The failed quarantine is restored to the active location when possible.
                try:
                    if not source.exists() and destination.exists():
                        destination.replace(source)
                except OSError:
                    pass
                raise RuntimeError("quarantined launcher checksum mismatch; restoration attempted")
            result["moved"].append(
                {
                    "source": str(source),
                    "destination": str(destination),
                    "sha256": after_hash,
                    "restore_instruction": f"Move {destination} back to {source} only if intentionally restoring the legacy launcher.",
                }
            )
        except Exception as exc:  # project-only repair must isolate each file
            result["errors"].append({"source": str(source), "error": f"{type(exc).__name__}: {exc}"})

    result["status"] = "completed" if not result["errors"] else "completed_with_errors"
    manifest = archive_dir / "quarantine_manifest.json"
    write_json_atomic(manifest, result)
    result["manifest"] = str(manifest)
    return result
