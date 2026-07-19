from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from .utils import write_json_atomic


class SingleInstanceLock:
    """Atomic owner-aware lock that remains safe during very long runs."""

    def __init__(
        self,
        lock_path: Path,
        stale_after_seconds: int = 24 * 60 * 60,
        heartbeat_seconds: int = 30,
        run_id: str = "",
        mode: str = "",
    ) -> None:
        self.lock_path = lock_path
        self.stale_after_seconds = max(60, int(stale_after_seconds))
        self.heartbeat_seconds = max(5, int(heartbeat_seconds))
        self.acquired = False
        self.owner_token = str(uuid.uuid4())
        self.hostname = socket.gethostname()
        self.created_epoch = time.time()
        self.process_start_epoch = _process_start_epoch(os.getpid())
        self.last_heartbeat_monotonic = 0.0
        self.run_id = str(run_id or "")
        self.mode = str(mode or "")

    def acquire(self) -> None:
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
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
                status = read_lock_status(self.lock_path, self.stale_after_seconds)
                if status.get("active"):
                    raise RuntimeError(
                        "Another MediaTaggerBot run appears active: "
                        f"pid={status.get('pid')} host={status.get('hostname')} lock={self.lock_path}"
                    )
                try:
                    self.lock_path.unlink()
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    raise RuntimeError(f"Stale lock exists and could not be removed: {self.lock_path}: {exc}") from exc
        raise RuntimeError(f"Could not acquire MediaTaggerBot lock after stale-lock recovery: {self.lock_path}")

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
            "created_epoch": self.created_epoch,
            "heartbeat_epoch": time.time(),
        }

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


def read_lock_status(lock_path: Path, stale_after_seconds: int = 24 * 60 * 60) -> dict[str, Any]:
    payload = read_lock_payload(lock_path)
    status: dict[str, Any] = {
        "path": str(lock_path),
        "exists": lock_path.exists(),
        "active": False,
        "stale": False,
        "pid": payload.get("pid"),
        "hostname": payload.get("hostname"),
        "heartbeat_age_seconds": None,
        "expected_process_start_epoch": payload.get("process_start_epoch"),
        "observed_process_start_epoch": None,
        "process_identity_match": None,
        "reason": "missing",
    }
    if not lock_path.exists():
        return status
    heartbeat = _safe_float(payload.get("heartbeat_epoch"))
    if heartbeat is None:
        try:
            heartbeat = lock_path.stat().st_mtime
        except OSError:
            heartbeat = 0.0
    age = max(0.0, time.time() - heartbeat)
    status["heartbeat_age_seconds"] = round(age, 3)
    same_host = str(payload.get("hostname") or "") == socket.gethostname()
    pid = _safe_int(payload.get("pid"))
    stale_limit = max(60, int(stale_after_seconds))
    pid_alive = bool(same_host and pid and _pid_alive(pid))
    expected_start = _safe_float(payload.get("process_start_epoch"))
    observed_start = _process_start_epoch(pid) if pid_alive and pid is not None else None
    status["observed_process_start_epoch"] = observed_start

    if expected_start is not None and observed_start is not None:
        identity_match = abs(expected_start - observed_start) <= 2.0
        status["process_identity_match"] = identity_match
        if identity_match:
            status.update({"active": True, "stale": False, "reason": "owner_pid_and_start_time_match"})
        elif age < stale_limit:
            status.update({"active": True, "stale": False, "reason": "recent_heartbeat_but_pid_was_reused"})
        else:
            status.update({"active": False, "stale": True, "reason": "pid_reused_and_heartbeat_stale"})
    elif pid_alive and age < stale_limit:
        status.update({"active": True, "stale": False, "reason": "owner_pid_alive_identity_unavailable"})
    elif pid_alive:
        # Legacy locks did not record process creation time. A different process can
        # eventually reuse the PID, so an old heartbeat must be allowed to expire.
        status.update({"active": False, "stale": True, "reason": "legacy_lock_pid_alive_but_heartbeat_stale"})
    elif age < stale_limit:
        status.update({"active": True, "stale": False, "reason": "recent_heartbeat_owner_unverifiable"})
    else:
        status.update({"active": False, "stale": True, "reason": "owner_not_alive_and_heartbeat_stale"})
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
