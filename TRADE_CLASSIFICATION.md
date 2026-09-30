# Trade Classification Algorithm — Mathematical Documentation

Source: [orderbook_engine.py](orderbook_engine.py) — classes `TradeClassifier` (L112–150) and `process_tick` (L314–357).

## 1. The core problem

Angel One's Snap Quote feed gives you the **best-5 bid/ask depth** and the **last traded price (LTP)**, but — like almost every real-time market data feed — it does **not** tell you directly whether the last trade was buyer-initiated (aggressor bought, hitting the ask) or seller-initiated (aggressor sold, hitting the bid). You only get:

- `ltp` — last traded price
- `ltt` — last traded time (epoch ms)
- `cum_volume` — cumulative traded volume for the session so far
- `depth_buy[0..4]`, `depth_sell[0..4]` — top-5 bid/ask levels

Two separate problems have to be solved before anything downstream (footprint, delta, CVD) is meaningful:

1. **How many shares/contracts actually traded on this tick?** (volume extraction)
2. **Was that trade a buy-side or sell-side aggressor?** (side classification)

The code solves (1) with a cumulative-volume differencing scheme, and (2) with the **Lee & Ready (1991) algorithm** — quote rule with a tick-rule fallback — which is the standard academic/industry method for inferring trade direction from public market data when the exchange doesn't tag trades itself.

---

## 2. Step 1 — Volume extraction (`extract_trade_qty`)

The feed does not send discrete trade prints — it sends a running total. The traded quantity on a given tick is recovered by differencing successive cumulative-volume snapshots:

```
qty_t = CumVol_t − CumVol_{t-1}
```

Formally, let $V_t$ be the cumulative session volume reported at tick $t$. The inferred trade size is:

$$
q_t =
\begin{cases}
0 & \text{if } V_{t-1} \text{ is undefined (first tick)} \\
0 & \text{if } V_t - V_{t-1} < 0 \text{ (sequence/session reset)} \\
V_t - V_{t-1} & \text{otherwise}
\end{cases}
$$

Implementation ([orderbook_engine.py:121-132](orderbook_engine.py#L121-L132)):

```python
def extract_trade_qty(self, cum_vol: int) -> int:
    if self.last_cum_vol is None:
        self.last_cum_vol = cum_vol
        return 0
    delta = cum_vol - self.last_cum_vol
    if delta < 0:
        self.last_cum_vol = cum_vol
        return 0
    self.last_cum_vol = cum_vol
    return delta
```

Notes on this formula:

- **First tick is discarded as `qty=0`** — there is no prior baseline to diff against, so no trade is registered, only the running total is primed.
- **Negative delta is treated as a reset, not a trade.** A negative $\Delta V$ can only happen if the feed's counter rolled over (new session) or an out-of-order/duplicate packet arrived. The classifier resynchronizes silently rather than recording a fabricated "negative volume" trade.
- **This is Strategy A / "diff-against-cumulative-day-volume"** per the design doc referenced in the module — the only strategy viable here because Angel One's Snap Quote mode gives snapshots, not an incremental trade tape.
- If `qty_t == 0` (no change in cumulative volume since the last tick, e.g. a pure quote update with no trade), **no trade is classified or recorded at all** — `process_tick` only proceeds to classification when `qty > 0` ([orderbook_engine.py:337](orderbook_engine.py#L337)).

Important asymmetry to be aware of: a single tick's $q_t$ can represent **more than one underlying trade** if several trades occurred between two ticks (the feed coalesces them into one volume jump). The algorithm has no way to split this quantity by side — the entire $q_t$ is assigned to **one** classified side per tick. This is a known limitation inherited from any snapshot-based (non-tape) feed.

---

## 3. Step 2 — Side classification (`classify`) — Lee-Ready algorithm

This is a direct implementation of the **Lee & Ready (1991)** trade classification algorithm, with a **quote rule** as primary test and a **tick rule** (plus a **zero-tick rule**) as fallback when quotes can't decide.

### 3.1 Quote rule (primary test)

At the moment of the trade, compare the trade price $P_t$ to the prevailing best bid $B_t$ and best ask $A_t$:

$$
\text{side}(t) =
\begin{cases}
\text{BUY}  & \text{if } P_t \ge A_t \\
\text{SELL} & \text{if } P_t \le B_t \\
\text{(undetermined — fall through to tick rule)} & \text{if } B_t < P_t < A_t
\end{cases}
$$

Rationale: if a trade executes **at or above the best ask**, the buyer must have been aggressive enough to cross the spread and lift the offer — hence buyer-initiated. Symmetrically, a trade **at or below the best bid** means the seller crossed the spread and hit the bid — seller-initiated. A trade **strictly inside the spread** (which can happen with hidden/mid-point liquidity, or stale quotes relative to the trade print) gives no such signal, so the algorithm falls back to price-history-based inference.

Implementation ([orderbook_engine.py:135-138](orderbook_engine.py#L135-L138)):

```python
if best_ask is not None and price >= best_ask:
    side = "BUY"
elif best_bid is not None and price <= best_bid:
    side = "SELL"
```

Note the `>=` / `<=` (not strict `>` / `<`): a trade printing **exactly at the touch** is classified by the quote rule, not deferred to the tick rule. This matches the standard Lee-Ready convention (Lee & Ready originally used the *midpoint* rather than raw bid/ask edges for the primary split in some variants, but the exchange-touch formulation used here — "at-or-through-the-ask ⇒ buy, at-or-through-the-bid ⇒ sell" — is the more common practical implementation, sometimes called the "spread rule" or a simplified/robust variant of the quote rule that avoids relying on the exact midpoint).

### 3.2 Tick rule (fallback, when price is strictly inside the spread)

If the quote rule cannot classify (price is strictly between bid and ask, or one side of the book is empty), the algorithm compares the current trade price to the **previous trade's price**:

$$
\text{side}(t) =
\begin{cases}
\text{BUY}  & \text{if } P_t > P_{t-1} \quad \text{(“uptick”)} \\
\text{SELL} & \text{if } P_t < P_{t-1} \quad \text{(“downtick”)} \\
\text{side}(t-1) & \text{if } P_t = P_{t-1} \quad \text{(“zero tick” — see 3.3)}
\end{cases}
$$

Rationale: a price rising from the last print suggests continued buying pressure absorbed the offer up; a falling price suggests selling pressure absorbed the bid down.

Implementation ([orderbook_engine.py:139-146](orderbook_engine.py#L139-L146)):

```python
elif self.last_price is None:
    side = "BUY"
elif price > self.last_price:
    side = "BUY"
elif price < self.last_price:
    side = "SELL"
else:
    side = self.last_side or "BUY"
```

### 3.3 Zero-tick rule and cold-start default

Two edge cases nested inside the tick-rule fallback:

- **Zero tick** ($P_t = P_{t-1}$, and quote rule was inconclusive): the classic Lee-Ready "zero-tick rule" — carry forward the **previous trade's classified side**, i.e. $\text{side}(t) = \text{side}(t-1)$. The reasoning is that a flat price against an unchanged/ambiguous quote most likely continues whatever pressure was already driving the tape.
- **Cold start** ($P_{t-1}$ undefined, i.e. this is literally the first classified trade ever seen, and quotes were unavailable/inconclusive): defaults to `BUY`. This is an arbitrary but harmless tie-break — it only affects the very first ambiguous trade of the session, since `last_price`/`last_side` are populated from that point on.

### 3.4 State update

At the end of every call, regardless of which rule fired:

```python
self.last_price = price
self.last_side = side
```

This is what allows the tick rule and zero-tick rule to function on the *next* call — the classifier is stateful across ticks (per instrument).

### 3.5 Full decision tree

```
                         ┌───────────────────────────┐
                         │  trade prints at price P   │
                         └─────────────┬──────────────┘
                                       │
                    ┌──────────────────┴──────────────────┐
                    │   Quote rule: compare P to book       │
                    └──────────────────┬──────────────────┘
                                       │
          ┌────────────────────────────┼────────────────────────────┐
          │ P ≥ best_ask                │ B < P < A (or book missing)│ P ≤ best_bid
          ▼                            ▼                             ▼
        BUY                    Tick rule: compare P to P_prev      SELL
   (aggressor lifted                    │
        the offer)      ┌────────────────┼────────────────┐
                         │ P > P_prev      │ P = P_prev      │ P < P_prev
                         ▼                 ▼                 ▼
                       BUY        Zero-tick rule:           SELL
                  (uptick,     carry forward last       (downtick,
                 momentum buy)  classified side           momentum sell)
                              (or BUY if no history)
```

---

## 4. Where this fits in `process_tick` (end-to-end)

Per incoming WebSocket tick ([orderbook_engine.py:314-357](orderbook_engine.py#L314-L357)):

1. **Book replace.** The top-5 bid/ask levels are wholesale-replaced from the snapshot (Angel One sends full depth, not deltas).
2. **Crossed-book guard.** If `best_bid ≥ best_ask`, the tick is discarded entirely — no trade classification happens on a broken book (`validate()`, [orderbook_engine.py:104-109](orderbook_engine.py#L104-L109)).
3. **Volume extraction.** $q_t$ = `extract_trade_qty(cum_volume)` as in §2.
4. **If $q_t > 0$:** read current `best_bid`/`best_ask` from the just-updated book, then call `classify(ltp, best_bid, best_ask)` as in §3.
5. **Trade object built:** `Trade(timestamp=ltt, price=ltp, quantity=q_t, side=side)`.
6. **Fed into the footprint** (`Footprint.add_trade`), which buckets the trade into a `(candle_ts, price_level)` cell and increments either `buy_volume`/`buy_trades` or `sell_volume`/`sell_trades` on that cell.

---

## 5. Downstream consumers of the BUY/SELL label

Everything past classification is arithmetic on the two counters `buy_volume` and `sell_volume` that every `FootprintCell` carries:

### 5.1 Delta (per price cell or per candle)

$$
\Delta = \text{buy\_volume} - \text{sell\_volume}
$$

([orderbook_engine.py:34-36](orderbook_engine.py#L34-L36); candle-level aggregate at [orderbook_engine.py:207-208](orderbook_engine.py#L207-L208), summing $\Delta$ across all price cells in the candle).

### 5.2 Total volume

$$
\text{Vol} = \text{buy\_volume} + \text{sell\_volume}
$$

### 5.3 Cumulative Volume Delta (CVD)

A running sum of **closed-candle** deltas, updated only when a new candle timestamp is observed (i.e. the previous candle has just closed):

$$
\text{CVD}_n = \text{CVD}_{n-1} + \Delta_n
$$

where $\Delta_n$ is the total buy-minus-sell delta of candle $n$. Implementation: `CVDTracker.update` ([orderbook_engine.py:153-159](orderbook_engine.py#L153-L159)), invoked from `process_tick` at candle rollover ([orderbook_engine.py:350-353](orderbook_engine.py#L350-L353)).

### 5.4 Point of Control (POC)

The price level with the highest **total** volume (buy + sell) in a candle:

$$
\text{POC} = \arg\max_{p} \big(\text{buy\_volume}_p + \text{sell\_volume}_p\big)
$$

([orderbook_engine.py:242-246](orderbook_engine.py#L242-L246)).

### 5.5 Value Area

Starting from the POC row, greedily expand up/down (adding whichever neighboring row — above or below — has more volume) until the accumulated volume covers `VALUE_AREA_PCT` (default 70%) of the candle's total volume ([orderbook_engine.py:248-271](orderbook_engine.py#L248-L271)):

$$
\text{VA} = \{p_{lo}, \dots, p_{hi}\} \text{ such that } \sum_{p=p_{lo}}^{p_{hi}} \text{Vol}_p \ge 0.70 \cdot \sum_{\text{all } p} \text{Vol}_p
$$

### 5.6 Stacked imbalance detection

Diagonal comparison between a row's buy volume and the **row below's** sell volume (and vice versa), classic footprint-chart "imbalance" logic — a level is flagged when one side dominates the diagonally adjacent opposite side by a threshold ratio (default 3.0×):

$$
\text{BUY\_IMBALANCE at } p_i \iff \frac{\text{buy\_volume}_{p_i}}{\text{sell\_volume}_{p_{i-1}}} \ge 3.0
$$
$$
\text{SELL\_IMBALANCE at } p_i \iff \frac{\text{sell\_volume}_{p_i}}{\text{buy\_volume}_{p_{i-1}}} \ge 3.0
$$

Consecutive imbalanced rows (≥ `MIN_STACK` = 3 in a row) form a "stack" ([orderbook_engine.py:273-294](orderbook_engine.py#L273-L294)) — a common footprint signal for absorption/exhaustion.

### 5.7 Order Book Imbalance (OBI) — a related but *separate* signal

Not derived from trade classification at all — this is a pure **standing-liquidity** imbalance computed directly from the top-5 resting book, independent of any trade:

$$
\text{OBI} = \frac{\sum \text{bid\_qty}_{1..5} - \sum \text{ask\_qty}_{1..5}}{\sum \text{bid\_qty}_{1..5} + \sum \text{ask\_qty}_{1..5}} \in [-1, 1]
$$

([orderbook_engine.py:98-102](orderbook_engine.py#L98-L102)). Worth distinguishing from footprint delta: OBI measures **resting order pressure** (who has more size waiting), while buy/sell volume from `TradeClassifier` measures **executed aggressor pressure** (who actually crossed the spread). They can and do diverge — e.g. heavy bid-side resting size (OBI > 0) with a stream of sell-classified trades (delta < 0) signals sellers aggressively hitting into a large passive bid, a classic absorption pattern.

---

## 6. Known limitations / edge cases worth documenting explicitly

1. **Snapshot coalescing.** As noted in §2, a single tick's volume delta can represent multiple real trades, all forced into one classified side. On a fast-moving/liquid instrument like NIFTY futures, this understates trade count and can misattribute volume when several small trades of opposite sides occurred between two snapshots.
2. **Quote staleness.** Because depth and LTP arrive in the same snapshot tick, the "prevailing quote" used by the quote rule is the **post-update** book, not necessarily the book that was live at the exact moment the trade executed on the exchange. This is an inherent limitation of quote-driven (vs. matching-engine tape) classification.
3. **Mid-spread trades fall through to tick rule** — meaningful when the instrument has a wide spread or hidden liquidity executes inside the touch.
4. **Reset detection is conservative.** Any negative cumulative-volume delta is treated as a reset and silently absorbed (zero trade recorded) rather than raising an error — acceptable for a real-time dashboard but means a genuine feed corruption could silently drop volume rather than surface as an alert.
5. **First-ever trade of a session defaults to BUY** if quotes are unavailable at that exact instant — a one-time, low-impact bias.
