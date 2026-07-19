from __future__ import annotations

import tomllib
from pathlib import Path

from mediataggerbot.config import load_config, load_config_resilient
from mediataggerbot.pathing import (
    attempt_repair_invalid_media_root,
    clean_user_path,
    toml_quote_path,
    update_media_root_in_config,
)


def _example_config() -> str:
    return """\
[project]
name = "MediaTaggerBot"
timezone = "America/Chicago"
contact = "local-user@example.invalid"

[paths]
media_root = ""
logs_dir = "logs"
exports_dir = "exports"
state_dir = "state"
diagnostics_dir = "diagnostics"
temp_dir = "temp"
"""


def test_clean_user_path_recovers_windows_trailing_quote() -> None:
    assert clean_user_path(chr(34) + "D:\\" + chr(34)) == "D:\\"
    assert clean_user_path("D:\\" + chr(34)) == "D:\\"
    assert clean_user_path(chr(34) + "D:\\Music Videos" + chr(34)) == "D:\\Music Videos"


def test_toml_path_quote_round_trips_windows_paths() -> None:
    for value in ["D:\\", "D:\\Music", "D:\\Music Videos", "\\\\server\\share\\"]:
        parsed = tomllib.loads(f"[paths]\nmedia_root = {toml_quote_path(value)}\n")
        assert parsed["paths"]["media_root"] == value
    apostrophe = "D:\\DJ's Music"
    parsed = tomllib.loads(f"[paths]\nmedia_root = {toml_quote_path(apostrophe)}\n")
    assert parsed["paths"]["media_root"] == apostrophe


def test_update_media_root_writes_literal_and_exact_value(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(_example_config(), encoding="utf-8")
    result = update_media_root_in_config(config_path, "D:\\", backup=True)
    assert result["new_media_root"] == "D:\\"
    assert result["toml_representation"] == "'D:\\'"
    assert Path(result["backup_path"]).exists()
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["paths"]["media_root"] == "D:\\"


def test_narrow_repair_fixes_unescaped_windows_media_root(tmp_path: Path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        _example_config().replace('media_root = ""', 'media_root = "D:\\Music"'),
        encoding="utf-8",
    )
    result = attempt_repair_invalid_media_root(config_path)
    assert result["repaired"] is True
    assert result["recovered_media_root"] == "D:\\Music"
    assert Path(result["backup_path"]).exists()
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["paths"]["media_root"] == "D:\\Music"
    assert parsed["project"]["name"] == "MediaTaggerBot"


def test_diagnostics_load_uses_fallback_without_mutating_invalid_config(tmp_path: Path) -> None:
    project = tmp_path / "bot"
    (project / "config").mkdir(parents=True)
    config_path = project / "config" / "config.toml"
    invalid = _example_config().replace('media_root = ""', 'media_root = "D:\\Music"')
    config_path.write_text(invalid, encoding="utf-8")
    before = config_path.read_bytes()
    cfg = load_config_resilient(project_root=project, config_path=config_path, mode="diagnostics")
    assert cfg.load_status["status"] == "fallback_invalid_config"
    assert config_path.read_bytes() == before


def test_preflight_load_auto_repairs_only_media_root_syntax(tmp_path: Path) -> None:
    project = tmp_path / "bot"
    (project / "config").mkdir(parents=True)
    config_path = project / "config" / "config.toml"
    config_path.write_text(
        _example_config().replace('media_root = ""', 'media_root = "D:\\Music"'),
        encoding="utf-8",
    )
    cfg = load_config_resilient(project_root=project, config_path=config_path, mode="preflight")
    assert cfg.load_status["status"] == "loaded_after_auto_path_repair"
    assert cfg.get("paths.media_root") == "D:\\Music"
    assert list((project / "config" / "backups").glob("config_before_auto_path_repair_*.toml.bak"))


def test_set_root_can_rebuild_unrelated_invalid_config_from_example(tmp_path: Path) -> None:
    project = tmp_path / "bot"
    config_dir = project / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.toml"
    example_path = config_dir / "config.example.toml"
    example_path.write_text(_example_config(), encoding="utf-8")
    config_path.write_text("[paths]\nmedia_root = \"D:\\Music\"\nbroken = [\n", encoding="utf-8")
    result = update_media_root_in_config(
        config_path,
        "D:\\New Music",
        backup=True,
        example_path=example_path,
        allow_rebuild_from_example=True,
    )
    assert result["rebuilt_from_example"] is True
    cfg = load_config(project_root=project, config_path=config_path)
    assert cfg.get("paths.media_root") == "D:\\New Music"
    assert Path(result["backup_path"]).exists()


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "bot"
    config_dir = project / "config"
    config_dir.mkdir(parents=True)
    (project / "Start_MediaTaggerBot.bat").write_text("@echo off\n", encoding="utf-8")
    (config_dir / "config.example.toml").write_text(_example_config(), encoding="utf-8")
    return project


def test_diagnostics_mode_survives_invalid_config_without_repair(tmp_path: Path, monkeypatch) -> None:
    import mediataggerbot.main as main_module

    project = _make_project(tmp_path)
    config_path = project / "config" / "config.toml"
    invalid = _example_config().replace('media_root = ""', 'media_root = "D:\\Music"')
    config_path.write_text(invalid, encoding="utf-8")
    before = config_path.read_bytes()
    monkeypatch.setattr(main_module, "find_project_root", lambda _start=None: project)
    assert main_module.main(["--mode", "diagnostics"]) == 0
    assert config_path.read_bytes() == before
    zips = list((project / "diagnostics").glob("MediaTaggerBot_DIAGNOSTIC_*.zip"))
    assert zips


def test_set_root_mode_recovers_invalid_config_via_environment_transport(tmp_path: Path, monkeypatch) -> None:
    import mediataggerbot.main as main_module

    project = _make_project(tmp_path)
    config_path = project / "config" / "config.toml"
    config_path.write_text("[paths]\nmedia_root = \"D:\\Music\"\nbroken = [\n", encoding="utf-8")
    monkeypatch.setattr(main_module, "find_project_root", lambda _start=None: project)
    monkeypatch.setenv("MEDIATAGGERBOT_ROOT_OVERRIDE", "D:\\")
    assert main_module.main(["--mode", "set-root"]) == 0
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["paths"]["media_root"] == "D:\\"
    assert list((project / "config" / "backups").glob("config_before_media_root_*.toml.bak"))


def test_validate_config_restores_pre_edit_backup_for_unrelated_syntax_error(tmp_path: Path) -> None:
    from mediataggerbot.main import run_validate_config

    project = _make_project(tmp_path)
    config_path = project / "config" / "config.toml"
    backup_path = project / "config" / "backups" / "before_edit.toml.bak"
    backup_path.parent.mkdir(parents=True)
    good = _example_config().replace('media_root = ""', "media_root = '/tmp/music'")
    backup_path.write_text(good, encoding="utf-8")
    config_path.write_text("[paths]\nmedia_root = '/tmp/music'\nbroken = [\n", encoding="utf-8")
    cfg = load_config_resilient(project_root=project, config_path=config_path, mode="validate-config")
    assert cfg.load_status["status"] == "fallback_invalid_config"
    log_path = project / "logs" / "edit.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("validation test\n", encoding="utf-8")
    code = run_validate_config(cfg, str(backup_path), "test_validate", log_path)
    assert code == 2
    parsed = tomllib.loads(config_path.read_text(encoding="utf-8"))
    assert parsed["paths"]["media_root"] == "/tmp/music"
    assert list((project / "config" / "backups").glob("config_rejected_*.toml"))
