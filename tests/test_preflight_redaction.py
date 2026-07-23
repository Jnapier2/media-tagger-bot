from __future__ import annotations

import json
from pathlib import Path

from mediataggerbot.config import load_config
from mediataggerbot.main import build_credential_status

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_credential_status_contains_booleans_not_values() -> None:
    cfg = load_config(
        project_root=PROJECT_ROOT,
        config_path=PROJECT_ROOT / "config" / "config.toml",
    )
    secret_values = {
        "acoustid_client_key": "acoustid-private-value",
        "lastfm_api_key": "lastfm-private-value",
        "discogs_user_token": "discogs-private-value",
    }
    cfg.data["apis"].update(secret_values)

    status = build_credential_status(cfg)
    serialized = json.dumps(status)

    assert status == {
        "acoustid_configured": True,
        "lastfm_configured": True,
        "discogs_configured": True,
    }
    assert all(value not in serialized for value in secret_values.values())
