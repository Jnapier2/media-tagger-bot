from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

PACKAGE_ID = "media-tagger-bot"
CONTROL_FILES = ("VERSION.txt", "MANIFEST.json", "PACKAGE_METADATA.json")
_STATUS_NAME = "runtime_identity_status.json"
_HEX64 = re.compile(r"^[0-9a-fA-F]{64}$")
_DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_version(value: Any) -> str:
    text = str(value or "").strip()
    return text[1:] if text.lower().startswith("v") else text


def _read_version_file(path: Path) -> dict[str, str]:
    raw = path.read_text(encoding="utf-8-sig")
    values: dict[str, str] = {}
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "=" in stripped:
            key, value = stripped.split("=", 1)
            values[key.strip().casefold()] = value.strip()
        elif "version" not in values:
            values["version"] = stripped
    return {
        "package_id": values.get("package_id", values.get("package", "")),
        "version": values.get("version", ""),
        "build_id": values.get("build_id", values.get("build", "")),
    }


def _safe_manifest_relative(raw: Any) -> tuple[str | None, str | None]:
    text = str(raw or "").strip()
    if not text:
        return None, "empty_path"
    # Release manifests use normalized POSIX project-relative paths even on Windows.
    if "\\" in text:
        return None, "backslash_not_normalized"
    if text.startswith("/") or _DRIVE_PREFIX.match(text):
        return None, "absolute_path"
    if ":" in text:
        return None, "colon_not_allowed"
    pure = PurePosixPath(text)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
        return None, "unsafe_relative_path"
    normalized = pure.as_posix()
    if normalized != text:
        return None, "path_not_normalized"
    return normalized, None


def _load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("top-level JSON value must be an object")
    return value


def verify_runtime_identity(
    project_root: Path,
    runtime_version: str,
    runtime_build_id: str,
    runtime_package_id: str = PACKAGE_ID,
) -> dict[str, Any]:
    """Verify release identity and every immutable package-managed file.

    Standard-library-only. It intentionally does not read runtime config,
    environment credentials, caches, state databases, or user media.
    """
    started_monotonic = time.monotonic()
    started_utc = _utc_now()
    root = Path(project_root).resolve()
    mismatches: list[dict[str, Any]] = []
    control_hashes: dict[str, str] = {}

    for name in CONTROL_FILES:
        path = root / name
        if not path.is_file():
            mismatches.append({"type": "missing_control_file", "path": name})
            continue
        try:
            control_hashes[name] = _sha256(path)
        except Exception as exc:
            mismatches.append({
                "type": "unreadable_control_file",
                "path": name,
                "error": f"{type(exc).__name__}: {exc}",
            })

    version_payload: dict[str, Any] = {}
    manifest: dict[str, Any] = {}
    metadata: dict[str, Any] = {}
    loaders = (
        ("VERSION.txt", lambda path: _read_version_file(path)),
        ("MANIFEST.json", _load_json),
        ("PACKAGE_METADATA.json", _load_json),
    )
    loaded: dict[str, dict[str, Any]] = {}
    for name, loader in loaders:
        path = root / name
        if not path.is_file():
            continue
        try:
            loaded[name] = loader(path)
        except Exception as exc:
            mismatches.append({
                "type": "invalid_control_file",
                "path": name,
                "error": f"{type(exc).__name__}: {exc}",
            })
    version_payload = loaded.get("VERSION.txt", {})
    manifest = loaded.get("MANIFEST.json", {})
    metadata = loaded.get("PACKAGE_METADATA.json", {})

    controls = {
        "running_code": {
            "package_id": runtime_package_id,
            "version": f"v{_normalize_version(runtime_version)}",
            "build_id": str(runtime_build_id or ""),
        },
        "VERSION.txt": {
            "package_id": version_payload.get("package_id", ""),
            "version": version_payload.get("version", ""),
            "build_id": version_payload.get("build_id", ""),
        },
        "MANIFEST.json": {
            "package_id": manifest.get("package_id", manifest.get("project_slug", "")),
            "version": manifest.get("version", ""),
            "build_id": manifest.get("build_id", ""),
        },
        "PACKAGE_METADATA.json": {
            "package_id": metadata.get("package_id", ""),
            "version": metadata.get("version", ""),
            "build_id": metadata.get("build_id", ""),
        },
    }

    expected_package = str(runtime_package_id)
    expected_version = _normalize_version(runtime_version)
    expected_build = str(runtime_build_id or "")
    for source, identity in controls.items():
        package_value = str(identity.get("package_id") or "")
        version_value = _normalize_version(identity.get("version"))
        build_value = str(identity.get("build_id") or "")
        if package_value != expected_package:
            mismatches.append({
                "type": "package_id_mismatch",
                "source": source,
                "expected": expected_package,
                "actual": package_value,
            })
        if version_value != expected_version:
            mismatches.append({
                "type": "version_mismatch",
                "source": source,
                "expected": expected_version,
                "actual": version_value,
            })
        if build_value != expected_build:
            mismatches.append({
                "type": "build_id_mismatch",
                "source": source,
                "expected": expected_build,
                "actual": build_value,
            })

    records = manifest.get("files", []) if isinstance(manifest, dict) else []
    if not isinstance(records, list):
        mismatches.append({"type": "manifest_files_not_list", "path": "MANIFEST.json"})
        records = []

    seen: set[str] = set()
    managed_count = 0
    verified_count = 0
    unmanaged_count = 0
    for index, row in enumerate(records):
        if not isinstance(row, dict):
            mismatches.append({"type": "manifest_record_not_object", "index": index})
            continue
        normalized, path_error = _safe_manifest_relative(row.get("path"))
        if path_error or normalized is None:
            mismatches.append({
                "type": "unsafe_managed_path",
                "index": index,
                "path": str(row.get("path", "")),
                "reason": path_error,
            })
            continue
        identity_key = normalized.casefold()
        if identity_key in seen:
            mismatches.append({"type": "duplicate_manifest_path", "path": normalized})
            continue
        seen.add(identity_key)

        managed_flag = row.get("package_managed")
        if type(managed_flag) is not bool:
            mismatches.append({
                "type": "missing_or_invalid_package_managed_flag",
                "path": normalized,
            })
            continue
        if not managed_flag:
            unmanaged_count += 1
            continue

        managed_count += 1
        candidate = root.joinpath(*PurePosixPath(normalized).parts)
        try:
            resolved = candidate.resolve(strict=True)
            resolved.relative_to(root)
        except FileNotFoundError:
            mismatches.append({"type": "missing_managed_file", "path": normalized})
            continue
        except Exception as exc:
            mismatches.append({
                "type": "managed_path_out_of_root",
                "path": normalized,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        if candidate.is_symlink() or not resolved.is_file():
            mismatches.append({"type": "managed_file_not_regular", "path": normalized})
            continue

        expected_size = row.get("size_bytes")
        actual_size = resolved.stat().st_size
        if expected_size is not None:
            if type(expected_size) is not int or expected_size < 0:
                mismatches.append({
                    "type": "invalid_expected_size",
                    "path": normalized,
                    "expected": expected_size,
                })
                continue
            if actual_size != expected_size:
                mismatches.append({
                    "type": "managed_size_mismatch",
                    "path": normalized,
                    "expected_size": expected_size,
                    "actual_size": actual_size,
                })

        expected_hash = str(row.get("sha256") or "")
        if not _HEX64.fullmatch(expected_hash):
            mismatches.append({"type": "invalid_expected_sha256", "path": normalized})
            continue
        try:
            actual_hash = _sha256(resolved)
        except Exception as exc:
            mismatches.append({
                "type": "managed_hash_read_error",
                "path": normalized,
                "error": f"{type(exc).__name__}: {exc}",
            })
            continue
        if actual_hash.casefold() != expected_hash.casefold():
            mismatches.append({
                "type": "managed_sha256_mismatch",
                "path": normalized,
                "expected_sha256": expected_hash.lower(),
                "actual_sha256": actual_hash.lower(),
                "expected_size": expected_size,
                "actual_size": actual_size,
            })
            continue
        verified_count += 1

    elapsed_ms = round((time.monotonic() - started_monotonic) * 1000.0, 3)
    passed = not mismatches and managed_count > 0 and verified_count == managed_count
    return {
        "schema": "MediaTaggerBot.runtime_identity_status.v1",
        "package_id": expected_package,
        "runtime_version": f"v{expected_version}",
        "runtime_build_id": expected_build,
        "control_identities": controls,
        "control_file_hashes": control_hashes,
        "manifest_record_count": len(records),
        "package_managed_count": managed_count,
        "package_unmanaged_count": unmanaged_count,
        "package_verified_count": verified_count,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "gate_started_utc": started_utc,
        "gate_finished_utc": _utc_now(),
        "gate_duration_ms": elapsed_ms,
        "gate_result": "PASS" if passed else "BLOCK",
        "authenticated_activity_permitted": passed,
        "authentication_prevented_until_pass": True,
        "config_or_credentials_loaded_before_gate": False,
        "pre_auth_assertion": "Runtime config and credentials are loaded only after an identity-gate PASS.",
        "verification_behavior": "read_only_no_release_file_rewrite",
    }


def write_runtime_identity_status(project_root: Path, status: dict[str, Any]) -> Path:
    state_dir = Path(project_root).resolve() / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    destination = state_dir / _STATUS_NAME
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(status, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, destination)
    return destination


def read_runtime_identity_status(project_root: Path) -> dict[str, Any]:
    path = Path(project_root).resolve() / "state" / _STATUS_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


# The startup-failure path cannot trust the original control or log contents.
# Keep only bounded, typed fields from evidence already collected by the gate.
_IDENTITY_EXPORT_MAX_BYTES = 65_536
_IDENTITY_EXPORT_MAX_MISMATCHES = 64
_IDENTITY_MISMATCH_TYPES = frozenset({
    "missing_control_file", "unreadable_control_file", "invalid_control_file",
    "package_id_mismatch", "version_mismatch", "build_id_mismatch",
    "manifest_files_not_list", "manifest_record_not_object", "unsafe_managed_path",
    "duplicate_manifest_path", "missing_or_invalid_package_managed_flag",
    "missing_managed_file", "managed_path_out_of_root", "managed_file_not_regular",
    "invalid_expected_size", "managed_size_mismatch", "invalid_expected_sha256",
    "managed_hash_read_error", "managed_sha256_mismatch", "runtime_identity_evidence_write_failed", "unclassified_mismatch",
})
_IDENTITY_COUNT_FIELDS = (
    "manifest_record_count", "package_managed_count", "package_unmanaged_count",
    "package_verified_count", "mismatch_count",
)
_IDENTITY_BOOL_FIELDS = (
    "authenticated_activity_permitted", "config_or_credentials_loaded_before_gate",
    "authentication_prevented_until_pass",
)


def _identity_export_summary(status: dict[str, Any]) -> dict[str, Any]:
    """Project cached evidence onto a small vocabulary; never copy free text."""
    if type(status) is not dict:
        raise ValueError("Bootstrap export requires a plain cached status object")
    result: dict[str, Any] = {
        "schema": "MediaTaggerBot.bootstrap_identity_summary.v1",
        "package_id": PACKAGE_ID,
        "evidence_scope": "cached_identity_report_not_a_new_verification",
        "gate_result": status.get("gate_result") if status.get("gate_result") in ("PASS", "BLOCK") else "UNKNOWN",
        "original_contents_included": False,
        "control_file_hashes": {},
        "mismatches": [],
        "mismatches_omitted": 0,
        "omitted_content": ["raw_control_files", "raw_log_bodies", "free_text_errors", "caller_run_and_mode_labels"],
    }
    for name in _IDENTITY_COUNT_FIELDS:
        value = status.get(name)
        result[name] = value if type(value) is int and 0 <= value <= 1_000_000 else None
    for name in _IDENTITY_BOOL_FIELDS:
        value = status.get(name)
        result[name] = value if type(value) is bool else None
    hashes = status.get("control_file_hashes")
    if type(hashes) is dict:
        for name in CONTROL_FILES:
            value = hashes.get(name)
            if type(value) is str and len(value) == 64 and _HEX64.fullmatch(value):
                result["control_file_hashes"][name] = value.lower()
    mismatches = status.get("mismatches")
    if type(mismatches) is list:
        result["mismatches_omitted"] = max(0, len(mismatches) - _IDENTITY_EXPORT_MAX_MISMATCHES)
        for item in mismatches[:_IDENTITY_EXPORT_MAX_MISMATCHES]:
            kind, source = "unclassified_mismatch", "managed_payload_or_unknown"
            if type(item) is dict:
                value = item.get("type")
                if type(value) is str and value in _IDENTITY_MISMATCH_TYPES:
                    kind = value
                value = item.get("source", item.get("path"))
                if type(value) is str and value in (*CONTROL_FILES, "running_code", "state/runtime_identity_status.json"):
                    source = value
            result["mismatches"].append({"type": kind, "source": source})
    return result


def _identity_export_payloads(status: dict[str, Any]) -> list[tuple[str, bytes]]:
    summary = _identity_export_summary(status)
    # Defense in depth: no extensible object or raw-text field reaches staging.
    expected = {"schema", "package_id", "evidence_scope", "gate_result", "original_contents_included",
                "control_file_hashes", "mismatches", "mismatches_omitted", "omitted_content",
                *_IDENTITY_COUNT_FIELDS, *_IDENTITY_BOOL_FIELDS}
    if set(summary) != expected or summary["original_contents_included"] is not False:
        raise ValueError("Bootstrap export privacy validation failed")
    if summary != _identity_export_summary(status):
        raise ValueError("Bootstrap export cached evidence changed during serialization")
    summary_data = (json.dumps(summary, indent=2, ensure_ascii=True, allow_nan=False) + "\n").encode("utf-8")
    recovery = (
        "MediaTaggerBot minimal bootstrap evidence.\n"
        "This archive summarizes a cached identity report; it does not rerun verification.\n"
        "No original control file, log body, caller label, or free-text error is included.\n"
        "VERSION.txt, MANIFEST.json and PACKAGE_METADATA.json entries are sanitized summaries, not original files.\n"
        "Do not install these summaries as replacement package files.\n"
        "Unknown or omitted evidence is not a PASS or proof of pre-authentication ordering.\n"
        "Preserve the original failure evidence privately. Re-extract a separately verified complete release into a fresh local folder.\n"
        "Do not mix source, launchers or control files across releases. Run Preflight after an authorized repair.\n"
    ).encode("utf-8")
    payloads = [("runtime_identity_status.json", summary_data), ("IDENTITY_GATE_RECOVERY.txt", recovery)]
    for name in CONTROL_FILES:
        control = {
            "evidence_kind": "sanitized_cached_control_summary_not_original_file",
            "control_file": name,
            "original_bytes_included": False,
            "file_read_during_export": False,
            "cached_sha256": summary["control_file_hashes"].get(name),
        }
        payloads.append((name, (json.dumps(control, indent=2, allow_nan=False) + "\n").encode("utf-8")))
    if len(payloads) > 20 or sum(len(data) for _, data in payloads) > _IDENTITY_EXPORT_MAX_BYTES:
        raise ValueError("Bootstrap export exceeds its bounded payload budget")
    return payloads


def _identity_export_directory(root: Path, child: str | None = None) -> Path:
    """Validate fixed output directories without following linked parents."""
    import stat

    def checked_directory(path: Path) -> None:
        info = path.lstat()
        if (stat.S_ISLNK(info.st_mode)
                or getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                or not stat.S_ISDIR(info.st_mode)):
            raise ValueError("Bootstrap output requires regular, non-linked project directories")

    for parent in reversed((root, *root.parents)):
        checked_directory(parent)
    if child is None:
        return root
    if child not in {"diagnostics", "temp"}:
        raise ValueError("Unsupported bootstrap output directory")
    target = root / child
    if os.path.lexists(target):
        checked_directory(target)
    else:
        target.mkdir(exist_ok=False)
        checked_directory(target)
    return target


def write_identity_gate_support_export(
    project_root: Path,
    run_id: str,
    mode: str,
    status: dict[str, Any],
    log_path: Path | None = None,
) -> Path:
    """Publish bounded cached bootstrap evidence, never rejected files or logs.

    The signature is retained for callers. Untrusted run/mode labels and log_path
    are deliberately not read or serialized. Output uses an internal timestamp
    and random suffix instead. The caller must establish verifier trust; this
    function does not authenticate its own code or assert that a gate ran.
    """
    import secrets
    import shutil
    import stat

    # Privacy checks happen before staging or creating any output directory.
    payloads = _identity_export_payloads(status)
    root = Path(project_root).absolute()
    _identity_export_directory(root)
    if shutil.disk_usage(root).free < 4 * _IDENTITY_EXPORT_MAX_BYTES:
        raise OSError("Insufficient project-local space for bootstrap evidence")
    diagnostics_dir = _identity_export_directory(root, "diagnostics")
    temp_dir = _identity_export_directory(root, "temp")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    final_zip = diagnostics_dir / f"MediaTaggerBot_DIAGNOSTIC_{stamp}_{secrets.token_hex(6)}_IDENTITY_BLOCK.zip"
    sidecar = final_zip.with_suffix(final_zip.suffix + ".sha256.txt")
    fd, temp_name = tempfile.mkstemp(prefix="identity_export_", suffix=".zip", dir=temp_dir)
    temp_zip = Path(temp_name)
    temp_sidecar: Path | None = None
    try:
        with os.fdopen(fd, "w+b") as handle:
            with zipfile.ZipFile(handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for name, data in sorted(payloads, key=lambda item: item[0].casefold()):
                    info = zipfile.ZipInfo(name)
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = (stat.S_IFREG | 0o600) << 16
                    archive.writestr(info, data)
            handle.flush()
        with zipfile.ZipFile(temp_zip) as archive:
            members = archive.infolist()
            if ({item.filename for item in members} != {name for name, _ in payloads}
                    or len(members) != len(payloads)
                    or sum(item.file_size for item in members) > _IDENTITY_EXPORT_MAX_BYTES
                    or any(not stat.S_ISREG(item.external_attr >> 16) for item in members)
                    or archive.testzip() is not None):
                raise RuntimeError("Bootstrap export integrity validation failed")
        if temp_zip.stat().st_size > 2 * _IDENTITY_EXPORT_MAX_BYTES:
            raise RuntimeError("Bootstrap archive exceeds its bounded file budget")
        digest = _sha256(temp_zip)  # Only the completed diagnostic ZIP is hashed here.
        side_fd, side_name = tempfile.mkstemp(prefix="identity_checksum_", suffix=".tmp", dir=temp_dir)
        temp_sidecar = Path(side_name)
        with os.fdopen(side_fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{digest}  {final_zip.name}\n")
            handle.flush()
        _identity_export_directory(root, "diagnostics")
        _identity_export_directory(root, "temp")
        # Hard-link publication is atomic and refuses an existing destination.
        # Unsupported filesystems fail closed: no overwrite or external fallback.
        os.link(temp_zip, final_zip)
        try:
            os.link(temp_sidecar, sidecar)
        except OSError:
            # Preserve an already published evidence ZIP, even if its companion failed.
            raise RuntimeError("Bootstrap archive preserved; checksum publication failed") from None
    finally:
        for temporary in (temp_zip, temp_sidecar):
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass  # Never delete other evidence or replace the primary error.
    return final_zip
