from __future__ import annotations

from pathlib import Path

from mediataggerbot.config import load_config
from mediataggerbot.main import build_already_managed_plan
from mediataggerbot.matcher import Matcher
from mediataggerbot.models import MediaFile

PROJECT_ROOT = Path(__file__).resolve().parents[1]


class FakeMusicBrainz:
    def __init__(self):
        self.recording_calls = 0
        self.isrc_calls = 0

    def lookup_recording(self, mbid: str):
        self.recording_calls += 1
        return {
            "id": mbid,
            "title": "Known Song",
            "artist-credit": [{"artist": {"name": "Known Artist"}, "name": "Known Artist"}],
            "isrcs": ["USABC1234567"],
            "genres": [{"name": "pop", "count": 5}],
            "tags": [],
            "releases": [],
        }

    def lookup_isrc(self, isrc: str):
        self.isrc_calls += 1
        return [{
            "id": "22222222-2222-2222-2222-222222222222",
            "title": "ISRC Song",
            "artist-credit": [{"artist": {"name": "ISRC Artist"}, "name": "ISRC Artist"}],
            "isrcs": [isrc],
            "genres": [{"name": "rock", "count": 3}],
            "tags": [],
            "releases": [],
            "length": 180000,
        }]

    def lookup_release_group(self, _mbid: str):
        return None

    def search_recording(self, _artist, _title, limit=5):
        return []


def config():
    return load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")


def media(tmp_path: Path, name: str = "old.mp3") -> MediaFile:
    path = tmp_path / name
    path.write_bytes(b"")
    return MediaFile(path=path, rel_path=name, extension=".mp3", size_bytes=0, media_kind="audio", duration_seconds=180.0)


def test_embedded_musicbrainz_id_is_used_first(tmp_path: Path):
    cfg = config()
    item = media(tmp_path)
    item.existing_musicbrainz_recording_id = "11111111-1111-1111-1111-111111111111"
    fake = FakeMusicBrainz()
    result = Matcher(cfg, None, fake, None, None).match(item)

    assert result.matched is True
    assert result.source == "musicbrainz_recording_id_tag"
    assert result.confidence == 99.0
    assert fake.recording_calls == 1


def test_embedded_isrc_is_used_without_fingerprint(tmp_path: Path):
    cfg = config()
    item = media(tmp_path)
    item.existing_isrc = "USABC1234567"
    fake = FakeMusicBrainz()
    result = Matcher(cfg, None, fake, None, None).match(item)

    assert result.matched is True
    assert result.source == "musicbrainz_isrc_tag"
    assert result.isrc == "USABC1234567"
    assert fake.isrc_calls == 1


def test_repeat_run_fast_skip_requires_bot_markers_and_exact_name(tmp_path: Path):
    cfg = config()
    item = media(tmp_path, "Known Artist - Known Song - Pop - Dance Pop.mp3")
    item.existing_artist = "Known Artist"
    item.existing_title = "Known Song"
    item.existing_genre = "Pop"
    item.existing_subgenre = "Dance Pop"
    item.existing_mtb_source = "musicbrainz_recording_id_tag"
    item.existing_mtb_version = "0.2.0"
    item.existing_mtb_confidence = 99.0

    plan = build_already_managed_plan(item, cfg)

    assert plan is not None
    assert plan.status == "already_managed_skipped"
    assert plan.action == "fast_skip_already_managed"
