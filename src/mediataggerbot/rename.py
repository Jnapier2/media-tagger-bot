from __future__ import annotations

import os
import re
from pathlib import Path

from .config import AppConfig, validate_sidecar_extension
from .models import GenreResult, MatchResult
from .utils import sanitize_component, truncate_filename_stem, truncate_to_windows_utf16_units, windows_utf16_units


class TargetCollisionError(RuntimeError):
    def __init__(self, target: Path, *, target_exists: bool, reserved: bool) -> None:
        self.target = target
        self.target_exists = target_exists
        self.reserved = reserved
        reason = "existing target" if target_exists else "another planned file"
        super().__init__(f"Canonical target collision with {reason}: {target}")


def build_target_path(
    source_path: Path,
    match: MatchResult,
    genre: GenreResult,
    config: AppConfig,
    reserved_paths: set[str] | None = None,
    allow_collision_suffix: bool = True,
) -> Path:
    if not bool(config.get("processing.same_folder_output", True)):
        raise RuntimeError("processing.same_folder_output=false is not supported by this local in-place build.")

    naming = config.section("naming")
    slash_replacement = str(naming.get("replace_slash_with", "-"))
    ampersand_replacement = str(naming.get("replace_ampersand_with", "&"))
    collapse = bool(naming.get("collapse_whitespace", True))

    def component(value: str) -> str:
        value = value.replace("&", ampersand_replacement)
        return sanitize_component(value, slash_replacement, collapse)

    artist = component(match.artist or str(naming.get("unknown_artist_label", "Unknown Artist")))
    title = component(match.title or str(naming.get("unknown_title_label", "Unknown Title")))
    main = component(genre.filename_main_genre)
    if genre.subgenre:
        subgenre = component(genre.subgenre)
    elif bool(naming.get("omit_subgenre_when_unknown", True)):
        subgenre = ""
    else:
        subgenre = component(str(naming.get("unknown_subgenre_label", "General")))

    pattern = str(naming.get("pattern", "{artist} - {title} - {genre} - {subgenre}"))
    try:
        stem = pattern.format(artist=artist, title=title, genre=main, subgenre=subgenre)
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"Invalid naming.pattern: {pattern!r}: {exc}") from exc
    stem = stem.strip(" .-")
    stem = re.sub(r"\s+", " ", stem).strip()
    if not stem:
        raise RuntimeError("naming.pattern produced an empty filename stem.")

    ext = source_path.suffix if bool(config.get("processing.preserve_extension_case", False)) else source_path.suffix.lower()
    stem = truncate_filename_stem(stem, ext, int(naming.get("max_filename_length", 180)))
    max_full_path_length = int(naming.get("max_full_path_length", 240) or 0)
    stem = fit_stem_to_full_path_budget(source_path.parent, stem, ext, max_full_path_length)
    target = source_path.with_name(f"{stem}{ext}")
    if _path_key(target) == _path_key(source_path):
        if reserved_paths is not None:
            reserved_paths.add(_path_key(target))
        return target
    target_key = _path_key(target)
    target_exists = target.exists()
    target_reserved = reserved_paths is not None and target_key in reserved_paths
    if (target_exists or target_reserved) and not allow_collision_suffix:
        raise TargetCollisionError(target, target_exists=target_exists, reserved=target_reserved)
    target = reserve_unique_path(
        target,
        reserved_paths=reserved_paths,
        style=str(config.get("processing.collision_suffix_style", "space_parentheses_number")),
        max_full_path_length=max_full_path_length,
    )
    return target


def reserve_unique_path(target: Path, reserved_paths: set[str] | None = None, style: str = "space_parentheses_number", max_full_path_length: int = 0) -> Path:
    if style != "space_parentheses_number":
        raise RuntimeError(f"Unsupported collision_suffix_style: {style}")
    reserved = reserved_paths if reserved_paths is not None else set()

    def available(path: Path) -> bool:
        return not path.exists() and _path_key(path) not in reserved

    if available(target):
        reserved.add(_path_key(target))
        return target
    parent, stem, suffix = target.parent, target.stem, target.suffix
    for index in range(2, 10000):
        collision_suffix = f" ({index})"
        candidate_stem = fit_stem_to_full_path_budget(
            parent, stem, suffix, max_full_path_length, collision_suffix=collision_suffix
        )
        candidate = parent / f"{candidate_stem}{collision_suffix}{suffix}"
        if available(candidate):
            reserved.add(_path_key(candidate))
            return candidate
    raise RuntimeError(f"Could not find available collision-free filename for {target}")


def build_sidecar_path(media_target_path: Path, config: AppConfig) -> Path:
    sidecar_extension = str(config.get("metadata.sidecar_extension", ".metadata.json"))
    error = validate_sidecar_extension(sidecar_extension)
    if error:
        raise RuntimeError(f"Unsafe metadata.sidecar_extension {sidecar_extension!r}: {error}")
    sidecar = media_target_path.with_suffix(media_target_path.suffix + sidecar_extension)
    if windows_path_identity_key(sidecar) == windows_path_identity_key(media_target_path):
        raise RuntimeError(
            "Refusing sidecar path that can address the media file under Windows path normalization: "
            f"media={media_target_path} sidecar={sidecar}"
        )
    return sidecar


def fit_stem_to_full_path_budget(
    parent: Path,
    stem: str,
    extension: str,
    max_full_path_length: int,
    *,
    collision_suffix: str = "",
) -> str:
    """Conservatively fit a filename under a complete Windows path budget.

    Python can support extended paths on configured systems, but Explorer, media
    players, taggers, network shares, and archive tools can still impose shorter
    limits.  A zero budget disables this compatibility guard.
    """
    if max_full_path_length <= 0:
        return stem
    parent_text = str(parent)
    fixed_length = (
        windows_utf16_units(parent_text)
        + (1 if parent_text else 0)
        + windows_utf16_units(extension)
        + windows_utf16_units(collision_suffix)
    )
    available = max_full_path_length - fixed_length
    if available < 20:
        raise RuntimeError(
            f"Parent folder leaves only {available} filename characters under the configured "
            f"{max_full_path_length}-character full-path budget: {parent}"
        )
    if windows_utf16_units(stem) <= available:
        return stem
    trimmed = truncate_to_windows_utf16_units(stem, available).rstrip(" .-_ ")
    if len(trimmed) < 20:
        raise RuntimeError(
            f"Unable to build a safe filename under the configured full-path budget for: {parent}"
        )
    return trimmed


def _path_key(path: Path) -> str:
    try:
        return os.path.normcase(str(path.resolve()))
    except OSError:
        return os.path.normcase(os.path.abspath(str(path)))


def windows_path_identity_key(path: Path) -> str:
    """Return a Windows-compatible path identity key on every test platform.

    Win32 normally trims trailing dots and spaces from path components and compares
    paths case-insensitively.  The release builder runs on Linux, so this explicit key
    is required to regression-test aliases such as ``song.mp3.`` safely.
    """
    try:
        text = str(path.resolve(strict=False))
    except (OSError, RuntimeError):
        text = os.path.abspath(str(path))
    text = text.replace("/", "\\")
    if text.startswith("\\\\?\\"):
        text = text[4:]
    components = text.split("\\")
    normalized = [component.rstrip(" .") for component in components]
    return "\\".join(normalized).casefold()
