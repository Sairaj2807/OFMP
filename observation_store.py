"""Append-only storage for the Vtrender reverse-engineering evidence pipeline
(see TRADE_CLASSIFICATION.md, Phases 1-4, and REPLAY_WORKFLOW.md).

Storage is partitioned by trading day under data/sessions/<YYYY-MM-DD>/:

- observations.jsonl — one record per classified trade, written live by the
  engine (orderbook_engine.process_tick via its observation_sink hook).
  Every trade becomes one observation; trade_id is assigned here and is
  unique *within a day* (not globally) — the (date, trade_id) pair is the
  real key once multiple sessions exist.
- labels.jsonl — one record per manual verification (live or via Vtrender's
  Orderflow Replay, days later), written by review_cli.py. Keyed by
  trade_id within that same day's folder. Algorithm prediction and verified
  label are always kept as two separate records so neither ever overwrites
  the other.

Partitioning by day (rather than one flat file) is what lets review happen
independently of when the server was running: a reviewer picks a date and
gets exactly that session's trades, in isolation.
"""
import json
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import config


IST = timezone(timedelta(hours=5, minutes=30))   # NSE has no DST


def date_str_from_ts_ms(ts_ms: int) -> str:
    """IST calendar date a trade timestamp falls on — the session-folder key.
    Explicit IST (not the host's timezone), so the folder is the same on any
    server and matches the database's session_date."""
    return datetime.fromtimestamp(ts_ms / 1000, IST).strftime(config.SESSION_DATE_FMT)


class _DayJsonlWriter:
    """Append-only <name>.jsonl for a single trading day. Assigns each
    logged record an incrementing trade_id, recovered by scanning the
    existing file on startup so a process restart doesn't collide ids with
    what's already on disk. Used by ObservationStore (observations.jsonl)."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()
        self._next_id = self._recover_next_id()

    def _recover_next_id(self) -> int:
        if not os.path.exists(self.path):
            return 1
        last_id = 0
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    last_id = max(last_id, obj.get("trade_id") or 0)
                except json.JSONDecodeError:
                    continue
        return last_id + 1

    def log(self, record: dict) -> int:
        with self._lock:
            trade_id = self._next_id
            self._next_id += 1
            out = dict(record)
            out["trade_id"] = trade_id
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(out, default=str) + "\n")
            return trade_id


class ObservationStore:
    """Routes each observation to the correct day's file based on its own
    ts_ms (not wall-clock "now"), so a session that happens to straddle
    midnight still lands in the right two folders. Lazily creates one
    _DayJsonlWriter per calendar day seen.

    Fed by orderbook_engine's heuristic (inferred-side) trade path."""

    FILENAME = "observations.jsonl"

    def __init__(self, sessions_dir: str = None):
        self.sessions_dir = sessions_dir or config.SESSIONS_DIR
        self._days: dict[str, _DayJsonlWriter] = {}
        self._route_lock = threading.Lock()

    def _writer_for(self, date_str: str) -> _DayJsonlWriter:
        writer = self._days.get(date_str)
        if writer is None:
            with self._route_lock:
                writer = self._days.get(date_str)
                if writer is None:
                    path = f"{self.sessions_dir}/{date_str}/{self.FILENAME}"
                    writer = _DayJsonlWriter(path)
                    self._days[date_str] = writer
        return writer

    def log(self, observation: dict) -> int:
        date_str = date_str_from_ts_ms(observation["ts_ms"])
        return self._writer_for(date_str).log(observation)

    def list_sessions(self) -> list:
        """Sorted (oldest first) list of date strings that have at least one
        logged observation, scanned from disk (works across restarts)."""
        if not os.path.isdir(self.sessions_dir):
            return []
        out = []
        for name in os.listdir(self.sessions_dir):
            obs_file = f"{self.sessions_dir}/{name}/{self.FILENAME}"
            if os.path.isfile(obs_file) and os.path.getsize(obs_file) > 0:
                out.append(name)
        return sorted(out)


def session_records_path(date_str: str) -> Optional[str]:
    """Path of the saved trade log to replay for one day, or None if there
    is none: the day's Angel observations.jsonl, if non-empty."""
    path = config.observations_path(date_str)
    if os.path.isfile(path) and os.path.getsize(path) > 0:
        return path
    return None


def list_replayable_sessions() -> list:
    """Sorted (oldest first) dates that have a non-empty saved trade log,
    scanned from disk."""
    root = config.SESSIONS_DIR
    if not os.path.isdir(root):
        return []
    return sorted(name for name in os.listdir(root) if session_records_path(name) is not None)


def load_session_records(date_str: str) -> list:
    """All saved trade records for one day, sorted by ts_ms (see
    replay_engine.trades_from_record)."""
    path = session_records_path(date_str)
    if path is None:
        return []
    return sorted(load_jsonl(path).values(), key=lambda o: o["ts_ms"])


class LabelStore:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.Lock()

    def labeled_ids(self) -> set:
        """trade_ids that already have at least one verification on file."""
        ids = set()
        if not os.path.exists(self.path):
            return ids
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    ids.add(obj["trade_id"])
                except (json.JSONDecodeError, KeyError):
                    continue
        return ids

    def log(self, trade_id: int, verified_side: str, correct: bool, notes: str = ""):
        with self._lock:
            record = {
                "trade_id": trade_id,
                "verified_side": verified_side,
                "correct": correct,
                "reviewer_notes": notes,
                "verified_at": time.time(),
            }
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")


def load_jsonl(path: str) -> dict:
    """Reads a JSONL file keyed by trade_id, last write wins per id."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            key = obj.get("trade_id")
            if key is not None:
                out[key] = obj
    return out


def load_session_observations(date_str: str) -> list:
    """All observations for one trading day, sorted by ts_ms — the shape a
    replay reviewer or the replay footprint engine wants to consume."""
    obs = load_jsonl(config.observations_path(date_str))
    return sorted(obs.values(), key=lambda o: o["ts_ms"])
