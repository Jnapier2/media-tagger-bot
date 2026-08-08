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
        current_markers = {
            f".deps_checked_v{current_version}",
            f".deps_attestation_v{current_version}.json",
        }
        markers = list(venv.glob(".deps_checked_v*")) + list(venv.glob(".deps_attestation_v*.json"))
        for marker in sorted(set(markers), key=lambda value: value.name.casefold()):
            if marker.name not in current_markers:
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
            "Versioned documentation from older releases can be reversibly archived by Repair/check.",
            "Nested project directories are reported only and are never moved automatically.",
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
    manifest = archive_dir / "quarantine_manifest.json"
    result["status"] = "planned"
    result["planned"] = [
        {
            "source": str(source),
            "destination": str(archive_dir / source.name),
            "sha256": sha256_file(source),
        }
        for source in candidates
    ]
    result["manifest"] = str(manifest)
    write_json_atomic(manifest, result)
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
        finally:
            result["status"] = "in_progress"
            write_json_atomic(manifest, result)

    result["status"] = "completed" if not result["errors"] else "completed_with_errors"
    write_json_atomic(manifest, result)
    return result



def archive_stale_release_artifacts(project_root: Path, current_version: str) -> dict[str, Any]:
    """Reversibly archive stale root-level release docs and dependency markers.

    This intentionally excludes directories, media, configuration, current release
    documents, and nested project roots. Every move is checksum-verified and listed
    in a recovery manifest.
    """
    project_root = project_root.resolve()
    candidates: list[Path] = []
    try:
        for child in sorted(project_root.iterdir(), key=lambda value: value.name.casefold()):
            if not child.is_file():
                continue
            match = _VERSIONED_ARTIFACT_RE.match(child.name)
            if match and match.group("version") != current_version:
                candidates.append(child)
    except OSError as exc:
        return {
            "schema": "MediaTaggerBot.stale_release_archive.v1",
            "project_root": str(project_root),
            "current_version": current_version,
            "status": "inspection_failed",
            "moved": [],
            "errors": [{"error": f"{type(exc).__name__}: {exc}"}],
            "media_files_mutated": False,
        }

    venv = project_root / ".venv"
    if venv.is_dir():
        current_markers = {
            f".deps_checked_v{current_version}",
            f".deps_attestation_v{current_version}.json",
        }
        markers = list(venv.glob(".deps_checked_v*")) + list(venv.glob(".deps_attestation_v*.json"))
        for marker in sorted(set(markers), key=lambda value: value.name.casefold()):
            if marker.is_file() and marker.name not in current_markers:
                candidates.append(marker)

    result: dict[str, Any] = {
        "schema": "MediaTaggerBot.stale_release_archive.v1",
        "project_root": str(project_root),
        "current_version": current_version,
        "status": "nothing_to_archive" if not candidates else "completed",
        "moved": [],
        "errors": [],
        "media_files_mutated": False,
    }
    if not candidates:
        return result

    archive_root = project_root / "archive" / "stale_release_artifacts"
    archive_dir = archive_root / timestamp_for_filename()
    suffix = 2
    while archive_dir.exists():
        archive_dir = archive_root / f"{timestamp_for_filename()}_{suffix}"
        suffix += 1
    archive_dir.mkdir(parents=True, exist_ok=False)
    manifest = archive_dir / "archive_manifest.json"
    result["status"] = "planned"
    result["planned"] = []
    for source in candidates:
        relative = source.relative_to(project_root)
        result["planned"].append(
            {
                "source": str(source),
                "destination": str(archive_dir / relative),
                "project_relative_source": relative.as_posix(),
                "sha256": sha256_file(source),
            }
        )
    result["manifest"] = str(manifest)
    write_json_atomic(manifest, result)

    for source in candidates:
        try:
            relative = source.relative_to(project_root)
            destination = archive_dir / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            before_hash = sha256_file(source)
            try:
                source.replace(destination)
            except OSError:
                shutil.move(str(source), str(destination))
            after_hash = sha256_file(destination)
            if before_hash != after_hash:
                try:
                    source.parent.mkdir(parents=True, exist_ok=True)
                    if not source.exists() and destination.exists():
                        destination.replace(source)
                except OSError:
                    pass
                raise RuntimeError("archived artifact checksum mismatch; restoration attempted")
            result["moved"].append(
                {
                    "source": str(source),
                    "destination": str(destination),
                    "project_relative_source": relative.as_posix(),
                    "sha256": after_hash,
                    "restore_instruction": f"Move {destination} back to {source} to restore this exact artifact.",
                }
            )
        except Exception as exc:
            result["errors"].append({"source": str(source), "error": f"{type(exc).__name__}: {exc}"})
        finally:
            result["status"] = "in_progress"
            write_json_atomic(manifest, result)

    result["status"] = "completed" if not result["errors"] else "completed_with_errors"
    write_json_atomic(manifest, result)
    return result
