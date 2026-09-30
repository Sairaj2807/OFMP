# Market data

Anything about how ticks get from the broker into the order-flow engine.

## Layers

| Layer | Location | Job |
|---|---|---|
| Canonical model | `backend/app/domain/market_data/` | `MarketTick`, `DepthLevel`, `InstrumentRef`, `ProviderHealth`, `DataQualityEvent`, `TickQualityMonitor`. Provider-independent. |
| Provider interface | `backend/app/services/market_data/provider.py` | `MarketDataProvider`: `connect`, `subscribe`, `unsubscribe`, `disconnect`, `stream` |
| Angel One adapter | `backend/app/infrastructure/providers/angelone/` | Snap Quote binary parser plus the WebSocket 2.0 protocol |
| Supervision | `backend/app/services/market_data/runner.py` | `ProviderRunner`: reconnects, backoff, resubscribe, health, quality checks, consumer isolation |
| Feeds for the API | `backend/app/services/market_data/feeds.py` | `EmbeddedFeed` / `RedisFeed` |
| Raw archive | `backend/app/services/market_data/recorder.py` | Every normalized tick to `data/ticks/<date>/` |
| Transport | `backend/app/infrastructure/redis_bus.py` | Redis streams and keys between worker and API |
| Worker | `backend/app/workers/ingest.py` | Holds the broker connection in Redis mode |

The engine never sees a broker packet. It receives `MarketTick.to_engine_tick()`.

**Adding a broker** means writing one `MarketDataProvider` subclass (plus its parser). The runner,
feeds, recorder, worker and engine stay unchanged.

## `MarketTick`

Every packet is normalized into a `MarketTick`:
- provider, token, exchange segment and sequence number
- three timestamps (epoch ms):
  - `exchange_ts_ms`: the packet's exchange timestamp as reported by the provider
  - `last_trade_ts_ms`: exchange time of the last trade (Angel: whole seconds)
  - `received_ts_ms`: server receive time
- LTP, last traded quantity, cumulative volume, best-5 bids/asks
- OHLC, OI, total buy/sell quantity
- `extra` for provider-specific fields

Provider times are the provider's claim; they are not independently verified exchange time.
Receive-time minus exchange time therefore measures "provider → server" latency, not true exchange
latency.

The engine still buckets footprint candles on `last_trade_ts_ms`, exactly as before. The new parser
decodes Angel packets identically to the old `ws_ingest.py`: this was checked on 2,000 random packets
before the old module was removed.

## Ingestion modes (`INGEST_MODE`)

**`embedded`** (default, no Redis needed):

```
Angel WS → AngelOneProvider → ProviderRunner → EmbeddedFeed → engine
                                             └→ RawTickRecorder
```

The provider runs inside the API process, as before this phase.

**`redis`:**

```
worker:  Angel WS → AngelOneProvider → ProviderRunner → Redis md:ticks:<token>
                                                     └→ RawTickRecorder
API:     Redis md:ticks:<token> → RedisFeed → engine
```

- Start the worker with `python -m backend.app.workers.ingest`.
- The worker is the only process that talks to Angel One. It subscribes to what the API last asked
  for: the API writes `md:desired` and notifies on `md:control`.
- The worker publishes `md:health` every second, with a 10 s expiry. If the worker dies, the API
  reports "ingest worker not running".
- Restarting or redeploying the API does not drop the broker connection.
- Local Redis: `docker compose -f docker-compose.dev.yml up -d`.

After an API restart in Redis mode, the engine starts fresh from new ticks, the same as today.
Rebuilding the in-progress session from stored ticks is Phase 3 work.

## Failure handling (`ProviderRunner`)

- **Reconnect** with exponential backoff and full jitter: 1 s, 2 s, 4 s … capped at 30 s. The backoff
  resets once a connection has stayed up for 60 s.
- **Resubscribe** to the current instrument set on every reconnect. Subscription changes apply live
  when connected.
- **Consumer isolation.** An exception from the tick consumer (the engine, the Redis publish) is
  counted in `health.ticks_failed` and logged, and streaming continues. Previously an engine error
  would have dropped the broker connection.
- **Malformed packets** (short or unparseable) are skipped and counted in `health.ticks_dropped`.
- **Health.** State is one of CONNECTING / CONNECTED / RECONNECTING / DEGRADED / STOPPED, along with:
  - last tick time, last message time, last connect/disconnect time
  - last error
  - reconnect count
  - subscriptions
  - counters

  It is exposed as `feed` in `GET /api/status`.

## Data quality

`TickQualityMonitor` records the following without altering or filling any tick:
- non-increasing sequence numbers
- last-trade-time reversals
- cumulative-volume resets
- crossed books
- empty book sides
- non-positive prices
- gaps of more than 60 s between ticks

Events are logged now; the `data_quality_events` table comes in Phase 3.

**Not checked:** gaps in the sequence number. Angel does not document the Snap Quote sequence as
contiguous per instrument, so a jump does not prove a missed tick.

**Not yet wired:** `ProviderRunner.check_liveness()` can mark a silent feed DEGRADED. It needs an
exchange calendar to avoid flagging normal off-hours silence, so it isn't called yet.

## Raw tick archive

`data/ticks/<YYYY-MM-DD>/<provider>_<token>.jsonl` holds one `MarketTick.to_dict()` per line, including
quote-only ticks.
- The day is the IST date of the receive time, independent of the host's timezone.
- Writes are batched every second on a worker thread.
- A tick is archived before it reaches the engine, so a consumer failure never loses it.
- Controlled by `RECORD_RAW_TICKS` (on by default).

This archive is what makes a future re-run of the engine over a past session possible. The trade-only
`observations.jsonl` cannot do that.
