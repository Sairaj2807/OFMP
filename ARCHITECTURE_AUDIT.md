# Charts_v2 — Architecture Audit

Audit date: 2026-09-30. Scope: every Python module, `static/`, `tests/`, config, data layout, and the
research documents. No code was changed to produce this document.

Purpose: establish exactly what exists before converting Charts_v2 into a production order-flow
platform, so the migration preserves the validated research logic and fixes the right problems first.

---

## 0. Summary

Charts_v2 is a **single-process, single-instrument, single-user research prototype** that works well
for what it was built for:

- one FastAPI process
- one Angel One WebSocket
- one NIFTY future
- an in-memory footprint
- JSONL files on disk
- three vanilla-JS pages

The engine code is careful, well commented and has reasonable tests for the chart and payload
layers. Everything *around* the engine is prototype-grade:

- no auth
- hardcoded broker secrets
- no database
- no version control
- no process separation
- no structured logging
- no deployment artifacts

**Top 5 findings**

| # | Finding | Severity |
|---|---|---|
| 1 | Live Angel One credentials (client code, PIN, TOTP secret, API key) are hardcoded in `angelone_autologin.py`; session JWTs are stored in plaintext `angel_tokens.txt`; Arrow credentials were hardcoded in `config.py` until today. | **Critical** |
| 2 | The project is **not a git repository**. There is no history, no rollback, and no way to diff a refactor against the validated state. | **Critical** (for the migration) |
| 3 | Server binds `0.0.0.0:8000` with **no authentication**. Anyone on the network can call `POST /api/contract` and switch the live feed for everyone. | High |
| 4 | **No test covers `TradeClassifier.classify`**, the one validated research result. It could regress silently. | High |
| 5 | Ingestion, classification, persistence, HTTP and WebSocket fan-out all run on **one asyncio event loop**, with blocking file I/O per trade. | High (scalability) |

**Classifier baseline, measured during this audit:** replaying `data/verified_dataset.csv` (55 rows)
through the current `TradeClassifier.classify`, with each row's recorded prior state
(`prev_ltp`, `prev_side`, `quote_age_ms`):

- current classifier: **54/55 correct**. The only miss is trade 25, the zero-tick case the docstring
  documents as intentionally unhandled.
- original Lee-Ready (`algo_side` column): 13/55.
- `VTRENDERS_RECONSTRUCTED_ALGORITHM.md` quotes 53/55 for the midpoint rule alone. The extra correct
  row comes from the stale-quote fallback (trade 17).

**This 54/55, with trade 25 as the only miss, is the regression baseline to pin before any refactor.**

---

## 1. Current architecture

```
┌──────────────────────────── one Python process (uvicorn, server.py) ─────────────────────────────┐
│                                                                                                  │
│  startup ─► angel_client.ensure_valid_token()  (REST login + TOTP, tokens → angel_tokens.txt)    │
│          ─► angel_client.resolve_configured_future()  (36 MB scrip-master JSON, 24h file cache)  │
│          ─► ws_ingest.AngelWebSocketClient.run()   ── asyncio task ──┐                           │
│          ─► _broadcast_loop()                      ── asyncio task ──┼──┐                        │
│                                                                      │  │                        │
│  Angel WS (binary Snap Quote) ─► parse_snap_quote ─► server._on_tick ┘  │                        │
│      └─► orderbook_engine.process_tick(STATE["engine"])                 │                        │
│              ├─ OrderBook.replace_side / validate                       │                        │
│              ├─ TradeClassifier.extract_trade_qty + classify            │                        │
│              ├─ Footprint.add_trade  (60 s native candles, ≤400 kept)   │                        │
│              ├─ _advance_candle → CVDTracker                            │                        │
│              └─ observation_sink → ObservationStore.log  (open/append/close per trade, sync)     │
│                                                                         │                        │
│  every 0.5 s: build snapshot per distinct (ppr, interval) ─► send_text to every browser socket   │
│                                                                                                  │
│  REST: /api/status /api/contracts /api/contract /api/replay/*     Static: / /chart /replay       │
└──────────────────────────────────────────────────────────────────────────────────────────────────┘
        disk: data/sessions/<date>/observations.jsonl, labels.jsonl; angel_tokens.txt; instruments_cache.json
```

**Module inventory**

| File | LOC | Role | Production relevance |
|---|---|---|---|
| `server.py` | 693 | FastAPI app, global `STATE`, payload serialisers, WS fan-out, replay API, contract switch | Split into API, realtime gateway, services |
| `orderbook_engine.py` | 876 | OrderBook, TradeClassifier, Footprint, CVD, POC/VA/imbalance, `process_tick` | **Core IP.** Extract into a domain package unchanged, then decompose |
| `ws_ingest.py` | 155 | Angel WS 2.0 client, binary parser, reconnect loop | Becomes `AngelOneProvider` |
| `angel_client.py` | 216 | Token lifecycle, scrip master, contract resolution/auto-roll | Becomes provider auth, plus the Instrument/Contract service |
| `angelone_autologin.py` | 122 | REST login, TOTP, **hardcoded credentials** | Replace with secret-backed credential provider |
| `replay_engine.py` | 112 | Rebuilds footprint from saved trades via the engine's own `Footprint`/`_advance_candle` | Keep the principle; replace the O(n)-per-request rebuild |
| `observation_store.py` | 206 | Day-partitioned JSONL writer/reader, labels | Moves to `research/`; production persistence → DB |
| `review_cli.py`, `export_dataset.py` | 309, 95 | Manual labelling and dataset export | Moves to `research/` |
| `lee_ready_classifier.py`, `emo_classifier.py` | 140, 150 | Alternative classifiers used for comparison reports | Become registered classifier implementations |
| `config.py` | 121 | Module-level constants | Replace with typed settings |
| `arrow_client.py`, `arrow_ingest.py`, `contract_resolver.py`, `tests/test_replay_arrow.py`, `tests/test_contract_resolver.py` | — | **Dead** after the Arrow removal; deletion pending the owner's confirmation | Delete |
| `test.py`, `try.py` | 0 | Empty files | Delete |
| `static/*.js/html/css` | ~1,900 | Three vanilla-JS pages; `/chart` uses vendored OpenAlgo Charts 2.4.0 | Replace the shell, keep the chart engine (see §4) |
| `openalgo-charts-master/` | — | Upstream library source (Apache-2.0), not imported at runtime | Keep out of the app tree; pin a version |

---

## 2. Current data flow

**Per tick (hot path)**

All of this runs synchronously on the event loop.

1. `ws.recv()` → length check (≥ 347 bytes) → `parse_snap_quote` (`struct.unpack`).
   - Produces `ltp`, `cum_volume`, `ltt`, `exch_timestamp_ms`, `sequence`, `depth_buy/sell` (5 levels
     each), OHLC, OI and total buy/sell quantity.
2. `server._on_tick` copies 5 fields into `tick_in`: `ltp`, `ltt`, `cum_volume`, `depth_buy`,
   `depth_sell`.
   - **`sequence`, `exch_timestamp_ms` and receive time are discarded.** Gap detection and latency
     measurement are therefore impossible today.
3. `process_tick`:
   - replace the book wholesale;
   - reject a crossed book (the tick is dropped and **not recorded anywhere**);
   - diff cumulative volume to get the trade quantity (a negative diff is silently treated as a reset);
   - classify;
   - add to the footprint;
   - roll the candle and CVD;
   - build a ~45-field observation dict;
   - append it to JSONL (`open()` → `write` → `close()` for every trade).
4. `engine.last_quote = tick` for header display.

**Per 0.5 s broadcast**

- For each distinct client state `(ppr, interval_sec)`, serialise up to `CHART_BARS × multiple`
  native candles (60 × up to 30 = up to 1,800 candle cells) plus book and quote.
- `json.dumps` it, then send it to every client sharing that state.
- Every message is a **full snapshot**, never a delta. A slow client's `send_text` awaits inline and
  blocks the broadcast to everyone behind it.

**Replay**

- `/api/replay/{date}/snapshot` and `/chart` load the whole day (cached in an unbounded dict).
- Each request runs `build_replay_state(observations, as_of_ms)`, which **re-aggregates from the
  first trade of the day up to the cursor on every scrub step**.
- Replay does **not** re-classify. It uses the stored `algo_side`, which is the correct property for
  reproducibility. However, **no classifier version is stored in the records**, so which algorithm
  produced a given day's sides is only known from the file date.

**Timestamps**

- The footprint buckets on `ltt`, which Angel sends in **seconds** (converted to ms by `×1000`).
  Intra-second ordering relies on arrival order.
- Session folders are chosen via `datetime.fromtimestamp()`, i.e. **the host's local timezone**. This
  is correct only while the server runs on an IST machine.

---

## 3. Current database / storage

| Data | Storage | Issues |
|---|---|---|
| Classified trades + 45 features | `data/sessions/<date>/observations.jsonl` | No schema, no index, no classifier version. The id is recovered by scanning the whole file at startup. Blocking append per trade. |
| Manual labels | `labels.jsonl` per day | Fine for research; belongs in `research/` |
| Verified dataset | `data/verified_dataset.csv` (55 rows) | The only ground truth. Must become an immutable test fixture. |
| Raw ticks | **Not stored.** | Only trade-producing ticks are logged, and only as derived observations. Quote-only ticks, crossed-book ticks, sequence numbers and OI are lost, so historical re-processing from raw input is impossible. |
| Candles/footprint | Memory only (400 native candles) | Lost on restart. Live CVD restarts from 0 at every server start. |
| Broker tokens | `angel_tokens.txt`, plaintext | Secret on disk |
| Instrument master | `instruments_cache.json`, 36 MB | Fully `json.load`-ed on **every** `/api/contracts` and `/api/contract` call |
| Users, workspaces, alerts, layouts | — | Do not exist |

Session data on disk today: 2026-09-22 → 2026-09-30 Angel observations, plus one legacy
Arrow day (2026-08-12) that is still used as a test fixture.

---

## 4. Current frontend

- **Shell:** three hand-written HTML pages (`index.html` for the original dashboard,
  `orderflow.html` → `/chart`, `replay.html`). There is no build step, framework, router or state
  management. `render.js` is shared through globals.
- **Chart engine:** OpenAlgo Charts 2.4.0 (Apache-2.0), vendored as two prebuilt ES modules
  (344 KB + 53 KB). `orderflow_adapter.js` (54 LOC) is the only place that maps engine fields to
  library fields. It is tested by `tests/js/adapter.test.mjs`.
- **Data transport:** one WebSocket per page, receiving a full snapshot every 0.5 s. The client
  re-renders from scratch per message.
- **Known divergence:**
  - The library's stacked-imbalance definition differs from the engine's: 6 vs 12 flagged candles on
    the fixture session, 0 identical. Which one matches Vtrender is not established.
  - The old dashboard's "Session CVD" is always 0 (documented).

**Chart-engine decision: RETAIN and ADAPT OpenAlgo Charts.** Do not replace it.

- It already ships, under a permissive licence:
  - a Footprint primitive, market profile and volume profile modules
  - a replay module and drawing tools (`src/draw`)
  - chart linking (`src/link`)
  - a WebGL backend and indicators
  - a widget shell with keymaps
- That covers most of §§24–46 of the brief. The per-page glue is the weak part, not the renderer.
- Replacing it with Lightweight Charts would mean re-building footprint, profile and drawing
  primitives from scratch.
- Plan: wrap it in a React component with **imperative** updates (no React re-render per tick), pin
  the version, and build it from source in CI instead of copying prebuilt files by hand.

---

## 5. Current backend

- **Layering:** none. `server.py` mixes several jobs:
  - route handlers
  - global mutable `STATE`
  - payload serialisation
  - WebSocket session management
  - contract switching and replay caching
- **Global state:** one `STATE` dict holding one engine, one contract and one WS client. Multi-instrument
  support is structurally impossible without a redesign.
- **Config:** module constants plus a few `os.environ.get` calls. There are no typed settings, no
  per-environment profiles and no feature flags.
- **Logging:** `print()` everywhere. There are no request IDs and no levels.
- **Errors:** the ingest loop catches everything and reconnects after a fixed 3 s, with no backoff,
  no jitter and no circuit breaker. Route handlers return ad-hoc `{"ok": false, "error": ...}`
  shapes.
- **Lifecycle:** uses the deprecated `@app.on_event` (4 deprecation warnings in the test run).
- **Dependencies:** `requirements.txt` has 8 unpinned packages. The local runtime is Python 3.14.6
  and Node 24.19.0. There is no lockfile, `pyproject.toml`, Dockerfile, systemd unit, nginx config or
  CI.

---

## 6. Current WebSocket architecture

| Channel | Direction | Protocol | Gaps |
|---|---|---|---|
| Angel `smart-stream` | provider → server | Binary Snap Quote, mode 3, 1 token | Fixed 3 s reconnect. Sends a text `ping` every 30 s. **One subscription per process.** `sequence` is ignored. |
| `/ws/frontend` | server → browser | Full JSON snapshot every 0.5 s; client sends `{ppr, interval}` | No auth, no heartbeat, no message sequence numbers, no backpressure, unbounded clients |
| `/ws/chart` | server → browser | Same, different payload | Same |

What is good and worth keeping:

- **Build-once-per-distinct-state dedup**, i.e. the payload is not rebuilt per client.
- The provider connection is **already centralised**: browsers never talk to Angel directly.

---

## 7. Current algorithm architecture

**Pipeline in `process_tick`**

```
book replace → validate → cum-volume diff → classify → Footprint.add_trade → _advance_candle/CVD → observation
```

**What is good**

- The classifier is already a class with explicit state (`last_price`, `last_side`, `last_reason`).
- `_advance_candle` is shared by live and replay, so rollover can't drift.
- Coarser intervals are grouped from native bars at payload time (`group_native_bars`), keeping the
  hot path fixed at 60 s.
- Min/max delta is tracked as trades arrive.
- Grouped bars chain their deltas correctly (documented and tested).

**What blocks production**

- `TradeClassifier` mixes two concerns: volume extraction (`extract_trade_qty`) and side
  classification (`classify`). It also still carries the exact-side (`btv/atv`) path, which is now
  unused.
- `process_tick` also does observation feature engineering (`_build_observation`, 45 fields) on every
  trade in the live hot path. That is research-only work.
- There is **no classifier version, no config version and no algorithm registry**. The classifier
  changed from Lee-Ready to the reconstructed rule in place. Sessions recorded before and after that
  change are indistinguishable except by date.
- **Session awareness:**
  - Candles bucket on epoch seconds modulo 60, which is fine.
  - CVD resets at *process start*, not at *session start*.
  - There is no exchange calendar.
- **Engine vs library analytics:** POC, value area and imbalances are computed twice, once by the
  engine (old pages) and once by the JS library (`/chart`), using **different stacked-imbalance
  definitions**. Production needs one authoritative definition, computed server-side and versioned.

---

## 8. Technical debt

1. No version control. The previous turn's file deletions were blocked for exactly this reason.
2. Dead Arrow code and stale Arrow references in docs:
   - `ORDERFLOW_CHART.md`
   - `CONTRACT_ROLLOVER.md`
   - `orderbook_engine.py` docstrings
   - `replay_engine.py` exact-side branch
   - `tests/helpers.py` naming
3. `_process_exact_sides` and `extract_trade_deltas` have no live caller since Arrow was removed.
   Only the fixture-based chart tests exercise them.
4. Two parallel UIs (`/` + `/replay` and `/chart`) with duplicated analytics paths
   (`_candles_payload` vs `_chart_payload`).
5. Deprecated FastAPI `on_event`; unpinned dependencies.
6. Research tooling (`review_cli`, `export_dataset`, `observation_store`, labels) is interleaved with
   production code.
7. `angelone_autologin.py` has import-time side effects (a module-level `requests.Session`) and a bare
   `except:`.
8. `get_public_ip()` calls `api.ipify.org` during login. That is a third-party dependency on the auth
   path.
9. Empty files `test.py` and `try.py`, plus a stale `server.log`.

---

## 9. Security risks

| Risk | Where | Severity | Remediation |
|---|---|---|---|
| Hardcoded broker credentials (client code, PIN, TOTP secret, API key) | `angelone_autologin.py:6-9` | **Critical** | **Rotate the PIN, TOTP seed and API key now.** Load them from env or a secret manager; never in source. |
| Arrow password, app secret and TOTP secret were in `config.py` until today | removed | **Critical** | Rotate them at Arrow even though the code is gone. They sat on disk and may exist in backups or copies. |
| Plaintext session tokens | `angel_tokens.txt` | High | Hold them in memory or Redis with a TTL; if persisted, encrypt at rest |
| No authentication or authorization on any route or socket | `server.py` | High | Auth + RBAC (Phase 4) |
| Global state mutation by any caller (`POST /api/contract`) | `server.py` | High | Admin-only; per-user chart subscriptions instead of switching the global feed |
| Binds `0.0.0.0` in `__main__` | `server.py:693` | Medium | Bind `127.0.0.1` behind nginx |
| No TLS, CORS policy, rate limits, request size limits or security headers | — | Medium | nginx + FastAPI middleware (Phase 7) |
| Unbounded WebSocket clients and unbounded replay cache | `server.py` | Medium (DoS) | Connection limits, LRU cache |
| TOTP value printed to stdout | `angelone_autologin.py` (`[LOGIN] TOTP generated`) | Low–Medium | Never log secrets or OTPs |
| Third-party IP lookup on the login path | `get_public_ip` | Low | Configure the IP statically |
| No `.gitignore` or secret scanning (no repo at all) | — | High once a repo exists | Add `.gitignore` covering `.env`, `angel_tokens.txt`, `data/`, `instruments_cache.json` **before** the first commit, so secrets never enter history |

---

## 10. Performance bottlenecks

Ranked by expected impact once there are multiple instruments and users.

1. **Single event loop does everything.**
   - A slow JSON serialise or slow client stalls tick processing.
   - Tick processing stalls HTTP.
   - Fix: separate the ingest/engine worker process from the API/gateway process, connected by Redis.
2. **Blocking file append per trade** on the loop. Fix: batched async writer, then DB `COPY`.
3. **Full-snapshot broadcast** every 0.5 s: up to 1,800 native candles' cells re-serialised per state.
   - Fix: send incremental updates (changed candle + book) with sequence numbers.
   - Serve the full snapshot only on subscribe or resync.
4. **Replay is O(trades-to-cursor) per request.**
   - Scrubbing late in the day re-aggregates the whole session each step.
   - Fix: checkpoint engine state every N candles and replay forward from the nearest checkpoint.
     Alternatively, stream server-side.
5. **36 MB scrip-master `json.load` per contract-list request.**
   - Fix: load it once into an instruments table, with an in-memory or Redis cache.
6. **Unbounded replay cache** (a whole day's observations per date, never evicted).
7. **`_build_observation`** (45 features, list comprehensions) on every live trade.
   - Fix: move it behind a research feature flag.

No benchmark exists yet. Tick rate on NIFTY futures via Snap Quote is modest, so the current design
copes with one instrument. The limits above bite at N instruments × M users.

---

## 11. Migration strategy

**Principle:** strangler pattern around a frozen, test-pinned engine. Every phase ships a working
system.

The **golden-output harness** is the gate for every engine-touching phase:

- replay every saved session through the old engine and the new one;
- require byte-identical chart and snapshot payloads;
- require identical classifications.

### Phase 0 — Safety net

No behaviour change.

1. `git init`, with a `.gitignore` in place first. Commit the current state as the validated baseline.
2. Rotate all Angel (and Arrow) secrets.
   - Move them to `.env` (git-ignored) plus a `.env.example`.
   - `angelone_autologin.py` reads from env.
3. Delete dead code: the five Arrow files, `test.py`, `try.py`.
4. **Classifier regression test** (`tests/regression/test_vtrender_classifier.py`):
   - Freeze `verified_dataset.csv` into `tests/fixtures/verified_trades/`.
   - Assert exactly 54/55, with trade 25 as the only miss.
   - Assert **per-row expected sides**, so any change to any row fails.
5. **Golden-output harness:** snapshot the engine's outputs for every session on disk as fixtures.
6. Pin dependencies with `pyproject.toml` + a lockfile; choose Python 3.12 LTS for production.

### Phase 1 — Extract the domain core

- Create `backend/app/domain/orderflow/`.
- Move `OrderBook`, `Footprint`, `CVDTracker`, and the POC/VA/imbalance functions **verbatim**.
- Split `TradeClassifier` into:
  - `VolumeExtractor` (cumulative diff)
  - a `TradeClassifier` protocol
  - `VtrenderReconstructedClassifierV1`, with the same code, `name="vtrender_reconstruction"` and
    `version="v1"`
  - `LeeReadyClassifier` and `EmoClassifier` (from the existing research files)
  - `TickRuleClassifier`
- Add an algorithm registry. Classifier selection is config-driven; the default is V1.
- Decompose `process_tick` into pipeline stages without changing their order.
- Move observation feature building to `research/`.
- Every derived trade carries `classifier_name`, `classifier_version`, `reason`, and
  `config_version` (a hash of the classification config).
- Gate: the golden harness is byte-identical.

### Phase 2 — Provider abstraction and ingest worker

- `MarketDataProvider` ABC. Implement `AngelOneProvider` from `ws_ingest.py` + `angel_client.py`.
- Canonical `MarketTick` with:
  - `sequence`
  - `exchange_ts` (from `exch_timestamp_ms`)
  - `ltt`
  - `received_ts`
  - `processed_ts`
- Separate the **ingest worker** process: provider → normalise → engine → Redis Streams (ticks, trades,
  candle updates, book).
- Reconnect with exponential backoff + jitter, resubscribe, and sequence-gap detection feeding
  `data_quality_events`.
- The API process stops owning the provider connection.

### Phase 3 — Persistence

- PostgreSQL 16 + TimescaleDB, with Alembic from migration 0001.
- Hypertables:
  - `raw_ticks` (every tick, including quote-only, for full reprocessing)
  - `trades`
  - `candles_1m`
  - `footprint_cells_1m`
  - Continuous aggregates for coarser intervals.
- Instruments/contracts table loaded from the scrip master once a day by a job.
- **Dual-write** JSONL and DB for one or two weeks, with a nightly reconciliation job. Then JSONL
  becomes archival only.
- Nightly Parquet+ZSTD export to S3-compatible storage (MinIO in dev).

### Phase 4 — API v1, auth and realtime gateway

- `/api/v1/*` with a layered backend (routes → services → repositories). Standard error envelope,
  request IDs, structured JSON logs.
- Auth:
  - Argon2id password hashing
  - short-lived access JWT
  - rotating refresh token in an HttpOnly/Secure/SameSite cookie
  - session table
- RBAC, organisation-ready ownership, `EntitlementService`.
- Realtime gateway:
  - authenticated `/ws/*`
  - subscribe/unsubscribe per `{contract, streams, timeframe}`, validated server-side
  - heartbeats
  - per-connection bounded queue with coalescing (latest-state-wins) for backpressure
  - message sequence numbers + resync

### Phase 5 — Frontend

- Next.js + TypeScript + Tailwind + Zustand + TanStack Query.
- OpenAlgo Charts wrapped imperatively. Port `orderflow_adapter.js` and its tests first.
- Build order:
  1. the terminal layout
  2. symbol search
  3. single chart
  4. DOM
  5. multi-chart
  6. workspaces
- Retire the old pages only when `/chart` parity is confirmed.

### Phase 6 — Feature breadth

- First-class replay:
  - server-side streaming from DB with engine checkpoints
  - **same engine as live**
  - speed control, step by tick or candle
- Volume and market profile; absorption and exhaustion detectors, exposed as versioned *derived
  signals*.
- Alert engine with a `NotificationProvider` interface.
- Data-quality admin dashboard; exchange calendar; session-aware CVD.
- Resolve the stacked-imbalance definition conflict (§7) with a documented decision and a version.

### Phase 7 — Operations

- Docker Compose for dev, staging and prod, with nginx + Let's Encrypt.
- Prometheus metrics, Grafana, OpenTelemetry.
- CI:
  - ruff, mypy, pytest (including the regression suite)
  - eslint, tsc, vitest
  - docker build
  - pip-audit, npm audit, Trivy, secret scan
- Backup/restore runbooks, threat model, DR docs.

---

## 12. Recommended target architecture

**Deployment:** modular monolith plus one isolated high-throughput worker. It deploys as four app
containers alongside Postgres/Timescale, Redis and nginx.

```
            nginx (TLS, headers, WS proxy, rate limits)
               │
   ┌───────────┼─────────────────────┐
   ▼           ▼                     ▼
frontend    api (FastAPI)        realtime gateway (FastAPI WS; may start inside `api`)
(Next.js)   REST /api/v1          subscribes to Redis, fans out per-subscription
               │                     ▲
               ▼                     │ Redis Streams / PubSub (latest state, fan-out, locks, rate limits)
          services ─ repositories    │
               │                     │
               ▼                     │
   PostgreSQL + TimescaleDB   ◄──── ingest worker: Provider(s) → normalise → orderflow engine
   (users, instruments,             (one process per provider; engine per subscribed contract;
    ticks, trades, candles,          checkpoints state to Redis; batched COPY to Timescale)
    footprint, alerts, …)
               │
   object storage (Parquet+ZSTD archives, research exports, backups)

   jobs worker (Arq on Redis): scrip-master sync, backfill, reprocessing, exports, alert delivery
```

**Why this shape**

- The ingest worker restarts independently of API deploys, so deploying the API doesn't cause a
  market-data gap.
- The engine stays one library, imported by both the ingest worker (live) and the replay service, so
  **live engine == replay engine** by construction.
- **Arq** is chosen over Celery because it is asyncio-native and Redis-only, matching the existing
  stack with no extra broker.
- **TimescaleDB** is chosen over a separate TSDB to keep one database technology. At this data volume
  (tens of millions of rows per month per instrument) it is comfortably sufficient.

**Backend layout:** follows the brief's §6 layout. The orderflow domain lives in
`backend/app/domain/orderflow/` as pure Python with no FastAPI, Redis or DB imports. That makes it
unit-testable and reusable by replay and research.

**Classifier contract (Phase 1)**

```python
class TradeClassifier(Protocol):
    name: str            # "vtrender_reconstruction"
    version: str         # "v1"
    def classify(self, ctx: ClassificationContext) -> Classification: ...
        # ctx: price, best_bid, best_ask, prev_price, prev_side, quote_age_ms, book
        # Classification: side, method ("midpoint"|"tick_rule_stale"|"cold_start"|...), confidence?
```

`VtrenderReconstructedClassifierV1` is today's `classify()` body, moved unchanged. The Phase 0
regression test must pass against it before and after the move.

---

## 13. Owner decisions (answered 2026-09-30)

1. **Secrets rotation.** Yes: the owner rotates the Angel One and Arrow credentials. The code now reads
   Angel credentials from the environment / `.env` only.
2. **Version control.** Yes: `git init` with `.gitignore`. No remote yet.
3. **Dead-file deletion.** Yes: the five Arrow-era files, `test.py` and `try.py` are deleted.
4. **Legacy Arrow session (2026-08-12).** Not needed; deleted. The system runs on, and stores, live
   Angel data. The chart and adapter tests now use a seeded synthetic Angel session generated in code
   (`tests/helpers.py`), so no stored data is needed.
5. **Stacked-imbalance definition.** Use the **engine's** rules as the single authoritative
   definition (v1, labelled unvalidated against Vtrender). The chart library's own imbalance
   computation is to be replaced by engine-supplied imbalances when the chart layer is rebuilt.
6. **Production Python version.** Owner has no preference. Target: Python ≥ 3.12; developed and
   tested on 3.14.6.

## 14. Phase 0 status

Done on 2026-09-30:

- Angel credentials moved out of source to env / `.env` (`.env.example` added). TOTP codes and
  session tokens are no longer printed.
- `.gitignore` added, covering secrets, `data/`, the instrument cache, logs and the upstream library
  source.
- Dead files and the Arrow session deleted.
- Dependencies pinned. `requirements-dev.txt` added. The two Arrow-only packages were removed.
- Regression tests added:
  - `tests/regression/test_vtrender_classifier.py`: all 55 verified trades pinned per row
    (side + reason); accuracy is exactly 54/55, with trade 25 the only miss.
  - `tests/regression/test_live_sessions_golden.py`: every stored live trade must re-classify to the
    side and reason written at the time. 40,460/40,460 trades reproduce.
- Chart and adapter tests moved from the Arrow file to the synthetic Angel session.
- Git repository initialised; baseline committed.

## 15. Phase 1 status

Done on 2026-09-30, on branch `phase1-domain-core`:

- Engine moved to `backend/app/domain/orderflow/` (pure Python, isolation enforced by a test).
  `orderbook_engine.py` is now a compatibility layer. See `docs/orderflow-engine.md`.
- The classifier sits behind a versioned `TradeClassifier` protocol, with a registry:
  - `vtrender_reconstruction/v1` is the default and is the unchanged production rule;
  - `lee_ready/v1` and `tick_rule/v1` are registered for comparison.
- Every observation record now stores `classifier_name` and `classifier_version`.
- `process_tick` is split into explicit stages. The research feature snapshot moved to
  `research/observation_features.py`, and the engine emits a `TradeEvent` instead.
- The unused exact-side (`btv/atv`) path and `coalesced` bookkeeping were removed from the engine and
  from replay.
- Engine parameters have a single source (`settings.py`), re-exported by `config.py`.
- Added an engine-wide golden test pinned to the pre-refactor engine. The refactored engine reproduces
  it byte-for-byte.
- **Found:** v1 decides trades exactly at the midpoint by floating-point rounding.
  - Affects 1.04% of live trades; 11 of 40,210 went BUY because of it.
  - Kept as-is and pinned by a test. A fix is a v2 candidate (see `docs/orderflow-engine.md`).

Still open:

- Move `review_cli.py`, `export_dataset.py`, `lee_ready_classifier.py` and `emo_classifier.py` into
  `research/`. They are unchanged so far, to keep their documented command lines working.
- Arrow-era docs carry a status note rather than a rewrite.

---

*Note: the request brief was truncated at §158 (threat model). Sections §158 onward were not
received and are not reflected here.*

## 16. Phase 2 status

Done on 2026-09-30, on branch `phase2-market-data` (stacked on `phase1-domain-core`). See
`docs/market-data.md`.

- Canonical `MarketTick` keeps the sequence number, the exchange timestamp and the server receive
  time. Previously the server discarded all three.
- `MarketDataProvider` interface, with `AngelOneProvider` implementing it. The parser was verified
  identical to the old `ws_ingest.py` on 2,000 random packets; `ws_ingest.py` is removed.
- `ProviderRunner`:
  - exponential backoff with jitter (replaces the fixed 3 s retry)
  - resubscribe on reconnect
  - health tracking
  - data-quality events
  - consumer isolation: an engine error no longer drops the broker connection
- `INGEST_MODE`:
  - `embedded` is the default and needs no infrastructure;
  - `redis` runs a separate ingest worker publishing to Redis streams, which the API consumes. API
    restarts no longer touch the broker connection.
- Raw tick archive (`data/ticks/`): every tick, quote-only included, dated in IST regardless of the
  host timezone, written off the event loop.
- `GET /api/status` includes provider health.

Still open:

- `DEGRADED` detection needs the exchange calendar.
- Restoring the live engine state after an API restart needs stored ticks (Phase 3).
- The observation JSONL write is still synchronous on the event loop (Phase 3 replaces it).
- Not exercised against the live Angel feed in this session: covered by unit tests with a fake
  WebSocket, and by an end-to-end worker → Redis → API test on fakeredis.


## 17. Phase 3 status

Done on 2026-09-30, on branch `phase3-persistence` (stacked on `phase2-market-data`). See
`docs/database.md`.

- **Local database.** TimescaleDB (PostgreSQL 16) and Redis in `docker-compose.dev.yml`, on offset
  ports 55432 and 56379. The previous default `REDIS_URL` pointed at port 6379, which on this machine
  belongs to another project's Redis; that is fixed.
- **Schema and migration.** Alembic migration `0001`:
  - `raw_ticks` and `trades` hypertables
  - `data_quality_events`, `instruments`, and seeded `algorithm_versions`
  - tested up/down/up with no drift reported by `alembic check`
- **`MarketDataWriter`.** Batched, idempotent and non-blocking. It buffers through database outages,
  bounded at 500,000 rows per table.
  - Raw ticks and quality events come from whichever process holds the broker connection.
  - Trades come from the API, with the same `trade_id` as the JSONL record.
- **Session restore.** The server rebuilds today's footprint and CVD from stored trades when a
  contract is activated. The restored state is verified identical to the live engine's.
- **Session dates in IST.** Session dates are explicit IST everywhere (JSONL folders and database)
  instead of the host's timezone.
- **CLI** (`python -m backend.app.cli`): `migrate`, `import-sessions`, `import-ticks`, `reconcile`,
  `db-status`.
- **Backfill.** All 40,460 recorded trades (2026-09-22..30) were imported into the dev database, and
  every session reconciles exactly against the JSONL files.

Still open:

- Compression and retention policies; Parquet export to object storage.
- A scheduled daily reconcile job.
- Removing the JSONL dual-write.
- The observation JSONL append is still synchronous on the event loop. It goes away with the
  dual-write rather than being rewritten.


## 18. Phase 4 status

Done on 2026-09-30, on branch `phase4-api-auth` (stacked on `phase3-persistence`). See `docs/api.md`.

**Core** (`backend/app/core/`):
- JSON or text structured logging, with the request ID and user ID on every line and known secret keys
  redacted
- the standard error envelope; unexpected errors never leak details
- request-ID and body-size middleware (pure ASGI, so WebSockets are covered too)
- Argon2id password hashing
- HS256 access tokens that reject `alg=none`, the wrong type, tampering and expiry
- opaque tokens stored only as SHA-256
- double-submit CSRF
- roles and permissions defined in code
- a sliding-window rate limiter

**Database.** Migration `0002` adds `organizations`, `users`, `organization_members`, `user_sessions`,
`refresh_tokens`, `email_tokens` and `audit_logs`. Every user gets a personal organization at
registration.

**Auth service:**
- register without revealing whether an email exists, plus email verification
- login with equal work for unknown emails
- rotating refresh tokens with reuse detection, which revokes the session
- logout, log out everywhere, and per-session revocation with an ownership check
- password reset, which revokes all sessions
- password change, which revokes other sessions
- audit log for security actions

*Bug caught during implementation:* raising inside the transaction would have rolled back the
failed-login audit row and the reuse revocation. Both now raise after commit, and tests pin this.

**API** (`/api/v1`):
- auth and account routes
- market status and contracts
- admin users (cursor pagination) and system
- `/health`, `/ready`, `/live`
- HttpOnly cookie sessions: access cookie SameSite=Lax; refresh cookie SameSite=Strict, scoped to
  `/api/v1/auth`
- Bearer tokens for API clients
- per-IP and per-account rate limits
- OpenAPI docs at `/api/docs` (hidden in production)

**`/ws/v1/stream`:**
- Origin check against cross-site WebSocket hijacking
- auth by cookie or first message
- `market.read` required
- validated subscribe messages, restricted to the active contract
- per-message `seq` and `ts`
- heartbeats with idle close
- session re-checked every 15 s (a revoked login is disconnected)
- connection limits: 5 per user, 1000 total
- constant-memory backpressure (only the latest snapshot is kept)

**Legacy.** `AUTH_REQUIRED=1` puts `/`, `/chart`, `/replay`, `/api/*` and `/ws/frontend|chart` behind
login, with a login page. With it on, switching the live contract needs `admin.system`. It is off by
default, so the current pages keep working unchanged.

**Tooling.** `python -m backend.app.cli create-user` bootstraps the first admin; the password is never
taken from command-line arguments.

Still open:

- An email provider (development logs the links).
- Redis-backed rate limits for multi-process deployments.
- OAuth, 2FA and passkeys.
- Security-event emails (new login, password changed).
- Entitlements, plans and billing tables.
- Legacy sockets are only checked at connect time, not re-checked like `/ws/v1/stream`.
- The new frontend (Phase 5) will use `/ws/v1/stream` and `/api/v1`.

## 19. Phase 5 status

Done on 2026-09-30. See `frontend/README.md`.

**Frontend.** `frontend/`: Next.js 16 (App Router, static export), React 19, TypeScript (strict),
Tailwind 4, Zustand.
- Production build is served by FastAPI at `/app`, the same origin as the API and stream.
- `next dev` proxies the API during development.

**Terminal:**
- top bar: contract, live status (colour + text), layout 1/2/4, qty/lots, command palette, sign out
- watchlist with live LTP/change
- resizable panels
- up to four independent footprint charts, each with its own interval, row size, cell style, value
  mode and POC/VA/imbalance toggles
- order-book ladder: spread, mid, imbalance
- bottom figures: LTP, bar volume and delta, CVD, and how sides are decided
- Ctrl/Cmd+K command palette and single-key shortcuts
- chart settings persisted as versioned JSON with a migration hook

**Chart engine.** OpenAlgo Charts is kept; the decision was made in §4. It is driven imperatively
through `FootprintController`, so the 2 Hz stream never re-renders React. The behaviour of the old
page was ported. The library's watermark is off (`branding: false`); its license and NOTICE are
vendored.

**Stream.** `/ws/v1/stream` now carries up to 6 subscriptions per connection, one per chart, each with
its own one-snapshot backpressure slot. The client:
- reconnects with jittered backoff
- resubscribes after reconnecting
- answers heartbeats
- tracks seq
- refreshes the session on close code 4401

**Security in the UI:**
- tokens are only ever in HttpOnly cookies
- CSRF header on writes
- one shared refresh on 401
- tooltips and messages are built with textContent, never HTML strings

**Development feed.** `MARKET_DATA_PROVIDER=synthetic` is a random-walk feed that needs no broker,
credentials or market hours. Nothing from it is recorded: no observations, tick archive or database
trades. It is refused in production.

**Verified:**
- frontend: `tsc` and ESLint (React Compiler rules) clean; `next build` OK
- 18 Vitest unit tests
- 6 Playwright end-to-end tests against the real backend with the synthetic feed:
  - redirect when signed out
  - login error
  - live data drawn, with no console errors
  - 4-chart layout
  - interval change
  - palette
  - persistence across reload
  - sign-out
- backend: 279 tests

**Still open:**
- In the 4-chart layout, the per-chart toolbar is wider than the panel and scrolls horizontally.
- Server-side workspaces and saved layouts; symbol search across instruments; watchlists with several
  underlyings.
- Replay in the new UI (the old `/chart?mode=replay` page still works).
- Drawing tools, alerts UI, volume/market profile panels, DOM trading ladder.
- The legacy pages (`/`, `/chart`, `/replay`) remain until the new UI reaches parity.

## 20. Phase 6 status (operations)

Done on 2026-09-30. See `docs/deployment.md`, `docs/monitoring.md`, `docs/disaster-recovery.md` and
`docs/security.md`.

**Images:**
- **Backend** (Python 3.12, non-root, runtime + test stages): OS security updates at build time; runs
  on a read-only root filesystem with all capabilities dropped.
- **nginx** (1.30): builds the terminal and generates the CSP from the shipped HTML.
- Both scan clean with Trivy (0 fixable HIGH/CRITICAL).

**Stack.** `docker-compose.prod.yml` runs nginx, api, worker (redis mode), timescaledb, redis,
prometheus, grafana, a one-shot migrate, and certbot (profile).
- Networks: `data` is internal-only; only nginx is published; Grafana binds to localhost.

**nginx:**
- TLS 1.2/1.3, HSTS, HTTP→HTTPS redirect
- security headers
- strict CSP built from hashes of the inline scripts (no `'unsafe-inline'`)
- WebSocket proxying
- per-IP rate and connection limits
- `/metrics` blocked from outside
- immutable caching for hashed assets

**Observability:**
- Prometheus metrics in the API and worker: ticks, provider latency (labelled as the provider's claim),
  engine time, classifications per version, stream, database writer, HTTP by route template
- 8 alert rules, validated with promtool
- a provisioned Grafana dashboard (14 panels)
- JSON logs with request IDs end to end (nginx → API)

**CI** (`.github/workflows/ci.yml`):
- ruff, pytest against TimescaleDB, and the node parity test
- pip-audit
- frontend lint, types, Vitest, build, npm audit
- Playwright end-to-end against a real backend
- image builds with a Trivy gate
- a gitleaks history scan
- Dependabot for pip, npm, actions and docker

**Backups.** `backup-db.sh` / `restore-db.sh` (TimescaleDB-aware, checksummed, refuses to overwrite the
live database without confirmation). A restore was tested into a scratch database and matched.

**Rehearsal.** The full stack was rehearsed locally on HTTPS (8443) with the synthetic feed. All six
Playwright tests passed through nginx under the strict CSP, and the legacy pages ran with zero CSP
violations. The backend suite ran on Python 3.12 inside the image: 272 passed.

**Defects the rehearsal caught and fixed:**
1. **Missing `greenlet`.** SQLAlchemy async needs `greenlet`, and it was missing from the
   requirements. It only worked locally by accident.
2. **API container ran the test suite.** Compose without `target:` builds the last Dockerfile stage
   (`test`), so the "API" container ran pytest.
3. **Forwarded headers dropped.** nginx does not inherit `proxy_set_header` into locations that set
   their own. As a result the API lost the real Host, client IP (rate limits and audit IPs would have
   been nginx's) and scheme. The Origin check blocked every WebSocket.
4. **Legacy pages blocked by the CSP.** Windows CRLF checkouts meant the build hashed different bytes
   than browsers do, since browsers normalize newlines before hashing. The hashing now normalizes, and
   `.gitattributes` enforces LF.
5. **Vulnerable base images.** They had fixable CVEs: OpenSSL in the Debian base, and the nginx binary
   on 1.28 (end-of-life).
6. **Tests depended on the environment** (`INGEST_MODE`, provider). An autouse fixture now pins them.

**Still open:**
- Alertmanager delivery channels.
- An exchange holiday calendar for the market-hours alerts.
- Redis-backed rate limits before running more than one API process.
- Pinning GitHub Actions to commit SHAs; image signing.
- Load testing.
- Off-site copy automation for backups (the documented `rclone` step).

## 21. Phase 7 status (workspaces + replay)

Done on 2026-09-30.

**Workspaces:**
- Migration `0003` adds the `workspaces` table. It is organization-scoped, with a soft delete, a unique
  name per user (among non-deleted workspaces), a revision counter and a default flag.
- `WorkspaceService` and `/api/v1/workspaces`:
  - ownership checks that look identical to "not found"
  - optimistic concurrency (409 on a stale revision)
  - size and count limits
  - audit logging
- Terminal:
  - loads the last-used workspace, else the default, else creates one
  - autosaves with the revision after 1.5 s and on `pagehide` (keepalive)
  - adopts the server copy on a conflict and shows a notice
  - menu: switch, save as, rename, duplicate, make default, delete
- Terminal config v1 → v2 is migrated field by field; invalid values are repaired, never discarded.

**Replay:**
- `ReplaySession` runs server-side on a virtual clock: play, pause, speed 1–50×, seek, step by trade or
  candle.
- It shares `replay_engine.apply_record` with the one-shot rebuild, and a parity test proves streamed
  replay equals the rebuild at every seek target.
- `ReplayLibrary` caches loaded days (LRU) and reads from the database when configured, else JSONL.
- The stream protocol gained `replay` and `replay_control`. An id is live or replaying, never both;
  there are at most 2 replays per connection, each with its own backpressure slot.
- Terminal: a Live/Replay switch per chart, and a replay bar with session date, stepping, play/pause,
  speed, a seek slider, the IST clock and progress. It resumes at the last position after a reconnect.

**Measured on real data (the 40,460 imported trades):**
- A 10,320-trade day loads in 0.51 s.
- Speeds are exact: 10× gave 30.3 s of market time in 3 s real; 50× gave 152.9 s.
- Seek and candle-step land exactly.

**CI fixes on `main` during this phase:**
- `next typegen` runs before `tsc`, because a clean checkout has no generated route types.
- All actions are pinned to commit SHAs, after trivy-action's unprefixed tag vanished.
- Actions moved off the deprecated Node 20 versions.
- Dependabot skips major versions.

**Caught by tests:**
- `PATCH` required `name` (the shared field definition had no default).
- An automatic token refresh fired with no session, causing a spurious 403.
- A layout change could be lost if the page reloaded before the autosave fired.
- Tests assumed per-user state that is now persisted, so they now set it explicitly.

**Verified:**
- 295 backend tests with the database
- 23 frontend unit tests
- 8 Playwright tests, including replay of a real session and workspace persistence (local storage is
  cleared before reload, to prove it is server-side)

**Still open:**
- Alerts (Phase 8, below).
- Replay across contract switches within one day (a session is replayed as a whole).
- Sharing workspaces within an organization.
- Workspace history / undo.

## 22. Phase 8 status (alerts)

Done on 2026-10-01.

**Domain** (`backend/app/domain/alerts`, pure, no I/O):
- Nine condition kinds:
  - trade conditions (price and CVD crossings)
  - candle-close conditions (delta, volume, stacked imbalance by the engine's rule, value-area break)
- Validation rejects unknown parameters.
- Crossing semantics: a newly loaded rule arms first and never fires on stale state.
- Cooldown is measured in market time, so the same tape always produces the same firings.
- `once` / `repeat` modes.
- `CandleCloseDetector` groups native candles exactly like the chart. A test checks every closed candle
  against the chart's own grouped bars (delta, volume, OHLC) at 1, 5 and 15 minutes.

**Persistence:**
- Migration `0004` adds `alert_rules`, `alert_events` and `notification_channels`.
- Events are unique on `(rule_id, dedup_key)`, so recording is idempotent: a firing is delivered once
  even if several API processes evaluate the same trade.

**Runtime** (`services/alerts/runtime.py`):
- Called on the tick path for each classified live trade. It is synchronous and cheap: evaluate, then
  put firings on a bounded queue.
- A background task records each firing, applies the per-user rate limit (20 a minute; over that, events
  are recorded as suppressed), and delivers in-app, then to webhooks concurrently.
- Rules update in place on API changes and fully reload every 30 s.
- Market state resets on a contract switch.
- Not evaluated in replay or for the startup restore.

**Delivery** (`services/alerts/delivery.py`):
- A `NotificationProvider` interface with in-app (stream `alert` message) and webhook implementations.
- Webhooks are HMAC-signed. The secret is derived from the server secret and channel id (never stored)
  and shown once.
- SSRF controls:
  - https and public addresses only in production
  - every resolved address is checked
  - the request is pinned to the checked IP, keeping Host and SNI, which defeats DNS rebinding
  - redirects are not followed
- 5 s timeout; 3 attempts on network errors, 5xx and 429.

**API and UI:**
- `/api/v1/alerts` (rules, events, channels, test delivery) with the new `alerts.read` permission.
  Ownership is checked like workspaces, with optimistic concurrency, limits and an audit trail.
- Terminal: an Alerts button with unread count; History, Rules (create, pause, delete) and Webhooks tabs;
  toasts; opt-in desktop notifications; an "Open alerts" command.

**Operations:**
- Metrics:
  - `ofmp_alert_rules_active`
  - `ofmp_alerts_fired_total`
  - `ofmp_alerts_suppressed_total`
  - `ofmp_alerts_dropped_total`
  - `ofmp_alert_deliveries_total`
- Prometheus alerts for webhook failures and dropped firings.
- `httpx` moved to the runtime requirements.

**Verified:**
- 336 backend tests with the database, including the trade → event → in-app and signed-webhook path,
  duplicate suppression, the rate limit, once rules disabling themselves, and ownership.
- 27 frontend unit tests.
- 9 Playwright tests, including a price rule created in the UI firing on live data (toast, history,
  unread count) and the webhook secret flow.
- A live smoke test on the synthetic feed: rules fired over the WebSocket, the webhook signature
  verified with the shown secret, and the metrics counted the firings.

**Still open:**
- Email and Telegram providers (the interface is ready).
- Rules for a specific contract (rules currently follow the active contract).
- Rate limits are per process (Redis-backed limits are already an open item).
- Alerts on order-book conditions (e.g. book imbalance), and per-user time windows (e.g. market hours
  only).

## 23. Phase 9 status (multi-process readiness, calendar, load test, legacy retired)

Done on 2026-10-01.

**Shared rate limits:**
- `RedisSlidingWindowLimiter` keeps one sorted set per key, updated in a single MULTI transaction.
  Refused attempts are not counted. The limiter interface is now async.
- `RATE_LIMIT_BACKEND=redis` is the default outside development. The login, alert-delivery and other
  limits now hold across every API process.
- Verified with two real API processes: an account got exactly 10 attempts in total, then both processes
  returned 429.
- If Redis fails, the limiter uses its in-process twin, logged and counted as
  `ofmp_ratelimit_fallbacks_total`. The client has a 0.5 s timeout, and Redis is skipped for 5 s after a
  failure.
- Verified with Redis frozen (`docker pause`): the first login took 1.1 s, later ones were normal speed,
  and shared limits resumed when Redis came back.

**NSE trading calendar:**
- `domain/market_data/calendar.py` reads its data from `calendars/nse.json`:
  - the 2026 holidays (15 weekdays), cross-checked against published NSE holiday lists; re-check against
    the official circular
  - room for special sessions such as Muhurat trading
- Outputs:
  - `ofmp_market_session_open` and `ofmp_market_calendar_days_left` (computed when Prometheus scrapes)
  - a `session` block in `/api/v1/market/status`
  - "Market closed · opens … IST" in the terminal
- Prometheus feed alerts now use the calendar (open for the last 5 minutes) instead of fixed weekday/UTC
  hours. A `MarketCalendarOutdated` alert covers the end of the file. promtool now runs in CI.

**Load testing:**
- `tools/loadtest.py` and `make loadtest`. Results are in `docs/load-testing.md`.
- At 200 connections × 4 charts (800 subscriptions): all connected, no errors, delivery lag p99 70 ms,
  engine p99 0.25 ms, about 0.3 MB per connection.
- The first run found and fixed two problems in the stream hub:
  - the push loop drifted (it slept after each push instead of keeping a fixed cadence)
  - every connection re-encoded the same snapshot (now encoded once per distinct snapshot)
- Result: delivery went from 1.74 to 1.85 per second against 2 nominal, lag p99 from 117 to 70 ms, and
  data volume fell 15%.

**Legacy retired:**
- Removed:
  - pages `/`, `/chart`, `/replay`, plus `static/`
  - sockets `/ws/frontend` and `/ws/chart`, and their broadcast loop
  - `/api/status`, `/api/contracts`, `/api/contract`, `/api/replay/*`
  - `LegacyAuthGuard` and the `AUTH_REQUIRED` setting
- `server.py` went from about 990 lines to 561.
- Redirects (308): the old pages go to `/app/`, and `/reset-password` and `/verify-email` go to their new
  `/app/` pages, keeping the token, so emails already sent still work.
- Ported:
  - create account, forgot password, reset password and verify email, into the Next.js terminal; email
    links now point to `/app/...`
  - contract switching: `POST /api/v1/admin/contract` (`admin.system`, audited) and **Make live** in the
    watchlist for admins
- The per-candle table moved to `domain/orderflow/table.py: candle_table`. The golden regression
  baselines still match byte for byte.
- The nginx image no longer copies legacy HTML; the CSP hashes cover only the terminal.

**Verified:**
- ruff clean; backend tests pass with the database.
- Frontend: typecheck, ESLint and 27 unit tests.
- 10 Playwright tests, including legacy redirects, forgot password, and reset/verify links with invalid
  and missing tokens.

**Still open:**
- In-app alert fan-out across API processes (Redis pub/sub), needed before running more than one.
- Image signing, off-site backup automation, replay across a mid-day contract switch.
- Measure capacity on the production host (Linux, uvloop).

## 24. Phase 10 status (market profile)

Done on 2026-10-01. Owner decisions:
- TPOs mark each period's high–low range (CBOT).
- Rows default to 5 points, adjustable per chart.
- The view switch is per chart.
- Periods are 30 minutes, with A at 09:15.

**Engine** (`domain/profile`, pure, incremental):
- Each period stores its tick range, and volume is stored per tick (buy/sell). That is enough to derive
  TPO rows at any row size when a snapshot is built.
- Calculations: TPO POC and volume POC; value area for each (same rule as the footprint's); initial
  balance and extensions; single prints and tails; HVN/LVN; session figures.
- New sessions roll over automatically, keeping the previous session for its POC/VAH/VAL. Trades
  outside the session are excluded.
- Tests:
  - the worked examples from the Market Profile notes (A/B/C letters; value area 24515–24530 at 70%
    of 38 TPOs)
  - parity with the footprint: buy, sell, volume and volume POC per row on the same trades
  - volume is conserved and the ladder stays contiguous at every row size

**Plumbing:**
- Live: fed from `server._on_tick`, with one profile per active contract.
- Restart: rebuilt from today's stored trades, plus the previous session from the database.
- Replay: fed through `replay_engine.apply_record`, with a parity test at arbitrary seek positions.
- Stream: `view`/`row` on `subscribe` and `replay`. Subscriptions are keyed by view, and the same row size
  is built once per push.
- History: `GET /api/v1/market/profile`.

**Terminal:**
- Left sidebar (the focused chart), the `M` key and command-palette entries.
- Per-chart `view` and `profileRow` in the workspace (layout format v3, migrated from v2).
- A canvas renderer: letters or blocks, value area, POC row, volume POC bar, IB bracket,
  single prints/tails, buy/sell volume histogram, HVN/LVN, previous-session lines, price axis, hover
  readout, scroll, and a figures strip.
- The stream client drops snapshots of the other view during a switch.
- Checked visually on a real recorded session (2026-09-29, 10,320 trades) at 5- and 2-point rows.

**Verified:**
- Backend tests pass with the database. Frontend: 31 unit tests.
- 11 Playwright tests, including one chart in profile view while the other stays a footprint, row size,
  persistence across reload, and the `M` toggle.
- The browser tests now set each chart's view explicitly, since views are saved per user.

**Still open:** composite (week/month) profiles, value/POC migration, day types, Market Profile alert
rules, and printed-only TPO marking as an option.

## 25. Multi-day market profile on one shared price axis

Done on 2026-10-08. It's a layer around the existing daily engine: the profile maths, classifier,
footprint, replay and golden baselines are unchanged.

**Backend:**
- `repositories.symbol_session_dates` / `untagged_session_dates` / `load_symbol_sessions`: lean,
  symbol-filtered, one query, no JSONB.
- `services/profile_history.ProfileHistory`: the contract policy (tagged sessions of the active contract
  only; untagged sessions excluded with a reason; nothing fabricated; per-day error isolation), per-day
  `SessionProfile` + `build_profile`, and a two-level LRU cache keyed by `PROFILE_VERSION`. Requests are
  logged.
- Gateway `profile_history` / `history_epoch`; `GET /api/v1/market/profile/history` with validation.
- Stream: `days` on `subscribe`; a `profile_history` message sent once, and re-sent only when the epoch
  (contract, live session) or the row size / `days` change. A generation token drops superseded sends.

**Frontend:**
- `lib/chart/multiDay.ts`: columns, absolute price grid, layout, viewport, and the mapper (`priceToY` /
  `yToPrice` / `sessionToX` / `xToSession`).
- `MultiDayProfileChart`: per-column session drawing; a fixed price axis; a header that scrolls with the
  columns; hit testing (session → price → that session's row); follow today.
- Per-chart `profileDays` (layout format v5). History lives on its own bus, separate from the 2 Hz
  snapshots.

**Verified:**
- 362 backend tests with the database, including 7 new history tests: independence, single-day parity,
  replay parity, contract filtering, untagged exclusion, missing days, cache, row sizes, error isolation,
  static history vs live today.
- 1 stream protocol test: sent once, resent only on change.
- 1 REST validation test.
- 40 frontend unit tests, including 7 coordinate-model tests: same price → same Y across days, different
  X, scrolling, hit testing, follow, split width.
- 11/11 Playwright tests on the synthetic feed. Against the live market, the alerts test can time out,
  because it needs a price crossing within 60 s.
- Checked visually on live NIFTY27OCT26FUT: 30 Sep beside today, on one axis, showing the gap down.

**Open:**
- Multi-day in replay.
- Continuous futures across expiries.
- A contract backfill for the untagged September days.
- A persistent profile cache.

## 26. Market profile pan and zoom (both axes)

Done on 2026-10-12. Frontend only.

**Model:**
- A continuous viewport (`topPrice`, `rowPx`, `leftX`, `colScale`, `follow`) replaces integer scrolling.
- Pure, tested operations: zoom anchored at the cursor on each axis, pan, clamping (half a screen past the
  data, short ranges centred, narrow content right-aligned), fit, and follow.
- Column width is strictly proportional to `colScale`, so cursor anchoring is exact.

**Interaction:** `useProfileViewport` maps drag, wheel, Ctrl+wheel/trackpad pinch, touch pinch, axis drags
and wheels, double-clicks and keys to viewport actions. Redraws are coalesced to one per animation frame.

**Rendering:**
- The single-day chart now uses the same renderer (one column). The old `MarketProfileChart` was removed.
- Previous-day reference lines are kept in single-day mode.
- Level of detail: letters → blocks → thin bars; volume bars → vPOC tick; round axis label steps; sticky
  date labels; replay is labelled.
- Zoom levels are saved per chart (layout format v6).

**Verified:**
- 42 frontend unit tests, including 9 viewport tests: cursor anchoring on both axes, pan without zoom
  change, the same Y across days after any pan or zoom, clamping/centring, fit, follow, level of detail,
  label steps.
- 12/12 Playwright tests, twice, including a gesture test: drag goes free, double-click follows, Ctrl+wheel
  zoom is saved and survives a reload, `0` fit, `F` follow, the buttons, and hover after zoom.
- Backend unchanged: 363 tests passed.

**Not done:** a cached history layer (offscreen canvas). Culling plus frame-coalesced drawing was enough
in testing. Per-day column resizing wasn't requested.

## 27. Multi-day order-flow footprint

Done on 2026-10-12.

**Owner decisions:** CVD resets each session; day caps 1 m → 5, 3 m → 10, 5 m and above → 20.

**Backend:**
- The footprint payload builder moved to `backend/app/domain/orderflow/chart.py` (`chart_payload`), shared by
  the live stream and history, with an optional `[since, until)` window and no bar limit.
- `services/footprint_history.py` (`FootprintHistory`):
  - lists the active contract's finished sessions (untagged days excluded with a reason);
  - folds a day's stored trades through `restore_trades` once and caches the state (LRU 64);
  - caches each built payload by date, row size and interval (LRU 512), so changing interval or row size
    never reloads trades.
- Sides are stored and never re-classified.
- Endpoints: `GET /api/v1/market/footprint/sessions` and `/history` (see `docs/api.md`). Finished days are
  immutable and ETag-cached; today comes from the live engine (`no-store`). Gzip is on for responses over
  4 KB (a full 1 m day at 1 ₹/row is about 200 KB raw).

**Frontend:**
- `FootprintHistoryLoader` loads the list, today, then the newest finished day; older days load one at a
  time when fewer than 20 columns are left of the view (checked after wheel, pointer-up and key events,
  since the chart library has no range event).
- One `Footprint` primitive per session, each with `cvdOffset` 0: session-reset CVD with no library
  change. The newest layer scales the price axis on the visible columns; the others return no autoscale.
- Prepending a day shifts the visible range by the number of added columns, so the view does not move.
- `SessionMarks` draws the separators and sticky date labels.
- Per-chart `footprintDays` (0/5/10/20) is saved in the workspace (layout v7).

**Verified:**
- Backend: 370 tests passed, including `tests/test_footprint_history.py` (each day equals a fresh build;
  contracts never mix; caching; caps; gateway parity with replay; today from the live engine) and the
  endpoint test (auth, caps, ETag/304, gzip, no-store, error codes). Golden regression unchanged.
- Frontend: 47 unit tests (caps, IST dating, merge, session building, loader order and cap, labels);
  13/13 Playwright tests, including a multi-day footprint test.
- Checked by hand with two real stored sessions (30 Sep, 08 Oct, served to the browser through request
  mocks): separators, sticky labels, TODAY, autoscale per view, view kept when a day is prepended, 30 Sep
  loaded on reaching the left edge, then "Start of recorded history".

**Limitations:** replay shows one session. Days recorded before contract tagging are excluded. Volume
profile/statistics across several days are not part of this change.

## 28. Footprint table: Volume, VPOC, VPOC Vol, COT, OI, dOI

Done on 2026-10-13.

**Owner decisions:**
- COT = each candle's buy − sell volume.
- dOI on a session's first candle compares with the previous session's close.
- Store OI with each trade and recompute the rest.
- VPOC drawn as a pink line on the candles.

**Storage:**
- `trades.open_interest` (migration 0005) is written from the tick that produced each trade. Ticks
  already carried OI (Angel One SnapQuote) and `raw_ticks` stored it, but trades did not.
- The migration fills stored trades from `raw_ticks`.
- Volume, VPOC and COT stay derived from the stored trades, so they cannot drift from the chart.

**Engine:**
- `Trade.open_interest` and `Footprint.candle_oi` hold each native candle's closing OI: the last trade in
  it that carried one.
- Live ticks, the startup restore, replay (from the stored column when the record predates `oi`) and history
  all feed it.
- The observation record gained `oi`.

**Payload:** `chart_payload` adds `volume`, `cot`, `vpoc`, `vpoc_volume`, `oi` and `doi` to every display
bar.
- VPOC is computed on the grouped bar's cells at the row size. Its tie rule (higher price) matches the
  chart library's POC, so the pink line and the table agree.
- dOI compares with the latest earlier native candle the engine holds, inside the window or not. For the
  first candle it uses `oi_before`: the previous stored session's closing OI, found by
  `last_open_interest_before` (live: at restore; history: per day; replay: at open).
- `FOOTPRINT_VERSION` is now `footprint-v2`, so browsers drop days cached without these fields.

**Frontend:**
- `FootprintTable` primitive: fixed row labels; cells aligned to the footprint columns; full → compact →
  dot as columns narrow.
- COT and dOI are coloured by sign and the VPOC rows are pink.
- Hovering a cell shows exact values. Units follow Qty/Lots.
- The price scale's bottom margin is set to the table height, so candles stay above it.
- Toolbar: Table toggle plus a ▾ row picker (a fixed popup, since the toolbar scrolls). Saved per chart,
  layout v8.

**Verified:**
- Backend: 380 tests.
  - Golden digests are unchanged with the new fields excluded, so every existing output is identical.
  - New `tests/test_footprint_table.py`: VPOC tie rule, closing OI and dOI at 1m/5m, a limited window's
    first bar, live = replay = restore at 1/3/5 m, trade rows, OI round trip and the previous-close baseline,
    the 0005 backfill rules, history days using the baseline.
- `alembic check`: no drift.
- Frontend: 51 unit tests; 14/14 Playwright tests, including a table test (rows saved across a reload,
  toggle, the fields present).
- Checked by eye with real 08 and 12 Oct data at 5 m.

**Limitations:**
- OI changes in steps (88 changes on 13 Oct to ~14k ticks), so 1 m dOI is often 0.
- The baseline is the previous *stored* session: with 09 Oct not recorded, 12 Oct's first dOI is measured
  against 08 Oct.
- Days without stored raw ticks (30 Sep) have no OI.
- Futures OI only.

## 29. Daily broker session (Angel One login every trading morning)

Done on 2026-10-13. Hosting is deferred (owner decision); alerts go to the existing webhooks plus the log;
times are the plan's defaults (08:30 login, retries to 09:10, feed off 15:45).

**Why:**
- Angel tokens expire at midnight (seen today: the saved token was valid until 2026-10-14 00:00).
- The process logged in only when the feed reconnected.
- A cancelled token that still looked unexpired could be reused forever.
- The contract and the trading day changed only on restart.
- A failed login was noticed only from an empty chart.

**What:**
- `services/broker_session.py`: the NSE-calendar schedule (see `docs/broker-session.md`), on an injectable
  clock.
- Server hooks:
  - forced login checked by `getProfile`;
  - start of day (fresh instrument master, pinned-or-front-month contract, `_activate_contract`);
  - `_stop_feed`;
  - admin notices (in-app plus webhooks).
- Startup outside market hours restores the engine without a login and leaves the feed off.
- `angel_client`:
  - `ensure_valid_token(force_login)`;
  - `mark_token_invalid` (the feed's 401/403 handshake path);
  - a lock-out guard (3 failures → 15-minute pause, `LoginBlocked`);
  - request timeouts;
  - owner-only token file;
  - error messages carry Angel's message and code, not the whole response.
- Admin **Make live** survives the next morning while the contract is unexpired.
- API: `broker` in market status (error details for admins only), `broker_session` in admin system,
  `POST /api/v1/admin/broker/relogin` (audited).
- CLI: `broker-login [--check]`. Metrics for logins and token expiry.
- Frontend: top-bar chip plus admin **Re-login**.
- Fixed: the multi-day status box covered the footprint table's labels; it is now at the top left.

**Verified:**
- 393 backend tests. New: 12 broker-session tests on a fake clock:
  - Fri→Wed with a weekend and a holiday;
  - two failures then success;
  - giving up at 09:10 (21 attempts) and retrying the next morning;
  - a 10:00 start with its 40-minute window;
  - manual re-login after giving up and in the evening;
  - roll notice and watchdog;
  - feed off when found running after the close;
  - forced login and dead-token rules;
  - the lock-out guard;
  - 401/403 vs 503 on the handshake;
  - pinned vs expired vs front-month contract.
  Plus API tests (trader vs admin view, 403s, CSRF, audit).
- The schedule found and fixed a lost-wake-up bug: a re-login arriving just before a wait was dropped.
- Server startup smoke test (Angel mocked):
  - after the close: engine restored, feed off, next login tomorrow 08:30;
  - inside the window: forced login → check → fresh master → feed on.
- `broker-login --check` against Angel: OK (token valid until 00:00).
- Frontend: 55 unit tests, 15/15 Playwright tests (new: top-bar states and admin re-login).

**Not done (with hosting):** running the process as a service or container, moving the schedule into the
ingest worker for `INGEST_MODE=redis`, and an email/Telegram channel (webhooks can bridge to them).

## 30. Market profile levels, cross-session levels and day structure

Done on 2026-10-21 in three phases, each committed separately.

**Owner decisions:**
- HVN ≥ 70% of the VPOC row's volume;
- naked POCs from the VPOC only, looking back 20 sessions;
- composites of 5 / 10 / 20 days, this week or this month;
- no IB30/IS30;
- no gamma (no options data).

**Phase 1, levels:**
- engine: volume per period;
- `build_profile`: exact VWAP, TWAP, HVN v2 bands, IB extensions, the DPOC trail, weak highs and lows, and the
  previous session's volume VA, VWAP, high and low;
- `profile-v2`.

**Phase 2, across sessions:**
- `domain/profile/levels.py`: naked POCs and composite profiles;
- `GET /api/v1/market/profile/levels`, served from the cached profile history.

**Phase 3, day structure:** `domain/profile/structure.py` (shape, double distribution, open drive, the 80% rule,
volume clusters), in every profile under `structure`.

**Frontend:**
- `lib/chart/profileLevels.ts` turns a profile into drawing data (shading, lines, trail, weak edges, boxes) plus
  labels and hover names; it is unit tested;
- the renderer only paints that data;
- Levels menu and Composite selector per chart (layout v9);
- a checkbox menu shared with the footprint table rows.

**Verified:**
- 409 backend tests (24 new: every rule, plus the levels endpoint);
- 62 frontend unit tests;
- 16/16 Playwright tests (Levels menu saved across a reload, composite request);
- real sessions:
  - 13 Oct replay: VPOC 22790, TPOC 22615, VWAP 22694.51, 2× IB up, b-shape, one absorption cluster;
  - nPOCs on 08, 12, 13, 16 and 21 Oct; 30 Sep's was correctly visited by 13 Oct;
  - the 5-day composite: VPOC 22290, VWAP 22664.67.

**Notes:**
- Shapes use the TPO distribution (time), so 13 Oct is a "b" even though its volume POC sits high.
- The 80% rule uses the previous session's volume value area.
- A day missing from the database (09 Oct) shifts "previous session" and nPOC ages to the stored sessions.
