from __future__ import annotations

import csv
import hashlib
import html
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .models import PlanResult, ScanCoverage, dataclass_to_jsonable
from .asset_metadata import ASSET_METADATA_SCHEMA, PROJECT_SLUG
from .utils import atomic_text_writer, comparison_key, normalize_display_text, write_json_atomic, write_text_atomic

CSV_FIELDS = [
    "run_id", "mode", "status", "action", "should_apply", "original_path", "relative_path", "relative_depth",
    "proposed_path", "sidecar_path", "source_verified", "renamed", "rename_verified", "metadata_written", "metadata_verified", "operation_id",
    "confidence", "source", "identity_tier", "identity_cache_hit", "candidate_count", "candidate_margin", "ambiguity_status",
    "version_evidence", "apply_blockers", "write_readiness_status",
    "existing_artist", "artist", "source_artist_credit", "existing_title", "title",
    "canonicalization_status", "canonicalization_score", "repository_agreement", "repository_conflicts",
    "musicbrainz_artist_ids", "genre", "filename_genre", "subgenre", "album", "date", "isrc",
    "mb_recording_id", "mb_release_id", "mb_release_group_id", "acoustid_id", "existing_mb_recording_id",
    "existing_isrc", "inventory_cache_hit", "fingerprint_cache_hit", "error", "notes", "raw_terms",
]


def plan_to_row(plan: PlanResult, run_id: str, mode: str) -> dict[str, Any]:
    match = plan.match
    genre = plan.genre
    return {
        "run_id": run_id,
        "mode": mode,
        "status": plan.status,
        "action": plan.action,
        "should_apply": plan.should_apply,
        "original_path": str(plan.media.path),
        "relative_path": plan.media.rel_path,
        "relative_depth": plan.media.relative_depth,
        "proposed_path": str(plan.proposed_path) if plan.proposed_path else "",
        "sidecar_path": str(plan.sidecar_path) if plan.sidecar_path else "",
        "source_verified": plan.source_verified,
        "renamed": plan.renamed,
        "rename_verified": plan.rename_verified,
        "metadata_written": plan.metadata_written,
        "metadata_verified": plan.metadata_verified,
        "operation_id": plan.operation_id or "",
        "confidence": f"{match.confidence:.1f}",
        "source": match.source,
        "identity_tier": match.identity_tier,
        "identity_cache_hit": match.identity_cache_hit,
        "candidate_count": match.candidate_count,
        "candidate_margin": f"{match.candidate_margin:.1f}" if match.candidate_margin is not None else "",
        "ambiguity_status": match.ambiguity_status,
        "version_evidence": " | ".join(match.version_evidence),
        "apply_blockers": " | ".join(match.apply_blockers),
        "write_readiness_status": str((match.evidence.get("write_readiness_at_apply") or match.evidence.get("write_readiness") or {}).get("status") or ""),
        "existing_artist": plan.media.existing_artist or "",
        "artist": match.artist or "",
        "source_artist_credit": match.source_artist_credit or "",
        "existing_title": plan.media.existing_title or "",
        "title": match.title or "",
        "canonicalization_status": match.canonicalization_status,
        "canonicalization_score": f"{match.canonicalization_score:.1f}",
        "repository_agreement": " | ".join(match.repository_agreement),
        "repository_conflicts": " | ".join(match.repository_conflicts),
        "musicbrainz_artist_ids": " | ".join(match.musicbrainz_artist_ids),
        "genre": genre.main_genre if genre else "",
        "filename_genre": genre.filename_main_genre if genre else "",
        "subgenre": genre.subgenre if genre and genre.subgenre else "",
        "album": match.album or "",
        "date": match.date or "",
        "isrc": match.isrc or "",
        "mb_recording_id": match.musicbrainz_recording_id or "",
        "mb_release_id": match.musicbrainz_release_id or "",
        "mb_release_group_id": match.musicbrainz_release_group_id or "",
        "acoustid_id": match.acoustid_id or "",
        "existing_mb_recording_id": plan.media.existing_musicbrainz_recording_id or "",
        "existing_isrc": plan.media.existing_isrc or "",
        "inventory_cache_hit": plan.media.inventory_cache_hit,
        "fingerprint_cache_hit": plan.media.fingerprint_cache_hit,
        "error": plan.error or plan.media.scan_error or "",
        "notes": " | ".join(match.notes + (genre.notes if genre else [])),
        "raw_terms": " | ".join((genre.raw_terms if genre else []) or match.raw_genres + match.raw_tags),
    }


def write_reports(
    plans: list[PlanResult],
    output_dir: Path,
    run_id: str,
    mode: str,
    scan_coverage: ScanCoverage | None = None,
    write_coverage: bool = True,
    write_exception_report: bool = True,
    report_duplicate_candidates: bool = True,
    report_acoustic_clusters: bool = True,
    review_confidence: float = 90.0,
    write_csv: bool = True,
    write_jsonl: bool = True,
    write_html: bool = True,
    write_consistency_reports: bool = True,
) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}

    if write_csv:
        csv_path = output_dir / f"media_tagger_report_{run_id}.csv"
        _write_plan_csv(csv_path, plans, run_id, mode)
        paths["csv_report"] = csv_path

    if write_jsonl:
        jsonl_path = output_dir / f"media_tagger_report_{run_id}.jsonl"
        with atomic_text_writer(jsonl_path, encoding="utf-8", newline="\n") as handle:
            for plan in plans:
                handle.write(json.dumps(dataclass_to_jsonable(plan), ensure_ascii=False) + "\n")
        paths["jsonl_report"] = jsonl_path

    duplicate_groups = find_duplicate_recording_groups(plans)
    summary = build_summary(
        plans,
        run_id,
        mode,
        scan_coverage=scan_coverage,
        duplicate_group_count=len(duplicate_groups),
        review_confidence=review_confidence,
    )
    summary_path = output_dir / f"summary_{run_id}.json"
    write_json_atomic(summary_path, summary)
    paths["summary_json"] = summary_path

    if write_html:
        html_path = output_dir / f"summary_{run_id}.html"
        write_html_summary(html_path, summary, plans[:500])
        paths["summary_html"] = html_path

    if scan_coverage and write_coverage:
        paths.update(write_scan_coverage(scan_coverage, output_dir, run_id))

    if write_exception_report:
        exceptions = [plan for plan in plans if needs_review(plan, review_confidence)]
        exception_path = output_dir / f"needs_review_{run_id}.csv"
        _write_plan_csv(exception_path, exceptions, run_id, mode)
        paths["needs_review_csv"] = exception_path

        prior_text_matches = [
            plan
            for plan in plans
            if "prior_mediataggerbot_text_identity_requires_review" in plan.match.apply_blockers
        ]
        if prior_text_matches:
            prior_path = output_dir / f"prior_text_identity_review_{run_id}.csv"
            _write_plan_csv(prior_path, prior_text_matches, run_id, mode)
            paths["prior_text_identity_review_csv"] = prior_path

    if report_duplicate_candidates and duplicate_groups:
        duplicate_path = output_dir / f"duplicate_recording_candidates_{run_id}.csv"
        write_duplicate_candidates(duplicate_path, duplicate_groups, run_id, mode)
        paths["duplicate_candidates_csv"] = duplicate_path

    acoustic_groups = find_acoustic_fingerprint_groups(plans)
    if report_acoustic_clusters and acoustic_groups:
        acoustic_path = output_dir / f"acoustic_duplicate_clusters_{run_id}.csv"
        write_acoustic_duplicate_clusters(acoustic_path, acoustic_groups, run_id, mode)
        paths["acoustic_duplicate_clusters_csv"] = acoustic_path

    if write_consistency_reports and mode != "scan-only":
        paths.update(write_name_consistency_reports(plans, output_dir, run_id, mode))

    rollback_records = [
        plan.rollback_record
        for plan in plans
        if plan.rollback_record and bool(plan.rollback_record.get("renamed"))
    ]
    if rollback_records:
        rollback_json = output_dir / f"rollback_manifest_{run_id}.json"
        write_json_atomic(rollback_json, rollback_records)
        paths["rollback_manifest_json"] = rollback_json
        rollback_csv = output_dir / f"rollback_manifest_{run_id}.csv"
        write_rollback_csv(rollback_csv, rollback_records)
        paths["rollback_manifest_csv"] = rollback_csv
    return paths


def _write_plan_csv(path: Path, plans: list[PlanResult], run_id: str, mode: str) -> None:
    with atomic_text_writer(path, encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for plan in plans:
            writer.writerow(plan_to_row(plan, run_id, mode))


def write_name_consistency_reports(
    plans: list[PlanResult], output_dir: Path, run_id: str, mode: str
) -> dict[str, Path]:
    """Write exception-focused canonical spelling evidence without requiring per-file review."""
    paths: dict[str, Path] = {}

    changes: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for plan in plans:
        match = plan.match
        if not match.matched:
            continue
        observed_artist = plan.media.existing_artist or match.source_artist_credit
        observed_title = plan.media.existing_title
        artist_changed = bool(
            observed_artist
            and match.artist
            and normalize_display_text(observed_artist) != normalize_display_text(match.artist)
        )
        title_changed = bool(
            observed_title
            and match.title
            and normalize_display_text(observed_title) != normalize_display_text(match.title)
        )
        source_credit_differs = bool(
            match.source_artist_credit
            and match.artist
            and normalize_display_text(match.source_artist_credit) != normalize_display_text(match.artist)
        )
        if artist_changed or title_changed or source_credit_differs:
            changes.append(
                {
                    "run_id": run_id,
                    "mode": mode,
                    "path": str(plan.media.path),
                    "stable_recording_id": match.musicbrainz_recording_id or match.isrc or match.acoustid_id or "",
                    "musicbrainz_artist_ids": " | ".join(match.musicbrainz_artist_ids),
                    "existing_artist": plan.media.existing_artist or "",
                    "source_artist_credit": match.source_artist_credit or "",
                    "canonical_artist": match.artist or "",
                    "existing_title": plan.media.existing_title or "",
                    "canonical_title": match.title or "",
                    "artist_changed": artist_changed,
                    "title_changed": title_changed,
                    "source_credit_differs": source_credit_differs,
                    "canonicalization_status": match.canonicalization_status,
                    "canonicalization_score": f"{match.canonicalization_score:.1f}",
                    "source": match.source,
                }
            )
        if match.repository_conflicts:
            conflicts.append(
                {
                    "run_id": run_id,
                    "mode": mode,
                    "path": str(plan.media.path),
                    "artist": match.artist or "",
                    "title": match.title or "",
                    "mb_recording_id": match.musicbrainz_recording_id or "",
                    "canonicalization_status": match.canonicalization_status,
                    "repository_agreement": " | ".join(match.repository_agreement),
                    "repository_conflicts": " | ".join(match.repository_conflicts),
                    "confidence": f"{match.confidence:.1f}",
                    "action": plan.action,
                    "status": plan.status,
                }
            )

    changes_path = output_dir / f"canonical_name_changes_{run_id}.csv"
    _write_dict_rows(
        changes_path,
        changes,
        [
            "run_id", "mode", "path", "stable_recording_id", "musicbrainz_artist_ids",
            "existing_artist", "source_artist_credit", "canonical_artist", "existing_title",
            "canonical_title", "artist_changed", "title_changed", "source_credit_differs",
            "canonicalization_status", "canonicalization_score", "source",
        ],
    )
    paths["canonical_name_changes_csv"] = changes_path

    conflicts_path = output_dir / f"repository_name_conflicts_{run_id}.csv"
    _write_dict_rows(
        conflicts_path,
        conflicts,
        [
            "run_id", "mode", "path", "artist", "title", "mb_recording_id",
            "canonicalization_status", "repository_agreement", "repository_conflicts",
            "confidence", "action", "status",
        ],
    )
    paths["repository_name_conflicts_csv"] = conflicts_path

    clusters = build_name_variant_clusters(plans, run_id, mode)
    cluster_path = output_dir / f"name_variant_clusters_{run_id}.csv"
    _write_dict_rows(
        cluster_path,
        clusters,
        [
            "run_id", "mode", "entity_type", "stable_id", "canonical_name", "observed_variants",
            "comparison_variants", "file_count", "path_sample",
        ],
    )
    paths["name_variant_clusters_csv"] = cluster_path
    return paths


def build_name_variant_clusters(plans: list[PlanResult], run_id: str, mode: str) -> list[dict[str, Any]]:
    clusters: dict[tuple[str, str], dict[str, Any]] = {}
    for plan in plans:
        match = plan.match
        if not match.matched:
            continue
        components = match.evidence.get("musicbrainz_artist_components") if isinstance(match.evidence, dict) else None
        if isinstance(components, list):
            for component in components:
                if not isinstance(component, dict) or not component.get("id"):
                    continue
                stable_id = str(component["id"])
                key = ("artist", stable_id)
                entry = clusters.setdefault(
                    key,
                    {
                        "entity_type": "artist",
                        "stable_id": stable_id,
                        "canonical_name": normalize_display_text(component.get("entity_name")),
                        "variants": set(),
                        "paths": set(),
                    },
                )
                for value in [component.get("entity_name"), component.get("credited_name"), plan.media.existing_artist]:
                    text = normalize_display_text(value)
                    if text:
                        entry["variants"].add(text)
                entry["paths"].add(str(plan.media.path))
        if match.musicbrainz_recording_id:
            key = ("recording", match.musicbrainz_recording_id)
            entry = clusters.setdefault(
                key,
                {
                    "entity_type": "recording",
                    "stable_id": match.musicbrainz_recording_id,
                    "canonical_name": match.title or "",
                    "variants": set(),
                    "paths": set(),
                },
            )
            for value in [match.title, plan.media.existing_title]:
                text = normalize_display_text(value)
                if text:
                    entry["variants"].add(text)
            for candidate in match.name_candidates:
                text = normalize_display_text(candidate.get("title")) if isinstance(candidate, dict) else ""
                if text:
                    entry["variants"].add(text)
            entry["paths"].add(str(plan.media.path))

    rows: list[dict[str, Any]] = []
    for (_entity_type, _stable_id), entry in sorted(clusters.items()):
        variants = sorted(entry["variants"], key=lambda value: (comparison_key(value), value.casefold()))
        comparison_variants = sorted({comparison_key(value) for value in variants if comparison_key(value)})
        # Include every stable-ID cluster: even one visible variant confirms consistency.
        rows.append(
            {
                "run_id": run_id,
                "mode": mode,
                "entity_type": entry["entity_type"],
                "stable_id": entry["stable_id"],
                "canonical_name": entry["canonical_name"],
                "observed_variants": " | ".join(variants),
                "comparison_variants": " | ".join(comparison_variants),
                "file_count": len(entry["paths"]),
                "path_sample": " | ".join(sorted(entry["paths"])[:5]),
            }
        )
    return rows


def _write_dict_rows(path: Path, rows: list[dict[str, Any]], fields: list[str]) -> None:
    with atomic_text_writer(path, encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def write_scan_coverage(coverage: ScanCoverage, output_dir: Path, run_id: str) -> dict[str, Path]:
    paths: dict[str, Path] = {}
    json_path = output_dir / f"scan_coverage_{run_id}.json"
    write_json_atomic(json_path, dataclass_to_jsonable(coverage))
    paths["scan_coverage_json"] = json_path

    csv_path = output_dir / f"scan_coverage_{run_id}.csv"
    flat_rows = [
        ("status", coverage.status),
        ("root", coverage.root),
        ("recursive", coverage.recursive),
        ("require_recursive_scan", coverage.require_recursive_scan),
        ("all_reachable_subfolders_checked", coverage.all_reachable_subfolders_checked),
        ("follow_directory_symlinks", coverage.follow_directory_symlinks),
        ("directories_visited", coverage.directories_visited),
        ("subdirectories_discovered", coverage.subdirectories_discovered),
        ("directories_excluded", coverage.directories_excluded),
        ("directory_symlinks_skipped", coverage.directory_symlinks_skipped),
        ("directory_error_count", len(coverage.directory_errors)),
        ("files_seen", coverage.files_seen),
        ("unsupported_files_seen", coverage.unsupported_files_seen),
        ("media_files_found", coverage.media_files_found),
        ("media_files_scanned", coverage.media_files_scanned),
        ("media_scan_errors", coverage.media_scan_errors),
        ("inventory_cache_hits", coverage.inventory_cache_hits),
        ("inventory_cache_misses", coverage.inventory_cache_misses),
        ("inventory_cache_writes", coverage.inventory_cache_writes),
        ("deepest_relative_depth", coverage.deepest_relative_depth),
        ("limit_reached", coverage.limit_reached),
        ("max_files_per_run", coverage.max_files_per_run),
        ("media_by_extension", json.dumps(coverage.media_by_extension, ensure_ascii=False, sort_keys=True)),
        ("media_by_depth", json.dumps(coverage.media_by_depth, ensure_ascii=False, sort_keys=True)),
    ]
    with atomic_text_writer(csv_path, encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["metric", "value"])
        writer.writerows(flat_rows)
    paths["scan_coverage_csv"] = csv_path

    if coverage.directory_errors:
        error_path = output_dir / f"unreadable_or_failed_paths_{run_id}.csv"
        with atomic_text_writer(error_path, encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=["path", "error"])
            writer.writeheader()
            writer.writerows(coverage.directory_errors)
        paths["scan_path_errors_csv"] = error_path
    return paths


def needs_review(plan: PlanResult, review_confidence: float) -> bool:
    if plan.match.source == "scan_only":
        return bool(plan.error or plan.media.scan_error)
    if plan.error or plan.media.scan_error:
        return True
    if not plan.match.matched:
        return True
    if plan.match.repository_conflicts or plan.match.canonicalization_status == "repository_conflict":
        return True
    if plan.match.apply_blockers or plan.match.ambiguity_status.startswith("ambiguous"):
        return True
    if plan.match.confidence < review_confidence:
        return True
    if plan.status in {
        "apply_failed",
        "processing_failed",
        "scan_error",
        "reported_only",
        "unmatched",
        "applied_with_warning",
        "source_changed_skipped",
        "metadata_verification_failed",
        "embedded_metadata_write_failed",
    }:
        return True
    return False


def find_duplicate_recording_groups(plans: list[PlanResult]) -> list[tuple[str, str, list[PlanResult]]]:
    by_mbid: dict[str, list[PlanResult]] = defaultdict(list)
    by_acoustid: dict[str, list[PlanResult]] = defaultdict(list)
    for plan in plans:
        if plan.match.musicbrainz_recording_id:
            by_mbid[plan.match.musicbrainz_recording_id].append(plan)
        elif plan.match.acoustid_id:
            by_acoustid[plan.match.acoustid_id].append(plan)
    groups: list[tuple[str, str, list[PlanResult]]] = []
    for key, items in sorted(by_mbid.items()):
        if len(items) > 1:
            groups.append(("musicbrainz_recording_id", key, items))
    for key, items in sorted(by_acoustid.items()):
        if len(items) > 1:
            groups.append(("acoustid_id", key, items))
    return groups


def write_duplicate_candidates(path: Path, groups: list[tuple[str, str, list[PlanResult]]], run_id: str, mode: str) -> None:
    fields = ["run_id", "mode", "identifier_type", "identifier", "group_size", "path", "artist", "title", "confidence", "status"]
    with atomic_text_writer(path, encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for identifier_type, identifier, items in groups:
            for plan in items:
                writer.writerow({
                    "run_id": run_id,
                    "mode": mode,
                    "identifier_type": identifier_type,
                    "identifier": identifier,
                    "group_size": len(items),
                    "path": str(plan.media.path),
                    "artist": plan.match.artist or "",
                    "title": plan.match.title or "",
                    "confidence": f"{plan.match.confidence:.1f}",
                    "status": plan.status,
                })


def find_acoustic_fingerprint_groups(plans: list[PlanResult]) -> list[tuple[str, int, list[PlanResult]]]:
    groups: dict[tuple[str, int], list[PlanResult]] = defaultdict(list)
    for plan in plans:
        fingerprint = plan.media.fingerprint
        duration = plan.media.fingerprint_duration
        if not fingerprint or duration is None:
            continue
        digest = hashlib.sha256(fingerprint.encode("utf-8")).hexdigest()
        groups[(digest, int(duration))].append(plan)
    return [
        (digest, duration, items)
        for (digest, duration), items in sorted(groups.items())
        if len(items) > 1
    ]


def write_acoustic_duplicate_clusters(
    path: Path,
    groups: list[tuple[str, int, list[PlanResult]]],
    run_id: str,
    mode: str,
) -> None:
    fields = [
        "run_id", "mode", "fingerprint_sha256", "fingerprint_duration_seconds", "group_size",
        "path", "size_bytes", "artist", "title", "mb_recording_id", "confidence", "status",
    ]
    with atomic_text_writer(path, encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for digest, duration, items in groups:
            for plan in items:
                writer.writerow({
                    "run_id": run_id,
                    "mode": mode,
                    "fingerprint_sha256": digest,
                    "fingerprint_duration_seconds": duration,
                    "group_size": len(items),
                    "path": str(plan.media.path),
                    "size_bytes": plan.media.size_bytes,
                    "artist": plan.match.artist or "",
                    "title": plan.match.title or "",
                    "mb_recording_id": plan.match.musicbrainz_recording_id or "",
                    "confidence": f"{plan.match.confidence:.1f}",
                    "status": plan.status,
                })


def build_summary(
    plans: list[PlanResult],
    run_id: str,
    mode: str,
    scan_coverage: ScanCoverage | None = None,
    duplicate_group_count: int = 0,
    review_confidence: float = 90.0,
) -> dict[str, Any]:
    status_counts = Counter(plan.status for plan in plans)
    action_counts = Counter(plan.action for plan in plans)
    genre_counts = Counter(plan.genre.main_genre for plan in plans if plan.genre)
    source_counts = Counter(plan.match.source for plan in plans)
    canonicalization_counts = Counter(plan.match.canonicalization_status for plan in plans)
    return {
        "schema": "MediaTaggerBot.summary.v5",
        "asset_metadata": {"schema": ASSET_METADATA_SCHEMA, "project_slug": PROJECT_SLUG, "run_manifest_expected": True},
        "run_id": run_id,
        "mode": mode,
        "total_planned": len(plans),
        "applied_count": sum(1 for plan in plans if plan.should_apply and plan.status == "applied"),
        "renamed_count": sum(1 for plan in plans if plan.renamed),
        "rename_verified_count": sum(1 for plan in plans if plan.rename_verified),
        "metadata_written_count": sum(1 for plan in plans if plan.metadata_written),
        "metadata_verified_count": sum(1 for plan in plans if plan.metadata_verified),
        "source_verified_count": sum(1 for plan in plans if plan.source_verified),
        "already_managed_skipped_count": sum(1 for plan in plans if plan.status == "already_managed_skipped"),
        "fingerprint_cache_hit_count": sum(1 for plan in plans if plan.media.fingerprint_cache_hit),
        "inventory_cache_hit_count": sum(1 for plan in plans if plan.media.inventory_cache_hit),
        "identity_memory_hit_count": sum(1 for plan in plans if plan.match.identity_cache_hit),
        "ambiguous_identity_count": sum(1 for plan in plans if plan.match.ambiguity_status.startswith("ambiguous")),
        "apply_blocker_count": sum(1 for plan in plans if plan.match.apply_blockers),
        "write_readiness_blocker_count": sum(1 for plan in plans if "write_readiness_blocked" in plan.match.apply_blockers or plan.status == "write_readiness_blocked"),
        "prior_text_identity_review_count": sum(
            1 for plan in plans
            if "prior_mediataggerbot_text_identity_requires_review" in plan.match.apply_blockers
        ),
        "source_changed_skip_count": sum(1 for plan in plans if plan.status == "source_changed_skipped"),
        "metadata_verification_failure_count": sum(1 for plan in plans if plan.status == "metadata_verification_failed"),
        "embedded_metadata_write_failure_count": sum(1 for plan in plans if plan.status == "embedded_metadata_write_failed"),
        "scan_error_count": sum(1 for plan in plans if plan.status == "scan_error"),
        "apply_failure_count": sum(1 for plan in plans if plan.status == "apply_failed"),
        "per_file_processing_failure_count": sum(1 for plan in plans if plan.status == "processing_failed"),
        "applied_with_warning_count": sum(1 for plan in plans if plan.status == "applied_with_warning"),
        "rename_verification_failure_count": sum(1 for plan in plans if plan.renamed and not plan.rename_verified),
        "review_confidence_threshold": review_confidence,
        "needs_review_count": sum(1 for plan in plans if needs_review(plan, review_confidence)),
        "repository_conflict_count": sum(1 for plan in plans if plan.match.repository_conflicts),
        "canonical_name_change_count": sum(1 for plan in plans if _plan_has_visible_name_change(plan)),
        "stable_artist_id_count": len({mbid for plan in plans for mbid in plan.match.musicbrainz_artist_ids}),
        "stable_recording_id_count": len({plan.match.musicbrainz_recording_id for plan in plans if plan.match.musicbrainz_recording_id}),
        "duplicate_recording_group_count": duplicate_group_count,
        "status_counts": dict(status_counts),
        "action_counts": dict(action_counts),
        "genre_counts": dict(genre_counts),
        "source_counts": dict(source_counts),
        "canonicalization_counts": dict(canonicalization_counts),
        "scan_coverage": _compact_coverage(scan_coverage),
        "errors": [
            {"path": str(plan.media.path), "error": plan.error or plan.media.scan_error}
            for plan in plans
            if plan.error or plan.media.scan_error
        ][:100],
    }


def _plan_has_visible_name_change(plan: PlanResult) -> bool:
    match = plan.match
    return bool(
        (plan.media.existing_artist and match.artist and normalize_display_text(plan.media.existing_artist) != normalize_display_text(match.artist))
        or (plan.media.existing_title and match.title and normalize_display_text(plan.media.existing_title) != normalize_display_text(match.title))
        or (match.source_artist_credit and match.artist and normalize_display_text(match.source_artist_credit) != normalize_display_text(match.artist))
    )


def _compact_coverage(coverage: ScanCoverage | None) -> dict[str, Any] | None:
    if coverage is None:
        return None
    return {
        "status": coverage.status,
        "all_reachable_subfolders_checked": coverage.all_reachable_subfolders_checked,
        "directories_visited": coverage.directories_visited,
        "subdirectories_discovered": coverage.subdirectories_discovered,
        "directories_excluded": coverage.directories_excluded,
        "directory_symlinks_skipped": coverage.directory_symlinks_skipped,
        "directory_error_count": len(coverage.directory_errors),
        "media_files_found": coverage.media_files_found,
        "media_files_scanned": coverage.media_files_scanned,
        "deepest_relative_depth": coverage.deepest_relative_depth,
        "limit_reached": coverage.limit_reached,
    }


def write_html_summary(path: Path, summary: dict[str, Any], sample_plans: list[PlanResult]) -> None:
    rows = []
    table_fields = [
        "status", "action", "relative_depth", "confidence", "source", "identity_tier", "ambiguity_status",
        "candidate_margin", "artist", "title", "canonicalization_status", "repository_conflicts",
        "genre", "subgenre", "proposed_path", "error",
    ]
    for plan in sample_plans:
        row = plan_to_row(plan, summary["run_id"], summary["mode"])
        rows.append(
            "<tr>" + "".join(f"<td>{html.escape(str(row.get(field, '')))}</td>" for field in table_fields) + "</tr>"
        )
    headings = "".join(f"<th>{html.escape(field.replace('_', ' ').title())}</th>" for field in table_fields)
    body = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>MediaTaggerBot Summary {html.escape(summary['run_id'])}</title>
<style>body{{font-family:Segoe UI,Arial,sans-serif;margin:24px}} table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #ddd;padding:6px;font-size:12px}} th{{background:#eee;text-align:left}} code{{background:#eee;padding:2px 4px}}</style></head>
<body>
<h1>MediaTaggerBot Summary</h1>
<p><b>Run:</b> <code>{html.escape(summary['run_id'])}</code> &nbsp; <b>Mode:</b> {html.escape(summary['mode'])}</p>
<h2>Counts, canonicalization, and recursive coverage</h2>
<pre>{html.escape(json.dumps(summary, indent=2, ensure_ascii=False))}</pre>
<h2>First {len(sample_plans)} planned items</h2>
<table><thead><tr>{headings}</tr></thead><tbody>{''.join(rows)}</tbody></table>
</body></html>
"""
    write_text_atomic(path, body, encoding="utf-8")


def write_rollback_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = ["original_path", "new_path", "metadata_sidecar", "source_verified", "renamed", "rename_verified", "metadata_written", "metadata_verified", "post_apply_size_bytes", "post_apply_modified_ns", "operation_id", "run_id"]
    with atomic_text_writer(path, encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
