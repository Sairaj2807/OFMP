# Verifying against Vtrender's Orderflow Replay

> **Status (2026-09-30):** Arrow support has been removed; the platform runs on Angel One only. Arrow-specific parts below (exact-side `btv`/`atv` records, `arrow_trades.jsonl`, `contract_resolver.py`, `ARROW_*` settings) are historical. The engine now lives in `backend/app/domain/orderflow/` — see [docs/orderflow-engine.md](docs/orderflow-engine.md).

Companion to [TRADE_CLASSIFICATION.md](TRADE_CLASSIFICATION.md), which documents the classification
algorithm itself. This document covers *how the algorithm's output gets checked against Vtrender* —
specifically, using Vtrender's **Orderflow Replay** feature instead of watching Vtrender live.

## Why replay instead of live

The original verification loop required a human to watch this dashboard and Vtrender simultaneously,
trade by trade, in real time — [review_cli.py](review_cli.py)'s `--live` mode tails the log as it's
written and blocks on a keypress per trade. That's fragile (miss one trade, you're desynced) and slow
(you're rate-limited by the market, not by how fast you can actually verify).

Vtrender's Orderflow Replay lets you re-play a past session on its own clock — paused, stepped,
scrubbed, rewound. That decouples the two systems in time: this project just needs to **record**
during market hours, and verification can happen **any time after**, at whatever pace the reviewer
wants, against Vtrender's replay of the same date/instrument.

## Storage: partitioned by trading day

`data/sessions/<YYYY-MM-DD>/observations.jsonl` and `.../labels.jsonl` (see [config.py](config.py)'s
`session_dir`/`observations_path`/`labels_path` and [observation_store.py](observation_store.py)'s
`ObservationStore`). Everything from a single calendar day lives together so a reviewer can load
"just this session" independent of when the server happened to be running. `trade_id` is unique
*within* a day, not globally — `(session_date, trade_id)` is the real key once multiple sessions exist,
which is why the exported CSV ([export_dataset.py](export_dataset.py)) carries a `session_date` column.

## Two ways to verify against a replay

### 1. `review_cli.py` — trade-by-trade CLI

```
python review_cli.py                    # today's session, replay/seek mode
python review_cli.py --date 2026-08-05  # a past session
python review_cli.py --live             # old behavior: tail today's live feed
```

Replay mode loads the whole day up front and lets you navigate freely:

- `n` / `p` — next / previous trade
- `g` — jump to a wall-clock time (`HH:MM:SS`) — use this to sync to wherever you've scrubbed
  Vtrender's replay to
- `a` / `b` (`A` / `B` for + a note) — label the current trade BUY / SELL as shown in Vtrender
- `s` — skip, `q` — quit

Each trade's display includes a **"Next Trade"** line — the gap in seconds to the next print and its
price/side — so you know exactly how far to advance Vtrender's replay before looking for the next
match, instead of hunting for it by eye.

### 2. `/replay` — visual footprint replay

The live dashboard ([server.py](server.py), `/`) has a sibling view at `/replay`
([static/replay.html](static/replay.html)) that reconstructs the **whole footprint chart** — not just
one trade — from a saved session, via [replay_engine.py](replay_engine.py). Pick a session date, then
scrub/play/step through it next to Vtrender's own Orderflow Replay for the same date, comparing whole
candles (delta, POC, value area, imbalance stacks) at a glance instead of confirming one print at a
time. Drop into `review_cli.py --date <date>` for the specific candles that visibly disagree.

Backing endpoints (all read-only, never touch the live engine):

- `GET /api/replay/sessions` — dates with at least one logged trade
- `GET /api/replay/<date>/meta` — time range, trade count, tick size
- `GET /api/replay/<date>/snapshot?as_of_ms=<ms>&ppr=<1-5>` — footprint/book state as of that instant,
  in the same shape the live `/ws/frontend` feed uses (`_candles_payload` in [server.py](server.py) is
  shared by both)

## Time-sync anchor

`ts_ms` in every observation is Angel One's `ltt` (last-traded-time, exchange clock — see
[ws_ingest.py](ws_ingest.py)). Vtrender's replay should also be keyed to exchange time. Before trusting
a whole session's alignment, sanity-check 3-4 unambiguous prints (large size, clean up/downticks)
against what Vtrender's replay clock shows at that exact print. If there's a constant offset (feed
latency, clock skew), it should show up as the same delta on all four — note it and mentally adjust,
or treat it as a review-tool bug if it isn't constant.

## What replay does *not* reconstruct exactly

[replay_engine.py](replay_engine.py) rebuilds the footprint **exactly** — it's pure aggregation over
already-classified trades (`ts_ms`/`ltp`/`qty`/`algo_side`), so delta/POC/value-area/imbalances all
match what the live engine computed at the time.

The **order book** shown during replay is not continuous. Only the depth snapshot attached to each
*trade* tick was ever persisted (`observation["bid_levels"]`/`["ask_levels"]`) — quote-only ticks with
no trade were never logged, since [orderbook_engine.process_tick](orderbook_engine.py) only calls the
observation sink when `qty > 0`. So the replay book is always "as of the most recent trade at or before
the cursor," not a tick-by-tick reconstruction. This doesn't affect classification verification (the
algorithm itself only ever reads the book at trade instants — see TRADE_CLASSIFICATION.md §3.1) but
don't read it as a live-quality L2 replay between prints.

## Arrow sessions and the chart page

Replay reads either saved schema: `observations.jsonl` (Angel, one classified trade per record) or
`arrow_trades.jsonl` (Arrow, `buy_qty`/`sell_qty`, up to two trades per record). The file matching
`DATA_SOURCE` wins if both exist ([observation_store.py](observation_store.py)
`session_records_path`). Before this, an Arrow session was listed by `/api/replay/sessions` but
reported no trades. Arrow records carry no `algo_side`, so the trade card shows the feed's side
("BUY", "SELL", or "BUY+SELL" for a coalesced update).

The same sessions can be scrubbed on the order-flow chart at `/chart?mode=replay`
(see [ORDERFLOW_CHART.md](ORDERFLOW_CHART.md)); its jump-to-time box takes IST.

## Labels stay day-scoped, export combines everything

`export_dataset.py` now walks every session under `data/sessions/` (or one, with `--date`) and writes
a single combined `data/verified_dataset.csv` with a `session_date` column, so downstream analysis
(error clustering, rule discovery) isn't limited to one day's sample.
