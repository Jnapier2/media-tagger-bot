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


def write_identity_gate_support_export(
    project_root: Path,
    run_id: str,
    mode: str,
    status: dict[str, Any],
    log_path: Path | None = None,
) -> Path:
    """Create a compact pre-auth Support Export20 without reading runtime config."""
    root = Path(project_root).resolve()
    diagnostics_dir = root / "diagnostics"
    temp_dir = root / "temp"
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    temp_dir.mkdir(parents=True, exist_ok=True)
    final_zip = diagnostics_dir / f"MediaTaggerBot_DIAGNOSTIC_{run_id}_IDENTITY_BLOCK.zip"

    candidates: list[tuple[str, bytes]] = [
        (
            "runtime_identity_status.json",
            (json.dumps(status, indent=2, ensure_ascii=False) + "\n").encode("utf-8"),
        )
    ]
    recovery = (
        "MediaTaggerBot runtime identity gate blocked startup.\n"
        "No runtime config, credentials, authenticated API activity, or media mutation was started.\n"
        "Recovery: preserve this diagnostic, then re-extract the complete verified release ZIP into a fresh local folder.\n"
        "Do not copy old source, launchers, manifests, VERSION.txt, or PACKAGE_METADATA.json into the repaired release.\n"
        "After repair, rerun Preflight; the identity gate must report PASS before authenticated processing.\n"
    ).encode("utf-8")
    candidates.append(("IDENTITY_GATE_RECOVERY.txt", recovery))

    for name in CONTROL_FILES:
        path = root / name
        if path.is_file() and path.stat().st_size <= 2_000_000:
            candidates.append((name, path.read_bytes()))
    for name in ("KNOWN_GOOD_STATE.md", "RUNBOOK.md", "RELEASE_NOTES.md"):
        path = root / name
        if path.is_file() and path.stat().st_size <= 500_000:
            candidates.append((name, path.read_bytes()))
    if log_path and Path(log_path).is_file():
        data = Path(log_path).read_bytes()[-200_000:]
        try:
            text = data.decode("utf-8", errors="replace")
            text = text.replace(str(root), "<PROJECT_ROOT>")
            text = text.replace(str(Path.home()), "<USER_HOME>")
            data = text.encode("utf-8")
        except Exception:
            data = b"Bootstrap log excerpt unavailable after privacy sanitization.\n"
        candidates.append(("current_log_tail.txt", data))
    candidates = candidates[:20]

    fd, temp_name = tempfile.mkstemp(prefix="identity_export_", suffix=".zip", dir=str(temp_dir))
    os.close(fd)
    temp_zip = Path(temp_name)
    try:
        with zipfile.ZipFile(temp_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in sorted(candidates, key=lambda item: item[0].casefold()):
                archive.writestr(name, data)
        with zipfile.ZipFile(temp_zip) as archive:
            if len(archive.infolist()) > 20:
                raise RuntimeError("Support Export20 exceeded 20 entries")
            bad = archive.testzip()
            if bad:
                raise RuntimeError(f"Support Export20 integrity failure at {bad}")
        os.replace(temp_zip, final_zip)
    finally:
        if temp_zip.exists():
            temp_zip.unlink(missing_ok=True)

    digest = _sha256(final_zip)
    sidecar = final_zip.with_suffix(final_zip.suffix + ".sha256.txt")
    sidecar.write_text(f"{digest}  {final_zip.name}\n", encoding="utf-8")
    return final_zip
