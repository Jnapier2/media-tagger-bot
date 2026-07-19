from __future__ import annotations

import json
from pathlib import Path

import pytest

import mediataggerbot.inventory_cache as inventory_module
import mediataggerbot.scanner as scanner_module
from mediataggerbot.cache import JsonCache
from mediataggerbot.config import AppConfig, load_config
from mediataggerbot.launcher_attestation import build_launcher_attestation
from mediataggerbot.portable_stop import run_portable_stop
from mediataggerbot.scanner import scan_media_root
from mediataggerbot.single_instance import SingleInstanceLock

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def isolated_config(tmp_path: Path) -> AppConfig:
    cfg = load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")
    media_root = tmp_path / "Media Library"
    media_root.mkdir(parents=True, exist_ok=True)
    cfg.data["paths"].update(
        {
            "media_root": str(media_root),
            "logs_dir": str(tmp_path / "logs"),
            "exports_dir": str(tmp_path / "exports"),
            "state_dir": str(tmp_path / "state"),
            "diagnostics_dir": str(tmp_path / "diagnostics"),
            "temp_dir": str(tmp_path / "temp"),
        }
    )
    for directory in [cfg.logs_dir, cfg.exports_dir, cfg.state_dir, cfg.diagnostics_dir, cfg.temp_dir]:
        directory.mkdir(parents=True, exist_ok=True)
    return cfg


def test_inventory_cache_reuses_unchanged_scan_and_invalidates_source_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = isolated_config(tmp_path)
    root = cfg.media_root
    assert root is not None
    path = root / "nested" / "Artist - Song.mp3"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"first")
    calls = {"tags": 0, "duration": 0, "which": 0}

    def fake_tags(_path: Path):
        calls["tags"] += 1
        return {"artist": "Artist", "title": "Song"}

    def fake_duration(_path: Path, _timeout: int):
        calls["duration"] += 1
        return 123.4

    def fake_which(name: str):
        calls["which"] += 1
        return "/tool/ffprobe" if name == "ffprobe" else None

    monkeypatch.setattr(scanner_module, "read_existing_tags", fake_tags)
    monkeypatch.setattr(scanner_module, "ffprobe_duration", fake_duration)
    monkeypatch.setattr(inventory_module, "which", fake_which)
    inventory_module.scanner_capability_signature.cache_clear()
    cache_path = cfg.state_dir / "inventory_cache.sqlite3"

    with JsonCache(cache_path, ttl_days=3650) as cache:
        first, first_coverage = scan_media_root(root, cfg, inventory_cache=cache)
    with JsonCache(cache_path, ttl_days=3650) as cache:
        second, second_coverage = scan_media_root(root, cfg, inventory_cache=cache)

    assert first[0].inventory_cache_hit is False
    assert first_coverage.inventory_cache_misses == 1
    assert first_coverage.inventory_cache_writes == 1
    assert second[0].inventory_cache_hit is True
    assert second_coverage.inventory_cache_hits == 1
    assert second[0].existing_artist == "Artist"
    assert second[0].duration_seconds == 123.4
    assert calls["tags"] == 1
    assert calls["duration"] == 1
    assert calls["which"] == 1  # capability probe is process-cached, not repeated per file/run

    path.write_bytes(b"second-version")
    with JsonCache(cache_path, ttl_days=3650) as cache:
        changed, changed_coverage = scan_media_root(root, cfg, inventory_cache=cache)
    assert changed[0].inventory_cache_hit is False
    assert changed_coverage.inventory_cache_misses == 1
    assert calls["tags"] == 2
    inventory_module.scanner_capability_signature.cache_clear()


def test_launcher_attestation_confirms_bat_and_rejects_stale_version(tmp_path: Path) -> None:
    project = tmp_path / "Bot Folder"
    batch_log = project / "logs" / "batch_runs" / "run.txt"
    env = {
        "MEDIATAGGERBOT_LAUNCHER_KIND": "bat_menu",
        "MEDIATAGGERBOT_LAUNCHER_VERSION": "0.5.1",
        "MEDIATAGGERBOT_LAUNCHER_PROJECT_ROOT": str(project),
        "MEDIATAGGERBOT_BATCH_LOG": str(batch_log),
    }
    confirmed = build_launcher_attestation(project, "0.5.1", env)
    assert confirmed["status"] == "confirmed_bat"
    assert confirmed["confirmed"] is True
    assert confirmed["safe_to_process"] is True

    stale = build_launcher_attestation(project, "0.5.2", env)
    assert stale["status"] == "launcher_mismatch"
    assert stale["safe_to_process"] is False
    assert "launcher_version_mismatch" in stale["reasons"]


def test_launcher_attestation_allows_direct_python(tmp_path: Path) -> None:
    result = build_launcher_attestation(tmp_path, "0.5.1", {})
    assert result["status"] == "direct_python"
    assert result["confirmed"] is True
    assert result["safe_to_process"] is True


def test_portable_stop_never_modifies_virtual_environment(tmp_path: Path) -> None:
    project = tmp_path / "Portable Bot"
    state = project / "state"
    exports = project / "exports"
    config = project / "config"
    config.mkdir(parents=True)
    (config / "config.toml").write_text(
        '[paths]\nstate_dir = "state"\nexports_dir = "exports"\n\n'
        '[processing]\nsingle_instance_stale_after_seconds = 86400\n',
        encoding="utf-8",
    )
    venv_sentinel = project / ".venv" / "sentinel.txt"
    venv_sentinel.parent.mkdir(parents=True)
    venv_sentinel.write_text("must-not-change", encoding="utf-8")
    lock = SingleInstanceLock(state / "mediataggerbot.lock", run_id="active", mode="dry-run")
    lock.acquire()
    try:
        code, result, evidence = run_portable_stop(project, app_version="0.5.1", env={})
        assert code == 0
        assert result["status"] == "requested"
        assert result["runtime_setup_attempted"] is False
        assert result["virtual_environment_modified"] is False
        assert result["dependency_install_attempted"] is False
        assert venv_sentinel.read_text(encoding="utf-8") == "must-not-change"
        assert evidence.parent == exports
        payload = json.loads(evidence.read_text(encoding="utf-8"))
        assert payload["target_run_id"] == "active"
    finally:
        lock.release()


def test_portable_stop_survives_malformed_config(tmp_path: Path) -> None:
    project = tmp_path / "Portable Bot"
    (project / "config").mkdir(parents=True)
    (project / "config" / "config.toml").write_text('media_root = "D:\\Music"\nbroken = "\\q"\n', encoding="utf-8")
    code, result, evidence = run_portable_stop(project, app_version="0.5.1", env={})
    assert code == 0
    assert result["status"] == "no_active_run"
    assert result["config_read_status"] == "invalid_fallback_defaults"
    assert evidence.exists()
    assert not (project / ".venv").exists()
