# Contract auto-roll and manual switching

> **Status (2026-09-30):** Arrow support has been removed; the platform runs on Angel One only. Arrow-specific parts below (exact-side `btv`/`atv` records, `arrow_trades.jsonl`, `contract_resolver.py`, `ARROW_*` settings) are historical. The engine now lives in `backend/app/domain/orderflow/` — see [docs/orderflow-engine.md](docs/orderflow-engine.md).

Fixes the recurring "no NIFTY FUTIDX contract found expiring in SEP" crash: the server used
to require an exact expiry-month match (`config.INSTRUMENT_EXPIRY_MONTH`), which goes stale the
moment that month's contract rolls off the scrip master. Now it always resolves "whichever
contract hasn't expired yet and expires soonest" — a question that's never stale — and a
dropdown on `/` and `/chart` (live mode only) lets you switch to a different upcoming expiry
without restarting the server.

## Auto-roll

[angel_client.py](angel_client.py):
- `parse_expiry_date("28AUG2026") -> date(2026, 8, 28)` (handles 1- or 2-digit days).
- `list_configured_futures(instruments=None, as_of=None, expiry_month=None)` — every
  `config.INSTRUMENT_NAME`/`INSTRUMENT_TYPE`/`INSTRUMENT_EXCH_SEG` row with expiry `>= as_of`
  (today's own expiry day still counts — it's still a trading day), soonest first.
  `expiry_month` (e.g. `"DEC"`) narrows to one month; `None` (the default) doesn't filter by
  month at all, which is what makes this auto-roll.
- `resolve_configured_future()` = `list_configured_futures(expiry_month=config.
  INSTRUMENT_EXPIRY_MONTH)[0]`. `config.INSTRUMENT_EXPIRY_MONTH` defaults to `None` now (was a
  fixed month string); set it (or the `INSTRUMENT_EXPIRY_MONTH` env var) to pin a specific
  far-month contract instead of always taking the front month.

Arrow has no scrip master, so it can't ask the same question directly. [contract_resolver.py](contract_resolver.py):
- `last_thursday(year, month)` — NSE's textbook monthly expiry day. Documented as an
  approximation: real exchange expiries can differ by a day or two around holidays (verified
  directly — August 2026's real expiry is the 25th, the algorithmic last-Thursday rule gives
  the 27th).
- `resolve_arrow_symbol()` — tries Angel's **public** scrip master first (no login needed, just
  the same public URL `fetch_instrument_master()` already downloads) for the real expiry date,
  and falls back to the algorithmic `last_thursday` only if that lookup fails for any reason.
- `config.ARROW_SYMBOL` defaults to `None` now (was a fixed string) — `None` means resolve at
  startup via `resolve_arrow_symbol()`; set the env var to pin a specific symbol instead.

Both resolve fresh in `server.startup()` on every server (re)start — no change needed month to
month.

## Manual switching

New in [server.py](server.py):
- `GET /api/contracts` — candidate contracts for the dropdown. Angel: the next four unexpired
  monthly expiries from the scrip master. Arrow: the next two, computed the same way
  `resolve_arrow_symbol()` picks its own default. Each entry carries `"active": bool`.
- `POST /api/contract` (`{"token": "..."}` for Angel, `{"symbol": "..."}` for Arrow) —
  `_activate_contract()`: stops whatever ws client is currently running, builds a **brand new**
  `TickProcessorState`, and starts ingesting the new contract.

**A switch always resets the live footprint/CVD/order book**, the same as a server restart
would — `TickProcessorState` (order book, footprint, CVD) is specific to one instrument's tick
size and price series; there is no meaningful way to carry September's footprint over onto
October's. The frontend doesn't hide this: the chart/table visibly goes back to empty right
after a switch.

`CONTRACT_SWITCH_LOCK` (an `asyncio.Lock`) serializes calls to `_activate_contract`, including
the one at startup. Verified: the current implementation's STATE-mutating block happens to have
no `await` inside it, so two concurrent switches can't actually interleave mid-update even
without the lock (checked by mutation — removing the lock didn't break the concurrency test).
The lock stays in anyway as a real safeguard against a future change (e.g. an async step added
to client teardown, or a real ws client whose constructor does I/O) — see the test's own
docstring in `tests/test_contract_switch.py` for the full reasoning, so this isn't
re-discovered the hard way later.

**Observation logging.** Every record `_on_tick` writes to `observation_sink` now carries a
`"symbol"` field, read fresh from `STATE["contract"]` on each call — a switch mid-session no
longer leaves `data/sessions/<date>/observations.jsonl` (or `arrow_trades.jsonl`) silently
mixing two instruments' trades with no way to tell them apart. Purely additive: old records
without the field are unaffected, and `replay_engine.py`/`review_cli.py`/`export_dataset.py`
don't need to know about it (they only ever read the specific keys they use).

## Frontend

`/` and `/chart` (live mode only — `/chart?mode=replay` hides it, since replay browses saved
session *dates*, not a live contract) gained a "Contract" `<select>` in the header, wired by a
shared helper, `OFRender.initContractSwitcher` (in [static/render.js](static/render.js)), reused
verbatim by `app.js` and `orderflow.js`. It fetches `/api/contracts` on load, POSTs the chosen
one to `/api/contract` on change, and re-syncs the list afterward (success or failure) so it
always reflects whichever contract is genuinely active — never trusts its own optimistic guess.

## Scope

Switches between **expiries of the currently configured underlying** (`config.INSTRUMENT_NAME`,
i.e. NIFTY) only — not to a different underlying entirely. Arrow's tick size and lot size
(`config.ARROW_TICK_SIZE`/`ARROW_LOT_SIZE`) are fixed constants per instrument, not derived from
anything, so there's nowhere to look up a different underlying's contract spec for that source.

## Testing

`tests/test_contract_resolver.py`, `tests/test_contract_roll.py` (includes a check against the
real, current `instruments_cache.json` if present on disk), `tests/test_contract_switch.py`
(a `FakeWsClient` stands in for the real ws clients — nothing here touches the network). Each
significant piece of boundary/concurrency logic was deliberately mutated at least once to
confirm its test actually fails without it. Verified in a real Chrome run against an offline
harness (`/`, `/chart`, and `/chart?mode=replay` — dropdown population, switching, engine reset,
an empty-body rejection, and the replay-mode hide).

**Not verified:** an actual live broker feed mid-switch (market hours only; today's harness runs
use a fake ws client) — the resolution logic (`resolve_configured_future`, `resolve_arrow_symbol`)
*is* verified against the real, current scrip master, but the live-swap-while-streaming path
itself has only been exercised against `FakeWsClient`, not a real `AngelWebSocketClient` or
`ArrowHFTClient`.
