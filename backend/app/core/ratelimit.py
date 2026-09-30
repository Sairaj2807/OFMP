"""Sliding-window rate limiting.

In-process implementation: correct for a single API process. With several
API processes behind a load balancer this must move to Redis (same
interface); limits are per process until then."""
import time
from collections import defaultdict, deque
from typing import Callable

from .errors import AppError


class SlidingWindowLimiter:
    def __init__(self, limit: int, window_sec: float, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 100_000):
        self.limit, self.window_sec, self._clock, self.max_keys = limit, window_sec, clock, max_keys
        self._hits = defaultdict(deque)

    def hit(self, key: str) -> tuple:
        """Record one attempt. (allowed, retry_after_sec)."""
        now = self._clock()
        q = self._hits[key]
        while q and now - q[0] >= self.window_sec:
            q.popleft()
        if len(q) >= self.limit:
            return False, max(1, int(self.window_sec - (now - q[0])) + 1)
        q.append(now)
        if len(self._hits) > self.max_keys:          # bound memory under key-spraying
            for k in list(self._hits)[: len(self._hits) // 10]:
                del self._hits[k]
        return True, 0

    def check(self, key: str) -> None:
        """Raise RATE_LIMITED (429) when over the limit."""
        allowed, retry_after = self.hit(key)
        if not allowed:
            raise AppError("RATE_LIMITED", "too many requests, try again later", 429,
                           headers={"Retry-After": str(retry_after)})

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)
