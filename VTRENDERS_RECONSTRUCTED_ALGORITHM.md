# VTRenders Trade Classification — Reverse-Engineered Algorithm

Source data: [data/verified_dataset.csv](data/verified_dataset.csv) — 55 trades, each carrying a full order-book snapshot, the current pipeline's own classification (`algo_side`/`algo_reason`, a Lee-Ready implementation — see [TRADE_CLASSIFICATION.md](TRADE_CLASSIFICATION.md)), and a human-reviewed ground truth label (`verified_side`, `correct`).

This document does **not** describe code that exists in the repo yet. It is a specification, reconstructed empirically by testing candidate formulas against `verified_side` and keeping the one that fits. Treat it as a hypothesis validated at 96–98% on a 55-row sample, not a certified implementation.

---

## 1. Executive summary

The pipeline's existing classifier ([orderbook_engine.py](orderbook_engine.py), documented in [TRADE_CLASSIFICATION.md](TRADE_CLASSIFICATION.md)) is a textbook **Lee & Ready (1991)** quote-rule/tick-rule implementation. Against the verified labels in this dataset it scores:

$$
\text{Accuracy}_{\text{baseline}} = \frac{13}{55} = 23.6\%
$$

That is worse than a coin flip on a two-class label. A correctly-implemented classic algorithm doesn't fail *that* badly by chance — it fails that badly when its **sign convention is inverted** relative to whatever convention the ground truth actually uses. Isolating that inversion and testing it directly against the trade price's position in the spread produces a single-line formula that scores:

$$
\text{Accuracy}_{\text{reconstructed}} = \frac{53}{55} = 96.4\%
$$

$$
\boxed{\;\text{side}(t) = \text{BUY if } P_t < \text{mid}_t, \quad \text{SELL if } P_t > \text{mid}_t \;} \qquad \text{mid}_t = \frac{\text{best\_bid}_t + \text{best\_ask}_t}{2}
$$

This is the **exact opposite polarity of the standard Lee-Ready quote rule** (which assigns BUY above mid, SELL below). No tick history, no state, no fallback logic is required to reach 96.4% — it is a pure, stateless comparison of the trade print to the current NBBO midpoint.

---

## 2. Method

1. Take every row in `verified_dataset.csv` where `verified_side` is populated (all 55).
2. Compute the baseline algorithm's accuracy against `verified_side` (via the `correct` column, already in the data) to confirm it's failing, and characterize *how*.
3. Search candidate formulas built only from features present in the row *at the time of the trade* (bid/ask, mid, micro-price, volume-weighted mid, relative position in spread, tick direction, quote staleness) for the one that best predicts `verified_side`.
4. Cross-check the winning formula with an independently-fit shallow decision tree (`max_depth=3`) over the same feature set, to confirm the same split is found by an unbiased method rather than cherry-picked.
5. Inspect every remaining misclassification by hand for a structural (not coincidental) explanation.

---

## 3. Diagnosing the baseline: it has the sign backwards

Breaking the baseline's `correct` rate down by which rule fired (`algo_reason`):

| `algo_reason` | n | accuracy |
|---|---|---|
| Cold Start | 1 | 100% |
| Zero Tick Rule | 2 | 100% |
| Tick Rule (uptick) | 9 | 55.6% |
| Tick Rule (downtick) | 7 | 57.1% |
| **Quote Rule (at/above Ask)** | 7 | **14.3%** |
| **Quote Rule (at/below Bid)** | 29 | **0.0%** |

The tick-rule buckets hover near 50–57% — consistent with random noise, i.e. tick direction alone carries little information here. The quote-rule buckets are the signal: they are **almost perfectly anti-correlated** with the standard convention.

Directly confirming this by grouping on the raw quote-touch flags rather than the algo's own labels:

| condition (`at_bid` / `at_ask`) | n | `verified_side` |
|---|---|---|
| `at_bid == True` (print at/below best bid) | 29 | **BUY**: 29/29 (100%) |
| `at_ask == True` (print at/above best ask) | 7 | **SELL**: 6/7 (85.7%), BUY: 1/7 |

Standard Lee-Ready says at/below bid → SELL (seller crossed the spread to hit the bid) and at/above ask → BUY (buyer crossed to lift the offer). Every single at-bid print in this dataset is labeled the opposite way, and 6 of 7 at-ask prints are too. This is not "a different algorithm" so much as **the same geometric test with the label flipped.**

---

## 4. The reconstructed rule

### 4.1 Primary rule (stateless, covers 53/55 = 96.4%)

$$
\text{mid}_t = \frac{\text{best\_bid}_t + \text{best\_ask}_t}{2}
$$

$$
\text{side}(t) =
\begin{cases}
\text{BUY}  & \text{if } P_t < \text{mid}_t \\
\text{SELL} & \text{if } P_t > \text{mid}_t
\end{cases}
$$

Equivalently, using a column already computed in the dataset:

$$
\text{relative\_position\_in\_spread}_t = \frac{P_t - \text{best\_bid}_t}{\text{best\_ask}_t - \text{best\_bid}_t}
\qquad\Rightarrow\qquad
\text{side}(t) = \begin{cases}\text{BUY} & \text{rel\_pos} < 0.5 \\ \text{SELL} & \text{rel\_pos} > 0.5\end{cases}
$$

Note this single test **subsumes** the quote-boundary cases found in §3 without any special-casing: a print at/below the bid is trivially below mid ($\Rightarrow$ BUY, matches 29/29), and a print at/above the ask is trivially above mid ($\Rightarrow$ SELL, matches 6/7). The one at-ask exception (trade 17) is discussed in §4.3.

**Interpretation.** Rather than tagging the *aggressor* (the classic academic framing — "who crossed the spread"), this looks like it tags trades by **which side of fair value the print landed on**: a fill below the midpoint is treated as a purchase at a discount (`BUY`), a fill above the midpoint as a sale at a premium (`SELL`). This is consistent with labeling from the *resting/passive* side's perspective rather than the *incoming* order's — but this economic story is a plausible read, not something confirmed by documentation, so treat it as color, not as a load-bearing assumption.

### 4.2 Independent confirmation

A depth-3 decision tree fit on 13 candidate features (`mid_price`, `micro_price`, `weighted_mid_top5`, `distance_from_bid/ask`, `relative_position_in_spread`, `bid_ask_ratio_l1`, `book_imbalance_top5`, `quote_age_ms`, `ticks_since_last_trade`, `price_changed`, `spread`, ...) converges, unprompted, on the **same single feature and nearly the same threshold**:

```
|--- relative_position_in_spread <= 0.40 --> BUY
|--- relative_position_in_spread >  0.40 --> SELL
```

Train accuracy: 98.2% (54/55) — one point better than the 0.5 threshold, because the tree's threshold happens to also catch trade 25 (§4.3). No other feature (order-book imbalance, quote staleness, bid/ask size ratio, tick direction) produced a second useful split — the tree stops after one. That a plain, unregularized tree finds nothing better than "compare price to roughly the midpoint" is a strong independent signal that the midpoint comparison *is* the mechanism, not an artifact of how the formula was searched for.

**Which threshold to use, 0.40 or 0.50?** 0.50 is the economically meaningful value — it *is* the midpoint, and requires no free parameter. 0.40 fits training data marginally better only because it happens to reclassify one edge case (relative position 0.449, a hair below the true 0.5 line). With a single supporting example, moving the cutoff off the true midpoint is not distinguishable from noise. **Use 0.50 (the literal midpoint) as the rule**; note 0.40 only as a sample-specific curiosity, not as a calibrated parameter.

### 4.3 Confusion matrix and the two exceptions

Using the 0.5-threshold rule against all 55 rows:

| | predicted BUY | predicted SELL |
|---|---|---|
| **actual BUY** (n=39) | 38 | 1 |
| **actual SELL** (n=16) | 1 | 15 |

**trade_id 17 — stale-quote miss.** `best_bid`/`best_ask` had not moved in **14,000 ms / 21 ticks** (`quote_age_ms=14000`, `best_bid_changed=False`, `best_ask_changed=False` — an outlier by roughly an order of magnitude; every other row in the dataset has `quote_age_ms=0`). The print sat exactly at the (stale) ask, above mid → the rule predicts SELL, but `verified_side=BUY`. Ordinary tick direction (`ltp=24715.0 > prev_ltp=24712.8`, an uptick) *does* match. Reading: when the quoted book hasn't been refreshed in a long time, it's no longer a trustworthy reference for "fair value," and classification falls back to price momentum instead.

**trade_id 25 — zero-tick, near-midpoint miss.** `price_changed=False` (print identical to the previous trade, `24726.8`), and the print sits only **0.25** away from mid (`ltp=24726.8`, `mid=24727.05` — one of the three smallest mid-gaps in the whole dataset). Predicted BUY (barely below mid), actual `SELL`. This looks like an inherent tie-break/ambiguity case rather than a rule failure: when the trade doesn't move the tape and sits almost exactly on top of fair value, the sign of `P_t - \text{mid}_t` is close to measurement noise.

Both refinements below are **inferred from one example each** — they are documented as a hypothesis for what a more complete rule might look like, not as validated logic. Do not treat the specific numeric thresholds (14,000 ms; "zero-tick") as calibrated without more stale-quote and zero-tick examples to test against.

### 4.4 Full decision tree (primary rule + unvalidated refinements)

```
                         ┌────────────────────────────┐
                         │   trade prints at price P    │
                         └──────────────┬───────────────┘
                                        │
                     ┌──────────────────┴──────────────────┐
                     │  quote stale?  quote_age_ms > ~14s    │   <- 1 example only
                     └──────────────────┬──────────────────┘
                     yes                                  no
                      │                                    │
        ┌─────────────┴─────────────┐        ┌─────────────┴─────────────┐
        │  Tick rule (fallback)      │        │  price_changed == False?   │  <- 1 example only
        │  P > P_prev → BUY          │        └─────────────┬─────────────┘
        │  P < P_prev → SELL         │        yes                          no
        │  P = P_prev → prev side    │         │                            │
        └────────────────────────────┘  ┌──────┴──────┐         ┌──────────┴──────────┐
                                          │ side = prev  │         │  mid = (bid+ask)/2   │
                                          │ side (weak,  │         │  P < mid → BUY        │
                                          │ ambiguous)   │         │  P > mid → SELL       │
                                          └──────────────┘         └───────────────────────┘
```

### 4.5 Pseudocode

```python
def classify_vtrenders(price, best_bid, best_ask, prev_price, prev_side,
                        price_changed, quote_age_ms, stale_threshold_ms=14000):
    # --- unvalidated refinement: single example (trade_id 17) ---
    if quote_age_ms is not None and quote_age_ms > stale_threshold_ms:
        if prev_price is None:
            return "BUY"
        if price > prev_price:
            return "BUY"
        if price < prev_price:
            return "SELL"
        return prev_side or "BUY"

    # --- unvalidated refinement: single example (trade_id 25) ---
    if price_changed is False:
        return prev_side or "BUY"

    # --- primary rule: validated at 53/55 (96.4%) on 55 trades ---
    mid = (best_bid + best_ask) / 2.0
    return "BUY" if price < mid else "SELL"
```

---

## 5. Baseline vs. reconstructed — side by side

| | Baseline (`algo_side`, Lee-Ready) | Reconstructed (VTRenders) |
|---|---|---|
| Primary signal | Quote rule: $P \ge \text{ask} \Rightarrow$ BUY, $P \le \text{bid} \Rightarrow$ SELL | Midpoint rule: $P < \text{mid} \Rightarrow$ BUY, $P > \text{mid} \Rightarrow$ SELL (**opposite polarity**) |
| Fallback | Tick rule → zero-tick rule → cold-start BUY | None needed for 96.4%; stale-quote/zero-tick fallbacks are unvalidated extras |
| State required | Yes (`last_price`, `last_side` per instrument) | No, for the primary rule |
| Accuracy vs. `verified_side` (this dataset) | 23.6% (13/55) | 96.4% (53/55), 98.2% with the two single-example refinements |
| Reference frame | Aggressor (who crossed the spread) | Apparent: fair-value side (who transacted at a discount/premium to mid) — hypothesis, not confirmed |

---

## 6. Limitations

1. **N=55.** This is enough to establish the primary midpoint-inversion rule with high confidence (53/55, and independently reproduced by a decision tree), but far too small to certify the stale-quote and zero-tick refinements — each rests on exactly one observation.
2. **No causal/documentary confirmation.** The "fair-value side" interpretation in §4.1 is a plausible story that fits the data, not something read out of VTRenders' own documentation or source. If VTRenders' actual convention is discovered (e.g. from a spec or support contact), it should supersede this reconstruction.
3. **`quote_age_ms` threshold is a guess.** The only evidence for "stale" is one 14,000 ms observation against a population that is otherwise uniformly 0 ms; the true cutover point (1s? 5s? 10s?) is unknown from this sample.
4. **Session/instrument scope.** All 55 rows come from a single session date (2026-08-06) on what appears to be one instrument (tick size and lot-size pattern consistent with an index future, qty always a multiple of 65). Generalization to other instruments, sessions, or volatility regimes is untested.
5. **Recommended next step before productionizing:** collect a larger verified sample specifically oversampling stale-quote and zero-tick prints, then re-fit/confirm the two refinements (or drop them and use the primary rule alone, which already clears 96%).
