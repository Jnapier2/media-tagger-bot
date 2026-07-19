from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

from .utils import compact_list, comparison_key, normalize_display_text, text_similarity

# Presentation/channel labels are not recording identity. Keep this list conservative:
# generic words such as "lyrics" or "topic" are removed only when bracketed or trailing,
# never from the middle of a legitimate title.
_PRESENTATION_PATTERNS = [
    r"official(?:\s+music)?\s+video",
    r"official\s+audio",
    r"official\s+visuali[sz]er",
    r"audio\s+only",
    r"lyrics?\s+video",
    r"with\s+lyrics?",
    r"lyrics?",
    r"visuali[sz]er",
    r"music\s+video",
    r"(?:uhd|hd|hq|4k|8k|1080p|720p|480p)",
    r"(?:vevo|topic)",
]
_PRESENTATION_EXACT_RE = re.compile(r"^(?:" + "|".join(_PRESENTATION_PATTERNS) + r")$", re.IGNORECASE)
# Only unmistakable multi-word production labels are safe to remove inline.
_SAFE_INLINE_PRESENTATION_RE = re.compile(
    r"\b(?:official(?:\s+music)?\s+video|official\s+audio|official\s+visuali[sz]er|"
    r"lyrics?\s+video|audio\s+only|music\s+video)\b",
    re.IGNORECASE,
)
_TRAILING_PRESENTATION_RE = re.compile(
    r"\s*[-–—|:]\s*(?:" + "|".join(_PRESENTATION_PATTERNS) + r")\s*$",
    re.IGNORECASE,
)
_BRACKET_RE = re.compile(r"([\[(])([^\]\)]{1,160})([\])])")

# Material qualifiers distinguish recordings/versions and must not be discarded.
_VERSION_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("remix", re.compile(r"\b(?:remix|rmx|re-?mix|rework|bootleg|mashup)\b", re.IGNORECASE)),
    ("mix", re.compile(r"\b(?:club|dance|house|radio|single|album|original|extended|12[\"']?|7[\"']?)\s+mix\b|\bmix\b", re.IGNORECASE)),
    ("dub", re.compile(r"\b(?:dub|dubwise)\b", re.IGNORECASE)),
    ("edit", re.compile(r"\b(?:radio|single|album|extended|club|video|clean)\s+edit\b|\bedit\b", re.IGNORECASE)),
    ("extended", re.compile(r"\bextended(?:\s+(?:version|mix|edit))?\b", re.IGNORECASE)),
    ("live", re.compile(r"\blive(?:\s+(?:at|from|in|on))?\b", re.IGNORECASE)),
    ("acoustic", re.compile(r"\bacoustic(?:\s+version)?\b|\bunplugged\b", re.IGNORECASE)),
    ("instrumental", re.compile(r"\binstrumental(?:\s+version)?\b", re.IGNORECASE)),
    ("demo", re.compile(r"\bdemo(?:\s+version)?\b", re.IGNORECASE)),
    ("karaoke", re.compile(r"\bkaraoke(?:\s+version)?\b", re.IGNORECASE)),
    ("clean", re.compile(r"\bclean(?:\s+(?:version|edit))?\b", re.IGNORECASE)),
    ("explicit", re.compile(r"\bexplicit(?:\s+version)?\b", re.IGNORECASE)),
    ("mono", re.compile(r"\bmono(?:\s+(?:mix|version))?\b", re.IGNORECASE)),
    ("stereo", re.compile(r"\bstereo(?:\s+(?:mix|version))?\b", re.IGNORECASE)),
    ("version", re.compile(r"\b(?:alternate|alternative|original|album|single|film|soundtrack)\s+version\b", re.IGNORECASE)),
    # Mastering labels are retained for display/evidence, but are not treated as a different
    # recording identity by default because MusicBrainz generally keeps remasters together.
    ("mastering", re.compile(r"\b(?:re-?master(?:ed)?|digitally\s+remastered)(?:\s+\d{4})?\b", re.IGNORECASE)),
)

_MATERIAL_CATEGORIES = frozenset(
    {"remix", "mix", "dub", "edit", "extended", "live", "acoustic", "instrumental", "demo", "karaoke", "clean", "explicit", "mono", "stereo", "version"}
)
_GENERIC_QUALIFIER_WORDS = {
    "remix", "rmx", "mix", "dub", "edit", "extended", "version", "live", "at", "from", "in", "on",
    "acoustic", "unplugged", "instrumental", "demo", "karaoke", "clean", "explicit", "mono", "stereo",
    "radio", "single", "album", "club", "original", "alternate", "alternative", "film", "soundtrack",
    "remaster", "remastered", "digitally", "rework", "bootleg", "mashup",
}


@dataclass(slots=True)
class VersionProfile:
    original: str
    cleaned: str
    base_title: str
    categories: set[str] = field(default_factory=set)
    phrases: list[str] = field(default_factory=list)
    presentation_removed: list[str] = field(default_factory=list)

    @property
    def material_categories(self) -> set[str]:
        return set(self.categories) & set(_MATERIAL_CATEGORIES)

    @property
    def qualifier_key(self) -> str:
        tokens: list[str] = []
        for phrase in self.phrases:
            for token in comparison_key(phrase).split():
                if token not in _GENERIC_QUALIFIER_WORDS:
                    tokens.append(token)
        return " ".join(compact_list(tokens, limit=20))

    def as_dict(self) -> dict[str, object]:
        return {
            "original": self.original,
            "cleaned": self.cleaned,
            "base_title": self.base_title,
            "categories": sorted(self.categories),
            "material_categories": sorted(self.material_categories),
            "phrases": list(self.phrases),
            "qualifier_key": self.qualifier_key,
            "presentation_removed": list(self.presentation_removed),
        }


def strip_presentation_noise(value: str | None) -> tuple[str, list[str]]:
    """Remove only channel/video-presentation labels, preserving real title words."""
    text = normalize_display_text(value)
    removed: list[str] = []

    def bracket_repl(match: re.Match[str]) -> str:
        content = normalize_display_text(match.group(2))
        if _PRESENTATION_EXACT_RE.fullmatch(content):
            removed.append(content)
            return " "
        return match.group(0)

    text = _BRACKET_RE.sub(bracket_repl, text)
    for match in list(_SAFE_INLINE_PRESENTATION_RE.finditer(text)):
        token = normalize_display_text(match.group(0))
        if token:
            removed.append(token)
    text = _SAFE_INLINE_PRESENTATION_RE.sub(" ", text)

    # Repeatedly peel only trailing labels. This handles "Title - VEVO - HD" without
    # corrupting legitimate titles such as "Hot Topic" or "Lyrics".
    while True:
        trailing = _TRAILING_PRESENTATION_RE.search(text)
        if not trailing:
            break
        token = normalize_display_text(trailing.group(0)).strip(" -–—|:")
        if token:
            removed.append(token)
        text = text[: trailing.start()]

    text = re.sub(r"\s+", " ", text).strip(" -_.")
    return text, compact_list(removed, limit=12)


def _categories_for_text(text: str) -> set[str]:
    return {category for category, pattern in _VERSION_RULES if pattern.search(text)}


def extract_version_profile(value: str | None) -> VersionProfile:
    original = normalize_display_text(value)
    cleaned, removed = strip_presentation_noise(original)
    categories: set[str] = set()
    phrases: list[str] = []
    base = cleaned
    spans: list[tuple[int, int]] = []

    # If a parenthetical/bracketed qualifier contains a material version signal, the
    # entire qualifier is excluded from base-title comparison. This prevents venue,
    # remixer, and edition names from being mistaken for the song's base title.
    for bracket in _BRACKET_RE.finditer(cleaned):
        content = normalize_display_text(bracket.group(2))
        bracket_categories = _categories_for_text(content)
        if bracket_categories:
            categories.update(bracket_categories)
            phrases.append(content)
            spans.append(bracket.span())

    for category, pattern in _VERSION_RULES:
        for match in pattern.finditer(cleaned):
            categories.add(category)
            phrase = normalize_display_text(match.group(0))
            if phrase:
                phrases.append(phrase)
            # A whole version-bearing bracket is already marked above.
            if not any(start <= match.start() and match.end() <= end for start, end in spans):
                spans.append(match.span())

    if spans:
        chars = list(base)
        for start, end in spans:
            for index in range(start, min(end, len(chars))):
                chars[index] = " "
        base = "".join(chars)
    base = re.sub(r"[\[\](){}]", " ", base)
    base = re.sub(r"\s+", " ", base).strip(" -_.") or cleaned
    return VersionProfile(
        original=original,
        cleaned=cleaned,
        base_title=base,
        categories=categories,
        phrases=compact_list(phrases, limit=16),
        presentation_removed=removed,
    )


def version_compatibility(
    query_title: str | None,
    candidate_title: str | None,
    *,
    match_bonus: float = 5.0,
    mismatch_penalty: float = 14.0,
) -> dict[str, object]:
    """Score recording-version agreement without collapsing distinct mixes/edits."""
    query = extract_version_profile(query_title)
    candidate = extract_version_profile(candidate_title)
    query_material = query.material_categories
    candidate_material = candidate.material_categories
    shared = query_material & candidate_material
    missing = query_material - candidate_material
    unexpected = candidate_material - query_material

    score = 0.0
    if shared:
        score += min(float(match_bonus), float(match_bonus) * len(shared))
    if missing:
        score -= float(mismatch_penalty) * len(missing)
    if unexpected:
        score -= max(3.0, float(mismatch_penalty) * 0.65) * len(unexpected)

    qualifier_similarity: float | None = None
    qualifier_conflict = False
    if shared and query.qualifier_key and candidate.qualifier_key:
        qualifier_similarity = text_similarity(query.qualifier_key, candidate.qualifier_key)
        # "Tiësto Remix" and "Armin Remix" share the broad category but are not the
        # same version. Penalize conflicting named qualifiers without requiring an exact
        # string match (accents and punctuation are comparison-only noise).
        if qualifier_similarity < 0.60:
            qualifier_conflict = True
            score -= max(4.0, float(mismatch_penalty) * 0.75)
        elif qualifier_similarity >= 0.90:
            score += min(2.0, float(match_bonus) * 0.40)

    # Mastering disagreement is evidence but only a weak score adjustment.
    query_mastering = "mastering" in query.categories
    candidate_mastering = "mastering" in candidate.categories
    if query_mastering == candidate_mastering and query_mastering:
        score += min(1.5, float(match_bonus) * 0.25)
    elif query_mastering != candidate_mastering:
        score -= 0.75

    if missing or unexpected or qualifier_conflict:
        status = "material_version_mismatch"
    elif shared:
        status = "material_version_agreement"
    elif query_mastering != candidate_mastering:
        status = "mastering_label_difference"
    else:
        status = "no_material_version_signal"

    return {
        "status": status,
        "score_adjustment": round(score, 3),
        "query": query.as_dict(),
        "candidate": candidate.as_dict(),
        "shared": sorted(shared),
        "missing_from_candidate": sorted(missing),
        "unexpected_in_candidate": sorted(unexpected),
        "qualifier_similarity": round(qualifier_similarity, 6) if qualifier_similarity is not None else None,
        "qualifier_conflict": qualifier_conflict,
        "full_title_similarity": round(text_similarity(query.cleaned, candidate.cleaned), 6),
        "base_title_similarity": round(text_similarity(query.base_title, candidate.base_title), 6),
    }


def best_title_similarity(query_title: str | None, candidate_title: str | None) -> float:
    query = extract_version_profile(query_title)
    candidate = extract_version_profile(candidate_title)
    full = text_similarity(query.cleaned, candidate.cleaned)
    base = text_similarity(query.base_title, candidate.base_title)
    # Full title is authoritative when version text exists; base comparison prevents harmless
    # presentation labels from dominating the score.
    if query.material_categories or candidate.material_categories:
        return max(0.0, min(1.0, 0.62 * full + 0.38 * base))
    return max(full, base)


def summarize_version_evidence(values: Iterable[dict[str, object]]) -> list[str]:
    out: list[str] = []
    for value in values:
        status = str(value.get("status") or "")
        if status and status != "no_material_version_signal":
            out.append(status)
        if bool(value.get("qualifier_conflict")):
            out.append("named_version_qualifier_conflict")
    return compact_list(out, limit=10)
