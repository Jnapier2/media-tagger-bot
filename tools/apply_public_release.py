#!/usr/bin/env python3
from __future__ import annotations
import base64, hashlib, os, sys, tempfile, zlib
from pathlib import Path
EXPECTED_SHA256 = "4ddf00b991d9deb189d615be7947ccc97d82cabdb228e3816fcc1b6e691c05a8"

def main() -> int:
    root = Path(__file__).resolve().parent / "payload"
    encoded = "".join(path.read_text(encoding="ascii") for path in sorted(root.glob("part*.txt")))
    raw = zlib.decompress(base64.b64decode(encoded))
    actual = hashlib.sha256(raw).hexdigest()
    if actual != EXPECTED_SHA256:
        raise RuntimeError(f"embedded reconciler SHA-256 mismatch: {actual}")
    work = Path(tempfile.mkdtemp(prefix="mtb-reconciler-"))
    impl = work / "apply_public_release_impl.py"
    impl.write_bytes(raw)
    os.execv(sys.executable, [sys.executable, str(impl), *sys.argv[1:]])
    return 1

if __name__ == "__main__":
    raise SystemExit(main())
