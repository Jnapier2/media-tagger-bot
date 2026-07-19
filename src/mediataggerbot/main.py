from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from . import __version__
from .cache import JsonCache
from .apply_readiness import probe_apply_readiness, readiness_blocks_apply
from .asset_metadata import write_run_asset_manifest
from .canonicalization import safe_apply_conflict
from .config import (
    AppConfig,
    copy_example_config_if_missing,
    find_project_root,
    load_config,
    load_config_resilient,
    redacted_effective_config,
)
from .diagnostics import build_environment_summary, write_diagnostics_export
from .fingerprint import fingerprint_backend_available, fingerprint_backend_status, fingerprint_media
from .genre import classify_genre
from .launcher_attestation import build_launcher_attestation
from .logging_setup import setup_logging
from .models import GenreResult, MatchResult, MediaFile, PlanResult, ScanCoverage, dataclass_to_jsonable
from .operation_journal import OperationJournal
from .rename import build_sidecar_path, build_target_path
from .reporting import write_reports
from .project_repair import build_project_drift_status, quarantine_legacy_launchers
from .runtime_state import last_exit_matches_run, write_run_exit_report, write_run_status
from .single_instance import SingleInstanceLock
from .run_control import check_graceful_stop, clear_graceful_stop, request_graceful_stop
from .pathing import (
    build_input_assurance,
    build_path_status,
    clean_user_path,
    update_media_root_in_config,
    write_repair_report,
)
from .timeutil import now_utc, timestamp_for_filename
from .utils import write_json_atomic

LOG = logging.getLogger(__name__)

MODES = ["preflight", "scan-only", "dry-run", "apply-safe", "apply-all", "diagnostics", "rollback", "set-root", "repair", "validate-config", "request-stop"]


# Test/embedding injection hook. Runtime scanner import remains lazy so diagnostics,
# repair, config validation, and portable stop can start without third-party media packages.
scan_media_root: Any | None = None


def _safe_bootstrap_int(config: AppConfig, key: str, default: int, minimum: int, maximum: int) -> int:
    """Read an integer needed before full config validation without coercion."""
    value = config.get(key, default)
    if type(value) is int and minimum <= value <= maximum:
        return value
    return default


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    project_root = find_project_root(Path(__file__).resolve()).resolve()
    prepend_local_tool_paths(project_root)

    requested_mode = args.mode or "dry-run"
    default_config_path = (project_root / "config" / "config.toml").resolve()
    if args.config:
        supplied_config = Path(args.config).expanduser()
        config_path = (supplied_config if supplied_config.is_absolute() else project_root / supplied_config).resolve()
    else:
        config_path = default_config_path

    # Diagnostics and request-stop are control-plane paths: they never create a
    # venv, install packages, repair config, or bootstrap config.toml.  Normal
    # modes may create only the shipped default config, never an arbitrary custom
    # config path.
    if requested_mode not in {"diagnostics", "request-stop"} and not args.config:
        copy_example_config_if_missing(project_root, default_config_path)

    root_override = clean_user_path(args.root or os.environ.get("MEDIATAGGERBOT_ROOT_OVERRIDE", ""))
    rollback_manifest = clean_user_path(
        args.rollback_manifest or os.environ.get("MEDIATAGGERBOT_ROLLBACK_MANIFEST", "")
    )
    config_backup = clean_user_path(
        args.config_backup or os.environ.get("MEDIATAGGERBOT_CONFIG_BACKUP", "")
    )

    # Config loading is deliberately resilient. A narrowly recognized Windows-path
    # quoting error is backed up and repaired. Diagnostics, repair, and request-stop
    # can start with an in-memory fallback even when the TOML is otherwise malformed.
    config = load_config_resilient(project_root=project_root, config_path=config_path, mode=requested_mode)
    if root_override and requested_mode != "set-root":
        config.data.setdefault("paths", {})["media_root"] = root_override
    if args.limit is not None:
        config.data.setdefault("processing", {})["max_files_per_run"] = int(args.limit)
        if args.limit < 0 or args.limit > 10_000_000:
            config.validation_errors.append("--limit must be between 0 and 10000000.")
    config.validation_errors = list(dict.fromkeys(config.validation_errors))
    if config.validation_errors:
        # Never allow invalid config or invalid CLI overrides to redirect project-owned
        # logs/state/diagnostics before preflight can fail closed.
        config.safe_runtime_dirs = True
        config.load_status["safe_runtime_dirs_active"] = True
        config.load_status["semantic_validation"] = "failed"
    mode = args.mode or str(config.get("processing.default_mode", "dry-run"))
    run_id = f"{timestamp_for_filename()}_{mode.replace('-', '_')}"
    log_path = setup_logging(
        config.logs_dir,
        run_id,
        verbose=args.verbose,
        max_bytes=_safe_bootstrap_int(config, "processing.log_max_bytes", 10_000_000, 100_000, 1_000_000_000),
        backup_count=_safe_bootstrap_int(config, "processing.log_backup_count", 3, 1, 100),
    )
    LOG.info("MediaTaggerBot v%s run_id=%s mode=%s project_root=%s", __version__, run_id, mode, config.project_root)
    LOG.info("Config load status: %s", config.load_status.get("status", "unknown"))
    for warning in config.warnings:
        LOG.warning(warning)

    launcher_attestation = build_launcher_attestation(config.project_root, __version__)
    if launcher_attestation.get("launcher_environment_present") and not launcher_attestation.get("safe_to_process"):
        LOG.error("Launcher attestation mismatch: %s", launcher_attestation.get("reasons"))

    lock = SingleInstanceLock(
        config.state_dir / "mediataggerbot.lock",
        stale_after_seconds=_safe_bootstrap_int(
            config, "processing.single_instance_stale_after_seconds", 86400, 60, 31_536_000
        ),
        heartbeat_seconds=_safe_bootstrap_int(
            config, "processing.single_instance_heartbeat_seconds", 30, 5, 3600
        ),
        run_id=run_id,
        mode=mode,
    )
    generic_outputs: dict[str, str | Path] = {"log": log_path}
    try:
        # Every mode that can update config, project files, state, reports, or media
        # acquires the same owner-aware lock before publishing live run status.
        # Diagnostics and portable stop remain lock-free/read-only control paths so
        # they can inspect or stop an active long run without clobbering its state.
        if mode not in {"diagnostics", "request-stop"}:
            lock.acquire()
            write_run_status(config, run_id, mode, "running", "startup", 0, None)

        if (
            launcher_attestation.get("launcher_environment_present")
            and not launcher_attestation.get("safe_to_process")
            and mode not in {"diagnostics", "repair", "preflight", "request-stop"}
        ):
            raise RuntimeError(
                "BAT launcher attestation does not match this package; no config or media work was started. "
                f"Reasons: {', '.join(str(value) for value in launcher_attestation.get('reasons', []))}"
            )
        config_invalid = config.load_status.get("status") == "fallback_invalid_config"
        if config_invalid and mode not in {
            "diagnostics", "set-root", "repair", "validate-config", "preflight", "request-stop"
        }:
            raise RuntimeError(
                "Config TOML is invalid. No media work was started. Use BAT option 8 Set media root, "
                "option 9 Repair/check, or option 6 Diagnostics."
            )
        if config.validation_errors and mode not in {
            "diagnostics", "set-root", "repair", "validate-config", "preflight", "request-stop"
        }:
            raise RuntimeError(
                "Config semantic validation failed before media access: "
                + " | ".join(config.validation_errors)
            )

        # Diagnostics/request-stop are media-read-only. Config validation/set-root
        # may update only config files after creating backups. All other routes own
        # the top-level lock acquired above.
        if mode == "diagnostics":
            diag = write_diagnostics_export(config, run_id, mode, log_path=log_path)
            generic_outputs["diagnostics_zip"] = diag
            print(f"Diagnostics ZIP: {diag}")
            if config_invalid:
                print("Config status: INVALID - diagnostic fallback used; no media was scanned or changed.")
            elif config.load_status.get("status") == "defaults_missing_config_read_only":
                print("Config status: MISSING - in-memory defaults used; diagnostics did not create config.toml.")
            elif config.validation_errors:
                print("Config status: BLOCKED - semantic/type errors were captured; diagnostics did not modify config.toml.")
            exit_code = 0
        elif mode == "request-stop":
            exit_code = run_request_stop(config, run_id, log_path)
        elif mode == "repair":
            exit_code = run_repair(config, run_id, log_path)
        elif mode == "validate-config":
            exit_code = run_validate_config(config, config_backup, run_id, log_path)
        elif mode == "set-root":
            exit_code = run_set_root(config, root_override, run_id, log_path)
        elif mode == "rollback":
            exit_code = run_rollback(config, rollback_manifest, run_id, log_path)
        elif mode == "preflight":
            exit_code = run_preflight(config, run_id, log_path)
        else:
            exit_code = run_processing_mode(config, mode, run_id, log_path, lock=lock)

        if mode in {"set-root", "rollback", "repair", "preflight", "validate-config"} and lock.acquired:
            write_run_status(
                config,
                run_id,
                mode,
                "completed" if exit_code == 0 else "failed",
                "run_complete",
                shutdown_reason="normal_exit" if exit_code == 0 else f"exit_code_{exit_code}",
            )
        if mode not in {"request-stop", "diagnostics"} and not last_exit_matches_run(config, run_id):
            write_run_exit_report(
                config,
                run_id,
                mode,
                exit_code=exit_code,
                terminal_status="completed" if exit_code == 0 else "blocked_or_failed",
                completion_class="completed_verified" if exit_code == 0 else "blocked_or_failed",
                completed_verified=["Selected mode returned normally and the terminal status was persisted."] if exit_code == 0 else [],
                skipped_deferred_blocked=[] if exit_code == 0 else [f"Mode ended with exit code {exit_code}; inspect the transcript and diagnostics."],
                exact_outputs=generic_outputs,
                safest_next_action=(
                    "Continue with the next intended mode."
                    if exit_code == 0
                    else "Review state/last_run_exit.json and the newest diagnostic ZIP before retrying."
                ),
                update_last=lock.acquired,
            )
        asset_paths = _write_asset_manifest_best_effort(
            config, run_id, mode, "completed" if exit_code == 0 else "blocked_or_failed"
        )
        generic_outputs.update(asset_paths)
        if asset_paths:
            print(f"Asset manifest: {asset_paths.get('asset_manifest_json')}")
        return exit_code
    except KeyboardInterrupt:
        LOG.warning("Interrupted by user")
        if lock.acquired:
            write_run_status(config, run_id, mode, "interrupted", "keyboard_interrupt", shutdown_reason="user_interrupt")
        if mode != "diagnostics":
            write_run_exit_report(
                config,
                run_id,
                mode,
                exit_code=130,
                terminal_status="interrupted",
                completion_class="partial_not_fully_verified",
                partial_or_rushed=["The process received KeyboardInterrupt before its normal finalization path completed."],
                actual_timeouts_errors=["KeyboardInterrupt/user interrupt"],
                exact_outputs=generic_outputs,
                safest_next_action="Run diagnostics, then use dry-run or apply mode again after reviewing partial reports and journal state.",
                update_last=lock.acquired,
            )
        _write_asset_manifest_best_effort(config, run_id, mode, "interrupted")
        return 130
    except Exception as exc:
        LOG.exception("Run failed: %s", exc)
        if lock.acquired:
            write_run_status(config, run_id, mode, "failed", "unhandled_exception", shutdown_reason=type(exc).__name__)
        # Do not recursively invoke diagnostics while the diagnostics exporter itself
        # is failing. Other modes still get a compact failure bundle when possible.
        if mode != "diagnostics":
            try:
                diag = write_diagnostics_export(config, run_id, mode, log_path=log_path)
                generic_outputs["diagnostics_zip"] = diag
                LOG.info("Failure diagnostics written: %s", diag)
            except Exception as diag_exc:
                LOG.warning("Diagnostics export after failure also failed: %s", diag_exc)
                generic_outputs["diagnostics_error"] = str(diag_exc)
            write_run_exit_report(
                config,
                run_id,
                mode,
                exit_code=1,
                terminal_status="failed",
                completion_class="failed_before_completion",
                skipped_deferred_blocked=["Remaining work in this mode was not attempted after the unhandled failure."],
                actual_timeouts_errors=[f"{type(exc).__name__}: {exc}"],
                exact_outputs=generic_outputs,
                safest_next_action="Review the newest diagnostic ZIP and transcript; fix the reported blocker before a mutating retry.",
                update_last=lock.acquired,
            )
        _write_asset_manifest_best_effort(config, run_id, mode, "failed")
        return 1
    finally:
        lock.release()


def _write_asset_manifest_best_effort(config: AppConfig, run_id: str, mode: str, terminal_status: str) -> dict[str, Path]:
    """Publish the canonical v2.16.5 run-asset registry without masking the run result."""
    try:
        paths = write_run_asset_manifest(config, run_id, mode, terminal_status=terminal_status)
        LOG.info("Run asset manifest written: %s", paths.get("asset_manifest_json"))
        return paths
    except Exception as exc:
        LOG.warning("Run asset manifest could not be finalized: %s", exc)
        return {}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="MediaTaggerBot local music/video matcher, tagger, and renamer.")
    parser.add_argument("--mode", choices=MODES, default=None, help="Run mode. Defaults to config processing.default_mode.")
    parser.add_argument("--root", default="", help="Media root folder override. Supports spaces.")
    parser.add_argument("--config", default="", help="Config TOML path override.")
    parser.add_argument("--limit", type=int, default=None, help="Max files for this run; overrides config.")
    parser.add_argument("--rollback-manifest", default="", help="Rollback manifest JSON path for rollback mode.")
    parser.add_argument("--config-backup", default="", help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true", help="Verbose console/log output.")
    return parser


def run_request_stop(config: AppConfig, run_id: str, log_path: Path) -> int:
    result = request_graceful_stop(
        config.state_dir,
        config.state_dir / "mediataggerbot.lock",
        int(config.get("processing.single_instance_stale_after_seconds", 86400)),
    )
    out_path = config.exports_dir / f"graceful_stop_request_{run_id}.json"
    write_json_atomic(out_path, result)
    print(result.get("message", "Graceful-stop request status recorded."))
    print(f"Request evidence: {out_path}")
    write_run_exit_report(
        config,
        run_id,
        "request-stop",
        exit_code=0,
        terminal_status="completed",
        completion_class="completed_verified",
        completed_verified=[
            "The request was bound to the currently active lock owner token."
            if result.get("request_written")
            else "No active owner-aware run was found; no stop request was written."
        ],
        exact_outputs={"request_evidence": out_path, "log": log_path},
        safest_next_action=(
            "Allow the active run to finish its current bounded step and write partial reports."
            if result.get("request_written")
            else "Start the intended mode; there is no active run to stop."
        ),
        details={"request_status": result.get("status")},
        update_last=False,
    )
    return 0


def run_set_root(config: AppConfig, new_root: str, run_id: str, log_path: Path) -> int:
    if not new_root or not str(new_root).strip():
        raise RuntimeError('set-root mode requires --root "D:\\Your Media Folder".')
    result = update_media_root_in_config(
        config.config_path,
        str(new_root),
        backup=True,
        example_path=config.project_root / "config" / "config.example.toml",
        allow_rebuild_from_example=True,
    )
    # Reload after write so preflight/path status reflects the saved config.
    updated = load_config(project_root=config.project_root, config_path=config.config_path)
    summary = build_environment_summary(updated, run_id, "set-root")
    summary["set_root_result"] = result
    summary["path_status"] = build_path_status(updated)
    out_path = updated.exports_dir / f"set_root_{run_id}.json"
    write_json_atomic(out_path, summary)
    diag = write_diagnostics_export(updated, run_id, "set-root", log_path=log_path, report_paths={"set_root": out_path})
    print("Media root saved to config.")
    print(f"New media root: {result['new_media_root']}")
    print(f"Config: {result['config_path']}")
    print(f"Backup: {result['backup_path']}")
    if result.get("rebuilt_from_example"):
        print("NOTICE: The prior config had unrelated TOML damage, so a clean example was used; custom settings remain in the backup.")
    print(f"Saved TOML value: {result.get('toml_representation', '')}")
    print(f"Exists: {summary['path_status']['media_root']['exists']} is_dir={summary['path_status']['media_root']['is_dir']}")
    print(f"Set-root report: {out_path}")
    print(f"Diagnostics ZIP: {diag}")
    print(f"Log: {log_path}")
    if updated.validation_errors:
        print("Set-root completed, but the remaining config has semantic errors that block processing:")
        for error in updated.validation_errors:
            print(f"  - {error}")
        return 2
    return 0


def run_repair(config: AppConfig, run_id: str, log_path: Path) -> int:
    cleanup = quarantine_legacy_launchers(config.project_root, __version__)
    drift = build_project_drift_status(config.project_root, __version__)
    report_path = write_repair_report(
        config,
        run_id,
        project_drift=drift,
        cleanup_result=cleanup,
    )
    diag = write_diagnostics_export(config, run_id, "repair", log_path=log_path, report_paths={"repair_report": report_path})
    status = build_path_status(config)
    invalid = config.load_status.get("status") == "fallback_invalid_config"
    semantic_invalid = bool(config.validation_errors)
    print("Repair/path/upgrade cleanup complete.")
    print("No media files were scanned, renamed, deleted, or retagged.")
    print(f"Config load status: {config.load_status.get('status', 'unknown')}")
    if cleanup.get("moved"):
        print(f"Legacy launcher quarantined with checksum evidence: {cleanup.get('manifest', '')}")
    if cleanup.get("errors"):
        print(f"Legacy launcher cleanup had {len(cleanup['errors'])} error(s); inspect the repair report.")
    if invalid:
        print("Config remains invalid after the narrow Windows-path repair attempt.")
        print("Use option 8 Set media root to rebuild from the shipped example while preserving a backup.")
    if semantic_invalid:
        print("Config semantic errors still block processing:")
        for error in config.validation_errors:
            print(f"  - {error}")
    print(f"Project root: {config.project_root}")
    print(f"Media root: {status['media_root']['resolved'] or 'not_set'} status={status['media_root']['status']}")
    assurance = build_input_assurance(config)
    print(f"Portability: {status['portability_check']['status']} - {status['portability_check']['summary']}")
    print(f"Project drift status: {drift['status']} findings={drift['finding_count']}")
    print(f"Input assurance: media_root={assurance['paths.media_root']['status']} mapped={assurance['paths.media_root']['mapped_to_resolved_path'] or 'not_set'}")
    print(f"Repair report: {report_path}")
    print(f"Diagnostics ZIP: {diag}")
    print(f"Log: {log_path}")
    return 2 if invalid or semantic_invalid or cleanup.get("errors") else 0


def run_validate_config(config: AppConfig, backup_raw: str, run_id: str, log_path: Path) -> int:
    """Validate a manual Notepad edit and restore the pre-edit backup if necessary."""
    status = config.load_status.get("status", "unknown")
    report: dict[str, Any] = {
        "schema": "MediaTaggerBot.config_validation.v1",
        "run_id": run_id,
        "config_path": str(config.config_path),
        "load_status": config.load_status,
        "validation_errors": list(config.validation_errors),
        "restored_backup": False,
        "rejected_copy": "",
    }
    if status != "fallback_invalid_config" and not config.validation_errors:
        report_path = config.exports_dir / f"config_validation_{run_id}.json"
        write_json_atomic(report_path, report)
        print(f"Config validation passed: {config.config_path}")
        if status == "loaded_after_auto_path_repair":
            print("The Windows media-root quoting error was repaired automatically after a backup.")
        print(f"Validation report: {report_path}")
        return 0

    backup_text = clean_user_path(backup_raw)
    backup_path = Path(backup_text).expanduser() if backup_text else None
    if not backup_path or not backup_path.exists():
        report["restore_error"] = "No valid pre-edit backup was provided."
        if config.validation_errors:
            report["semantic_errors"] = list(config.validation_errors)
        report_path = config.exports_dir / f"config_validation_{run_id}.json"
        write_json_atomic(report_path, report)
        diag = write_diagnostics_export(config, run_id, "validate-config", log_path=log_path, report_paths={"config_validation": report_path})
        print("Config validation failed. The invalid file was left in place because no valid backup was available.")
        print(f"Validation report: {report_path}")
        print(f"Diagnostics ZIP: {diag}")
        return 2

    try:
        backup_config = load_config(project_root=config.project_root, config_path=backup_path)
        if backup_config.validation_errors:
            raise RuntimeError("Pre-edit backup has semantic config errors: " + " | ".join(backup_config.validation_errors))
    except Exception as exc:
        report["restore_error"] = f"Pre-edit backup is also invalid: {type(exc).__name__}: {exc}"
        report_path = config.exports_dir / f"config_validation_{run_id}.json"
        write_json_atomic(report_path, report)
        print(report["restore_error"])
        return 2

    backups_dir = config.config_path.parent / "backups"
    backups_dir.mkdir(parents=True, exist_ok=True)
    rejected = backups_dir / f"config_rejected_{timestamp_for_filename()}.toml"
    if config.config_path.exists():
        shutil.copy2(config.config_path, rejected)
    restore_tmp = config.config_path.with_suffix(config.config_path.suffix + ".restore.tmp")
    try:
        shutil.copy2(backup_path, restore_tmp)
        os.replace(restore_tmp, config.config_path)
    finally:
        restore_tmp.unlink(missing_ok=True)
    restored = load_config(project_root=config.project_root, config_path=config.config_path)
    if restored.validation_errors:
        raise RuntimeError("Restored config unexpectedly failed semantic validation: " + " | ".join(restored.validation_errors))
    report.update({
        "restored_backup": True,
        "backup_path": str(backup_path),
        "rejected_copy": str(rejected),
        "restored_media_root": str(restored.get("paths.media_root", "")),
    })
    report_path = restored.exports_dir / f"config_validation_{run_id}.json"
    write_json_atomic(report_path, report)
    diag = write_diagnostics_export(restored, run_id, "validate-config", log_path=log_path, report_paths={"config_validation": report_path})
    print("Config validation failed; the pre-edit known-good config was restored.")
    print(f"Rejected edit preserved at: {rejected}")
    print(f"Restored backup: {backup_path}")
    print(f"Validation report: {report_path}")
    print(f"Diagnostics ZIP: {diag}")
    return 2


def run_preflight(config: AppConfig, run_id: str, log_path: Path) -> int:
    summary = build_environment_summary(config, run_id, "preflight")
    media_root = config.media_root
    summary["media_root"] = str(media_root) if media_root else "not_set"
    summary["media_root_exists"] = bool(media_root and media_root.exists())
    summary["path_status"] = build_path_status(config)
    summary["input_assurance"] = build_input_assurance(config)
    summary["api_keys"] = {
        "acoustid_client_key": "present" if config.get("apis.acoustid_client_key") else "missing",
        "lastfm_api_key": "present" if config.get("apis.lastfm_api_key") else "missing",
        "discogs_user_token": "present" if config.get("apis.discogs_user_token") else "missing",
    }
    summary["effective_config_redacted"] = redacted_effective_config(config)
    preflight_path = config.exports_dir / f"preflight_{run_id}.json"
    write_json_atomic(preflight_path, summary)

    print("Preflight complete.")
    print(f"Project root: {config.project_root}")
    print(f"Config: {config.config_path}")
    print(f"Launcher: BAT direct Python | legacy PowerShell present={summary['launcher_status']['legacy_powershell_launcher_present']}")
    launcher_attestation = summary["launcher_status"]["attestation"]
    print(
        "Launcher handshake: "
        f"{launcher_attestation.get('status')} confirmed={launcher_attestation.get('confirmed')}"
    )
    print(f"Media root: {summary['media_root']} exists={summary['media_root_exists']}")
    print(f"Path status: {summary['path_status']['media_root']['status']} | portability={summary['path_status']['portability_check']['status']}")
    relationship_warning = summary["path_status"]["root_relationship"]["warning"]
    if relationship_warning != "none":
        print(f"Root relationship warning: {relationship_warning}")
    print(f"Input assurance: media_root={summary['input_assurance']['paths.media_root']['status']} mapped={summary['input_assurance']['paths.media_root']['mapped_to_resolved_path'] or 'not_set'}")
    policy = summary["scan_policy"]
    print(
        "Recursive scan: "
        f"enabled={policy['recursive']} required={policy['require_recursive_scan']} "
        f"follow_links={policy['follow_directory_symlinks']} exclusions={len(policy['exclude_dir_names'])} "
        f"max_files={policy['max_files_per_run'] or 'unlimited'}"
    )
    print(f"Recursive success signal: {policy['complete_signal']}")
    print(
        "Filename/path budget: "
        f"filename={config.get('naming.max_filename_length', 180)} "
        f"full_path={config.get('naming.max_full_path_length', 240) or 'disabled'}"
    )
    print(
        "Inventory cache: "
        f"enabled={policy.get('inventory_cache_enabled', True)} "
        f"ttl_days={policy.get('inventory_cache_ttl_days', 3650)} "
        f"path={config.state_dir / 'inventory_cache.sqlite3'}"
    )
    print(f"fpcalc: {summary['tools']['fpcalc'] or 'missing'}")
    fingerprint_status = summary["tools"].get("fingerprint_backend", {})
    print(
        "Fingerprint backend: "
        f"{fingerprint_status.get('selected', 'none')} "
        f"(FFmpeg Chromaprint={fingerprint_status.get('ffmpeg_chromaprint', False)})"
    )
    print(f"ffprobe: {summary['tools']['ffprobe'] or 'missing'}")
    print(f"exiftool: {summary['tools']['exiftool'] or 'missing/optional'}")
    print(f"AcoustID key: {summary['api_keys']['acoustid_client_key']}")
    print(f"Last.fm key: {summary['api_keys']['lastfm_api_key']}")
    print(f"Discogs token: {summary['api_keys']['discogs_user_token']}")
    canonical = config.section("canonicalization")
    print(
        "Canonical names: "
        f"enabled={canonical.get('enabled', True)} policy={canonical.get('artist_name_policy', 'musicbrainz_entity')} "
        f"unicode={canonical.get('unicode_form', 'NFC')} lastfm_crosscheck={canonical.get('crosscheck_lastfm', True)} "
        f"discogs_crosscheck={canonical.get('crosscheck_discogs', True)}"
    )
    print(f"Canonical override file: {canonical.get('overrides_file', 'config/canonical_overrides.toml')}")
    print(f"Config load status: {config.load_status.get('status', 'unknown')}")
    print(f"Config semantic validation: {'pass' if not config.validation_errors else 'BLOCKED'}")
    for error in config.validation_errors:
        print(f"  CONFIG ERROR: {error}")
    print(f"Preflight JSON: {preflight_path}")
    print(f"Log: {log_path}")
    if (
        config.load_status.get("status") == "fallback_invalid_config"
        or config.validation_errors
        or not bool(launcher_attestation.get("safe_to_process", True))
    ):
        diag = write_diagnostics_export(config, run_id, "preflight", log_path=log_path, report_paths={"preflight": preflight_path})
        if config.load_status.get("status") == "fallback_invalid_config":
            print("Preflight blocked: config TOML is invalid; safe fallback was used and no media work started.")
        elif config.validation_errors:
            print("Preflight blocked: semantic config validation failed; no media work started.")
        else:
            print(
                "Preflight blocked: BAT launcher version/root/transcript attestation mismatched this package; "
                "extract a clean release folder before processing media."
            )
        print(f"Diagnostics ZIP: {diag}")
        return 2
    return 0


def build_clients(config: AppConfig, cache: JsonCache) -> tuple[Any | None, Any | None, Any | None, Any | None]:
    from .databases import AcoustIDClient, DiscogsClient, LastFmClient, MusicBrainzClient

    apis = config.section("apis")
    processing = config.section("processing")
    user_agent = str(apis.get("user_agent") or f"MediaTaggerBot/{__version__}")
    contact = str(config.get("project.contact", "") or "").strip()
    if contact and "set contact in config" in user_agent:
        user_agent = user_agent.replace("set contact in config", contact)
    timeout = int(processing.get("network_timeout_seconds", 30))
    connect_timeout = int(processing.get("api_connect_timeout_seconds", 10))
    max_retries = int(processing.get("max_retries", 3))
    backoff = float(processing.get("retry_backoff_seconds", 2.0))
    jitter = float(processing.get("retry_jitter_seconds", 0.6))

    acoustid = None
    if bool(apis.get("enable_acoustid", True)):
        acoustid = AcoustIDClient(
            client_key=str(apis.get("acoustid_client_key") or ""),
            cache=cache,
            namespace="acoustid",
            user_agent=user_agent,
            timeout_seconds=timeout,
            min_interval_seconds=float(apis.get("acoustid_min_interval_seconds", 0.40)),
            max_retries=max_retries,
            retry_backoff_seconds=backoff,
            connect_timeout_seconds=connect_timeout,
            retry_jitter_seconds=jitter,
        )
    musicbrainz = None
    if bool(apis.get("enable_musicbrainz", True)):
        musicbrainz = MusicBrainzClient(
            cache=cache,
            namespace="musicbrainz",
            user_agent=user_agent,
            timeout_seconds=timeout,
            min_interval_seconds=float(apis.get("musicbrainz_min_interval_seconds", 1.05)),
            max_retries=max_retries,
            retry_backoff_seconds=backoff,
            connect_timeout_seconds=connect_timeout,
            retry_jitter_seconds=jitter,
        )
    lastfm = None
    if bool(apis.get("enable_lastfm", True)):
        lastfm = LastFmClient(
            api_key=str(apis.get("lastfm_api_key") or ""),
            cache=cache,
            namespace="lastfm",
            user_agent=user_agent,
            timeout_seconds=timeout,
            min_interval_seconds=float(apis.get("lastfm_min_interval_seconds", 0.25)),
            max_retries=max_retries,
            retry_backoff_seconds=backoff,
            connect_timeout_seconds=connect_timeout,
            retry_jitter_seconds=jitter,
        )
    discogs = None
    if bool(apis.get("enable_discogs", False)):
        discogs = DiscogsClient(
            user_token=str(apis.get("discogs_user_token") or ""),
            cache=cache,
            namespace="discogs",
            user_agent=user_agent,
            timeout_seconds=timeout,
            min_interval_seconds=float(apis.get("discogs_min_interval_seconds", 1.10)),
            max_retries=max_retries,
            retry_backoff_seconds=backoff,
            connect_timeout_seconds=connect_timeout,
            retry_jitter_seconds=jitter,
        )
    return acoustid, musicbrainz, lastfm, discogs


def run_processing_mode(
    config: AppConfig,
    mode: str,
    run_id: str,
    log_path: Path,
    lock: SingleInstanceLock | None = None,
) -> int:
    # Heavy/runtime dependencies are imported only after config validation.  This
    # keeps diagnostics/request-stop runnable on base Python without installing or
    # importing requests/mutagen. The module-level hook preserves test/integration
    # injection while normal runtime imports the scanner only here.
    from .matcher import Matcher

    scanner_fn = scan_media_root
    if scanner_fn is None:
        from .scanner import scan_media_root as scanner_fn

    media_root = config.media_root
    if not media_root:
        raise RuntimeError("Media root is not set. Use BAT menu option 8 or pass --root \"D:\\Your Media Folder\".")
    media_root = media_root.resolve()
    if not media_root.exists() or not media_root.is_dir():
        raise RuntimeError(f"Media root does not exist or is not a folder: {media_root}")
    if same_path(media_root, config.project_root):
        raise RuntimeError("Media root points to the bot project folder. Choose the actual music/library root instead.")
    if not bool(config.get("processing.same_folder_output", True)):
        raise RuntimeError("processing.same_folder_output=false is unsupported; this build intentionally renames in the source folder.")

    output_dir = config.exports_dir / run_id
    output_dir.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    owner_token = lock.owner_token if lock else ""

    # Remove only a stale request from an earlier owner. A request matching this run is
    # preserved and observed at the next safe checkpoint.
    if owner_token:
        matched, stop_payload = check_graceful_stop(config.state_dir, owner_token)
        if not matched and stop_payload.get("status") == "stale_owner_mismatch":
            clear_graceful_stop(config.state_dir)

    def stop_requested() -> bool:
        if not owner_token:
            return False
        matched, _payload = check_graceful_stop(config.state_dir, owner_token)
        return matched

    def progress_checkpoint(phase: str, processed: int, total: int | None, relative_path: str) -> None:
        if lock:
            lock.heartbeat()
        LOG.info(
            "Progress phase=%s processed=%s total=%s current=%s",
            phase,
            processed,
            total if total is not None else "unknown",
            relative_path or "<root>",
        )
        write_run_status(
            config,
            run_id,
            mode,
            "running",
            phase,
            processed,
            total,
            extra={"current_relative_path": relative_path},
        )

    report_kwargs = {
        "write_coverage": bool(config.get("processing.write_scan_coverage_reports", True)),
        "write_exception_report": bool(config.get("processing.write_exception_only_report", True)),
        "report_duplicate_candidates": bool(config.get("processing.report_duplicate_recording_candidates", True)),
        "report_acoustic_clusters": bool(config.get("matching.report_acoustic_duplicate_clusters", True)),
        "review_confidence": float(config.get("processing.min_auto_confidence_apply_safe", 90.0)),
        "write_csv": bool(config.get("reports.write_csv", True)),
        "write_jsonl": bool(config.get("reports.write_jsonl", True)),
        "write_html": bool(config.get("reports.write_html_summary", True)),
        "write_consistency_reports": bool(config.get("canonicalization.write_consistency_reports", True)),
    }

    def finalize(
        plans: list[PlanResult],
        scan_coverage: ScanCoverage,
        *,
        exit_code: int,
        terminal_status: str,
        completion_class: str,
        completed_verified: list[str] | None = None,
        completed_not_fully_verified: list[str] | None = None,
        partial_or_rushed: list[str] | None = None,
        skipped_deferred_blocked: list[str] | None = None,
        actual_timeouts_errors: list[str] | None = None,
        safest_next_action: str,
        shutdown_reason: str,
    ) -> int:
        reports = write_reports(plans, output_dir, run_id, mode, scan_coverage=scan_coverage, **report_kwargs)
        exit_details = {
            "processed_plans": len(plans),
            "discovered_media_files": scan_coverage.media_files_found,
            "scan_coverage": compact_scan_coverage(scan_coverage),
            "elapsed_seconds": round(time.monotonic() - started, 3),
        }
        # Persist the truthful exit classification before diagnostics so the support bundle captures
        # the current run rather than only the preceding run. Rewrite it once after the
        # diagnostic path is known.
        exact_outputs: dict[str, str | Path] = {**reports, "log": log_path}
        exit_report = write_run_exit_report(
            config,
            run_id,
            mode,
            exit_code=exit_code,
            terminal_status=terminal_status,
            completion_class=completion_class,
            completed_verified=completed_verified,
            completed_not_fully_verified=completed_not_fully_verified,
            partial_or_rushed=partial_or_rushed,
            skipped_deferred_blocked=skipped_deferred_blocked,
            actual_timeouts_errors=actual_timeouts_errors,
            exact_outputs=exact_outputs,
            safest_next_action=safest_next_action,
            details=exit_details,
        )
        reports_with_exit = {**reports, "run_exit_report": exit_report}
        diag = write_diagnostics_export(config, run_id, mode, log_path=log_path, report_paths=reports_with_exit)
        exact_outputs.update({"diagnostics_zip": diag, "run_exit_report": exit_report})
        exit_report = write_run_exit_report(
            config,
            run_id,
            mode,
            exit_code=exit_code,
            terminal_status=terminal_status,
            completion_class=completion_class,
            completed_verified=completed_verified,
            completed_not_fully_verified=completed_not_fully_verified,
            partial_or_rushed=partial_or_rushed,
            skipped_deferred_blocked=skipped_deferred_blocked,
            actual_timeouts_errors=actual_timeouts_errors,
            exact_outputs=exact_outputs,
            safest_next_action=safest_next_action,
            details=exit_details,
        )
        write_run_status(
            config,
            run_id,
            mode,
            terminal_status,
            "reports_diagnostics_and_exit_report_complete",
            len(plans),
            scan_coverage.media_files_scanned,
            shutdown_reason=shutdown_reason,
            extra={"scan_coverage": compact_scan_coverage(scan_coverage), "exit_report": str(exit_report)},
        )
        if owner_token:
            clear_graceful_stop(config.state_dir, owner_token)
        if exit_code == 0:
            print_success(run_id, mode, reports, diag, len(plans), time.monotonic() - started, scan_coverage)
        else:
            print(f"MediaTaggerBot ended with status: {terminal_status}")
            print(f"Run ID: {run_id}")
            print(f"Exit code: {exit_code}")
            print(f"Plans finalized: {len(plans)}")
            print(f"Recursive coverage: {scan_coverage.status}")
            print(f"All reachable subfolders checked: {scan_coverage.all_reachable_subfolders_checked}")
            print(f"Run exit report: {exit_report}")
            print(f"Diagnostics ZIP: {diag}")
        return exit_code

    LOG.info("Scanning media root recursively: %s", media_root)
    write_run_status(
        config,
        run_id,
        mode,
        "running",
        "recursive_scan_started",
        0,
        None,
        extra={"media_root": str(media_root)},
    )
    if lock:
        lock.heartbeat(force=True)
    inventory_cache: JsonCache | None = None
    inventory_cache_metrics: dict[str, Any] = {
        "schema": "MediaTaggerBot.inventory_cache_metrics.v1",
        "run_id": run_id,
        "enabled": bool(config.get("processing.cache_media_inventory", True)),
        "status": "disabled",
    }
    if inventory_cache_metrics["enabled"]:
        try:
            inventory_cache = JsonCache(
                config.state_dir / "inventory_cache.sqlite3",
                ttl_days=int(config.get("processing.inventory_cache_ttl_days", 3650)),
                auto_recover=True,
            )
            inventory_cache_metrics["status"] = "ready"
        except Exception as exc:
            # Inventory caching is an optimization only.  A cache problem never blocks
            # traversal or matching; the scanner falls back to direct metadata reads.
            inventory_cache_metrics.update(
                {
                    "status": "unavailable_fallback_to_direct_scan",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            LOG.warning("Inventory cache unavailable; continuing with direct scan: %s", exc)
    try:
        media_files, scan_coverage = scanner_fn(
            media_root,
            config,
            progress_callback=progress_checkpoint,
            stop_check=stop_requested,
            inventory_cache=inventory_cache,
        )
    finally:
        if inventory_cache is not None:
            inventory_cache_metrics.update(inventory_cache.snapshot())
            inventory_cache.close()
        inventory_cache_metrics["created_utc"] = now_utc().isoformat()
        try:
            write_json_atomic(config.state_dir / "last_inventory_cache_metrics.json", inventory_cache_metrics)
        except OSError as exc:
            LOG.warning("Inventory-cache telemetry could not be written; continuing: %s", exc)
    if lock:
        lock.heartbeat(force=True)
    write_json_atomic(config.state_dir / "last_scan_coverage.json", dataclass_to_jsonable(scan_coverage))
    LOG.info(
        "Recursive scan status=%s all_subfolders=%s directories=%s media=%s depth=%s",
        scan_coverage.status,
        scan_coverage.all_reachable_subfolders_checked,
        scan_coverage.directories_visited,
        len(media_files),
        scan_coverage.deepest_relative_depth,
    )
    write_run_status(
        config,
        run_id,
        mode,
        "running",
        "recursive_scan_complete",
        0,
        len(media_files),
        extra={"scan_coverage": compact_scan_coverage(scan_coverage)},
    )

    inventory_plans = [
        PlanResult(
            media=item,
            match=MatchResult(
                matched=False,
                confidence=0.0,
                source="scan_only" if mode == "scan-only" else "scan_coverage_gate",
                ambiguity_status="not_applicable",
                identity_tier="inventory",
            ),
            genre=None,
            proposed_path=None,
            proposed_filename=None,
            action="inventory_only",
            should_apply=False,
            status="scanned" if not item.scan_error else "scan_error",
        )
        for item in media_files
    ]

    if scan_coverage.graceful_stop_requested:
        return finalize(
            inventory_plans,
            scan_coverage,
            exit_code=75,
            terminal_status="graceful_stop_partial",
            completion_class="partial_not_fully_verified",
            completed_verified=["Partial traversal evidence and reports were written without media mutation."],
            partial_or_rushed=[
                f"The active run honored a graceful-stop request during {scan_coverage.stopped_phase or 'scanning'}."
            ],
            skipped_deferred_blocked=["Remaining directories/files were not processed after the stop checkpoint."],
            safest_next_action="Review the partial scan coverage and run-exit report, then rerun the same mode when ready.",
            shutdown_reason="graceful_stop_requested",
        )

    if mode == "scan-only":
        return finalize(
            inventory_plans,
            scan_coverage,
            exit_code=0,
            terminal_status="completed",
            completion_class="completed_verified",
            completed_verified=[
                "Recursive traversal completed and a coverage proof was written.",
                "Inventory reports and bounded support diagnostics were integrity-tested and finalized.",
            ],
            safest_next_action="Review the coverage status; continue to dry-run only when the intended subfolders were covered.",
            shutdown_reason="normal_exit",
        )

    if (
        mode in {"apply-safe", "apply-all"}
        and bool(config.get("processing.require_complete_scan_before_apply", True))
        and not scan_coverage.all_reachable_subfolders_checked
    ):
        for plan in inventory_plans:
            plan.status = "apply_blocked_incomplete_scan"
            plan.action = "blocked_incomplete_recursive_coverage"
            plan.match.apply_blockers.append("incomplete_recursive_coverage")
            plan.match.notes.append("No media was mutated because recursive coverage was incomplete.")
        return finalize(
            inventory_plans,
            scan_coverage,
            exit_code=3,
            terminal_status="blocked",
            completion_class="blocked_before_mutation",
            completed_verified=["Traversal evidence and a blocking report were written; no media mutation was attempted."],
            skipped_deferred_blocked=[
                f"{mode} was blocked because scan coverage status was {scan_coverage.status!r}."
            ],
            safest_next_action=(
                "Resolve file limits, directory access errors, exclusions, or skipped junctions, then rerun scan-only before apply."
            ),
            shutdown_reason="incomplete_recursive_coverage",
        )

    cache_path = config.state_dir / "api_cache.sqlite3"
    journal: OperationJournal | None = None
    if mode in {"apply-safe", "apply-all"} and bool(config.get("processing.operation_journal_enabled", True)):
        journal = OperationJournal(config.state_dir / "operation_journal.sqlite3", run_id)
        if bool(config.get("processing.reconcile_operation_journal_on_apply", True)):
            reconciliation = journal.reconcile_prior_incomplete()
            write_json_atomic(config.state_dir / "last_journal_reconciliation.json", reconciliation)
            if reconciliation.get("checked"):
                LOG.info(
                    "Reconciled %s crash-left operation(s): completed=%s retryable=%s conflict=%s missing=%s",
                    reconciliation.get("checked"),
                    reconciliation.get("completed_after_crash"),
                    reconciliation.get("retryable"),
                    reconciliation.get("conflict"),
                    reconciliation.get("missing"),
                )

    plans: list[PlanResult] = []
    reserved_targets: set[str] = set()
    api_metrics_written = False
    stop_during_processing = False
    keyboard_interrupt_during_processing = False
    clients: tuple[Any | None, Any | None, Any | None, Any | None] = (
        None, None, None, None
    )
    matcher: Any | None = None
    try:
        with JsonCache(
            cache_path,
            ttl_days=int(config.get("apis.cache_ttl_days", 365)),
            auto_recover=bool(config.get("processing.api_cache_auto_recover", True)),
        ) as cache:
            clients = build_clients(config, cache)
            matcher = Matcher(config, *clients, cache=cache)
            fingerprint_status = fingerprint_backend_status()
            do_fingerprint = (
                bool(config.get("apis.enable_acoustid", True))
                and bool(config.get("apis.acoustid_client_key"))
                and fingerprint_backend_available()
            )
            if (
                bool(config.get("apis.enable_acoustid", True))
                and bool(config.get("apis.acoustid_client_key"))
                and not fingerprint_backend_available()
            ):
                LOG.warning(
                    "AcoustID is configured but no fingerprint backend is available; "
                    "falling back to identifiers, tags, and filename searches."
                )
            elif do_fingerprint:
                LOG.info("Fingerprint backend selected: %s", fingerprint_status.get("selected"))

            try:
                progress_every = max(1, int(config.get("processing.progress_log_every_files", 25) or 25))
                for idx, media in enumerate(media_files, start=1):
                    if stop_requested():
                        stop_during_processing = True
                        LOG.warning("Graceful stop observed before file %s/%s; finalizing partial reports.", idx, len(media_files))
                        break
                    if lock:
                        lock.heartbeat()
                    if idx == 1 or idx % progress_every == 0:
                        LOG.info("Processing %s/%s: %s", idx, len(media_files), media.rel_path)
                        write_run_status(
                            config,
                            run_id,
                            mode,
                            "running",
                            "matching_and_planning",
                            idx - 1,
                            len(media_files),
                            extra={"current_relative_path": media.rel_path},
                        )

                    try:
                        if media.scan_error:
                            plans.append(
                                PlanResult(
                                    media=media,
                                    match=MatchResult(
                                        matched=False,
                                        confidence=0.0,
                                        source="scan_error",
                                        notes=[
                                            "Metadata/duration inventory failed; this file was isolated and no repository lookup or mutation was attempted."
                                        ],
                                        ambiguity_status="not_evaluated",
                                        identity_tier="error",
                                        apply_blockers=["media_scan_error"],
                                    ),
                                    genre=None,
                                    proposed_path=None,
                                    proposed_filename=None,
                                    action="scan_error_review_only",
                                    should_apply=False,
                                    status="scan_error",
                                    error=media.scan_error,
                                )
                            )
                            continue

                        managed_plan = build_already_managed_plan(media, config)
                        if managed_plan is not None:
                            plans.append(managed_plan)
                            reserved_targets.add(_path_reservation_key(media.path))
                            continue

                        has_embedded_identifier = bool(
                            media.existing_musicbrainz_recording_id or media.existing_isrc
                        )
                        if do_fingerprint and not (
                            has_embedded_identifier
                            and bool(config.get("processing.prefer_existing_identifier_shortcuts", True))
                        ):
                            fingerprint_media(
                                media,
                                int(config.get("processing.fingerprint_timeout_seconds", 120)),
                                cache=cache,
                                use_cache=bool(config.get("processing.cache_fingerprints", True)),
                            )

                        match = matcher.match(media)
                        if bool(config.get("genres.prefer_musicbrainz_genres", True)):
                            raw_genre_terms = list(match.raw_genres) + list(match.raw_tags)
                        else:
                            raw_genre_terms = list(match.raw_tags) + list(match.raw_genres)
                        if media.existing_genre:
                            raw_genre_terms.append(media.existing_genre)
                        if media.existing_subgenre:
                            raw_genre_terms.append(media.existing_subgenre)
                        genre = classify_genre(raw_genre_terms, config) if match.matched else None
                        proposed_path = (
                            build_target_path(
                                media.path,
                                match,
                                genre,
                                config,
                                reserved_paths=reserved_targets,
                            )
                            if match.matched and genre
                            else None
                        )
                        # Dry-run doubles as a non-mutating apply-readiness preview for
                        # candidates that would otherwise qualify for apply-safe. Apply-safe
                        # repeats the probe immediately before mutation because locks can change.
                        hypothetical_safe, _hypothetical_action = decide_apply("apply-safe", match, config, genre)
                        if proposed_path is not None and (mode == "apply-safe" or (mode == "dry-run" and hypothetical_safe)):
                            readiness = probe_apply_readiness(media.path, proposed_path, config)
                            match.evidence["write_readiness"] = readiness
                            if readiness_blocks_apply(readiness):
                                if "write_readiness_blocked" not in match.apply_blockers:
                                    match.apply_blockers.append("write_readiness_blocked")
                                match.notes.append(
                                    "Apply readiness probe blocked mutation: "
                                    + str(readiness.get("status") or "unknown")
                                )
                        should_apply, action = decide_apply(mode, match, config, genre)
                        sidecar_path = build_sidecar_path(proposed_path, config) if proposed_path else None
                        plan = PlanResult(
                            media=media,
                            match=match,
                            genre=genre,
                            proposed_path=proposed_path,
                            proposed_filename=proposed_path.name if proposed_path else None,
                            action=action,
                            should_apply=should_apply,
                            sidecar_path=sidecar_path,
                            status="planned" if not should_apply else "pending_apply",
                        )
                        if should_apply:
                            apply_plan(plan, config, run_id, mode=mode, journal=journal)
                        elif not match.matched:
                            plan.status = "unmatched"
                        elif mode == "dry-run":
                            plan.status = "dry_run"
                        else:
                            plan.status = "reported_only"
                        plans.append(plan)
                    except KeyboardInterrupt:
                        raise
                    except Exception as exc:
                        LOG.exception("File processing failed and was isolated for %s: %s", media.path, exc)
                        plans.append(
                            PlanResult(
                                media=media,
                                match=MatchResult(
                                    matched=False,
                                    confidence=0.0,
                                    source="processing_exception",
                                    notes=["Per-file failure was isolated; remaining media continued."],
                                    ambiguity_status="not_evaluated",
                                    identity_tier="error",
                                    apply_blockers=["file_processing_exception"],
                                ),
                                genre=None,
                                proposed_path=None,
                                proposed_filename=None,
                                action="file_processing_failed_report_only",
                                should_apply=False,
                                status="processing_failed",
                                error=str(exc),
                            )
                        )
            finally:
                if bool(config.get("processing.write_api_metrics", True)) and matcher is not None:
                    write_api_metrics(config, run_id, clients, cache, matcher)
                    api_metrics_written = True
    except KeyboardInterrupt:
        keyboard_interrupt_during_processing = True
        LOG.warning(
            "Processing interrupted after %s/%s terminal plans; entering controlled partial finalization.",
            len(plans),
            len(media_files),
        )
    finally:
        if journal is not None:
            journal.close()
        if lock:
            lock.heartbeat(force=True)
        if not api_metrics_written and bool(config.get("processing.write_api_metrics", True)):
            fallback = {
                "schema": "MediaTaggerBot.api_metrics.v1",
                "run_id": run_id,
                "created_utc": now_utc().isoformat(),
                "status": "not_available_before_client_initialization",
            }
            write_json_atomic(config.state_dir / "last_api_metrics.json", fallback)

    if keyboard_interrupt_during_processing:
        return finalize(
            plans,
            scan_coverage,
            exit_code=130,
            terminal_status="interrupted_partial",
            completion_class="partial_not_fully_verified",
            completed_verified=[
                "Completed per-file operations remained journaled and partial reports/diagnostics were finalized."
            ],
            partial_or_rushed=[
                "The process received KeyboardInterrupt during matching/apply; files without a terminal plan were not processed."
            ],
            skipped_deferred_blocked=[f"Up to {max(0, len(media_files) - len(plans))} file(s) did not reach a terminal plan."],
            actual_timeouts_errors=["KeyboardInterrupt/user interrupt"],
            safest_next_action=(
                "Review the operation journal and partial exception reports. Use menu option 14 for future long-run stops, then rerun the same mode."
            ),
            shutdown_reason="keyboard_interrupt_controlled_finalization",
        )

    if stop_during_processing:
        return finalize(
            plans,
            scan_coverage,
            exit_code=75,
            terminal_status="graceful_stop_partial",
            completion_class="partial_not_fully_verified",
            completed_verified=[
                "All completed per-file operations were journaled and partial reports/diagnostics were finalized."
            ],
            partial_or_rushed=["The active run honored a graceful-stop request between bounded per-file operations."],
            skipped_deferred_blocked=[f"{len(media_files) - len(plans)} remaining file(s) were not processed."],
            safest_next_action="Review the partial run-exit report and operation journal, then rerun the same mode to continue.",
            shutdown_reason="graceful_stop_requested",
        )

    hard_failure_statuses = {
        "scan_error",
        "processing_failed",
        "apply_failed",
        "metadata_verification_failed",
        "embedded_metadata_write_failed",
        "source_changed_skipped",
        "write_readiness_blocked",
    }
    status_counts = {
        status: sum(1 for plan in plans if plan.status == status)
        for status in sorted({plan.status for plan in plans})
    }
    hard_failures = sum(status_counts.get(status, 0) for status in hard_failure_statuses)
    warnings_count = status_counts.get("applied_with_warning", 0)
    if hard_failures:
        exit_code = 2
        terminal_status = "completed_with_errors"
        completion_class = "partial_not_fully_verified"
        completed_verified = [
            "All discovered media files reached a terminal plan status.",
            "Reports, diagnostics, API metrics, and run-exit evidence were finalized.",
        ]
        completed_not_fully_verified = [
            f"{hard_failures} file(s) ended in a hard failure/skip status; successful files remain journaled and verified.",
            "Result accuracy depends on metadata-provider responses available during this run.",
        ]
        actual_errors = [
            "Per-file terminal failures: "
            + ", ".join(
                f"{status}={status_counts.get(status, 0)}"
                for status in sorted(hard_failure_statuses)
                if status_counts.get(status, 0)
            )
        ]
        safest_next = "Review needs_review, the operation journal, and exact error statuses before another mutating batch."
        shutdown_reason = "completed_with_per_file_errors"
    elif warnings_count:
        exit_code = 0
        terminal_status = "completed_with_warnings"
        completion_class = "completed_not_fully_verified"
        completed_verified = [
            "All discovered media files reached a terminal plan status.",
            "Reports, diagnostics, API metrics, and run-exit evidence were finalized.",
        ]
        completed_not_fully_verified = [
            f"{warnings_count} applied file(s) completed with warnings.",
            "Result accuracy depends on metadata-provider responses available during this run.",
        ]
        actual_errors = []
        safest_next = "Review applied_with_warning rows and archive the rollback manifest before another mutating batch."
        shutdown_reason = "normal_exit_with_warnings"
    else:
        exit_code = 0
        terminal_status = "completed"
        completion_class = "completed_verified"
        completed_verified = [
            "All discovered media files reached a terminal plan status.",
            "Reports, diagnostics, API metrics, and run-exit evidence were finalized.",
        ]
        completed_not_fully_verified = (
            ["Result accuracy depends on metadata-provider responses available during this run."]
            if mode in {"dry-run", "apply-safe", "apply-all"}
            else []
        )
        actual_errors = []
        safest_next = (
            "Review needs_review and repository-conflict reports before apply-safe."
            if mode == "dry-run"
            else "Archive the rollback manifest and review exception reports before another mutating batch."
        )
        shutdown_reason = "normal_exit"

    return finalize(
        plans,
        scan_coverage,
        exit_code=exit_code,
        terminal_status=terminal_status,
        completion_class=completion_class,
        completed_verified=completed_verified,
        completed_not_fully_verified=completed_not_fully_verified,
        actual_timeouts_errors=actual_errors,
        safest_next_action=safest_next,
        shutdown_reason=shutdown_reason,
    )


def decide_apply(
    mode: str,
    match: MatchResult,
    config: AppConfig,
    genre: GenreResult | None = None,
) -> tuple[bool, str]:
    if mode == "dry-run":
        return False, "dry_run_report_only"
    if not match.matched:
        return False, "unmatched_report_only"
    if mode == "apply-safe":
        threshold = float(config.get("processing.min_auto_confidence_apply_safe", 90.0))
        if safe_apply_conflict(match, config):
            return False, "repository_conflict_review_only"
        blockers = set(match.apply_blockers)
        # A genuinely independent repository agreement may satisfy only the
        # text-corroboration blocker; every other explicit blocker remains fail-closed.
        if "text_match_lacks_independent_corroboration" in blockers and match.repository_agreement:
            blockers.remove("text_match_lacks_independent_corroboration")
        if blockers:
            if any(item.startswith("ambiguous_") for item in blockers):
                return False, "ambiguous_identity_review_only"
            if "prior_mediataggerbot_text_identity_requires_review" in blockers:
                return False, "prior_text_identity_review_only"
            return False, "identity_safety_blocker_review_only"
        if (
            genre is not None
            and bool(config.get("processing.block_fallback_genre_in_apply_safe", True))
            and genre.source == "fallback_main_genre"
        ):
            return False, "genre_evidence_missing_review_only"
        if match.confidence >= threshold and match.source not in {
            "filename_parse",
            "existing_tags",
            "existing_mediataggerbot_text_tags",
        }:
            return True, "apply_safe"
        return False, "below_apply_safe_threshold"
    if mode == "apply-all":
        threshold = float(config.get("processing.min_auto_confidence_apply_all", 55.0))
        if match.confidence >= threshold:
            return True, "apply_all"
        if bool(config.get("processing.allow_filename_only_matches_in_apply_all", True)) and match.source == "filename_parse":
            return True, "apply_all_filename_only"
        return False, "below_apply_all_threshold"
    return False, "unsupported_mode_report_only"


def apply_plan(
    plan: PlanResult,
    config: AppConfig,
    run_id: str,
    *,
    mode: str,
    journal: OperationJournal | None = None,
) -> None:
    from .metadata import embedded_metadata_supported, verify_metadata_write, write_metadata

    assert plan.proposed_path is not None
    assert plan.genre is not None
    original_path = plan.media.path
    target_path = plan.proposed_path
    current_path = original_path
    operation_id: str | None = None
    current_sidecar: Path | None = None
    warning_messages: list[str] = []

    plan.rollback_record = {
        "run_id": run_id,
        "original_path": str(original_path),
        "new_path": str(target_path),
        "metadata_sidecar": "",
        "renamed": False,
        "rename_verified": False,
        "metadata_written": False,
        "metadata_verified": False,
        "source_verified": False,
        "post_apply_size_bytes": None,
        "post_apply_modified_ns": None,
        "operation_id": "",
    }

    try:
        readiness = probe_apply_readiness(original_path, target_path, config)
        plan.match.evidence["write_readiness_at_apply"] = readiness
        if readiness_blocks_apply(readiness):
            plan.status = "write_readiness_blocked"
            plan.error = (
                "Apply readiness check blocked metadata/rename before mutation: "
                + str(readiness.get("status") or "unknown")
                + (" | " + str(readiness.get("error")) if readiness.get("error") else "")
            )
            return
        if journal is not None:
            operation_id = journal.start(
                original_path,
                target_path,
                details={
                    "mode": mode,
                    "confidence": round(plan.match.confidence, 3),
                    "source": plan.match.source,
                    "identity_tier": plan.match.identity_tier,
                },
            )
            plan.operation_id = operation_id
            plan.rollback_record["operation_id"] = operation_id

        source_ok, source_details = verify_source_unchanged(plan.media)
        plan.source_verified = source_ok
        plan.rollback_record["source_verified"] = source_ok
        if journal and operation_id:
            journal.update(operation_id, "source_verified" if source_ok else "source_changed", details=source_details)
        if not source_ok and bool(config.get("processing.verify_source_unchanged_before_apply", True)):
            plan.status = "source_changed_skipped"
            plan.error = source_details.get("reason", "Source file changed after scan; skipped stale apply plan.")
            if journal and operation_id:
                journal.fail(operation_id, "source_changed", plan.error, source_details)
            return

        # Metadata is written and verified before rename. If a crash occurs, the source path still
        # exists and the journal identifies the exact completed stage for a clean rerun/recovery.
        if bool(config.get("processing.write_metadata", True)):
            current_sidecar = build_sidecar_path(current_path, config)
            wrote, error, sidecar_written = write_metadata(
                current_path,
                plan.match,
                plan.genre,
                config,
                sidecar_path=current_sidecar,
                original_path=original_path,
            )
            plan.metadata_written = wrote
            plan.sidecar_path = sidecar_written
            plan.rollback_record["metadata_sidecar"] = str(sidecar_written) if sidecar_written else ""
            plan.rollback_record["metadata_written"] = wrote
            durable_output = wrote or sidecar_written is not None
            if journal and operation_id:
                journal.update(
                    operation_id,
                    "metadata_written" if durable_output else "metadata_write_failed",
                    details={"embedded_written": wrote, "sidecar": str(sidecar_written) if sidecar_written else "", "metadata_error": error or ""},
                )

            if bool(config.get("processing.verify_metadata_after_write", True)):
                verified, verify_details = verify_metadata_write(
                    current_path,
                    plan.match,
                    plan.genre,
                    embedded_written=wrote,
                    sidecar_path=sidecar_written,
                )
            else:
                verified, verify_details = durable_output, {"verification_disabled": True, "verified": durable_output}
            plan.metadata_verified = verified
            plan.rollback_record["metadata_verified"] = verified
            if journal and operation_id:
                journal.update(operation_id, "metadata_verified" if verified else "metadata_verification_failed", details={"metadata_verification": verify_details})

            if error:
                warning_messages.append(error)
            embedded_required = (
                mode == "apply-safe"
                and bool(config.get("processing.require_embedded_metadata_for_supported_formats_apply_safe", True))
                and embedded_metadata_supported(current_path, config)
            )
            if embedded_required and not wrote:
                plan.status = "embedded_metadata_write_failed"
                plan.error = (
                    "Embedded metadata write failed for a supported format; apply-safe retained the original filename. "
                    + (error or "A sidecar alone is not sufficient for this format.")
                )
                if journal and operation_id:
                    journal.fail(
                        operation_id,
                        "embedded_metadata_write_failed",
                        plan.error,
                        {"metadata_verification": verify_details, "sidecar": str(sidecar_written) if sidecar_written else ""},
                    )
                return
            require_verified = mode == "apply-safe" and bool(config.get("processing.require_verified_metadata_before_rename_apply_safe", True))
            if require_verified and not verified:
                plan.status = "metadata_verification_failed"
                plan.error = "Metadata could not be verified; apply-safe did not rename the file."
                if journal and operation_id:
                    journal.fail(operation_id, "metadata_verification_failed", plan.error, {"metadata_verification": verify_details})
                return
            if not verified:
                warning_messages.append("Metadata verification did not pass; apply-all continued under aggressive policy.")

        if bool(config.get("processing.rename_files", True)) and not same_path_spelling(target_path, current_path):
            target_path.parent.mkdir(parents=True, exist_ok=True)
            case_only = is_case_only_rename(current_path, target_path)
            if target_path.exists() and not case_only:
                raise RuntimeError(f"Rename target appeared after planning; refusing to overwrite: {target_path}")
            post_metadata_size = current_path.stat().st_size
            rename_path_with_case_support(current_path, target_path)
            current_path = target_path
            plan.renamed = True
            plan.rollback_record["renamed"] = True
            source_gone = (
                not path_entry_exists_exact(original_path)
                if case_only
                else not original_path.exists()
            )
            target_exists = path_entry_exists_exact(current_path) and current_path.is_file()
            target_stat = current_path.stat() if target_exists else None
            target_size = target_stat.st_size if target_stat is not None else None
            target_modified_ns = getattr(target_stat, "st_mtime_ns", None) if target_stat is not None else None
            plan.rename_verified = bool(source_gone and target_exists and target_size == post_metadata_size)
            plan.rollback_record["rename_verified"] = plan.rename_verified
            plan.rollback_record["post_apply_size_bytes"] = target_size
            plan.rollback_record["post_apply_modified_ns"] = target_modified_ns
            rename_details = {
                "current_path": str(current_path),
                "source_path_absent": source_gone,
                "target_exists": target_exists,
                "expected_size_bytes": post_metadata_size,
                "target_size_bytes": target_size,
                "verified": plan.rename_verified,
            }
            if journal and operation_id:
                journal.update(operation_id, "rename_verified" if plan.rename_verified else "rename_verification_failed", details={"rename_verification": rename_details})
            if not plan.rename_verified:
                raise RuntimeError("Rename completed but post-rename verification failed; see operation journal and rollback manifest.")

            # Keep a generated sidecar paired with the renamed media file.
            if plan.sidecar_path and plan.sidecar_path.exists():
                final_sidecar = build_sidecar_path(current_path, config)
                if not same_path_spelling(plan.sidecar_path, final_sidecar):
                    sidecar_case_only = is_case_only_rename(plan.sidecar_path, final_sidecar)
                    if final_sidecar.exists() and not sidecar_case_only:
                        warning_messages.append(f"Sidecar target already exists; retained sidecar at {plan.sidecar_path}")
                    else:
                        rename_path_with_case_support(plan.sidecar_path, final_sidecar)
                        plan.sidecar_path = final_sidecar
                        plan.rollback_record["metadata_sidecar"] = str(final_sidecar)
        else:
            plan.proposed_path = current_path
            plan.rename_verified = True
            plan.rollback_record["rename_verified"] = True

        if warning_messages:
            plan.error = " | ".join(dict.fromkeys(message for message in warning_messages if message))
            plan.status = "applied_with_warning"
        else:
            plan.status = "applied"
        if journal and operation_id:
            journal.complete(
                operation_id,
                details={
                    "final_path": str(current_path),
                    "metadata_written": plan.metadata_written,
                    "metadata_verified": plan.metadata_verified,
                    "renamed": plan.renamed,
                    "rename_verified": plan.rename_verified,
                    "warning": plan.error or "",
                },
            )
    except Exception as exc:
        plan.status = "apply_failed"
        plan.error = str(exc)
        LOG.warning("Apply failed for %s: %s", original_path, exc)
        if journal and operation_id:
            journal.fail(operation_id, "apply_failed", str(exc), {"current_path": str(current_path)})


def verify_source_unchanged(media: MediaFile) -> tuple[bool, dict[str, Any]]:
    details: dict[str, Any] = {
        "path": str(media.path),
        "expected_size_bytes": media.size_bytes,
        "expected_modified_ns": media.modified_ns,
    }
    try:
        current = media.path.stat()
    except FileNotFoundError:
        details["reason"] = "Source file no longer exists after scan."
        return False, details
    except OSError as exc:
        details["reason"] = f"Source file could not be re-statted after scan: {exc}"
        return False, details
    current_modified_ns = getattr(current, "st_mtime_ns", None)
    details["current_size_bytes"] = current.st_size
    details["current_modified_ns"] = current_modified_ns
    if current.st_size != media.size_bytes:
        details["reason"] = "Source file size changed after scan; stale plan skipped."
        return False, details
    if media.modified_ns is not None and current_modified_ns is not None and current_modified_ns != media.modified_ns:
        details["reason"] = "Source file modification time changed after scan; stale plan skipped."
        return False, details
    details["reason"] = "unchanged"
    return True, details


def write_api_metrics(
    config: AppConfig,
    run_id: str,
    clients: tuple[Any | None, Any | None, Any | None, Any | None],
    cache: JsonCache,
    matcher: Any,
) -> Path:
    payload = {
        "schema": "MediaTaggerBot.api_metrics.v1",
        "run_id": run_id,
        "created_utc": now_utc().isoformat(),
        "services": {
            client.namespace: client.metrics_snapshot()
            for client in clients
            if client is not None
        },
        "cache": cache.snapshot(),
        "identity_memory": dict(matcher.identity_memory_stats),
    }
    path = config.state_dir / "last_api_metrics.json"
    write_json_atomic(path, payload)
    write_json_atomic(config.state_dir / "last_identity_memory_stats.json", {"run_id": run_id, **matcher.identity_memory_stats})
    return path


def _path_reservation_key(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(str(path)))


def run_rollback(config: AppConfig, manifest_arg: str, run_id: str, log_path: Path) -> int:
    if not manifest_arg:
        raise RuntimeError("Rollback mode requires --rollback-manifest path to a rollback_manifest_*.json file.")
    manifest_path = Path(manifest_arg).expanduser().resolve()
    if not manifest_path.exists() or not manifest_path.is_file():
        raise RuntimeError(f"Rollback manifest not found: {manifest_path}")
    if manifest_path.stat().st_size > 20_000_000:
        raise RuntimeError("Rollback manifest exceeds the 20 MB safety limit.")

    records = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise RuntimeError("Rollback manifest JSON must contain a list.")

    require_containment = bool(config.get("processing.rollback_require_paths_under_media_root", True))
    media_root = config.media_root
    if require_containment:
        if not media_root:
            raise RuntimeError("Rollback containment is enabled, but paths.media_root is not configured.")
        media_root = media_root.resolve()
        if not media_root.exists() or not media_root.is_dir():
            raise RuntimeError(f"Rollback containment root is unavailable: {media_root}")

    validation_rows: list[dict[str, Any]] = []
    validated: list[tuple[int, Path, Path]] = []
    rollback_expectations: dict[int, tuple[int | None, int | None]] = {}
    for index, record in enumerate(records):
        row: dict[str, Any] = {"record_index": index, "status": "blocked", "error": ""}
        if not isinstance(record, dict):
            row["error"] = "record_not_object"
            validation_rows.append(row)
            continue
        original_raw = str(record.get("original_path") or "").strip()
        new_raw = str(record.get("new_path") or "").strip()
        row.update({"original_path": original_raw, "new_path": new_raw})
        if not original_raw or not new_raw:
            row["error"] = "missing_original_or_new_path"
            validation_rows.append(row)
            continue
        original = Path(original_raw).expanduser()
        new = Path(new_raw).expanduser()
        if not original.is_absolute() or not new.is_absolute():
            row["error"] = "rollback_paths_must_be_absolute"
            validation_rows.append(row)
            continue
        original = original.resolve(strict=False)
        new = new.resolve(strict=False)
        if require_containment and media_root is not None:
            if not path_is_within(original, media_root) or not path_is_within(new, media_root):
                row["error"] = "path_outside_configured_media_root"
                validation_rows.append(row)
                continue
        if original.parent != new.parent:
            row["error"] = "rollback_record_is_not_same_folder_rename"
            validation_rows.append(row)
            continue
        if original.suffix.casefold() != new.suffix.casefold():
            row["error"] = "rollback_record_changes_file_extension"
            validation_rows.append(row)
            continue
        if same_path_spelling(original, new):
            row["error"] = "original_and_new_paths_are_identical"
            validation_rows.append(row)
            continue
        try:
            expected_size = _optional_nonnegative_int(record.get("post_apply_size_bytes"), "post_apply_size_bytes")
            expected_modified_ns = _optional_nonnegative_int(record.get("post_apply_modified_ns"), "post_apply_modified_ns")
        except ValueError as exc:
            row["error"] = str(exc)
            validation_rows.append(row)
            continue
        rollback_expectations[index] = (expected_size, expected_modified_ns)
        row["expected_post_apply_size_bytes"] = expected_size
        row["expected_post_apply_modified_ns"] = expected_modified_ns
        row["status"] = "validated"
        validation_rows.append(row)
        validated.append((index, original, new))

    # A manifest can be individually well formed but collectively unsafe. Reject
    # duplicate destinations/sources and path-overlap graphs before the first move.
    # Case-only rename pairs within the same record remain valid.
    from collections import defaultdict

    original_map: dict[str, list[int]] = defaultdict(list)
    new_map: dict[str, list[int]] = defaultdict(list)
    for index, original, new in validated:
        original_map[rollback_path_key(original)].append(index)
        new_map[rollback_path_key(new)].append(index)

    conflict_reasons: dict[int, set[str]] = defaultdict(set)
    for indices in original_map.values():
        if len(indices) > 1:
            for index in indices:
                conflict_reasons[index].add("duplicate_original_path")
    for indices in new_map.values():
        if len(indices) > 1:
            for index in indices:
                conflict_reasons[index].add("duplicate_new_path")
    for key in set(original_map).intersection(new_map):
        related = set(original_map[key]) | set(new_map[key])
        if len(related) > 1:
            for index in related:
                conflict_reasons[index].add("cross_record_path_overlap")

    if conflict_reasons:
        row_by_index = {int(row["record_index"]): row for row in validation_rows}
        for index, reasons in conflict_reasons.items():
            row = row_by_index[index]
            row["status"] = "blocked"
            row["error"] = "rollback_manifest_path_conflict:" + ",".join(sorted(reasons))
        validated = [entry for entry in validated if entry[0] not in conflict_reasons]

    # Snapshot path-state conflicts before the first move. A manifest with both the
    # renamed and original path present is ambiguous and must not be partially applied.
    # Missing-both remains a non-mutating per-record outcome rather than a safety risk.
    row_by_index = {int(row["record_index"]): row for row in validation_rows}
    state_conflicts: set[int] = set()
    for index, original, new in validated:
        case_only = is_case_only_rename(new, original)
        if case_only:
            original_exists = path_entry_exists_exact(original)
            new_exists = path_entry_exists_exact(new)
        else:
            original_exists = original.exists()
            new_exists = new.exists()
        row_by_index[index]["preflight_original_exists"] = original_exists
        row_by_index[index]["preflight_new_exists"] = new_exists
        if original_exists and new_exists and not same_path_spelling(original, new):
            row_by_index[index]["status"] = "blocked"
            row_by_index[index]["error"] = "rollback_path_collision_both_original_and_new_exist"
            state_conflicts.add(index)
            continue
        if new_exists and not original_exists:
            expected_size, expected_modified_ns = rollback_expectations.get(index, (None, None))
            try:
                current_stat = new.stat()
            except OSError as exc:
                row_by_index[index]["status"] = "blocked"
                row_by_index[index]["error"] = f"rollback_source_stat_failed:{type(exc).__name__}"
                state_conflicts.add(index)
                continue
            row_by_index[index]["preflight_new_size_bytes"] = current_stat.st_size
            row_by_index[index]["preflight_new_modified_ns"] = getattr(current_stat, "st_mtime_ns", None)
            if expected_size is not None and current_stat.st_size != expected_size:
                row_by_index[index]["status"] = "blocked"
                row_by_index[index]["error"] = "rollback_source_changed_size_mismatch"
                state_conflicts.add(index)
                continue
            current_modified_ns = getattr(current_stat, "st_mtime_ns", None)
            if expected_modified_ns is not None and current_modified_ns is not None and current_modified_ns != expected_modified_ns:
                row_by_index[index]["status"] = "blocked"
                row_by_index[index]["error"] = "rollback_source_changed_mtime_mismatch"
                state_conflicts.add(index)
    if state_conflicts:
        validated = [entry for entry in validated if entry[0] not in state_conflicts]

    out_path = config.exports_dir / f"rollback_result_{run_id}.json"
    if len(validated) != len(records):
        payload = {
            "schema": "MediaTaggerBot.rollback_result.v2",
            "run_id": run_id,
            "status": "blocked_manifest_validation",
            "manifest": str(manifest_path),
            "configured_media_root": str(media_root) if media_root else "not_set",
            "records_total": len(records),
            "records_validated": len(validated),
            "media_files_mutated": False,
            "results": validation_rows,
        }
        write_json_atomic(out_path, payload)
        diag = write_diagnostics_export(
            config,
            run_id,
            "rollback",
            log_path=log_path,
            report_paths={"rollback_result": out_path},
        )
        write_run_exit_report(
            config,
            run_id,
            "rollback",
            exit_code=2,
            terminal_status="blocked",
            completion_class="blocked_before_mutation",
            completed_verified=["Every rollback record was validated before any file move was attempted."],
            skipped_deferred_blocked=["The entire rollback was blocked because one or more manifest records were unsafe or malformed."],
            exact_outputs={"rollback_result": out_path, "diagnostics_zip": diag, "log": log_path},
            safest_next_action="Use the rollback manifest generated by this bot for the currently configured media root, or inspect the blocked rows.",
        )
        print(f"Rollback blocked before mutation: {out_path}")
        print(f"Diagnostics ZIP: {diag}")
        return 2

    output: list[dict[str, Any]] = []
    failed_count = 0
    changed_count = 0
    for index, original, new in validated:
        status = "skipped"
        error = ""
        try:
            case_only = is_case_only_rename(new, original)
            if case_only and path_entry_exists_exact(new) and not same_path_spelling(new, original):
                original.parent.mkdir(parents=True, exist_ok=True)
                rename_path_with_case_support(new, original)
                status = "renamed_back_case_only"
                changed_count += 1
            elif new.exists() and not original.exists():
                original.parent.mkdir(parents=True, exist_ok=True)
                rename_path_with_case_support(new, original)
                status = "renamed_back"
                changed_count += 1
            elif original.exists():
                status = "original_already_exists"
            else:
                status = "missing_both_paths"
        except Exception as exc:
            status = "failed"
            error = str(exc)
            failed_count += 1
        output.append(
            {
                "record_index": index,
                "original_path": str(original),
                "new_path": str(new),
                "status": status,
                "error": error,
            }
        )

    payload = {
        "schema": "MediaTaggerBot.rollback_result.v2",
        "run_id": run_id,
        "status": "completed" if failed_count == 0 else "completed_with_failures",
        "manifest": str(manifest_path),
        "configured_media_root": str(media_root) if media_root else "containment_disabled",
        "records_total": len(records),
        "records_validated": len(validated),
        "records_renamed_back": changed_count,
        "records_failed": failed_count,
        "results": output,
    }
    write_json_atomic(out_path, payload)
    diag = write_diagnostics_export(
        config,
        run_id,
        "rollback",
        log_path=log_path,
        report_paths={"rollback_result": out_path},
    )
    exit_code = 0 if failed_count == 0 else 4
    write_run_exit_report(
        config,
        run_id,
        "rollback",
        exit_code=exit_code,
        terminal_status="completed" if exit_code == 0 else "completed_with_failures",
        completion_class="completed_verified" if exit_code == 0 else "completed_not_fully_verified",
        completed_verified=[f"Validated {len(validated)} record(s) and renamed back {changed_count} file(s)."],
        completed_not_fully_verified=(
            [f"{failed_count} rollback record(s) failed; inspect the result file."] if failed_count else []
        ),
        actual_timeouts_errors=(
            [f"{failed_count} per-record rollback failure(s) were isolated."] if failed_count else []
        ),
        exact_outputs={"rollback_result": out_path, "diagnostics_zip": diag, "log": log_path},
        safest_next_action="Use backups for embedded metadata restoration; rollback only restores filenames.",
    )
    print(f"Rollback result: {out_path}")
    print(f"Diagnostics ZIP: {diag}")
    print("Note: rollback mode renames files back; it does not undo embedded metadata edits. Use your backups for full metadata restore.")
    return exit_code


def _optional_nonnegative_int(value: Any, field_name: str) -> int | None:
    if value is None or value == "":
        return None
    if type(value) is bool:
        raise ValueError(f"invalid_{field_name}")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid_{field_name}") from exc
    if parsed < 0:
        raise ValueError(f"invalid_{field_name}")
    return parsed


def rollback_path_key(path: Path) -> str:
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        resolved = path.absolute()
    return os.path.normcase(str(resolved)).casefold()


def path_is_within(candidate: Path, root: Path) -> bool:
    try:
        candidate.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except (OSError, ValueError):
        return False



def is_untrusted_prior_bot_text_source(source: str | None) -> bool:
    value = str(source or "").strip().casefold()
    return value.startswith("musicbrainz_search_") or value in {
        "existing_mediataggerbot_text_tags",
        "existing_mediataggerbot_tags_text_repository",
    }

def build_already_managed_plan(media: MediaFile, config: AppConfig) -> PlanResult | None:
    """Skip repeat API/fingerprint work only for a file previously completed by this bot.

    A generic filename match is never enough. The file must carry MediaTaggerBot markers,
    usable identity/genre tags, and already have the exact target filename.
    """
    if not bool(config.get("processing.skip_if_filename_already_matches", True)):
        return None
    if not (media.existing_mtb_version and media.existing_mtb_source):
        return None
    if is_untrusted_prior_bot_text_source(media.existing_mtb_source):
        # v0.5.2 evidence proved that a high text-search score can confirm bad local tags.
        # Do not hide those files behind the repeat-run fast skip.
        return None
    if not (media.existing_artist and media.existing_title and media.existing_genre):
        return None

    confidence = media.existing_mtb_confidence if media.existing_mtb_confidence is not None else 95.0
    match = MatchResult(
        matched=True,
        confidence=max(0.0, min(100.0, float(confidence))),
        source="existing_mediataggerbot_tags",
        artist=media.existing_artist,
        source_artist_credit=media.existing_source_artist_credit or media.existing_artist,
        musicbrainz_artist_ids=list(media.existing_musicbrainz_artist_ids),
        title=media.existing_title,
        album=media.existing_album,
        album_artist=media.existing_album_artist,
        date=media.existing_date,
        isrc=media.existing_isrc,
        musicbrainz_recording_id=media.existing_musicbrainz_recording_id,
        musicbrainz_release_id=media.existing_musicbrainz_release_id,
        musicbrainz_release_group_id=media.existing_musicbrainz_release_group_id,
        acoustid_id=media.existing_acoustid_id,
        canonicalization_status=media.existing_canonicalization_status or "existing_mediataggerbot_tags",
        canonicalization_score=100.0 if media.existing_musicbrainz_recording_id else 80.0,
        raw_genres=[media.existing_genre],
        raw_tags=[media.existing_subgenre] if media.existing_subgenre else [],
        notes=[f"Fast repeat-run skip: already managed by MediaTaggerBot {media.existing_mtb_version}."],
    )
    genre = classify_genre([media.existing_genre, media.existing_subgenre or ""], config)
    if media.existing_subgenre:
        genre = replace(
            genre,
            subgenre=media.existing_subgenre,
            source="existing_mediataggerbot_tags",
            confidence=max(genre.confidence, match.confidence),
        )
    target = build_target_path(media.path, match, genre, config)
    if not same_path_spelling(target, media.path):
        return None
    return PlanResult(
        media=media,
        match=match,
        genre=genre,
        proposed_path=media.path,
        proposed_filename=media.path.name,
        action="fast_skip_already_managed",
        should_apply=False,
        status="already_managed_skipped",
    )


def compact_scan_coverage(coverage: ScanCoverage) -> dict[str, Any]:
    return {
        "status": coverage.status,
        "all_reachable_subfolders_checked": coverage.all_reachable_subfolders_checked,
        "directories_visited": coverage.directories_visited,
        "subdirectories_discovered": coverage.subdirectories_discovered,
        "directories_excluded": coverage.directories_excluded,
        "directory_symlinks_skipped": coverage.directory_symlinks_skipped,
        "directory_error_count": len(coverage.directory_errors),
        "media_files_found": coverage.media_files_found,
        "media_files_scanned": coverage.media_files_scanned,
        "inventory_cache_hits": coverage.inventory_cache_hits,
        "inventory_cache_misses": coverage.inventory_cache_misses,
        "inventory_cache_writes": coverage.inventory_cache_writes,
        "deepest_relative_depth": coverage.deepest_relative_depth,
        "limit_reached": coverage.limit_reached,
        "graceful_stop_requested": coverage.graceful_stop_requested,
        "graceful_stop_reason": coverage.graceful_stop_reason,
        "stopped_phase": coverage.stopped_phase,
    }


def same_path_spelling(left: Path, right: Path) -> bool:
    """Compare the requested path spelling without case-folding it away."""
    return os.path.abspath(os.path.normpath(str(left))) == os.path.abspath(os.path.normpath(str(right)))


def is_case_only_rename(source: Path, target: Path) -> bool:
    try:
        same_parent = source.parent.resolve() == target.parent.resolve()
    except OSError:
        same_parent = os.path.abspath(str(source.parent)) == os.path.abspath(str(target.parent))
    return same_parent and source.name != target.name and source.name.casefold() == target.name.casefold()


def path_entry_exists_exact(path: Path) -> bool:
    """Check the directory entry spelling exactly, even on case-insensitive Windows volumes."""
    try:
        return any(entry.name == path.name for entry in path.parent.iterdir())
    except OSError:
        return path.exists()


def rename_path_with_case_support(source: Path, target: Path) -> None:
    """Rename without overwriting and use a reversible two-step move for case-only changes."""
    if same_path_spelling(source, target):
        return
    if not is_case_only_rename(source, target):
        if target.exists():
            raise RuntimeError(f"Rename target already exists: {target}")
        source.rename(target)
        return

    temporary = source.with_name(f".mtb_case_{uuid.uuid4().hex}.tmp")
    while temporary.exists():
        temporary = source.with_name(f".mtb_case_{uuid.uuid4().hex}.tmp")
    source.rename(temporary)
    try:
        temporary.rename(target)
    except Exception:
        # Best-effort restore of the original spelling if the second move fails.
        if temporary.exists() and not source.exists():
            temporary.rename(source)
        raise


def same_path(left: Path, right: Path) -> bool:
    try:
        return os.path.normcase(str(left.resolve())) == os.path.normcase(str(right.resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(str(left))) == os.path.normcase(os.path.abspath(str(right)))


def prepend_local_tool_paths(project_root: Path) -> None:
    candidates = [
        project_root / "tools",
        project_root / "tools" / "ffmpeg" / "bin",
        project_root / "tools" / "chromaprint",
        project_root / "tools" / "exiftool",
    ]
    existing = [str(path) for path in candidates if path.exists()]
    if existing:
        os.environ["PATH"] = os.pathsep.join(existing + [os.environ.get("PATH", "")])


def print_success(
    run_id: str,
    mode: str,
    reports: dict[str, Path],
    diag: Path,
    count: int,
    elapsed: float,
    scan_coverage: ScanCoverage | None = None,
) -> None:
    print("MediaTaggerBot complete.")
    print(f"Run ID: {run_id}")
    print(f"Mode: {mode}")
    print(f"Files planned: {count}")
    print(f"Elapsed: {elapsed:.1f}s")
    if scan_coverage is not None:
        print(f"Recursive coverage: {scan_coverage.status}")
        print(f"All reachable subfolders checked: {scan_coverage.all_reachable_subfolders_checked}")
        print(f"Directories visited: {scan_coverage.directories_visited} (subdirectories discovered: {scan_coverage.subdirectories_discovered})")
        print(f"Deepest media depth: {scan_coverage.deepest_relative_depth}")
        print(
            "Inventory cache: "
            f"hits={scan_coverage.inventory_cache_hits} "
            f"misses={scan_coverage.inventory_cache_misses} "
            f"writes={scan_coverage.inventory_cache_writes}"
        )
    for label, path in reports.items():
        print(f"{label}: {path}")
    print(f"diagnostics_zip: {diag}")


if __name__ == "__main__":
    raise SystemExit(main())
