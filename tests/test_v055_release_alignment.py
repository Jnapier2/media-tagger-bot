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
