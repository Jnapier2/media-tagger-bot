from __future__ import annotations

import time


class RateLimiter:
    def __init__(self, min_interval_seconds: float) -> None:
        self.min_interval = float(min_interval_seconds)
        self.last_call_monotonic = 0.0

    def wait(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_call_monotonic
        remaining = self.min_interval - elapsed
        if remaining > 0:
            time.sleep(remaining)
        self.last_call_monotonic = time.monotonic()
