"""Manual verification terminal — TRADE_CLASSIFICATION.md Phases 2 & 3, and
the Orderflow-Replay-based workflow in REPLAY_WORKFLOW.md.

Two modes:

    python review_cli.py                  # replay mode, today's session
    python review_cli.py --date 2026-08-05 # replay mode, a past session
    python review_cli.py --live            # old live-tail mode

Replay mode (default) loads one full trading day's observations up front
and lets you navigate freely — step trade by trade, jump straight to a
wall-clock time, go back and re-review — so you can verify against
Vtrender's own Orderflow Replay at whatever pace you scrub it, instead of
needing to watch Vtrender live in the same instant. It loads the session
once at startup; if you're reviewing *today's* still-growing session,
restart the tool (or use --live) to pick up new trades.

Live mode is the original behavior: tails data/sessions/<today>/observations.jsonl
as the server writes it, for reviewing alongside Vtrender's live chart.

Keys (both modes):
    a = BUY     b = SELL     s = Skip     q = Quit
    A = BUY + note     B = SELL + note
Replay mode adds:
    n = next (no label)     p = previous     g = jump to time (HH:MM:SS)

Lowercase saves instantly with a single keystroke, no Enter. Uppercase
(Shift) does the same but also prompts for a one-line note before saving.

The verified label is appended to that session's labels.jsonl, keyed by
trade_id — it never overwrites the algorithm's prediction, which stays in
observations.jsonl.
"""
import argparse
import json
import os
import time
from datetime import datetime

import config
from observation_store import LabelStore, load_session_observations

LIVE_KEYS = ("a", "b", "A", "B", "s", "q")
REPLAY_KEYS = ("a", "b", "A", "B", "s", "q", "n", "p", "g")


def _read_key(allowed) -> str:
    """Single keypress, no Enter required, restricted to `allowed`."""
    try:
        import msvcrt
        while True:
            ch = msvcrt.getch()
            try:
                ch = ch.decode("utf-8", errors="ignore")
            except AttributeError:
                ch = ""
            if ch in allowed:
                return ch
    except ImportError:
        while True:
            ch = input(f"Enter ({'/'.join(allowed)}): ").strip()
            if ch in allowed:
                return ch
            print(f"Enter one of: {'/'.join(allowed)}")


def _fmt(v, fmt="{}"):
    return fmt.format(v) if v is not None else "N/A"


def _fmt_levels(levels: list) -> str:
    if not levels:
        return "    (empty)"
    return "\n".join(f"    {p:>10.2f}  qty={q:<8} orders={o}" for p, q, o in levels)


def _clock(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000).strftime("%H:%M:%S.%f")[:-3]


def _display(obs: dict, extra_lines: list = None):
    print("\n" + "-" * 60)
    print(f"Trade #{obs['trade_id']}")
    print(f"\nTime\n{_clock(obs['ts_ms'])}")
    print(f"\nPrice\n{obs['ltp']}")
    print(f"\nQty\n{obs['qty']}")
    print(f"\nBid\n{_fmt(obs['best_bid'])}")
    print(f"\nAsk\n{_fmt(obs['best_ask'])}")
    print(f"\nSpread\n{_fmt(obs['spread'])}")
    print(f"\nMid\n{_fmt(obs['mid_price'])}")
    print(f"\nMicroprice\n{_fmt(obs['micro_price'])}")
    print(f"\nDistance From Bid\n{_fmt(obs['distance_from_bid'])}")
    print(f"\nDistance From Ask\n{_fmt(obs['distance_from_ask'])}")
    print(f"\nInside Spread?\n{obs['inside_spread']}")
    print(f"\nCurrent Algorithm\n{obs['algo_side']}")
    print(f"\nReason\n{obs['algo_reason']}")
    print(f"\nPrevious Price\n{_fmt(obs['prev_ltp'])}")
    print(f"\nPrevious Side\n{_fmt(obs['prev_side'])}")
    print(f"\nBook Imbalance (top-5)\n{_fmt(obs['book_imbalance_top5'], '{:+.2f}')}")
    print(f"\nBid Qty (L1)\n{_fmt(obs['bid_qty_l1'])}")
    print(f"\nAsk Qty (L1)\n{_fmt(obs['ask_qty_l1'])}")
    print(f"\nBest Bid Changed? / Best Ask Changed?\n{obs['best_bid_changed']} / {obs['best_ask_changed']}")
    print(f"\nQuote Age (ms)\n{_fmt(obs['quote_age_ms'])}")
    print(f"\nTime Since Prev Trade (ms)\n{_fmt(obs['time_since_prev_trade_ms'])}")
    print("\nTop-5 Bids:")
    print(_fmt_levels(obs["bid_levels"]))
    print("\nTop-5 Asks:")
    print(_fmt_levels(obs["ask_levels"]))
    for line in (extra_lines or []):
        print(f"\n{line}")
    print("-" * 60)


def _save_label(label_store: LabelStore, obs: dict, key: str) -> bool:
    verified_side = "BUY" if key.lower() == "a" else "SELL"
    correct = (verified_side == obs["algo_side"])
    notes = input(f"\n{verified_side} — note: ").strip() if key.isupper() else ""
    label_store.log(obs["trade_id"], verified_side, correct, notes)
    verdict = "MATCH" if correct else "DISAGREE"
    print(f"Saved [{verdict}]: algorithm={obs['algo_side']}  verified={verified_side}\n")
    return correct


# --- Live mode: tails today's session file as the server writes it -------

def _iter_new_observations(path: str, seen_ids: set, poll_sec: float = 0.5):
    pos = 0
    while True:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                f.seek(pos)
                while True:
                    line = f.readline()
                    if not line:
                        break
                    pos = f.tell()
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obs = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obs.get("trade_id") in seen_ids:
                        continue
                    yield obs
        time.sleep(poll_sec)


def run_live():
    date_str = datetime.now().strftime(config.SESSION_DATE_FMT)
    obs_path = config.observations_path(date_str)
    label_store = LabelStore(config.labels_path(date_str))
    seen = label_store.labeled_ids()

    print(f"Live mode — today's session ({date_str})")
    print(f"Reviewing {obs_path}")
    print(f"{len(seen)} trades already labeled — skipping those.")
    print("Watches for new trades as the server logs them. Ctrl+C to stop.\n")

    reviewed = 0
    try:
        for obs in _iter_new_observations(obs_path, seen):
            _display(obs)
            print("\nVerify with Vtrender (live)\n\n"
                  "a = BUY      A = BUY + note\n"
                  "b = SELL     B = SELL + note\n"
                  "s = Skip     q = Quit\n\nKey: ", end="", flush=True)
            key = _read_key(LIVE_KEYS)
            if key == "q":
                print("\nQuit.")
                break
            if key == "s":
                seen.add(obs["trade_id"])
                print("SKIP\n")
                continue
            _save_label(label_store, obs, key)
            seen.add(obs["trade_id"])
            reviewed += 1
    except KeyboardInterrupt:
        print("\nStopped.")

    print(f"\n{reviewed} trades verified this session.")


# --- Replay mode: seekable browser over a saved (possibly past) session --

def _parse_time_to_ms(date_str: str, raw: str):
    raw = raw.strip()
    parts = raw.split(":")
    if not (2 <= len(parts) <= 3):
        return None
    try:
        hh = int(parts[0])
        mm = int(parts[1])
        ss_part = parts[2] if len(parts) == 3 else "0"
        ss = float(ss_part)
    except ValueError:
        return None
    base = datetime.strptime(date_str, config.SESSION_DATE_FMT)
    target = base.replace(hour=hh, minute=mm, second=int(ss),
                           microsecond=int((ss % 1) * 1_000_000))
    return int(target.timestamp() * 1000)


def _nearest_index(observations: list, target_ms: int) -> int:
    """Index of the trade closest to target_ms (binary search would be
    overkill for a single trading day's worth of trades)."""
    best_i, best_d = 0, abs(observations[0]["ts_ms"] - target_ms)
    for i, obs in enumerate(observations):
        d = abs(obs["ts_ms"] - target_ms)
        if d < best_d:
            best_i, best_d = i, d
    return best_i


def run_replay(date_str: str):
    obs_path = config.observations_path(date_str)
    if not os.path.exists(obs_path):
        print(f"No observations logged for {date_str} ({obs_path} not found).")
        print("Pass --date YYYY-MM-DD for a specific session, or run the server "
              "to record one first.")
        return

    observations = load_session_observations(date_str)
    if not observations:
        print(f"{obs_path} exists but has no trades yet.")
        return

    label_store = LabelStore(config.labels_path(date_str))
    labeled = label_store.labeled_ids()

    print(f"Replay mode — session {date_str}")
    print(f"{len(observations)} trades logged, {len(labeled)} already verified.")
    print("n=next  p=prev  g=goto time  a/b=BUY/SELL  A/B=+note  s=skip  q=quit\n")

    idx = 0
    while True:
        obs = observations[idx]
        already = obs["trade_id"] in labeled
        gap_next = (observations[idx + 1]["ts_ms"] - obs["ts_ms"]) / 1000 \
            if idx + 1 < len(observations) else None
        extra = [
            f"Position\n{idx + 1}/{len(observations)}",
            f"Already Verified?\n{'yes' if already else 'no'}",
        ]
        if gap_next is not None:
            extra.append(
                f"Next Trade\n+{gap_next:.2f}s -> {_clock(observations[idx + 1]['ts_ms'])} "
                f"({observations[idx + 1]['algo_side']} {observations[idx + 1]['qty']} "
                f"@ {observations[idx + 1]['ltp']})"
            )
        else:
            extra.append("Next Trade\n(end of session)")
        _display(obs, extra_lines=extra)
        print("\nVerify with Vtrender's Orderflow Replay\n\n"
              "a = BUY      A = BUY + note      n = next\n"
              "b = SELL     B = SELL + note      p = prev\n"
              "s = Skip     q = Quit             g = goto time (HH:MM:SS)\n\nKey: ",
              end="", flush=True)
        key = _read_key(REPLAY_KEYS)

        if key == "q":
            print("\nQuit.")
            break
        if key == "n":
            idx = min(idx + 1, len(observations) - 1)
            continue
        if key == "p":
            idx = max(idx - 1, 0)
            continue
        if key == "g":
            raw = input("\nGoto time (HH:MM:SS): ").strip()
            target_ms = _parse_time_to_ms(date_str, raw)
            if target_ms is None:
                print("Couldn't parse that — expected HH:MM:SS.")
                continue
            idx = _nearest_index(observations, target_ms)
            continue
        if key == "s":
            print("SKIP\n")
            idx = min(idx + 1, len(observations) - 1)
            continue

        _save_label(label_store, obs, key)
        labeled.add(obs["trade_id"])
        idx = min(idx + 1, len(observations) - 1)

    print(f"\n{len(labeled)} trades verified for {date_str} in total.")


def main():
    parser = argparse.ArgumentParser(
        description="Verify classified trades against Vtrender, live or via Orderflow Replay.")
    parser.add_argument("--date", default=None,
                         help="Session date YYYY-MM-DD to replay-review (default: today)")
    parser.add_argument("--live", action="store_true",
                         help="Tail today's session live instead of browsing a saved one")
    args = parser.parse_args()

    if args.live:
        run_live()
    else:
        date_str = args.date or datetime.now().strftime(config.SESSION_DATE_FMT)
        run_replay(date_str)


if __name__ == "__main__":
    main()
