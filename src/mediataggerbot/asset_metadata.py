from __future__ import annotations

import csv
import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TYPE_CHECKING

from . import __version__
from .timeutil import local_timestamp, now_utc
from .utils import sha256_file, write_json_atomic

if TYPE_CHECKING:  # pragma: no cover
    from .config import AppConfig
    from .models import GenreResult, MatchResult

ASSET_METADATA_SCHEMA = "asset-metadata-v1"
PROJECT_SLUG = "media-tagger-bot"
DEFAULT_SENSITIVITY = "local-private"
RUNTIME_HASH_MAX_BYTES = 64_000_000
RUNTIME_MANIFEST_FIELDS = [
    "asset_id", "path", "title", "purpose", "asset_class", "role", "format",
    "project_slug", "version", "status", "sensitivity", "source_of_truth",
    "tags", "aliases", "lineage", "created_utc", "modified_utc", "size_bytes",
    "sha256", "checksum_status",
]

_PURPOSES: dict[str, str] = {
    "csv_report": "Complete tabular media processing report",
    "jsonl_report": "Complete structured per-media processing report",
    "summary_json": "Machine-readable run summary",
    "summary_html": "Human-readable run summary",
    "scan_coverage_json": "Machine-readable recursive scan coverage proof",
    "scan_coverage_csv": "Tabular recursive scan coverage proof",
    "needs_review_csv": "Exception-only review queue",
    "prior_text_identity_review_csv": "Focused review queue for prior MediaTaggerBot text-search identities",
    "duplicate_candidates_csv": "Stable-recording duplicate candidate evidence",
    "acoustic_duplicate_clusters_csv": "Acoustic fingerprint duplicate cluster evidence",
    "canonical_name_changes_csv": "Canonical spelling change evidence",
    "repository_name_conflicts_csv": "Repository disagreement evidence",
    "name_variant_clusters_csv": "Stable-identity name variant register",
    "rollback_manifest_json": "Machine-readable filename rollback plan",
    "rollback_manifest_csv": "Tabular filename rollback plan",
    "run_exit_report": "Truthful terminal status and work-window exit report",
    "diagnostics_zip": "Redacted bounded support package",
    "diagnostics_sha256": "Diagnostic package checksum",
    "log": "Timestamped run log",
    "bat_transcript": "Full BAT launcher transcript",
    "preflight": "Preflight environment and input-assurance report",
    "repair_report": "Non-destructive path and portability repair report",
    "config_validation": "Configuration validation report",
    "set_root": "Media-root update evidence",
    "request_evidence": "Graceful-stop request evidence",
    "rollback_result": "Rollback validation and execution result",
}


def _slug(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9]+", "-", value.strip()).strip("-").upper()
    return text or "ASSET"


def _project_relative(project_root: Path, path: Path) -> str | None:
    try:
        return path.resolve().relative_to(project_root.resolve()).as_posix()
    except (OSError, ValueError):
        return None


def _class_for(path: Path, role: str) -> str:
    suffix = path.suffix.casefold()
    if role.startswith("diagnostic") or suffix == ".zip":
        return "diagnostic-export"
    if "manifest" in role:
        return "manifest"
    if suffix in {".md", ".txt", ".html"}:
        return "documentation"
    if suffix in {".json", ".jsonl", ".csv"}:
        return "report"
    if suffix in {".log"}:
        return "log"
    return "runtime-artifact"


def _title_for(role: str, path: Path) -> str:
    return role.replace("_", " ").replace("-", " ").strip().title() or path.name


def _record_for_path(
    *,
    project_root: Path,
    path: Path,
    run_id: str,
    mode: str,
    role: str,
    purpose: str | None = None,
    status: str = "current",
    source_of_truth: bool = False,
    sensitivity: str = DEFAULT_SENSITIVITY,
) -> dict[str, Any] | None:
    if not path.exists() or not path.is_file():
        return None
    rel = _project_relative(project_root, path)
    if rel is None:
        return None
    stat = path.stat()
    suffix = path.suffix.lstrip(".").lower() or "none"
    role_slug = _slug(role)
    asset_id = f"MTB-RUN-{_slug(run_id)}-{role_slug}"
    # A role should be unique within a run. Add a compact path suffix only when a
    # generic role would otherwise collide.
    if role in {"runtime_asset", "report", "output"}:
        asset_id += "-" + hashlib.sha256(rel.encode("utf-8")).hexdigest()[:10].upper()
    created = datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat()
    modified = datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat()
    mutable = role in {"log", "bat_transcript"}
    oversized = stat.st_size > RUNTIME_HASH_MAX_BYTES
    checksum = None if mutable or oversized else sha256_file(path)
    checksum_status = (
        "mutable_until_process_or_launcher_exit" if mutable
        else "skipped_over_runtime_hash_budget" if oversized
        else "verified"
    )
    return {
        "asset_id": asset_id,
        "path": rel,
        "title": _title_for(role, path),
        "purpose": purpose or _PURPOSES.get(role, "MediaTaggerBot run artifact"),
        "asset_class": _class_for(path, role),
        "role": role,
        "format": suffix,
        "project_slug": PROJECT_SLUG,
        "version": f"v{__version__}",
        "status": status,
        "sensitivity": sensitivity,
        "source_of_truth": source_of_truth,
        "tags": [PROJECT_SLUG, "runtime-output", mode, role.replace("_", "-")],
        "aliases": [path.name, role.replace("_", " ")],
        "lineage": f"generated by MediaTaggerBot v{__version__} run {run_id}",
        "created_utc": created,
        "modified_utc": modified,
        "size_bytes": stat.st_size,
        "sha256": checksum,
        "checksum_status": checksum_status,
    }


def _role_from_filename(path: Path) -> str:
    name = path.name.casefold()
    prefixes = [
        ("media_tagger_report_", "csv_report" if path.suffix.casefold() == ".csv" else "jsonl_report"),
        ("summary_", "summary_html" if path.suffix.casefold() == ".html" else "summary_json"),
        ("scan_coverage_", "scan_coverage_csv" if path.suffix.casefold() == ".csv" else "scan_coverage_json"),
        ("needs_review_", "needs_review_csv"),
        ("prior_text_identity_review_", "prior_text_identity_review_csv"),
        ("duplicate_recording_candidates_", "duplicate_candidates_csv"),
        ("acoustic_duplicate_clusters_", "acoustic_duplicate_clusters_csv"),
        ("canonical_name_changes_", "canonical_name_changes_csv"),
        ("repository_name_conflicts_", "repository_name_conflicts_csv"),
        ("name_variant_clusters_", "name_variant_clusters_csv"),
        ("rollback_manifest_", "rollback_manifest_csv" if path.suffix.casefold() == ".csv" else "rollback_manifest_json"),
        ("preflight_", "preflight"),
        ("repair_", "repair_report"),
        ("config_validation_", "config_validation"),
        ("set_root_", "set_root"),
        ("graceful_stop_request_", "request_evidence"),
        ("rollback_result_", "rollback_result"),
        ("run_exit_report_", "run_exit_report"),
    ]
    for prefix, role in prefixes:
        if name.startswith(prefix):
            return role
    if name.startswith("run_") and path.suffix.casefold() == ".log":
        return "log"
    if "diagnostic" in name and path.suffix.casefold() == ".zip":
        return "diagnostics_zip"
    if "diagnostic" in name and name.endswith(".sha256.txt"):
        return "diagnostics_sha256"
    return "runtime_asset"


def discover_run_assets(config: "AppConfig", run_id: str) -> dict[str, Path]:
    """Discover only project-owned files attributable to one run.

    This is intentionally bounded and does not recurse through the media root.
    """
    assets: dict[str, Path] = {}
    roots = [config.exports_dir, config.diagnostics_dir, config.logs_dir]
    for root in roots:
        if not root.exists() or not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or run_id not in path.name and run_id not in path.as_posix():
                continue
            role = _role_from_filename(path)
            key = role
            if key in assets:
                key = f"{role}__{hashlib.sha256(path.as_posix().encode('utf-8')).hexdigest()[:8]}"
            assets[key] = path
    batch_log_raw = os.environ.get("MEDIATAGGERBOT_BATCH_LOG", "").strip()
    if batch_log_raw:
        batch_log = Path(batch_log_raw).expanduser()
        if batch_log.exists() and batch_log.is_file() and _project_relative(config.project_root, batch_log) is not None:
            assets.setdefault("bat_transcript", batch_log)

    last_exit = config.state_dir / "last_run_exit.json"
    if last_exit.exists():
        try:
            import json
            payload = json.loads(last_exit.read_text(encoding="utf-8"))
            if payload.get("run_id") == run_id:
                assets.setdefault("run_exit_report", last_exit)
        except Exception:
            pass
    return assets


def write_run_asset_manifest(
    config: "AppConfig",
    run_id: str,
    mode: str,
    *,
    assets: dict[str, Path] | None = None,
    terminal_status: str = "current",
) -> dict[str, Path]:
    """Write the single two-format canonical registry for retained run artifacts."""
    assets = dict(assets or discover_run_assets(config, run_id))
    out_dir = config.exports_dir / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"ASSET_MANIFEST_{run_id}.json"
    csv_path = out_dir / f"ASSET_MANIFEST_{run_id}.csv"
    assets.pop("asset_manifest_json", None)
    assets.pop("asset_manifest_csv", None)

    records: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    for role_key, path in sorted(assets.items(), key=lambda item: (item[0], str(item[1]))):
        role = role_key.split("__", 1)[0]
        record = _record_for_path(
            project_root=config.project_root,
            path=path,
            run_id=run_id,
            mode=mode,
            role=role,
            purpose=_PURPOSES.get(role),
            status=terminal_status,
            source_of_truth=role in {"run_exit_report", "scan_coverage_json"},
        )
        if record is None:
            continue
        if record["asset_id"] in used_ids:
            record["asset_id"] += "-" + hashlib.sha256(record["path"].encode("utf-8")).hexdigest()[:8].upper()
        used_ids.add(record["asset_id"])
        records.append(record)

    generated_utc = now_utc().isoformat()
    manifest_base = {
        "metadata_schema": ASSET_METADATA_SCHEMA,
        "package_asset_id": f"MTB-RUN-{_slug(run_id)}",
        "package": "MediaTaggerBot runtime outputs",
        "project_slug": PROJECT_SLUG,
        "version": f"v{__version__}",
        "status": terminal_status,
        "sensitivity": DEFAULT_SENSITIVITY,
        "source_version": f"v{__version__}",
        "run_id": run_id,
        "mode": mode,
        "generated_utc": generated_utc,
        "generated_local": local_timestamp(str(config.get("project.timezone", "America/Chicago"))),
        "tags": [PROJECT_SLUG, "asset-metadata", "runtime-output", mode],
        "aliases": [f"{run_id} outputs", f"{mode} run artifacts"],
        "path_policy": "project-relative; no media-root or private absolute paths",
        "file_count": len(records) + 2,
    }

    # The two canonical manifest files record one another and themselves without a
    # recursive checksum. This is the one documented case where a checksum is not practical.
    self_records = []
    for role, path in (("asset_manifest_json", json_path), ("asset_manifest_csv", csv_path)):
        self_records.append({
            "asset_id": f"MTB-RUN-{_slug(run_id)}-{_slug(role)}",
            "path": _project_relative(config.project_root, path) or path.name,
            "title": _title_for(role, path),
            "purpose": "Canonical retained-asset registry for this run",
            "asset_class": "manifest",
            "role": role,
            "format": path.suffix.lstrip("."),
            "project_slug": PROJECT_SLUG,
            "version": f"v{__version__}",
            "status": terminal_status,
            "sensitivity": DEFAULT_SENSITIVITY,
            "source_of_truth": True,
            "tags": [PROJECT_SLUG, "asset-metadata", "manifest", mode],
            "aliases": [path.name, "run asset registry"],
            "lineage": f"generated by MediaTaggerBot v{__version__} run {run_id}",
            "created_utc": generated_utc,
            "modified_utc": generated_utc,
            "size_bytes": None,
            "sha256": None,
            "checksum_status": "self_referential_not_practical",
        })

    previous_sizes: tuple[int | None, int | None] = (None, None)
    for _attempt in range(5):
        payload = {**manifest_base, "files": records + self_records}
        write_json_atomic(json_path, payload)
        csv_tmp = csv_path.with_suffix(csv_path.suffix + ".tmp")
        with open(csv_tmp, "w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=RUNTIME_MANIFEST_FIELDS)
            writer.writeheader()
            for record in records + self_records:
                row = dict(record)
                row["tags"] = ";".join(record.get("tags", []))
                row["aliases"] = ";".join(record.get("aliases", []))
                writer.writerow({key: row.get(key) for key in RUNTIME_MANIFEST_FIELDS})
        os.replace(csv_tmp, csv_path)
        sizes = (json_path.stat().st_size, csv_path.stat().st_size)
        if sizes == previous_sizes:
            break
        previous_sizes = sizes
        self_records[0]["size_bytes"] = sizes[0]
        self_records[1]["size_bytes"] = sizes[1]
    return {"asset_manifest_json": json_path, "asset_manifest_csv": csv_path}


def media_asset_metadata(match: "MatchResult", genre: "GenreResult", path: Path) -> dict[str, Any]:
    """Compact stable metadata for the managed media asset itself.

    No expensive whole-file content hash is introduced. Stable repository IDs are
    preferred; unidentified fallback matches intentionally receive no invented ID.
    """
    if match.musicbrainz_recording_id:
        asset_id = f"musicbrainz-recording:{match.musicbrainz_recording_id}"
        lineage = "MusicBrainz recording identity"
    elif match.isrc:
        asset_id = f"isrc:{match.isrc}"
        lineage = "ISRC identity"
    elif match.acoustid_id:
        asset_id = f"acoustid:{match.acoustid_id}"
        lineage = "AcoustID fingerprint association"
    else:
        asset_id = ""
        lineage = f"repository match without stable identifier; source={match.source}"
    tags = [PROJECT_SLUG, "managed-media", genre.main_genre]
    if genre.subgenre:
        tags.append(genre.subgenre)
    tags.append("video" if path.suffix.casefold() in {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".flv"} else "audio")
    return {
        "asset_id": asset_id,
        "asset_status": "current-managed",
        "asset_class": "music-video" if tags[-1] == "video" else "audio-recording",
        "asset_tags": tags,
        "asset_lineage": lineage,
        "metadata_schema": ASSET_METADATA_SCHEMA,
    }
