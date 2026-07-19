from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class MediaFile:
    path: Path
    rel_path: str
    extension: str
    size_bytes: int
    media_kind: str
    modified_ns: int | None = None
    relative_depth: int = 0
    duration_seconds: float | None = None
    existing_artist: str | None = None
    existing_title: str | None = None
    existing_album: str | None = None
    existing_album_artist: str | None = None
    existing_genre: str | None = None
    existing_subgenre: str | None = None
    existing_date: str | None = None
    existing_isrc: str | None = None
    existing_musicbrainz_recording_id: str | None = None
    existing_musicbrainz_artist_ids: list[str] = field(default_factory=list)
    existing_musicbrainz_release_id: str | None = None
    existing_musicbrainz_release_group_id: str | None = None
    existing_acoustid_id: str | None = None
    existing_mtb_source: str | None = None
    existing_mtb_confidence: float | None = None
    existing_mtb_version: str | None = None
    existing_mtb_applied_utc: str | None = None
    existing_source_artist_credit: str | None = None
    existing_canonicalization_status: str | None = None
    inventory_cache_hit: bool = False
    fingerprint: str | None = None
    fingerprint_duration: int | None = None
    fingerprint_error: str | None = None
    fingerprint_cache_hit: bool = False
    scan_error: str | None = None


@dataclass(slots=True)
class ScanCoverage:
    root: str
    recursive: bool
    require_recursive_scan: bool
    follow_directory_symlinks: bool
    started_utc: str
    finished_utc: str | None = None
    status: str = "running"
    all_reachable_subfolders_checked: bool = False
    limit_reached: bool = False
    max_files_per_run: int = 0
    graceful_stop_requested: bool = False
    graceful_stop_reason: str = ""
    stopped_phase: str = ""
    directories_visited: int = 0
    subdirectories_discovered: int = 0
    directories_excluded: int = 0
    directory_symlinks_skipped: int = 0
    directory_errors: list[dict[str, str]] = field(default_factory=list)
    excluded_directories_sample: list[str] = field(default_factory=list)
    symlink_directories_sample: list[str] = field(default_factory=list)
    files_seen: int = 0
    unsupported_files_seen: int = 0
    media_files_found: int = 0
    media_files_scanned: int = 0
    media_scan_errors: int = 0
    inventory_cache_hits: int = 0
    inventory_cache_misses: int = 0
    inventory_cache_writes: int = 0
    deepest_relative_depth: int = 0
    media_by_extension: dict[str, int] = field(default_factory=dict)
    media_by_depth: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class MatchResult:
    matched: bool
    confidence: float
    source: str
    artist: str | None = None
    source_artist_credit: str | None = None
    musicbrainz_artist_ids: list[str] = field(default_factory=list)
    title: str | None = None
    album: str | None = None
    album_artist: str | None = None
    date: str | None = None
    original_year: str | None = None
    isrc: str | None = None
    musicbrainz_recording_id: str | None = None
    musicbrainz_release_id: str | None = None
    musicbrainz_release_group_id: str | None = None
    acoustid_id: str | None = None
    acoustid_score: float | None = None
    raw_genres: list[str] = field(default_factory=list)
    raw_tags: list[str] = field(default_factory=list)
    canonicalization_status: str = "not_run"
    canonicalization_score: float = 0.0
    repository_agreement: list[str] = field(default_factory=list)
    repository_conflicts: list[str] = field(default_factory=list)
    name_candidates: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    candidate_count: int = 0
    candidate_margin: float | None = None
    ambiguity_status: str = "not_evaluated"
    identity_tier: str = "unknown"
    identity_cache_hit: bool = False
    version_evidence: list[str] = field(default_factory=list)
    apply_blockers: list[str] = field(default_factory=list)


@dataclass(slots=True)
class GenreResult:
    main_genre: str
    filename_main_genre: str
    subgenre: str | None
    raw_terms: list[str]
    source: str
    confidence: float
    notes: list[str] = field(default_factory=list)


@dataclass(slots=True)
class PlanResult:
    media: MediaFile
    match: MatchResult
    genre: GenreResult | None
    proposed_path: Path | None
    proposed_filename: str | None
    action: str
    should_apply: bool
    status: str = "planned"
    error: str | None = None
    sidecar_path: Path | None = None
    metadata_written: bool = False
    metadata_verified: bool = False
    source_verified: bool = False
    renamed: bool = False
    rename_verified: bool = False
    operation_id: str | None = None
    rollback_record: dict[str, Any] = field(default_factory=dict)


def dataclass_to_jsonable(obj: Any) -> Any:
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "__dataclass_fields__"):
        return {k: dataclass_to_jsonable(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {str(k): dataclass_to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [dataclass_to_jsonable(v) for v in obj]
    return obj
