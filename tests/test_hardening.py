from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import get_type_hints

import pytest

import mediataggerbot.main as main_module
import mediataggerbot.single_instance as lock_module
import mediataggerbot.utils as utils_module
from mediataggerbot import __version__
from mediataggerbot.config import AppConfig, load_config
from mediataggerbot.main import run_rollback
from mediataggerbot.project_repair import quarantine_legacy_launchers
from mediataggerbot.single_instance import SingleInstanceLock, read_lock_status
from mediataggerbot.utils import atomic_text_writer, sha256_file

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_public_type_annotations_resolve() -> None:
    hints = get_type_hints(main_module.decide_apply)
    assert "genre" in hints


def make_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "Portable Bot Folder"
    (project / "config").mkdir(parents=True)
    config_path = project / "config" / "config.toml"
    config_path.write_text((PROJECT_ROOT / "config" / "config.example.toml").read_text(encoding="utf-8"), encoding="utf-8")
    return project, config_path


def isolated_config(tmp_path: Path) -> AppConfig:
    cfg = load_config(PROJECT_ROOT, PROJECT_ROOT / "config" / "config.toml")
    media_root = tmp_path / "Media Library"
    media_root.mkdir(parents=True)
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
    for directory in (cfg.logs_dir, cfg.exports_dir, cfg.state_dir, cfg.diagnostics_dir, cfg.temp_dir):
        directory.mkdir(parents=True, exist_ok=True)
    return cfg


def test_atomic_text_writer_preserves_existing_destination_on_failure(tmp_path: Path) -> None:
    target = tmp_path / "report.csv"
    target.write_text("known-good\n", encoding="utf-8")
    before = sha256_file(target)

    with pytest.raises(RuntimeError, match="forced writer failure"):
        with atomic_text_writer(target, encoding="utf-8", newline="") as handle:
            handle.write("partial-data")
            raise RuntimeError("forced writer failure")

    assert sha256_file(target) == before
    assert target.read_text(encoding="utf-8") == "known-good\n"
    assert [p for p in tmp_path.iterdir() if p != target] == []


def test_atomic_text_writer_retries_transient_replace_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "report.json"
    real_replace = utils_module.os.replace
    attempts = 0

    def transient_replace(source: str, destination: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError(32, "sharing violation")
        real_replace(source, destination)

    monkeypatch.setattr(utils_module.os, "replace", transient_replace)
    monkeypatch.setattr(utils_module.time, "sleep", lambda _seconds: None)

    with atomic_text_writer(target, encoding="utf-8", newline="\n") as handle:
        handle.write('{"status": "complete"}\n')

    assert attempts == 3
    assert target.read_text(encoding="utf-8") == '{"status": "complete"}\n'


def test_invalid_runtime_paths_fail_closed_to_project_local_directories(tmp_path: Path) -> None:
    project, config_path = make_project(tmp_path)
    text = config_path.read_text(encoding="utf-8")
    text = text.replace('logs_dir = "logs"', 'logs_dir = "C:\\\\Elsewhere\\\\logs"')
    text = text.replace('exports_dir = "exports"', 'exports_dir = "..\\\\outside_exports"')
    config_path.write_text(text, encoding="utf-8")

    cfg = load_config(project, config_path)

    assert cfg.validation_errors
    assert cfg.safe_runtime_dirs is True
    assert cfg.load_status["safe_runtime_dirs_active"] is True
    assert cfg.logs_dir == project / "logs"
    assert cfg.exports_dir == project / "exports"
    assert not (tmp_path / "outside_exports").exists()


def test_control_plane_main_import_does_not_require_media_dependencies() -> None:
    script = r'''
import builtins
import sys
real_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.', 1)[0] in {'mutagen', 'requests'}:
        raise ModuleNotFoundError(name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded
import mediataggerbot.main
assert 'mediataggerbot.scanner' not in sys.modules
print('control-plane-import-ok')
'''
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT_ROOT / "src")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=PROJECT_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "control-plane-import-ok" in completed.stdout


def test_diagnostics_does_not_clobber_active_runtime_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project, config_path = make_project(tmp_path)
    state = project / "state"
    state.mkdir(parents=True)
    status_path = state / "last_run_status.json"
    exit_path = state / "last_run_exit.json"
    status_payload = {"run_id": "active", "status": "running"}
    exit_payload = {"run_id": "active", "completion_class": "in_progress"}
    status_path.write_text(json.dumps(status_payload), encoding="utf-8")
    exit_path.write_text(json.dumps(exit_payload), encoding="utf-8")

    monkeypatch.setattr(main_module, "find_project_root", lambda *_args, **_kwargs: project)
    diagnostic = project / "diagnostics" / "diagnostic.zip"
    diagnostic.parent.mkdir(parents=True)
    diagnostic.write_bytes(b"zip-placeholder")
    monkeypatch.setattr(main_module, "write_diagnostics_export", lambda *_args, **_kwargs: diagnostic)

    code = main_module.main(["--mode", "diagnostics", "--config", str(config_path)])

    assert code == 0
    assert json.loads(status_path.read_text(encoding="utf-8")) == status_payload
    assert json.loads(exit_path.read_text(encoding="utf-8")) == exit_payload


def test_active_run_blocks_repair_before_legacy_launcher_is_moved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project, config_path = make_project(tmp_path)
    legacy = project / "Launch_MediaTaggerBot.ps1"
    legacy.write_text("Write-Host legacy", encoding="utf-8")
    state = project / "state"
    lock = SingleInstanceLock(state / "mediataggerbot.lock", run_id="active", mode="apply-safe")
    lock.acquire()
    try:
        monkeypatch.setattr(main_module, "find_project_root", lambda *_args, **_kwargs: project)
        diagnostic = project / "diagnostics" / "failure.zip"
        diagnostic.parent.mkdir(parents=True, exist_ok=True)
        diagnostic.write_bytes(b"failure")
        monkeypatch.setattr(main_module, "write_diagnostics_export", lambda *_args, **_kwargs: diagnostic)

        code = main_module.main(["--mode", "repair", "--config", str(config_path)])

        assert code == 1
        assert legacy.exists()
        assert not (project / "archive" / "legacy_launchers").exists()
    finally:
        lock.release()


def test_legacy_launcher_quarantine_is_reversible_and_checksum_verified(tmp_path: Path) -> None:
    project = tmp_path / "Bot"
    project.mkdir()
    legacy = project / "Launch_MediaTaggerBot.ps1"
    legacy.write_text("Write-Host legacy launcher", encoding="utf-8")
    original_hash = sha256_file(legacy)

    result = quarantine_legacy_launchers(project, __version__)

    assert result["status"] == "completed"
    assert not legacy.exists()
    assert len(result["moved"]) == 1
    destination = Path(result["moved"][0]["destination"])
    assert destination.exists()
    assert result["moved"][0]["sha256"] == original_hash == sha256_file(destination)
    assert Path(result["manifest"]).exists()
    assert result["media_files_mutated"] is False


def test_rollback_rejects_cross_record_path_graph_before_any_move(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = isolated_config(tmp_path)
    root = cfg.media_root
    assert root is not None
    a = root / "A.mp3"
    b = root / "B.mp3"
    c = root / "C.mp3"
    b.write_bytes(b"B")
    c.write_bytes(b"C")
    manifest = tmp_path / "rollback_manifest_conflict.json"
    manifest.write_text(
        json.dumps(
            [
                {"original_path": str(a), "new_path": str(b)},
                {"original_path": str(b), "new_path": str(c)},
            ]
        ),
        encoding="utf-8",
    )
    dummy_diag = cfg.diagnostics_dir / "diag.zip"
    dummy_diag.write_bytes(b"diag")
    monkeypatch.setattr(main_module, "write_diagnostics_export", lambda *_args, **_kwargs: dummy_diag)
    log = cfg.logs_dir / "rollback.log"
    log.write_text("test", encoding="utf-8")

    code = run_rollback(cfg, str(manifest), "rollback_graph", log)

    assert code == 2
    assert not a.exists()
    assert b.read_bytes() == b"B"
    assert c.read_bytes() == b"C"
    result = json.loads((cfg.exports_dir / "rollback_result_rollback_graph.json").read_text(encoding="utf-8"))
    assert result["media_files_mutated"] is False
    assert any("cross_record_path_overlap" in row.get("error", "") for row in result["results"])


def test_single_instance_detects_pid_reuse_using_process_start_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_path = tmp_path / "lock.json"
    payload = {
        "schema": "MediaTaggerBot.single_instance_lock.v3",
        "pid": 4242,
        "process_start_epoch": 100.0,
        "hostname": socket.gethostname(),
        "owner_token": "owner",
        "heartbeat_epoch": time.time() - 10_000,
    }
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(lock_module, "_pid_alive", lambda _pid: True)
    monkeypatch.setattr(lock_module, "_process_start_epoch", lambda _pid: 200.0)

    stale = read_lock_status(lock_path, stale_after_seconds=60)
    assert stale["active"] is False
    assert stale["stale"] is True
    assert stale["reason"] == "pid_reused_and_heartbeat_stale"

    payload["heartbeat_epoch"] = time.time()
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    recent = read_lock_status(lock_path, stale_after_seconds=60)
    assert recent["active"] is True
    assert recent["reason"] == "recent_heartbeat_but_pid_was_reused"

    monkeypatch.setattr(lock_module, "_process_start_epoch", lambda _pid: 100.5)
    payload["heartbeat_epoch"] = time.time() - 10_000
    lock_path.write_text(json.dumps(payload), encoding="utf-8")
    same_owner = read_lock_status(lock_path, stale_after_seconds=60)
    assert same_owner["active"] is True
    assert same_owner["reason"] == "owner_pid_and_start_time_match"


def test_version_is_consistent_across_launcher_and_package() -> None:
    bat = (PROJECT_ROOT / "Start_MediaTaggerBot.bat").read_text(encoding="utf-8")
    pyproject = (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f"MediaTaggerBot v{__version__}" in bat
    assert f'MEDIATAGGERBOT_LAUNCHER_VERSION={__version__}' in bat
    assert f'.deps_checked_v{__version__}' in bat
    assert f'version = "{__version__}"' in pyproject
    assert "0.5.0" not in bat


def test_rollback_blocks_when_original_and_renamed_paths_both_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = isolated_config(tmp_path)
    root = cfg.media_root
    assert root is not None
    original = root / "Original.mp3"
    renamed = root / "Renamed.mp3"
    original.write_bytes(b"original")
    renamed.write_bytes(b"renamed")
    manifest = tmp_path / "rollback_manifest_both_exist.json"
    manifest.write_text(
        json.dumps([{"original_path": str(original), "new_path": str(renamed)}]),
        encoding="utf-8",
    )
    dummy_diag = cfg.diagnostics_dir / "diag-both.zip"
    dummy_diag.write_bytes(b"diag")
    monkeypatch.setattr(main_module, "write_diagnostics_export", lambda *_args, **_kwargs: dummy_diag)
    log = cfg.logs_dir / "rollback-both.log"
    log.write_text("test", encoding="utf-8")

    code = run_rollback(cfg, str(manifest), "rollback_both", log)

    assert code == 2
    assert original.read_bytes() == b"original"
    assert renamed.read_bytes() == b"renamed"
    result = json.loads((cfg.exports_dir / "rollback_result_rollback_both.json").read_text(encoding="utf-8"))
    assert result["media_files_mutated"] is False
    assert result["results"][0]["error"] == "rollback_path_collision_both_original_and_new_exist"


def test_manual_config_edit_does_not_require_full_media_runtime() -> None:
    bat = (PROJECT_ROOT / "Start_MediaTaggerBot.bat").read_text(encoding="utf-8")
    edit_block = bat.rsplit("\n:editconfig\n", 1)[1].split("\n:openfolder\n", 1)[0]
    assert "call :ensure_runtime" not in edit_block
    assert 'call :runmode validate-config' in edit_block


def test_rollback_blocks_stale_manifest_when_renamed_file_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cfg = isolated_config(tmp_path)
    root = cfg.media_root
    assert root is not None
    original = root / "Original.mp3"
    renamed = root / "Renamed.mp3"
    renamed.write_bytes(b"current-content")
    stat = renamed.stat()
    manifest = tmp_path / "rollback_manifest_stale.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "original_path": str(original),
                    "new_path": str(renamed),
                    "post_apply_size_bytes": stat.st_size + 1,
                    "post_apply_modified_ns": stat.st_mtime_ns,
                }
            ]
        ),
        encoding="utf-8",
    )
    dummy_diag = cfg.diagnostics_dir / "diag-stale.zip"
    dummy_diag.write_bytes(b"diag")
    monkeypatch.setattr(main_module, "write_diagnostics_export", lambda *_args, **_kwargs: dummy_diag)
    log = cfg.logs_dir / "rollback-stale.log"
    log.write_text("test", encoding="utf-8")

    code = run_rollback(cfg, str(manifest), "rollback_stale", log)

    assert code == 2
    assert not original.exists()
    assert renamed.read_bytes() == b"current-content"
    result = json.loads((cfg.exports_dir / "rollback_result_rollback_stale.json").read_text(encoding="utf-8"))
    assert result["results"][0]["error"] == "rollback_source_changed_size_mismatch"


def test_portable_stop_rejects_absolute_and_traversal_runtime_paths(tmp_path: Path) -> None:
    from mediataggerbot.portable_stop import run_portable_stop

    project = tmp_path / "Portable Bot"
    (project / "config").mkdir(parents=True)
    outside_exports = tmp_path / "outside_exports"
    (project / "config" / "config.toml").write_text(
        "[paths]\n"
        "state_dir = 'C:\\\\Outside\\\\state'\n"
        "exports_dir = '../../outside_exports'\n\n"
        "[processing]\n"
        "single_instance_stale_after_seconds = 86400\n",
        encoding="utf-8",
    )

    code, result, evidence = run_portable_stop(project, app_version=__version__, env={})

    assert code == 0
    assert evidence.parent == project / "exports"
    assert result["resolved_runtime_dirs"]["state_dir"] == str((project / "state").resolve())
    assert result["resolved_runtime_dirs"]["exports_dir"] == str((project / "exports").resolve())
    assert result["runtime_path_status"]["state_dir"] == "fallback_rejected_absolute_or_traversal"
    assert result["runtime_path_status"]["exports_dir"] == "fallback_rejected_absolute_or_traversal"
    assert not outside_exports.exists()
