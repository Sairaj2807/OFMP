"""Sliding-window rate limiting.

Two implementations of one async interface (hit / check / reset):

- SlidingWindowLimiter: in process. Correct for a single API process, and
  the fallback when Redis is unavailable.
- RedisSlidingWindowLimiter: shared by every API process through Redis, so
  "10 login attempts per account per 15 minutes" holds no matter which
  process a request lands on. One sorted set per key (member per attempt,
  scored by time), updated in one MULTI transaction: trim the window, add
  this attempt, count. An attempt over the limit is removed again, so
  refused attempts do not extend the lockout (same as the in-process one).
  Timestamps come from the API host's clock; NTP-level skew between hosts
  only blurs the window edge by that skew.
  If Redis fails, the limiter falls back to its in-process twin (logged and
  counted in ofmp_ratelimit_fallbacks_total): limits stay enforced per
  process rather than being dropped. After a failure it skips Redis for
  RETRY_AFTER_FAILURE_SEC, so an outage costs one timeout, not one per request.
"""
import logging
import secrets
import time
from collections import defaultdict, deque
from typing import Callable, Optional

from . import metrics
from .errors import AppError

log = logging.getLogger(__name__)

MAX_KEY_LENGTH = 200
RETRY_AFTER_FAILURE_SEC = 5.0


class _Limiter:
    limit: int
    window_sec: float

    async def hit(self, key: str) -> tuple:
        raise NotImplementedError

    async def check(self, key: str) -> None:
        """Raise RATE_LIMITED (429) when over the limit."""
        allowed, retry_after = await self.hit(key)
        if not allowed:
            raise AppError("RATE_LIMITED", "too many requests, try again later", 429,
                           headers={"Retry-After": str(retry_after)})


class SlidingWindowLimiter(_Limiter):
    def __init__(self, limit: int, window_sec: float, clock: Callable[[], float] = time.monotonic,
                 max_keys: int = 100_000):
        self.limit, self.window_sec, self._clock, self.max_keys = limit, window_sec, clock, max_keys
        self._hits = defaultdict(deque)

    def hit_now(self, key: str) -> tuple:
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

    async def hit(self, key: str) -> tuple:
        return self.hit_now(key)

    async def reset(self, key: str) -> None:
        self._hits.pop(key, None)


class RedisSlidingWindowLimiter(_Limiter):
    def __init__(self, redis, name: str, limit: int, window_sec: float,
                 clock: Callable[[], float] = time.time, fallback: Optional[SlidingWindowLimiter] = None):
        self.redis, self.name = redis, name
        self.limit, self.window_sec, self._clock = limit, window_sec, clock
        self.fallback = fallback or SlidingWindowLimiter(limit, window_sec)
        self._skip_redis_until = 0.0

    def _key(self, key: str) -> str:
        key = key.removeprefix(f"{self.name}:")        # some callers already namespace their keys
        return f"rl:{self.name}:{key[:MAX_KEY_LENGTH]}"

    async def hit(self, key: str) -> tuple:
        if time.monotonic() < self._skip_redis_until:
            metrics.RATELIMIT_FALLBACKS.inc()
            return self.fallback.hit_now(key)
        rkey = self._key(key)
        now_ms = int(self._clock() * 1000)
        window_ms = int(self.window_sec * 1000)
        member = f"{now_ms}-{secrets.token_hex(4)}"
        try:
            async with self.redis.pipeline(transaction=True) as pipe:
                pipe.zremrangebyscore(rkey, "-inf", now_ms - window_ms)
                pipe.zadd(rkey, {member: now_ms})
                pipe.zcard(rkey)
                pipe.zrange(rkey, 0, 0, withscores=True)
                pipe.pexpire(rkey, window_ms)
                _, _, count, oldest, _ = await pipe.execute()
            if count <= self.limit:
                return True, 0
            await self.redis.zrem(rkey, member)      # refused attempts are not counted
            oldest_ms = int(oldest[0][1]) if oldest else now_ms
            return False, max(1, (window_ms - (now_ms - oldest_ms)) // 1000 + 1)
        except Exception as e:                       # Redis down: keep limiting, per process
            self._skip_redis_until = time.monotonic() + RETRY_AFTER_FAILURE_SEC
            metrics.RATELIMIT_FALLBACKS.inc()
            log.warning("rate limiter %s: redis unavailable (%r), using in-process fallback", self.name, e)
            return self.fallback.hit_now(key)

    async def reset(self, key: str) -> None:
        try:
            await self.redis.delete(self._key(key))
        except Exception:
            pass
        await self.fallback.reset(key)


LIMITS = {
    # name: (limit, window seconds)
    "login": (20, 60),               # per IP
    "login_email": (10, 15 * 60),    # per account
    "register": (10, 3600),
    "refresh": (60, 60),
    "email": (5, 3600),
    "token": (20, 3600),
    "alert_test": (10, 3600),        # webhook test deliveries, per user
    "alert_delivery": (20, 60),      # alert deliveries, per user
}


def build_limiters(redis=None, limits: dict = LIMITS) -> dict:
    """In-process limiters, or Redis-backed ones (with in-process fallback) when `redis` is given."""
    if redis is None:
        return {name: SlidingWindowLimiter(limit, window) for name, (limit, window) in limits.items()}
    return {name: RedisSlidingWindowLimiter(redis, name, limit, window) for name, (limit, window) in limits.items()}
