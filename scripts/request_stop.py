from __future__ import annotations

import sys
from pathlib import Path


def main() -> int:
    project_root = Path(__file__).resolve().parents[1]
    src = project_root / "src"
    sys.path.insert(0, str(src))

    from mediataggerbot import __version__
    from mediataggerbot.portable_stop import run_portable_stop

    code, result, evidence_path = run_portable_stop(project_root, app_version=__version__)
    print(result.get("message", "Graceful-stop request status recorded."))
    print(f"Control path: {result.get('control_path')}")
    print(f"Config read status: {result.get('config_read_status')}")
    print("Runtime setup attempted: no")
    print("Virtual environment modified: no")
    print(f"Request evidence: {evidence_path}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
