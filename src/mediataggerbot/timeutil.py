from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def timestamp_for_filename(dt: datetime | None = None) -> str:
    dt = dt or now_utc()
    return dt.astimezone(timezone.utc).strftime("%Y%m%d_%H%M%S_UTC")


def local_timestamp(tz_name: str = "America/Chicago", dt: datetime | None = None) -> str:
    dt = dt or now_utc()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M:%S %Z%z")
