from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import quote

from .timeutil import now_utc


class OperationJournal:
    """Crash-visible apply journal using durable local SQLite transactions."""

    SCHEMA_VERSION = 1

    def __init__(self, path: Path, run_id: str) -> None:
        self.path = path
        self.run_id = run_id
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=15.0)
        try:
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA synchronous=FULL")
            self.conn.execute("PRAGMA busy_timeout=15000")
            integrity = self.conn.execute("PRAGMA quick_check(1)").fetchone()
            if not integrity or str(integrity[0]).casefold() != "ok":
                raise RuntimeError(
                    f"Operation journal integrity check failed: {integrity!r}. "
                    "Apply is blocked; preserve the database for diagnostics."
                )
            current_version = int(self.conn.execute("PRAGMA user_version").fetchone()[0])
            if current_version > self.SCHEMA_VERSION:
                raise RuntimeError(
                    f"Operation journal schema {current_version} is newer than supported schema "
                    f"{self.SCHEMA_VERSION}; apply is blocked."
                )
            self.conn.execute(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    source_path TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_utc TEXT NOT NULL,
                    updated_utc TEXT NOT NULL,
                    details_json TEXT NOT NULL
                )
                """
            )
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_operations_run_id ON operations(run_id)")
            self.conn.execute("CREATE INDEX IF NOT EXISTS idx_operations_status ON operations(status)")
            if current_version < self.SCHEMA_VERSION:
                self.conn.execute(f"PRAGMA user_version={self.SCHEMA_VERSION}")
            self.conn.commit()
            try:
                self.conn.execute("PRAGMA optimize=0x10002")
            except sqlite3.Error:
                pass
        except Exception:
            self.conn.close()
            raise

    def start(self, source_path: Path, target_path: Path, details: dict[str, Any] | None = None) -> str:
        operation_id = str(uuid.uuid4())
        timestamp = now_utc().isoformat()
        self.conn.execute(
            "INSERT INTO operations(operation_id, run_id, source_path, target_path, stage, status, created_utc, updated_utc, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                operation_id,
                self.run_id,
                str(source_path),
                str(target_path),
                "planned",
                "in_progress",
                timestamp,
                timestamp,
                json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
            ),
        )
        self.conn.commit()
        return operation_id

    def update(
        self,
        operation_id: str,
        stage: str,
        *,
        status: str = "in_progress",
        details: dict[str, Any] | None = None,
    ) -> None:
        current = self.conn.execute(
            "SELECT details_json FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        merged: dict[str, Any] = {}
        if current:
            try:
                parsed = json.loads(current[0])
                if isinstance(parsed, dict):
                    merged.update(parsed)
            except Exception:
                pass
        if details:
            merged.update(details)
        self.conn.execute(
            "UPDATE operations SET stage=?, status=?, updated_utc=?, details_json=? WHERE operation_id=?",
            (stage, status, now_utc().isoformat(), json.dumps(merged, ensure_ascii=False, sort_keys=True), operation_id),
        )
        self.conn.commit()

    def complete(self, operation_id: str, details: dict[str, Any] | None = None) -> None:
        self.update(operation_id, "completed", status="completed", details=details)

    def fail(self, operation_id: str, stage: str, error: str, details: dict[str, Any] | None = None) -> None:
        payload = dict(details or {})
        payload["error"] = str(error)
        self.update(operation_id, stage, status="failed", details=payload)


    def reconcile_prior_incomplete(self, limit: int = 10000) -> dict[str, Any]:
        """Classify crash-left operations without touching media files.

        The journal is updated so diagnostics distinguish a completed rename from a
        safe-to-retry source, a path conflict, or missing evidence. No rename, tag write,
        deletion, or repair is performed here.
        """
        rows = self.conn.execute(
            "SELECT operation_id, run_id, source_path, target_path, stage, status FROM operations "
            "WHERE status NOT IN ('completed', 'failed', 'retryable', 'conflict', 'missing') "
            "AND run_id != ? ORDER BY updated_utc ASC LIMIT ?",
            (self.run_id, max(1, int(limit))),
        ).fetchall()
        counts = {"checked": 0, "completed_after_crash": 0, "retryable": 0, "conflict": 0, "missing": 0}
        for operation_id, prior_run_id, source_raw, target_raw, prior_stage, _status in rows:
            source = Path(source_raw)
            target = Path(target_raw)
            same = _same_path(source, target)
            source_exists = source.exists()
            target_exists = target.exists()
            counts["checked"] += 1
            details = {
                "reconciled_by_run_id": self.run_id,
                "prior_run_id": prior_run_id,
                "prior_stage": prior_stage,
                "source_exists": source_exists,
                "target_exists": target_exists,
                "same_source_target": same,
            }
            if same and source_exists:
                if prior_stage in {"metadata_verified", "rename_verified", "completed"}:
                    self.update(operation_id, "reconciled_same_path_durable_stage", status="completed", details=details)
                    counts["completed_after_crash"] += 1
                else:
                    self.update(operation_id, "reconciled_same_path_safe_to_retry", status="retryable", details=details)
                    counts["retryable"] += 1
            elif target_exists and not source_exists:
                # Target presence proves a rename reached its durable final path, but does
                # not claim metadata correctness beyond the prior recorded stage.
                self.update(operation_id, "reconciled_target_present", status="completed", details=details)
                counts["completed_after_crash"] += 1
            elif source_exists and not target_exists:
                self.update(operation_id, "reconciled_safe_to_retry", status="retryable", details=details)
                counts["retryable"] += 1
            elif source_exists and target_exists:
                self.update(operation_id, "reconciled_path_conflict", status="conflict", details=details)
                counts["conflict"] += 1
            else:
                self.update(operation_id, "reconciled_paths_missing", status="missing", details=details)
                counts["missing"] += 1
        return {
            "schema": "MediaTaggerBot.operation_journal_reconciliation.v1",
            "run_id": self.run_id,
            "created_utc": now_utc().isoformat(),
            **counts,
            "media_files_mutated": False,
        }

    def close(self) -> None:
        try:
            self.conn.execute("PRAGMA optimize")
        except sqlite3.Error:
            pass
        self.conn.close()

    def __enter__(self) -> "OperationJournal":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()


def read_operation_journal_summary(path: Path, limit: int = 100) -> dict[str, Any]:
    """Read-only compact summary suitable for diagnostics."""
    summary: dict[str, Any] = {
        "schema": "MediaTaggerBot.operation_journal_summary.v1",
        "created_utc": now_utc().isoformat(),
        "path": str(path),
        "exists": path.exists(),
        "status_counts": {},
        "stage_counts": {},
        "schema_version": None,
        "quick_check": "not_run",
        "incomplete_operations": [],
    }
    if not path.exists():
        return summary
    encoded_path = quote(path.as_posix(), safe="/:")
    uri = f"file:{encoded_path}?mode=ro"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        quick = conn.execute("PRAGMA quick_check(1)").fetchone()
        summary["quick_check"] = str(quick[0]) if quick else "no_result"
        summary["schema_version"] = int(conn.execute("PRAGMA user_version").fetchone()[0])
        status_rows = conn.execute("SELECT status, COUNT(*) FROM operations GROUP BY status").fetchall()
        stage_rows = conn.execute("SELECT stage, COUNT(*) FROM operations GROUP BY stage").fetchall()
        incomplete = conn.execute(
            "SELECT operation_id, run_id, source_path, target_path, stage, status, created_utc, updated_utc FROM operations WHERE status != 'completed' ORDER BY updated_utc DESC LIMIT ?",
            (max(1, int(limit)),),
        ).fetchall()
        summary["status_counts"] = {str(key): int(value) for key, value in status_rows}
        summary["stage_counts"] = {str(key): int(value) for key, value in stage_rows}
        summary["incomplete_operations"] = [
            {
                "operation_id": row[0],
                "run_id": row[1],
                "source_path": row[2],
                "target_path": row[3],
                "stage": row[4],
                "status": row[5],
                "created_utc": row[6],
                "updated_utc": row[7],
            }
            for row in incomplete
        ]
    except Exception as exc:
        summary["read_error"] = str(exc)
    finally:
        if conn is not None:
            conn.close()
    return summary


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve() == right.resolve()
    except OSError:
        return str(left).casefold() == str(right).casefold()
