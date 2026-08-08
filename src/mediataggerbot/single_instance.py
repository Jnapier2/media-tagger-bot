from __future__ import annotations

import hashlib
import json
import os
import socket
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .utils import write_json_atomic


class LockBusyError(RuntimeError):
    """Raised when a live same-computer owner still holds the local lock."""

    def __init__(self, message: str, status: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.status = dict(status or {})


class SingleInstanceLock:
    """Atomic owner-aware lock that remains safe during very long runs."""

    def __init__(
        self,
        lock_path: Path,
        stale_after_seconds: int = 24 * 60 * 60,
        heartbeat_seconds: int = 30,
        dead_owner_grace_seconds: int = 30,
        run_id: str = "",
        mode: str = "",
        computer_id: str = "PC-UNKNOWN",
        legacy_lock_paths: list[Path] | None = None,
    ) -> None:
        self.lock_path = lock_path
        self.stale_after_seconds = max(60, int(stale_after_seconds))
        self.heartbeat_seconds = max(5, int(heartbeat_seconds))
        self.dead_owner_grace_seconds = max(5, min(600, int(dead_owner_grace_seconds)))
        self.acquired = False
        self.owner_token = str(uuid.uuid4())
        self.hostname = socket.gethostname()
        self.created_epoch = time.time()
        self.process_start_epoch = _process_start_epoch(os.getpid())
        self.last_heartbeat_monotonic = 0.0
        self.run_id = str(run_id or "")
        self.mode = str(mode or "")
        self.computer_id = str(computer_id or "PC-UNKNOWN")
        self.legacy_lock_paths = [Path(value) for value in (legacy_lock_paths or [])]
        self.recovery_events: list[dict[str, Any]] = []

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        # Migration bridge: a legacy fixed-name or older profile lock blocks only
        # when it belongs to a live process on this same computer. Foreign-computer
        # locks are advisory-only and are never deleted or treated as ownership.
        for legacy_path in self.legacy_lock_paths:
            if legacy_path == self.lock_path or not legacy_path.exists():
                continue
            legacy_payload = read_lock_payload(legacy_path)
            legacy_status = read_lock_status(
                legacy_path,
                self.stale_after_seconds,
                self.dead_owner_grace_seconds,
            )
            if legacy_status.get("active") and legacy_status.get("same_host"):
                raise LockBusyError(
                    "Another MediaTaggerBot run appears active on this computer via a legacy lock: "
                    f"pid={legacy_status.get('pid')} lock={legacy_path}",
                    legacy_status,
                )
            if legacy_status.get("stale") and legacy_status.get("same_host"):
                self._archive_stale_lock(legacy_path, legacy_status, legacy_payload, "legacy_same_host_stale")

        for _attempt in range(3):
            payload = self._payload()
            try:
                fd = os.open(str(self.lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                    json.dump(payload, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                self.acquired = True
                self.last_heartbeat_monotonic = time.monotonic()
                return
            except FileExistsError:
                existing_payload = read_lock_payload(self.lock_path)
                status = read_lock_status(
                    self.lock_path,
                    self.stale_after_seconds,
                    self.dead_owner_grace_seconds,
                )
                if status.get("active"):
                    raise LockBusyError(
                        "Another MediaTaggerBot run appears active: "
                        f"pid={status.get('pid')} host={status.get('hostname')} lock={self.lock_path}",
                        status,
                    )
                if status.get("reason") == "foreign_host_lock_advisory_only":
                    # A synchronized/copied project may contain another computer's
                    # advisory lock. Never delete or wait on it; use a privacy-safe
                    # same-host fallback lock beside it.
                    self.lock_path = _foreign_collision_fallback_path(self.lock_path, self.hostname)
                    continue
                if not status.get("stale"):
                    raise LockBusyError(
                        "A MediaTaggerBot lock exists but is not safe to recover automatically: "
                        f"reason={status.get('reason')} lock={self.lock_path}",
                        status,
                    )
                self._archive_stale_lock(self.lock_path, status, existing_payload, "preferred_lock_stale")
                continue
        raise RuntimeError(f"Could not acquire MediaTaggerBot lock after stale-lock recovery: {self.lock_path}")

    def _archive_stale_lock(
        self,
        path: Path,
        status: dict[str, Any],
        expected_payload: dict[str, Any],
        recovery_kind: str,
    ) -> None:
        """Preserve and remove only a verified stale local lock.

        The owner token/PID/heartbeat are re-read immediately before the atomic move
        so a newly refreshed lock can never be mistaken for the stale one inspected.
        """
        current_payload = read_lock_payload(path)
        identity_fields = ("owner_token", "pid", "heartbeat_epoch", "run_id")
        if any(current_payload.get(key) != expected_payload.get(key) for key in identity_fields):
            refreshed = read_lock_status(path, self.stale_after_seconds, self.dead_owner_grace_seconds)
            raise LockBusyError(
                f"Lock changed while stale recovery was being prepared: {path}",
                refreshed,
            )
        recovery_dir = path.parent / "lock_recovery"
        recovery_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
        token = uuid.uuid4().hex[:8]
        archived_lock = recovery_dir / f"{path.name}.{timestamp}.{token}.json"
        os.replace(path, archived_lock)
        event = {
            "schema": "MediaTaggerBot.stale_lock_recovery.v1",
            "recovered_utc": datetime.now(timezone.utc).isoformat(),
            "recovery_kind": recovery_kind,
            "original_lock_path": str(path),
            "archived_lock_path": str(archived_lock),
            "prior_run_id": str(expected_payload.get("run_id") or ""),
            "prior_mode": str(expected_payload.get("mode") or ""),
            "prior_pid": expected_payload.get("pid"),
            "prior_hostname": str(expected_payload.get("hostname") or ""),
            "prior_computer_id": str(expected_payload.get("computer_id") or "PC-UNKNOWN"),
            "prior_heartbeat_epoch": expected_payload.get("heartbeat_epoch"),
            "prior_process_start_epoch": expected_payload.get("process_start_epoch"),
            "heartbeat_age_seconds": status.get("heartbeat_age_seconds"),
            "stale_reason": status.get("reason"),
            "recovery_eligible": bool(status.get("recovery_eligible")),
            "recovered_by_run_id": self.run_id,
            "recovered_by_mode": self.mode,
            "recovered_by_pid": os.getpid(),
            "recovered_by_hostname": self.hostname,
            "recovered_by_computer_id": self.computer_id,
            "media_files_mutated_by_recovery": False,
        }
        receipt = recovery_dir / f"stale_lock_recovery_{timestamp}_{token}.json"
        write_json_atomic(receipt, event)
        write_json_atomic(path.parent / "last_stale_lock_recovery.json", {**event, "receipt_path": str(receipt)})
        event["receipt_path"] = str(receipt)
        self.recovery_events.append(event)

    def heartbeat(self, force: bool = False) -> None:
        if not self.acquired:
            return
        now_mono = time.monotonic()
        if not force and now_mono - self.last_heartbeat_monotonic < self.heartbeat_seconds:
            return
        current = read_lock_payload(self.lock_path)
        if current.get("owner_token") != self.owner_token:
            raise RuntimeError("MediaTaggerBot lock ownership changed unexpectedly; stopping to prevent duplicate mutation.")
        write_json_atomic(self.lock_path, self._payload())
        self.last_heartbeat_monotonic = now_mono

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            current = read_lock_payload(self.lock_path)
            if current.get("owner_token") == self.owner_token:
                self.lock_path.unlink(missing_ok=True)
        finally:
            self.acquired = False

    def _payload(self) -> dict[str, Any]:
        return {
            "schema": "MediaTaggerBot.single_instance_lock.v3",
            "pid": os.getpid(),
            "process_start_epoch": self.process_start_epoch,
            "hostname": self.hostname,
            "owner_token": self.owner_token,
            "run_id": self.run_id,
            "mode": self.mode,
            "computer_id": self.computer_id,
            "created_epoch": self.created_epoch,
            "heartbeat_epoch": time.time(),
            "heartbeat_seconds": self.heartbeat_seconds,
            "dead_owner_grace_seconds": self.dead_owner_grace_seconds,
        }

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def read_lock_status(
    lock_path: Path,
    stale_after_seconds: int = 24 * 60 * 60,
    dead_owner_grace_seconds: int = 30,
) -> dict[str, Any]:
    payload = read_lock_payload(lock_path)
    dead_grace = max(5, min(600, int(dead_owner_grace_seconds)))
    status: dict[str, Any] = {
        "path": str(lock_path),
        "exists": lock_path.exists(),
        "active": False,
        "stale": False,
        "recovery_eligible": False,
        "pid": payload.get("pid"),
        "hostname": payload.get("hostname"),
        "computer_id": payload.get("computer_id", "PC-UNKNOWN"),
        "run_id": payload.get("run_id", ""),
        "mode": payload.get("mode", ""),
        "same_host": False,
        "pid_alive": False,
        "heartbeat_age_seconds": None,
        "dead_owner_grace_seconds": dead_grace,
        "expected_process_start_epoch": payload.get("process_start_epoch"),
        "observed_process_start_epoch": None,
        "process_identity_match": None,
        "reason": "missing",
    }
    if not lock_path.exists():
        return status
    payload_valid = bool(payload.get("owner_token") and payload.get("pid") and payload.get("hostname"))
    if not payload_valid:
        try:
            malformed_age = max(0.0, time.time() - lock_path.stat().st_mtime)
        except OSError:
            malformed_age = 0.0
        malformed_grace_seconds = 10.0
        status["heartbeat_age_seconds"] = round(malformed_age, 3)
        if malformed_age < malformed_grace_seconds:
            status.update({
                "active": True,
                "stale": False,
                "reason": "malformed_lock_within_short_creation_grace",
            })
        else:
            status.update({
                "active": False,
                "stale": True,
                "recovery_eligible": True,
                "reason": "malformed_lock_grace_expired",
            })
        return status

    heartbeat = _safe_float(payload.get("heartbeat_epoch"))
    if heartbeat is None:
        try:
            heartbeat = lock_path.stat().st_mtime
        except OSError:
            heartbeat = 0.0
    age = max(0.0, time.time() - heartbeat)
    status["heartbeat_age_seconds"] = round(age, 3)
    same_host = str(payload.get("hostname") or "").casefold() == socket.gethostname().casefold()
    status["same_host"] = same_host
    if not same_host:
        status.update({
            "active": False,
            "stale": False,
            "recovery_eligible": False,
            "reason": "foreign_host_lock_advisory_only",
        })
        return status

    pid = _safe_int(payload.get("pid"))
    stale_limit = max(60, int(stale_after_seconds))
    pid_alive = bool(pid and _pid_alive(pid))
    status["pid_alive"] = pid_alive
    expected_start = _safe_float(payload.get("process_start_epoch"))
    observed_start = _process_start_epoch(pid) if pid_alive and pid is not None else None
    status["observed_process_start_epoch"] = observed_start

    if expected_start is not None and observed_start is not None:
        identity_match = abs(expected_start - observed_start) <= 2.0
        status["process_identity_match"] = identity_match
        if identity_match:
            status.update({"active": True, "stale": False, "reason": "owner_pid_and_start_time_match"})
        elif age < dead_grace:
            status.update({
                "active": True,
                "stale": False,
                "reason": "pid_reused_within_short_recovery_grace",
            })
        else:
            status.update({
                "active": False,
                "stale": True,
                "recovery_eligible": True,
                "reason": "pid_reused_recovery_eligible",
            })
    elif pid_alive:
        # A process that is still alive always wins over heartbeat age when its
        # start identity cannot be observed. Recovering this lock could create a
        # second writer beside a live but slow/hung owner.
        status.update({
            "active": True,
            "stale": False,
            "recovery_eligible": False,
            "reason": "owner_pid_alive_identity_unavailable",
        })
    elif age < dead_grace:
        status.update({
            "active": True,
            "stale": False,
            "reason": "owner_pid_not_alive_within_short_recovery_grace",
        })
    else:
        status.update({
            "active": False,
            "stale": True,
            "recovery_eligible": True,
            "reason": "owner_pid_not_alive_recovery_eligible",
        })
    return status


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_start_epoch(pid: int | None) -> float | None:
    if not pid or pid <= 0:
        return None
    if os.name == "nt":
        return _windows_process_start_epoch(pid)
    if sys.platform.startswith("linux"):
        return _linux_process_start_epoch(pid)
    return None


def _linux_process_start_epoch(pid: int) -> float | None:
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing = stat_text.rfind(")")
        if closing < 0:
            return None
        fields = stat_text[closing + 2 :].split()
        start_ticks = int(fields[19])  # proc field 22; fields starts at field 3
        clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        boot_epoch = None
        for line in Path("/proc/stat").read_text(encoding="utf-8").splitlines():
            if line.startswith("btime "):
                boot_epoch = int(line.split()[1])
                break
        if boot_epoch is None or clock_ticks <= 0:
            return None
        return float(boot_epoch) + (start_ticks / clock_ticks)
    except (OSError, ValueError, IndexError):
        return None


def _windows_process_start_epoch(pid: int) -> float | None:
    try:
        import ctypes
        from ctypes import wintypes

        class FILETIME(ctypes.Structure):
            _fields_ = [("dwLowDateTime", wintypes.DWORD), ("dwHighDateTime", wintypes.DWORD)]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
            ctypes.POINTER(FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            handle = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
        if not handle:
            return None
        try:
            created, exited, kernel, user = FILETIME(), FILETIME(), FILETIME(), FILETIME()
            if not kernel32.GetProcessTimes(handle, created, exited, kernel, user):
                return None
            ticks = (created.dwHighDateTime << 32) | created.dwLowDateTime
            return (ticks - 116444736000000000) / 10_000_000.0
        finally:
            kernel32.CloseHandle(handle)
    except (AttributeError, OSError, ValueError):
        return None


def _foreign_collision_fallback_path(path: Path, hostname: str) -> Path:
    digest = hashlib.sha256(str(hostname or "unknown").casefold().encode("utf-8")).hexdigest()[:8]
    if path.suffix:
        name = f"{path.stem}.host-{digest}{path.suffix}"
    else:
        name = f"{path.name}.host-{digest}.lock"
    return path.with_name(name)


def read_lock_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def _safe_int(value: object) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _safe_float(value: object) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def find_active_same_host_lock(
    state_dir: Path,
    preferred_path: Path,
    stale_after_seconds: int = 24 * 60 * 60,
    dead_owner_grace_seconds: int = 30,
) -> tuple[Path, dict[str, Any]]:
    """Select an active lock owned by this computer only.

    The preferred profile-scoped lock is checked first. Other project-local lock
    files are considered solely as a migration/recovery bridge. Foreign-computer
    locks are ignored and never deleted or treated as ownership.
    """
    candidates = [preferred_path]
    try:
        candidates.extend(
            path for path in sorted(state_dir.glob("mediataggerbot*.lock")) if path != preferred_path
        )
    except OSError:
        pass
    stale_same_host: tuple[Path, dict[str, Any]] | None = None
    for path in candidates:
        status = read_lock_status(path, stale_after_seconds, dead_owner_grace_seconds)
        if status.get("active") and status.get("same_host"):
            return path, status
        if status.get("stale") and status.get("same_host") and stale_same_host is None:
            stale_same_host = (path, status)
    if stale_same_host is not None:
        return stale_same_host
    return preferred_path, read_lock_status(preferred_path, stale_after_seconds, dead_owner_grace_seconds)
