from __future__ import annotations

from pathlib import Path


def write_minimal_mp3(path: Path, frames: int = 4) -> Path:
    """Write a tiny parseable MPEG-1 Layer III stream for metadata tests."""
    frame = bytes.fromhex("FFFB9064") + bytes(417 - 4)
    path.write_bytes(frame * max(2, int(frames)))
    return path


def write_minimal_asf_signature(path: Path) -> Path:
    """Write the ASF header GUID; sufficient for container-family readiness tests."""
    path.write_bytes(bytes.fromhex("3026B2758E66CF11A6D900AA0062CE6C") + bytes(64))
    return path
