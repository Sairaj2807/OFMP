# Order-flow chart (`/chart`)

> **Status (2026-09-30):** Arrow support has been removed; the platform runs on Angel One only. Arrow-specific parts below (exact-side `btv`/`atv` records, `arrow_trades.jsonl`, `contract_resolver.py`, `ARROW_*` settings) are historical. The engine now lives in `backend/app/domain/orderflow/` — see [docs/orderflow-engine.md](docs/orderflow-engine.md).

A footprint chart drawn by OpenAlgo Charts' `Footprint` primitive from the engine's
data ([orderbook_engine.py](orderbook_engine.py)). Live at `/chart`, saved sessions at
`/chart?mode=replay`. The engine stays the source of truth for trades, sides, delta and
CVD; the library only draws.

## Data flow

```
process_tick (unchanged classification) ─► Footprint cells, OHLC, delta, min/max delta, trades
        │                                  (always at the NATIVE 60s interval — see below)
        └─ server._chart_payload(..., interval_sec)   ONE serializer
             │    └─ orderbook_engine.group_native_bars   groups native bars into interval_sec
             ├─ /ws/chart                        live, every 0.5 s, {"ppr": n, "interval": secs}
             └─ /api/replay/{date}/chart         replay, ?as_of_ms=&ppr=&interval=  (same "chart" object)
                  └─ static/orderflow.js  ── static/orderflow_adapter.js (engine -> library)
                       ├─ transparent candlestick series (time axis + autoscale)
                       ├─ Footprint primitive (ladders, POC, value area, imbalances, cards, table)
                       └─ order-book panel (render.js, shared with the old pages)
```

`/ws/frontend`, `/replay`, `/api/replay/{date}/snapshot` and the old pages are unchanged
(verified byte-identical against a pre-change capture, re-verified after the interval feature).

## Candle interval selector

The "Interval" control on `/chart` offers 1/3/5/15/30 minutes
(`config.ALLOWED_CHART_INTERVALS_SEC`). The engine itself **never** buckets trades at more
than one granularity — `Footprint.add_trade` always buckets at the native interval
(`config.CANDLE_INTERVAL_SEC`, 60s), live or in replay, exactly as before this feature. Coarser
display candles are assembled purely from already-aggregated native bars at payload time
(`orderbook_engine.group_native_bars`), so the hot tick path is untouched.

Grouping merges cells by summing buy/sell per price, takes the first/last native bar's
open/close and the group's high/low, and **chains** min/max delta rather than min/maxing the
raw per-native-bar values — each native bar's min/max already describes its own running-delta
path zeroed at that bar's start (`Footprint.candle_stats`), so the merged path's extremes are
`offset_before_i + sub.min_delta` / `offset_before_i + sub.max_delta`, `offset_before_i` being
the cumulative sum of earlier sub-bars' own final delta. A field missing on any sub-bar in a
group drops that field from the merged bar rather than fabricating a value.

`server._clamp_chart_interval` snaps any other value to the nearest allowed one (ties favour
the smaller). `MAX_CANDLES_KEPT` (native-candle retention, live pruning and replay's own
rollover) was raised from 120 to 400 (~a full NSE session) so a 30-minute view still has a
useful number of bars, and long replay scrollback works at any interval.

`/ws/chart`'s per-client state became `(ppr, interval_sec)` instead of a bare ppr int
(`server._parse_chart_message`); `/ws/frontend` keeps its own separate parser
(`_parse_ppr_message`) with the exact original semantics, including the pre-existing quirk that
a bare `{}` message resets ppr to 1 rather than leaving it alone — pinned down by
`tests/test_chart_interval.py`'s parse-function test tables since that logic no longer lives
inline in the socket loop.

## Payload (`chart` object)

`{ row_size, interval_sec, cvd_offset, bars: [ { time, open, high, low, close, delta, min_delta, max_delta, trades, cells: [ { price, buy, sell } ] } ] }`

Engine vocabulary (buy/sell), rows labelled by their price-band **floor**, newest last.
Replay adds `session`, `cursor_trade`, `prev_trade`/`next_trade`, gaps and `lot_size`; both
modes carry `source` (`angel`: sides inferred by the midpoint rule, `arrow`: feed btv/atv),
`book`, and live also `status` and `quote`.

## Engine -> library mapping (only in `orderflow_adapter.js`)

| Engine | Library | Note |
|---|---|---|
| `buy` | `askVol` (right, green) | BUY is the project's Vtrender-polarity label, **not** "aggressor at the ask" |
| `sell` | `bidVol` (left, red) | keeps library delta (ask - bid) == engine delta (buy - sell) |
| row price (band floor) | band **centre** (`floor + row/2`) | the library centres a cell on its price; tooltip shows the real band |
| `min_delta` / `max_delta` / `trades` | `minDelta` / `maxDelta` / `tradeCount` | tracked by `Footprint.candle_stats`; absent stays absent, never 0 |
| `cvd_offset` | `cvdOffset` | CVD before the first bar sent; the library adds each bar's delta |

`trades` counts `Trade` objects, so a coalesced Arrow update carrying both sides counts as 2.
Min/max delta inside such an update (about 6% of the saved Arrow session) depends on the
engine adding BUY before SELL; the true intra-update order is unknown.

## Decisions taken

1. **Library built outside the vendored folder** (scratch copy, `npm ci && npm run build`);
   only `openalgo-charts.mjs` and `openalgo-charts.profile.mjs` (+ LICENSE/NOTICE) live in
   `static/vendor/openalgo-charts/`. `openalgo-charts-master/` is untouched.
2. **Value area, POC, imbalances and stacks are drawn with the library's definitions.** The
   engine's own math is unchanged and still feeds the old pages.
3. **CVD on the chart includes the forming candle** and is labelled "since server start"
   (live) / "since first saved trade" (replay). The engine's tracker counts closed candles
   only; the old page's "Session CVD" (always 0, see KNOWN below) is unchanged.
4. **Rows keep the engine's floor-band grid** (1-5 price/row), drawn at the band centre.
5. **Replay accepts both saved schemas** (`observations.jsonl` and `arrow_trades.jsonl`) and
   reuses the engine's own candle rollover (`_advance_candle`), so live and replay can't drift.
6. **All three pages now offer the interval selector.** `/` and `/replay`'s footprint TABLE
   (`server._candles_payload`) grouped differently from `/chart`'s footprint PRIMITIVE
   (`_chart_payload`/`group_native_bars`) — see "Interval selector on the old pages" below.

## Engine vs library: measured differences

`node --test tests/js/adapter.test.mjs` prints these on the saved 2026-08-12 Arrow session
(57 candles, 2 price/row). Asserted equal: volume, both side totals, delta, min/max delta,
trade count, CVD (full and sliding window), POC. Reported, **not** asserted:

| | Result |
|---|---|
| Value area | identical on 57/57 candles |
| Stacked imbalances | engine flags stacks in 6 candles, library in 12; none identical |

Why stacks differ (from reading both implementations): the library's sell imbalance compares
`bid[P]` with `ask[P+1]` (the row above), the engine's compares `sell[P]` with `buy[P-1]`
(below); the library flags positive volume against a zero opposing row, the engine never does;
the library requires adjacent rows, the engine joins flagged rows even when unflagged rows sit
between them (reproduced with synthetic data). Which definition matches Vtrender is **not
established** by any data in this repo.

## Interval selector on the old pages (`/`, `/replay`)

`/` and `/replay`'s footprint TABLE computes POC, value area and stacked imbalances
**server-side** per candle, via `Footprint.poc`/`value_area`/`stacked_imbalances` — unlike
`/chart`, which hands raw cells to the JS library and lets it compute those client-side. So
grouping several native (60s) candles into one coarser display candle needs the analytics
themselves to run over the MERGED rows, not just merged cell dicts. That's what the following
enables, none of it changing the engine's hot tick path:

- **`orderbook_engine.poc_from_rows` / `value_area_from_rows` / `stacked_imbalances_from_rows`**:
  the POC/value-area/stacked-imbalance math extracted into pure functions over a `FootprintRow`
  list. `Footprint.poc`/`value_area`/`stacked_imbalances` now just delegate to these with
  `self.get_candle_rows(candle_ts, ppr)` — same signatures, same behavior, verified
  bit-identical against the pre-refactor output on the real Arrow session (0 mismatches across
  12 ppr/limit combinations) before anything else in this section was built on top of it.
- **`Footprint.merged_candle_rows(candle_ts_list, ppr)`**: several native candles' rows, summed
  by price into one price-descending list — feeds straight into the three functions above.
- **`orderbook_engine.group_candle_timestamps(ts_list, interval_sec, native_sec)`**: buckets
  native timestamps (not pre-built bar dicts) into interval-wide groups. `/chart`'s
  `group_native_bars` groups already-serialized bar dicts instead, because its per-bar fields
  don't need a second pass through the Footprint to recompute — the old table's do.
- **`server._candles_payload(fp, cvd_by_candle, ppr, interval_sec=..., limit=...)`**: groups the
  native window (`limit * multiple` candles), then per group: `merged_candle_rows` ->
  `poc_from_rows`/`value_area_from_rows`; delta summed across the group's native candles;
  `close_row`/`bullish` from the group's first/last native candle's OHLC (open from the first,
  close from the last); `cvd` from the LAST native candle in the group that has actually
  **closed** (`cvd_by_candle` only has entries for closed candles — a group's later native
  minutes may still be forming even if its earlier ones closed); `imbalances` computed over the
  newest DISPLAY group (however many native candles that spans), not just the newest single
  native candle as before.
- Adds one key, `interval_sec`, to the `footprint`/`chart` payload shape. At the default
  (native) interval, every other field is unchanged — checked directly against a captured
  pre-refactor baseline, and via the old-endpoint regression harness (`_build_snapshot`,
  `/api/replay/{date}/snapshot`), both confirmed additive-only (only the new key differs).
- **`/ws/frontend` and `/ws/chart` now share one message format and parser**
  (`{"ppr": n, "interval": seconds}`, `server._parse_ppr_interval_message`), replacing
  `/ws/frontend`'s old ppr-only `{"ppr": n}` protocol. This is an intentional wire-protocol
  change: the old socket's one quirk (a bare `{}` message reset ppr to 1, rather than leaving it
  alone) does not carry over, since both fields now travel together and either one missing from
  a message keeps the current value instead.
- `static/app.js`/`static/index.html` and `static/replay.js`/`static/replay.html` gained the
  same "Interval" `<select>` as `/chart`'s toolbar, persisted the same way (`localStorage`).
  `render.js`'s table/badge rendering needed no changes — it already only ever reads the
  candle/imbalance shape generically (`c.ts`, `c.rows`, `c.poc`, ...), with nothing hardcoding a
  1-minute assumption.
- `style.css`'s `#ppr-select` rule was ID-scoped, so the new `#interval-select` needed adding to
  it explicitly (`#ppr-select, #interval-select { ... }`) to pick up the same styling — caught
  by a real browser screenshot, not by any unit test, which is exactly the class of bug the
  "host-side workarounds" list below was already tracking one instance of.

## Host-side workarounds for library behaviour

- `Footprint.autoscaleInfo()` reports the extent of every loaded bar; the server sends up to
  60 columns, so visible ladders were squashed into a sliver. `orderflow.js` overrides the
  instance method to report only columns in or next to view.
- The chart keeps its distance from the right edge when data changes; when the user has panned
  away, `applySnapshot` puts the same columns (by time) back.
- The library's table rows "Total Ask/Bid Volume" are fixed strings that would mislabel our
  convention, so they are not offered. Sums come from the tooltip ("Buy volume" / "Sell volume").

## Running and testing

```
python server.py                      # then open /, /replay or /chart  (needs a live feed; market hours)
python -B -m pytest -q                # engine stats, payload, replay, interval grouping (102 tests)
node --test tests/js/adapter.test.mjs # adapter + the real built Footprint (needs python on PATH)
```

Rebuild the library: copy `openalgo-charts-master/` (minus `website`, `node_modules`) elsewhere,
`npm ci && npm run build`, copy the two `.mjs` files into `static/vendor/openalgo-charts/`.

Verified: unit and parity tests above (each significant piece of grouping/analytics logic was
also deliberately mutated at least once to confirm its test actually fails without it, not just
passes with it); plus real Chrome runs against an offline harness that streams a saved session
through the real `server._on_tick` — `/chart` (live stream, follow-live, pan hold, controls,
table, themes, interval switching, replay stepping/jump/play/scrub) and `/` + `/replay` (interval
switching, reload persistence, imbalance badges, replay trade-stepping unaffected by interval).

**Not verified:** a live broker feed (market closed; Angel/Arrow logins were not exercised);
the Angel-inferred path in a browser (only in unit tests); Firefox, Safari, touch/mobile.

## Semantics to keep in mind

Whether Arrow's `btv` corresponds to Vtrender's BUY is unverified (see the badge on the page and
the earlier project report). The chart shows what the engine says; the badge shows where it
came from.

KNOWN (unchanged, out of scope here): the old dashboard's "Session CVD" always shows 0 (it reads
the open candle's `cvd`, which is `None`); the engine's stacked-imbalance run logic does not
require adjacent rows.
