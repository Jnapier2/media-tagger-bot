from __future__ import annotations

import re
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXACT_REQUIREMENT = re.compile(r"^([A-Za-z0-9_.-]+)==([^\s]+)$")
LOCK_ENTRY = re.compile(r"(?m)^([A-Za-z0-9_.-]+)==([^\s\\]+)")
BAT_EXPECTED = re.compile(r"expected=\{([^}]+)\}")
BAT_PAIR = re.compile(r"'([^']+)':'([^']+)'")


def normalized_name(value: str) -> str:
    return re.sub(r"[-_.]+", "-", value).lower()


class DependencyContractTests(unittest.TestCase):
    def test_project_lock_and_primary_launcher_agree(self) -> None:
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        direct: dict[str, str] = {}
        for requirement in project["project"]["dependencies"]:
            match = EXACT_REQUIREMENT.fullmatch(requirement)
            self.assertIsNotNone(match, f"direct dependency is not exactly pinned: {requirement}")
            assert match is not None
            direct[normalized_name(match.group(1))] = match.group(2)

        lock_text = (ROOT / "requirements.lock.txt").read_text(encoding="utf-8")
        locked = {
            normalized_name(name): version
            for name, version in LOCK_ENTRY.findall(lock_text)
        }
        self.assertTrue(direct.items() <= locked.items(), "pyproject and runtime lock disagree")

        launcher_text = (ROOT / "Start_MediaTaggerBot.bat").read_text(encoding="utf-8-sig")
        launcher_blocks = BAT_EXPECTED.findall(launcher_text)
        self.assertEqual(
            len(launcher_blocks),
            2,
            "primary launcher must validate dependencies before and after installation",
        )
        launcher_maps = [
            {normalized_name(name): version for name, version in BAT_PAIR.findall(block)}
            for block in launcher_blocks
        ]
        self.assertEqual(launcher_maps[0], launcher_maps[1], "launcher dependency checks drifted")
        self.assertEqual(launcher_maps[0], locked, "launcher and runtime lock disagree")


if __name__ == "__main__":
    unittest.main()
