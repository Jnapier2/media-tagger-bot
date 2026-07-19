from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


class JsonCache:
    """Noncritical API/fingerprint cache with fail-isolated recovery.

    Cache loss must never block matching. Corrupt cache files are quarantined and a clean
    schema is created when auto recovery is enabled. The operation journal uses a separate,
    fail-closed implementation because it is recovery evidence rather than disposable cache.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Path, ttl_days: int = 365, *, auto_recover: bool = True) -> None:
        self.path = path
        self.ttl_seconds = ttl_days * 24 * 60 * 60
        self.conn: sqlite3.Connection | None = None
        self.disabled = False
        self.recovery: dict[str, Any] = {
            "auto_recover_enabled": bool(auto_recover),
            "recovered": False,
            "quarantined_files": [],
            "initial_error": "",
            "recovery_error": "",
            "disabled_reason": "",
        }
        self.stats: dict[str, int] = {
            "hits": 0,
            "misses": 0,
            "expired": 0,
            "writes": 0,
            "decode_errors": 0,
            "read_errors": 0,
            "write_errors": 0,
            "optimize_open_errors": 0,
            "optimize_close_errors": 0,
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._open_and_initialize()
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            if not auto_recover:
                raise
            self.recovery["initial_error"] = f"{type(exc).__name__}: {exc}"
            self._close_best_effort()
            try:
                self.recovery["quarantined_files"] = self._quarantine_database_files()
                self.path.parent.mkdir(parents=True, exist_ok=True)
                self._open_and_initialize()
            except (sqlite3.Error, OSError, RuntimeError) as recovery_exc:
                # This cache is reproducible optimization state. If quarantine/reopen
                # is impossible (permissions, lock, read-only volume), continue with
                # caching disabled rather than blocking a 30k-file matching run.
                self._close_best_effort()
                self.disabled = True
                self.recovery["recovery_error"] = f"{type(recovery_exc).__name__}: {recovery_exc}"
                self.recovery["disabled_reason"] = "cache_recovery_failed_noncritical"
            else:
                self.recovery["recovered"] = True

    def _open_and_initialize(self) -> None:
        self.conn = sqlite3.connect(str(self.path), timeout=15.0)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=15000")
        integrity = self.conn.execute("PRAGMA quick_check(1)").fetchone()
        if not integrity or str(integrity[0]).casefold() != "ok":
            raise sqlite3.DatabaseError(f"cache quick_check failed: {integrity!r}")
        current_version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
        if current_version > self.SCHEMA_VERSION:
            raise RuntimeError(
                f"API cache schema {current_version} is newer than supported schema {self.SCHEMA_VERSION}."
            )
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS cache (namespace TEXT NOT NULL, cache_key TEXT NOT NULL, "
            "created REAL NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(namespace, cache_key))"
        )
        if current_version < self.SCHEMA_VERSION:
            self.conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
        self.conn.commit()
        try:
            self.conn.execute("PRAGMA optimize=0x10002")
        except sqlite3.Error:
            self.stats["optimize_open_errors"] += 1
        self.disabled = False

    def get(self, namespace: str, key: str) -> Any | None:
        if self.disabled or self.conn is None:
            self.stats["misses"] += 1
            return None
        try:
            row = self.conn.execute(
                "SELECT created, payload FROM cache WHERE namespace=? AND cache_key=?", (namespace, key)
            ).fetchone()
        except sqlite3.Error:
            self.stats["read_errors"] += 1
            self.stats["misses"] += 1
            self.disabled = True
            return None
        if not row:
            self.stats["misses"] += 1
            return None
        created, payload = row
        if self.ttl_seconds > 0 and time.time() - float(created) > self.ttl_seconds:
            self.stats["expired"] += 1
            self.stats["misses"] += 1
            return None
        try:
            decoded = json.loads(payload)
        except (ValueError, TypeError):
            self.stats["decode_errors"] += 1
            return None
        self.stats["hits"] += 1
        return decoded

    def set(self, namespace: str, key: str, payload: Any) -> None:
        if self.disabled or self.conn is None:
            return
        try:
            self.conn.execute(
                "INSERT OR REPLACE INTO cache(namespace, cache_key, created, payload) VALUES (?, ?, ?, ?)",
                (namespace, key, time.time(), json.dumps(payload, ensure_ascii=False, sort_keys=True)),
            )
            self.conn.commit()
            self.stats["writes"] += 1
        except (sqlite3.Error, ValueError, TypeError):
            # A successful provider response remains usable even when optional cache
            # persistence fails. Disable further writes for this run and report telemetry.
            self.stats["write_errors"] += 1
            self.disabled = True

    def snapshot(self) -> dict[str, Any]:
        schema_version: int | None = None
        if self.conn is not None and not self.disabled:
            try:
                schema_version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
            except sqlite3.Error:
                schema_version = None
        return {
            "path": str(self.path),
            "schema_version": schema_version,
            "supported_schema_version": self.SCHEMA_VERSION,
            "disabled_for_run": self.disabled,
            "recovery": self.recovery,
            **self.stats,
        }

    def close(self) -> None:
        self._close_best_effort()

    def _close_best_effort(self) -> None:
        if self.conn is not None:
            try:
                if not self.disabled:
                    try:
                        self.conn.execute("PRAGMA optimize")
                    except sqlite3.Error:
                        self.stats["optimize_close_errors"] += 1
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None

    def _quarantine_database_files(self) -> list[str]:
        stamp = time.strftime("%Y%m%d_%H%M%S", time.gmtime())
        quarantined: list[str] = []
        for source in [self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")]:
            if not source.exists():
                continue
            target = source.with_name(f"{source.name}.corrupt_{stamp}")
            counter = 2
            while target.exists():
                target = source.with_name(f"{source.name}.corrupt_{stamp}_{counter}")
                counter += 1
            os.replace(source, target)
            quarantined.append(str(target))
        return quarantined

    def __enter__(self) -> "JsonCache":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
