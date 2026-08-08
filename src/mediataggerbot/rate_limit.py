from __future__ import annotations

import time
from collections.abc import Callable


class RateLimiter:
    def __init__(self, min_interval_seconds: float, stop_check: Callable[[], bool] | None = None) -> None:
        self.min_interval = float(min_interval_seconds)
        self.last_call_monotonic = 0.0
        self.stop_check = stop_check

    def wait(self) -> bool:
        """Wait for the provider interval, returning False when a graceful stop interrupts it."""
        now = time.monotonic()
        elapsed = now - self.last_call_monotonic
        remaining = self.min_interval - elapsed
        while remaining > 0:
            if self.stop_check and self.stop_check():
                return False
            sleep_for = min(0.25, remaining)
            time.sleep(sleep_for)
            remaining -= sleep_for
        if self.stop_check and self.stop_check():
            return False
        self.last_call_monotonic = time.monotonic()
        return True
