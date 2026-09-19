from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from pathlib import Path

from mediataggerbot import __version__
from mediataggerbot.asset_metadata import write_run_asset_manifest
from mediataggerbot.config import AppConfig, DEFAULT_CONFIG

ROOT = Path(__file__).resolve().parents[1]


def test_public_source_sbom_covers_locked_runtime_dependencies() -> None:
    sbom = json.loads((ROOT / "DEPENDENCY_SBOM.json").read_text(encoding="utf-8"))
    assert sbom["schema_version"] == 1
    assert sbom["project"] == "MediaTaggerBot"
    assert sbom["version"] == "0.5.9"
    components = {item["name"]: item["version"] for item in sbom["components"]}
    for name, version in {
        "requests": "2.33.0",
        "mutagen": "1.47.0",
        "charset-normalizer": "3.4.9",
        "idna": "3.18",
        "urllib3": "2.7.0",
        "certifi": "2026.6.17",
    }.items():
        assert components[name] == version
    assert components["pytest"] == "9.0.3"
    assert components["setuptools"] == "83.0.0"
    assert sbom["distribution"].startswith("source-only")
    assert not (ROOT / "wheels").exists()


def test_first_party_rights_notice_is_explicit_and_not_a_license_grant() -> None:
    text = (ROOT / "RIGHTS_NOTICE.txt").read_text(encoding="utf-8")
    assert "Copyright © 2026 Gateway Information Group LLC. All rights reserved." in text
    assert "not a license grant" in text.casefold()
    assert "third-party" in text.casefold()


def test_runtime_asset_manifest_includes_safe_advisory_computer_context(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEDIATAGGERBOT_COMPUTER_OVERRIDE", "ASCEND")
    data = deepcopy(DEFAULT_CONFIG)
    data["paths"].update({
        "exports_dir": "exports",
        "logs_dir": "logs",
        "state_dir": "state",
        "diagnostics_dir": "diagnostics",
        "temp_dir": "temp",
    })
    config = AppConfig(
        project_root=tmp_path,
        config_path=tmp_path / "config" / "config.toml",
        data=data,
    )
    log = tmp_path / "logs" / "run.log"
    log.parent.mkdir(parents=True)
    log.write_text("ok\n", encoding="utf-8")
    paths = write_run_asset_manifest(config, "run_v055", "preflight", assets={"log": log})
    payload = json.loads(paths["asset_manifest_json"].read_text(encoding="utf-8"))
    context = payload["computer_context"]
    assert context["canonical_id"] == "PC-ASCEND-02"
    assert context["advisory_only"] is True
    assert context["cross_computer_startup_blocking"] is False
    assert context["cross_computer_handoff_required"] is False
    assert context["shared_lease_or_write_fence"] is False
    assert "hostname" not in context


def test_environment_verifier_matches_existing_lock_and_sbom() -> None:
    import re
    import tomllib
    from scripts import verify_runtime_environment as verifier

    lock = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8")
    locked = dict(re.findall(r"^([A-Za-z0-9_-]+)==([^\s]+)", lock, re.MULTILINE))
    sbom = json.loads((ROOT / "DEPENDENCY_SBOM.json").read_text(encoding="utf-8"))
    runtime = {item["name"]: item["version"] for item in sbom["components"]
               if item["relationship"] in {"direct-runtime", "transitive-runtime"}}
    assert verifier.EXPECTED == locked == runtime
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for requirement in project["project"]["dependencies"]:
        name, version = requirement.split("==")
        assert verifier.EXPECTED[name] == version


def _synthetic_distribution(tmp_path, version):
    import base64
    data = b"synthetic dependency record, not installed library code\n"
    fixture = tmp_path / "synthetic_record.dat"
    fixture.write_bytes(data)
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).decode().rstrip("=")
    class DistributionFixture:
        def read_text(self, name):
            assert name == "RECORD"
            return f"synthetic_record.dat,sha256={digest},{len(data)}\n"
        def locate_file(self, name):
            assert name == "synthetic_record.dat"
            return fixture
    result = DistributionFixture()
    result.version = version
    return result


def test_environment_accepts_current_locked_requests_record_fixture(tmp_path, monkeypatch) -> None:
    from scripts import verify_runtime_environment as verifier
    fixture = _synthetic_distribution(tmp_path, "2.33.0")
    monkeypatch.setattr(verifier.metadata, "distribution", lambda _name: fixture)
    result = verifier.verify_distribution("requests", verifier.EXPECTED["requests"])
    assert result["version"] == "2.33.0"
    assert result["record_hashes_checked"] == 1


def test_environment_rejects_previous_requests_record_fixture(tmp_path, monkeypatch) -> None:
    import pytest
    from scripts import verify_runtime_environment as verifier
    fixture = _synthetic_distribution(tmp_path, "2.32.5")
    monkeypatch.setattr(verifier.metadata, "distribution", lambda _name: fixture)
    with pytest.raises(RuntimeError, match="expected"):
        verifier.verify_distribution("requests", verifier.EXPECTED["requests"])
