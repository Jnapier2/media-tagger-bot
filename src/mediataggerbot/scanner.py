from __future__ import annotations

import logging
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from mutagen import File as MutagenFile
from mutagen.id3 import ID3

from .cache import JsonCache
from .config import AppConfig
from .ffprobe import ffprobe_duration
from .inventory_cache import load_inventory_cache, store_inventory_cache
from .models import MediaFile, ScanCoverage
from .timeutil import now_utc
from .utils import normalize_text, safe_relpath

LOG = logging.getLogger(__name__)
_REPARSE_POINT = 0x0400
_KEY_CLEAN_RE = re.compile(r"[^a-z0-9]+")


def discover_media_files(
    root: Path,
    config: AppConfig,
    *,
    progress_callback: Callable[[str, int, int | None, str], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
) -> tuple[list[tuple[Path, int]], ScanCoverage]:
    """Discover media with an explicit, auditable directory traversal.

    The traversal is intentionally not based on Path.rglob(): every directory entry is
    inspected so access errors, exclusions, junctions/symlinks, depth, and early limits
    can be reported instead of silently disappearing from the result.
    """
    processing = config.section("processing")
    recursive = bool(processing.get("recursive", True))
    require_recursive = bool(processing.get("require_recursive_scan", True))
    follow_links = bool(processing.get("follow_directory_symlinks", False))
    include_audio = bool(processing.get("include_audio", True))
    include_video = bool(processing.get("include_video", True))
    audio_exts = {str(e).lower() for e in processing.get("supported_audio_extensions", [])}
    video_exts = {str(e).lower() for e in processing.get("supported_video_extensions", [])}
    excluded_names = {str(d).casefold() for d in processing.get("exclude_dir_names", []) if str(d).strip()}
    max_files = max(0, int(processing.get("max_files_per_run", 0) or 0))

    if require_recursive and not recursive:
        raise RuntimeError(
            "Recursive scanning is required by processing.require_recursive_scan=true, "
            "but processing.recursive=false. Set recursive=true to cover all subfolders."
        )

    allowed: set[str] = set()
    if include_audio:
        allowed.update(audio_exts)
    if include_video:
        allowed.update(video_exts)
    if not allowed:
        raise RuntimeError("No media extensions are enabled. Enable audio and/or video extensions in config.")

    coverage = ScanCoverage(
        root=str(root),
        recursive=recursive,
        require_recursive_scan=require_recursive,
        follow_directory_symlinks=follow_links,
        started_utc=now_utc().isoformat(),
        max_files_per_run=max_files,
    )
    found: list[tuple[Path, int]] = []
    by_extension: Counter[str] = Counter()
    by_depth: Counter[str] = Counter()
    stack: list[tuple[Path, int]] = [(root, 0)]
    visited_resolved: set[str] = set()
    progress_every_directories = max(1, int(processing.get("scan_progress_every_directories", 100) or 100))

    while stack:
        if stop_check and stop_check():
            coverage.graceful_stop_requested = True
            coverage.graceful_stop_reason = "graceful stop request matched the active run owner"
            coverage.stopped_phase = "directory_discovery"
            break
        directory, directory_depth = stack.pop()
        if follow_links:
            try:
                resolved_key = os.path.normcase(str(directory.resolve()))
            except OSError:
                resolved_key = os.path.normcase(str(directory.absolute()))
            if resolved_key in visited_resolved:
                coverage.directory_symlinks_skipped += 1
                _append_sample(coverage.symlink_directories_sample, safe_relpath(directory, root))
                continue
            visited_resolved.add(resolved_key)

        try:
            entries = list(os.scandir(directory))
            coverage.directories_visited += 1
            if progress_callback and (
                coverage.directories_visited == 1
                or coverage.directories_visited % progress_every_directories == 0
            ):
                progress_callback(
                    "directory_discovery",
                    coverage.directories_visited,
                    None,
                    safe_relpath(directory, root),
                )
        except OSError as exc:
            rel = safe_relpath(directory, root)
            coverage.directory_errors.append({"path": rel, "error": str(exc)})
            LOG.warning("Unable to scan directory %s: %s", directory, exc)
            continue

        subdirectories: list[tuple[Path, int]] = []
        file_entries: list[os.DirEntry[str]] = []
        for entry in sorted(entries, key=lambda item: item.name.casefold()):
            entry_path = Path(entry.path)
            try:
                is_link_or_reparse = entry.is_symlink() or _is_reparse_point(entry)
                # A symlink/junction reports False for is_dir(follow_symlinks=False), so
                # inspect its target only to classify the entry. It is still skipped unless
                # follow_directory_symlinks=true.
                is_directory = entry.is_dir(follow_symlinks=False)
                if is_link_or_reparse and not is_directory:
                    try:
                        is_directory = entry.is_dir(follow_symlinks=True)
                    except OSError:
                        is_directory = False

                if is_directory:
                    coverage.subdirectories_discovered += 1
                    if entry.name.casefold() in excluded_names:
                        coverage.directories_excluded += 1
                        _append_sample(coverage.excluded_directories_sample, safe_relpath(entry_path, root))
                        continue
                    if is_link_or_reparse and not follow_links:
                        coverage.directory_symlinks_skipped += 1
                        _append_sample(coverage.symlink_directories_sample, safe_relpath(entry_path, root))
                        continue
                    if recursive:
                        subdirectories.append((entry_path, directory_depth + 1))
                    continue

                is_file = entry.is_file(follow_symlinks=follow_links)
                if is_file:
                    if is_link_or_reparse and not follow_links:
                        continue
                    file_entries.append(entry)
            except OSError as exc:
                coverage.directory_errors.append({"path": safe_relpath(entry_path, root), "error": str(exc)})
                LOG.warning("Unable to inspect directory entry %s: %s", entry.path, exc)

        for entry in file_entries:
            coverage.files_seen += 1
            path = Path(entry.path)
            ext = path.suffix.lower()
            if ext not in allowed:
                coverage.unsupported_files_seen += 1
                continue
            try:
                relative_depth = max(0, len(path.relative_to(root).parts) - 1)
            except ValueError:
                # This can only occur when following an unusual link outside root.
                relative_depth = max(0, directory_depth)
            found.append((path, relative_depth))
            coverage.media_files_found += 1
            coverage.deepest_relative_depth = max(coverage.deepest_relative_depth, relative_depth)
            by_extension[ext] += 1
            by_depth[str(relative_depth)] += 1
            if max_files and len(found) >= max_files:
                coverage.limit_reached = True
                stack.clear()
                break

        if coverage.limit_reached:
            break
        if recursive:
            # Reverse before a LIFO push so processing remains deterministic A-to-Z.
            for subdirectory in reversed(subdirectories):
                stack.append(subdirectory)

    coverage.media_by_extension = dict(sorted(by_extension.items()))
    coverage.media_by_depth = dict(sorted(by_depth.items(), key=lambda item: int(item[0])))
    _finalize_coverage(coverage)
    return found, coverage


def scan_media_file(
    path: Path,
    root: Path,
    config: AppConfig,
    relative_depth: int = 0,
    *,
    inventory_cache: JsonCache | None = None,
) -> MediaFile:
    ext = path.suffix.lower()
    audio_exts = {str(e).lower() for e in config.get("processing.supported_audio_extensions", [])}
    media_kind = "audio" if ext in audio_exts else "video"
    stat_result = path.stat()
    rel_path = safe_relpath(path, root)
    modified_ns = getattr(stat_result, "st_mtime_ns", None)
    cached = load_inventory_cache(
        inventory_cache,
        path=path,
        rel_path=rel_path,
        extension=ext,
        size_bytes=stat_result.st_size,
        modified_ns=modified_ns,
        relative_depth=relative_depth,
        media_kind=media_kind,
    )
    if cached is not None:
        return cached
    item = MediaFile(
        path=path,
        rel_path=rel_path,
        extension=ext,
        size_bytes=stat_result.st_size,
        modified_ns=modified_ns,
        relative_depth=relative_depth,
        media_kind=media_kind,
    )
    try:
        tag_summary = read_existing_tags(path)
        item.existing_artist = tag_summary.get("artist")
        item.existing_title = tag_summary.get("title")
        item.existing_album = tag_summary.get("album")
        item.existing_album_artist = tag_summary.get("album_artist")
        item.existing_genre = tag_summary.get("genre")
        item.existing_subgenre = tag_summary.get("subgenre")
        item.existing_date = tag_summary.get("date")
        item.existing_isrc = tag_summary.get("isrc")
        item.existing_musicbrainz_recording_id = tag_summary.get("musicbrainz_recording_id")
        item.existing_musicbrainz_artist_ids = _split_multi_id(tag_summary.get("musicbrainz_artist_ids"))
        item.existing_musicbrainz_release_id = tag_summary.get("musicbrainz_release_id")
        item.existing_musicbrainz_release_group_id = tag_summary.get("musicbrainz_release_group_id")
        item.existing_acoustid_id = tag_summary.get("acoustid_id")
        item.existing_mtb_source = tag_summary.get("mediataggerbot_source")
        item.existing_mtb_confidence = _safe_float(tag_summary.get("mediataggerbot_confidence"))
        item.existing_mtb_version = tag_summary.get("mediataggerbot_version")
        item.existing_mtb_applied_utc = tag_summary.get("mediataggerbot_applied_utc")
        item.existing_source_artist_credit = tag_summary.get("source_artist_credit")
        item.existing_canonicalization_status = tag_summary.get("canonicalization_status")
        if tag_summary.get("duration_seconds") is not None:
            item.duration_seconds = _safe_float(tag_summary.get("duration_seconds"))
        if item.duration_seconds is None:
            item.duration_seconds = ffprobe_duration(path, int(config.get("processing.ffprobe_timeout_seconds", 45)))
    except Exception as exc:
        item.scan_error = str(exc)
        LOG.warning("Scan failed for %s: %s", path, exc)
    store_inventory_cache(inventory_cache, item)
    return item


def scan_media_root(
    root: Path,
    config: AppConfig,
    *,
    progress_callback: Callable[[str, int, int | None, str], None] | None = None,
    stop_check: Callable[[], bool] | None = None,
    inventory_cache: JsonCache | None = None,
) -> tuple[list[MediaFile], ScanCoverage]:
    discovered, coverage = discover_media_files(
        root,
        config,
        progress_callback=progress_callback,
        stop_check=stop_check,
    )
    files: list[MediaFile] = []
    if coverage.graceful_stop_requested:
        coverage.finished_utc = now_utc().isoformat()
        _finalize_coverage(coverage)
        return files, coverage
    progress_every_files = max(1, int(config.get("processing.scan_progress_every_files", 250) or 250))
    for index, (path, relative_depth) in enumerate(discovered, start=1):
        if stop_check and stop_check():
            coverage.graceful_stop_requested = True
            coverage.graceful_stop_reason = "graceful stop request matched the active run owner"
            coverage.stopped_phase = "media_inventory"
            break
        try:
            writes_before = int(inventory_cache.stats.get("writes", 0)) if inventory_cache else 0
            item = scan_media_file(
                path,
                root,
                config,
                relative_depth=relative_depth,
                inventory_cache=inventory_cache,
            )
            if item.inventory_cache_hit:
                coverage.inventory_cache_hits += 1
            elif inventory_cache is not None:
                coverage.inventory_cache_misses += 1
                if int(inventory_cache.stats.get("writes", 0)) > writes_before:
                    coverage.inventory_cache_writes += 1
        except OSError as exc:
            item = MediaFile(
                path=path,
                rel_path=safe_relpath(path, root),
                extension=path.suffix.lower(),
                size_bytes=0,
                relative_depth=relative_depth,
                media_kind="unknown",
                scan_error=str(exc),
            )
            LOG.warning("Unable to stat/scan media file %s: %s", path, exc)
        files.append(item)
        if item.scan_error:
            coverage.media_scan_errors += 1
        if progress_callback and (index == 1 or index % progress_every_files == 0 or index == len(discovered)):
            progress_callback("media_inventory", index, len(discovered), item.rel_path)
    coverage.media_files_scanned = len(files)
    coverage.finished_utc = now_utc().isoformat()
    _finalize_coverage(coverage)
    return files, coverage


def read_existing_tags(path: Path) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        media = MutagenFile(str(path), easy=False)
    except Exception:
        media = None

    tags: Any = None
    if media is not None:
        info = getattr(media, "info", None)
        if info is not None and getattr(info, "length", None) is not None:
            result["duration_seconds"] = _safe_float(getattr(info, "length", None))
        tags = getattr(media, "tags", None)
    elif path.suffix.lower() == ".mp3":
        # MutagenFile can return None for a tag-only/truncated MP3. Direct ID3 readback
        # still verifies the exact durable metadata frames the bot just wrote.
        try:
            tags = ID3(path)
        except Exception:
            tags = None
    if not tags:
        return result

    flat = _flatten_tags(tags)
    aliases: dict[str, tuple[str, ...]] = {
        "artist": ("artist", "tpe1", "author", "wmauthor"),
        "album_artist": ("albumartist", "tpe2", "aart", "wmalbumartist", "wmalbumartistssortorder"),
        "title": ("title", "tit2", "nam"),
        "album": ("album", "talb", "alb", "wmalbumtitle"),
        "genre": ("genre", "tcon", "gen", "wmgenre"),
        "subgenre": ("mediataggerbotsubgenre",),
        "date": ("date", "year", "tdrc", "tyer", "day", "wmyear", "originaldate"),
        "isrc": ("isrc", "tsrc", "wmisrc"),
        "musicbrainz_recording_id": (
            "musicbrainzrecordingid", "musicbrainztrackid", "musicbrainzrecordingidentifier",
            "ufidhttpmusicbrainzorg",
        ),
        "musicbrainz_artist_ids": ("musicbrainzartistid", "musicbrainzartistids"),
        "musicbrainz_release_id": ("musicbrainzreleaseid", "musicbrainzalbumid"),
        "musicbrainz_release_group_id": ("musicbrainzreleasegroupid",),
        "acoustid_id": ("acoustidid", "acoustid"),
        "mediataggerbot_source": ("mediataggerbotsource",),
        "mediataggerbot_confidence": ("mediataggerbotconfidence",),
        "mediataggerbot_version": ("mediataggerbotversion",),
        "mediataggerbot_applied_utc": ("mediataggerbotappliedutc",),
        "source_artist_credit": ("mediataggerbotsourceartistcredit", "sourceartistcredit"),
        "canonicalization_status": ("mediataggerbotcanonicalizationstatus", "canonicalizationstatus"),
    }
    for output_key, candidates in aliases.items():
        value = _first_flat(flat, *candidates)
        if value is not None:
            result[output_key] = normalize_text(value) if isinstance(value, str) else value
    return result


def read_existing_tags_raw(path: Path) -> dict[str, Any]:
    try:
        media = MutagenFile(str(path), easy=False)
    except Exception as exc:
        return {"error": str(exc)}
    if media is None:
        return {"error": "mutagen returned no handler"}
    out: dict[str, Any] = {"type": type(media).__name__}
    info = getattr(media, "info", None)
    if info is not None and getattr(info, "length", None) is not None:
        out["duration_seconds"] = getattr(info, "length", None)
    tags = getattr(media, "tags", None)
    if tags:
        safe_tags: dict[str, Any] = {}
        for key in tags.keys():
            try:
                safe_tags[str(key)] = _stringify_tag_value(tags[key])
            except Exception:
                safe_tags[str(key)] = "<unreadable>"
        out["tags"] = safe_tags
    return out


def _flatten_tags(tags: Any) -> dict[str, str]:
    flat: dict[str, str] = {}
    try:
        keys = list(tags.keys())
    except Exception:
        return flat
    for key in keys:
        try:
            value = tags[key]
        except Exception:
            continue
        normalized_key = _normalize_tag_key(str(key))
        text = _stringify_tag_value(value)
        if normalized_key and text and normalized_key not in flat:
            flat[normalized_key] = text

        # ID3 TXXX descriptions and UFID owners deserve a direct normalized alias.
        try:
            desc = getattr(value, "desc", None)
            if desc:
                flat.setdefault(_normalize_tag_key(str(desc)), text)
            owner = getattr(value, "owner", None)
            data = getattr(value, "data", None)
            if owner and data:
                owner_key = _normalize_tag_key(f"ufid:{owner}")
                flat.setdefault(owner_key, _decode_bytes(data))
        except Exception:
            pass
    return flat


def _stringify_tag_value(value: Any) -> str:
    if value is None:
        return ""
    data = getattr(value, "data", None)
    if isinstance(data, (bytes, bytearray)):
        decoded = _decode_bytes(data)
        if decoded:
            return decoded
    text = getattr(value, "text", None)
    if isinstance(text, (list, tuple)):
        return _first_nonempty(text)
    if text is not None:
        return str(text)
    if isinstance(value, (list, tuple)):
        return _first_nonempty(value)
    if isinstance(value, (bytes, bytearray)):
        return _decode_bytes(value)
    return str(value)


def _first_nonempty(values: Any) -> str:
    for value in values:
        if isinstance(value, (bytes, bytearray)):
            text = _decode_bytes(value)
        else:
            text = str(value)
        if text:
            return text
    return ""


def _decode_bytes(value: bytes | bytearray) -> str:
    for encoding in ("utf-8", "utf-16", "latin-1"):
        try:
            return bytes(value).decode(encoding).strip("\x00")
        except Exception:
            continue
    return bytes(value).hex()


def _normalize_tag_key(value: str) -> str:
    replacements = {
        "©nam": "title",
        "©art": "artist",
        "©alb": "album",
        "©gen": "genre",
        "©day": "date",
    }
    lowered = value.casefold()
    for source, target in replacements.items():
        lowered = lowered.replace(source, target)
    return _KEY_CLEAN_RE.sub("", lowered)


def _first_flat(flat: dict[str, str], *keys: str) -> str | None:
    normalized_keys = [_normalize_tag_key(key) for key in keys]
    for normalized in normalized_keys:
        value = flat.get(normalized)
        if value:
            return value
    # MP4 freeform keys include a com.apple.iTunes namespace prefix.  Suffix
    # matching lets the bot reliably reread its own custom provenance fields.
    for normalized in normalized_keys:
        for actual_key, value in flat.items():
            if value and actual_key.endswith(normalized):
                return value
    return None


def _split_multi_id(value: Any) -> list[str]:
    if value is None:
        return []
    text = str(value)
    parts = re.split(r"[;,|\s]+", text)
    seen: set[str] = set()
    out: list[str] = []
    for part in parts:
        token = part.strip()
        if token and token.casefold() not in seen:
            seen.add(token.casefold())
            out.append(token)
    return out


def _is_reparse_point(entry: os.DirEntry[str]) -> bool:
    try:
        attributes = getattr(entry.stat(follow_symlinks=False), "st_file_attributes", 0)
        return bool(attributes & _REPARSE_POINT)
    except OSError:
        return False


def _append_sample(values: list[str], value: str, limit: int = 100) -> None:
    if value and len(values) < limit:
        values.append(value)


def _finalize_coverage(coverage: ScanCoverage) -> None:
    directory_complete = (
        coverage.recursive
        and not coverage.limit_reached
        and not coverage.directory_errors
        and coverage.directories_excluded == 0
        and coverage.directory_symlinks_skipped == 0
        and coverage.directories_visited == coverage.subdirectories_discovered + 1
    )
    coverage.all_reachable_subfolders_checked = directory_complete

    if coverage.graceful_stop_requested:
        coverage.status = "partial_graceful_stop"
    elif not coverage.recursive:
        coverage.status = "top_level_only"
    elif coverage.limit_reached:
        coverage.status = "partial_file_limit"
    elif coverage.directory_errors:
        coverage.status = "partial_directory_errors"
    elif coverage.directories_excluded:
        coverage.status = "partial_exclusions"
    elif coverage.directory_symlinks_skipped:
        coverage.status = "partial_links_skipped"
    elif directory_complete and coverage.media_scan_errors:
        coverage.status = "complete_all_subfolders_with_media_errors"
    elif directory_complete:
        coverage.status = "complete_all_subfolders"
    else:
        coverage.status = "partial_unverified"

    coverage.notes = []
    if directory_complete:
        coverage.notes.append("Every regular subfolder reachable from the selected root was traversed.")
    else:
        coverage.notes.append("Subfolder coverage is partial; inspect the status and scan coverage report.")
    if coverage.graceful_stop_requested:
        coverage.notes.append(
            f"A graceful stop was requested during {coverage.stopped_phase or 'scanning'}; partial evidence was finalized."
        )
    if coverage.directories_excluded:
        coverage.notes.append(f"{coverage.directories_excluded} explicitly excluded director(ies) were not traversed.")
    if coverage.directory_symlinks_skipped:
        coverage.notes.append(
            f"{coverage.directory_symlinks_skipped} directory symlink/junction entr(ies) were skipped; "
            "set follow_directory_symlinks=true only when those targets should be included."
        )
    if coverage.directory_errors:
        coverage.notes.append(f"{len(coverage.directory_errors)} directory/entry access error(s) were recorded.")
    if coverage.limit_reached:
        coverage.notes.append("max_files_per_run stopped discovery before the tree was completely traversed.")
    if coverage.media_scan_errors:
        coverage.notes.append(f"{coverage.media_scan_errors} media file(s) were found but could not be fully inspected.")


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None
