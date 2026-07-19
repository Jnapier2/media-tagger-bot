from __future__ import annotations

from pathlib import Path

from mediataggerbot import __version__

import pytest
from mutagen.id3 import ID3

from mediataggerbot.config import load_config
from mediataggerbot.metadata import write_metadata
from mediataggerbot.models import GenreResult, MatchResult
from mediataggerbot.rename import build_target_path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def config():
    return load_config(project_root=PROJECT_ROOT, config_path=PROJECT_ROOT / "config" / "config.toml")


def match() -> MatchResult:
    return MatchResult(
        matched=True,
        confidence=99.0,
        source="musicbrainz_recording_id_tag",
        artist="A & B",
        source_artist_credit="A and B",
        musicbrainz_artist_ids=["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"],
        title="Song",
        album="Album",
        album_artist="A & B",
        date="2024-01-02",
        isrc="USABC1234567",
        musicbrainz_recording_id="11111111-1111-1111-1111-111111111111",
        musicbrainz_release_id="22222222-2222-2222-2222-222222222222",
        musicbrainz_release_group_id="33333333-3333-3333-3333-333333333333",
        acoustid_id="44444444-4444-4444-4444-444444444444",
        canonicalization_status="musicbrainz_canonical",
        canonicalization_score=95.0,
        repository_agreement=["lastfm:recording_id"],
    )


def genre() -> GenreResult:
    return GenreResult(
        main_genre="Pop",
        filename_main_genre="Pop",
        subgenre="Dance Pop",
        raw_terms=["dance pop"],
        source="database_terms",
        confidence=95.0,
    )


def test_configured_naming_pattern_and_ampersand(tmp_path: Path):
    cfg = config()
    cfg.data["naming"]["pattern"] = "{artist}__{title}__{genre}__{subgenre}"
    cfg.data["naming"]["replace_ampersand_with"] = "and"
    source = tmp_path / "old.mp3"
    source.write_bytes(b"")

    target = build_target_path(source, match(), genre(), cfg)

    assert target.name == "A and B__Song__Pop__Dance Pop.mp3"


def test_same_folder_false_fails_closed(tmp_path: Path):
    cfg = config()
    cfg.data["processing"]["same_folder_output"] = False
    source = tmp_path / "old.mp3"
    source.write_bytes(b"")
    with pytest.raises(RuntimeError, match="same_folder_output=false"):
        build_target_path(source, match(), genre(), cfg)


def test_id3_writer_populates_identity_and_provenance(tmp_path: Path):
    cfg = config()
    path = tmp_path / "song.mp3"
    path.write_bytes(b"")

    wrote, error, sidecar = write_metadata(path, match(), genre(), cfg)

    assert wrote is True
    assert error is None
    assert sidecar is None
    tags = ID3(path)
    assert str(tags["TIT2"]) == "Song"
    assert str(tags["TPE1"]) == "A & B"
    assert str(tags["TCON"]) == "Pop"
    assert str(tags["TSRC"]) == "USABC1234567"
    assert tags["UFID:http://musicbrainz.org"].data.decode("ascii") == "11111111-1111-1111-1111-111111111111"
    assert str(tags["TXXX:MusicBrainz Album Id"]) == "22222222-2222-2222-2222-222222222222"
    assert str(tags["TXXX:Acoustid Id"]) == "44444444-4444-4444-4444-444444444444"
    assert str(tags["TXXX:MusicBrainz Artist Id"]) == "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
    assert str(tags["TXXX:MediaTaggerBot Source Artist Credit"]) == "A and B"
    assert str(tags["TXXX:MediaTaggerBot Canonicalization Status"]) == "musicbrainz_canonical"
    assert str(tags["TXXX:MediaTaggerBot Version"]) == __version__


def test_unsupported_writer_creates_sidecar(tmp_path: Path):
    cfg = config()
    path = tmp_path / "song.wav"
    path.write_bytes(b"not a real wav")
    sidecar = tmp_path / "song.wav.metadata.json"

    wrote, error, written_sidecar = write_metadata(path, match(), genre(), cfg, sidecar_path=sidecar)

    assert wrote is False
    assert error
    assert written_sidecar == sidecar
    assert sidecar.exists()
