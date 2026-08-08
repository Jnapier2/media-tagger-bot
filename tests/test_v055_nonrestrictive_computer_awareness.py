from pathlib import Path

from mediataggerbot.computer_context import (
    detect_computer_context,
    legacy_lock_path,
    local_lock_path,
    local_stop_request_path,
)


def test_known_computer_aliases_are_advisory_only(tmp_path: Path):
    for alias, expected in [
        ("ALPHA", "PC-ALPHA-01"),
        ("Ascend laptop", "PC-ASCEND-02"),
        ("Raider", "PC-DEUSEX-03"),
        ("GE66", "PC-DEUSEX-03"),
    ]:
        context = detect_computer_context(hostname=alias, env={})
        assert context["canonical_id"] == expected
        assert context["advisory_only"] is True
        assert context["cross_computer_startup_blocking"] is False
        assert context["cross_computer_ownership"] is False
        assert context["shared_lease_or_write_fence"] is False
        assert context["forced_read_only"] is False


def test_unknown_computer_uses_generic_defaults_without_exporting_hostname(tmp_path: Path):
    context = detect_computer_context(hostname="UNIQUE-PRIVATE-HOST-123", env={})
    assert context["canonical_id"] == "PC-UNKNOWN"
    assert context["display_name"] == "Unknown computer"
    assert context["safe_generic_defaults"] is True
    assert context["raw_unknown_hostname_exported"] is False
    assert "UNIQUE" not in str(context)


def test_lock_and_stop_files_are_local_profile_scoped(tmp_path: Path):
    alpha = detect_computer_context(hostname="ALPHA", env={})
    ascend = detect_computer_context(hostname="ASCEND", env={})
    assert local_lock_path(tmp_path, alpha) != local_lock_path(tmp_path, ascend)
    assert local_stop_request_path(tmp_path, alpha) != local_stop_request_path(tmp_path, ascend)
    assert legacy_lock_path(tmp_path).name == "mediataggerbot.lock"


def test_foreign_legacy_lock_never_blocks_local_profile_lock(tmp_path: Path):
    import json
    import socket
    import time

    from mediataggerbot.single_instance import SingleInstanceLock

    foreign = legacy_lock_path(tmp_path)
    foreign.write_text(json.dumps({
        "schema": "MediaTaggerBot.single_instance_lock.v3",
        "pid": 999999,
        "hostname": "SOME-OTHER-COMPUTER",
        "owner_token": "foreign",
        "heartbeat_epoch": time.time(),
    }), encoding="utf-8")
    context = detect_computer_context(hostname="ALPHA", env={})
    scoped = local_lock_path(tmp_path, context)
    lock = SingleInstanceLock(scoped, legacy_lock_paths=[foreign], computer_id="PC-ALPHA-01")
    lock.acquire()
    try:
        assert scoped.exists()
        assert foreign.exists()  # never deleted or treated as a shared lease
    finally:
        lock.release()


def test_environment_summary_exposes_only_safe_advisory_context(monkeypatch, tmp_path: Path):
    from copy import deepcopy

    from mediataggerbot.config import AppConfig, DEFAULT_CONFIG
    from mediataggerbot.diagnostics import build_environment_summary

    monkeypatch.setenv("MEDIATAGGERBOT_COMPUTER_OVERRIDE", "ALPHA")
    data = deepcopy(DEFAULT_CONFIG)
    config = AppConfig(
        project_root=tmp_path,
        config_path=tmp_path / "config" / "config.toml",
        data=data,
    )
    summary = build_environment_summary(config, "run", "preflight")
    context = summary["computer_context"]
    assert context["canonical_id"] == "PC-ALPHA-01"
    assert context["advisory_only"] is True
    assert "hostname" not in context
    assert context["cross_computer_startup_blocking"] is False
