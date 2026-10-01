# Load testing

`tools/loadtest.py` drives the terminal's live path: many users, each with several `/ws/v1/stream`
connections, each connection subscribed to several charts.

```bash
# once: create the test users (development/staging database; refuses ENVIRONMENT=production)
DATABASE_URL=... python tools/loadtest.py --create-users --users 40
# run: 40 users x 5 connections x 4 charts = 200 connections, 800 live chart subscriptions, for 60 s
python tools/loadtest.py --base-url http://127.0.0.1:8010 --users 40 --conns-per-user 5 --charts 4 --duration 60 --out result.json
# or: make loadtest
```

**What it reports** (printed, and as JSON with `--out`):
- **Connections.** Opened, failed or closed early (with close codes), and time to `welcome`.
- **Snapshots.** Delivered per subscription per second (the server pushes at 2 Hz), delivery lag
  (receive time minus the server's send timestamp), and bytes received.
- **Server, from `/metrics` before and after.** Engine tick-processing p50/p99, snapshots coalesced for
  slow readers, peak open connections, resident memory (when the platform exports it; otherwise use
  `--server-pid` with psutil installed), and 5xx responses.

**Notes:**
- Logins respect the API's own rate limits (20 per IP per minute), so 40 users take about a minute to
  sign in. That's the limiter working.
- Keep `--duration` below the access-token lifetime (15 min by default). The stream re-checks the session
  on every heartbeat.
- Never point it at production.

## Results (2026-10-01)

Setup: one API process (uvicorn, Python 3.14) with the synthetic feed. The dev database runs in Docker
and the load generator on the same Windows workstation, so lag includes the client's own JSON parsing.
Each chart snapshot carries 60 footprint columns.

**200 connections × 4 charts (800 live subscriptions), 60 s:**

| | Before | After |
|---|---|---|
| connections opened / problems | 200 / none | 200 / none |
| time to welcome p50 / p99 | 0.9 s / 1.2 s | 1.1 s / 1.5 s (all 200 opened at once) |
| snapshots per subscription per second | 1.74 | **1.85** (2.0 nominal; the shortfall is mostly the ramp-up within the 60 s window) |
| delivery lag p50 / p99 / max | 55 / 117 / 145 ms | **39 / 70 / 83 ms** |
| data sent in 60 s | 121.7 MB | **103.7 MB** |
| engine tick processing p50 / p99 | 0.1 / 0.25 ms | 0.1 / 0.25 ms (unaffected by fan-out) |
| snapshots coalesced (slow readers) | 4 | 4 |
| HTTP 5xx | 0 | 0 |

The "before" column is what the first run measured. It found two inefficiencies, fixed in
`backend/app/api/websocket/stream.py`:
1. **The push loop drifted.** It slept 0.5 s *after* each push, so the period grew with the push's own
   work (0.575 s at this load). It now runs on a fixed cadence and skips missed slots instead of bursting.
2. **Each connection re-encoded the same snapshot.** A snapshot shared by many subscribers is now built and
   JSON-encoded once per push (`Encoded`), with compact separators.

**Server resources** (Windows working set and CPU, sampled every 3 s):
- Memory went from 124 MB idle to 183 MB with 200 connections, about 0.3 MB per connection.
- CPU was about 0.2 of one core.
- The engine path stays far below its 10 ms p99 target.

**What this means for capacity:**
- One API process comfortably serves about 200 concurrent terminals with four charts each, within the
  WebSocket targets in `docs/monitoring.md`.
- The cost per extra viewer is serialization and socket writes, not the engine. Distinct (row size,
  interval) combinations are built once per push, whoever asks.
- Before going well past that, measure on the production host. Linux and uvicorn with uvloop will differ
  from this workstation.
- The next scaling step would be several API processes behind nginx, all consuming the Redis tick stream.
  Already in place for that:
  - rate limits shared through Redis (`RATE_LIMIT_BACKEND=redis`)
  - alert firings de-duplicated in the database
  - workspaces kept in the database
- **One piece is still missing.** An alert's in-app push only reaches terminals connected to the process
  that recorded the firing. Webhooks and history are unaffected. Fanning in-app alerts out across
  processes (e.g. Redis pub/sub) needs building before running more than one API process.
