"""Joins each day's observations.jsonl with its labels.jsonl into the flat
ground-truth CSV described in TRADE_CLASSIFICATION.md's reverse-engineering
plan (Phase 4) — combining every session under data/sessions/ (or just one,
with --date) since verification now happens per-day, possibly on different
days than when the trades were recorded (see REPLAY_WORKFLOW.md).

Only trades that have been manually verified are included — this is the
dataset Phase 5+ statistical analysis (error clustering, rule discovery)
runs against. Run after one or more review_cli.py sessions:

    python export_dataset.py                  # all sessions
    python export_dataset.py --date 2026-08-05 # just one
"""
import argparse
import csv
import os

import config
from observation_store import ObservationStore, load_jsonl

FIELDS = [
    "session_date", "trade_id", "ts_ms", "ltp", "qty", "best_bid", "best_ask", "spread", "mid_price",
    "micro_price", "weighted_mid_top5", "distance_from_bid", "distance_from_ask",
    "relative_position_in_spread", "inside_spread", "at_bid", "at_ask",
    "bid_qty_l1", "ask_qty_l1", "bid_ask_ratio_l1", "book_imbalance_top5",
    "prev_ltp", "prev_side", "price_changed", "prev_best_bid", "prev_best_ask",
    "best_bid_changed", "best_ask_changed", "bid_movement", "ask_movement",
    "time_since_prev_trade_ms", "quote_age_ms", "ticks_since_last_trade",
    "volume_since_last_price_change", "cum_volume",
    "algo_side", "algo_reason", "verified_side", "correct", "reviewer_notes",
]


def _rows_for_session(date_str: str) -> list:
    observations = load_jsonl(config.observations_path(date_str))
    labels = load_jsonl(config.labels_path(date_str))

    rows = []
    for trade_id, obs in observations.items():
        label = labels.get(trade_id)
        if label is None:
            continue  # unverified — excluded from the ground-truth dataset
        row = {k: obs.get(k) for k in FIELDS if k != "session_date"}
        row["session_date"] = date_str
        row["verified_side"] = label["verified_side"]
        row["correct"] = label["correct"]
        row["reviewer_notes"] = label.get("reviewer_notes", "")
        rows.append(row)
    return rows, len(observations)


def main():
    parser = argparse.ArgumentParser(description="Export the verified ground-truth dataset.")
    parser.add_argument("--date", default=None,
                         help="Export only this session (YYYY-MM-DD). Default: all sessions.")
    args = parser.parse_args()

    if args.date:
        dates = [args.date] if os.path.exists(config.observations_path(args.date)) else []
    else:
        dates = ObservationStore().list_sessions()

    if not dates:
        print("No sessions found under", config.SESSIONS_DIR)
        return

    all_rows = []
    total_observations = 0
    per_day = []
    for date_str in dates:
        rows, obs_count = _rows_for_session(date_str)
        all_rows.extend(rows)
        total_observations += obs_count
        per_day.append((date_str, obs_count, len(rows)))

    all_rows.sort(key=lambda r: (r["session_date"], r["trade_id"]))

    out_path = config.VERIFIED_DATASET_CSV
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(all_rows)

    verified = len(all_rows)
    wrong = sum(1 for r in all_rows if r["correct"] is False)

    print(f"{len(dates)} session(s): " + ", ".join(f"{d} ({v}/{o} verified)" for d, o, v in per_day))
    print(f"{verified}/{total_observations} logged trades have a verified label.")
    if verified:
        print(f"{wrong}/{verified} disagree with Vtrender ({wrong / verified:.1%}).")
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
