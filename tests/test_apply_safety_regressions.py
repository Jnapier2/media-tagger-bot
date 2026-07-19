from __future__ import annotations

from pathlib import Path

import requests

from mediataggerbot.cache import JsonCache
from mediataggerbot.config import load_config
from mediataggerbot.genre import classify_genre
from mediataggerbot.databases import ApiClientBase, MusicBrainzClient
from mediataggerbot.main import apply_plan, build_already_managed_plan, decide_apply
from mediataggerbot.matcher import Matcher
from mediataggerbot.rename import build_target_path
from mediataggerbot.models import GenreResult, MatchResult, MediaFile, PlanResult


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def cfg(tmp_path: Path | None = None):
    value = load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")
    value.data["apis"]["enable_lastfm"] = False
    value.data["apis"]["enable_discogs"] = False
    value.data["matching"]["musicbrainz_artist_genre_fallback"] = False
    if tmp_path is not None:
        value.data["paths"]["media_root"] = str(tmp_path)
    return value


def mb_candidate(recording_id: str = "recording-one", *, artist: str = "Steelheart", title: str = "All Your Love"):
    return {
        "id": recording_id,
        "score": 100,
        "title": title,
        "length": 180000,
        "artist-credit": [
            {
                "name": artist,
                "artist": {"id": "artist-id", "name": artist},
                "joinphrase": "",
            }
        ],
        "releases": [],
        "genres": [{"name": "rock", "count": 2}],
        "tags": [],
    }


class FakeMusicBrainz:
    def __init__(self, results):
        self.results = results
        self.lookup_calls = 0
        self.search_calls = 0

    def search_recording(self, _artist, _title, limit=5):
        self.search_calls += 1
        return self.results[:limit]

    def lookup_recording(self, mbid):
        self.lookup_calls += 1
        return next((item for item in self.results if item["id"] == mbid), None)

    def lookup_release_group(self, _mbid):
        return None

    def lookup_artist(self, _mbid):
        return None


def media(tmp_path: Path, name: str = "Egdw.mp3") -> MediaFile:
    path = tmp_path / name
    path.write_bytes(b"")
    return MediaFile(
        path=path,
        rel_path=path.name,
        extension=path.suffix,
        size_bytes=0,
        modified_ns=path.stat().st_mtime_ns,
        media_kind="audio",
        duration_seconds=180.0,
    )


def mapped_genre() -> GenreResult:
    return GenreResult(
        main_genre="Rock",
        filename_main_genre="Rock",
        subgenre="Hard Rock",
        raw_terms=["hard rock"],
        source="database_terms",
        confidence=90.0,
    )


def test_title_only_text_search_is_never_apply_safe(tmp_path: Path):
    item = media(tmp_path)
    item.existing_title = "All Your Love"
    fake = FakeMusicBrainz([mb_candidate()])
    result = Matcher(cfg(), None, fake, None, None).match(item)

    assert "text_match_missing_or_generic_artist" in result.apply_blockers
    assert decide_apply("apply-safe", result, cfg(), mapped_genre())[0] is False


def test_prior_text_search_identity_does_not_trust_embedded_mbid_or_fast_skip(tmp_path: Path):
    item = media(tmp_path, "Steelheart - All Your Love - Rock.mp3")
    item.existing_artist = "Steelheart"
    item.existing_title = "All Your Love"
    item.existing_genre = "Rock"
    item.existing_musicbrainz_recording_id = "recording-one"
    item.existing_mtb_version = "0.5.2"
    item.existing_mtb_source = "musicbrainz_search_from_existing_tags"
    fake = FakeMusicBrainz([mb_candidate()])

    assert build_already_managed_plan(item, cfg()) is None
    result = Matcher(cfg(), None, fake, None, None).match(item)
    assert fake.lookup_calls == 1  # enrichment after search, not direct shortcut
    assert fake.search_calls == 1
    assert "prior_mediataggerbot_text_identity_requires_review" in result.apply_blockers
    assert decide_apply("apply-safe", result, cfg(), mapped_genre()) == (
        False,
        "prior_text_identity_review_only",
    )


def test_fallback_genre_blocks_apply_safe():
    result = MatchResult(
        matched=True,
        confidence=99.0,
        source="musicbrainz_recording_id_tag",
        artist="Artist",
        title="Song",
        identity_tier="stable_identifier",
        ambiguity_status="exact_stable_identifier",
    )
    fallback = GenreResult(
        main_genre="Pop",
        filename_main_genre="Pop",
        subgenre=None,
        raw_terms=[],
        source="fallback_main_genre",
        confidence=25.0,
    )
    assert decide_apply("apply-safe", result, cfg(), fallback) == (
        False,
        "genre_evidence_missing_review_only",
    )


def test_invalid_isrc_is_rejected_before_request(tmp_path: Path):
    with JsonCache(tmp_path / "cache.sqlite3") as cache:
        client = MusicBrainzClient(
            cache=cache,
            namespace="musicbrainz",
            user_agent="test",
            timeout_seconds=1,
            min_interval_seconds=0,
        )
        called = {"value": False}

        def forbidden(*_args, **_kwargs):
            called["value"] = True
            raise AssertionError("request_json must not be called")

        client.request_json = forbidden  # type: ignore[method-assign]
        assert client.lookup_isrc("WWWRNBXCLUSIVECOM") == []
        assert called["value"] is False


class FakeResponse:
    status_code = 400
    headers = {}

    def raise_for_status(self):
        raise requests.HTTPError("400 bad request")

    def json(self):
        return {}


def test_permanent_400_does_not_open_global_circuit(tmp_path: Path):
    with JsonCache(tmp_path / "cache.sqlite3") as cache:
        client = ApiClientBase(
            cache=cache,
            namespace="test",
            user_agent="test",
            timeout_seconds=1,
            min_interval_seconds=0,
            max_retries=0,
        )
        client.session.request = lambda **_kwargs: FakeResponse()  # type: ignore[method-assign]
        for index in range(4):
            assert client.request_json("GET", f"https://example.invalid/{index}", use_cache=False) is None
        metrics = client.metrics_snapshot()
        assert metrics["requests_sent"] == 4
        assert metrics["circuit_skips"] == 0
        assert metrics["circuit_open"] is False
        assert metrics["failure_streak"] == 0


def test_supported_sidecar_only_failure_does_not_rename_in_apply_safe(tmp_path: Path, monkeypatch):
    config = cfg(tmp_path)
    source = tmp_path / "Artist - Song.mp3"
    source.write_bytes(b"media")
    target = tmp_path / "Artist - Song - Rock.mp3"
    item = MediaFile(
        path=source,
        rel_path=source.name,
        extension=".mp3",
        size_bytes=source.stat().st_size,
        modified_ns=source.stat().st_mtime_ns,
        media_kind="audio",
        duration_seconds=180.0,
    )
    result = MatchResult(
        matched=True,
        confidence=99.0,
        source="musicbrainz_recording_id_tag",
        artist="Artist",
        title="Song",
        identity_tier="stable_identifier",
        ambiguity_status="exact_stable_identifier",
    )
    plan = PlanResult(
        media=item,
        match=result,
        genre=mapped_genre(),
        proposed_path=target,
        proposed_filename=target.name,
        action="apply_safe",
        should_apply=True,
    )
    sidecar = Path(str(source) + ".metadata.json")

    def fake_write(*_args, **_kwargs):
        sidecar.write_text("{}", encoding="utf-8")
        return False, "permission denied", sidecar

    monkeypatch.setattr("mediataggerbot.metadata.write_metadata", fake_write)
    monkeypatch.setattr("mediataggerbot.metadata.verify_metadata_write", lambda *_a, **_k: (True, {"verified": True}))
    monkeypatch.setattr("mediataggerbot.metadata.embedded_metadata_supported", lambda *_a, **_k: True)

    apply_plan(plan, config, "run", mode="apply-safe", journal=None)

    assert plan.status == "embedded_metadata_write_failed"
    assert source.exists()
    assert not target.exists()
    assert plan.renamed is False


def test_windows_invalid_terminal_punctuation_does_not_leave_dash(tmp_path: Path):
    cfg = load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")
    source = tmp_path / "old.mp3"
    source.write_bytes(b"")
    match = MatchResult(
        matched=True,
        confidence=99.0,
        source="musicbrainz_recording_id_tag",
        artist="CAKE",
        title="Is This Love?",
        identity_tier="stable_identifier",
    )
    genre = GenreResult(
        main_genre="Rock",
        filename_main_genre="Rock",
        subgenre="Alternative Rock",
        raw_terms=["alternative rock"],
        source="database_terms",
        confidence=95.0,
    )
    target = build_target_path(source, match, genre, cfg)
    assert target.name == "CAKE - Is This Love - Rock - Alternative Rock.mp3"
    assert "- -" not in target.name


def test_subgenre_alias_preserves_contemporary_r_and_b_spelling():
    cfg = load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")
    result = classify_genre(["contemporary r&b"], cfg)
    assert result.main_genre == "R&B/Soul"
    assert result.subgenre == "Contemporary R&B"


def test_summary_counts_prior_text_identity_review(tmp_path: Path):
    from mediataggerbot.models import PlanResult
    from mediataggerbot.reporting import build_summary

    item = media(tmp_path, "prior.mp3")
    result = MatchResult(
        matched=True,
        confidence=95.0,
        source="existing_mediataggerbot_text_tags",
        artist="Artist",
        title="Song",
        apply_blockers=["prior_mediataggerbot_text_identity_requires_review"],
    )
    plan = PlanResult(
        media=item,
        match=result,
        genre=mapped_genre(),
        proposed_filename=item.path.name,
        proposed_path=item.path,
        sidecar_path=None,
        should_apply=False,
        action="prior_text_identity_review_only",
        status="reported_only",
    )
    summary = build_summary([plan], "run", "apply-safe")
    assert summary["prior_text_identity_review_count"] == 1
