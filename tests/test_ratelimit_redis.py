"""Redis-backed rate limits: shared across API processes, refused attempts
not counted, retry-after from the oldest attempt, in-process fallback when
Redis fails."""
import asyncio

import fakeredis
import pytest

from backend.app.core.errors import AppError
from backend.app.core.ratelimit import RedisSlidingWindowLimiter, build_limiters, LIMITS, SlidingWindowLimiter


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t


def test_limit_is_shared_between_processes_and_slides():
    async def go():
        redis, clock = fakeredis.FakeAsyncRedis(), Clock()
        p1 = RedisSlidingWindowLimiter(redis, "login_email", 3, 60, clock=clock)
        p2 = RedisSlidingWindowLimiter(redis, "login_email", 3, 60, clock=clock)   # another API process
        results = [(await p.hit("a@x"))[0] for p in (p1, p2, p1, p2)]
        assert results == [True, True, True, False]
        assert (await p2.hit("b@x"))[0]                                         # keys are independent
        clock.t += 30
        allowed, retry = await p1.hit("a@x")
        assert not allowed and 30 <= retry <= 31            # oldest attempt leaves the window in 30 s
        assert await redis.zcard("rl:login_email:a@x") == 3  # refused attempts are not stored
        await p1.hit("login_email:a@x")                       # an already-namespaced key is not doubled
        assert await redis.exists("rl:login_email:login_email:a@x") == 0
        clock.t += 31
        assert (await p2.hit("a@x"))[0]
        assert 0 < await redis.pttl("rl:login_email:a@x") <= 60_000                 # keys expire
        await p1.reset("a@x")
        assert await redis.exists("rl:login_email:a@x") == 0
        with pytest.raises(AppError) as e:
            for _ in range(5):
                await p1.check("c@x")
        assert e.value.status == 429 and int(e.value.headers["Retry-After"]) >= 1
    asyncio.run(go())


class BrokenRedis:
    calls = 0

    def pipeline(self, transaction=True):
        BrokenRedis.calls += 1
        raise ConnectionError("redis down")


def test_falls_back_to_in_process_limits_when_redis_fails_and_backs_off(monkeypatch):
    async def go():
        lim = RedisSlidingWindowLimiter(BrokenRedis(), "login", 2, 60)
        assert [(await lim.hit("ip"))[0] for _ in range(3)] == [True, True, False]
        assert BrokenRedis.calls == 1                 # one failure, then Redis is skipped for a while
        lim._skip_redis_until = 0                     # retry window over: Redis is tried again
        await lim.hit("other")
        assert BrokenRedis.calls == 2
    asyncio.run(go())


def test_build_limiters():
    assert all(isinstance(v, SlidingWindowLimiter) for v in build_limiters().values())
    shared = build_limiters(fakeredis.FakeAsyncRedis())
    assert set(shared) == set(LIMITS) and all(isinstance(v, RedisSlidingWindowLimiter) for v in shared.values())
    assert (shared["login_email"].limit, shared["login_email"].window_sec) == (10, 900)
