from __future__ import annotations

from mediataggerbot.version_identity import (
    best_title_similarity,
    extract_version_profile,
    strip_presentation_noise,
    version_compatibility,
)


def test_presentation_cleanup_is_conservative_for_real_title_words():
    assert strip_presentation_noise("Hot Topic")[0] == "Hot Topic"
    assert strip_presentation_noise("Lyrics")[0] == "Lyrics"
    assert strip_presentation_noise("Song Title [Official Video] - HD")[0] == "Song Title"
    assert strip_presentation_noise("Song Title (Lyric Video)")[0] == "Song Title"


def test_version_profile_removes_whole_named_qualifier_from_base_title():
    profile = extract_version_profile("Song Title (Tiësto Remix)")
    assert profile.base_title == "Song Title"
    assert "remix" in profile.material_categories
    assert profile.qualifier_key == "tiesto"


def test_named_remixes_do_not_collapse_to_same_recording_version():
    same = version_compatibility("Song (Tiësto Remix)", "Song (Tiesto Remix)")
    different = version_compatibility("Song (Tiësto Remix)", "Song (Armin Remix)")

    assert same["status"] == "material_version_agreement"
    assert same["qualifier_conflict"] is False
    assert float(same["score_adjustment"]) > 0
    assert different["status"] == "material_version_mismatch"
    assert different["qualifier_conflict"] is True
    assert float(different["score_adjustment"]) < 0


def test_material_version_mismatch_is_strong_but_remaster_difference_is_weak():
    mismatch = version_compatibility("Song (Radio Edit)", "Song (Extended Mix)")
    remaster = version_compatibility("Song (Remastered 2024)", "Song")

    assert mismatch["status"] == "material_version_mismatch"
    assert float(mismatch["score_adjustment"]) <= -10
    assert remaster["status"] == "mastering_label_difference"
    assert -2 < float(remaster["score_adjustment"]) < 0


def test_presentation_noise_does_not_reduce_title_similarity():
    assert best_title_similarity("Song Title (Official Video)", "Song Title") == 1.0
