from __future__ import annotations

from pathlib import Path

from mediataggerbot.cache import JsonCache
from mediataggerbot.config import load_config
from mediataggerbot.main import decide_apply
from mediataggerbot.matcher import Matcher
from mediataggerbot.models import GenreResult, MatchResult, MediaFile

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def cfg():
    value = load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")
    value.data["apis"]["enable_lastfm"] = False
    value.data["apis"]["enable_discogs"] = False
    value.data["matching"]["musicbrainz_artist_genre_fallback"] = False
    return value


def candidate(recording_id: str, score: int = 100, *, artist: str = "Artist", title: str = "Song", video: bool = False):
    return {
        "id": recording_id,
        "score": score,
        "title": title,
        "length": 180000,
        "video": video,
        "artist-credit": [
            {
                "name": artist,
                "artist": {"id": "artist-id", "name": artist},
                "joinphrase": "",
            }
        ],
        "releases": [],
        "genres": [{"name": "pop", "count": 1}],
        "tags": [],
    }


class FakeMusicBrainz:
    def __init__(self, results):
        self.results = results

    def search_recording(self, _artist, _title, limit=5):
        return self.results[:limit]

    def lookup_recording(self, mbid):
        return next((item for item in self.results if item["id"] == mbid), None)

    def lookup_release_group(self, _mbid):
        return None

    def lookup_artist(self, _mbid):
        return None


def media(tmp_path: Path, *, kind: str = "audio", name: str = "Artist - Song.mp3") -> MediaFile:
    path = tmp_path / name
    path.write_bytes(b"")
    return MediaFile(
        path=path,
        rel_path=path.name,
        extension=path.suffix,
        size_bytes=0,
        media_kind=kind,
        duration_seconds=180.0,
    )


def test_close_musicbrainz_runner_up_blocks_apply_safe(tmp_path: Path):
    config = cfg()
    fake = FakeMusicBrainz([candidate("one", 100), candidate("two", 99)])
    result = Matcher(config, None, fake, None, None).match(media(tmp_path))

    assert result.musicbrainz_recording_id == "one"
    assert result.ambiguity_status == "ambiguous_close_candidates"
    assert "ambiguous_text_candidates" in result.apply_blockers
    assert decide_apply("apply-safe", result, config) == (False, "ambiguous_identity_review_only")


def test_clear_single_repository_candidate_can_apply_safe(tmp_path: Path):
    config = cfg()
    item = media(tmp_path, name="Example Band - Distinct Song.mp3")
    item.existing_artist = "Example Band"
    item.existing_title = "Distinct Song"
    result = Matcher(
        config,
        None,
        FakeMusicBrainz([candidate("one", artist="Example Band", title="Distinct Song")]),
        None,
        None,
    ).match(item)

    mapped_genre = GenreResult(
        main_genre="Pop",
        filename_main_genre="Pop",
        subgenre=None,
        raw_terms=["pop"],
        source="database_terms",
        confidence=90.0,
    )
    should_apply, action = decide_apply("apply-safe", result, config, mapped_genre)
    assert result.ambiguity_status == "single_candidate"
    assert should_apply is True
    assert action == "apply_safe"


def test_video_recording_flag_is_a_tiebreaker_for_video_media(tmp_path: Path):
    config = cfg()
    fake = FakeMusicBrainz([
        candidate("audio-recording", 100, video=False),
        candidate("video-recording", 100, video=True),
    ])
    result = Matcher(config, None, fake, None, None).match(media(tmp_path, kind="video", name="Artist - Song.mp4"))

    assert result.musicbrainz_recording_id == "video-recording"
    ranking = result.evidence["text_candidate_ranking"]
    assert ranking[0]["candidate_video"] is True
    assert ranking[0]["media_kind_adjustment"] == 2.0


def test_named_remix_evidence_selects_correct_candidate(tmp_path: Path):
    config = cfg()
    fake = FakeMusicBrainz([
        candidate("wrong", 100, title="Song (Armin Remix)"),
        candidate("right", 98, title="Song (Tiësto Remix)"),
    ])
    item = media(tmp_path, name="Artist - Song (Tiesto Remix).mp3")
    result = Matcher(config, None, fake, None, None).match(item)

    assert result.musicbrainz_recording_id == "right"
    assert "material_version_mismatch" not in result.version_evidence


def test_identity_memory_rejects_ambiguous_results_but_keeps_exact_identity(tmp_path: Path):
    config = cfg()
    item = media(tmp_path)
    item.fingerprint = "fingerprint"
    item.fingerprint_duration = 180
    with JsonCache(tmp_path / "cache.sqlite3") as cache:
        matcher = Matcher(config, None, None, None, None, cache=cache)
        ambiguous = MatchResult(
            matched=True,
            confidence=95.0,
            source="musicbrainz_search_from_filename_parse",
            artist="Artist",
            title="Song",
            musicbrainz_recording_id="one",
            identity_tier="text_ambiguous",
            ambiguity_status="ambiguous_close_candidates",
            apply_blockers=["ambiguous_text_candidates"],
        )
        matcher._store_identity_memory(ambiguous, item)
        assert matcher.identity_memory_stats["writes"] == 0

        exact = MatchResult(
            matched=True,
            confidence=99.0,
            source="musicbrainz_recording_id_tag",
            artist="Artist",
            title="Song",
            musicbrainz_recording_id="one",
            identity_tier="stable_identifier",
            ambiguity_status="exact_stable_identifier",
        )
        matcher._store_identity_memory(exact, item)
        assert matcher.identity_memory_stats["writes"] >= 1



class FakeMusicBrainzArtistGenre(FakeMusicBrainz):
    def lookup_artist(self, _mbid):
        return {
            "id": "artist-id",
            "genres": [{"name": "rock", "count": 10}],
            "tags": [{"name": "alternative rock", "count": 5}],
        }


def test_musicbrainz_artist_genre_is_a_bounded_last_resort(tmp_path: Path):
    config = cfg()
    config.data["matching"]["musicbrainz_artist_genre_fallback"] = True
    item = candidate("one")
    item["genres"] = []
    item["tags"] = []
    result = Matcher(config, None, FakeMusicBrainzArtistGenre([item]), None, None).match(media(tmp_path))

    assert "rock" in result.raw_genres
    assert result.evidence["musicbrainz_artist_genre_fallback"]["artist_id"] == "artist-id"
