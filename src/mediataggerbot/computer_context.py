from __future__ import annotations

import hashlib
import os
import re
import socket
from pathlib import Path
from typing import Any, Mapping

COMPUTER_CONTEXT_SCHEMA = "MediaTaggerBot.computer_context.v1"
PROFILE_VERSION = "Gateway computer profiles v2.17.5 / parameter 2026-08-06; source index 2026-07-24"

_PROFILES: dict[str, dict[str, Any]] = {
    "PC-ALPHA-01": {
        "display_name": "ALPHA",
        "aliases": ["alpha", "alpha computer", "pc-alpha-01", "main desktop", "primary desktop"],
        "role": "primary workstation",
        "performance_hint": "Suitable for sustained bulk media work; still verify current free space and temperatures.",
    },
    "PC-ASCEND-02": {
        "display_name": "ASCEND",
        "aliases": ["ascend", "ascend laptop", "pc-ascend-02", "g634jy", "asus rog strix"],
        "role": "mobile high-performance workstation",
        "performance_hint": "Use AC power and verify current thermals before long unattended processing.",
    },
    "PC-DEUSEX-03": {
        "display_name": "DeusEx",
        "aliases": [
            "deusex", "deus ex", "pc-deusex-03", "raider", "msi raider", "ge66",
            "raider ge66 12uhs", "pc-raider-03",
        ],
        "role": "secondary compatibility workstation",
        "performance_hint": "Use AC power and generic bounded settings for long unattended processing.",
    },
}

_ALIAS_INDEX: dict[str, str] = {}
for _canonical_id, _profile in _PROFILES.items():
    for _alias in [_canonical_id, _profile["display_name"], *_profile["aliases"]]:
        _ALIAS_INDEX[re.sub(r"[^a-z0-9]+", "", str(_alias).casefold())] = _canonical_id


def detect_computer_context(
    config: Any | None = None,
    *,
    raw_config: Mapping[str, Any] | None = None,
    env: Mapping[str, str] | None = None,
    hostname: str | None = None,
) -> dict[str, Any]:
    """Return privacy-safe advisory computer context.

    Computer identity may label diagnostics and select hints only. It never grants or
    denies startup, assigns ownership, creates a cross-computer lease, or changes media.
    Raw unknown hostnames are intentionally not returned or exported.
    """
    env_map = env if env is not None else os.environ
    enabled = True
    manual_override = ""
    show_active = True
    performance_hints = True

    if config is not None and hasattr(config, "get"):
        enabled = bool(config.get("computer_awareness.enabled", True))
        manual_override = str(config.get("computer_awareness.manual_override", "") or "").strip()
        show_active = bool(config.get("computer_awareness.show_active_computer", True))
        performance_hints = bool(config.get("computer_awareness.performance_hints", True))
    elif raw_config is not None:
        section = raw_config.get("computer_awareness")
        if isinstance(section, Mapping):
            enabled = bool(section.get("enabled", True))
            manual_override = str(section.get("manual_override", "") or "").strip()
            show_active = bool(section.get("show_active_computer", True))
            performance_hints = bool(section.get("performance_hints", True))

    env_override = str(env_map.get("MEDIATAGGERBOT_COMPUTER_OVERRIDE", "") or "").strip()
    selected = env_override or manual_override
    source = "environment_override" if env_override else ("config_override" if manual_override else "hostname_alias")
    observed = selected or str(hostname if hostname is not None else socket.gethostname())

    canonical_id = _resolve_alias(observed) if enabled else None
    known = bool(canonical_id and canonical_id in _PROFILES)
    if known:
        profile = _PROFILES[str(canonical_id)]
        display_name = str(profile["display_name"])
        role = str(profile["role"])
        hint = str(profile["performance_hint"]) if performance_hints else "disabled"
        detection_status = "recognized_known_profile"
    else:
        canonical_id = "PC-UNKNOWN"
        display_name = selected if selected else "Unknown computer"
        role = "generic local workstation"
        hint = (
            "Generic bounded defaults; verify free space and, for laptops, AC power and thermals before long runs."
            if performance_hints else "disabled"
        )
        detection_status = "manual_unknown_profile" if selected else ("awareness_disabled" if not enabled else "unknown_generic")
        source = source if selected else ("disabled" if not enabled else "hostname_no_known_alias")

    return {
        "schema": COMPUTER_CONTEXT_SCHEMA,
        "profile_version": PROFILE_VERSION,
        "canonical_id": canonical_id,
        "display_name": display_name,
        "role": role,
        "known_profile": known,
        "detection_status": detection_status,
        "detection_source": source,
        "show_active_computer": show_active,
        "performance_hint": hint,
        "safe_generic_defaults": not known,
        "advisory_only": True,
        "independent_installation_allowed": True,
        "cross_computer_startup_blocking": False,
        "cross_computer_ownership": False,
        "cross_computer_handoff_required": False,
        "shared_lease_or_write_fence": False,
        "forced_read_only": False,
        "raw_unknown_hostname_exported": False,
    }


def local_lock_path(state_dir: Path, context: Mapping[str, Any]) -> Path:
    """Return a privacy-safe same-computer lock path.

    A host digest is included for every advisory profile—not only unknown machines—so
    two independent computers can never share a lock merely because both use the same
    friendly profile label. The raw hostname is never exported.
    """
    canonical = str(context.get("canonical_id") or "PC-UNKNOWN")
    slug = re.sub(r"[^a-z0-9]+", "-", canonical.casefold()).strip("-") or "pc-unknown"
    digest = hashlib.sha256(socket.gethostname().casefold().encode("utf-8")).hexdigest()[:8]
    return state_dir / f"mediataggerbot.{slug}-{digest}.lock"


def legacy_lock_path(state_dir: Path) -> Path:
    return state_dir / "mediataggerbot.lock"


def local_stop_request_path(state_dir: Path, context: Mapping[str, Any]) -> Path:
    lock_name = local_lock_path(state_dir, context).name
    slug = lock_name.removeprefix("mediataggerbot.").removesuffix(".lock")
    return state_dir / f"graceful_stop_request.{slug}.json"


def stop_request_path_for_lock(state_dir: Path, lock_path: Path) -> Path:
    """Derive the request path from the selected same-host lock itself."""
    name = lock_path.name
    if name == "mediataggerbot.lock":
        return state_dir / "graceful_stop_request.json"
    prefix = "mediataggerbot."
    suffix = ".lock"
    if name.startswith(prefix) and name.endswith(suffix):
        slug = name[len(prefix) : -len(suffix)]
        if slug:
            return state_dir / f"graceful_stop_request.{slug}.json"
    return state_dir / "graceful_stop_request.json"


def safe_computer_label(context: Mapping[str, Any]) -> str:
    name = str(context.get("display_name") or "Unknown computer")
    canonical = str(context.get("canonical_id") or "PC-UNKNOWN")
    return f"{name} ({canonical})"


def _resolve_alias(value: str) -> str | None:
    key = re.sub(r"[^a-z0-9]+", "", str(value).casefold())
    return _ALIAS_INDEX.get(key)
