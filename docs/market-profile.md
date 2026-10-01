# Market Profile

Each terminal chart can be an **order-flow footprint** or a **market profile**.
- Switch the focused chart with the left sidebar, the `M` key, or the command palette.
- The choice is per chart and saved with the workspace.
- Live and replay both work in either view.

## Definitions (v1)

The engine is `backend/app/domain/profile/` (pure code). It is fed the same classified trades as the
footprint, one at a time: live trades, the startup restore and replay. It never re-classifies anything.

| Item | Rule |
|---|---|
| Periods | 30 minutes from the session open: A = 09:15–09:45, B = 09:45–10:15, … M = 15:15–15:30. Letters run A–Z, then a–z. |
| TPO | A period marks **every price row between its high and its low** (the CBOT definition), not only the prices that printed. Angel One sends about one snapshot a second, so "printed only" would leave gaps that never existed. |
| Session | NSE 09:15–15:30 IST. Trades outside it are not part of the profile. A new session starts on the first trade of a new day; the finished one is kept as the previous session. The synthetic development feed uses a 24-hour session. |
| Rows | Integer ticks internally. Rows group ticks: 1, 2, 5, 10, 20 or 50 points, set per chart, default 5. Each row is labelled by its floor price, as in the footprint. The ladder is contiguous from the session low to the high. |
| POC | Computed separately for TPOs and for volume: the row with the most TPOs or the most volume. Ties go to the row closest to the middle of the range, then to the lower row. |
| Value area | 70%, for TPOs and for volume separately. Start at the POC and repeatedly add the neighbouring row (above or below) with more weight, taking the row below on a tie, until 70% is covered. This is the same rule as the footprint's value area. |
| Initial balance | High and low of periods A and B (09:15–10:15). Shown as "forming" until C trades. Extensions above and below are measured from it. |
| Single prints | Runs of adjacent rows with exactly one TPO from the same finished period, inside the profile. |
| Tails | Single-print runs at the top of the profile (selling tail) or the bottom (buying tail). |
| HVN / LVN (v1) | A row with the largest / smallest volume within ±2 rows, and ≥ 1.5× / ≤ 0.5× the mean row volume. LVNs only count away from the edges. |
| Order flow per row | Every row carries volume, buy, sell and delta, from the classified trades. |
| Previous session | Its POC, VAH and VAL are drawn as dotted reference lines. At startup it is loaded from stored trades. |

## Where it comes from

- **Live.** `server._on_tick` passes each classified trade to `engine.profile`, created with the
  footprint engine for the active contract. A contract switch starts a new profile.
- **Restart.** Today's stored trades are folded back in with the footprint, and the previous session is
  loaded for the reference lines.
- **Replay.** `replay_engine.apply_record` feeds the replay's own profile. A test proves that the profile
  at any replay position equals one built fresh from the trades up to that position.
- **Stream.** A chart subscription (or replay) with `"view": "profile", "row": 5` receives snapshots
  with `data.view = "profile"` and `data.profile`. They are built once per push and shared by every
  viewer with the same row size.
- **History.** `GET /api/v1/market/profile?date=YYYY-MM-DD&row=5` returns a stored session's whole
  profile.

## The chart

- TPO letters per period, coloured by period. When rows are too small for text, the letters become
  coloured blocks.
- Value-area shading. The TPO POC is a highlighted row; the volume POC is an outlined bar.
- Initial-balance bracket at the left edge (dashed while still forming).
- Single prints (amber) and tails (grey) as marks at the left edge.
- Volume at price split into buy (green) and sell (red), with HVN / LVN ticks.
- The previous session's POC, VAH and VAL as dotted lines.
- The price axis, with the last price.

**Using it:** hover a row for its letters, TPO count, volume, buy/sell and delta. The wheel scrolls tall
profiles, and double-click returns to following the last price. A strip above the chart shows POC,
volume POC, VAH, VAL, initial balance, TPO count, volume, delta, the number of single prints and the
previous POC.

## Not done yet

- Composite profiles (week, month), value migration, and POC migration.
- Day-type and open-type classification.
- Market Profile alert rules, e.g. "price leaves the value area", "IB break", "POC migrates".
- Printed-prices-only TPO marking as an option (the engine stores tick ranges per period, which is what
  the high–low rule needs).
