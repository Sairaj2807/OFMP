# Order-flow engine

The engine lives in [`backend/app/domain/orderflow/`](../backend/app/domain/orderflow/). It is pure
Python: it imports no web framework, database, broker client or app config, and a test
(`tests/test_orderflow_domain.py`) enforces that. Live ingestion, replay and research all run the same
code.

## Modules

| Module | Contents |
|---|---|
| `settings.py` | Default engine parameters. `config.py` re-exports them, so there is one source of truth. |
| `models.py` | `Trade`, `FootprintCell`, `FootprintRow`, `TradeSide` |
| `orderbook.py` | `OrderBook`: best-5 book, replaced wholesale per Snap Quote tick |
| `volume.py` | `CumulativeVolumeExtractor`: trade quantity from the cumulative volume counter |
| `classification.py` | `TradeClassifier` protocol, `ClassificationContext`, `Classification`, and the classifiers |
| `registry.py` | Registered classifiers and versions, the default, and engine stage versions |
| `footprint.py` | `Footprint`: native 60 s candles × tick-size price levels, OHLC, running min/max delta |
| `analytics.py` | POC, value area, stacked imbalances (pure functions over rows) |
| `bars.py` | Grouping native candles into display intervals |
| `cvd.py` | `CVDTracker` |
| `pipeline.py` | `TickProcessorState`, `process_tick`, `advance_candle`, `TradeEvent` |

## Pipeline

```
tick → book update → crossed-book check → quote-change tracking → volume extraction
     → classification → footprint + candle rollover + CVD → TradeEvent (optional callback)
```

`process_tick(state, tick, on_trade=None)` returns what happened to the tick. The optional
`on_trade(TradeEvent)` callback receives everything known about each classified trade.
[`research/observation_features.py`](../research/observation_features.py) turns that event into the
observation record that is written to `observations.jsonl`.

`orderbook_engine.py` at the repo root is a compatibility layer. It keeps the old import names and the
old `observation_sink` callback (which receives the flat observation dict). New code should import from
`backend.app.domain.orderflow`.

## Classifiers and versioning

| name / version | Rule | Status |
|---|---|---|
| `vtrender_reconstruction` / `v1` (default) | Price below mid → BUY, above (or at) mid → SELL. Tick-rule fallback when the quote is ≥ 14 s stale or one book side is empty. | Validated: 54/55 on the verified set (trade 25 is the known miss) |
| `lee_ready` / `v1` | Textbook quote rule, then tick rule | Reference only (13/55) |
| `tick_rule` / `v1` | Tick rule only | Reference only |

Classifiers are stateless. The previous trade's price and side arrive in `ClassificationContext`, and
the pipeline owns that history. Every `Classification` carries `classifier_name` and
`classifier_version`, and every observation record stores them.

**Rule:** a classifier's output never changes under an existing version. To change behaviour:

1. Add a new class, e.g. `VtrenderReconstructedClassifierV2`, with `version = "v2"`.
2. Register it in `registry.py`.
3. Keep v1 untouched. Stored data records which version produced it, and the live-session golden test
   re-classifies each record with the version named in it.
4. Changing the default is a separate, deliberate step, done in `registry.DEFAULT_CLASSIFIER`.

Replay never re-classifies. It uses the side stored with each record, so a past session always replays
exactly as it was recorded.

## Known issue in v1: ties at the midpoint

The midpoint is computed in floating point, so a trade printed exactly at mid can fall on either side.
For example, `(99.9 + 100.2) / 2 == 100.05000000000001`, so a print at 100.05 is treated as below mid
and classified BUY.

In the live sessions of 22–30 Sep 2026:
- 418 of 40,210 midpoint-rule trades (1.04%) printed exactly at mid;
- 407 of those were classified SELL;
- 11 were classified BUY because of rounding.

This is pinned by a test and kept as-is for reproducibility. A fix (exact paise arithmetic) must ship as
v2. Separately, the at-mid → SELL behaviour itself has never been validated against Vtrender.

## Regression protection

| Test | Guards |
|---|---|
| `tests/regression/test_vtrender_classifier.py` | All 55 verified trades, pinned per row (side + rule); accuracy exactly 54/55 |
| `tests/regression/test_live_sessions_golden.py` | Every stored live trade re-classifies to its stored side and rule, using the classifier version recorded on it |
| `tests/regression/test_engine_golden.py` | SHA-256 of all engine output on seeded tick streams with crossed books, volume resets, one-sided books and stale quotes: per-tick results, observations, chart and table payloads, replay. Pinned against the pre-refactor engine. |
| `tests/test_orderflow_domain.py` | Classifier rules, registry, volume extraction, trade events, domain isolation |

A one-character change to the v1 tie rule (`<` → `<=`) fails 9 of these tests.
