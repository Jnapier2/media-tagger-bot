from __future__ import annotations

import hashlib
import json
from pathlib import Path

from mediataggerbot import __build_id__, __package_id__, __version__
from mediataggerbot.package_identity import verify_runtime_identity

ROOT = Path(__file__).resolve().parents[1]

def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def test_public_release_manifest_is_unique_and_verified() -> None:
    manifest = json.loads((ROOT / "MANIFEST.json").read_text(encoding="utf-8"))
    assert manifest["metadata_schema"] == "asset-metadata-v1"
    assert manifest["package_id"] == __package_id__
    assert manifest["version"] == f"v{__version__}"
    assert manifest["build_id"] == __build_id__
    records = manifest["files"]
    paths = [row["path"] for row in records]
    assert len(paths) == len(set(path.casefold() for path in paths))
    assert any(row["package_managed"] for row in records)
    for row in records:
        assert isinstance(row["package_managed"], bool)
        path = ROOT / row["path"]
        if row["path"] == "MANIFEST.json":
            assert row["package_managed"] is False
            continue
        assert path.is_file()
        if row["package_managed"]:
            assert path.stat().st_size == row["size_bytes"]
            assert _sha(path) == row["sha256"]

def test_checked_out_public_source_passes_runtime_identity_gate() -> None:
    status = verify_runtime_identity(ROOT, __version__, __build_id__)
    assert status["gate_result"] == "PASS", status["mismatches"]
    assert status["package_verified_count"] == status["package_managed_count"]
    assert status["config_or_credentials_loaded_before_gate"] is False
