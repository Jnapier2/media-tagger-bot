#!/usr/bin/env python3
"""Verify MediaTaggerBot's project-local Python environment without network access.

Copyright © 2026 Gateway Information Group LLC. All rights reserved.
"""
from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import json
import platform
import re
import sys
from importlib import metadata
from pathlib import Path
from typing import Any

EXPECTED = {
    "requests": "2.32.5",
    "mutagen": "1.47.0",
    "charset-normalizer": "3.4.9",
    "idna": "3.18",
    "urllib3": "2.7.0",
    "certifi": "2026.6.17",
}
SUPPORTED_PYTHON = {(3, 11), (3, 12), (3, 13), (3, 14)}
LOCK_HASH_RE = re.compile(r"--hash=sha256:([0-9a-fA-F]{64})")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_distribution(name: str, expected_version: str) -> dict[str, Any]:
    dist = metadata.distribution(name)
    actual_version = dist.version
    if actual_version != expected_version:
        raise RuntimeError(f"{name} version {actual_version!r} != expected {expected_version!r}")
    record_text = dist.read_text("RECORD")
    if not record_text:
        raise RuntimeError(f"{name} has no installed RECORD metadata")
    checked = 0
    missing = 0
    mismatched = 0
    for row in csv.reader(record_text.splitlines()):
        if len(row) < 2 or not row[1].startswith("sha256="):
            continue
        relative, encoded_hash = row[0], row[1].split("=", 1)[1]
        path = Path(dist.locate_file(relative))
        if not path.is_file():
            missing += 1
            continue
        expected = base64.urlsafe_b64decode(encoded_hash + "=" * (-len(encoded_hash) % 4)).hex()
        actual = sha256_file(path)
        checked += 1
        if actual != expected:
            mismatched += 1
    if checked == 0 or missing or mismatched:
        raise RuntimeError(
            f"{name} RECORD verification failed: checked={checked} missing={missing} mismatched={mismatched}"
        )
    return {"name": name, "version": actual_version, "record_hashes_checked": checked}


def verify(project_root: Path) -> dict[str, Any]:
    project_root = project_root.resolve()
    if sys.version_info[:2] not in SUPPORTED_PYTHON:
        raise RuntimeError(f"unsupported Python ABI {sys.version_info.major}.{sys.version_info.minor}")
    machine = platform.machine().casefold()
    if machine not in {"amd64", "x86_64"}:
        raise RuntimeError(f"unsupported machine architecture {platform.machine()!r}")

    lock_path = project_root / "requirements.lock.txt"
    wheels_dir = project_root / "wheels"
    if not lock_path.is_file() or not wheels_dir.is_dir():
        raise RuntimeError("requirements.lock.txt or wheels/ is missing")
    lock_text = lock_path.read_text(encoding="utf-8")
    allowed_hashes = {value.casefold() for value in LOCK_HASH_RE.findall(lock_text)}
    if not allowed_hashes:
        raise RuntimeError("requirements.lock.txt contains no SHA256 hashes")

    wheel_records: list[dict[str, Any]] = []
    for wheel in sorted(wheels_dir.glob("*.whl")):
        digest = sha256_file(wheel)
        if digest.casefold() not in allowed_hashes:
            raise RuntimeError(f"bundled wheel hash is not authorized by requirements.lock.txt: {wheel.name}")
        wheel_records.append({"name": wheel.name, "sha256": digest, "size_bytes": wheel.stat().st_size})
    if not wheel_records:
        raise RuntimeError("bundled wheels directory is empty")

    distributions = [verify_distribution(name, version) for name, version in EXPECTED.items()]
    pyproject_path = project_root / "pyproject.toml"
    pyproject_text = pyproject_path.read_text(encoding="utf-8") if pyproject_path.is_file() else ""
    version_match = re.search(r'^version\s*=\s*"([^"]+)"', pyproject_text, flags=re.MULTILINE)
    application_version = version_match.group(1) if version_match else "unknown"

    material = {
        "schema": "MediaTaggerBot.runtime_environment_attestation.v1",
        "application_version": application_version,
        "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        "abi": f"cp{sys.version_info.major}{sys.version_info.minor}",
        "architecture": platform.machine(),
        "requirements_lock_sha256": sha256_file(lock_path),
        "wheels": wheel_records,
        "distributions": distributions,
        "status": "verified",
    }
    canonical = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    material["attestation_sha256"] = hashlib.sha256(canonical).hexdigest()
    return material


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", required=True)
    parser.add_argument("--marker")
    parser.add_argument("--write-marker", action="store_true")
    args = parser.parse_args()
    try:
        result = verify(Path(args.project_root))
        if args.write_marker:
            if not args.marker:
                raise RuntimeError("--write-marker requires --marker")
            marker = Path(args.marker)
            marker.parent.mkdir(parents=True, exist_ok=True)
            temp = marker.with_suffix(marker.suffix + ".tmp")
            temp.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            temp.replace(marker)
        return 0
    except Exception as exc:
        print(f"Runtime environment verification failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
