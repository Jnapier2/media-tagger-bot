from __future__ import annotations

import csv
from pathlib import Path

from mediataggerbot.canonicalization import canonicalize_match, safe_apply_conflict
from mediataggerbot.config import load_config
from mediataggerbot.matcher import musicbrainz_recording_to_match, parse_lastfm_track_info
from mediataggerbot.models import MatchResult, MediaFile, PlanResult
from mediataggerbot.reporting import write_name_consistency_reports
from mediataggerbot.utils import comparison_key, normalize_display_text

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def config():
    return load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")


def media(tmp_path: Path, artist: str | None = None, title: str | None = None) -> MediaFile:
    path = tmp_path / "old.mp3"
    path.write_bytes(b"")
    return MediaFile(
        path=path,
        rel_path=path.name,
        extension=".mp3",
        size_bytes=0,
        media_kind="audio",
        existing_artist=artist,
        existing_title=title,
    )


def test_display_normalization_preserves_stylized_repository_spelling():
    assert normalize_display_text("  t.A.T.u.  ") == "t.A.T.u."
    assert normalize_display_text("P!nk") == "P!nk"
    assert normalize_display_text("…Baby One More Time") == "…Baby One More Time"
    assert normalize_display_text("Beyonce\u0301") == "Beyoncé"
    assert normalize_display_text("Family 👨‍👩‍👧‍👦") == "Family 👨‍👩‍👧‍👦"
    assert comparison_key("Sigur Rós") == comparison_key("Sigur Ros")
    assert comparison_key("AC/DC") == comparison_key("ACDC")
    assert comparison_key("t.A.T.u.") == "tatu"
    assert comparison_key("Би-2") == "би2"


def test_musicbrainz_entity_name_is_visible_and_printed_credit_is_preserved(tmp_path: Path):
    payload = {
        "id": "11111111-1111-1111-1111-111111111111",
        "title": "Firestarter",
        "artist-credit": [
            {
                "name": "Prodigy",
                "artist": {
                    "id": "22222222-2222-2222-2222-222222222222",
                    "name": "The Prodigy",
                },
                "joinphrase": "",
            }
        ],
        "releases": [],
        "genres": [],
        "tags": [],
    }
    raw = musicbrainz_recording_to_match(payload, "musicbrainz_recording_id_tag", 99.0)
    result = canonicalize_match(raw, media(tmp_path, "Prodigy", "Firestarter"), config())

    assert result.artist == "The Prodigy"
    assert result.source_artist_credit == "Prodigy"
    assert result.musicbrainz_artist_ids == ["22222222-2222-2222-2222-222222222222"]
    assert result.canonicalization_status == "musicbrainz_canonical"


def test_stable_id_override_never_depends_on_free_text(tmp_path: Path):
    cfg = config()
    override = tmp_path / "canonical_overrides.toml"
    override.write_text(
        '[artist_by_mbid]\n"22222222-2222-2222-2222-222222222222" = "The Prodigy (Library)"\n',
        encoding="utf-8",
    )
    cfg.data["canonicalization"]["overrides_file"] = str(override)
    match = MatchResult(
        matched=True,
        confidence=99.0,
        source="musicbrainz_recording_id_tag",
        artist="The Prodigy",
        source_artist_credit="Prodigy",
        title="Firestarter",
        musicbrainz_recording_id="11111111-1111-1111-1111-111111111111",
        musicbrainz_artist_ids=["22222222-2222-2222-2222-222222222222"],
        evidence={
            "musicbrainz_artist_components": [
                {
                    "id": "22222222-2222-2222-2222-222222222222".upper(),
                    "entity_name": "The Prodigy",
                    "credited_name": "Prodigy",
                    "joinphrase": "",
                }
            ]
        },
    )

    result = canonicalize_match(match, media(tmp_path), cfg)

    assert result.artist == "The Prodigy (Library)"
    assert result.canonicalization_status == "local_override_by_stable_id"


def test_text_match_repository_conflict_is_exception_only_for_apply_safe(tmp_path: Path):
    cfg = config()
    match = MatchResult(
        matched=True,
        confidence=92.0,
        source="musicbrainz_search_from_filename_parse",
        artist="Right Artist",
        source_artist_credit="Right Artist",
        title="Right Song",
        musicbrainz_recording_id="11111111-1111-1111-1111-111111111111",
        name_candidates=[
            {
                "source": "lastfm",
                "role": "repository",
                "artist": "Different Artist",
                "title": "Different Song",
                "recording_id": "99999999-9999-9999-9999-999999999999",
            }
        ],
    )

    result = canonicalize_match(match, media(tmp_path), cfg)

    assert result.canonicalization_status == "repository_conflict"
    assert result.repository_conflicts == ["lastfm:recording_id_mismatch"]
    assert safe_apply_conflict(result, cfg) is True


def test_lastfm_info_parser_reuses_autocorrect_names_mbid_and_tags():
    parsed = parse_lastfm_track_info(
        {
            "track": {
                "name": "The Correct Title",
                "mbid": "11111111-1111-1111-1111-111111111111",
                "artist": {"name": "The Correct Artist"},
                "toptags": {"tag": [{"name": "dance-pop"}, {"name": "pop"}]},
            }
        }
    )
    assert parsed == {
        "artist": "The Correct Artist",
        "title": "The Correct Title",
        "mbid": "11111111-1111-1111-1111-111111111111",
        "tags": ["dance-pop", "pop"],
    }


def test_consistency_reports_cluster_variants_by_stable_ids(tmp_path: Path):
    item = media(tmp_path, "Prodigy", "Fire Starter")
    match = MatchResult(
        matched=True,
        confidence=99.0,
        source="musicbrainz_recording_id_tag",
        artist="The Prodigy",
        source_artist_credit="Prodigy",
        title="Firestarter",
        musicbrainz_recording_id="11111111-1111-1111-1111-111111111111",
        musicbrainz_artist_ids=["22222222-2222-2222-2222-222222222222"],
        canonicalization_status="musicbrainz_canonical",
        canonicalization_score=95.0,
        evidence={
            "musicbrainz_artist_components": [
                {
                    "id": "22222222-2222-2222-2222-222222222222",
                    "entity_name": "The Prodigy",
                    "credited_name": "Prodigy",
                    "joinphrase": "",
                }
            ]
        },
    )
    plan = PlanResult(
        media=item,
        match=match,
        genre=None,
        proposed_path=None,
        proposed_filename=None,
        action="dry_run_report_only",
        should_apply=False,
        status="dry_run",
    )

    paths = write_name_consistency_reports([plan], tmp_path, "test", "dry-run")

    with paths["name_variant_clusters_csv"].open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    artist_row = next(row for row in rows if row["entity_type"] == "artist")
    assert artist_row["stable_id"] == "22222222-2222-2222-2222-222222222222"
    assert "Prodigy" in artist_row["observed_variants"]
    assert "The Prodigy" in artist_row["observed_variants"]


def test_musicbrainz_joinphrases_remain_readable():
    payload = {
        "id": "11111111-1111-1111-1111-111111111111",
        "title": "Collaboration",
        "artist-credit": [
            {"name": "Alias A", "artist": {"id": "a", "name": "Artist A"}, "joinphrase": " feat. "},
            {"name": "Alias B", "artist": {"id": "b", "name": "Artist B"}, "joinphrase": ""},
        ],
        "releases": [],
    }
    match = musicbrainz_recording_to_match(payload, "musicbrainz_recording_id_tag", 99.0)
    assert match.artist == "Artist A feat. Artist B"
    assert match.source_artist_credit == "Alias A feat. Alias B"
