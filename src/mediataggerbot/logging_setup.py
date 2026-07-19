from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_logging(
    log_dir: Path,
    run_id: str,
    verbose: bool = False,
    *,
    max_bytes: int = 10_000_000,
    backup_count: int = 3,
) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"run_{run_id}.log"
    level = logging.DEBUG if verbose else logging.INFO
    fmt = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
    datefmt = "%Y-%m-%d %H:%M:%S%z"

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
        try:
            handler.flush()
        finally:
            handler.close()
    root.setLevel(level)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=max(100_000, int(max_bytes)),
        backupCount=max(1, int(backup_count)),
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter(fmt=fmt, datefmt=datefmt))
    file_handler.setLevel(level)
    root.addHandler(file_handler)

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(console)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_path
