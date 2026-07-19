from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture(scope="session", autouse=True)
def repository_test_config():
    """Exercise the checked-in example config without committing runtime settings."""
    root = Path(__file__).resolve().parents[1]
    target = root / "config" / "config.toml"
    example = root / "config" / "config.example.toml"
    created = not target.exists()
    if created:
        shutil.copy2(example, target)
    try:
        yield
    finally:
        if created:
            target.unlink(missing_ok=True)
