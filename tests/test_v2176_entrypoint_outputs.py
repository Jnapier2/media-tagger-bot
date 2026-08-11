from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from mediataggerbot.config import find_project_root

ROOT = Path(__file__).resolve().parents[1]


def test_project_root_is_anchored_to_package_not_caller_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    foreign = tmp_path / "foreign-cwd"
    foreign.mkdir()
    (foreign / "Start_MediaTaggerBot.bat").write_text("fake", encoding="utf-8")
    monkeypatch.chdir(foreign)
    assert find_project_root(Path(__file__)) == ROOT


def test_missing_package_anchor_fails_closed_even_if_cwd_looks_valid(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    foreign = tmp_path / "foreign-cwd"
    foreign.mkdir()
    (foreign / "Start_MediaTaggerBot.bat").write_text("fake", encoding="utf-8")
    orphan = tmp_path / "orphan" / "module.py"
    orphan.parent.mkdir()
    orphan.write_text("# no project markers", encoding="utf-8")
    monkeypatch.chdir(foreign)
    with pytest.raises(RuntimeError, match="project root could not be resolved"):
        find_project_root(orphan)


def test_execution_and_output_metadata_is_v2176_aligned() -> None:
    metadata = json.loads((ROOT / "PACKAGE_METADATA.json").read_text(encoding="utf-8"))
    execution = metadata["execution_identity"]
    outputs = metadata["project_local_outputs"]
    assert "source_baseline_sha256" not in metadata
    assert metadata["source_baseline_commit_hash_algorithm"] == "git-sha1"
    assert re.fullmatch(r"[0-9a-f]{40}", metadata["source_baseline_commit_sha"])
    assert execution["namespace"] == "MediaTaggerBot"
    assert execution["canonical_entrypoint"] == "Start_MediaTaggerBot.bat"
    assert execution["entrypoint_is_stable_unversioned_project_qualified"] is True
    assert execution["backend_target"] == "python -m mediataggerbot"
    assert outputs["caller_current_working_directory_is_authority"] is False
    assert "logs" in outputs["runtime_owned_roots"]
    assert "exports" in outputs["runtime_owned_roots"]
    assert "diagnostics" in outputs["runtime_owned_roots"]
    for relative in outputs["runtime_owned_roots"]:
        path = Path(relative)
        assert not path.is_absolute()
        assert ".." not in path.parts


def test_canonical_launcher_is_project_local_and_unversioned() -> None:
    launcher = ROOT / "Start_MediaTaggerBot.bat"
    text = launcher.read_text(encoding="utf-8")
    assert launcher.is_file()
    assert "%~dp0" in text
    assert 'cd /d "%PROJECT_ROOT%"' in text
    assert "0.5.9" not in launcher.name
    assert launcher.name.casefold() not in {"run.bat", "start.bat", "main.bat", "app.bat", "bot.bat", "menu.bat"}
