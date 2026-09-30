"""Operations CLI.

    python -m backend.app.cli migrate                  apply database migrations (alembic upgrade head)
    python -m backend.app.cli import-sessions [--date D]   observations.jsonl -> trades (idempotent)
    python -m backend.app.cli import-ticks [--date D]      data/ticks archive -> raw_ticks (idempotent)
    python -m backend.app.cli reconcile [--date D]         compare JSONL trade counts with the database
    python -m backend.app.cli db-status                    row counts per table / session

Database commands need DATABASE_URL (environment or .env)."""
import argparse
import asyncio
import glob
import json
import os
import sys
from datetime import date

import config


def _require_db() -> str:
    if not config.DATABASE_URL:
        sys.exit("DATABASE_URL is not set (see .env.example)")
    return config.DATABASE_URL


def cmd_migrate(args) -> int:
    from alembic import command
    from alembic.config import Config
    _require_db()
    command.upgrade(Config(os.path.join(os.path.dirname(os.path.abspath(config.__file__)), "alembic.ini")), "head")
    return 0


def _session_dates(root: str, only: str = None) -> list:
    if not os.path.isdir(root):
        return []
    return sorted(d for d in os.listdir(root) if (only is None or d == only) and os.path.isdir(os.path.join(root, d)))


async def _import_sessions(args) -> int:
    from backend.app.infrastructure.postgres.database import create_engine
    from backend.app.infrastructure.postgres.writer import MarketDataWriter
    from observation_store import load_jsonl

    engine = create_engine(_require_db())
    writer = MarketDataWriter(engine)
    try:
        for d in _session_dates(args.sessions_dir, args.date):
            path = os.path.join(args.sessions_dir, d, "observations.jsonl")
            if not os.path.isfile(path):
                continue
            records = load_jsonl(path)
            for obs in records.values():
                writer.record_trade(obs, provider="angelone")
            await writer.flush()
            if writer.last_error:
                print(f"{d}: FAILED ({writer.last_error})")
                return 1
            print(f"{d}: {len(records)} trades sent (already-present rows are skipped)")
    finally:
        await engine.dispose()
    return 0


async def _import_ticks(args) -> int:
    from backend.app.domain.market_data import MarketTick
    from backend.app.infrastructure.postgres.database import create_engine
    from backend.app.infrastructure.postgres.writer import MarketDataWriter

    engine = create_engine(_require_db())
    writer = MarketDataWriter(engine)
    try:
        for d in _session_dates(args.ticks_dir, args.date):
            for path in sorted(glob.glob(os.path.join(args.ticks_dir, d, "*.jsonl"))):
                n = 0
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            writer.record_tick(MarketTick.from_dict(json.loads(line)))
                            n += 1
                            if n % 20_000 == 0:
                                await writer.flush()
                await writer.flush()
                if writer.last_error:
                    print(f"{path}: FAILED ({writer.last_error})")
                    return 1
                print(f"{path}: {n} ticks sent")
    finally:
        await engine.dispose()
    return 0


async def _reconcile(args) -> int:
    from backend.app.infrastructure.postgres.database import create_engine
    from backend.app.infrastructure.postgres.repositories import trade_counts_by_session
    from observation_store import load_jsonl

    engine = create_engine(_require_db())
    try:
        db_counts = await trade_counts_by_session(engine, provider="angelone")
    finally:
        await engine.dispose()
    mismatches = 0
    print(f"{'session':<12}{'jsonl':>10}{'database':>10}  status")
    for d in _session_dates(args.sessions_dir, args.date):
        path = os.path.join(args.sessions_dir, d, "observations.jsonl")
        if not os.path.isfile(path):
            continue
        jsonl = len(load_jsonl(path))
        db = db_counts.get(date.fromisoformat(d), 0)
        ok = jsonl == db
        mismatches += not ok
        print(f"{d:<12}{jsonl:>10}{db:>10}  {'ok' if ok else 'MISMATCH'}")
    return 1 if mismatches else 0


async def _db_status(args) -> int:
    from backend.app.infrastructure.postgres.database import create_engine
    from backend.app.infrastructure.postgres.repositories import raw_tick_count, trade_counts_by_session

    engine = create_engine(_require_db())
    try:
        counts = await trade_counts_by_session(engine)
        ticks = await raw_tick_count(engine)
    finally:
        await engine.dispose()
    print(f"raw_ticks: {ticks}")
    print(f"trades: {sum(counts.values())} across {len(counts)} session(s)")
    for d in sorted(counts):
        print(f"  {d}  {counts[d]}")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m backend.app.cli", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("migrate", help="apply database migrations").set_defaults(func=cmd_migrate)
    for name, fn, help_ in (("import-sessions", _import_sessions, "import observations.jsonl into trades"),
                            ("import-ticks", _import_ticks, "import the raw tick archive into raw_ticks"),
                            ("reconcile", _reconcile, "compare JSONL and database trade counts")):
        p = sub.add_parser(name, help=help_)
        p.add_argument("--date", help="only this session (YYYY-MM-DD)")
        p.add_argument("--sessions-dir", default=config.SESSIONS_DIR)
        p.add_argument("--ticks-dir", default=config.TICKS_DIR)
        p.set_defaults(func=lambda a, fn=fn: asyncio.run(fn(a)))
    sub.add_parser("db-status", help="row counts").set_defaults(func=lambda a: asyncio.run(_db_status(a)))
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
