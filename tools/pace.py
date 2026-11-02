"""A shared request budget for tools that call EODHD from a thread pool.

The first daily backfill used eight workers with no budget and burst to about 24 calls/s;
EODHD answered 669 of 3,075 calls with HTTP 429. The client now waits out a 429, but the
cheaper fix is not to earn one: this spaces calls evenly across all workers.
"""

from __future__ import annotations

import threading
import time


class RateLimiter:
    """At most ``per_second`` calls per second, shared by every thread using it."""

    def __init__(self, per_second: float) -> None:
        self.interval = 1.0 / per_second if per_second > 0 else 0.0
        self._lock = threading.Lock()
        self._next_at = time.monotonic()

    def wait(self) -> None:
        if self.interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next_at)
            self._next_at = slot + self.interval
        delay = slot - time.monotonic()
        if delay > 0:
            time.sleep(delay)
