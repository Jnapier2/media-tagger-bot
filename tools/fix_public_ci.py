from __future__ import annotations

import csv
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RIGHTS = "Copyright © 2026 Gateway Information Group LLC. All rights reserved."


def run(*args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(list(args), cwd=ROOT, env=env, check=True)


(ROOT / ".gitattributes").write_text(
    "* text=auto\n"
    "*.bat text eol=crlf\n"
    "*.cmd text eol=crlf\n"
    "*.ps1 text eol=crlf\n"
    "*.py text eol=lf\n"
    "*.md text eol=lf\n"
    "*.toml text eol=lf\n"
    "*.txt text eol=lf\n"
    "*.json text eol=lf\n"
    "*.csv text eol=lf\n"
    "*.yml text eol=lf\n"
    "*.yaml text eol=lf\n",
    encoding="utf-8",
    newline="\n",
)

path = ROOT / "tests" / "test_v054_stability_usability.py"
text = path.read_text(encoding="utf-8")
text = text.replace(
    '    cfg.data["naming"]["max_full_path_length"] = 140\n'
    '    nested = tmp_path / ("deep-folder-" * 4)\n',
    '    nested = tmp_path / ("deep-folder-" * 4)\n',
)
text = text.replace(
    '    source.write_bytes(b"")\n\n'
    '    target = build_target_path(source, _match(), _genre(), cfg)\n\n'
    '    assert len(str(target)) <= 140\n',
    '    source.write_bytes(b"")\n'
    '    from mediataggerbot.utils import windows_utf16_units\n'
    '    budget = windows_utf16_units(str(nested)) + 1 + windows_utf16_units(".mp3") + 60\n'
    '    cfg.data["naming"]["max_full_path_length"] = budget\n\n'
    '    target = build_target_path(source, _match(), _genre(), cfg)\n\n'
    '    assert windows_utf16_units(str(target)) <= budget\n',
    1,
)
text = text.replace(
    '    cfg.data["naming"]["max_full_path_length"] = 150\n'
    '    source = tmp_path / "source.mp3"\n',
    '    source = tmp_path / "source.mp3"\n',
)
text = text.replace(
    '    source.write_bytes(b"")\n'
    '    first = build_target_path(source, _match(), _genre(), cfg)\n',
    '    source.write_bytes(b"")\n'
    '    from mediataggerbot.utils import windows_utf16_units\n'
    '    budget = windows_utf16_units(str(tmp_path)) + 1 + windows_utf16_units(" (2).mp3") + 70\n'
    '    cfg.data["naming"]["max_full_path_length"] = budget\n'
    '    first = build_target_path(source, _match(), _genre(), cfg)\n',
    1,
)
text = text.replace('    assert len(str(second)) <= 150\n', '    assert windows_utf16_units(str(second)) <= budget\n')
text = text.replace(
    '    cfg.data["naming"]["max_full_path_length"] = 120\n'
    '    parent = tmp_path / ("x" * 100)\n',
    '    parent = tmp_path / ("x" * 100)\n',
)
text = text.replace(
    '    source.write_bytes(b"")\n\n'
    '    with pytest.raises(RuntimeError, match="full-path budget"):\n',
    '    source.write_bytes(b"")\n'
    '    from mediataggerbot.utils import windows_utf16_units\n'
    '    cfg.data["naming"]["max_full_path_length"] = windows_utf16_units(str(parent)) + 1 + windows_utf16_units(".mp3") + 10\n\n'
    '    with pytest.raises(RuntimeError, match="full-path budget"):\n',
    1,
)
path.write_text(text, encoding="utf-8", newline="\n")

path = ROOT / "tests" / "test_v057_review_hardening.py"
text = path.read_text(encoding="utf-8")
text = text.replace(
    '    fitted = fit_stem_to_full_path_budget(tmp_path, stem, ".mp3", 120)\n'
    '    full = str(tmp_path / f"{fitted}.mp3")\n'
    '    assert windows_utf16_units(full) <= 120\n',
    '    budget = windows_utf16_units(str(tmp_path)) + 1 + windows_utf16_units(".mp3") + 50\n'
    '    fitted = fit_stem_to_full_path_budget(tmp_path, stem, ".mp3", budget)\n'
    '    full = str(tmp_path / f"{fitted}.mp3")\n'
    '    assert windows_utf16_units(full) <= budget\n',
)
path.write_text(text, encoding="utf-8", newline="\n")

manifest_path = ROOT / "MANIFEST.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
for row in manifest["files"]:
    if not row.get("package_managed"):
        continue
    target = ROOT / row["path"]
    data = target.read_bytes()
    row["size_bytes"] = len(data)
    row["sha256"] = hashlib.sha256(data).hexdigest()
manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
with (ROOT / "MANIFEST.csv").open("w", encoding="utf-8-sig", newline="") as handle:
    writer = csv.writer(handle)
    writer.writerow(["asset_id", "path", "version", "status", "sensitivity", "package_managed", "size_bytes", "sha256", "rights_notice"])
    for row in manifest["files"]:
        writer.writerow([
            row.get("asset_id"), row["path"], row["version"], row["status"], row["sensitivity"],
            str(row["package_managed"]), "" if row.get("size_bytes") is None else row["size_bytes"],
            "" if row.get("sha256") is None else row["sha256"], RIGHTS,
        ])

# The temporary repair script removes itself from the public tree.
path = Path(__file__)
path.unlink()
try:
    path.parent.rmdir()
except OSError:
    pass

env = os.environ.copy()
env["PYTHONPATH"] = str(ROOT / "src")
run(sys.executable, "-m", "pytest", "-q", "tests", env=env)
run(sys.executable, "-c", "from pathlib import Path; from mediataggerbot import __version__,__build_id__; from mediataggerbot.package_identity import verify_runtime_identity; r=verify_runtime_identity(Path('.'),__version__,__build_id__); print(r['gate_result']); raise SystemExit(0 if r['gate_result']=='PASS' else 1)", env=env)
run("git", "config", "user.name", "github-actions[bot]")
run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
run("git", "add", "-A")
run("git", "commit", "-m", "Make v0.5.9 integrity and path tests checkout-independent")
run("git", "push", "origin", f"HEAD:{os.environ['GITHUB_REF_NAME']}")
