from __future__ import annotations

import logging
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import AppConfig
from .models import MatchResult, MediaFile
from .utils import comparison_key, normalize_display_text, normalize_joinphrase, text_similarity

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

LOG = logging.getLogger(__name__)

_HIGH_AUTHORITY_PREFIXES = (
    "musicbrainz_recording_id",
    "musicbrainz_isrc",
    "acoustid",
)
_LOCAL_ONLY_SOURCES = {"filename_parse", "existing_tags", "unmatched"}


def canonicalize_match(match: MatchResult, media: MediaFile, config: AppConfig) -> MatchResult:
    """Apply one deterministic visible spelling policy and record repository consensus.

    MusicBrainz stable IDs and entity names are the default authority.  Release-specific
    printed credits are retained separately rather than being discarded.  Last.fm and
    Discogs are treated as cross-checks, not silent overrides of an exact MBID/ISRC/
    fingerprint identity.
    """
    if not match.matched:
        return replace(match, canonicalization_status="unmatched", canonicalization_score=0.0)

    enabled = bool(config.get("canonicalization.enabled", True))
    unicode_form = str(config.get("canonicalization.unicode_form", "NFC") or "NFC")
    artist_policy = str(config.get("canonicalization.artist_name_policy", "musicbrainz_entity") or "musicbrainz_entity")

    overrides = load_overrides(config) if enabled else empty_overrides()
    components = _artist_components(match)
    override_used: list[str] = []

    artist = match.artist
    if components:
        artist = _render_artist_components(
            components,
            artist_policy=artist_policy,
            artist_overrides=overrides["artist_by_mbid"],
            override_used=override_used,
        )
    elif artist_policy == "source_credit" and match.source_artist_credit:
        artist = match.source_artist_credit

    if match.musicbrainz_recording_id:
        title_override = overrides["recording_title_by_mbid"].get(match.musicbrainz_recording_id.casefold())
    else:
        title_override = None
    title = title_override or match.title
    if title_override:
        override_used.append(f"recording:{match.musicbrainz_recording_id}")

    album = match.album
    if match.musicbrainz_release_group_id:
        album_override = overrides["release_group_title_by_mbid"].get(match.musicbrainz_release_group_id.casefold())
        if album_override:
            album = album_override
            override_used.append(f"release_group:{match.musicbrainz_release_group_id}")

    artist = normalize_display_text(artist, unicode_form) or None
    title = normalize_display_text(title, unicode_form) or None
    album = normalize_display_text(album, unicode_form) or None
    album_artist = normalize_display_text(match.album_artist, unicode_form) or None
    source_credit = normalize_display_text(match.source_artist_credit, unicode_form) or None

    candidates = _dedupe_candidates(_base_candidates(match, media) + list(match.name_candidates))
    primary_artist = _primary_artist(components, artist_policy, overrides["artist_by_mbid"]) or artist
    agreements, conflicts = evaluate_repository_consensus(
        canonical_artist=artist,
        canonical_title=title,
        primary_artist=primary_artist,
        canonical_recording_id=match.musicbrainz_recording_id,
        candidates=candidates,
        title_agreement=float(config.get("canonicalization.title_agreement_similarity", 0.94)),
        artist_agreement=float(config.get("canonicalization.artist_agreement_similarity", 0.88)),
        conflict_similarity=float(config.get("canonicalization.conflict_similarity", 0.68)),
    )

    high_authority = match.source.startswith(_HIGH_AUTHORITY_PREFIXES)
    independent_agreements = [item for item in agreements if not item.startswith("existing_tags:")]
    independent_conflicts = [item for item in conflicts if not item.startswith("existing_tags:")]

    if not enabled:
        status = "disabled_normalization_only"
    elif override_used:
        status = "local_override_by_stable_id"
    elif independent_conflicts and not high_authority:
        status = "repository_conflict"
    elif independent_conflicts and high_authority:
        status = "musicbrainz_canonical_with_repository_conflict"
    elif independent_agreements:
        status = "verified_multi_repository"
    elif match.musicbrainz_recording_id:
        status = "musicbrainz_canonical"
    elif match.source not in _LOCAL_ONLY_SOURCES:
        status = "normalized_repository"
    else:
        status = "normalized_local_only"

    score = _canonicalization_score(match, independent_agreements, independent_conflicts, override_used)
    confidence = float(match.confidence)
    if not high_authority:
        if independent_agreements:
            confidence += float(config.get("canonicalization.repository_agreement_bonus", 3.0))
        if independent_conflicts:
            confidence -= float(config.get("canonicalization.repository_conflict_penalty", 12.0))
    confidence = max(0.0, min(100.0, confidence))

    notes = list(match.notes)
    if source_credit and artist and comparison_key(source_credit) != comparison_key(artist):
        notes.append(f"Library-canonical artist spelling differs from printed source credit: {source_credit!r} -> {artist!r}.")
    if independent_agreements:
        notes.append("Repository spelling cross-check agreed: " + ", ".join(independent_agreements[:5]) + ".")
    if independent_conflicts:
        notes.append("Repository spelling conflict recorded: " + ", ".join(independent_conflicts[:5]) + ".")
    if override_used:
        notes.append("Stable-ID canonical override applied: " + ", ".join(override_used) + ".")

    evidence = dict(match.evidence)
    evidence["canonicalization"] = {
        "enabled": enabled,
        "artist_name_policy": artist_policy,
        "unicode_form": unicode_form,
        "status": status,
        "score": round(score, 1),
        "agreement": independent_agreements,
        "conflicts": independent_conflicts,
        "override_used": override_used,
        "visible_artist": artist,
        "source_artist_credit": source_credit,
        "visible_title": title,
    }

    return replace(
        match,
        confidence=confidence,
        artist=artist,
        title=title,
        album=album,
        album_artist=album_artist,
        source_artist_credit=source_credit,
        canonicalization_status=status,
        canonicalization_score=score,
        repository_agreement=independent_agreements,
        repository_conflicts=independent_conflicts,
        name_candidates=candidates,
        notes=_dedupe_text(notes, 30),
        evidence=evidence,
    )


def safe_apply_conflict(match: MatchResult, config: AppConfig) -> bool:
    """Return True when repository conflict should block apply-safe."""
    return bool(
        match.canonicalization_status == "repository_conflict"
        and config.get("canonicalization.block_text_match_conflicts_in_apply_safe", True)
    )


def evaluate_repository_consensus(
    *,
    canonical_artist: str | None,
    canonical_title: str | None,
    primary_artist: str | None,
    canonical_recording_id: str | None,
    candidates: list[dict[str, Any]],
    title_agreement: float,
    artist_agreement: float,
    conflict_similarity: float,
) -> tuple[list[str], list[str]]:
    agreements: list[str] = []
    conflicts: list[str] = []
    for candidate in candidates:
        source = str(candidate.get("source") or "unknown")
        role = str(candidate.get("role") or "repository")
        if role == "canonical":
            continue
        candidate_artist = normalize_display_text(candidate.get("artist")) or None
        candidate_title = normalize_display_text(candidate.get("title")) or None
        candidate_mbid = normalize_display_text(candidate.get("recording_id")) or None

        if canonical_recording_id and candidate_mbid:
            if canonical_recording_id.casefold() == candidate_mbid.casefold():
                agreements.append(f"{source}:recording_id")
                continue
            conflicts.append(f"{source}:recording_id_mismatch")
            continue

        title_score = text_similarity(canonical_title, candidate_title)
        artist_score = max(
            text_similarity(canonical_artist, candidate_artist),
            text_similarity(primary_artist, candidate_artist),
        )
        if candidate_title and candidate_artist and title_score >= title_agreement and artist_score >= artist_agreement:
            agreements.append(f"{source}:name")
        elif candidate_title and candidate_artist and (title_score < conflict_similarity or artist_score < conflict_similarity):
            conflicts.append(f"{source}:name_mismatch")
    return _dedupe_text(agreements, 20), _dedupe_text(conflicts, 20)


def load_overrides(config: AppConfig) -> dict[str, dict[str, str]]:
    raw_path = str(config.get("canonicalization.overrides_file", "config/canonical_overrides.toml") or "").strip()
    if not raw_path:
        return empty_overrides()
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = config.project_root / path
    if not path.exists():
        return empty_overrides()
    try:
        with path.open("rb") as handle:
            payload = tomllib.load(handle)
    except Exception as exc:
        LOG.warning("Canonical override file could not be read (%s): %s", path, exc)
        return empty_overrides()

    out = empty_overrides()
    for section in out:
        node = payload.get(section, {})
        if isinstance(node, dict):
            out[section] = {
                normalize_display_text(str(key)).casefold(): normalize_display_text(str(value))
                for key, value in node.items()
                if normalize_display_text(str(key)) and normalize_display_text(str(value))
            }
    return out


def empty_overrides() -> dict[str, dict[str, str]]:
    return {
        "artist_by_mbid": {},
        "recording_title_by_mbid": {},
        "release_group_title_by_mbid": {},
    }


def _base_candidates(match: MatchResult, media: MediaFile) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if match.musicbrainz_recording_id or match.source.startswith("musicbrainz") or match.source.startswith("acoustid"):
        candidates.append(
            {
                "source": "musicbrainz",
                "role": "canonical",
                "artist": match.artist,
                "title": match.title,
                "recording_id": match.musicbrainz_recording_id,
            }
        )
    if media.existing_artist or media.existing_title:
        candidates.append(
            {
                "source": "existing_tags",
                "role": "observation",
                "artist": media.existing_artist,
                "title": media.existing_title,
                "recording_id": media.existing_musicbrainz_recording_id,
            }
        )
    return candidates


def _artist_components(match: MatchResult) -> list[dict[str, Any]]:
    node = match.evidence.get("musicbrainz_artist_components") if isinstance(match.evidence, dict) else None
    if not isinstance(node, list):
        return []
    return [dict(item) for item in node if isinstance(item, dict)]


def _render_artist_components(
    components: list[dict[str, Any]],
    *,
    artist_policy: str,
    artist_overrides: dict[str, str],
    override_used: list[str],
) -> str | None:
    pieces: list[str] = []
    for item in components:
        mbid = normalize_display_text(item.get("id"))
        override = artist_overrides.get(mbid.casefold()) if mbid else None
        if override:
            name = override
            override_used.append(f"artist:{mbid}")
        elif artist_policy == "source_credit":
            name = item.get("credited_name") or item.get("entity_name")
        else:
            name = item.get("entity_name") or item.get("credited_name")
        name = normalize_display_text(name)
        joinphrase = normalize_joinphrase(item.get("joinphrase"))
        if name:
            pieces.append(name + joinphrase)
    text = "".join(pieces).strip()
    return text or None


def _primary_artist(components: list[dict[str, Any]], artist_policy: str, overrides: dict[str, str]) -> str | None:
    if not components:
        return None
    item = components[0]
    mbid = normalize_display_text(item.get("id"))
    if mbid and mbid.casefold() in overrides:
        return overrides[mbid.casefold()]
    if artist_policy == "source_credit":
        return normalize_display_text(item.get("credited_name") or item.get("entity_name")) or None
    return normalize_display_text(item.get("entity_name") or item.get("credited_name")) or None


def _canonicalization_score(
    match: MatchResult,
    agreements: list[str],
    conflicts: list[str],
    overrides: list[str],
) -> float:
    if overrides:
        base = 100.0
    elif match.musicbrainz_recording_id:
        base = 95.0
    elif match.source not in _LOCAL_ONLY_SOURCES:
        base = 78.0
    else:
        base = 50.0
    base += min(5.0, 3.0 * len(agreements))
    base -= min(30.0, 10.0 * len(conflicts))
    return max(0.0, min(100.0, base))


def _dedupe_candidates(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for item in values:
        source = normalize_display_text(item.get("source")) or "unknown"
        artist = normalize_display_text(item.get("artist"))
        title = normalize_display_text(item.get("title"))
        recording_id = normalize_display_text(item.get("recording_id"))
        key = (source.casefold(), comparison_key(artist), comparison_key(title), recording_id.casefold())
        if key in seen:
            continue
        seen.add(key)
        cleaned = dict(item)
        cleaned.update({"source": source, "artist": artist or None, "title": title or None, "recording_id": recording_id or None})
        out.append(cleaned)
    return out[:20]


def _dedupe_text(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        text = normalize_display_text(value)
        if not text or text.casefold() in seen:
            continue
        seen.add(text.casefold())
        out.append(text)
        if len(out) >= limit:
            break
    return out
