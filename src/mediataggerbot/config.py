from __future__ import annotations

import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .pathing import attempt_repair_invalid_media_root, clean_user_path, looks_absolute_path, resolve_user_path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore


AUDIO_EXTENSIONS_DEFAULT = [
    ".mp3", ".m4a", ".aac", ".flac", ".wav", ".aiff", ".aif", ".ogg", ".oga",
    ".opus", ".wma", ".alac", ".ape", ".mpc", ".mka"
]
VIDEO_EXTENSIONS_DEFAULT = [
    ".mp4", ".m4v", ".mov", ".mkv", ".webm", ".avi", ".wmv", ".mpg", ".mpeg",
    ".ts", ".m2ts", ".3gp", ".flv"
]
CONFIG_SCHEMA_VERSION = 1

MAIN_GENRES = [
    "Pop",
    "Rock",
    "Hip-Hop/Rap",
    "Electronic Dance Music (EDM)",
    "Country",
    "R&B/Soul",
    "Jazz",
    "Classical",
]

DEFAULT_CONFIG: dict[str, Any] = {
    "project": {
        "name": "MediaTaggerBot",
        "timezone": "America/Chicago",
        "contact": "local-user@example.invalid",
        "config_schema_version": CONFIG_SCHEMA_VERSION,
    },
    "paths": {
        "media_root": "",
        "logs_dir": "logs",
        "exports_dir": "exports",
        "state_dir": "state",
        "diagnostics_dir": "diagnostics",
        "temp_dir": "temp",
    },
    "processing": {
        "default_mode": "dry-run",
        "include_audio": True,
        "include_video": True,
        "supported_audio_extensions": AUDIO_EXTENSIONS_DEFAULT,
        "supported_video_extensions": VIDEO_EXTENSIONS_DEFAULT,
        "exclude_dir_names": [],
        "max_files_per_run": 0,
        "same_folder_output": True,
        "recursive": True,
        "require_recursive_scan": True,
        "require_complete_scan_before_apply": True,
        "follow_directory_symlinks": False,
        "write_metadata": True,
        "rename_files": True,
        "write_sidecar_for_unsupported_metadata": True,
        "create_sidecar_for_every_apply": False,
        "min_auto_confidence_apply_safe": 90.0,
        "min_auto_confidence_apply_all": 55.0,
        "allow_filename_only_matches_in_apply_all": True,
        "preserve_extension_case": False,
        "collision_suffix_style": "space_parentheses_number",
        "skip_if_filename_already_matches": True,
        "prefer_existing_identifier_shortcuts": True,
        "cache_media_inventory": True,
        "inventory_cache_ttl_days": 3650,
        "cache_fingerprints": True,
        "write_scan_coverage_reports": True,
        "write_exception_only_report": True,
        "report_duplicate_recording_candidates": True,
        "scan_progress_every_directories": 100,
        "scan_progress_every_files": 250,
        "fingerprint_timeout_seconds": 120,
        "ffprobe_timeout_seconds": 45,
        "api_connect_timeout_seconds": 10,
        "network_timeout_seconds": 30,
        "max_retries": 3,
        "retry_backoff_seconds": 2.0,
        "retry_jitter_seconds": 0.6,
        "single_instance_stale_after_seconds": 86400,
        "single_instance_heartbeat_seconds": 30,
        "operation_journal_enabled": True,
        "reconcile_operation_journal_on_apply": True,
        "verify_source_unchanged_before_apply": True,
        "verify_metadata_after_write": True,
        "require_verified_metadata_before_rename_apply_safe": True,
        "require_embedded_metadata_for_supported_formats_apply_safe": True,
        "repair_readonly_attribute_on_apply": True,
        "block_fallback_genre_in_apply_safe": True,
        "write_api_metrics": True,
        "api_cache_auto_recover": True,
        "rollback_require_paths_under_media_root": True,
        "log_max_bytes": 10_000_000,
        "log_backup_count": 3,
        "diagnostic_max_total_bytes": 10_000_000,
        "progress_log_every_files": 25,
    },
    "matching": {
        "identity_memory_enabled": True,
        "musicbrainz_artist_genre_fallback": True,
        "text_search_candidate_limit": 8,
        "min_text_candidate_margin_apply_safe": 10.0,
        "text_apply_safe_require_independent_corroboration": True,
        "text_apply_safe_min_artist_similarity": 0.90,
        "text_apply_safe_min_title_similarity": 0.95,
        "text_apply_safe_max_duration_difference_seconds": 6.0,
        "block_ambiguous_text_matches_in_apply_safe": True,
        "version_match_bonus": 5.0,
        "version_mismatch_penalty": 14.0,
        "report_acoustic_duplicate_clusters": True,
    },
    "naming": {
        "pattern": "{artist} - {title} - {genre} - {subgenre}",
        "omit_subgenre_when_unknown": True,
        "unknown_subgenre_label": "General",
        "unknown_artist_label": "Unknown Artist",
        "unknown_title_label": "Unknown Title",
        "max_filename_length": 180,
        "max_full_path_length": 240,
        "replace_slash_with": "-",
        "replace_ampersand_with": "&",
        "collapse_whitespace": True,
    },
    "genres": {
        "main_genres": MAIN_GENRES,
        "fallback_main_genre": "Pop",
        "filename_main_genre_overrides": {
            "Hip-Hop/Rap": "Hip-Hop-Rap",
            "R&B/Soul": "R&B-Soul",
        },
        "prefer_musicbrainz_genres": True,
        "prefer_lastfm_for_subgenre": True,
        "subgenre_max_words": 4,
    },
    "canonicalization": {
        "enabled": True,
        "artist_name_policy": "musicbrainz_entity",
        "unicode_form": "NFC",
        "crosscheck_lastfm": True,
        "crosscheck_discogs": True,
        "use_lastfm_corrections_for_local_fallback": True,
        "block_text_match_conflicts_in_apply_safe": True,
        "repository_agreement_bonus": 3.0,
        "repository_conflict_penalty": 12.0,
        "title_agreement_similarity": 0.94,
        "artist_agreement_similarity": 0.88,
        "conflict_similarity": 0.68,
        "overrides_file": "config/canonical_overrides.toml",
        "write_consistency_reports": True,
    },
    "apis": {
        "user_agent": "MediaTaggerBot/0.5.7 (local personal media tagger; set contact in config)",
        "acoustid_client_key": "",
        "lastfm_api_key": "",
        "discogs_user_token": "",
        "enable_acoustid": True,
        "enable_musicbrainz": True,
        "enable_lastfm": True,
        "enable_discogs": False,
        "cache_ttl_days": 365,
        "musicbrainz_min_interval_seconds": 1.05,
        "acoustid_min_interval_seconds": 0.40,
        "lastfm_min_interval_seconds": 0.25,
        "discogs_min_interval_seconds": 1.10,
    },
    "metadata": {
        "id3_version": 3,
        "use_exiftool_for_video_when_available": True,
        "write_comment_with_match_evidence": True,
        "overwrite_existing_tags": True,
        "sidecar_extension": ".metadata.json",
    },
    "reports": {
        "write_csv": True,
        "write_jsonl": True,
        "write_html_summary": True,
        "redact_api_keys": True,
    },
}

ALLOWED_KEYS: dict[str, set[str]] = {
    section: set(values.keys()) for section, values in DEFAULT_CONFIG.items()
}


@dataclass(slots=True)
class AppConfig:
    project_root: Path
    config_path: Path
    data: dict[str, Any]
    unknown_keys: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)
    load_status: dict[str, Any] = field(default_factory=lambda: {"status": "loaded"})
    safe_runtime_dirs: bool = False

    def get(self, dotted_key: str, default: Any = None) -> Any:
        node: Any = self.data
        for part in dotted_key.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    def section(self, name: str) -> dict[str, Any]:
        value = self.data.get(name, {})
        return value if isinstance(value, dict) else {}

    @property
    def media_root(self) -> Path | None:
        return resolve_user_path(self.project_root, self.get("paths.media_root", ""), project_relative=True)

    def _runtime_dir(self, key: str, default: str) -> Path:
        # Semantic/config parse failures must not redirect logs, state, or
        # diagnostics outside the portable project before preflight can block.
        if self.safe_runtime_dirs:
            return self.project_root / default
        return resolve_project_relative(self.project_root, self.get(f"paths.{key}", default))

    @property
    def logs_dir(self) -> Path:
        return self._runtime_dir("logs_dir", "logs")

    @property
    def exports_dir(self) -> Path:
        return self._runtime_dir("exports_dir", "exports")

    @property
    def state_dir(self) -> Path:
        return self._runtime_dir("state_dir", "state")

    @property
    def diagnostics_dir(self) -> Path:
        return self._runtime_dir("diagnostics_dir", "diagnostics")

    @property
    def temp_dir(self) -> Path:
        return self._runtime_dir("temp_dir", "temp")


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in base.items():
        if isinstance(value, dict):
            result[key] = deep_merge(value, overlay.get(key, {}) if isinstance(overlay.get(key), dict) else {})
        else:
            result[key] = overlay[key] if key in overlay else value
    for key, value in overlay.items():
        if key not in result:
            result[key] = value
    return result


def find_project_root(start: Path | None = None) -> Path:
    start = (start or Path(__file__)).resolve()
    candidates = [start] + list(start.parents)
    for candidate in candidates:
        if (candidate / "Start_MediaTaggerBot.bat").exists() or (candidate / "config" / "config.example.toml").exists():
            return candidate
    return Path.cwd().resolve()


def resolve_project_relative(project_root: Path, raw: str | Path) -> Path:
    path = resolve_user_path(project_root, raw, project_relative=True)
    return path if path is not None else project_root


def load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("rb") as f:
        return tomllib.load(f)


def ensure_runtime_dirs(config: AppConfig) -> None:
    for directory in [config.logs_dir, config.exports_dir, config.state_dir, config.diagnostics_dir, config.temp_dir]:
        directory.mkdir(parents=True, exist_ok=True)


def validate_unknown_keys(data: dict[str, Any]) -> list[str]:
    unknown: list[str] = []
    for section, values in data.items():
        if section not in ALLOWED_KEYS:
            unknown.append(section)
            continue
        if isinstance(values, dict):
            for key in values.keys():
                if key not in ALLOWED_KEYS[section]:
                    unknown.append(f"{section}.{key}")
    return unknown


def validate_config_types(raw: dict[str, Any]) -> list[str]:
    """Validate TOML value types against the shipped schema without coercion.

    Python truthiness is unsafe for correctness settings: ``bool("false")`` is
    true.  This validator therefore rejects string/number substitutes for booleans
    and prevents a mistyped mutating option from silently reverting to a default.
    """
    errors: list[str] = []
    if not isinstance(raw, dict):
        return ["The TOML document root must be a table."]

    def describe(expected: Any) -> str:
        if type(expected) is bool:
            return "boolean"
        if type(expected) is int:
            return "integer"
        if type(expected) is float:
            return "number"
        if isinstance(expected, str):
            return "string"
        if isinstance(expected, list):
            return "array of strings"
        if isinstance(expected, dict):
            return "table of string keys and string values"
        return type(expected).__name__

    def valid_type(value: Any, expected: Any) -> bool:
        if type(expected) is bool:
            return type(value) is bool
        if type(expected) is int:
            return type(value) is int
        if type(expected) is float:
            return type(value) in {int, float}
        if isinstance(expected, str):
            return isinstance(value, str)
        if isinstance(expected, list):
            return isinstance(value, list) and all(isinstance(item, str) for item in value)
        if isinstance(expected, dict):
            return (
                isinstance(value, dict)
                and all(isinstance(key, str) for key in value)
                and all(isinstance(item, str) for item in value.values())
            )
        return isinstance(value, type(expected))

    for section, values in raw.items():
        expected_section = DEFAULT_CONFIG.get(section)
        if expected_section is None:
            continue
        if not isinstance(values, dict):
            errors.append(f"{section} must be a TOML table, not {type(values).__name__}.")
            continue
        for key, value in values.items():
            if key not in expected_section:
                continue
            expected = expected_section[key]
            if not valid_type(value, expected):
                errors.append(
                    f"{section}.{key} must be a {describe(expected)}; got {type(value).__name__}."
                )
    return errors


def _dedupe_messages(messages: list[str]) -> list[str]:
    return list(dict.fromkeys(message for message in messages if message))



def validate_config_values(data: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    processing = data.get("processing", {}) if isinstance(data.get("processing"), dict) else {}
    if bool(processing.get("require_recursive_scan", True)) and not bool(processing.get("recursive", True)):
        warnings.append("processing.require_recursive_scan=true conflicts with processing.recursive=false; processing modes will stop before scanning.")
    try:
        if type(processing.get("max_files_per_run", 0)) is int and int(processing.get("max_files_per_run", 0)) < 0:
            warnings.append("processing.max_files_per_run is negative; use 0 for unlimited.")
    except (TypeError, ValueError):
        pass
    if not bool(processing.get("same_folder_output", True)):
        warnings.append("processing.same_folder_output=false is unsupported by this build; processing modes will stop rather than silently ignore it.")
    if str(processing.get("collision_suffix_style", "space_parentheses_number")) != "space_parentheses_number":
        warnings.append("processing.collision_suffix_style supports only space_parentheses_number in this build.")
    configured_genres = data.get("genres", {}).get("main_genres", []) if isinstance(data.get("genres"), dict) else []
    if list(configured_genres) != MAIN_GENRES:
        warnings.append("genres.main_genres differs from the required eight-bucket taxonomy; built-in mapping remains fixed to the requested buckets.")
    reports = data.get("reports", {}) if isinstance(data.get("reports"), dict) else {}
    if not bool(reports.get("redact_api_keys", True)):
        warnings.append("reports.redact_api_keys=false is ignored for safety; secrets remain redacted in diagnostics/log evidence.")
    naming = data.get("naming", {}) if isinstance(data.get("naming"), dict) else {}
    pattern = str(naming.get("pattern", ""))
    allowed_fields = {"artist", "title", "genre", "subgenre"}
    import string
    try:
        fields = {field_name for _literal, field_name, _format_spec, _conversion in string.Formatter().parse(pattern) if field_name}
        unknown_fields = sorted(fields - allowed_fields)
        if unknown_fields:
            warnings.append("naming.pattern contains unsupported field(s): " + ", ".join(unknown_fields))
        if not {"artist", "title", "genre"}.issubset(fields):
            warnings.append("naming.pattern should include {artist}, {title}, and {genre} for the requested naming standard.")
    except ValueError as exc:
        warnings.append(f"naming.pattern is malformed: {exc}")
    canonicalization = data.get("canonicalization", {}) if isinstance(data.get("canonicalization"), dict) else {}
    artist_policy = str(canonicalization.get("artist_name_policy", "musicbrainz_entity"))
    if artist_policy not in {"musicbrainz_entity", "source_credit"}:
        warnings.append("canonicalization.artist_name_policy must be musicbrainz_entity or source_credit.")
    unicode_form = str(canonicalization.get("unicode_form", "NFC")).upper()
    if unicode_form not in {"NFC", "NFD", "NFKC", "NFKD"}:
        warnings.append("canonicalization.unicode_form must be NFC, NFD, NFKC, or NFKD.")
    for key in ("title_agreement_similarity", "artist_agreement_similarity", "conflict_similarity"):
        try:
            value = float(canonicalization.get(key, 0.0))
            if not 0.0 <= value <= 1.0:
                warnings.append(f"canonicalization.{key} must be between 0 and 1.")
        except (TypeError, ValueError):
            warnings.append(f"canonicalization.{key} is not numeric.")
    for key in ("fingerprint_timeout_seconds", "ffprobe_timeout_seconds", "api_connect_timeout_seconds", "network_timeout_seconds", "single_instance_heartbeat_seconds"):
        try:
            if float(processing.get(key, 1)) <= 0:
                warnings.append(f"processing.{key} must be greater than zero.")
        except (TypeError, ValueError):
            warnings.append(f"processing.{key} is not numeric.")

    project = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
    apis = data.get("apis", {}) if isinstance(data.get("apis"), dict) else {}
    contact = str(project.get("contact", "") or "").strip().casefold()
    user_agent = str(apis.get("user_agent", "") or "")
    if bool(apis.get("enable_musicbrainz", True)) and (not contact or contact.endswith("@example.invalid")) and "set contact in config" in user_agent:
        warnings.append("project.contact is still a placeholder; set a real email or project URL for MusicBrainz User-Agent etiquette.")

    matching = data.get("matching", {}) if isinstance(data.get("matching"), dict) else {}
    try:
        margin = float(matching.get("min_text_candidate_margin_apply_safe", 10.0))
        if margin < 0:
            warnings.append("matching.min_text_candidate_margin_apply_safe must be zero or greater.")
    except (TypeError, ValueError):
        warnings.append("matching.min_text_candidate_margin_apply_safe is not numeric.")
    try:
        candidate_limit = int(matching.get("text_search_candidate_limit", 8))
        if candidate_limit < 2 or candidate_limit > 25:
            warnings.append("matching.text_search_candidate_limit should be between 2 and 25.")
    except (TypeError, ValueError):
        warnings.append("matching.text_search_candidate_limit is not an integer.")

    try:
        schema_version = int(project.get("config_schema_version", 1))
        if schema_version != 1:
            warnings.append("project.config_schema_version is unsupported; this release expects schema version 1.")
    except (TypeError, ValueError):
        warnings.append("project.config_schema_version is not an integer.")

    bounded_integer_rules = {
        "scan_progress_every_directories": (1, 100000),
        "scan_progress_every_files": (1, 100000),
        "log_max_bytes": (100000, 1000000000),
        "log_backup_count": (1, 100),
        "diagnostic_max_total_bytes": (1000000, 100000000),
        "inventory_cache_ttl_days": (1, 36500),
    }
    for key, (minimum, maximum) in bounded_integer_rules.items():
        try:
            value = int(processing.get(key, minimum))
            if value < minimum or value > maximum:
                warnings.append(f"processing.{key} must be between {minimum} and {maximum}.")
        except (TypeError, ValueError):
            warnings.append(f"processing.{key} is not an integer.")
    return warnings


def validate_config_errors(data: dict[str, Any], unknown_keys: list[str] | None = None) -> list[str]:
    """Return semantic errors that must block processing before media is touched."""
    errors: list[str] = []
    unknown_keys = list(unknown_keys or [])
    if unknown_keys:
        errors.append(
            "Unknown config key(s) are not accepted because they may be misspellings or unsupported settings: "
            + ", ".join(unknown_keys)
        )

    project = data.get("project", {}) if isinstance(data.get("project"), dict) else {}
    paths = data.get("paths", {}) if isinstance(data.get("paths"), dict) else {}
    processing = data.get("processing", {}) if isinstance(data.get("processing"), dict) else {}
    naming = data.get("naming", {}) if isinstance(data.get("naming"), dict) else {}
    genres = data.get("genres", {}) if isinstance(data.get("genres"), dict) else {}
    canonicalization = data.get("canonicalization", {}) if isinstance(data.get("canonicalization"), dict) else {}
    matching = data.get("matching", {}) if isinstance(data.get("matching"), dict) else {}
    metadata = data.get("metadata", {}) if isinstance(data.get("metadata"), dict) else {}

    # Runtime-owned folders are intentionally portable and project-local.  An
    # invalid absolute/stale path is blocked before logging/state/diagnostics can
    # be redirected to an unintended location.
    runtime_keys = ("logs_dir", "exports_dir", "state_dir", "diagnostics_dir", "temp_dir")
    normalized_runtime_paths: dict[str, str] = {}
    for key in runtime_keys:
        raw = clean_user_path(paths.get(key, ""))
        if not raw:
            errors.append(f"paths.{key} must be a non-empty project-relative folder.")
            continue
        drive_relative = len(raw) >= 2 and raw[0].isalpha() and raw[1] == ":"
        if looks_absolute_path(raw) or drive_relative:
            errors.append(f"paths.{key} must remain project-relative for portability and safe diagnostics.")
            continue
        parts = Path(raw.replace("\\", "/")).parts
        if ".." in parts:
            errors.append(f"paths.{key} must not escape the project root with '..'.")
            continue
        normalized = "/".join(part for part in parts if part not in {".", ""}).casefold()
        if not normalized:
            errors.append(f"paths.{key} must resolve to a subfolder, not the project root.")
            continue
        normalized_runtime_paths[key] = normalized

    duplicate_runtime_paths: dict[str, list[str]] = {}
    for key, normalized in normalized_runtime_paths.items():
        duplicate_runtime_paths.setdefault(normalized, []).append(key)
    for keys in duplicate_runtime_paths.values():
        if len(keys) > 1:
            errors.append(
                "Runtime output folders must be distinct; duplicate mapping: "
                + ", ".join(f"paths.{key}" for key in sorted(keys))
            )

    try:
        schema_version = int(project.get("config_schema_version", CONFIG_SCHEMA_VERSION))
    except (TypeError, ValueError):
        errors.append("project.config_schema_version must be an integer.")
    else:
        if schema_version != CONFIG_SCHEMA_VERSION:
            errors.append(
                f"project.config_schema_version={schema_version} is unsupported; this release requires {CONFIG_SCHEMA_VERSION}."
            )

    default_mode = str(processing.get("default_mode", "dry-run"))
    if default_mode not in {"scan-only", "dry-run", "apply-safe", "apply-all"}:
        errors.append("processing.default_mode must be scan-only, dry-run, apply-safe, or apply-all.")
    if bool(processing.get("require_recursive_scan", True)) and not bool(processing.get("recursive", True)):
        errors.append("processing.require_recursive_scan=true conflicts with processing.recursive=false.")
    if not bool(processing.get("same_folder_output", True)):
        errors.append("processing.same_folder_output=false is unsupported by this release.")
    if str(processing.get("collision_suffix_style", "space_parentheses_number")) != "space_parentheses_number":
        errors.append("processing.collision_suffix_style supports only space_parentheses_number.")

    def bounded_number(section: dict[str, Any], key: str, minimum: float, maximum: float) -> None:
        try:
            value = float(section.get(key))
        except (TypeError, ValueError):
            errors.append(f"processing.{key} must be numeric.")
            return
        if value < minimum or value > maximum:
            errors.append(f"processing.{key} must be between {minimum:g} and {maximum:g}.")

    for key in ("fingerprint_timeout_seconds", "ffprobe_timeout_seconds", "api_connect_timeout_seconds", "network_timeout_seconds"):
        bounded_number(processing, key, 1, 3600)
    bounded_number(processing, "retry_backoff_seconds", 0, 300)
    bounded_number(processing, "retry_jitter_seconds", 0, 60)
    bounded_number(processing, "min_auto_confidence_apply_safe", 0, 100)
    bounded_number(processing, "min_auto_confidence_apply_all", 0, 100)

    integer_bounds = {
        "max_files_per_run": (0, 10_000_000),
        "max_retries": (0, 10),
        "single_instance_stale_after_seconds": (60, 31_536_000),
        "single_instance_heartbeat_seconds": (5, 3600),
        "scan_progress_every_directories": (1, 100_000),
        "scan_progress_every_files": (1, 100_000),
        "progress_log_every_files": (1, 100_000),
        "log_max_bytes": (100_000, 1_000_000_000),
        "log_backup_count": (1, 100),
        "diagnostic_max_total_bytes": (1_000_000, 100_000_000),
    }
    for key, (minimum, maximum) in integer_bounds.items():
        try:
            value = int(processing.get(key))
        except (TypeError, ValueError):
            errors.append(f"processing.{key} must be an integer.")
            continue
        if value < minimum or value > maximum:
            errors.append(f"processing.{key} must be between {minimum} and {maximum}.")

    try:
        safe_threshold = float(processing.get("min_auto_confidence_apply_safe", 90.0))
        all_threshold = float(processing.get("min_auto_confidence_apply_all", 55.0))
        if safe_threshold < all_threshold:
            errors.append("processing.min_auto_confidence_apply_safe must be greater than or equal to apply-all threshold.")
    except (TypeError, ValueError):
        pass

    import string
    pattern = str(naming.get("pattern", ""))
    try:
        fields = {field_name for _literal, field_name, _format_spec, _conversion in string.Formatter().parse(pattern) if field_name}
    except ValueError as exc:
        errors.append(f"naming.pattern is malformed: {exc}")
    else:
        unknown_fields = sorted(fields - {"artist", "title", "genre", "subgenre"})
        if unknown_fields:
            errors.append("naming.pattern contains unsupported field(s): " + ", ".join(unknown_fields))
        missing = sorted({"artist", "title", "genre"} - fields)
        if missing:
            errors.append("naming.pattern is missing required field(s): " + ", ".join(missing))
    try:
        max_filename = int(naming.get("max_filename_length", 180))
        if max_filename < 40 or max_filename > 240:
            errors.append("naming.max_filename_length must be between 40 and 240.")
    except (TypeError, ValueError):
        errors.append("naming.max_filename_length must be an integer.")
    try:
        max_full_path = int(naming.get("max_full_path_length", 240))
        if max_full_path != 0 and (max_full_path < 120 or max_full_path > 32760):
            errors.append("naming.max_full_path_length must be 0 (disabled) or between 120 and 32760.")
    except (TypeError, ValueError):
        errors.append("naming.max_full_path_length must be an integer.")

    if list(genres.get("main_genres", [])) != MAIN_GENRES:
        errors.append("genres.main_genres must exactly match the required eight-bucket taxonomy.")

    if str(canonicalization.get("artist_name_policy", "musicbrainz_entity")) not in {"musicbrainz_entity", "source_credit"}:
        errors.append("canonicalization.artist_name_policy must be musicbrainz_entity or source_credit.")
    if str(canonicalization.get("unicode_form", "NFC")).upper() not in {"NFC", "NFD", "NFKC", "NFKD"}:
        errors.append("canonicalization.unicode_form must be NFC, NFD, NFKC, or NFKD.")
    for key in ("title_agreement_similarity", "artist_agreement_similarity", "conflict_similarity"):
        try:
            value = float(canonicalization.get(key))
        except (TypeError, ValueError):
            errors.append(f"canonicalization.{key} must be numeric.")
            continue
        if not 0.0 <= value <= 1.0:
            errors.append(f"canonicalization.{key} must be between 0 and 1.")

    try:
        candidate_limit = int(matching.get("text_search_candidate_limit", 8))
        if candidate_limit < 2 or candidate_limit > 25:
            errors.append("matching.text_search_candidate_limit must be between 2 and 25.")
    except (TypeError, ValueError):
        errors.append("matching.text_search_candidate_limit must be an integer.")
    try:
        if float(matching.get("min_text_candidate_margin_apply_safe", 10.0)) < 0:
            errors.append("matching.min_text_candidate_margin_apply_safe must be zero or greater.")
    except (TypeError, ValueError):
        errors.append("matching.min_text_candidate_margin_apply_safe must be numeric.")
    for key in ("text_apply_safe_min_artist_similarity", "text_apply_safe_min_title_similarity"):
        try:
            value = float(matching.get(key))
        except (TypeError, ValueError):
            errors.append(f"matching.{key} must be numeric.")
            continue
        if not 0.0 <= value <= 1.0:
            errors.append(f"matching.{key} must be between 0 and 1.")
    try:
        duration_limit = float(matching.get("text_apply_safe_max_duration_difference_seconds"))
        if duration_limit < 0 or duration_limit > 3600:
            errors.append("matching.text_apply_safe_max_duration_difference_seconds must be between 0 and 3600.")
    except (TypeError, ValueError):
        errors.append("matching.text_apply_safe_max_duration_difference_seconds must be numeric.")

    try:
        id3_version = int(metadata.get("id3_version", 3))
        if id3_version not in {3, 4}:
            errors.append("metadata.id3_version must be 3 or 4.")
    except (TypeError, ValueError):
        errors.append("metadata.id3_version must be an integer.")
    sidecar_extension = str(metadata.get("sidecar_extension", ".metadata.json"))
    if not sidecar_extension.startswith(".") or "/" in sidecar_extension or "\\" in sidecar_extension:
        errors.append("metadata.sidecar_extension must be a filename suffix beginning with '.' and contain no path separators.")

    return errors


def load_config(project_root: Path | None = None, config_path: Path | None = None) -> AppConfig:
    root = (project_root or find_project_root()).resolve()
    path = config_path or (root / "config" / "config.toml")
    path = path if path.is_absolute() else root / path
    path = path.resolve()
    config_exists = path.exists()
    raw = load_toml(path)
    merged = deep_merge(DEFAULT_CONFIG, raw)
    merged = apply_environment_overrides(merged)
    unknown = validate_unknown_keys(raw)
    warnings: list[str] = []
    if unknown:
        warnings.append("Unknown config keys present: " + ", ".join(unknown))
    if not config_exists:
        warnings.append(
            "Config file is missing; safe in-memory defaults are active. "
            "Diagnostics/request-stop may continue, but processing remains blocked until config is created."
        )
    warnings.extend(validate_config_values(merged))
    validation_errors = _dedupe_messages(
        ([] if config_exists else [f"Config file is missing: {path}"])
        + validate_config_types(raw)
        + validate_config_errors(merged, unknown)
    )
    cfg = AppConfig(
        project_root=root,
        config_path=path,
        data=merged,
        unknown_keys=unknown,
        warnings=_dedupe_messages(warnings),
        validation_errors=validation_errors,
        load_status={
            "status": "loaded" if config_exists else "defaults_missing_config_read_only",
            "config_path": str(path),
            "config_exists": config_exists,
            "config_schema_version": merged.get("project", {}).get("config_schema_version"),
            "semantic_validation": "pass" if not validation_errors else "failed",
            "safe_runtime_dirs_active": bool(validation_errors),
        },
        safe_runtime_dirs=bool(validation_errors),
    )
    ensure_runtime_dirs(cfg)
    return cfg



def build_fallback_config(
    project_root: Path,
    config_path: Path,
    error: Exception,
    *,
    recovery: dict[str, Any] | None = None,
) -> AppConfig:
    """Create an in-memory safe config so diagnostics can survive malformed TOML."""
    merged = deep_merge(DEFAULT_CONFIG, {})
    merged = apply_environment_overrides(merged)
    error_text = str(error)
    warnings = [
        "CONFIG INVALID: runtime is using safe in-memory defaults for diagnostics only.",
        f"Config parse error: {error_text}",
        "Use BAT option 8 Set media root or option 9 Repair/check; processing modes fail closed until config is valid.",
    ]
    cfg = AppConfig(
        project_root=project_root,
        config_path=config_path,
        data=merged,
        unknown_keys=[],
        warnings=warnings,
        validation_errors=[f"Config could not be parsed: {error_text}"],
        safe_runtime_dirs=True,
        load_status={
            "status": "fallback_invalid_config",
            "config_path": str(config_path),
            "error_type": type(error).__name__,
            "error": error_text,
            "recovery": recovery or {},
        },
    )
    ensure_runtime_dirs(cfg)
    return cfg


def load_config_resilient(
    project_root: Path | None = None,
    config_path: Path | None = None,
    *,
    mode: str = "dry-run",
) -> AppConfig:
    """Load config with one narrow, backed-up Windows-path syntax repair.

    Diagnostics remain read-only: they use an in-memory fallback if TOML is malformed.
    Other modes may repair only the common unescaped ``paths.media_root`` line, then
    validate the entire document before replacing it.  Unrelated syntax errors are never
    guessed at.
    """
    root = project_root or find_project_root()
    path = config_path or (root / "config" / "config.toml")
    try:
        return load_config(project_root=root, config_path=path)
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError, TypeError, ValueError) as exc:
        recovery: dict[str, Any] = {}
        if mode not in {"diagnostics", "request-stop"}:
            try:
                recovery = attempt_repair_invalid_media_root(path, backup=True)
            except Exception as repair_exc:
                recovery = {
                    "attempted": True,
                    "repaired": False,
                    "error": f"repair_exception: {type(repair_exc).__name__}: {repair_exc}",
                }
            if recovery.get("repaired"):
                repaired = load_config(project_root=root, config_path=path)
                repaired.load_status.update(
                    {
                        "status": "loaded_after_auto_path_repair",
                        "config_path": str(path),
                        "recovery": recovery,
                    }
                )
                repaired.warnings.insert(
                    0,
                    "Config media_root quoting was repaired automatically after a timestamped backup; no media was changed.",
                )
                return repaired
        return build_fallback_config(root, path, exc, recovery=recovery)

def apply_environment_overrides(data: dict[str, Any]) -> dict[str, Any]:
    result = deep_merge(DEFAULT_CONFIG, data)
    env_map = {
        "MEDIATAGGERBOT_MEDIA_ROOT": ("paths", "media_root"),
        "ACOUSTID_CLIENT_KEY": ("apis", "acoustid_client_key"),
        "LASTFM_API_KEY": ("apis", "lastfm_api_key"),
        "DISCOGS_USER_TOKEN": ("apis", "discogs_user_token"),
        "MEDIATAGGERBOT_USER_AGENT": ("apis", "user_agent"),
    }
    for env_name, (section, key) in env_map.items():
        value = os.environ.get(env_name)
        if value:
            result.setdefault(section, {})[key] = value
    return result


def copy_example_config_if_missing(project_root: Path, config_path: Path | None = None) -> Path:
    """Create only the normal project config, never an arbitrary custom path.

    Diagnostics and request-stop deliberately skip this function so their control
    path does not bootstrap configuration or dependencies.
    """
    config_dir = project_root / "config"
    target = (config_path or (config_dir / "config.toml")).resolve()
    default_target = (config_dir / "config.toml").resolve()
    if target != default_target:
        return target
    config_dir.mkdir(parents=True, exist_ok=True)
    example_path = config_dir / "config.example.toml"
    if not target.exists() and example_path.exists():
        temporary = target.with_name(f".{target.name}.bootstrap.tmp")
        try:
            shutil.copy2(example_path, temporary)
            # Validate the complete candidate before publishing it.
            load_toml(temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return target


def redacted_effective_config(config: AppConfig) -> dict[str, Any]:
    redacted = deep_merge(DEFAULT_CONFIG, config.data)
    for key in ["acoustid_client_key", "lastfm_api_key", "discogs_user_token"]:
        value = redacted.get("apis", {}).get(key, "")
        if value:
            redacted["apis"][key] = f"present:{len(str(value))}chars"
        else:
            redacted["apis"][key] = "missing"
    return redacted


def python_version_summary() -> str:
    return f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro} ({sys.executable})"
