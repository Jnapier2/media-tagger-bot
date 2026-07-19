from __future__ import annotations

import re
from collections import defaultdict
from typing import Iterable

from .config import AppConfig, MAIN_GENRES
from .models import GenreResult
from .utils import compact_list, sanitize_component

TOKEN_RE = re.compile(r"[^a-z0-9&+]+")

# Main-bucket mapping. The filename uses safe replacements, but metadata keeps the exact main genre label.
KEYWORD_TO_MAIN: list[tuple[str, str]] = [
    # Specific crossover labels before broad component words.
    ("dance pop", "Pop"), ("electropop", "Pop"), ("synthpop", "Pop"),
    # Hip-Hop/Rap first so pop-rap and rap rock do not get swallowed by Pop/Rock prematurely.
    ("hip hop", "Hip-Hop/Rap"), ("hip-hop", "Hip-Hop/Rap"), ("rap", "Hip-Hop/Rap"),
    ("trap", "Hip-Hop/Rap"), ("drill", "Hip-Hop/Rap"), ("boom bap", "Hip-Hop/Rap"),
    ("grime", "Hip-Hop/Rap"), ("crunk", "Hip-Hop/Rap"), ("gangsta", "Hip-Hop/Rap"),
    # R&B/Soul.
    ("r&b", "R&B/Soul"), ("rnb", "R&B/Soul"), ("rhythm and blues", "R&B/Soul"),
    ("soul", "R&B/Soul"), ("neo soul", "R&B/Soul"), ("neosoul", "R&B/Soul"),
    ("funk", "R&B/Soul"), ("motown", "R&B/Soul"), ("gospel", "R&B/Soul"),
    ("blues", "R&B/Soul"),
    # EDM/electronic.
    ("edm", "Electronic Dance Music (EDM)"), ("electronic", "Electronic Dance Music (EDM)"),
    ("electronica", "Electronic Dance Music (EDM)"), ("dance", "Electronic Dance Music (EDM)"),
    ("house", "Electronic Dance Music (EDM)"), ("techno", "Electronic Dance Music (EDM)"),
    ("trance", "Electronic Dance Music (EDM)"), ("dubstep", "Electronic Dance Music (EDM)"),
    ("drum and bass", "Electronic Dance Music (EDM)"), ("dnb", "Electronic Dance Music (EDM)"),
    ("garage", "Electronic Dance Music (EDM)"), ("breakbeat", "Electronic Dance Music (EDM)"),
    ("synthwave", "Electronic Dance Music (EDM)"), ("ambient", "Electronic Dance Music (EDM)"),
    ("electro", "Electronic Dance Music (EDM)"), ("disco", "Electronic Dance Music (EDM)"),
    # Country.
    ("country", "Country"), ("bluegrass", "Country"), ("honky tonk", "Country"),
    ("americana", "Country"), ("alt-country", "Country"), ("country pop", "Country"),
    # Jazz.
    ("jazz", "Jazz"), ("bebop", "Jazz"), ("swing", "Jazz"), ("big band", "Jazz"),
    ("fusion", "Jazz"), ("smooth jazz", "Jazz"), ("ragtime", "Jazz"), ("vocal jazz", "Jazz"),
    # Classical.
    ("classical", "Classical"), ("baroque", "Classical"), ("romantic", "Classical"),
    ("opera", "Classical"), ("symphony", "Classical"), ("concerto", "Classical"),
    ("orchestral", "Classical"), ("chamber", "Classical"), ("choral", "Classical"),
    # Rock.
    ("rock", "Rock"), ("metal", "Rock"), ("punk", "Rock"), ("grunge", "Rock"),
    ("alternative", "Rock"), ("indie", "Rock"), ("hardcore", "Rock"), ("emo", "Rock"),
    ("shoegaze", "Rock"), ("post-rock", "Rock"), ("prog", "Rock"), ("psychedelic", "Rock"),
    # Pop last as the broad fallback bucket.
    ("pop", "Pop"), ("top 40", "Pop"), ("teen pop", "Pop"), ("k-pop", "Pop"),
    ("j-pop", "Pop"), ("synthpop", "Pop"), ("new wave", "Pop"), ("adult contemporary", "Pop"),
]

BROAD_TERMS = {
    "pop", "rock", "hip hop", "hip-hop", "rap", "electronic", "edm", "dance", "country",
    "r&b", "rnb", "rhythm and blues", "soul", "jazz", "classical", "music", "seen live",
    "favorites", "favorite", "male vocalists", "female vocalists", "american", "british", "oldies",
    "singer-songwriter", "soundtrack", "cover", "remix", "single", "video", "music video",
    "other pop", "dance & dj", "alternative and punk", "classic pop and rock",
}

SUBGENRE_ALIASES = {
    "hip hop": "Hip-Hop",
    "hip-hop": "Hip-Hop",
    "rnb": "R&B",
    "r&b": "R&B",
    "edm": "EDM",
    "dnb": "Drum and Bass",
    "drum n bass": "Drum and Bass",
    "drum and bass": "Drum and Bass",
    "alt rock": "Alternative Rock",
    "alternative": "Alternative Rock",
    "neo soul": "Neo Soul",
    "neosoul": "Neo Soul",
    "kpop": "K-Pop",
    "k-pop": "K-Pop",
    "jpop": "J-Pop",
    "j-pop": "J-Pop",
    "contemporary r&b": "Contemporary R&B",
    "contemporary rnb": "Contemporary R&B",
    "hip hop rap": "Hip-Hop",
    "hip hop-rap": "Hip-Hop",
    "alternative and punk": "Alternative Rock",
    "classic pop and rock": "Pop Rock",
    "dance & dj": "Dance",
    "electronica-dance": "Electronica",
    "rock and roll": "Rock & Roll",
}


def classify_genre(raw_terms: Iterable[str], config: AppConfig) -> GenreResult:
    terms = compact_list([clean_term(t) for t in raw_terms if clean_term(t)], limit=25)
    scores: dict[str, float] = defaultdict(float)
    term_to_main: dict[str, str] = {}

    for index, term in enumerate(terms):
        normalized = normalize_genre_key(term)
        weight = max(1.0, 10.0 - index)  # earlier database terms carry more weight
        main = map_term_to_main(normalized)
        if main:
            scores[main] += weight
            term_to_main[term] = main

    fallback = str(config.get("genres.fallback_main_genre", "Pop"))
    if fallback not in MAIN_GENRES:
        fallback = "Pop"

    if scores:
        main_genre = max(scores.items(), key=lambda kv: kv[1])[0]
        confidence = min(98.0, 60.0 + max(scores.values()) * 3.0)
        source = "database_terms"
    else:
        main_genre = fallback
        confidence = 25.0
        source = "fallback_main_genre"

    subgenre = choose_subgenre(terms, main_genre)
    max_words = max(1, int(config.get("genres.subgenre_max_words", 4) or 4))
    if subgenre and len(subgenre.split()) > max_words:
        subgenre = " ".join(subgenre.split()[:max_words])
    filename_overrides = config.get("genres.filename_main_genre_overrides", {}) or {}
    filename_main = filename_overrides.get(main_genre, main_genre)
    filename_main = sanitize_component(
        filename_main,
        slash_replacement=str(config.get("naming.replace_slash_with", "-")),
        collapse_whitespace=bool(config.get("naming.collapse_whitespace", True)),
    )
    notes: list[str] = []
    if not terms:
        notes.append("No usable genre/tag terms found; used fallback main genre.")
    elif not subgenre:
        notes.append("No specific subgenre term selected.")
    return GenreResult(
        main_genre=main_genre,
        filename_main_genre=filename_main,
        subgenre=subgenre,
        raw_terms=terms,
        source=source,
        confidence=confidence,
        notes=notes,
    )


def map_term_to_main(normalized: str) -> str | None:
    if not normalized:
        return None
    padded = f" {normalized} "
    for keyword, main in KEYWORD_TO_MAIN:
        k = normalize_genre_key(keyword)
        if padded == f" {k} " or f" {k} " in padded:
            return main
    return None


def choose_subgenre(terms: list[str], main_genre: str) -> str | None:
    for term in terms:
        normalized = normalize_genre_key(term)
        if not normalized or normalized in BROAD_TERMS:
            continue
        if map_term_to_main(normalized) != main_genre:
            continue
        cleaned = SUBGENRE_ALIASES.get(normalized) or titleize_subgenre(term)
        if cleaned and clean_term(cleaned).casefold() not in {main_genre.casefold(), main_genre.replace("/", " ").casefold()}:
            return cleaned
    return None


def clean_term(term: str) -> str:
    term = str(term or "").strip()
    term = re.sub(r"\s+", " ", term)
    return term.strip(" -_./\\")


def normalize_genre_key(term: str) -> str:
    term = term.casefold().strip()
    term = term.replace("&amp;", "&")
    term = term.replace("'", "")
    term = term.replace("/", " ")
    term = term.replace("_", " ")
    term = term.replace("-", " ")
    term = TOKEN_RE.sub(" ", term)
    return re.sub(r"\s+", " ", term).strip()


def titleize_subgenre(term: str) -> str:
    term = clean_term(term)
    lower = term.casefold()
    if lower in SUBGENRE_ALIASES:
        return SUBGENRE_ALIASES[lower]
    small_words = {"and", "or", "the", "of", "for", "a", "an", "to", "in"}
    words = re.split(r"(\s+|-)", term)
    out: list[str] = []
    word_index = 0
    for part in words:
        if part.isspace() or part == "-":
            out.append(part)
            continue
        if not part:
            continue
        if word_index > 0 and part.casefold() in small_words:
            out.append(part.casefold())
        elif part.isupper() and len(part) <= 4:
            out.append(part)
        else:
            out.append(part[:1].upper() + part[1:].lower())
        word_index += 1
    return "".join(out).strip()
