from __future__ import annotations

import json
import logging
import os
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any

from mutagen import File as MutagenFile
from mutagen.id3 import COMM, TALB, TCON, TDRC, TIT2, TPE1, TPE2, TSRC, TXXX, UFID, ID3, ID3NoHeaderError
from mutagen.mp4 import MP4, MP4FreeForm
from mutagen.asf import ASF

from . import __version__
from .asset_metadata import media_asset_metadata
from .config import AppConfig
from .ffprobe import classify_container, ffprobe_available, ffprobe_media_info
from .models import GenreResult, MatchResult, dataclass_to_jsonable
from .rename import windows_path_identity_key
from .scanner import read_existing_tags, read_existing_tags_raw
from .timeutil import now_utc, timestamp_for_filename
from .utils import comparison_key, normalize_display_text, run_command, which, write_json_atomic

LOG = logging.getLogger(__name__)

AUDIO_ID3_EXTS = {".mp3"}
MP4_EXTS = {".mp4", ".m4a", ".m4v"}
MOV_EXTS = {".mov"}
ASF_EXTS = {".wma", ".asf"}
VORBIS_EXTS = {".flac", ".ogg", ".oga", ".opus"}
VIDEO_EXTS = {".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".mpg", ".mpeg", ".ts", ".m2ts", ".3gp", ".flv"}


def exiftool_available() -> bool:
    return which("exiftool") is not None or which("exiftool.exe") is not None


def metadata_writer_plan(path: Path, config: AppConfig) -> dict[str, Any]:
    """Select an embedded writer from actual container evidence when needed."""
    ext = path.suffix.casefold()
    plan: dict[str, Any] = {
        "schema": "MediaTaggerBot.metadata_writer_plan.v1",
        "extension": ext,
        "writer": "none",
        "supported": False,
        "container_verified": False,
        "container_family": "unknown",
        "reason": "unsupported_extension",
        "probe": {},
    }
    if ext in AUDIO_ID3_EXTS:
        verified, family, reason = _verify_audio_container(path, "mp3")
        plan.update(
            writer="id3" if verified else "none",
            supported=verified,
            container_verified=verified,
            container_family=family,
            reason=reason,
        )
        return plan
    if ext in VORBIS_EXTS:
        expected = "flac" if ext == ".flac" else ("opus" if ext == ".opus" else "ogg")
        verified, family, reason = _verify_audio_container(path, expected)
        plan.update(
            writer="vorbis" if verified else "none",
            supported=verified,
            container_verified=verified,
            container_family=family,
            reason=reason,
        )
        return plan
    if ext in ASF_EXTS:
        verified, family, reason = _verify_audio_container(path, "asf")
        plan.update(
            writer="asf" if verified else "none",
            supported=verified,
            container_verified=verified,
            container_family=family,
            reason=reason,
        )
        return plan

    if ext in MP4_EXTS | MOV_EXTS or ext in VIDEO_EXTS:
        probe = ffprobe_media_info(path, int(config.get("processing.ffprobe_timeout_seconds", 45)))
        plan["probe"] = probe
        family = classify_container(probe)
        plan["container_family"] = family
        plan["container_verified"] = bool(probe.get("ok"))
        if not probe.get("ok"):
            plan["reason"] = "container_probe_failed"
            return plan
        if ext in MP4_EXTS:
            if family != "mp4_family":
                plan["reason"] = f"extension_container_mismatch:{family}"
                return plan
            plan.update(writer="mp4", supported=True, reason="ffprobe_verified_mp4_family")
            return plan
        if ext in MOV_EXTS:
            # Mutagen can parse some MOV-family files, but production evidence showed
            # unreliable write/readback. Use ExifTool only, otherwise fail closed.
            if family != "mp4_family":
                plan["reason"] = f"extension_container_mismatch:{family}"
                return plan
            if bool(config.get("metadata.use_exiftool_for_video_when_available", True)) and exiftool_available():
                plan.update(writer="exiftool", supported=True, reason="ffprobe_verified_mov_exiftool")
            else:
                plan["reason"] = "mov_requires_exiftool_for_verified_apply"
            return plan
        if bool(config.get("metadata.use_exiftool_for_video_when_available", True)) and exiftool_available():
            plan.update(writer="exiftool", supported=True, reason="video_container_exiftool")
            return plan
        plan["reason"] = "video_container_requires_exiftool"
        return plan

    # Raw AAC and other supported scan extensions may still receive a sidecar in
    # apply-all, but apply-safe will not rename without verified embedded metadata.
    return plan


def probe_metadata_writer_readiness(path: Path, plan: dict[str, Any]) -> tuple[bool, str]:
    """Parse through the selected writer without saving bytes."""
    writer = str(plan.get("writer") or "none")
    try:
        if writer == "id3":
            verified, _family, reason = _verify_audio_container(path, "mp3")
            return verified, "" if verified else reason
        if writer == "mp4":
            MP4(str(path))
            return True, ""
        if writer == "vorbis":
            media = MutagenFile(str(path))
            if media is None:
                return False, "Mutagen could not recognize the Vorbis-like container"
            return True, ""
        if writer == "asf":
            ASF(str(path))
            return True, ""
        if writer == "exiftool":
            return (exiftool_available(), "" if exiftool_available() else "ExifTool unavailable")
        return False, str(plan.get("reason") or "No verified embedded metadata writer")
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def write_metadata(
    path: Path,
    match: MatchResult,
    genre: GenreResult,
    config: AppConfig,
    sidecar_path: Path | None = None,
    original_path: Path | None = None,
    writer_plan: dict[str, Any] | None = None,
) -> tuple[bool, str | None, Path | None]:
    before = read_existing_tags_raw(path)
    applied_utc = now_utc().isoformat()
    asset_meta = media_asset_metadata(match, genre, path)
    comment = build_comment(match, genre, applied_utc, asset_meta)
    writer_plan = dict(writer_plan or metadata_writer_plan(path, config))
    writer = str(writer_plan.get("writer") or "none")
    wrote = False
    error: str | None = None

    def write_once() -> bool:
        if writer == "id3":
            write_id3(path, match, genre, config, comment, applied_utc, asset_meta)
            return True
        if writer == "mp4":
            write_mp4(path, match, genre, config, comment, applied_utc, asset_meta)
            return True
        if writer == "vorbis":
            write_vorbis_like(path, match, genre, config, comment, applied_utc, asset_meta)
            return True
        if writer == "asf":
            write_asf(path, match, genre, config, comment, applied_utc, asset_meta)
            return True
        if writer == "exiftool":
            write_with_exiftool(
                path,
                match,
                genre,
                comment,
                applied_utc,
                int(config.get("processing.ffprobe_timeout_seconds", 45)),
                asset_meta,
            )
            return True
        return False

    try:
        wrote = write_once()
        if not wrote:
            error = f"No verified embedded metadata writer: {writer_plan.get('reason') or path.suffix.lower()}"
    except Exception as exc:
        if (
            _is_permission_error(exc)
            and bool(config.get("processing.repair_readonly_attribute_on_apply", True))
            and _file_is_readonly(path)
        ):
            original_mode = path.stat().st_mode
            try:
                os.chmod(path, original_mode | stat.S_IWUSR)
                wrote = write_once()
                if wrote:
                    LOG.info("Temporarily cleared the read-only attribute and embedded metadata was written: %s", path)
                    error = None
            except Exception as retry_exc:
                error = str(retry_exc)
                LOG.warning("Metadata write retry after read-only repair failed for %s: %s", path, retry_exc)
            finally:
                try:
                    os.chmod(path, original_mode)
                except OSError as restore_exc:
                    LOG.warning("Could not restore original file mode after metadata write retry for %s: %s", path, restore_exc)
        else:
            error = str(exc)
            LOG.warning("Metadata write failed for %s using %s: %s", path, writer, exc)

    sidecar_written: Path | None = None
    write_sidecar = bool(config.get("processing.create_sidecar_for_every_apply", False)) or (
        bool(config.get("processing.write_sidecar_for_unsupported_metadata", True)) and not wrote
    )
    if write_sidecar and sidecar_path:
        sidecar_backup = _prepare_sidecar_destination(path, sidecar_path)
        payload = {
            "schema": "MediaTaggerBot.sidecar.v5",
            "app_version": __version__,
            "created_utc": applied_utc,
            "path": str(path),
            "original_path": str(original_path) if original_path else str(path),
            "embedded_metadata_written": wrote,
            "embedded_metadata_error": error,
            "metadata_writer_plan": writer_plan,
            "before_tags": before,
            "match": dataclass_to_jsonable(match),
            "genre": dataclass_to_jsonable(genre),
            "asset_metadata": asset_meta,
            "previous_sidecar_backup": str(sidecar_backup) if sidecar_backup else "",
        }
        write_json_atomic(sidecar_path, payload)
        sidecar_written = sidecar_path
    return wrote, error, sidecar_written


def embedded_metadata_supported(path: Path, config: AppConfig) -> bool:
    return bool(metadata_writer_plan(path, config).get("supported"))



def _is_permission_error(exc: Exception) -> bool:
    return isinstance(exc, PermissionError) or (
        isinstance(exc, OSError) and getattr(exc, "errno", None) in {13}
    )


def _file_is_readonly(path: Path) -> bool:
    try:
        return not bool(path.stat().st_mode & stat.S_IWUSR)
    except OSError:
        return False


def _verify_audio_container(path: Path, expected: str) -> tuple[bool, str, str]:
    """Verify the actual audio/container family before selecting an embedded writer."""
    media = None
    mutagen_error = ""
    try:
        media = MutagenFile(str(path), easy=False)
    except Exception as exc:
        mutagen_error = f"mutagen_probe_failed:{type(exc).__name__}:{exc}"

    class_name = type(media).__name__.casefold() if media is not None else ""
    try:
        with path.open("rb") as handle:
            header = handle.read(4096)
    except OSError as exc:
        return False, "unreadable", f"header_probe_failed:{type(exc).__name__}:{exc}"

    if expected == "mp3":
        if class_name == "mp3" or header.startswith(b"ID3") or _contains_mpeg_audio_sync(header):
            return True, "mp3", "mutagen_or_header_verified_mp3"
        return False, class_name or "unknown", mutagen_error or f"extension_container_mismatch:{class_name or 'unknown'}"
    if expected == "flac":
        if class_name == "flac" or header.startswith(b"fLaC"):
            return True, "flac", "mutagen_or_header_verified_flac"
        return False, class_name or "unknown", mutagen_error or f"extension_container_mismatch:{class_name or 'unknown'}"
    if expected == "opus":
        if class_name in {"oggopus", "opus"} or (header.startswith(b"OggS") and b"OpusHead" in header):
            return True, "opus", "mutagen_or_header_verified_opus"
        return False, class_name or "unknown", mutagen_error or f"extension_container_mismatch:{class_name or 'unknown'}"
    if expected == "ogg":
        if class_name in {"oggvorbis", "oggflac", "oggspeex", "oggtheora"} or header.startswith(b"OggS"):
            return True, "ogg", "mutagen_or_header_verified_ogg"
        return False, class_name or "unknown", mutagen_error or f"extension_container_mismatch:{class_name or 'unknown'}"
    if expected == "asf":
        asf_guid = bytes.fromhex("3026B2758E66CF11A6D900AA0062CE6C")
        if class_name == "asf" or header.startswith(asf_guid):
            return True, "asf", "mutagen_or_header_verified_asf"
        return False, class_name or "unknown", mutagen_error or f"extension_container_mismatch:{class_name or 'unknown'}"
    return False, class_name or "unknown", "unsupported_expected_container"


def _contains_mpeg_audio_sync(header: bytes) -> bool:
    for index in range(max(0, len(header) - 1)):
        if header[index] == 0xFF and (header[index + 1] & 0xE0) == 0xE0:
            return True
    return False


def sidecar_destination_readiness(media_path: Path, sidecar_path: Path) -> tuple[bool, str]:
    """Check sidecar safety without writing or backing up anything."""
    if windows_path_identity_key(media_path) == windows_path_identity_key(sidecar_path):
        return False, "sidecar_destination_aliases_media_under_windows_semantics"
    if not sidecar_path.exists():
        return True, "sidecar_destination_available"
    if not sidecar_path.is_file():
        return False, "sidecar_destination_exists_but_is_not_a_regular_file"
    if not _is_bot_owned_sidecar(sidecar_path):
        return False, "sidecar_destination_exists_and_is_not_mediataggerbot_owned"
    return True, "owned_sidecar_update_with_backup"


def _prepare_sidecar_destination(media_path: Path, sidecar_path: Path) -> Path | None:
    """Fail closed on aliases/unowned files and back up bot-owned sidecars."""
    ready, reason = sidecar_destination_readiness(media_path, sidecar_path)
    if not ready:
        if reason == "sidecar_destination_exists_and_is_not_mediataggerbot_owned":
            raise RuntimeError(
                "Refusing to replace an existing sidecar that is not recognized as MediaTaggerBot-owned: "
                f"{sidecar_path}"
            )
        raise RuntimeError(f"Refusing unsafe sidecar write: {reason}: {sidecar_path}")
    if not sidecar_path.exists():
        return None
    backup = _unique_sidecar_backup_path(sidecar_path)
    _copy_file_atomic(sidecar_path, backup)
    return backup


def _is_bot_owned_sidecar(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(payload, dict):
        return False
    schema = str(payload.get("schema") or "")
    return schema.startswith("MediaTaggerBot.sidecar.") and bool(payload.get("app_version"))


def _unique_sidecar_backup_path(path: Path) -> Path:
    stamp = timestamp_for_filename()
    candidate = path.with_name(f"{path.name}.bak_{stamp}")
    index = 2
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.bak_{stamp}_{index}")
        index += 1
    return candidate


def _copy_file_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=str(target.parent), delete=False) as handle:
            temp_path = Path(handle.name)
        shutil.copy2(source, temp_path)
        os.replace(temp_path, target)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

def write_id3(path: Path, match: MatchResult, genre: GenreResult, config: AppConfig, comment: str, applied_utc: str, asset_meta: dict[str, Any]) -> None:
    try:
        tags = ID3(str(path))
    except ID3NoHeaderError:
        tags = ID3()

    overwrite = bool(config.get("metadata.overwrite_existing_tags", True))

    def set_text(frame_id: str, frame: Any) -> None:
        if not overwrite and tags.getall(frame_id):
            return
        tags.delall(frame_id)
        tags.add(frame)

    encoding = 3
    if match.title:
        set_text("TIT2", TIT2(encoding=encoding, text=[match.title]))
    if match.artist:
        set_text("TPE1", TPE1(encoding=encoding, text=[match.artist]))
    if match.album_artist:
        set_text("TPE2", TPE2(encoding=encoding, text=[match.album_artist]))
    if match.album:
        set_text("TALB", TALB(encoding=encoding, text=[match.album]))
    if match.date:
        set_text("TDRC", TDRC(encoding=encoding, text=[match.date]))
    if genre.main_genre:
        set_text("TCON", TCON(encoding=encoding, text=[genre.main_genre]))
    if match.isrc:
        set_text("TSRC", TSRC(encoding=encoding, text=[match.isrc]))

    # Picard-compatible recording identifier plus legacy/custom mirrors for interoperability.
    if match.musicbrainz_recording_id:
        tags.delall("UFID:http://musicbrainz.org")
        tags.add(UFID(owner="http://musicbrainz.org", data=match.musicbrainz_recording_id.encode("ascii", errors="ignore")))
    set_txxx(tags, "MusicBrainz Recording Id", match.musicbrainz_recording_id)
    set_txxx(tags, "MusicBrainz Artist Id", ";".join(match.musicbrainz_artist_ids) if match.musicbrainz_artist_ids else None)
    set_txxx(tags, "MusicBrainz Album Id", match.musicbrainz_release_id)
    set_txxx(tags, "MusicBrainz Release Group Id", match.musicbrainz_release_group_id)
    set_txxx(tags, "Acoustid Id", match.acoustid_id)
    set_txxx(tags, "MediaTaggerBot Source", match.source)
    set_txxx(tags, "MediaTaggerBot Confidence", f"{match.confidence:.1f}")
    set_txxx(tags, "MediaTaggerBot Version", __version__)
    set_txxx(tags, "MediaTaggerBot Applied UTC", applied_utc)
    set_txxx(tags, "MediaTaggerBot Asset Id", asset_meta.get("asset_id") or None)
    set_txxx(tags, "MediaTaggerBot Asset Status", str(asset_meta.get("asset_status") or ""))
    set_txxx(tags, "MediaTaggerBot Asset Class", str(asset_meta.get("asset_class") or ""))
    set_txxx(tags, "MediaTaggerBot Asset Tags", ";".join(asset_meta.get("asset_tags", [])))
    set_txxx(tags, "MediaTaggerBot Asset Lineage", str(asset_meta.get("asset_lineage") or ""))
    set_txxx(tags, "MediaTaggerBot Metadata Schema", str(asset_meta.get("metadata_schema") or ""))
    set_txxx(tags, "MediaTaggerBot Source Artist Credit", match.source_artist_credit)
    set_txxx(tags, "MediaTaggerBot Canonicalization Status", match.canonicalization_status)
    set_txxx(tags, "MediaTaggerBot Canonicalization Score", f"{match.canonicalization_score:.1f}")
    set_txxx(tags, "MediaTaggerBot Identity Tier", match.identity_tier)
    set_txxx(tags, "MediaTaggerBot Ambiguity Status", match.ambiguity_status)
    set_txxx(tags, "MediaTaggerBot Candidate Margin", f"{match.candidate_margin:.1f}" if match.candidate_margin is not None else None)
    set_txxx(tags, "MediaTaggerBot Version Evidence", ";".join(match.version_evidence) if match.version_evidence else None)
    set_txxx(tags, "MediaTaggerBot Apply Blockers", ";".join(match.apply_blockers) if match.apply_blockers else None)
    set_txxx(tags, "MediaTaggerBot Repository Agreement", ";".join(match.repository_agreement) if match.repository_agreement else None)
    set_txxx(tags, "MediaTaggerBot Repository Conflicts", ";".join(match.repository_conflicts) if match.repository_conflicts else None)
    if genre.subgenre:
        set_txxx(tags, "MediaTaggerBot Subgenre", genre.subgenre)
    if bool(config.get("metadata.write_comment_with_match_evidence", True)):
        tags.delall("COMM:MediaTaggerBot:eng")
        tags.add(COMM(encoding=encoding, lang="eng", desc="MediaTaggerBot", text=[comment]))
    tags.save(str(path), v2_version=int(config.get("metadata.id3_version", 3)))


def set_txxx(tags: ID3, desc: str, value: str | None) -> None:
    tags.delall(f"TXXX:{desc}")
    if value:
        tags.add(TXXX(encoding=3, desc=desc, text=[str(value)]))


def write_mp4(path: Path, match: MatchResult, genre: GenreResult, config: AppConfig, comment: str, applied_utc: str, asset_meta: dict[str, Any]) -> None:
    media = MP4(str(path))
    if media.tags is None:
        media.add_tags()
    if media.tags is None:
        raise RuntimeError("MP4 tag container could not be initialized")
    overwrite = bool(config.get("metadata.overwrite_existing_tags", True))

    def set_tag(key: str, value: str | None) -> None:
        if not value:
            return
        if overwrite or key not in media.tags:
            media.tags[key] = [value]

    set_tag("\xa9nam", match.title)
    set_tag("\xa9ART", match.artist)
    set_tag("aART", match.album_artist)
    set_tag("\xa9alb", match.album)
    set_tag("\xa9day", match.date)
    set_tag("\xa9gen", genre.main_genre)
    set_tag("desc", comment[:255])
    set_mp4_freeform(media, "ISRC", match.isrc)
    set_mp4_freeform(media, "MusicBrainz Track Id", match.musicbrainz_recording_id)
    set_mp4_freeform(media, "MusicBrainz Artist Id", ";".join(match.musicbrainz_artist_ids) if match.musicbrainz_artist_ids else None)
    set_mp4_freeform(media, "MusicBrainz Album Id", match.musicbrainz_release_id)
    set_mp4_freeform(media, "MusicBrainz Release Group Id", match.musicbrainz_release_group_id)
    set_mp4_freeform(media, "Acoustid Id", match.acoustid_id)
    set_mp4_freeform(media, "MediaTaggerBot Source", match.source)
    set_mp4_freeform(media, "MediaTaggerBot Confidence", f"{match.confidence:.1f}")
    set_mp4_freeform(media, "MediaTaggerBot Version", __version__)
    set_mp4_freeform(media, "MediaTaggerBot Applied UTC", applied_utc)
    set_mp4_freeform(media, "MediaTaggerBot Asset Id", str(asset_meta.get("asset_id") or "") or None)
    set_mp4_freeform(media, "MediaTaggerBot Asset Status", str(asset_meta.get("asset_status") or ""))
    set_mp4_freeform(media, "MediaTaggerBot Asset Class", str(asset_meta.get("asset_class") or ""))
    set_mp4_freeform(media, "MediaTaggerBot Asset Tags", ";".join(asset_meta.get("asset_tags", [])))
    set_mp4_freeform(media, "MediaTaggerBot Asset Lineage", str(asset_meta.get("asset_lineage") or ""))
    set_mp4_freeform(media, "MediaTaggerBot Metadata Schema", str(asset_meta.get("metadata_schema") or ""))
    set_mp4_freeform(media, "MediaTaggerBot Source Artist Credit", match.source_artist_credit)
    set_mp4_freeform(media, "MediaTaggerBot Canonicalization Status", match.canonicalization_status)
    set_mp4_freeform(media, "MediaTaggerBot Canonicalization Score", f"{match.canonicalization_score:.1f}")
    set_mp4_freeform(media, "MediaTaggerBot Identity Tier", match.identity_tier)
    set_mp4_freeform(media, "MediaTaggerBot Ambiguity Status", match.ambiguity_status)
    set_mp4_freeform(media, "MediaTaggerBot Candidate Margin", f"{match.candidate_margin:.1f}" if match.candidate_margin is not None else None)
    set_mp4_freeform(media, "MediaTaggerBot Version Evidence", ";".join(match.version_evidence) if match.version_evidence else None)
    set_mp4_freeform(media, "MediaTaggerBot Apply Blockers", ";".join(match.apply_blockers) if match.apply_blockers else None)
    set_mp4_freeform(media, "MediaTaggerBot Repository Agreement", ";".join(match.repository_agreement) if match.repository_agreement else None)
    set_mp4_freeform(media, "MediaTaggerBot Repository Conflicts", ";".join(match.repository_conflicts) if match.repository_conflicts else None)
    if genre.subgenre:
        set_mp4_freeform(media, "MediaTaggerBot Subgenre", genre.subgenre)
    media.save()


def set_mp4_freeform(media: MP4, name: str, value: str | None) -> None:
    key = f"----:com.apple.iTunes:{name}"
    if value:
        media[key] = [MP4FreeForm(str(value).encode("utf-8"))]
    elif key in media:
        del media[key]


def write_vorbis_like(path: Path, match: MatchResult, genre: GenreResult, config: AppConfig, comment: str, applied_utc: str, asset_meta: dict[str, Any]) -> None:
    media = MutagenFile(str(path))
    if media is None:
        raise RuntimeError("Mutagen returned no handler")
    if media.tags is None:
        if hasattr(media, "add_tags"):
            media.add_tags()
        else:
            raise RuntimeError("Container has no tags and cannot add tags")
    tags = media.tags
    overwrite = bool(config.get("metadata.overwrite_existing_tags", True))
    _set_mapping(tags, "title", match.title, overwrite)
    _set_mapping(tags, "artist", match.artist, overwrite)
    _set_mapping(tags, "albumartist", match.album_artist, overwrite)
    _set_mapping(tags, "album", match.album, overwrite)
    _set_mapping(tags, "date", match.date, overwrite)
    _set_mapping(tags, "genre", genre.main_genre, overwrite)
    _set_mapping(tags, "isrc", match.isrc, overwrite)
    _set_mapping(tags, "musicbrainz_trackid", match.musicbrainz_recording_id, True)
    _set_mapping(tags, "musicbrainz_artistid", ";".join(match.musicbrainz_artist_ids) if match.musicbrainz_artist_ids else None, True)
    _set_mapping(tags, "musicbrainz_albumid", match.musicbrainz_release_id, True)
    _set_mapping(tags, "musicbrainz_releasegroupid", match.musicbrainz_release_group_id, True)
    _set_mapping(tags, "acoustid_id", match.acoustid_id, True)
    _set_mapping(tags, "mediataggerbot_source", match.source, True)
    _set_mapping(tags, "mediataggerbot_confidence", f"{match.confidence:.1f}", True)
    _set_mapping(tags, "mediataggerbot_version", __version__, True)
    _set_mapping(tags, "mediataggerbot_applied_utc", applied_utc, True)
    _set_mapping(tags, "mediataggerbot_asset_id", str(asset_meta.get("asset_id") or "") or None, True)
    _set_mapping(tags, "mediataggerbot_asset_status", str(asset_meta.get("asset_status") or ""), True)
    _set_mapping(tags, "mediataggerbot_asset_class", str(asset_meta.get("asset_class") or ""), True)
    _set_mapping(tags, "mediataggerbot_asset_tags", ";".join(asset_meta.get("asset_tags", [])), True)
    _set_mapping(tags, "mediataggerbot_asset_lineage", str(asset_meta.get("asset_lineage") or ""), True)
    _set_mapping(tags, "mediataggerbot_metadata_schema", str(asset_meta.get("metadata_schema") or ""), True)
    _set_mapping(tags, "mediataggerbot_source_artist_credit", match.source_artist_credit, True)
    _set_mapping(tags, "mediataggerbot_canonicalization_status", match.canonicalization_status, True)
    _set_mapping(tags, "mediataggerbot_canonicalization_score", f"{match.canonicalization_score:.1f}", True)
    _set_mapping(tags, "mediataggerbot_identity_tier", match.identity_tier, True)
    _set_mapping(tags, "mediataggerbot_ambiguity_status", match.ambiguity_status, True)
    _set_mapping(tags, "mediataggerbot_candidate_margin", f"{match.candidate_margin:.1f}" if match.candidate_margin is not None else None, True)
    _set_mapping(tags, "mediataggerbot_version_evidence", ";".join(match.version_evidence) if match.version_evidence else None, True)
    _set_mapping(tags, "mediataggerbot_apply_blockers", ";".join(match.apply_blockers) if match.apply_blockers else None, True)
    _set_mapping(tags, "mediataggerbot_repository_agreement", ";".join(match.repository_agreement) if match.repository_agreement else None, True)
    _set_mapping(tags, "mediataggerbot_repository_conflicts", ";".join(match.repository_conflicts) if match.repository_conflicts else None, True)
    _set_mapping(tags, "comment", comment, True)
    if genre.subgenre:
        _set_mapping(tags, "mediataggerbot_subgenre", genre.subgenre, True)
    media.save()



def write_asf(path: Path, match: MatchResult, genre: GenreResult, config: AppConfig, comment: str, applied_utc: str, asset_meta: dict[str, Any]) -> None:
    media = ASF(str(path))
    overwrite = bool(config.get("metadata.overwrite_existing_tags", True))
    values = {
        "Title": match.title,
        "Author": match.artist,
        "WM/AlbumTitle": match.album,
        "WM/AlbumArtist": match.album_artist,
        "WM/Year": match.date,
        "WM/Genre": genre.main_genre,
        "WM/ISRC": match.isrc,
        "MusicBrainz/Track Id": match.musicbrainz_recording_id,
        "MusicBrainz/Artist Id": ";".join(match.musicbrainz_artist_ids) if match.musicbrainz_artist_ids else None,
        "MediaTaggerBot/Source": match.source,
        "MediaTaggerBot/Confidence": f"{match.confidence:.1f}",
        "MediaTaggerBot/Version": __version__,
        "MediaTaggerBot/Applied UTC": applied_utc,
        "Description": comment,
    }
    for key, value in values.items():
        if value and (overwrite or key not in media.tags):
            media.tags[key] = [str(value)]
    media.save()

def _set_mapping(tags: Any, key: str, value: str | None, overwrite: bool) -> None:
    if value and (overwrite or key not in tags):
        tags[key] = [str(value)]
    elif not value and overwrite and key in tags:
        del tags[key]


def write_with_exiftool(path: Path, match: MatchResult, genre: GenreResult, comment: str, applied_utc: str, timeout_seconds: int, asset_meta: dict[str, Any]) -> None:
    exe = which("exiftool") or which("exiftool.exe")
    if not exe:
        raise RuntimeError("exiftool not found")
    args = [exe, "-overwrite_original"]
    tag_values = {
        "Title": match.title,
        "Artist": match.artist,
        "Album": match.album,
        "AlbumArtist": match.album_artist,
        "Genre": genre.main_genre,
        "Year": match.date,
        "Comment": comment,
        "XMP:MetadataDate": applied_utc,
        "XMP:Identifier": match.musicbrainz_recording_id or match.isrc,
        "XMP:Relation": asset_meta.get("asset_id"),
        "XMP:Label": asset_meta.get("asset_status"),
        "XMP:Subject": ";".join(asset_meta.get("asset_tags", [])),
        "XMP:HistorySoftwareAgent": f"MediaTaggerBot v{__version__}",
    }
    for key, value in tag_values.items():
        if value:
            args.append(f"-{key}={value}")
    args.append(str(path))
    code, out, err = run_command(args, timeout=timeout_seconds)
    if code != 0:
        raise RuntimeError(err.strip() or out.strip() or f"exiftool failed with code {code}")


def build_comment(match: MatchResult, genre: GenreResult, applied_utc: str, asset_meta: dict[str, Any] | None = None) -> str:
    parts = [
        f"MediaTaggerBot version={__version__}",
        f"source={match.source}",
        f"confidence={match.confidence:.1f}",
        f"genre={genre.main_genre}",
        f"canonicalization={match.canonicalization_status}",
        f"canonicalization_score={match.canonicalization_score:.1f}",
        f"identity_tier={match.identity_tier}",
        f"ambiguity={match.ambiguity_status}",
        f"applied_utc={applied_utc}",
    ]
    if genre.subgenre:
        parts.append(f"subgenre={genre.subgenre}")
    if match.source_artist_credit and match.artist and match.source_artist_credit != match.artist:
        parts.append(f"source_artist_credit={match.source_artist_credit}")
    if match.musicbrainz_recording_id:
        parts.append(f"mb_recording={match.musicbrainz_recording_id}")
    if match.musicbrainz_artist_ids:
        parts.append(f"mb_artists={','.join(match.musicbrainz_artist_ids)}")
    if match.acoustid_id:
        parts.append(f"acoustid={match.acoustid_id}")
    if match.isrc:
        parts.append(f"isrc={match.isrc}")
    if asset_meta:
        if asset_meta.get("asset_id"):
            parts.append(f"asset_id={asset_meta['asset_id']}")
        parts.append(f"asset_status={asset_meta.get('asset_status', 'current-managed')}")
        parts.append(f"metadata_schema={asset_meta.get('metadata_schema', 'asset-metadata-v1')}")
    return "; ".join(parts)


def verify_metadata_write(
    path: Path,
    match: MatchResult,
    genre: GenreResult,
    *,
    embedded_written: bool,
    sidecar_path: Path | None = None,
) -> tuple[bool, dict[str, Any]]:
    """Re-read the durable output and confirm the core identity that was intended."""
    expected = {
        "artist": match.artist or "",
        "title": match.title or "",
        "genre": genre.main_genre or "",
        "musicbrainz_recording_id": match.musicbrainz_recording_id or "",
    }
    details: dict[str, Any] = {"path": str(path), "expected": expected, "method": "none", "mismatches": []}

    if embedded_written:
        observed = read_existing_tags(path)
        details["method"] = "mutagen_reread"
        details["observed"] = {key: observed.get(key) for key in expected}
        # Some video containers written through ExifTool are not exposed by Mutagen. Use a
        # bounded ExifTool read-back when available before declaring verification failure.
        if not observed.get("artist") and not observed.get("title") and path.suffix.lower() in VIDEO_EXTS and exiftool_available():
            observed = _read_exiftool_core(path)
            details["method"] = "exiftool_reread"
            details["observed"] = observed
        for key in ("artist", "title", "genre"):
            if expected[key] and comparison_key(str(observed.get(key) or "")) != comparison_key(expected[key]):
                details["mismatches"].append(key)
        expected_mbid = expected["musicbrainz_recording_id"]
        observed_mbid = normalize_display_text(str(observed.get("musicbrainz_recording_id") or ""))
        if expected_mbid and not observed_mbid:
            details["mismatches"].append("musicbrainz_recording_id_missing")
            details["identifier_verification_status"] = (
                "partial_missing_exiftool_identifier"
                if details["method"] == "exiftool_reread"
                else "missing_expected_identifier"
            )
        elif expected_mbid and observed_mbid.casefold() != expected_mbid.casefold():
            details["mismatches"].append("musicbrainz_recording_id")
        verified = not details["mismatches"]
        details["verified"] = verified
        return verified, details

    if sidecar_path and sidecar_path.exists():
        details["method"] = "sidecar_reread"
        try:
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
            side_match = payload.get("match", {}) if isinstance(payload, dict) else {}
            side_genre = payload.get("genre", {}) if isinstance(payload, dict) else {}
            observed = {
                "artist": side_match.get("artist"),
                "title": side_match.get("title"),
                "genre": side_genre.get("main_genre"),
                "musicbrainz_recording_id": side_match.get("musicbrainz_recording_id"),
            }
            details["observed"] = observed
            for key in ("artist", "title", "genre"):
                if expected[key] and comparison_key(str(observed.get(key) or "")) != comparison_key(expected[key]):
                    details["mismatches"].append(key)
            if expected["musicbrainz_recording_id"] and normalize_display_text(str(observed.get("musicbrainz_recording_id") or "")).casefold() != expected["musicbrainz_recording_id"].casefold():
                details["mismatches"].append("musicbrainz_recording_id")
        except Exception as exc:
            details["mismatches"].append("sidecar_unreadable")
            details["error"] = str(exc)
        verified = not details["mismatches"]
        details["verified"] = verified
        return verified, details

    details["mismatches"].append("no_durable_metadata_output")
    details["verified"] = False
    return False, details


def _read_exiftool_core(path: Path) -> dict[str, Any]:
    exe = which("exiftool") or which("exiftool.exe")
    if not exe:
        return {}
    code, out, _err = run_command(
        [exe, "-j", "-Title", "-Artist", "-Genre", "-XMP:Identifier", str(path)],
        timeout=45,
    )
    if code != 0:
        return {}
    try:
        node = json.loads(out)
        item = node[0] if isinstance(node, list) and node and isinstance(node[0], dict) else {}
        return {
            "title": item.get("Title"),
            "artist": item.get("Artist"),
            "genre": item.get("Genre"),
            "musicbrainz_recording_id": item.get("Identifier"),
        }
    except Exception:
        return {}
