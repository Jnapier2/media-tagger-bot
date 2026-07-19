from __future__ import annotations

import json
import os
import platform
import re
import sqlite3
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

try:
    import requests
except Exception:  # pragma: no cover
    requests = None  # type: ignore
try:
    import mutagen
except Exception:  # pragma: no cover
    mutagen = None  # type: ignore

from . import __version__
from .asset_metadata import ASSET_METADATA_SCHEMA, PROJECT_SLUG
from .config import AppConfig, python_version_summary, redacted_effective_config
from .fingerprint import fingerprint_backend_status
from .launcher_attestation import build_launcher_attestation
from .pathing import build_input_assurance, build_path_status, looks_absolute_path
from .project_repair import build_project_drift_status
from .operation_journal import read_operation_journal_summary
from .single_instance import read_lock_status
from .run_control import graceful_stop_status
from .timeutil import local_timestamp, now_utc
from .utils import redact_sensitive_text, sha256_file, which, write_json_atomic

SUPPORT_EXPORT_MAX_FILES = 20
MAX_CANDIDATE_BYTES = 2_000_000
INTEGRATION_REVIEW_DATE = "2026-07-10"


def _safe_bool(value: Any, default: bool) -> bool:
    return value if type(value) is bool else default


def _safe_int(value: Any, default: int, minimum: int | None = None, maximum: int | None = None) -> int:
    parsed = value if type(value) is int else default
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _safe_float(value: Any, default: float) -> float:
    if type(value) not in {int, float}:
        return default
    return float(value)


def redact_text(text: str) -> str:
    return redact_sensitive_text(text)


def build_scan_policy(config: AppConfig) -> dict[str, Any]:
    processing = config.section("processing")
    exclusions = [str(value) for value in processing.get("exclude_dir_names", []) if str(value).strip()]
    return {
        "recursive": _safe_bool(processing.get("recursive"), True),
        "require_recursive_scan": _safe_bool(processing.get("require_recursive_scan"), True),
        "follow_directory_symlinks": _safe_bool(processing.get("follow_directory_symlinks"), False),
        "exclude_dir_names": exclusions,
        "max_files_per_run": _safe_int(processing.get("max_files_per_run"), 0, 0, 10_000_000),
        "coverage_proof_written": _safe_bool(processing.get("write_scan_coverage_reports"), True),
        "fingerprint_cache_enabled": _safe_bool(processing.get("cache_fingerprints"), True),
        "inventory_cache_enabled": _safe_bool(processing.get("cache_media_inventory"), True),
        "inventory_cache_ttl_days": _safe_int(processing.get("inventory_cache_ttl_days"), 3650, 1, 36_500),
        "require_complete_scan_before_apply": _safe_bool(processing.get("require_complete_scan_before_apply"), True),
        "graceful_stop_supported": True,
        "scan_progress_every_directories": _safe_int(processing.get("scan_progress_every_directories"), 100, 1, 100_000),
        "scan_progress_every_files": _safe_int(processing.get("scan_progress_every_files"), 250, 1, 100_000),
        "complete_signal": "all_reachable_subfolders_checked=true",
        "strict_complete_conditions": [
            "recursive traversal enabled",
            "no file limit reached",
            "no directory access errors",
            "no named directory exclusions",
            "no directory symlink/junction entries skipped",
            "visited directory count equals discovered subdirectories plus root",
        ],
    }


def build_environment_summary(config: AppConfig, run_id: str, mode: str) -> dict[str, Any]:
    return {
        "schema": "MediaTaggerBot.environment.v5",
        "app_version": __version__,
        "run_id": run_id,
        "mode": mode,
        "created_utc": now_utc().isoformat(),
        "created_local": local_timestamp(str(config.get("project.timezone", "America/Chicago"))),
        "configured_timezone": str(config.get("project.timezone", "America/Chicago")),
        "python": python_version_summary(),
        "sqlite_runtime": build_sqlite_runtime_status(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "cwd": os.getcwd(),
        "project_root": str(config.project_root),
        "config_path": str(config.config_path),
        "config_load_status": config.load_status,
        "control_runtime_policy": {
            "dependency_free_modes": ["diagnostics", "repair", "set-root", "validate-config", "rollback", "request-stop"],
            "diagnostics_config_bootstrap_allowed": False,
            "diagnostics_dependency_install_allowed": False,
            "diagnostics_media_scan_allowed": False,
            "diagnostics_network_probe_allowed": False,
            "current_mode_eligible": mode in {"diagnostics", "repair", "set-root", "validate-config", "rollback", "request-stop"},
        },
        "launcher_status": {
            "primary_bat": str(config.project_root / "Start_MediaTaggerBot.bat"),
            "primary_bat_exists": (config.project_root / "Start_MediaTaggerBot.bat").exists(),
            "legacy_powershell_launcher_present": (config.project_root / "Launch_MediaTaggerBot.ps1").exists(),
            "execution_policy_dependency": False,
            "local_tool_folders_supported": True,
            "attestation": build_launcher_attestation(config.project_root, __version__),
        },
        "path_status": build_path_status(config),
        "scan_policy": build_scan_policy(config),
        "canonicalization_policy": {
            "enabled": _safe_bool(config.get("canonicalization.enabled"), True),
            "artist_name_policy": config.get("canonicalization.artist_name_policy", "musicbrainz_entity"),
            "unicode_form": config.get("canonicalization.unicode_form", "NFC"),
            "crosscheck_lastfm": _safe_bool(config.get("canonicalization.crosscheck_lastfm"), True),
            "crosscheck_discogs": _safe_bool(config.get("canonicalization.crosscheck_discogs"), True),
            "block_text_match_conflicts_in_apply_safe": _safe_bool(config.get("canonicalization.block_text_match_conflicts_in_apply_safe"), True),
            "overrides_file": config.get("canonicalization.overrides_file", "config/canonical_overrides.toml"),
            "write_consistency_reports": _safe_bool(config.get("canonicalization.write_consistency_reports"), True),
        },
        "asset_metadata_policy": {
            "schema": ASSET_METADATA_SCHEMA,
            "project_slug": PROJECT_SLUG,
            "canonical_release_record": "source repository and Git history",
            "runtime_manifest_pattern": "exports/<run_id>/ASSET_MANIFEST_<run_id>.json|csv",
            "key_asset_headers": True,
            "per_file_sidecars_required": False,
            "media_asset_id_authority": ["MusicBrainz recording ID", "ISRC", "AcoustID"],
            "privacy_policy": "project-relative paths only in asset registries; no secrets",
        },
        "identity_resolution_policy": {
            "identity_memory_enabled": _safe_bool(config.get("matching.identity_memory_enabled"), True),
            "candidate_limit": _safe_int(config.get("matching.text_search_candidate_limit"), 8, 2, 25),
            "apply_safe_min_candidate_margin": _safe_float(config.get("matching.min_text_candidate_margin_apply_safe"), 6.0),
            "block_ambiguous_matches": _safe_bool(config.get("matching.block_ambiguous_text_matches_in_apply_safe"), True),
            "version_aware_matching": True,
            "video_recording_tiebreak": True,
            "musicbrainz_artist_genre_fallback": _safe_bool(config.get("matching.musicbrainz_artist_genre_fallback"), True),
            "acoustic_duplicate_cluster_report": _safe_bool(config.get("matching.report_acoustic_duplicate_clusters"), True),
        },
        "stability_policy": {
            "per_file_failure_isolation": True,
            "operation_journal_enabled": _safe_bool(config.get("processing.operation_journal_enabled"), True),
            "journal_reconciliation_on_apply": _safe_bool(config.get("processing.reconcile_operation_journal_on_apply"), True),
            "source_change_guard": _safe_bool(config.get("processing.verify_source_unchanged_before_apply"), True),
            "metadata_readback_verification": _safe_bool(config.get("processing.verify_metadata_after_write"), True),
            "rename_readback_verification": True,
            "single_instance_heartbeat_seconds": _safe_int(config.get("processing.single_instance_heartbeat_seconds"), 30, 5, 3600),
            "api_connect_timeout_seconds": _safe_int(config.get("processing.api_connect_timeout_seconds"), 10, 1, 3600),
            "api_read_timeout_seconds": _safe_int(config.get("processing.network_timeout_seconds"), 30, 1, 3600),
            "api_retries": _safe_int(config.get("processing.max_retries"), 3, 0, 10),
            "api_retry_after_honored": True,
            "api_metrics_written": _safe_bool(config.get("processing.write_api_metrics"), True),
            "graceful_stop_owner_bound": True,
            "graceful_stop_control_skips_runtime_setup": True,
            "truthful_run_exit_report": True,
            "apply_requires_complete_scan": _safe_bool(config.get("processing.require_complete_scan_before_apply"), True),
            "rollback_path_containment": _safe_bool(config.get("processing.rollback_require_paths_under_media_root"), True),
            "rotating_log_max_bytes": _safe_int(config.get("processing.log_max_bytes"), 10_000_000, 100_000, 1_000_000_000),
            "rotating_log_backup_count": _safe_int(config.get("processing.log_backup_count"), 3, 1, 100),
            "diagnostic_max_total_bytes": _safe_int(config.get("processing.diagnostic_max_total_bytes"), 10_000_000, 1_000_000, 100_000_000),
            "hash_checked_dependency_install": True,
            "inventory_cache_signature_invalidation": True,
            "sqlite_pragma_optimize_on_open_close": True,
            "dry_run_write_readiness_probe": True,
            "full_path_compatibility_budget": _safe_int(config.get("naming.max_full_path_length"), 240, 0, 32760),
        },
        "tools": {
            "fpcalc": which("fpcalc"),
            "ffprobe": which("ffprobe"),
            "ffmpeg": which("ffmpeg"),
            "exiftool": which("exiftool") or which("exiftool.exe"),
            "fingerprint_backend": fingerprint_backend_status(),
        },
        "python_packages": {
            "requests": getattr(requests, "__version__", "missing") if requests else "missing",
            "mutagen": (getattr(mutagen, "version_string", None) or getattr(mutagen, "__version__", "missing")) if mutagen else "missing",
        },
        "unknown_config_keys": config.unknown_keys,
        "warnings": config.warnings,
        "validation_errors": config.validation_errors,
        "config_semantic_status": "pass" if not config.validation_errors else "blocked",
        "project_drift": build_project_drift_status(config.project_root, __version__),
        "input_assurance": build_input_assurance(config),
    }



def build_sqlite_runtime_status() -> dict[str, Any]:
    """Record the embedded SQLite runtime and the bot's concurrency mitigation."""
    return {
        "sqlite_version": sqlite3.sqlite_version,
        "threadsafety": sqlite3.threadsafety,
        "api_cache_journal_mode": "WAL",
        "operation_journal_mode": "WAL",
        "single_writer_enforced_by_process_lock": True,
        "diagnostics_open_databases_read_only": True,
        "wal_concurrent_writer_risk_mitigation": (
            "Each database has one writer because all processing modes share the owner-aware single-instance lock; "
            "diagnostics never write those databases."
        ),
        "pragma_optimize_policy": "0x10002 on writable open; optimize before writable close",
    }

def write_diagnostics_export(
    config: AppConfig,
    run_id: str,
    mode: str,
    log_path: Path | None = None,
    report_paths: dict[str, Path] | None = None,
) -> Path:
    """Build a deterministic, read-only, redacted support bundle.

    A minimal fallback ZIP is produced when any advanced collector or packaging stage
    fails. The fallback never scans media, repairs config, writes API/cache state, or
    performs network calls.
    """
    try:
        return _write_diagnostics_export_primary(config, run_id, mode, log_path, report_paths or {})
    except Exception as exc:
        return _write_diagnostics_export_fallback(config, run_id, mode, log_path, exc)


def _write_diagnostics_export_primary(
    config: AppConfig,
    run_id: str,
    mode: str,
    log_path: Path | None,
    report_paths: dict[str, Path],
) -> Path:
    started = time.monotonic()
    config.diagnostics_dir.mkdir(parents=True, exist_ok=True)
    config.temp_dir.mkdir(parents=True, exist_ok=True)
    final_zip = config.diagnostics_dir / f"MediaTaggerBot_DIAGNOSTIC_{run_id}.zip"
    collector_errors: list[dict[str, str]] = []
    max_total_bytes = max(1_000_000, _safe_int(config.get("processing.diagnostic_max_total_bytes"), 10_000_000, 1_000_000, 100_000_000))
    max_candidate_bytes = min(MAX_CANDIDATE_BYTES, max_total_bytes)

    with tempfile.TemporaryDirectory(prefix="diag_stage_", dir=str(config.temp_dir)) as stage_raw:
        stage = Path(stage_raw)
        environment = build_environment_summary(config, run_id, mode)
        operation_journal_summary = read_operation_journal_summary(
            config.state_dir / "operation_journal.sqlite3"
        )
        lock_status_summary = read_lock_status(
            config.state_dir / "mediataggerbot.lock",
            _safe_int(config.get("processing.single_instance_stale_after_seconds"), 86400, 60, 31_536_000),
        )
        stop_status_summary = graceful_stop_status(config.state_dir)
        generated = {
            "redacted_effective_config.json": redacted_effective_config(config),
            "config_load_status.json": config.load_status,
            "environment_summary.json": environment,
            "api_status_summary.json": build_api_status(config),
            "path_status_summary.json": build_path_status(config),
            "dependency_status_summary.json": build_dependency_status(config),
            "operation_journal_summary.json": operation_journal_summary,
            "lock_status_summary.json": lock_status_summary,
            "graceful_stop_status.json": stop_status_summary,
        }
        for name, payload in generated.items():
            try:
                write_json_atomic(stage / name, sanitize_diagnostic_value(payload, config))
            except Exception as exc:
                collector_errors.append({"collector": name, "error": sanitize_diagnostic_text(str(exc), config)})

        journal_relevant = bool(
            operation_journal_summary.get("exists")
            or operation_journal_summary.get("read_error")
            or operation_journal_summary.get("incomplete_operations")
        )
        lock_relevant = bool(lock_status_summary.get("active") or lock_status_summary.get("stale"))
        stop_relevant = bool(stop_status_summary.get("exists"))
        candidates: list[tuple[int, Path, str, str]] = [
            (6, stage / "config_load_status.json", "config_load_status.json", "config parse/recovery status"),
            (7, stage / "redacted_effective_config.json", "redacted_effective_config.json", "redacted effective config"),
            (8, stage / "environment_summary.json", "environment_summary.json", "environment, tools, and runtime policy"),
            (21, stage / "path_status_summary.json", "path_status_summary.json", "path and relocation status"),
            (17, stage / "dependency_status_summary.json", "dependency_status_summary.json", "dependency and provenance status"),
            (19, stage / "api_status_summary.json", "api_status_summary.json", "API integration registry/status"),
            (16 if journal_relevant else 40, stage / "operation_journal_summary.json", "operation_journal_summary.json", "read-only crash journal summary"),
            (9 if lock_relevant else 41, stage / "lock_status_summary.json", "lock_status_summary.json", "single-instance owner/heartbeat status"),
            (9 if stop_relevant else 42, stage / "graceful_stop_status.json", "graceful_stop_status.json", "owner-bound graceful-stop request status"),
        ]

        # Persisted state is parsed and staged through both path and secret redaction.
        for priority, name, purpose in [
            (9, "last_run_exit.json", "last truthful run-exit classification"),
            (10, "last_run_status.json", "last runtime progress and shutdown reason"),
            (11, "last_scan_coverage.json", "last recursive traversal proof"),
            (18, "last_api_metrics.json", "last API/cache/identity-memory telemetry"),
            (18, "last_inventory_cache_metrics.json", "last scanner inventory-cache telemetry"),
            (20, "last_journal_reconciliation.json", "last crash-journal reconciliation result"),
        ]:
            state_file = config.state_dir / name
            if not state_file.exists():
                continue
            try:
                staged_state = stage / f"state_{name}"
                payload = json_load_file(state_file)
                write_json_atomic(staged_state, sanitize_diagnostic_value(payload, config))
                candidates.append((priority, staged_state, f"state/{name}", purpose))
            except Exception as exc:
                collector_errors.append(
                    {"collector": f"state/{name}", "error": sanitize_diagnostic_text(str(exc), config)}
                )

        doc_priorities = {
            "README.md": 1,
            "CHANGELOG.md": 2,
            "SECURITY.md": 3,
            "LICENSE.md": 4,
            "docs/OPERATIONS.md": 30,
            "docs/API_NOTES.md": 31,
            "docs/VERIFICATION.md": 32,
        }
        for name, priority in doc_priorities.items():
            project_file = config.project_root / Path(name)
            if not project_file.exists() or not project_file.is_file():
                continue
            try:
                staged_doc = stage / f"doc_{safe_arc_component(name)}"
                stage_redacted_text_file(project_file, staged_doc, config, max_candidate_bytes)
                candidates.append((priority, staged_doc, name, "project documentation"))
            except Exception as exc:
                collector_errors.append({"collector": name, "error": sanitize_diagnostic_text(str(exc), config)})

        if log_path and log_path.exists():
            try:
                recent_log = stage / "recent_run_log_tail.txt"
                recent_log.write_text(
                    sanitize_diagnostic_text(tail_text(log_path, max_bytes=200_000), config),
                    encoding="utf-8",
                )
                candidates.append((12, recent_log, "recent_run_log_tail.txt", "redacted recent log tail"))
            except Exception as exc:
                collector_errors.append(
                    {"collector": "recent_run_log_tail", "error": sanitize_diagnostic_text(str(exc), config)}
                )

        report_priorities = {
            "rollback_manifest_json": 8,
            "rollback_result": 8,
            "run_exit_report": 9,
            "summary_json": 13,
            "needs_review_csv": 14,
            "scan_coverage_json": 15,
            "repository_name_conflicts_csv": 22,
            "acoustic_duplicate_clusters_csv": 23,
            "canonical_name_changes_csv": 24,
            "name_variant_clusters_csv": 25,
            "scan_path_errors_csv": 26,
            "duplicate_candidates_csv": 27,
            "scan_coverage_csv": 28,
            "csv_report": 70,
            "jsonl_report": 71,
            "summary_html": 72,
            "asset_manifest_json": 6,
            "asset_manifest_csv": 29,
        }
        for label, report_path in sorted(report_paths.items()):
            if not report_path.exists() or not report_path.is_file():
                continue
            try:
                if report_path.stat().st_size > max_candidate_bytes:
                    candidates.append(
                        (
                            999,
                            report_path,
                            f"reports/{safe_arc_component(label)}_{safe_arc_component(report_path.name)}",
                            "oversized run report",
                        )
                    )
                    continue
                staged_report = stage / f"report_{safe_arc_component(label)}_{safe_arc_component(report_path.name)}"
                stage_redacted_report(report_path, staged_report, config)
                priority = report_priorities.get(label, 60)
                candidates.append(
                    (
                        priority,
                        staged_report,
                        f"reports/{safe_arc_component(label)}_{safe_arc_component(report_path.name)}",
                        "redacted run report",
                    )
                )
            except Exception as exc:
                collector_errors.append(
                    {"collector": f"report/{label}", "error": sanitize_diagnostic_text(str(exc), config)}
                )

        eligible, omitted = filter_candidate_sizes(candidates, max_candidate_bytes)
        selected = select_export_candidates_budgeted(
            eligible,
            max_files=18,
            max_total_bytes=max(100_000, max_total_bytes - 400_000),
        )

        # Build summary/manifest after selection. If their final sizes push the export over
        # budget, drop lowest-priority optional entries until all limits are satisfied.
        while True:
            diag_summary = sanitize_diagnostic_value(
                {
                    "schema": "MediaTaggerBot.diagnostic_summary.v5",
                    "app_version": __version__,
                    "run_id": run_id,
                    "mode": mode,
                    "created_utc": now_utc().isoformat(),
                    "elapsed_seconds_before_zip": round(time.monotonic() - started, 3),
                    "support_export_max_files": SUPPORT_EXPORT_MAX_FILES,
                    "max_candidate_bytes": max_candidate_bytes,
                    "max_total_uncompressed_bytes": max_total_bytes,
                    "selected_optional_file_count": len(selected),
                    "candidate_file_count": len(candidates),
                    "omitted_candidate_count": len(omitted),
                    "collector_errors": collector_errors,
                    "config_load_status": config.load_status,
                    "path_status": build_path_status(config),
                    "scan_policy": build_scan_policy(config),
                    "canonicalization_policy": environment["canonicalization_policy"],
                    "identity_resolution_policy": environment["identity_resolution_policy"],
                    "asset_metadata_policy": environment["asset_metadata_policy"],
                    "stability_policy": environment["stability_policy"],
                    "sqlite_runtime": build_sqlite_runtime_status(),
                    "operation_journal_status": operation_journal_summary,
                    "lock_status": lock_status_summary,
                    "graceful_stop_status": stop_status_summary,
                    "input_assurance": build_input_assurance(config),
                    "dependency_status": build_dependency_status(config),
                    "api_status": build_api_status(config),
                    "redaction": (
                        "Secrets, known project/media roots, user-home paths, and report path fields are staged through redaction."
                    ),
                    "notes": [
                        "Diagnostics is deterministic, allowlist-based, read-only, offline, and failure-isolated.",
                        "No media, API cache, venv, prior diagnostic ZIP, package cache, or project-tree recursion is included.",
                        "No live API probe, docs crawl, install, repair, Drive write, or media mutation is performed.",
                        "Oversized and over-budget candidates are listed in the export manifest.",
                    ],
                },
                config,
            )
            write_json_atomic(stage / "diagnostic_summary.json", diag_summary)
            manifest_entries = selected + [
                (stage / "diagnostic_summary.json", "diagnostic_summary.json", "diagnostic summary")
            ]
            export_manifest = build_export_manifest(
                manifest_entries,
                file_count_in_zip=len(selected) + 2,
                omitted=omitted,
                collector_errors=collector_errors,
                max_total_bytes=max_total_bytes,
            )
            write_json_atomic(
                stage / "diagnostic_export_manifest.json",
                sanitize_diagnostic_value(export_manifest, config),
            )
            final_entries = selected + [
                (stage / "diagnostic_summary.json", "diagnostic_summary.json", "diagnostic summary"),
                (stage / "diagnostic_export_manifest.json", "diagnostic_export_manifest.json", "export manifest"),
            ]
            total_size = sum(source.stat().st_size for source, _arcname, _purpose in final_entries)
            if len(final_entries) <= SUPPORT_EXPORT_MAX_FILES and total_size <= max_total_bytes:
                break
            if not selected:
                raise RuntimeError(
                    f"Required diagnostic summary/manifest exceed configured total budget of {max_total_bytes} bytes."
                )
            dropped = selected.pop()
            omitted.append(
                {
                    "archive_name": dropped[1],
                    "reason": "dropped_to_fit_final_total_budget",
                    "size_bytes": dropped[0].stat().st_size,
                }
            )

        tmp_zip = final_zip.with_suffix(".zip.tmp")
        tmp_zip.unlink(missing_ok=True)
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source, arcname, _purpose in final_entries:
                archive.write(source, arcname=arcname)
        with zipfile.ZipFile(tmp_zip, "r") as archive:
            bad = archive.testzip()
            if bad:
                raise RuntimeError(f"Diagnostics ZIP integrity failure at {bad}")
            infos = archive.infolist()
            if len(infos) > SUPPORT_EXPORT_MAX_FILES:
                raise RuntimeError(f"Diagnostics ZIP exceeded bounded file cap: {len(infos)}")
            uncompressed = sum(info.file_size for info in infos)
            if uncompressed > max_total_bytes:
                raise RuntimeError(
                    f"Diagnostics ZIP exceeded uncompressed-byte cap: {uncompressed} > {max_total_bytes}"
                )
        os.replace(tmp_zip, final_zip)

    write_checksum_sidecar(final_zip)
    return final_zip


def _write_diagnostics_export_fallback(
    config: AppConfig,
    run_id: str,
    mode: str,
    log_path: Path | None,
    primary_error: Exception,
) -> Path:
    config.diagnostics_dir.mkdir(parents=True, exist_ok=True)
    config.temp_dir.mkdir(parents=True, exist_ok=True)
    final_zip = config.diagnostics_dir / f"MediaTaggerBot_DIAGNOSTIC_{run_id}.zip"
    with tempfile.TemporaryDirectory(prefix="diag_fallback_", dir=str(config.temp_dir)) as stage_raw:
        stage = Path(stage_raw)
        failure_payload = sanitize_diagnostic_value(
            {
                "schema": "MediaTaggerBot.diagnostic_fallback.v1",
                "app_version": __version__,
                "run_id": run_id,
                "mode": mode,
                "created_utc": now_utc().isoformat(),
                "status": "minimal_fallback_after_primary_export_failure",
                "primary_error": f"{type(primary_error).__name__}: {primary_error}",
                "config_load_status": config.load_status,
                "network_probe_performed": False,
                "media_files_mutated": False,
                "safest_next_action": "Upload this fallback ZIP and the BAT transcript for the next repair pass.",
            },
            config,
        )
        write_json_atomic(stage / "diagnostic_failure.json", failure_payload)
        entries: list[tuple[Path, str]] = [(stage / "diagnostic_failure.json", "diagnostic_failure.json")]
        if log_path and log_path.exists():
            try:
                log_tail = stage / "recent_run_log_tail.txt"
                log_tail.write_text(
                    sanitize_diagnostic_text(tail_text(log_path, max_bytes=100_000), config),
                    encoding="utf-8",
                )
                entries.append((log_tail, "recent_run_log_tail.txt"))
            except Exception:
                pass
        tmp_zip = final_zip.with_suffix(".zip.tmp")
        tmp_zip.unlink(missing_ok=True)
        with zipfile.ZipFile(tmp_zip, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source, arcname in entries:
                archive.write(source, arcname=arcname)
        with zipfile.ZipFile(tmp_zip, "r") as archive:
            bad = archive.testzip()
            if bad:
                raise RuntimeError(f"Fallback diagnostic ZIP integrity failure at {bad}")
        os.replace(tmp_zip, final_zip)
    write_checksum_sidecar(final_zip)
    return final_zip


def write_checksum_sidecar(path: Path) -> Path:
    checksum = sha256_file(path)
    checksum_tmp = path.with_suffix(path.suffix + ".sha256.txt.tmp")
    checksum_final = path.with_suffix(path.suffix + ".sha256.txt")
    checksum_tmp.write_text(f"{checksum}  {path.name}\n", encoding="utf-8")
    os.replace(checksum_tmp, checksum_final)
    return checksum_final


def stage_redacted_text_file(source: Path, target: Path, config: AppConfig, max_bytes: int) -> None:
    data = source.read_bytes()
    if len(data) > max_bytes:
        raise RuntimeError(f"candidate exceeds per-file diagnostic limit: {len(data)} > {max_bytes}")
    target.write_text(sanitize_diagnostic_text(data.decode("utf-8", errors="replace"), config), encoding="utf-8")


def stage_redacted_report(source: Path, target: Path, config: AppConfig) -> None:
    suffix = source.suffix.casefold()
    if suffix == ".json":
        payload = json_load_file(source)
        write_json_atomic(target, sanitize_diagnostic_value(payload, config))
        return
    if suffix == ".jsonl":
        lines: list[str] = []
        for raw_line in source.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                lines.append(json.dumps(sanitize_diagnostic_value(json.loads(raw_line), config), ensure_ascii=False))
            except (ValueError, TypeError):
                lines.append(sanitize_diagnostic_text(raw_line, config))
        target.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        return
    target.write_text(
        sanitize_diagnostic_text(source.read_text(encoding="utf-8", errors="replace"), config),
        encoding="utf-8",
    )


def filter_candidate_sizes(
    candidates: list[tuple[int, Path, str, str]], max_bytes: int
) -> tuple[list[tuple[int, Path, str, str]], list[dict[str, Any]]]:
    eligible: list[tuple[int, Path, str, str]] = []
    omitted: list[dict[str, Any]] = []
    for priority, source, arcname, purpose in candidates:
        if not source.exists() or not source.is_file():
            continue
        try:
            size = source.stat().st_size
        except OSError as exc:
            omitted.append({"archive_name": arcname, "reason": "stat_failed", "error": redact_text(str(exc))})
            continue
        if size > max_bytes:
            omitted.append(
                {"archive_name": arcname, "reason": "over_per_file_limit", "size_bytes": size, "max_bytes": max_bytes}
            )
            continue
        eligible.append((priority, source, arcname, purpose))
    return eligible, omitted


def select_export_candidates_budgeted(
    candidates: list[tuple[int, Path, str, str]],
    *,
    max_files: int,
    max_total_bytes: int,
) -> list[tuple[Path, str, str]]:
    selected: list[tuple[Path, str, str]] = []
    seen_arc: set[str] = set()
    total = 0
    for _priority, source, arcname, purpose in sorted(candidates, key=lambda item: (item[0], item[2])):
        normalized = arcname.replace("\\", "/")
        if normalized in seen_arc or not source.exists() or not source.is_file():
            continue
        size = source.stat().st_size
        if total + size > max_total_bytes:
            continue
        seen_arc.add(normalized)
        selected.append((source, normalized, purpose))
        total += size
        if len(selected) >= max_files:
            break
    return selected


def build_export_manifest(
    selected: list[tuple[Path, str, str]],
    *,
    file_count_in_zip: int,
    omitted: list[dict[str, Any]],
    collector_errors: list[dict[str, str]],
    max_total_bytes: int,
) -> dict[str, Any]:
    files: list[dict[str, Any]] = []
    for source, arcname, purpose in selected:
        try:
            files.append(
                {
                    "archive_name": arcname,
                    "purpose": purpose,
                    "size_bytes": source.stat().st_size,
                    "sha256": sha256_file(source),
                }
            )
        except Exception as exc:
            files.append({"archive_name": arcname, "purpose": purpose, "error": redact_text(str(exc))})
    return {
        "schema": "MediaTaggerBot.diagnostic_export_manifest.v5",
        "created_utc": now_utc().isoformat(),
        "file_count_in_zip": file_count_in_zip,
        "files_described_excluding_manifest_itself": len(files),
        "manifest_self_entry": "diagnostic_export_manifest.json",
        "max_total_uncompressed_bytes": max_total_bytes,
        "files": files,
        "omitted_candidates": omitted,
        "collector_errors": collector_errors,
    }


def build_dependency_status(config: AppConfig) -> dict[str, Any]:
    root = config.project_root
    lock_path = root / "requirements.lock.txt"
    bat_path = root / "Start_MediaTaggerBot.bat"
    bat_text = bat_path.read_text(encoding="utf-8", errors="replace") if bat_path.exists() else ""
    return {
        "schema": "MediaTaggerBot.dependency_status.v2",
        "created_utc": now_utc().isoformat(),
        "requirements_lock_present": lock_path.exists(),
        "requirements_lock_sha256": sha256_file(lock_path) if lock_path.exists() else "missing",
        "runtime_install_policy": {
            "no_index_flag_present": "--no-index" in bat_text,
            "require_hashes_flag_present": "--require-hashes" in bat_text,
            "network_download_required_for_dependencies": True,
            "supported_runtime": "Windows AMD64 CPython 3.11-3.14",
        },
        "publisher_signature": "not_authenticode_signed",
        "norton_on_exact_final_artifact_test": "not_run_in_linux_build_environment",
        "automatic_security_exclusions": False,
    }


def build_api_status(config: AppConfig) -> dict[str, Any]:
    apis = config.section("apis")
    return {
        "schema": "MediaTaggerBot.api_status.v5",
        "created_utc": now_utc().isoformat(),
        "network_probe_performed": False,
        "integration_review_date": INTEGRATION_REVIEW_DATE,
        "integration_review_basis": "official documentation reviewed during build; diagnostics itself performs no live calls",
        "transport_policy": {
            "separate_connect_and_read_timeouts": True,
            "bounded_exponential_backoff_with_jitter": True,
            "retry_after_honored": True,
            "transient_statuses": [408, 425, 429, 500, 502, 503, 504],
            "per_provider_circuit_breaker": True,
            "telemetry_file": "state/last_api_metrics.json",
        },
        "musicbrainz": {
            "enabled": _safe_bool(apis.get("enable_musicbrainz"), True),
            "official_reference": "https://musicbrainz.org/doc/MusicBrainz_API",
            "auth": "not_required_for_read_lookup",
            "uses": ["recording lookup", "ISRC lookup", "recording search", "canonical artist entity names", "recording titles", "release/genre enrichment"],
            "rate_limit_min_interval_seconds": apis.get("musicbrainz_min_interval_seconds"),
            "status": "configured_static",
        },
        "acoustid": {
            "enabled": _safe_bool(apis.get("enable_acoustid"), True),
            "official_reference": "https://acoustid.org/webservice",
            "api_key": "present" if apis.get("acoustid_client_key") else "missing",
            "uses": ["Chromaprint fingerprint lookup"],
            "rate_limit_min_interval_seconds": apis.get("acoustid_min_interval_seconds"),
            "status": "ready_when_key_and_fingerprint_backend_present",
        },
        "lastfm": {
            "enabled": _safe_bool(apis.get("enable_lastfm"), True),
            "official_reference": "https://www.last.fm/api",
            "api_key": "present" if apis.get("lastfm_api_key") else "missing",
            "uses": ["track.getInfo autocorrected spelling cross-check", "MusicBrainz ID cross-check", "genre/subgenre tag enrichment"],
            "rate_limit_min_interval_seconds": apis.get("lastfm_min_interval_seconds"),
            "status": "optional",
        },
        "discogs": {
            "enabled": _safe_bool(apis.get("enable_discogs"), False),
            "official_reference": "https://www.discogs.com/developers",
            "token": "present" if apis.get("discogs_user_token") else "missing",
            "uses": ["optional bounded track/release spelling cross-check", "release genre/style enrichment"],
            "rate_limit_min_interval_seconds": apis.get("discogs_min_interval_seconds"),
            "status": "optional_disabled_by_default",
        },
    }


def json_load_file(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def sanitize_diagnostic_text(text: str, config: AppConfig) -> str:
    """Redact secrets and private absolute roots from diagnostic text."""
    cleaned = redact_text(str(text))
    prefixes: list[tuple[str, str]] = []
    if config.media_root:
        prefixes.append((str(config.media_root), "<MEDIA_ROOT>"))
    prefixes.append((str(config.project_root), "<PROJECT_ROOT>"))
    try:
        prefixes.append((str(Path.home()), "<USER_HOME>"))
    except Exception:
        pass
    for prefix, marker in sorted(prefixes, key=lambda item: len(item[0]), reverse=True):
        if prefix:
            cleaned = _replace_path_prefix(cleaned, prefix, marker)

    # Catch Windows profile paths produced by third-party tools even when the Python
    # process home differs. Keep the relative suffix for useful debugging.
    cleaned = re.sub(r"(?i)[A-Z]:\\Users\\[^\\/\r\n\"']+", "<USER_HOME>", cleaned)
    cleaned = re.sub(r"(?i)\\\\[^\\/\s]+\\[^\\/\s]+", "<UNC_ROOT>", cleaned)
    return cleaned


def sanitize_diagnostic_value(value: Any, config: AppConfig) -> Any:
    """Redact secrets and replace known absolute roots while preserving structure."""

    def clean(node: Any) -> Any:
        if isinstance(node, dict):
            return {str(key): clean(item) for key, item in node.items()}
        if isinstance(node, list):
            return [clean(item) for item in node]
        if isinstance(node, tuple):
            return [clean(item) for item in node]
        if isinstance(node, str):
            text = sanitize_diagnostic_text(node, config)
            # A standalone absolute path that is not under a known root still should not
            # leave diagnostics with a private full path. Preserve only its basename.
            if "\n" not in text and looks_absolute_path(text) and not text.startswith(("http://", "https://")):
                name = text.rstrip("\\/").replace("\\", "/").split("/")[-1]
                return f"<ABSOLUTE_PATH>/{name}" if name else "<ABSOLUTE_PATH>"
            return text
        return node

    return clean(value)


def _replace_path_prefix(text: str, prefix: str, marker: str) -> str:
    if not prefix:
        return text
    # Windows paths are case-insensitive; str replacement is sufficient for the normal
    # exact-case form and a casefold index handles diagnostics produced with different case.
    direct = text.replace(prefix, marker)
    lowered = direct.casefold()
    needle = prefix.casefold()
    while needle in lowered:
        index = lowered.index(needle)
        direct = direct[:index] + marker + direct[index + len(prefix):]
        lowered = direct.casefold()
    return direct


def tail_text(path: Path, max_bytes: int = 200_000) -> str:
    data = path.read_bytes()
    if len(data) > max_bytes:
        data = data[-max_bytes:]
    return data.decode("utf-8", errors="replace")


def safe_arc_component(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._") or "report"
