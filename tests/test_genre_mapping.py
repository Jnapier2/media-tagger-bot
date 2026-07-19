from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mediataggerbot.config import load_config
from mediataggerbot.genre import classify_genre


def test_core_genre_mapping():
    cfg = load_config(project_root=ROOT, config_path=ROOT / "config" / "config.toml")
    cases = [
        (["dance pop"], "Pop"),
        (["hard rock"], "Rock"),
        (["trap"], "Hip-Hop/Rap"),
        (["house"], "Electronic Dance Music (EDM)"),
        (["country pop"], "Country"),
        (["neo soul"], "R&B/Soul"),
        (["bebop"], "Jazz"),
        (["baroque"], "Classical"),
    ]
    for terms, expected in cases:
        assert classify_genre(terms, cfg).main_genre == expected
