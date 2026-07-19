from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping

_LAUNCHER_ENV_KEYS = (
    "MEDIATAGGERBOT_LAUNCHER_KIND",
    "MEDIATAGGERBOT_LAUNCHER_VERSION",
    "MEDIATAGGERBOT_LAUNCHER_PROJECT_ROOT",
    "MEDIATAGGERBOT_BATCH_LOG",
)


def build_launcher_attestation(
    project_root: Path,
    app_version: str,
    env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Describe and validate the launcher-to-Python handoff.

    A direct ``python -m mediataggerbot`` invocation is supported and reported as
    ``direct_python``.  When the BAT menu declares itself, its version, project
    root, and transcript destination must agree with the running package.  The
    values contain no credentials and are safe for redacted diagnostics.
    """
    values = os.environ if env is None else env
    raw = {key: str(values.get(key, "") or "") for key in _LAUNCHER_ENV_KEYS}
    kind = raw["MEDIATAGGERBOT_LAUNCHER_KIND"].strip()
    version = raw["MEDIATAGGERBOT_LAUNCHER_VERSION"].strip()
    root_text = raw["MEDIATAGGERBOT_LAUNCHER_PROJECT_ROOT"].strip()
    batch_log_text = raw["MEDIATAGGERBOT_BATCH_LOG"].strip()

    base: dict[str, object] = {
        "schema": "MediaTaggerBot.launcher_attestation.v1",
        "launcher_environment_present": any(raw.values()),
        "kind": kind or "direct_python",
        "declared_version": version or "not_declared",
        "expected_version": str(app_version),
        "declared_project_root": root_text or "not_declared",
        "expected_project_root": str(project_root),
        "batch_log": batch_log_text or "not_declared",
        "confirmed": False,
        "safe_to_process": True,
        "reasons": [],
    }
    if not any(raw.values()):
        base.update(
            {
                "status": "direct_python",
                "confirmed": True,
                "safe_to_process": True,
                "reasons": ["No BAT attestation was declared; direct Python invocation is supported."],
            }
        )
        return base

    reasons: list[str] = []
    if kind.casefold() != "bat_menu":
        reasons.append("launcher_kind_not_bat_menu")
    if version != str(app_version):
        reasons.append("launcher_version_mismatch")
    if not root_text or not _same_path(root_text, project_root):
        reasons.append("launcher_project_root_mismatch")
    if not batch_log_text:
        reasons.append("batch_log_not_declared")
    elif not _path_is_under(batch_log_text, project_root / "logs" / "batch_runs"):
        reasons.append("batch_log_outside_project_logs")

    confirmed = not reasons
    base.update(
        {
            "status": "confirmed_bat" if confirmed else "launcher_mismatch",
            "confirmed": confirmed,
            "safe_to_process": confirmed,
            "reasons": reasons or ["BAT version, project root, and transcript path matched the running package."],
        }
    )
    return base


def _same_path(raw_path: str, expected: Path) -> bool:
    return _normalize(raw_path) == _normalize(str(expected))


def _path_is_under(raw_path: str, expected_parent: Path) -> bool:
    try:
        path = _normalize(raw_path)
        parent = _normalize(str(expected_parent))
        return os.path.commonpath([path, parent]) == parent
    except (OSError, ValueError):
        return False


def _normalize(raw_path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(str(raw_path)))).rstrip("\\/")
