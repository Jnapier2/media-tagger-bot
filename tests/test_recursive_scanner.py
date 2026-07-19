from __future__ import annotations

import os
from pathlib import Path

import pytest

from mediataggerbot.config import load_config
from mediataggerbot.scanner import discover_media_files, scan_media_root

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def make_config(media_root: Path):
    cfg = load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")
    cfg.data["paths"]["media_root"] = str(media_root)
    cfg.data["processing"]["recursive"] = True
    cfg.data["processing"]["require_recursive_scan"] = True
    cfg.data["processing"]["follow_directory_symlinks"] = False
    cfg.data["processing"]["exclude_dir_names"] = []
    cfg.data["processing"]["max_files_per_run"] = 0
    return cfg


def test_recursive_scan_covers_nested_tree(tmp_path: Path):
    (tmp_path / "top.mp3").write_bytes(b"")
    nested = tmp_path / "A" / "B" / "C"
    nested.mkdir(parents=True)
    (nested / "deep.flac").write_bytes(b"")
    (nested / "ignore.txt").write_text("not media", encoding="utf-8")

    cfg = make_config(tmp_path)
    discovered, coverage = discover_media_files(tmp_path, cfg)

    assert sorted(path.name for path, _depth in discovered) == ["deep.flac", "top.mp3"]
    assert coverage.status == "complete_all_subfolders"
    assert coverage.all_reachable_subfolders_checked is True
    assert coverage.directories_visited == 4
    assert coverage.subdirectories_discovered == 3
    assert coverage.deepest_relative_depth == 3
    assert coverage.media_by_depth == {"0": 1, "3": 1}


def test_file_limit_never_claims_complete_coverage(tmp_path: Path):
    (tmp_path / "A").mkdir()
    (tmp_path / "A" / "one.mp3").write_bytes(b"")
    (tmp_path / "B").mkdir()
    (tmp_path / "B" / "two.mp3").write_bytes(b"")
    cfg = make_config(tmp_path)
    cfg.data["processing"]["max_files_per_run"] = 1

    _discovered, coverage = discover_media_files(tmp_path, cfg)

    assert coverage.limit_reached is True
    assert coverage.status == "partial_file_limit"
    assert coverage.all_reachable_subfolders_checked is False


def test_named_exclusion_is_explicit_and_prevents_complete_signal(tmp_path: Path):
    excluded = tmp_path / "DoNotScan"
    excluded.mkdir()
    (excluded / "song.mp3").write_bytes(b"")
    cfg = make_config(tmp_path)
    cfg.data["processing"]["exclude_dir_names"] = ["DoNotScan"]

    discovered, coverage = discover_media_files(tmp_path, cfg)

    assert discovered == []
    assert coverage.directories_excluded == 1
    assert coverage.status == "partial_exclusions"
    assert coverage.all_reachable_subfolders_checked is False


def test_required_recursive_scan_fails_closed(tmp_path: Path):
    cfg = make_config(tmp_path)
    cfg.data["processing"]["recursive"] = False
    with pytest.raises(RuntimeError, match="Recursive scanning is required"):
        discover_media_files(tmp_path, cfg)


def test_directory_symlink_is_reported_when_skipped(tmp_path: Path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "linked.mp3").write_bytes(b"")
    link = tmp_path / "linked-folder"
    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Directory symlinks are unavailable in this environment")

    cfg = make_config(tmp_path)
    _discovered, coverage = discover_media_files(tmp_path, cfg)

    assert coverage.directory_symlinks_skipped == 1
    assert coverage.status == "partial_links_skipped"
    assert coverage.all_reachable_subfolders_checked is False


def test_scan_media_root_marks_discovered_and_scanned_counts(tmp_path: Path):
    (tmp_path / "one.mp3").write_bytes(b"")
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "two.mp4").write_bytes(b"")
    cfg = make_config(tmp_path)

    files, coverage = scan_media_root(tmp_path, cfg)

    assert len(files) == 2
    assert coverage.media_files_found == 2
    assert coverage.media_files_scanned == 2
    assert coverage.finished_utc
    assert coverage.all_reachable_subfolders_checked is True
