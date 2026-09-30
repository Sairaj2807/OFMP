"""
Standalone, from-scratch implementation of the EMO (Ellis, Michaely &
O'Hara, 2000) trade classification algorithm, run independently against the
raw ltp/bid/ask tape in data/sessions/<date>/observations.jsonl.

EMO shares Lee-Ready's two tests (quote rule, tick rule) but narrows the
quote rule's jurisdiction from inequalities (P_t >= A_t / P_t <= B_t) to
equalities (P_t == A_t / P_t == B_t) -- only an exact print at the touch is
trusted as a direct quote-crossing signal. Everything else, including
prints strictly beyond the quotes (which Lee-Ready would resolve via the
quote rule), falls through to the tick rule / zero-tick rule / cold-start
default -- mathematically identical to Lee-Ready's fallback chain, just
covering a wider set of trades.

Writes a CSV comparing EMO's output, trade by trade, against whatever the
pipeline's own classifier (orderbook_engine.py, a Lee-Ready implementation
-- see lee_ready_classifier.py and TRADE_CLASSIFICATION.md) already
recorded for that trade (the `algo_side` / `algo_reason` fields already
present in each record).
"""
import argparse
import csv
import json
from collections import Counter
from pathlib import Path


class EMOClassifier:
    """Stateful, from-scratch EMO (2000) classifier.

    Test 1 (quote rule, narrowed): P_t == A_t -> BUY, P_t == B_t -> SELL.
    Test 2 (tick rule, fallback):  P_t > P_prev -> BUY, P_t < P_prev -> SELL.
                                   Covers strictly-inside-spread AND
                                   beyond-the-touch prints (unlike Lee-Ready).
    Test 2a (zero-tick, nested):   P_t == P_prev -> carry forward last side.
    Cold start: no prior trade and quote rule inconclusive -> BUY.
    """

    def __init__(self):
        self.last_price = None
        self.last_side = None

    def classify(self, price: float, best_bid, best_ask):
        # --- Test 1: Quote Rule (narrowed to exact touch only) ---
        if best_ask is not None and price == best_ask:
            side, reason = "BUY", "Quote Rule (at Ask, exact)"
        elif best_bid is not None and price == best_bid:
            side, reason = "SELL", "Quote Rule (at Bid, exact)"
        else:
            # --- Test 2: Tick Rule ---
            # Covers: strictly inside the spread (B_t < P_t < A_t), AND
            # strictly beyond the touch (P_t < B_t or P_t > A_t) -- both
            # fall through here under EMO, unlike Lee-Ready.
            if self.last_price is None:
                # --- Cold start: no P_{t-1}, quote rule also inconclusive ---
                side, reason = "BUY", "Cold Start"
            elif price > self.last_price:
                side, reason = "BUY", "Tick Rule (uptick)"
            elif price < self.last_price:
                side, reason = "SELL", "Tick Rule (downtick)"
            else:
                # --- Test 2a: Zero-Tick Rule ---
                side = self.last_side or "BUY"
                reason = "Zero Tick Rule"

        # --- State update: s_{t-1} <- s_t, P_{t-1} <- P_t ---
        self.last_price = price
        self.last_side = side
        return side, reason


def run(input_path: Path, output_path: Path) -> None:
    classifier = EMOClassifier()
    rows_out = []
    agree = 0
    total = 0

    with input_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)

            ltp = rec["ltp"]
            best_bid = rec.get("best_bid")
            best_ask = rec.get("best_ask")

            emo_side, emo_reason = classifier.classify(ltp, best_bid, best_ask)

            pipeline_side = rec.get("algo_side")
            pipeline_reason = rec.get("algo_reason")

            match = emo_side == pipeline_side
            total += 1
            agree += int(match)

            rows_out.append(
                {
                    "trade_id": rec.get("trade_id"),
                    "ts_ms": rec.get("ts_ms"),
                    "ltp": ltp,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "prev_ltp": rec.get("prev_ltp"),
                    "emo_side": emo_side,
                    "emo_reason": emo_reason,
                    "pipeline_algo_side": pipeline_side,
                    "pipeline_algo_reason": pipeline_reason,
                    "match": match,
                }
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows_out[0].keys()))
        writer.writeheader()
        writer.writerows(rows_out)

    print(f"Total trades classified : {total}")
    print(f"Agreement w/ pipeline    : {agree}/{total} ({agree / total:.1%})")
    print(f"Disagreements            : {total - agree}")
    print(f"Output written to        : {output_path}")

    mism = [r for r in rows_out if not r["match"]]
    if mism:
        print("\nDisagreement breakdown by EMO reason:")
        for reason, n in Counter(r["emo_reason"] for r in mism).most_common():
            print(f"  {reason:35s} {n}")
        print("\nDisagreement breakdown by pipeline (Lee-Ready) reason:")
        for reason, n in Counter(r["pipeline_algo_reason"] for r in mism).most_common():
            print(f"  {reason:35s} {n}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input",
        type=Path,
        default=Path("data/sessions/2026-08-06/observations.jsonl"),
        help="Path to the observations.jsonl file to classify",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/emo_vs_pipeline_comparison.csv"),
        help="Path to write the comparison CSV",
    )
    args = parser.parse_args()
    run(args.input, args.output)
