from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

try:
    import mutagen
except Exception:  # pragma: no cover - runtime dependency is verified by launcher
    mutagen = None  # type: ignore

from .cache import JsonCache
from .models import MediaFile
from .utils import which

INVENTORY_CACHE_NAMESPACE = "media_inventory_v1"
INVENTORY_CACHE_SCHEMA = 1

# Only scanner-derived, non-path fields are persisted.  Fingerprints, scan errors,
# and apply state have separate lifecycles and are intentionally excluded.
_CACHED_FIELDS = (
    "duration_seconds",
    "existing_artist",
    "existing_title",
    "existing_album",
    "existing_album_artist",
    "existing_genre",
    "existing_subgenre",
    "existing_date",
    "existing_isrc",
    "existing_musicbrainz_recording_id",
    "existing_musicbrainz_artist_ids",
    "existing_musicbrainz_release_id",
    "existing_musicbrainz_release_group_id",
    "existing_acoustid_id",
    "existing_mtb_source",
    "existing_mtb_confidence",
    "existing_mtb_version",
    "existing_mtb_applied_utc",
    "existing_source_artist_credit",
    "existing_canonicalization_status",
)


@lru_cache(maxsize=1)
def scanner_capability_signature() -> str:
    """Return the process-stable scanner capability signature used by cache keys.

    ``shutil.which`` can inspect PATH repeatedly.  A library with tens of thousands
    of files should not perform that lookup once per item, so the capability state
    is resolved once per process.  A new run naturally reprobes after tools change.
    """
    mutagen_version = (
        getattr(mutagen, "version_string", None)
        or getattr(mutagen, "__version__", None)
        or "missing"
    )
    return (
        f"schema={INVENTORY_CACHE_SCHEMA};mutagen={mutagen_version};"
        f"ffprobe={bool(which('ffprobe'))}"
    )


def inventory_cache_key(path: Path, size_bytes: int, modified_ns: int | None) -> str:
    """Key an inventory result to exact file identity and scanner capabilities.

    The FFprobe capability bit prevents an old "duration unavailable" result from
    remaining authoritative after FFprobe is installed.  A schema bump invalidates
    older payloads if scanner extraction behavior changes materially.
    """
    try:
        normalized_path = os.path.normcase(str(path.resolve()))
    except OSError:
        normalized_path = os.path.normcase(os.path.abspath(str(path)))
    capability = scanner_capability_signature()
    material = f"{normalized_path}\0{int(size_bytes)}\0{int(modified_ns or 0)}\0{capability}"
    return hashlib.sha256(material.encode("utf-8", errors="surrogatepass")).hexdigest()


def load_inventory_cache(
    cache: JsonCache | None,
    *,
    path: Path,
    rel_path: str,
    extension: str,
    size_bytes: int,
    modified_ns: int | None,
    relative_depth: int,
    media_kind: str,
) -> MediaFile | None:
    if cache is None or cache.disabled:
        return None
    key = inventory_cache_key(path, size_bytes, modified_ns)
    payload = cache.get(INVENTORY_CACHE_NAMESPACE, key)
    if not isinstance(payload, dict) or payload.get("schema") != INVENTORY_CACHE_SCHEMA:
        return None
    if str(payload.get("extension") or "").casefold() != extension.casefold():
        return None
    if str(payload.get("media_kind") or "") != media_kind:
        return None

    item = MediaFile(
        path=path,
        rel_path=rel_path,
        extension=extension,
        size_bytes=size_bytes,
        modified_ns=modified_ns,
        relative_depth=relative_depth,
        media_kind=media_kind,
        inventory_cache_hit=True,
    )
    for field_name in _CACHED_FIELDS:
        if field_name not in payload:
            continue
        value: Any = payload[field_name]
        if field_name == "existing_musicbrainz_artist_ids":
            value = [str(entry) for entry in value] if isinstance(value, list) else []
        setattr(item, field_name, value)
    return item


def store_inventory_cache(cache: JsonCache | None, item: MediaFile) -> bool:
    if cache is None or cache.disabled or item.scan_error:
        return False
    key = inventory_cache_key(item.path, item.size_bytes, item.modified_ns)
    payload: dict[str, Any] = {
        "schema": INVENTORY_CACHE_SCHEMA,
        "extension": item.extension,
        "media_kind": item.media_kind,
    }
    for field_name in _CACHED_FIELDS:
        payload[field_name] = getattr(item, field_name)
    writes_before = int(cache.stats.get("writes", 0))
    cache.set(INVENTORY_CACHE_NAMESPACE, key, payload)
    return int(cache.stats.get("writes", 0)) > writes_before
