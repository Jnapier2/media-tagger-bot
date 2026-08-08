from __future__ import annotations

import runpy
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
repair = ROOT / "tools" / "fix_review_findings.py"
text = repair.read_text(encoding="utf-8")
old = 'test_path = ROOT / "tests" / "test_public_release_contract.py"'
new = 'test_path = ROOT / "tests" / "test_v057_review_hardening.py"'
if text.count(old) != 1:
    raise SystemExit("review repair test target changed")
repair.write_text(text.replace(old, new, 1), encoding="utf-8", newline="\n")
Path(__file__).unlink()
runpy.run_path(str(repair), run_name="__main__")
