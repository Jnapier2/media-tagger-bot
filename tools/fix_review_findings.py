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


def replace_once(text: str, old: str, new: str, *, label: str) -> str:
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{label}: expected exactly one match, found {count}")
    return text.replace(old, new, 1)


# Public source may install the exact hash-locked requirements from an index.
# Bundled wheels remain supported and verified when they are present, but are
# not required in a sanitized public checkout.
verifier = ROOT / "scripts" / "verify_runtime_environment.py"
text = verifier.read_text(encoding="utf-8")
text = replace_once(
    text,
    '    lock_path = project_root / "requirements.lock.txt"\n'
    '    wheels_dir = project_root / "wheels"\n'
    '    if not lock_path.is_file() or not wheels_dir.is_dir():\n'
    '        raise RuntimeError("requirements.lock.txt or wheels/ is missing")\n'
    '    lock_text = lock_path.read_text(encoding="utf-8")\n',
    '    lock_path = project_root / "requirements.lock.txt"\n'
    '    wheels_dir = project_root / "wheels"\n'
    '    if not lock_path.is_file():\n'
    '        raise RuntimeError("requirements.lock.txt is missing")\n'
    '    lock_text = lock_path.read_text(encoding="utf-8")\n',
    label="optional wheels precondition",
)
text = replace_once(
    text,
    '    wheel_records: list[dict[str, Any]] = []\n'
    '    for wheel in sorted(wheels_dir.glob("*.whl")):\n'
    '        digest = sha256_file(wheel)\n'
    '        if digest.casefold() not in allowed_hashes:\n'
    '            raise RuntimeError(f"bundled wheel hash is not authorized by requirements.lock.txt: {wheel.name}")\n'
    '        wheel_records.append({"name": wheel.name, "sha256": digest, "size_bytes": wheel.stat().st_size})\n'
    '    if not wheel_records:\n'
    '        raise RuntimeError("bundled wheels directory is empty")\n\n'
    '    distributions = [verify_distribution(name, version) for name, version in EXPECTED.items()]\n',
    '    wheel_records: list[dict[str, Any]] = []\n'
    '    dependency_source = "package_index_with_hash_locked_requirements"\n'
    '    if wheels_dir.exists():\n'
    '        if not wheels_dir.is_dir():\n'
    '            raise RuntimeError("wheels exists but is not a directory")\n'
    '        for wheel in sorted(wheels_dir.glob("*.whl")):\n'
    '            digest = sha256_file(wheel)\n'
    '            if digest.casefold() not in allowed_hashes:\n'
    '                raise RuntimeError(f"bundled wheel hash is not authorized by requirements.lock.txt: {wheel.name}")\n'
    '            wheel_records.append({"name": wheel.name, "sha256": digest, "size_bytes": wheel.stat().st_size})\n'
    '        if not wheel_records:\n'
    '            raise RuntimeError("bundled wheels directory is empty")\n'
    '        dependency_source = "bundled_verified_wheels"\n\n'
    '    distributions = [verify_distribution(name, version) for name, version in EXPECTED.items()]\n',
    label="optional wheels verification",
)
text = replace_once(
    text,
    '        "requirements_lock_sha256": sha256_file(lock_path),\n'
    '        "wheels": wheel_records,\n'
    '        "distributions": distributions,\n',
    '        "requirements_lock_sha256": sha256_file(lock_path),\n'
    '        "dependency_source": dependency_source,\n'
    '        "wheels": wheel_records,\n'
    '        "distributions": distributions,\n',
    label="dependency source attestation",
)
verifier.write_text(text, encoding="utf-8", newline="\n")

# Restore bounded retries for transient Windows sharing violations while
# preserving atomic same-directory publication and fsync behavior.
utils = ROOT / "src" / "mediataggerbot" / "utils.py"
text = utils.read_text(encoding="utf-8")
text = replace_once(text, "import tempfile\nimport unicodedata\n", "import tempfile\nimport time\nimport unicodedata\n", label="time import")
if text.count("        os.replace(temp_name, path)\n") != 2:
    raise SystemExit("atomic replace call count changed")
text = text.replace("        os.replace(temp_name, path)\n", "        _replace_with_retry(temp_name, path)\n")
marker = '''def append_jsonl(path: Path, data: Any) -> None:\n'''
helper = '''def _replace_with_retry(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:\n    \"\"\"Bound transient sharing violations without weakening atomic publication.\"\"\"\n    attempts = 4\n    for attempt in range(attempts):\n        try:\n            os.replace(source, destination)\n            return\n        except PermissionError:\n            if attempt == attempts - 1:\n                raise\n            time.sleep(0.05 * (attempt + 1))\n\n\n'''
if helper not in text:
    text = replace_once(text, marker, helper + marker, label="sharing-violation retry helper")
utils.write_text(text, encoding="utf-8", newline="\n")

# Add focused regression tests for both findings.
test_path = ROOT / "tests" / "test_public_release_contract.py"
test_text = test_path.read_text(encoding="utf-8")
append = '''\n\ndef test_runtime_environment_verifier_allows_index_installed_dependencies_without_wheels(tmp_path, monkeypatch):\n    from scripts import verify_runtime_environment as verifier\n\n    source_lock = Path(__file__).resolve().parents[1] / "requirements.lock.txt"\n    (tmp_path / "requirements.lock.txt").write_bytes(source_lock.read_bytes())\n    (tmp_path / "pyproject.toml").write_text('[project]\\nversion = "0.5.9"\\n', encoding="utf-8")\n    monkeypatch.setattr(verifier.platform, "machine", lambda: "AMD64")\n    monkeypatch.setattr(verifier, "verify_distribution", lambda name, version: {"name": name, "version": version, "record_hashes_checked": 1})\n\n    result = verifier.verify(tmp_path)\n\n    assert result["dependency_source"] == "package_index_with_hash_locked_requirements"\n    assert result["wheels"] == []\n    assert result["status"] == "verified"\n\n\ndef test_atomic_replace_retries_transient_permission_error(tmp_path, monkeypatch):\n    from mediataggerbot import utils\n\n    source = tmp_path / "source.tmp"\n    destination = tmp_path / "destination.json"\n    source.write_text("new", encoding="utf-8")\n    destination.write_text("old", encoding="utf-8")\n    real_replace = os.replace\n    calls = {"count": 0}\n\n    def flaky_replace(left, right):\n        calls["count"] += 1\n        if calls["count"] < 3:\n            raise PermissionError("transient sharing violation")\n        return real_replace(left, right)\n\n    monkeypatch.setattr(utils.os, "replace", flaky_replace)\n    monkeypatch.setattr(utils.time, "sleep", lambda _seconds: None)\n\n    utils._replace_with_retry(source, destination)\n\n    assert calls["count"] == 3\n    assert destination.read_text(encoding="utf-8") == "new"\n'''
if "test_runtime_environment_verifier_allows_index_installed_dependencies_without_wheels" not in test_text:
    test_path.write_text(test_text.rstrip() + append + "\n", encoding="utf-8", newline="\n")

# Re-seal all immutable public managed files after the reviewed code/test fixes.
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

# Remove the one-use repair script before validating and committing.
script = Path(__file__)
script.unlink()
try:
    script.parent.rmdir()
except OSError:
    pass

env = os.environ.copy()
env["PYTHONPATH"] = str(ROOT / "src")
run(sys.executable, "-m", "pytest", "-q", "tests", env=env)
run(sys.executable, "-c", "from pathlib import Path; from mediataggerbot import __version__,__build_id__; from mediataggerbot.package_identity import verify_runtime_identity; r=verify_runtime_identity(Path('.'),__version__,__build_id__); print(r['gate_result'],r['package_verified_count'],r['package_managed_count']); raise SystemExit(0 if r['gate_result']=='PASS' else 1)", env=env)
run("git", "config", "user.name", "github-actions[bot]")
run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
run("git", "add", "-A")
run("git", "commit", "-m", "Address public v0.5.9 runtime review findings")
run("git", "push", "origin", f"HEAD:{os.environ['GITHUB_REF_NAME']}")
