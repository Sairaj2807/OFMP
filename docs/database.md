# Database

PostgreSQL 16 + TimescaleDB 2.17, accessed with SQLAlchemy 2 (async, asyncpg). Schema changes go
through Alembic.

Persistence is optional. With `DATABASE_URL` unset, the platform runs exactly as before, on JSONL files
only.

## Local setup

```bash
docker compose -f docker-compose.dev.yml up -d      # TimescaleDB on 127.0.0.1:55432, Redis on 127.0.0.1:56379
# in .env:
#   DATABASE_URL=postgresql+asyncpg://ofmp:ofmp_dev_password@127.0.0.1:55432/ofmp
python -m backend.app.cli migrate                   # alembic upgrade head
python -m backend.app.cli import-sessions           # backfill existing observations.jsonl (idempotent)
python -m backend.app.cli reconcile                 # JSONL vs database trade counts per session
```

**Ports.** They are offset from the defaults (5432/6379) because other local projects use those.

**Host address.** Use `127.0.0.1`, not `localhost`. On Windows, `localhost` tries IPv6 first and adds
about 2 s to every new connection.

The dev password in `docker-compose.dev.yml` and `.env.example` is for local use only.

## Tables

| Table | Kind | Key / indexes | Written by |
|---|---|---|---|
| `raw_ticks` | hypertable on `received_at`, 1-day chunks | unique (provider, token, received_at, sequence) | the process holding the broker connection |
| `trades` | hypertable on `trade_ts`, 1-day chunks | unique (provider, session_date, trade_seq, trade_ts); index (symbol, trade_ts DESC) | API process, one row per classified trade |
| `data_quality_events` | table | (detected_at DESC), (kind, detected_at DESC) | the process holding the broker connection |
| `instruments` | table | unique (provider, token); (name, instrument_type, expiry); (symbol) | API, when a contract is activated |
| `algorithm_versions` | table | unique (kind, name, version) | migration seed |
| `workspaces` | table | partial unique (owner, name) where not deleted | API (migration 0003) |
| `alert_rules` | table | (owner) and (enabled) where not deleted | API (migration 0004) |
| `alert_events` | table | unique (rule_id, dedup_key); (owner, fired_at DESC); (owner) where unread | alert runtime; pruned after 90 days |
| `notification_channels` | table | (owner) where not deleted; no secrets stored | API |

**Trades.**
- Each trade stores `classifier_name`, `classifier_version`, the deciding `method`, and the full
  observation record in `features` (JSONB).
- `trade_seq` is the same per-day id as `observations.jsonl`'s `trade_id`, so the two reconcile exactly.
- `session_date` is the IST trading date. The JSONL day folders now use explicit IST too.

**No foreign keys on hypertables.** A FK check on every tick insert costs ingestion throughput, and it
would reject ticks for an instrument not yet synced. The time-series tables are joined to `instruments`
on (provider, token) or symbol at query time instead.

**No default time index.** TimescaleDB's default time-only index is turned off. The unique keys, which
lead with instrument and then time, already serve the query pattern (one instrument over a time range),
so an extra index would only cost writes.

## Writing

`MarketDataWriter` (`backend/app/infrastructure/postgres/writer.py`):
- `record_*` only appends to memory, so it is safe on the tick path.
- A background loop flushes every second as multi-row `INSERT … ON CONFLICT DO NOTHING`, so a re-sent
  row is harmless.
- **Database down:** rows stay buffered and are retried. Past 500,000 rows per table, the oldest are
  dropped and counted.
- JSONL is still written alongside during the dual-write period, as the fallback.
- Stats (written / pending / dropped / last error) appear under `database_writer` in
  `GET /api/v1/admin/system`.

## Session restore

When a contract is activated (startup or `POST /api/v1/admin/contract`), the server:
1. upserts the instrument;
2. loads today's trades for that contract (time, price, qty, side, plus the last trade's depth);
3. rebuilds footprint, candle rollover, CVD and the trade tape with `orderflow.restore_trades`;
4. only then starts the live feed.

Stored sides are reused and never re-classified. A test checks that a restored engine reproduces the
live engine's chart payload, CVD history and book exactly.

What restore intentionally does not rebuild:
- **The volume baseline.** The first live tick re-primes it, so trades missed while the server was down
  are never merged into one fabricated trade.
- **The quote-change time.** Until the quote next changes, the stale-quote fallback cannot fire.

Turn restore off with `RESTORE_SESSION_ON_START=0`.

## Migrations

- `backend/alembic/versions/0001_initial_market_data_schema.py`
- Tested: upgrade, downgrade to base, and upgrade again; `alembic check` reports no drift between the
  code schema and the migration.
- New change: `python -m alembic revision --autogenerate -m "..."`. Review the generated file, then
  `python -m backend.app.cli migrate`.

## Tests

`tests/test_persistence.py` runs against a disposable database (the schema is dropped and recreated):

```bash
TEST_DATABASE_URL=postgresql+asyncpg://ofmp:ofmp_dev_password@127.0.0.1:55432/ofmp_test python -m pytest tests/test_persistence.py
```

Without `TEST_DATABASE_URL` these tests are skipped.

## Not done yet

- Compression and retention policies (hot / warm / cold). These should be configuration-driven, not
  hardcoded in a migration.
- Parquet export to object storage.
- Scheduling `reconcile` as a daily job (operations phase).
- Removing the JSONL dual-write once reconciliation has been clean for a while.
- User, workspace and alert tables (Phase 4 onward).
