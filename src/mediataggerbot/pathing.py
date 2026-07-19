from __future__ import annotations

import json
import os
import re
import shutil
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import write_json_atomic

WIN_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")
UNC_RE = re.compile(r"^\\\\")


def clean_user_path(raw: str | Path | None) -> str:
    """Normalize user-entered paths without changing their filesystem meaning.

    Windows ``cmd.exe`` historically mangled a quoted drive root such as
    ``"D:\\"`` into an argument ending in a literal quote.  The BAT launcher now
    transports paths through an environment variable, but this recovery keeps older
    transcripts/CLI invocations usable as well.
    """
    text = str(raw or "").strip()
    if (text.startswith('"') and text.endswith('"')) or (text.startswith("'") and text.endswith("'")):
        text = text[1:-1].strip()
    elif text.endswith('"') and (WIN_DRIVE_RE.match(text[:-1]) or UNC_RE.match(text[:-1])):
        # Recover a trailing quote introduced by Windows argv parsing when the
        # original path ended with a backslash.
        text = text[:-1].rstrip()
    return os.path.expandvars(text)


def looks_absolute_path(raw: str | Path | None) -> bool:
    text = clean_user_path(raw)
    if not text:
        return False
    return Path(text).expanduser().is_absolute() or bool(WIN_DRIVE_RE.match(text)) or bool(UNC_RE.match(text))


def resolve_user_path(project_root: Path, raw: str | Path | None, *, project_relative: bool = True) -> Path | None:
    text = clean_user_path(raw)
    if not text:
        return None
    path = Path(text).expanduser()
    if path.is_absolute() or looks_absolute_path(text):
        return path
    return (project_root / path) if project_relative else path


def build_path_status(config: Any) -> dict[str, Any]:
    raw_media_root = str(config.get("paths.media_root", "") or "").strip()
    media_root = config.media_root
    media_exists = bool(media_root and media_root.exists())
    media_is_dir = bool(media_root and media_root.is_dir())
    runtime_dirs = {
        "logs_dir": config.logs_dir,
        "exports_dir": config.exports_dir,
        "state_dir": config.state_dir,
        "diagnostics_dir": config.diagnostics_dir,
        "temp_dir": config.temp_dir,
    }
    runtime_status = {}
    stale_absolute_findings: list[dict[str, str]] = []
    for name, path in runtime_dirs.items():
        raw_value = str(config.get(f"paths.{name}", "") or "")
        exists = path.exists()
        runtime_status[name] = {
            "raw": raw_value,
            "resolved": str(path),
            "exists": exists,
            "is_absolute_like": looks_absolute_path(raw_value),
            "project_relative": not looks_absolute_path(raw_value),
        }
        if looks_absolute_path(raw_value) and not exists:
            stale_absolute_findings.append({"key": f"paths.{name}", "path": str(path), "reason": "absolute_runtime_path_missing"})

    if raw_media_root and looks_absolute_path(raw_media_root) and not media_exists:
        stale_absolute_findings.append({"key": "paths.media_root", "path": str(media_root), "reason": "media_root_missing"})

    project_root_resolved = config.project_root.resolve()
    media_root_resolved = media_root.resolve() if media_root and media_exists else media_root
    media_equals_project = bool(media_root_resolved and media_root_resolved == project_root_resolved)
    project_inside_media = False
    media_inside_project = False
    if media_root_resolved:
        try:
            project_root_resolved.relative_to(media_root_resolved)
            project_inside_media = not media_equals_project
        except (ValueError, OSError):
            pass
        try:
            media_root_resolved.relative_to(project_root_resolved)
            media_inside_project = not media_equals_project
        except (ValueError, OSError):
            pass
    portability_ok = not stale_absolute_findings and all(item.get("exists") for item in runtime_status.values())
    return {
        "schema": "MediaTaggerBot.path_status.v1",
        "project_root": str(config.project_root),
        "config_path": str(config.config_path),
        "install_mode": "portable_project_folder",
        "repair_available": True,
        "root_relationship": {
            "media_root_equals_project_root": media_equals_project,
            "project_root_is_inside_media_root": project_inside_media,
            "media_root_is_inside_project_root": media_inside_project,
            "warning": "Choose the media library, not the bot project folder." if media_equals_project else ("The bot project is inside the scan tree; its folders will also be traversed." if project_inside_media else "none"),
        },
        "media_root": {
            "raw": raw_media_root,
            "resolved": str(media_root) if media_root else "",
            "is_set": bool(raw_media_root),
            "exists": media_exists,
            "is_dir": media_is_dir,
            "is_absolute_like": looks_absolute_path(raw_media_root),
            "project_relative": bool(raw_media_root) and not looks_absolute_path(raw_media_root),
            "status": "ok" if media_is_dir else ("not_set" if not raw_media_root else "missing_or_not_directory"),
        },
        "runtime_dirs": runtime_status,
        "stale_absolute_path_findings": stale_absolute_findings,
        "portability_check": {
            "status": "pass" if portability_ok else "warning",
            "summary": "Project-owned folders are local and media root is reachable." if portability_ok else "One or more configured paths may be stale or missing.",
        },
    }


def toml_quote_path(value: str) -> str:
    """Return a TOML string that is safe and readable for Windows paths.

    Literal strings keep backslashes literal, so ``D:\\Music`` does not become an
    invalid TOML escape sequence.  Paths containing an apostrophe fall back to a
    JSON/TOML basic string with escaped backslashes.
    """
    if "'" not in value and "\n" not in value and "\r" not in value:
        return f"'{value}'"
    return json.dumps(value, ensure_ascii=False)


def _backup_config(config_path: Path, label: str) -> Path:
    backup_dir = config_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f_UTC")
    backup_path = backup_dir / f"{label}_{stamp}.toml.bak"
    shutil.copy2(config_path, backup_path)
    return backup_path


def _write_config_payload_atomic(config_path: Path, payload: str) -> dict[str, Any]:
    temp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        with temp_path.open("rb") as handle:
            parsed = tomllib.load(handle)
        os.replace(temp_path, config_path)
        return parsed
    finally:
        temp_path.unlink(missing_ok=True)


def _replace_media_root_line(payload: str, cleaned: str) -> str:
    lines = payload.splitlines()
    out: list[str] = []
    in_paths = False
    paths_seen = False
    media_root_written = False
    for line in lines:
        stripped = line.strip()
        section_match = re.match(r"^\[([^\]]+)\]\s*$", stripped)
        if section_match:
            if in_paths and not media_root_written:
                out.append(f"media_root = {toml_quote_path(cleaned)}")
                media_root_written = True
            section = section_match.group(1).strip()
            in_paths = section == "paths"
            paths_seen = paths_seen or in_paths
            out.append(line)
            continue
        if in_paths and re.match(r"^media_root\s*=", stripped):
            indent = line[: len(line) - len(line.lstrip())]
            out.append(f"{indent}media_root = {toml_quote_path(cleaned)}")
            media_root_written = True
        else:
            out.append(line)

    if not paths_seen:
        if out and out[-1].strip():
            out.append("")
        out.append("[paths]")
        out.append(f"media_root = {toml_quote_path(cleaned)}")
    elif in_paths and not media_root_written:
        out.append(f"media_root = {toml_quote_path(cleaned)}")
    return "\n".join(out).rstrip() + "\n"


def update_media_root_in_config(
    config_path: Path,
    new_root: str,
    *,
    backup: bool = True,
    example_path: Path | None = None,
    allow_rebuild_from_example: bool = False,
) -> dict[str, Any]:
    """Persist ``paths.media_root`` with parse-before-replace protection.

    If the existing config is malformed only because a Windows path was written with
    unescaped backslashes, replacing the media-root assignment repairs it in place.
    Set-root may additionally rebuild from the shipped example after backing up an
    otherwise-unparseable config; no media or runtime state is touched.
    """
    config_path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = clean_user_path(new_root)
    if not cleaned:
        raise ValueError("New media root is empty.")

    if not config_path.exists():
        if example_path and example_path.exists():
            shutil.copy2(example_path, config_path)
        else:
            raise FileNotFoundError(f"Config file not found: {config_path}")

    backup_path = _backup_config(config_path, "config_before_media_root") if backup else None
    original_payload = config_path.read_text(encoding="utf-8-sig")
    candidate_payload = _replace_media_root_line(original_payload, cleaned)
    rebuilt_from_example = False
    original_parse_error = ""

    try:
        parsed = _write_config_payload_atomic(config_path, candidate_payload)
    except tomllib.TOMLDecodeError as exc:
        original_parse_error = str(exc)
        if not allow_rebuild_from_example or not example_path or not example_path.exists():
            raise
        example_payload = example_path.read_text(encoding="utf-8-sig")
        candidate_payload = _replace_media_root_line(example_payload, cleaned)
        parsed = _write_config_payload_atomic(config_path, candidate_payload)
        rebuilt_from_example = True

    written_root = str(parsed.get("paths", {}).get("media_root", "")) if isinstance(parsed.get("paths"), dict) else ""
    if written_root != cleaned:
        raise RuntimeError("Generated config did not preserve the requested media_root exactly.")
    return {
        "config_path": str(config_path),
        "new_media_root": cleaned,
        "backup_path": str(backup_path) if backup_path else "",
        "toml_representation": toml_quote_path(cleaned),
        "rebuilt_from_example": rebuilt_from_example,
        "original_parse_error": original_parse_error,
    }


def attempt_repair_invalid_media_root(config_path: Path, *, backup: bool = True) -> dict[str, Any]:
    """Repair the common ``media_root = "D:\\Music"`` TOML mistake only.

    The function is intentionally narrow: it changes exactly one assignment, validates
    the entire candidate document, and atomically replaces the config only when the
    result parses.  Unrelated TOML errors are reported without mutation.
    """
    result: dict[str, Any] = {
        "attempted": False,
        "repaired": False,
        "config_path": str(config_path),
        "backup_path": "",
        "recovered_media_root": "",
        "error": "",
    }
    if not config_path.exists():
        result["error"] = "config_missing"
        return result
    try:
        with config_path.open("rb") as handle:
            tomllib.load(handle)
        result["error"] = "config_already_valid"
        return result
    except tomllib.TOMLDecodeError as exc:
        result["attempted"] = True
        result["original_error"] = str(exc)

    payload = config_path.read_text(encoding="utf-8-sig")
    lines = payload.splitlines()
    in_paths = False
    recovered = ""
    for line in lines:
        stripped = line.strip()
        section_match = re.match(r"^\[([^\]]+)\]\s*$", stripped)
        if section_match:
            in_paths = section_match.group(1).strip() == "paths"
            continue
        if not in_paths or not re.match(r"^media_root\s*=", stripped):
            continue
        raw_value = stripped.split("=", 1)[1].strip()
        if " #" in raw_value:
            raw_value = raw_value.split(" #", 1)[0].rstrip()
        if len(raw_value) >= 2 and raw_value[0] == raw_value[-1] and raw_value[0] in {"'", '"'}:
            recovered = raw_value[1:-1]
        else:
            recovered = raw_value
        recovered = clean_user_path(recovered)
        break

    if not recovered or not looks_absolute_path(recovered):
        result["error"] = "media_root_not_safely_recoverable"
        return result

    candidate_payload = _replace_media_root_line(payload, recovered)
    temp_path = config_path.with_suffix(config_path.suffix + ".repair.tmp")
    try:
        temp_path.write_text(candidate_payload, encoding="utf-8", newline="\n")
        with temp_path.open("rb") as handle:
            parsed = tomllib.load(handle)
        written_root = str(parsed.get("paths", {}).get("media_root", "")) if isinstance(parsed.get("paths"), dict) else ""
        if written_root != recovered:
            result["error"] = "repaired_value_mismatch"
            return result
        backup_path = _backup_config(config_path, "config_before_auto_path_repair") if backup else None
        os.replace(temp_path, config_path)
        result.update({
            "repaired": True,
            "backup_path": str(backup_path) if backup_path else "",
            "recovered_media_root": recovered,
            "error": "",
        })
        return result
    except tomllib.TOMLDecodeError as exc:
        result["error"] = f"unrelated_toml_error_remains: {exc}"
        return result
    finally:
        temp_path.unlink(missing_ok=True)



def build_input_assurance(config: Any) -> dict[str, Any]:
    """Compact proof chain for high-impact user path inputs.

    This keeps diagnostics useful after a drive-letter/folder move without requiring
    the user to upload raw media or large logs.
    """
    raw_media_root = str(config.get("paths.media_root", "") or "")
    cleaned_media_root = clean_user_path(raw_media_root)
    resolved_media_root = config.media_root
    media_exists = bool(resolved_media_root and resolved_media_root.exists())
    media_is_dir = bool(resolved_media_root and resolved_media_root.is_dir())
    return {
        "schema": "MediaTaggerBot.input_assurance.v1",
        "paths.media_root": {
            "recognized": bool(raw_media_root.strip()),
            "validated_non_empty": bool(cleaned_media_root),
            "normalized": cleaned_media_root,
            "mapped_to_resolved_path": str(resolved_media_root) if resolved_media_root else "",
            "project_relative": bool(cleaned_media_root) and not looks_absolute_path(cleaned_media_root),
            "absolute_like": looks_absolute_path(cleaned_media_root),
            "exercised_by_path_check": True,
            "confirmed_exists": media_exists,
            "confirmed_is_directory": media_is_dir,
            "status": "confirmed" if media_is_dir else ("not_set" if not cleaned_media_root else "needs_user_path_update"),
            "recommended_fix": "Use BAT menu option 8 Set media root, or pass --root for a one-run override." if not media_is_dir else "none",
        },
        "runtime_dirs": {
            "logs_dir": {"mapped_to": str(config.logs_dir), "confirmed_exists": config.logs_dir.exists()},
            "exports_dir": {"mapped_to": str(config.exports_dir), "confirmed_exists": config.exports_dir.exists()},
            "state_dir": {"mapped_to": str(config.state_dir), "confirmed_exists": config.state_dir.exists()},
            "diagnostics_dir": {"mapped_to": str(config.diagnostics_dir), "confirmed_exists": config.diagnostics_dir.exists()},
            "temp_dir": {"mapped_to": str(config.temp_dir), "confirmed_exists": config.temp_dir.exists()},
        },
    }

def write_repair_report(
    config: Any,
    run_id: str,
    *,
    project_drift: dict[str, Any] | None = None,
    cleanup_result: dict[str, Any] | None = None,
) -> Path:
    report = {
        "schema": "MediaTaggerBot.repair_report.v1",
        "run_id": run_id,
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "action": "non_destructive_repair_check",
        "project_root": str(config.project_root),
        "config_path": str(config.config_path),
        "config_load_status": getattr(config, "load_status", {"status": "unknown"}),
        "path_status": build_path_status(config),
        "input_assurance": build_input_assurance(config),
        "config_validation_errors": list(getattr(config, "validation_errors", [])),
        "project_drift": project_drift or {},
        "cleanup_result": cleanup_result or {},
        "notes": [
            "Repair/check is idempotent and never changes media files.",
            "Exact obsolete launcher filenames may be moved into archive/legacy_launchers with checksum evidence; they are never deleted.",
            "A narrowly recognized unescaped Windows media_root line may be repaired only after a timestamped config backup.",
            "Use BAT option 8 Set media root to change paths.media_root without hand-editing TOML.",
            "No media files are scanned, renamed, deleted, or retagged by repair mode.",
        ],
    }
    out = config.exports_dir / f"repair_report_{run_id}.json"
    write_json_atomic(out, report)
    return out
