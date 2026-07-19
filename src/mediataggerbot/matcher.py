from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import fields, replace
from typing import Any

from .cache import JsonCache
from .canonicalization import canonicalize_match
from .config import AppConfig
from .databases import AcoustIDClient, DiscogsClient, LastFmClient, MusicBrainzClient
from .models import MatchResult, MediaFile, dataclass_to_jsonable
from .genre import map_term_to_main, normalize_genre_key
from .utils import compact_list, comparison_key, normalize_display_text, normalize_joinphrase, normalize_text, text_similarity
from .version_identity import best_title_similarity, strip_presentation_noise, summarize_version_evidence, version_compatibility

LOG = logging.getLogger(__name__)


class Matcher:
    def __init__(
        self,
        config: AppConfig,
        acoustid: AcoustIDClient | None,
        musicbrainz: MusicBrainzClient | None,
        lastfm: LastFmClient | None,
        discogs: DiscogsClient | None,
        cache: JsonCache | None = None,
    ) -> None:
        self.config = config
        self.acoustid = acoustid
        self.musicbrainz = musicbrainz
        self.lastfm = lastfm
        self.discogs = discogs
        self.cache = cache
        self.identity_memory_stats = {"hits": 0, "misses": 0, "writes": 0}

    def match(self, media: MediaFile) -> MatchResult:
        prior_untrusted_text_identity = is_untrusted_prior_bot_text_identity(media)
        cached_identity = None if prior_untrusted_text_identity else self._load_identity_memory(media)
        if cached_identity is not None:
            return canonicalize_match(cached_identity, media, self.config)

        # Fastest/highest-confidence path: reuse embedded MusicBrainz recording IDs or ISRCs.
        if (
            self.musicbrainz
            and bool(self.config.get("apis.enable_musicbrainz", True))
            and bool(self.config.get("processing.prefer_existing_identifier_shortcuts", True))
            and not prior_untrusted_text_identity
        ):
            try:
                result = self._match_existing_identifiers(media)
                if result and result.matched:
                    return self._finalize(result, media)
            except Exception as exc:
                LOG.warning("Existing identifier lookup failed for %s: %s", media.path, exc)

        # Best remaining path: acoustic fingerprint -> AcoustID -> MusicBrainz enrichment.
        if self.acoustid and self.acoustid.enabled and media.fingerprint and media.fingerprint_duration:
            try:
                result = self._match_acoustid(media)
                if result and result.matched:
                    return self._finalize(result, media)
            except Exception as exc:
                LOG.warning("AcoustID match failed for %s: %s", media.path, exc)

        # Next: existing metadata or filename -> MusicBrainz search.
        artist, title, parse_source = self._best_text_identity(media)
        if title and self.musicbrainz and bool(self.config.get("apis.enable_musicbrainz", True)):
            try:
                result = self._match_musicbrainz_search(media, artist, title, parse_source)
                if result and result.matched:
                    if prior_untrusted_text_identity:
                        result.apply_blockers.append("prior_mediataggerbot_text_identity_requires_review")
                        result.notes.append(
                            "A prior MediaTaggerBot text-search identity is being revalidated; apply-safe will not trust its embedded MBID/ISRC without independent evidence."
                        )
                    return self._finalize(result, media)
            except Exception as exc:
                LOG.warning("MusicBrainz text match failed for %s: %s", media.path, exc)

        # Last resort: existing tags/filename only, optionally usable in apply-all.
        if title:
            notes = [f"No MusicBrainz/AcoustID identity; starting from {parse_source}."]
            raw = compact_list([media.existing_genre])
            confidence = 55.0 if parse_source == "existing_tags" else 40.0
            result = MatchResult(
                matched=True,
                confidence=confidence,
                source=parse_source,
                artist=artist,
                source_artist_credit=artist,
                title=title,
                album=media.existing_album,
                album_artist=media.existing_album_artist,
                date=media.existing_date,
                isrc=media.existing_isrc,
                musicbrainz_recording_id=media.existing_musicbrainz_recording_id,
                musicbrainz_release_id=media.existing_musicbrainz_release_id,
                musicbrainz_release_group_id=media.existing_musicbrainz_release_group_id,
                acoustid_id=media.existing_acoustid_id,
                raw_genres=raw,
                raw_tags=raw,
                notes=notes,
                candidate_count=1,
                ambiguity_status="local_only",
                identity_tier="local_fallback",
                apply_blockers=[
                    "no_stable_repository_identity",
                    *(["prior_mediataggerbot_text_identity_requires_review"] if prior_untrusted_text_identity else []),
                ],
            )
            return self._finalize(result, media)

        return canonicalize_match(
            MatchResult(
                matched=False,
                confidence=0.0,
                source="unmatched",
                notes=["No fingerprint, tag, or filename identity found."],
                ambiguity_status="unmatched",
                identity_tier="unmatched",
                apply_blockers=["unmatched"],
            ),
            media,
            self.config,
        )

    def _finalize(self, result: MatchResult, media: MediaFile) -> MatchResult:
        enriched = self._enrich_secondary_sources(result)
        enriched = self._apply_identity_defaults(enriched)
        self._store_identity_memory(enriched, media)
        return canonicalize_match(enriched, media, self.config)

    def _match_existing_identifiers(self, media: MediaFile) -> MatchResult | None:
        assert self.musicbrainz is not None
        if media.existing_musicbrainz_recording_id:
            payload = self.musicbrainz.lookup_recording(media.existing_musicbrainz_recording_id)
            if payload:
                result = musicbrainz_recording_to_match(
                    payload,
                    source="musicbrainz_recording_id_tag",
                    confidence=99.0,
                )
                result.notes.append("Matched directly from an embedded MusicBrainz recording ID.")
                result.evidence["embedded_musicbrainz_recording_id"] = media.existing_musicbrainz_recording_id
                result.candidate_count = 1
                result.ambiguity_status = "exact_stable_identifier"
                result.identity_tier = "stable_identifier"
                return result

        valid_isrc = normalize_valid_isrc(media.existing_isrc)
        if valid_isrc:
            candidates = self.musicbrainz.lookup_isrc(valid_isrc)
            if candidates:
                scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
                query_title = media.existing_title or parse_artist_title_from_filename(media.path.stem)[1]
                for candidate in candidates:
                    confidence = 96.0 if len(candidates) == 1 else 92.0
                    candidate_title = normalize_text(candidate.get("title"))
                    candidate_artist = extract_mb_artist_credit(candidate.get("artist-credit"), canonical_entity=True)
                    if media.existing_title and best_title_similarity(media.existing_title, candidate_title) >= 0.98:
                        confidence += 2.0
                    if media.existing_artist and candidate_artist and text_similarity(media.existing_artist, candidate_artist) >= 0.88:
                        confidence += 1.0
                    duration = _safe_float(candidate.get("length"))
                    if duration is not None:
                        duration /= 1000.0
                    duration_diff = None
                    if media.duration_seconds and duration:
                        duration_diff = abs(media.duration_seconds - duration)
                        if duration_diff <= 3.0:
                            confidence += 1.0
                        elif duration_diff > 15.0:
                            confidence -= min(15.0, duration_diff / 5.0)
                    version = version_compatibility(
                        query_title,
                        candidate_title,
                        match_bonus=float(self.config.get("matching.version_match_bonus", 5.0)),
                        mismatch_penalty=float(self.config.get("matching.version_mismatch_penalty", 14.0)),
                    )
                    confidence += float(version.get("score_adjustment") or 0.0)
                    media_kind_adjustment = recording_media_kind_adjustment(media, candidate)
                    confidence += media_kind_adjustment
                    scored.append((max(0.0, min(99.0, confidence)), candidate, {
                        "duration_difference_seconds": round(duration_diff, 3) if duration_diff is not None else None,
                        "version": version,
                        "media_kind_adjustment": media_kind_adjustment,
                        "candidate_video": bool(candidate.get("video", False)),
                    }))
                scored.sort(key=lambda item: item[0], reverse=True)
                best_confidence, best_payload, best_details = scored[0]
                runner = scored[1][0] if len(scored) > 1 else None
                margin = round(best_confidence - runner, 3) if runner is not None else None
                ambiguous = runner is not None and margin is not None and margin < 3.0
                result = musicbrainz_recording_to_match(
                    best_payload,
                    source="musicbrainz_isrc_tag",
                    confidence=best_confidence,
                )
                result.isrc = result.isrc or valid_isrc
                result.notes.append(
                    f"Matched from embedded ISRC; MusicBrainz returned {len(candidates)} recording candidate(s)."
                )
                result.evidence["embedded_isrc"] = valid_isrc
                result.evidence["isrc_candidate_count"] = len(candidates)
                result.evidence["isrc_candidate_margin"] = margin
                result.evidence["selected_version_evidence"] = best_details["version"]
                result.candidate_count = len(candidates)
                result.candidate_margin = margin
                result.ambiguity_status = "ambiguous_isrc_candidates" if ambiguous else ("single_candidate" if len(candidates) == 1 else "clear_margin")
                result.identity_tier = "stable_identifier"
                result.version_evidence = summarize_version_evidence([best_details["version"]])
                if ambiguous:
                    result.apply_blockers.append("ambiguous_isrc_candidates")
                return result
        return None

    def _match_acoustid(self, media: MediaFile) -> MatchResult | None:
        assert self.acoustid is not None
        payload = self.acoustid.lookup_fingerprint(media.fingerprint_duration or 0, media.fingerprint or "")
        if not payload or payload.get("status") != "ok":
            return None
        candidates: list[MatchResult] = []
        for result in payload.get("results", []) or []:
            if not isinstance(result, dict):
                continue
            score = float(result.get("score") or 0.0)
            recordings = result.get("recordings") or []
            if isinstance(recordings, dict):
                recordings = [recordings]
            for recording in recordings:
                if not isinstance(recording, dict):
                    continue
                candidate = self._match_from_acoustid_recording(recording, result, media, score)
                if candidate:
                    candidates.append(candidate)
        if not candidates:
            return None
        candidates.sort(key=lambda item: item.confidence, reverse=True)
        best = candidates[0]
        runner = candidates[1].confidence if len(candidates) > 1 else None
        margin = round(best.confidence - runner, 3) if runner is not None else None
        competing_ids = {item.musicbrainz_recording_id for item in candidates[:3] if item.musicbrainz_recording_id}
        ambiguous = len(competing_ids) > 1 and margin is not None and margin < 3.0
        best.candidate_count = len(candidates)
        best.candidate_margin = margin
        best.ambiguity_status = "ambiguous_fingerprint_candidates" if ambiguous else ("single_candidate" if len(candidates) == 1 else "clear_margin")
        best.identity_tier = "fingerprint"
        best.evidence["acoustid_candidate_count"] = len(candidates)
        best.evidence["acoustid_candidate_margin"] = margin
        best.evidence["acoustid_top_candidates"] = [
            {
                "recording_id": item.musicbrainz_recording_id,
                "artist": item.artist,
                "title": item.title,
                "confidence": round(item.confidence, 3),
                "acoustid_score": item.acoustid_score,
            }
            for item in candidates[:5]
        ]
        if ambiguous:
            best.apply_blockers.append("ambiguous_fingerprint_candidates")
            best.notes.append(
                f"Fingerprint maps to close competing recordings (margin {margin:.1f}); apply-safe review required."
            )
        if (
            best.musicbrainz_recording_id
            and self.musicbrainz
            and bool(self.config.get("apis.enable_musicbrainz", True))
        ):
            enriched = self.musicbrainz.lookup_recording(best.musicbrainz_recording_id)
            if enriched:
                best = self._merge_musicbrainz_recording(best, enriched, confidence_bonus=3.0)
        return best

    def _match_from_acoustid_recording(
        self,
        recording: dict[str, Any],
        acoustid_result: dict[str, Any],
        media: MediaFile,
        score: float,
    ) -> MatchResult | None:
        title = normalize_text(recording.get("title"))
        artist_components = extract_acoustid_artist_components(recording)
        artist = render_artist_components(artist_components, canonical_entity=True)
        mbid = normalize_display_text(recording.get("id")) or None
        if not title and not mbid:
            return None
        duration = _safe_float(recording.get("duration"))
        confidence = max(0.0, min(100.0, score * 100.0))
        notes: list[str] = []
        compare_duration = media.duration_seconds or media.fingerprint_duration
        if compare_duration and duration:
            diff = abs(float(compare_duration) - float(duration))
            if diff <= 3:
                confidence = min(100.0, confidence + 2.0)
            elif diff <= 12:
                notes.append(f"Duration differs by {diff:.1f}s.")
            else:
                penalty = min(25.0, diff / 4.0)
                confidence = max(0.0, confidence - penalty)
                notes.append(f"Duration differs by {diff:.1f}s; confidence penalized.")
        query_title = media.existing_title or parse_artist_title_from_filename(media.path.stem)[1]
        version = version_compatibility(
            query_title,
            title,
            match_bonus=float(self.config.get("matching.version_match_bonus", 5.0)),
            mismatch_penalty=float(self.config.get("matching.version_mismatch_penalty", 14.0)),
        )
        media_kind_adjustment = recording_media_kind_adjustment(media, recording)
        confidence = max(
            0.0,
            min(100.0, confidence + float(version.get("score_adjustment") or 0.0) + media_kind_adjustment),
        )
        raw_genres = collect_terms(recording.get("genres"))
        raw_tags = collect_terms(recording.get("tags"))
        release_group_id = None
        release_groups = recording.get("releasegroups") or recording.get("release-groups") or []
        if isinstance(release_groups, list) and release_groups:
            rg = release_groups[0]
            if isinstance(rg, dict):
                release_group_id = rg.get("id")
        evidence = {
            "acoustid_id": acoustid_result.get("id"),
            "acoustid_score": score,
            "musicbrainz_artist_components": artist_components,
            "selected_version_evidence": version,
            "media_kind_adjustment": media_kind_adjustment,
            "candidate_video": bool(recording.get("video", False)),
        }
        return MatchResult(
            matched=True,
            confidence=confidence,
            source="acoustid+musicbrainz",
            artist=artist,
            source_artist_credit=artist,
            musicbrainz_artist_ids=[str(item["id"]) for item in artist_components if item.get("id")],
            title=title,
            musicbrainz_recording_id=mbid,
            musicbrainz_release_group_id=normalize_display_text(release_group_id) or None,
            acoustid_id=normalize_display_text(acoustid_result.get("id")) or None,
            acoustid_score=score,
            raw_genres=raw_genres,
            raw_tags=raw_tags,
            notes=notes,
            evidence=evidence,
            identity_tier="fingerprint",
            version_evidence=summarize_version_evidence([version]),
        )

    def _match_musicbrainz_search(
        self,
        media: MediaFile,
        artist: str | None,
        title: str,
        parse_source: str,
    ) -> MatchResult | None:
        assert self.musicbrainz is not None
        limit = max(2, min(25, int(self.config.get("matching.text_search_candidate_limit", 8))))
        candidates = self.musicbrainz.search_recording(artist, title, limit=limit)
        scored: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
        version_items: list[dict[str, object]] = []
        for candidate in candidates:
            mb_score = float(candidate.get("score") or 0.0)
            candidate_title = normalize_text(candidate.get("title"))
            candidate_artist = extract_mb_artist_credit(candidate.get("artist-credit"), canonical_entity=True)
            confidence = min(93.0, mb_score * 0.88)
            artist_similarity = text_similarity(artist, candidate_artist) if artist and candidate_artist else 0.0
            if artist and candidate_artist:
                if artist_similarity >= 0.94:
                    confidence += 4.0
                elif artist_similarity < 0.60:
                    confidence -= 8.0
            title_similarity = best_title_similarity(title, candidate_title)
            if title_similarity >= 0.98:
                confidence += 4.0
            elif title_similarity < 0.70:
                confidence -= 8.0
            if parse_source == "existing_tags":
                confidence += 3.0

            version = version_compatibility(
                title,
                candidate_title,
                match_bonus=float(self.config.get("matching.version_match_bonus", 5.0)),
                mismatch_penalty=float(self.config.get("matching.version_mismatch_penalty", 14.0)),
            )
            confidence += float(version.get("score_adjustment") or 0.0)
            version_items.append(version)
            media_kind_adjustment = recording_media_kind_adjustment(media, candidate)
            confidence += media_kind_adjustment

            length_ms = _safe_float(candidate.get("length"))
            duration_difference = None
            if media.duration_seconds and length_ms:
                duration_difference = abs(media.duration_seconds - length_ms / 1000.0)
                if duration_difference <= 3.0:
                    confidence += 2.0
                elif duration_difference > 20.0:
                    confidence -= min(12.0, duration_difference / 6.0)
            # Preserve the small media-kind tie-break after normal confidence capping so
            # two otherwise identical MusicBrainz candidates do not erase the video flag.
            confidence = max(0.0, min(95.0, confidence - media_kind_adjustment))
            confidence = max(0.0, min(97.0, confidence + media_kind_adjustment))
            safe_evidence = build_text_apply_safe_evidence(
                media=media,
                parse_source=parse_source,
                query_artist=artist,
                query_title=title,
                candidate_artist=candidate_artist,
                candidate_title=candidate_title,
                candidate=candidate,
                artist_similarity=artist_similarity,
                title_similarity=title_similarity,
                duration_difference=duration_difference,
            )
            scored.append((confidence, candidate, {
                "musicbrainz_score": mb_score,
                "artist_similarity": round(artist_similarity, 6),
                "title_similarity": round(title_similarity, 6),
                "duration_difference_seconds": round(duration_difference, 3) if duration_difference is not None else None,
                "version": version,
                "media_kind_adjustment": media_kind_adjustment,
                "candidate_video": bool(candidate.get("video", False)),
                "apply_safe_text_evidence": safe_evidence,
            }))

        if not scored:
            return None
        scored.sort(key=lambda item: item[0], reverse=True)
        best_confidence, best_payload, best_details = scored[0]
        runner_up = scored[1][0] if len(scored) > 1 else None
        margin = round(best_confidence - runner_up, 3) if runner_up is not None else None
        min_margin = float(self.config.get("matching.min_text_candidate_margin_apply_safe", 6.0))
        ambiguous = runner_up is not None and margin is not None and margin < min_margin

        base = musicbrainz_recording_to_match(
            best_payload,
            source=f"musicbrainz_search_from_{parse_source}",
            confidence=best_confidence,
        )
        base.candidate_count = len(scored)
        base.candidate_margin = margin
        base.ambiguity_status = "ambiguous_close_candidates" if ambiguous else ("single_candidate" if len(scored) == 1 else "clear_margin")
        base.identity_tier = "text_ambiguous" if ambiguous else "text_repository"
        base.version_evidence = summarize_version_evidence([best_details["version"]])
        if ambiguous:
            base.apply_blockers.append("ambiguous_text_candidates")
            base.notes.append(
                f"Top MusicBrainz text candidates are separated by only {margin:.1f} confidence points; apply-safe review required."
            )

        safe_evidence = dict(best_details.get("apply_safe_text_evidence") or {})
        min_artist_similarity = float(self.config.get("matching.text_apply_safe_min_artist_similarity", 0.90))
        min_title_similarity = float(self.config.get("matching.text_apply_safe_min_title_similarity", 0.95))
        max_duration_difference = float(
            self.config.get("matching.text_apply_safe_max_duration_difference_seconds", 6.0)
        )
        if not artist or is_generic_artist(artist):
            base.apply_blockers.append("text_match_missing_or_generic_artist")
        if is_generic_title(title):
            base.apply_blockers.append("text_match_generic_title")
        if float(best_details.get("artist_similarity") or 0.0) < min_artist_similarity:
            base.apply_blockers.append("text_match_artist_similarity_below_safe")
        if float(best_details.get("title_similarity") or 0.0) < min_title_similarity:
            base.apply_blockers.append("text_match_title_similarity_below_safe")
        duration_difference = best_details.get("duration_difference_seconds")
        if duration_difference is not None and float(duration_difference) > max_duration_difference:
            base.apply_blockers.append("text_match_duration_difference_above_safe")
        if (
            bool(self.config.get("matching.text_apply_safe_require_independent_corroboration", True))
            and not safe_evidence.get("independent_identity_corroborated")
        ):
            base.apply_blockers.append("text_match_lacks_independent_corroboration")
        if is_untrusted_prior_bot_text_identity(media):
            base.apply_blockers.append("prior_mediataggerbot_text_identity_requires_review")
        if base.apply_blockers:
            base.notes.append(
                "Text-search match retained for reporting, but apply-safe blockers were recorded: "
                + ", ".join(compact_list(base.apply_blockers, limit=12))
                + "."
            )
        base.name_candidates.append(
            {
                "source": parse_source,
                "role": "query_input",
                "artist": artist,
                "title": title,
                "recording_id": media.existing_musicbrainz_recording_id,
            }
        )
        base.evidence["text_candidate_ranking"] = [
            {
                "rank": index + 1,
                "recording_id": normalize_display_text(candidate.get("id")) or None,
                "artist": extract_mb_artist_credit(candidate.get("artist-credit"), canonical_entity=True),
                "title": normalize_display_text(candidate.get("title")) or None,
                "confidence": round(confidence, 3),
                **details,
            }
            for index, (confidence, candidate, details) in enumerate(scored[:5])
        ]
        base.evidence["candidate_margin"] = margin
        base.evidence["candidate_count"] = len(scored)
        base.evidence["apply_safe_text_evidence"] = safe_evidence
        if base.musicbrainz_recording_id and self.musicbrainz:
            enriched = self.musicbrainz.lookup_recording(base.musicbrainz_recording_id)
            if enriched:
                base = self._merge_musicbrainz_recording(base, enriched, confidence_bonus=2.0)
        return base

    def _merge_musicbrainz_recording(
        self,
        base: MatchResult,
        payload: dict[str, Any],
        confidence_bonus: float = 0.0,
    ) -> MatchResult:
        merged = musicbrainz_recording_to_match(
            payload,
            source=base.source,
            confidence=min(100.0, base.confidence + confidence_bonus),
        )
        # Preserve fingerprint/identifier evidence and values if lookup payload is sparse.
        for field in ["acoustid_id", "acoustid_score", "isrc"]:
            if not getattr(merged, field):
                setattr(merged, field, getattr(base, field))
        if not merged.source_artist_credit:
            merged.source_artist_credit = base.source_artist_credit
        if not merged.musicbrainz_artist_ids:
            merged.musicbrainz_artist_ids = list(base.musicbrainz_artist_ids)
        merged.raw_genres = compact_list(base.raw_genres + merged.raw_genres, limit=20)
        merged.raw_tags = compact_list(base.raw_tags + merged.raw_tags, limit=25)
        merged.notes = compact_list(base.notes + merged.notes, limit=20)
        merged.name_candidates = _merge_candidate_lists(base.name_candidates, merged.name_candidates)
        merged.evidence = {**base.evidence, **merged.evidence}
        merged.candidate_count = base.candidate_count
        merged.candidate_margin = base.candidate_margin
        merged.ambiguity_status = base.ambiguity_status
        merged.identity_tier = base.identity_tier
        merged.identity_cache_hit = base.identity_cache_hit
        merged.version_evidence = list(base.version_evidence)
        merged.apply_blockers = compact_list(base.apply_blockers + merged.apply_blockers, limit=20)
        return merged

    def _enrich_secondary_sources(self, result: MatchResult) -> MatchResult:
        raw_genres = list(result.raw_genres)
        raw_tags = list(result.raw_tags)
        notes = list(result.notes)
        evidence = dict(result.evidence)
        candidates = list(result.name_candidates)
        artist = result.artist
        title = result.title
        confidence = float(result.confidence)

        # MusicBrainz release-group genres are often better than recording-level tags.
        if (
            self.musicbrainz
            and result.musicbrainz_release_group_id
            and bool(self.config.get("apis.enable_musicbrainz", True))
            and len(raw_genres) < 2
        ):
            try:
                rg = self.musicbrainz.lookup_release_group(result.musicbrainz_release_group_id)
                if rg:
                    raw_genres.extend(collect_terms(rg.get("genres")))
                    raw_tags.extend(collect_terms(rg.get("tags")))
            except Exception as exc:
                notes.append(f"MusicBrainz release-group enrichment failed: {exc}")

        # One Last.fm track.getInfo call supplies autocorrected names, MBID and top tags.
        if (
            self.lastfm
            and self.lastfm.enabled
            and artist
            and title
            and bool(self.config.get("apis.enable_lastfm", True))
            and bool(self.config.get("canonicalization.crosscheck_lastfm", True))
        ):
            try:
                payload = self.lastfm.track_get_info(
                    artist=artist,
                    title=title,
                    autocorrect=True,
                )
                parsed = parse_lastfm_track_info(payload)
                if parsed:
                    candidates.append(
                        {
                            "source": "lastfm",
                            "role": "repository",
                            "artist": parsed.get("artist"),
                            "title": parsed.get("title"),
                            "recording_id": parsed.get("mbid"),
                        }
                    )
                    lastfm_tags = [str(v) for v in parsed.get("tags", []) if str(v).strip()]
                    if lastfm_tags:
                        if bool(self.config.get("genres.prefer_lastfm_for_subgenre", True)):
                            raw_tags = lastfm_tags + raw_tags
                        else:
                            raw_tags.extend(lastfm_tags)
                        evidence["lastfm_tags_used"] = True
                        evidence["lastfm_tags"] = compact_list(lastfm_tags, limit=20)
                    evidence["lastfm_name_candidate"] = {
                        "artist": parsed.get("artist"),
                        "title": parsed.get("title"),
                        "mbid": parsed.get("mbid"),
                    }
                    if (
                        result.source in {"filename_parse", "existing_tags"}
                        and bool(self.config.get("canonicalization.use_lastfm_corrections_for_local_fallback", True))
                        and parsed.get("artist")
                        and parsed.get("title")
                        and text_similarity(artist, parsed.get("artist")) >= 0.72
                        and text_similarity(title, parsed.get("title")) >= 0.72
                    ):
                        old_identity = f"{artist or ''} - {title or ''}".strip(" -")
                        artist = normalize_display_text(parsed.get("artist")) or artist
                        title = normalize_display_text(parsed.get("title")) or title
                        confidence = max(confidence, 68.0)
                        notes.append(
                            f"Last.fm autocorrect standardized local fallback identity: {old_identity!r} -> {artist} - {title}."
                        )
            except Exception as exc:
                notes.append(f"Last.fm enrichment failed: {exc}")

        # Last-resort MusicBrainz artist genres reduce blind fallback-to-Pop labeling. This
        # adds at most one cached artist lookup and runs only when recording/release/Last.fm
        # evidence contains no term that maps into the requested eight-bucket taxonomy.
        if (
            self.musicbrainz
            and result.musicbrainz_artist_ids
            and bool(self.config.get("apis.enable_musicbrainz", True))
            and bool(self.config.get("matching.musicbrainz_artist_genre_fallback", True))
            and not _has_mappable_genre_terms(raw_genres + raw_tags)
        ):
            try:
                artist_payload = self.musicbrainz.lookup_artist(result.musicbrainz_artist_ids[0])
                if artist_payload:
                    artist_genres = collect_terms(artist_payload.get("genres"))
                    artist_tags = collect_terms(artist_payload.get("tags"))
                    raw_genres.extend(artist_genres)
                    raw_tags.extend(artist_tags)
                    evidence["musicbrainz_artist_genre_fallback"] = {
                        "artist_id": result.musicbrainz_artist_ids[0],
                        "genres": compact_list(artist_genres, limit=10),
                        "tags": compact_list(artist_tags, limit=10),
                    }
            except Exception as exc:
                notes.append(f"MusicBrainz artist-genre fallback failed: {exc}")

        # Discogs is optional and disabled by default because it may require an extra release lookup.
        if (
            self.discogs
            and self.discogs.enabled
            and artist
            and title
            and bool(self.config.get("apis.enable_discogs", False))
            and bool(self.config.get("canonicalization.crosscheck_discogs", True))
        ):
            try:
                discogs_candidate = self._discogs_track_candidate(artist, title)
                if discogs_candidate:
                    candidates.append(
                        {
                            "source": "discogs",
                            "role": "repository",
                            "artist": discogs_candidate.get("artist"),
                            "title": discogs_candidate.get("title"),
                            "recording_id": None,
                        }
                    )
                    raw_genres.extend([str(v) for v in discogs_candidate.get("genres", [])])
                    raw_tags.extend([str(v) for v in discogs_candidate.get("styles", [])])
                    if not result.date and discogs_candidate.get("year"):
                        result.date = str(discogs_candidate["year"])
                    evidence["discogs_track_candidate"] = {
                        "release_id": discogs_candidate.get("release_id"),
                        "artist": discogs_candidate.get("artist"),
                        "title": discogs_candidate.get("title"),
                    }
            except Exception as exc:
                notes.append(f"Discogs enrichment failed: {exc}")

        return replace(
            result,
            confidence=max(0.0, min(100.0, confidence)),
            artist=artist,
            title=title,
            raw_genres=compact_list(raw_genres, limit=25),
            raw_tags=compact_list(raw_tags, limit=30),
            notes=compact_list(notes, limit=25),
            name_candidates=_merge_candidate_lists(candidates),
            evidence=evidence,
        )

    def _apply_identity_defaults(self, result: MatchResult) -> MatchResult:
        tier = result.identity_tier
        ambiguity = result.ambiguity_status
        count = result.candidate_count
        if tier == "unknown":
            if result.source.startswith(("musicbrainz_recording_id", "musicbrainz_isrc")):
                tier = "stable_identifier"
            elif result.source.startswith("acoustid"):
                tier = "fingerprint"
            elif result.source.startswith("musicbrainz_search"):
                tier = "text_repository"
            elif result.source in {"existing_tags", "filename_parse"}:
                tier = "local_fallback"
        if ambiguity == "not_evaluated":
            ambiguity = "exact_or_unambiguous" if tier in {"stable_identifier", "fingerprint"} else "not_evaluated"
        if count == 0 and result.matched:
            count = 1
        return replace(result, identity_tier=tier, ambiguity_status=ambiguity, candidate_count=count)

    def _identity_memory_keys(self, media: MediaFile, result: MatchResult | None = None) -> list[str]:
        keys: list[str] = []
        mbid = (result.musicbrainz_recording_id if result else None) or media.existing_musicbrainz_recording_id
        isrc = normalize_valid_isrc((result.isrc if result else None) or media.existing_isrc)
        if mbid:
            keys.append("mbid:" + str(mbid).strip().casefold())
        if isrc:
            keys.append("isrc:" + isrc)
        if media.fingerprint and media.fingerprint_duration:
            material = f"{int(media.fingerprint_duration)}\0{media.fingerprint}"
            keys.append("fingerprint:" + hashlib.sha256(material.encode("utf-8")).hexdigest())
        return compact_list(keys, limit=5)

    def _load_identity_memory(self, media: MediaFile) -> MatchResult | None:
        if not self.cache or not bool(self.config.get("matching.identity_memory_enabled", True)):
            return None
        keys = self._identity_memory_keys(media)
        if not keys:
            return None
        allowed = {item.name for item in fields(MatchResult)}
        for key in keys:
            payload = self.cache.get("identity_memory_v2", key)
            if not isinstance(payload, dict) or not isinstance(payload.get("match"), dict):
                continue
            try:
                raw = {name: value for name, value in payload["match"].items() if name in allowed}
                result = MatchResult(**raw)
            except Exception:
                continue
            evidence = dict(result.evidence)
            evidence["identity_memory"] = {"cache_key_type": key.split(":", 1)[0], "stored_source": result.source}
            notes = compact_list(result.notes + ["Reused a previously resolved high-confidence repository identity from local stable identity memory."], limit=30)
            self.identity_memory_stats["hits"] += 1
            return replace(result, identity_cache_hit=True, evidence=evidence, notes=notes)
        self.identity_memory_stats["misses"] += 1
        return None

    def _store_identity_memory(self, result: MatchResult, media: MediaFile) -> None:
        if not self.cache or not bool(self.config.get("matching.identity_memory_enabled", True)):
            return
        if not result.matched or result.confidence < 85.0:
            return
        if result.apply_blockers or result.repository_conflicts or result.ambiguity_status.startswith("ambiguous"):
            return
        authoritative_tier = result.identity_tier in {"stable_identifier", "fingerprint"}
        # Text-search identities are deliberately not promoted into durable identity memory.
        # The v0.5.2 production evidence showed that high text scores can still confirm bad
        # local tags; only MBID/ISRC shortcuts or acoustic fingerprints are authoritative here.
        if not authoritative_tier:
            return
        if not (result.musicbrainz_recording_id or media.fingerprint):
            return
        payload = {
            "schema": "MediaTaggerBot.identity_memory.v2",
            "match": dataclass_to_jsonable(replace(result, identity_cache_hit=False)),
        }
        for key in self._identity_memory_keys(media, result):
            self.cache.set("identity_memory_v2", key, payload)
            self.identity_memory_stats["writes"] += 1

    def _discogs_track_candidate(self, artist: str, title: str) -> dict[str, Any] | None:
        assert self.discogs is not None
        search_results = self.discogs.search_track(artist, title, limit=3)
        best: tuple[float, dict[str, Any]] | None = None
        # Bound network work: inspect at most two release details.
        for result in search_results[:2]:
            release_id = result.get("id")
            if not release_id:
                continue
            details = self.discogs.lookup_release(str(release_id))
            if not details:
                continue
            release_artist = extract_discogs_artist(details.get("artists")) or extract_discogs_search_artist(result)
            tracklist = details.get("tracklist") if isinstance(details.get("tracklist"), list) else []
            for track in tracklist:
                if not isinstance(track, dict):
                    continue
                track_title = normalize_display_text(track.get("title"))
                if not track_title:
                    continue
                track_artist = extract_discogs_artist(track.get("artists")) or release_artist
                title_score = text_similarity(title, track_title)
                artist_score = max(text_similarity(artist, track_artist), text_similarity(artist, release_artist))
                score = 0.72 * title_score + 0.28 * artist_score
                candidate = {
                    "release_id": str(release_id),
                    "artist": track_artist or release_artist,
                    "title": track_title,
                    "genres": details.get("genres") if isinstance(details.get("genres"), list) else [],
                    "styles": details.get("styles") if isinstance(details.get("styles"), list) else [],
                    "year": details.get("year"),
                    "score": score,
                }
                if best is None or score > best[0]:
                    best = (score, candidate)
        return best[1] if best and best[0] >= 0.74 else None

    def _best_text_identity(self, media: MediaFile) -> tuple[str | None, str | None, str]:
        if media.existing_title:
            artist = normalize_text(media.existing_artist)
            title = normalize_text(media.existing_title)
            source = (
                "existing_mediataggerbot_text_tags"
                if is_untrusted_prior_bot_text_identity(media)
                else "existing_tags"
            )
            return artist or None, title or None, source
        parsed_artist, parsed_title = parse_artist_title_from_filename(media.path.stem)
        return parsed_artist, parsed_title, "filename_parse"


def musicbrainz_recording_to_match(payload: dict[str, Any], source: str, confidence: float) -> MatchResult:
    components = extract_mb_artist_components(payload.get("artist-credit"))
    artist = render_artist_components(components, canonical_entity=True)
    source_artist_credit = render_artist_components(components, canonical_entity=False)
    title = normalize_text(payload.get("title"))
    release = choose_release(payload.get("releases"))
    album = None
    album_artist = None
    date = None
    release_id = None
    release_group_id = None
    release_artist_components: list[dict[str, Any]] = []
    if release:
        album = normalize_text(release.get("title"))
        date = normalize_text(release.get("date"))
        release_id = normalize_display_text(release.get("id")) or None
        release_artist_components = extract_mb_artist_components(release.get("artist-credit"))
        album_artist = render_artist_components(release_artist_components, canonical_entity=True)
        rg = release.get("release-group") if isinstance(release.get("release-group"), dict) else None
        if rg:
            release_group_id = normalize_display_text(rg.get("id")) or None
    raw_genres = collect_terms(payload.get("genres"))
    raw_tags = collect_terms(payload.get("tags"))
    if release:
        raw_genres += collect_terms(release.get("genres"))
        raw_tags += collect_terms(release.get("tags"))
        rg = release.get("release-group") if isinstance(release.get("release-group"), dict) else None
        if rg:
            raw_genres += collect_terms(rg.get("genres"))
            raw_tags += collect_terms(rg.get("tags"))
    isrcs = payload.get("isrcs") or []
    isrc = normalize_valid_isrc(isrcs[0]) if isinstance(isrcs, list) and isrcs else None
    recording_id = normalize_display_text(payload.get("id")) or None
    evidence = {
        "musicbrainz_score": payload.get("score"),
        "musicbrainz_artist_components": components,
        "musicbrainz_release_artist_components": release_artist_components,
    }
    return MatchResult(
        matched=bool(title or recording_id),
        confidence=confidence,
        source=source,
        artist=artist,
        source_artist_credit=source_artist_credit,
        musicbrainz_artist_ids=[str(item["id"]) for item in components if item.get("id")],
        title=title,
        album=album,
        album_artist=album_artist,
        date=date,
        original_year=(date[:4] if date and len(date) >= 4 else None),
        isrc=isrc,
        musicbrainz_recording_id=recording_id,
        musicbrainz_release_id=release_id,
        musicbrainz_release_group_id=release_group_id,
        raw_genres=compact_list(raw_genres, limit=20),
        raw_tags=compact_list(raw_tags, limit=25),
        name_candidates=[
            {
                "source": "musicbrainz",
                "role": "canonical",
                "artist": artist,
                "title": title,
                "recording_id": recording_id,
            }
        ],
        evidence=evidence,
    )


def choose_release(releases: Any) -> dict[str, Any] | None:
    if not isinstance(releases, list) or not releases:
        return None

    def key(rel: dict[str, Any]) -> tuple[int, str]:
        status = str(rel.get("status") or "").casefold()
        date = str(rel.get("date") or "9999")
        score = 0 if status == "official" else 1
        return score, date

    dicts = [r for r in releases if isinstance(r, dict)]
    if not dicts:
        return None
    return sorted(dicts, key=key)[0]


def extract_mb_artist_components(artist_credit: Any) -> list[dict[str, Any]]:
    if not isinstance(artist_credit, list):
        return []
    components: list[dict[str, Any]] = []
    for item in artist_credit:
        if isinstance(item, str):
            text = normalize_display_text(item)
            if text:
                components.append(
                    {"id": None, "entity_name": text, "credited_name": text, "joinphrase": ""}
                )
            continue
        if not isinstance(item, dict):
            continue
        artist_node = item.get("artist") if isinstance(item.get("artist"), dict) else {}
        entity_name = normalize_display_text(artist_node.get("name") or item.get("name"))
        credited_name = normalize_display_text(item.get("name") or entity_name)
        joinphrase = normalize_joinphrase(item.get("joinphrase"))
        mbid = normalize_display_text(artist_node.get("id")) or None
        if entity_name or credited_name:
            components.append(
                {
                    "id": mbid,
                    "entity_name": entity_name or credited_name,
                    "credited_name": credited_name or entity_name,
                    "joinphrase": joinphrase,
                }
            )
    return components


def render_artist_components(components: list[dict[str, Any]], *, canonical_entity: bool) -> str | None:
    pieces: list[str] = []
    for item in components:
        if canonical_entity:
            name = item.get("entity_name") or item.get("credited_name")
        else:
            name = item.get("credited_name") or item.get("entity_name")
        name_text = normalize_display_text(name)
        joinphrase = normalize_joinphrase(item.get("joinphrase"))
        if name_text:
            pieces.append(name_text + joinphrase)
    text = "".join(pieces).strip()
    return text or None


def extract_mb_artist_credit(artist_credit: Any, canonical_entity: bool = False) -> str | None:
    """Render a MusicBrainz artist credit.

    ``canonical_entity=False`` preserves the release/recording credit as printed for
    backward compatibility.  The bot's library-visible spelling uses
    ``canonical_entity=True`` and retains the printed credit separately.
    """
    return render_artist_components(
        extract_mb_artist_components(artist_credit),
        canonical_entity=canonical_entity,
    )


def extract_acoustid_artist_components(recording: dict[str, Any]) -> list[dict[str, Any]]:
    artists = recording.get("artists") or []
    if not isinstance(artists, list):
        return []
    components: list[dict[str, Any]] = []
    for index, artist in enumerate(artists):
        if not isinstance(artist, dict) or not artist.get("name"):
            continue
        name = normalize_display_text(artist.get("name"))
        components.append(
            {
                "id": normalize_display_text(artist.get("id")) or None,
                "entity_name": name,
                "credited_name": name,
                "joinphrase": " & " if index < len(artists) - 1 else "",
            }
        )
    return components


def extract_acoustid_artist(recording: dict[str, Any]) -> str | None:
    return render_artist_components(extract_acoustid_artist_components(recording), canonical_entity=True)


def parse_lastfm_track_info(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(payload, dict) or not isinstance(payload.get("track"), dict):
        return None
    track = payload["track"]
    title = normalize_display_text(track.get("name")) or None
    artist_node = track.get("artist")
    if isinstance(artist_node, dict):
        artist = normalize_display_text(artist_node.get("name")) or None
    else:
        artist = normalize_display_text(artist_node) or None
    mbid = normalize_display_text(track.get("mbid")) or None
    tags: list[str] = []
    top_tags = track.get("toptags")
    tag_node = top_tags.get("tag", []) if isinstance(top_tags, dict) else []
    if isinstance(tag_node, dict):
        tag_node = [tag_node]
    if isinstance(tag_node, list):
        for item in tag_node:
            if isinstance(item, dict) and item.get("name"):
                tags.append(str(item["name"]))
            elif isinstance(item, str):
                tags.append(item)
    if not title and not artist:
        return None
    return {"artist": artist, "title": title, "mbid": mbid, "tags": compact_list(tags, limit=20)}


def extract_discogs_artist(node: Any) -> str | None:
    if not isinstance(node, list):
        return None
    names: list[str] = []
    for item in node:
        if isinstance(item, dict):
            name = normalize_display_text(item.get("name") or item.get("anv"))
            if name:
                names.append(name)
    return " & ".join(names) or None


def extract_discogs_search_artist(result: dict[str, Any]) -> str | None:
    # Discogs search result title is commonly "Artist - Release".
    text = normalize_display_text(result.get("title"))
    if " - " in text:
        return text.split(" - ", 1)[0].strip() or None
    return None


def collect_terms(node: Any) -> list[str]:
    terms: list[tuple[int, str]] = []
    if isinstance(node, dict):
        node = [node]
    if isinstance(node, list):
        for item in node:
            if isinstance(item, str):
                terms.append((0, item))
            elif isinstance(item, dict):
                name = item.get("name") or item.get("tag")
                raw_count = str(item.get("count") or "0")
                count = int(float(raw_count)) if raw_count.replace(".", "", 1).lstrip("-").isdigit() else 0
                if name:
                    terms.append((count, str(name)))
    return [term for _count, term in sorted(terms, key=lambda kv: kv[0], reverse=True)]



_GENERIC_ARTISTS = {
    "", "unknown artist", "various artists", "various", "artist", "track", "audio",
    "soundtrack", "original soundtrack", "ost", "dj mix",
}
_GENERIC_TITLES = {
    "", "unknown title", "untitled", "untitled track", "track", "audio", "audio 1",
    "audio 2", "bonus track", "intro", "outro", "interlude", "sample", "test",
}
_ISRC_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$")


def normalize_valid_isrc(value: Any) -> str | None:
    normalized = "".join(ch for ch in str(value or "").upper() if ch.isalnum())
    return normalized if _ISRC_RE.fullmatch(normalized) else None


def is_untrusted_prior_bot_text_identity(media: MediaFile) -> bool:
    source = normalize_display_text(media.existing_mtb_source).casefold()
    if not source:
        return False
    return source.startswith("musicbrainz_search_") or source in {
        "existing_mediataggerbot_text_tags",
        "existing_mediataggerbot_tags_text_repository",
    }


def is_generic_artist(value: str | None) -> bool:
    key = comparison_key(value)
    return key in {comparison_key(item) for item in _GENERIC_ARTISTS}


def is_generic_title(value: str | None) -> bool:
    key = comparison_key(value)
    if key in {comparison_key(item) for item in _GENERIC_TITLES}:
        return True
    return bool(re.fullmatch(r"(?:track|audio|song)\s*\d*", key))


def build_text_apply_safe_evidence(
    *,
    media: MediaFile,
    parse_source: str,
    query_artist: str | None,
    query_title: str,
    candidate_artist: str | None,
    candidate_title: str | None,
    candidate: dict[str, Any],
    artist_similarity: float,
    title_similarity: float,
    duration_difference: float | None,
) -> dict[str, Any]:
    signals: list[str] = []
    filename_artist, filename_title = parse_artist_title_from_filename(media.path.stem)

    if parse_source.startswith("existing_"):
        if filename_artist and candidate_artist and text_similarity(filename_artist, candidate_artist) >= 0.88:
            signals.append("filename_artist")
        if filename_title and candidate_title and best_title_similarity(filename_title, candidate_title) >= 0.94:
            signals.append("filename_title")
    else:
        if media.existing_artist and candidate_artist and text_similarity(media.existing_artist, candidate_artist) >= 0.90:
            signals.append("existing_tag_artist")
        if media.existing_title and candidate_title and best_title_similarity(media.existing_title, candidate_title) >= 0.95:
            signals.append("existing_tag_title")

    if media.existing_album_artist and candidate_artist and text_similarity(media.existing_album_artist, candidate_artist) >= 0.90:
        signals.append("album_artist")

    release_titles = [
        normalize_display_text(item.get("title"))
        for item in (candidate.get("releases") or [])
        if isinstance(item, dict) and normalize_display_text(item.get("title"))
    ]
    if media.existing_album and release_titles:
        if max(text_similarity(media.existing_album, item) for item in release_titles) >= 0.90:
            signals.append("album_release")

    artist_key = comparison_key(candidate_artist)
    if artist_key and len(artist_key) >= 4:
        for parent in list(media.path.parents)[:3]:
            parent_key = comparison_key(parent.name)
            if parent_key and (artist_key in parent_key or parent_key in artist_key):
                signals.append("folder_artist")
                break

    if duration_difference is not None and duration_difference <= 6.0:
        signals.append("duration_close")

    signal_set = set(signals)
    if parse_source.startswith("existing_"):
        independent_identity_corroborated = bool(
            {"filename_artist", "filename_title"} <= signal_set
            or (
                "filename_title" in signal_set
                and bool({"folder_artist", "album_artist"} & signal_set)
            )
            or (
                "album_release" in signal_set
                and bool({"folder_artist", "album_artist"} & signal_set)
            )
        )
    else:
        independent_identity_corroborated = bool(
            {"existing_tag_artist", "existing_tag_title"} <= signal_set
            or (
                "existing_tag_title" in signal_set
                and bool({"folder_artist", "album_artist"} & signal_set)
            )
            or (
                "album_release" in signal_set
                and bool({"folder_artist", "album_artist"} & signal_set)
            )
        )

    return {
        "parse_source": parse_source,
        "query_artist": query_artist,
        "query_title": query_title,
        "candidate_artist": candidate_artist,
        "candidate_title": candidate_title,
        "artist_similarity": round(float(artist_similarity), 6),
        "title_similarity": round(float(title_similarity), 6),
        "duration_difference_seconds": round(float(duration_difference), 3) if duration_difference is not None else None,
        "filename_artist": filename_artist,
        "filename_title": filename_title,
        "release_titles_sample": compact_list(release_titles, limit=5),
        "independent_signals": compact_list(signals, limit=10),
        "independent_identity_corroborated": independent_identity_corroborated,
    }

def parse_artist_title_from_filename(stem: str) -> tuple[str | None, str | None]:
    cleaned = stem.replace("_", " ").strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    separators = [" - ", " – ", " — ", " -- ", " ~ "]
    for sep in separators:
        if sep in cleaned:
            parts = [p.strip() for p in cleaned.split(sep) if p.strip()]
            if len(parts) >= 2:
                artist = clean_identity_part(parts[0])
                title = clean_identity_part(parts[1])
                return artist or None, title or None
    if "-" in cleaned:
        parts = [p.strip() for p in cleaned.split("-", 1) if p.strip()]
        if len(parts) == 2 and len(parts[0]) <= 80:
            return clean_identity_part(parts[0]) or None, clean_identity_part(parts[1]) or None
    title = clean_identity_part(cleaned)
    return None, title or None


def clean_identity_part(value: str) -> str:
    # Remove only presentation/channel noise. Material qualifiers such as remix,
    # radio edit, live, acoustic and instrumental remain part of the query identity.
    cleaned, _removed = strip_presentation_noise(value)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" -_.")


def _merge_candidate_lists(*groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for group in groups:
        for item in group:
            if not isinstance(item, dict):
                continue
            source = normalize_display_text(item.get("source")) or "unknown"
            artist = normalize_display_text(item.get("artist"))
            title = normalize_display_text(item.get("title"))
            recording_id = normalize_display_text(item.get("recording_id"))
            key = (source.casefold(), comparison_key(artist), comparison_key(title), recording_id.casefold())
            if key in seen:
                continue
            seen.add(key)
            cleaned = dict(item)
            cleaned.update(
                {
                    "source": source,
                    "artist": artist or None,
                    "title": title or None,
                    "recording_id": recording_id or None,
                }
            )
            out.append(cleaned)
            if len(out) >= 20:
                return out
    return out


def _has_mappable_genre_terms(values: list[str]) -> bool:
    return any(map_term_to_main(normalize_genre_key(str(value))) for value in values if str(value).strip())


def recording_media_kind_adjustment(media: MediaFile, recording: dict[str, Any]) -> float:
    """Small tie-breaker for MusicBrainz video recordings without overriding stronger identity evidence."""
    is_video_recording = bool(recording.get("video", False))
    if media.media_kind == "video":
        return 2.0 if is_video_recording else 0.0
    if media.media_kind == "audio" and is_video_recording:
        return -5.0
    return 0.0


def _safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except Exception:
        return None
