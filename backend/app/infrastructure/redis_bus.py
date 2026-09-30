"""Redis transport between the ingest worker and the API process.

    md:ticks:<token>   stream   normalized ticks (field "tick": MarketTick JSON), capped
    md:desired         string   JSON list of the instruments the API wants
    md:control         stream   change notifications for md:desired
    md:health          string   ingest worker's ProviderHealth JSON, expires if the worker dies

The worker owns the broker connection; any number of API processes can
consume ticks, and an API restart does not drop the broker connection.
Redis is transport only, never the source of truth."""
import json
from typing import Optional

from backend.app.domain.market_data import InstrumentRef, MarketTick

TICK_STREAM = "md:ticks:{token}"
DESIRED_KEY = "md:desired"
CONTROL_STREAM = "md:control"
HEALTH_KEY = "md:health"


class RedisMarketDataBus:
    def __init__(self, redis, tick_stream_maxlen: int = 200_000, health_ttl_sec: int = 10):
        self.redis = redis
        self.tick_stream_maxlen = tick_stream_maxlen
        self.health_ttl_sec = health_ttl_sec

    # -- ticks ----------------------------------------------------------------

    async def publish_tick(self, tick: MarketTick) -> None:
        await self.redis.xadd(TICK_STREAM.format(token=tick.token),
                              {"tick": json.dumps(tick.to_dict(), separators=(",", ":"))},
                              maxlen=self.tick_stream_maxlen, approximate=True)

    async def latest_tick_id(self, token: str) -> str:
        """Id of the newest tick in the stream ("0-0" if empty): reading from
        here returns only ticks published after this call."""
        return await self._latest_id(TICK_STREAM.format(token=token))

    async def read_ticks(self, token: str, last_id: str, block_ms: int = 1000,
                         count: int = 1000) -> tuple:
        """(new last_id, [MarketTick, ...]) for ticks after last_id."""
        stream = TICK_STREAM.format(token=token)
        resp = await self.redis.xread({stream: last_id}, block=block_ms, count=count)
        ticks = []
        for _stream, entries in resp or []:
            for entry_id, fields in entries:
                last_id = _decode(entry_id)
                raw = fields.get(b"tick") or fields.get("tick")
                ticks.append(MarketTick.from_dict(json.loads(raw)))
        return last_id, ticks

    # -- desired subscriptions (API -> worker) ---------------------------------

    async def set_desired(self, instruments: list) -> None:
        payload = json.dumps([i.__dict__ for i in instruments])
        await self.redis.set(DESIRED_KEY, payload)
        await self.redis.xadd(CONTROL_STREAM, {"desired": payload}, maxlen=1000, approximate=True)

    async def get_desired(self) -> list:
        raw = await self.redis.get(DESIRED_KEY)
        return [InstrumentRef(**d) for d in json.loads(raw)] if raw else []

    async def latest_control_id(self) -> str:
        return await self._latest_id(CONTROL_STREAM)

    async def wait_control(self, last_id: str, block_ms: int = 1000) -> tuple:
        """(new last_id, desired instruments or None if nothing changed)."""
        resp = await self.redis.xread({CONTROL_STREAM: last_id}, block=block_ms, count=100)
        desired = None
        for _stream, entries in resp or []:
            for entry_id, fields in entries:
                last_id = _decode(entry_id)
                raw = fields.get(b"desired") or fields.get("desired")
                desired = [InstrumentRef(**d) for d in json.loads(raw)]   # latest wins
        return last_id, desired

    # -- health (worker -> API) ---------------------------------------------------

    async def publish_health(self, health: dict) -> None:
        await self.redis.set(HEALTH_KEY, json.dumps(health), ex=self.health_ttl_sec)

    async def get_health(self) -> Optional[dict]:
        raw = await self.redis.get(HEALTH_KEY)
        return json.loads(raw) if raw else None


    async def _latest_id(self, stream: str) -> str:
        entries = await self.redis.xrevrange(stream, count=1)
        return _decode(entries[0][0]) if entries else "0-0"


def _decode(v) -> str:
    return v.decode() if isinstance(v, (bytes, bytearray)) else str(v)
