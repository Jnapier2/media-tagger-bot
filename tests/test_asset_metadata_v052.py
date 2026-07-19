from __future__ import annotations

import json
from pathlib import Path

from mutagen.id3 import ID3

from mediataggerbot.asset_metadata import (
    ASSET_METADATA_SCHEMA,
    media_asset_metadata,
    write_run_asset_manifest,
)
from mediataggerbot.config import load_config
from mediataggerbot import metadata as metadata_module
from mediataggerbot.metadata import write_metadata, write_mp4
from mediataggerbot.models import GenreResult, MatchResult

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _match(**updates) -> MatchResult:
    values = dict(
        matched=True,
        confidence=99.0,
        source="musicbrainz_recording_id_tag",
        artist="Artist",
        title="Title",
        isrc="USABC1234567",
        musicbrainz_recording_id="11111111-1111-1111-1111-111111111111",
        acoustid_id="22222222-2222-2222-2222-222222222222",
        identity_tier="stable_id",
    )
    values.update(updates)
    return MatchResult(**values)


def _genre() -> GenreResult:
    return GenreResult("Pop", "Pop", "Dance Pop", ["dance pop"], "test", 95.0)


def test_media_asset_id_prefers_stable_repository_identity(tmp_path: Path):
    meta = media_asset_metadata(_match(), _genre(), tmp_path / "song.mp3")
    assert meta["asset_id"] == "musicbrainz-recording:11111111-1111-1111-1111-111111111111"
    assert meta["metadata_schema"] == ASSET_METADATA_SCHEMA
    assert meta["asset_status"] == "current-managed"
    assert "Dance Pop" in meta["asset_tags"]


def test_media_asset_id_does_not_invent_path_identity(tmp_path: Path):
    meta = media_asset_metadata(
        _match(musicbrainz_recording_id=None, isrc=None, acoustid_id=None, source="filename_fallback"),
        _genre(),
        tmp_path / "song.mp3",
    )
    assert meta["asset_id"] == ""
    assert "without stable identifier" in meta["asset_lineage"]


def test_run_asset_manifest_uses_project_relative_paths_and_checksums(tmp_path: Path):
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True)
    (project / "exports").mkdir()
    (project / "logs").mkdir()
    (project / "diagnostics").mkdir()
    (project / "state").mkdir()
    (project / "temp").mkdir()
    config_path = project / "config" / "config.toml"
    config_path.write_text(
        "[project]\nname='MediaTaggerBot'\ntimezone='America/Chicago'\ncontact='test@example.invalid'\n"
        "[paths]\nmedia_root=''\nlogs_dir='logs'\nexports_dir='exports'\nstate_dir='state'\ndiagnostics_dir='diagnostics'\ntemp_dir='temp'\n",
        encoding="utf-8",
    )
    cfg = load_config(project_root=project, config_path=config_path)
    run_id = "20260713_120000_UTC_dry_run"
    run_dir = cfg.exports_dir / run_id
    run_dir.mkdir(parents=True)
    summary = run_dir / f"summary_{run_id}.json"
    summary.write_text('{"ok": true}\n', encoding="utf-8")
    log = cfg.logs_dir / f"run_{run_id}.log"
    log.write_text("done\n", encoding="utf-8")

    paths = write_run_asset_manifest(
        cfg,
        run_id,
        "dry-run",
        assets={"summary_json": summary, "log": log},
        terminal_status="completed",
    )
    payload = json.loads(paths["asset_manifest_json"].read_text(encoding="utf-8"))
    assert payload["metadata_schema"] == ASSET_METADATA_SCHEMA
    assert payload["file_count"] == 4
    assert all(not Path(item["path"]).is_absolute() for item in payload["files"])
    summary_record = next(item for item in payload["files"] if item["role"] == "summary_json")
    assert summary_record["sha256"]
    assert summary_record["checksum_status"] == "verified"
    self_record = next(item for item in payload["files"] if item["role"] == "asset_manifest_json")
    assert self_record["sha256"] is None
    assert self_record["checksum_status"] == "self_referential_not_practical"


def test_id3_writer_adds_asset_metadata(tmp_path: Path):
    cfg = load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")
    path = tmp_path / "song.mp3"
    path.write_bytes(b"")
    wrote, error, _ = write_metadata(path, _match(), _genre(), cfg)
    assert wrote is True and error is None
    tags = ID3(path)
    assert str(tags["TXXX:MediaTaggerBot Asset Id"]) == "musicbrainz-recording:11111111-1111-1111-1111-111111111111"
    assert str(tags["TXXX:MediaTaggerBot Asset Status"]) == "current-managed"
    assert str(tags["TXXX:MediaTaggerBot Metadata Schema"]) == ASSET_METADATA_SCHEMA


def test_discovery_keeps_multiple_run_reports(tmp_path: Path):
    from mediataggerbot.asset_metadata import discover_run_assets
    project = tmp_path / "project"
    for name in ["config", "exports", "logs", "diagnostics", "state", "temp"]:
        (project / name).mkdir(parents=True, exist_ok=True)
    config_path = project / "config" / "config.toml"
    config_path.write_text(
        "[project]\nname='MediaTaggerBot'\ntimezone='America/Chicago'\ncontact='test@example.invalid'\n"
        "[paths]\nmedia_root=''\nlogs_dir='logs'\nexports_dir='exports'\nstate_dir='state'\ndiagnostics_dir='diagnostics'\ntemp_dir='temp'\n",
        encoding="utf-8",
    )
    cfg = load_config(project_root=project, config_path=config_path)
    run_id = "20260713_130000_UTC_dry_run"
    run_dir = cfg.exports_dir / run_id
    run_dir.mkdir()
    (run_dir / f"summary_{run_id}.json").write_text("{}", encoding="utf-8")
    (run_dir / f"summary_{run_id}.html").write_text("<html></html>", encoding="utf-8")
    (run_dir / f"needs_review_{run_id}.csv").write_text("a\n", encoding="utf-8")
    assets = discover_run_assets(cfg, run_id)
    assert {key.split("__", 1)[0] for key in assets} >= {"summary_json", "summary_html", "needs_review_csv"}


def test_mp4_writer_adds_asset_metadata(monkeypatch, tmp_path: Path):
    class FakeMP4:
        last = None

        def __init__(self, _path: str):
            self.tags = {}
            self.saved = False
            FakeMP4.last = self

        def add_tags(self):
            self.tags = {}

        def __setitem__(self, key, value):
            self.tags[key] = value

        def __contains__(self, key):
            return key in self.tags

        def __delitem__(self, key):
            del self.tags[key]

        def save(self):
            self.saved = True

    monkeypatch.setattr(metadata_module, "MP4", FakeMP4)
    cfg = load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")
    asset = media_asset_metadata(_match(), _genre(), tmp_path / "song.mp4")
    write_mp4(tmp_path / "song.mp4", _match(), _genre(), cfg, "evidence", "2026-07-13T00:00:00Z", asset)

    assert FakeMP4.last is not None and FakeMP4.last.saved is True
    tags = FakeMP4.last.tags
    prefix = "----:com.apple.iTunes:"
    assert bytes(tags[prefix + "MediaTaggerBot Asset Id"][0]).decode() == asset["asset_id"]
    assert bytes(tags[prefix + "MediaTaggerBot Asset Status"][0]).decode() == "current-managed"
    assert bytes(tags[prefix + "MediaTaggerBot Metadata Schema"][0]).decode() == ASSET_METADATA_SCHEMA
