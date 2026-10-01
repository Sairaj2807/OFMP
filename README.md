# OFMP — Order-Flow Market Platform

Live NIFTY futures order flow: trade-side classification (the validated `vtrender_reconstruction/v1`
rule), footprint charts, CVD, and the order book, streamed to a browser terminal.

| | |
|---|---|
| Backend | FastAPI (Python 3.12+), PostgreSQL 16 + TimescaleDB, Redis, Alembic |
| Frontend | Next.js 16, React 19, TypeScript, Tailwind 4, OpenAlgo Charts |
| Market data | Angel One SmartAPI (WebSocket 2.0, Snap Quote); synthetic feed for development |
| Ops | Docker Compose, nginx (TLS, strict CSP), Prometheus + Grafana, GitHub Actions |

## Quick start (development, no broker needed)

```bash
cp .env.example .env                               # set DATABASE_URL and JWT_SECRET (see comments)
docker compose -f docker-compose.dev.yml up -d     # TimescaleDB :55432, Redis :56379
pip install -r requirements-dev.txt
python -m backend.app.cli migrate
python -m backend.app.cli create-user --email you@example.com --role admin
make dev-api                                       # API on :8010 with the synthetic feed
cd frontend && npm ci && npm run build             # terminal served by the API at http://127.0.0.1:8010/app/
```

`MARKET_DATA_PROVIDER=synthetic` streams demo data that is never recorded. Leave it unset (with Angel
One credentials in `.env`) for the live feed.

## Tests

```bash
make test          # backend unit, regression and golden tests
make test-db       # plus database integration (needs the dev TimescaleDB)
make frontend-test # Vitest
make e2e           # Playwright against a running backend
```

The classifier is protected by three regression tests:
- `tests/regression/test_vtrender_classifier.py` pins all 55 verified trades;
- `tests/regression/test_live_sessions_golden.py` re-classifies every stored live trade;
- `tests/regression/test_engine_golden.py` pins the engine's full output.

A change in classifier behaviour ships as a new classifier version. See `docs/orderflow-engine.md`.

## Documentation

| Topic | File |
|---|---|
| Architecture audit and phase log | `ARCHITECTURE_AUDIT.md` |
| Order-flow engine and classifier versioning | `docs/orderflow-engine.md` |
| Market Profile (TPO, value area, IB, single prints) | `docs/market-profile.md` |
| Market data, providers, ingest worker | `docs/market-data.md` |
| Database, persistence, session restore | `docs/database.md` |
| API, authentication, WebSocket stream | `docs/api.md` |
| Terminal (frontend) | `frontend/README.md` |
| Deployment | `docs/deployment.md` |
| Monitoring and alerts | `docs/monitoring.md` |
| Load testing and measured capacity | `docs/load-testing.md` |
| Backups and disaster recovery | `docs/disaster-recovery.md` |
| Security and threat model | `docs/security.md` |
| Original research | `VTRENDERS_RECONSTRUCTED_ALGORITHM.md`, `TRADE_CLASSIFICATION.md`, `REPLAY_WORKFLOW.md` |
