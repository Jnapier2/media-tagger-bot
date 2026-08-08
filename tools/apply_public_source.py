from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
PAYLOAD_URL = "https://drive.google.com/uc?export=download&id=1KZju5XMvpimSERWJQPpcVyzKhP_XUfI_"
EXPECTED_SHA256 = "e33722ae13becd581a621ac13bb525085ee094c2a74c6987c901d48b0d7067c8"
PRESERVE = {".git", ".github", ".gitattributes", ".gitignore", "LICENSE.md", "SECURITY.md"}


def run(*args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(list(args), cwd=ROOT, env=env, check=True)


with tempfile.TemporaryDirectory(prefix="mtb_public_source_") as temporary:
    zip_path = Path(temporary) / "MediaTaggerBot_v0.5.9_PUBLIC_SOURCE.zip"
    urllib.request.urlretrieve(PAYLOAD_URL, zip_path)
    payload = zip_path.read_bytes()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != EXPECTED_SHA256:
        raise SystemExit(f"payload SHA-256 mismatch: {actual}")

    with zipfile.ZipFile(zip_path) as archive:
        if archive.testzip() is not None:
            raise SystemExit("payload ZIP CRC failed")
        seen: set[str] = set()
        for info in archive.infolist():
            name = info.filename.replace("\\", "/")
            pure = PurePosixPath(name)
            if pure.is_absolute() or any(part in {"", ".", ".."} for part in pure.parts):
                raise SystemExit(f"unsafe payload path: {name}")
            folded = name.casefold()
            if folded in seen:
                raise SystemExit(f"duplicate payload path: {name}")
            seen.add(folded)

        for child in ROOT.iterdir():
            if child.name in PRESERVE or child.name == "tools":
                continue
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
        archive.extractall(ROOT)

# The one-use transfer script deletes itself from the resulting public tree.
shutil.rmtree(ROOT / "tools", ignore_errors=True)

env = os.environ.copy()
env["PYTHONPATH"] = str(ROOT / "src")
run(sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--only-binary=:all:", "--require-hashes", "-r", "requirements.lock.txt", env=env)
run(sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-r", "requirements-test.txt", env=env)
run(sys.executable, "-m", "pip", "check", env=env)
run(sys.executable, "-m", "compileall", "-q", "src", "scripts", "tests", env=env)
run(sys.executable, "-m", "pytest", "-q", "tests", env=env)
run(
    sys.executable,
    "-c",
    "from pathlib import Path; from mediataggerbot import __version__, __build_id__; from mediataggerbot.package_identity import verify_runtime_identity; r=verify_runtime_identity(Path('.'),__version__,__build_id__); print(r['gate_result'],r['package_verified_count'],r['package_managed_count']); raise SystemExit(0 if r['gate_result']=='PASS' else 1)",
    env=env,
)

run("git", "config", "user.name", "github-actions[bot]")
run("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
run("git", "add", "-A")
status = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
if status.returncode == 0:
    print("No publication changes to commit.")
    raise SystemExit(0)
run("git", "commit", "-m", "Publish sanitized MediaTaggerBot v0.5.9 source")
run("git", "push", "origin", f"HEAD:{os.environ['GITHUB_REF_NAME']}")
