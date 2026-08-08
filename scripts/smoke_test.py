from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mediataggerbot.config import load_config
from mediataggerbot.genre import classify_genre
from mediataggerbot.matcher import parse_artist_title_from_filename
from mediataggerbot.models import MatchResult
from mediataggerbot.rename import build_target_path
from mediataggerbot.utils import redact_sensitive_text
from mediataggerbot.pathing import build_input_assurance, build_path_status, looks_absolute_path


def main() -> int:
    cfg = load_config(project_root=ROOT, config_path=ROOT / "config" / "config.toml")
    genre = classify_genre(["trap", "hip hop", "southern hip hop"], cfg)
    assert genre.main_genre == "Hip-Hop/Rap"
    assert "/" not in genre.filename_main_genre
    artist, title = parse_artist_title_from_filename("The Artist - The Song (Official Music Video) [HD]")
    assert artist == "The Artist"
    assert title == "The Song"
    status = build_path_status(cfg)
    assert status["portability_check"]["status"] in {"pass", "warning"}
    assert looks_absolute_path("D:\\Music Videos")
    match = MatchResult(matched=True, confidence=99, source="smoke", artist="The Artist", title="The Song")
    target = build_target_path(Path("C:/Temp/The Artist - The Song.mp3"), match, genre, cfg)
    assert "The Artist - The Song - Hip-Hop-Rap" in str(target)
    print("Smoke test passed.")
    print(f"Target sample: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
