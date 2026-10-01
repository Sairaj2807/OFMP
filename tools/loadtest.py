"""Load test for the terminal's live path: many users, each with several
/ws/v1/stream connections, each connection subscribed to several charts.

    # once: create the test users (development/staging database only)
    python tools/loadtest.py --create-users --users 40
    # run: 40 users x 5 connections x 4 charts for 60 s
    python tools/loadtest.py --base-url http://127.0.0.1:8010 --users 40 --conns-per-user 5 --charts 4 --duration 60

What it measures (printed, and written as JSON with --out):
  - connections: opened / failed / closed early (with close codes), time to welcome
  - snapshots: delivered per subscription per second (the server pushes at 2 Hz),
    delivery lag (receive time minus the server's send timestamp, same host clock
    when run next to the server), received bytes
  - server, from /metrics before and after: p50/p99 engine tick processing,
    snapshots coalesced for slow readers, open connections, resident memory (when
    the platform exports it) and request errors

Logins respect the API's own rate limits (Retry-After is honoured), so a large
user count takes a while to sign in; that is the limiter doing its job. Keep
--duration below the access-token lifetime (15 min by default): the stream
re-checks the session every heartbeat.

Never point this at production: it creates load, and --create-users writes users.
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import websockets

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

DEFAULT_PASSWORD = "load test password 1"
INTERVALS = (60, 180, 300, 900, 1800)


@dataclass
class ConnStats:
    welcome_ms: float = None
    snapshots: dict = field(default_factory=dict)       # sub id -> count
    lags_ms: list = field(default_factory=list)
    bytes: int = 0
    close_code: int = None
    error: str = None
    alerts: int = 0


def email_for(i: int) -> str:
    return f"loadtest+{i}@example.com"


async def create_users(n: int, password: str) -> None:
    import config
    if config.ENVIRONMENT == "production":
        sys.exit("refusing to create load-test users in production")
    from backend.app.core.errors import AppError
    from backend.app.infrastructure.postgres.database import create_engine
    from backend.app.services.auth.email import MemoryEmailSender
    from backend.app.services.auth.service import AuthService
    if not config.DATABASE_URL:
        sys.exit("DATABASE_URL is not set")
    engine = create_engine(config.DATABASE_URL)
    svc = AuthService(engine, "unused-for-user-creation", MemoryEmailSender(), config.PUBLIC_BASE_URL)
    created = 0
    try:
        for i in range(n):
            try:
                await svc.create_user(email_for(i), password, "user", f"Load test {i}")
                created += 1
            except AppError:
                pass                                    # already exists
    finally:
        await engine.dispose()
    print(f"created {created} users ({n - created} already existed)")


async def login(client: httpx.AsyncClient, email: str, password: str) -> str:
    while True:
        r = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
        if r.status_code == 429:
            await asyncio.sleep(int(r.headers.get("Retry-After", "5")))
            continue
        r.raise_for_status()
        return r.json()["access_token"]


async def run_connection(ws_url: str, origin: str, token: str, charts: int, until: float, stats: ConnStats) -> None:
    started = time.perf_counter()
    try:
        async with websockets.connect(ws_url, additional_headers={"Origin": origin}, max_size=8 * 2**20,
                                      open_timeout=20) as ws:
            await ws.send(json.dumps({"action": "auth", "token": token}))
            for c in range(charts):
                await ws.send(json.dumps({"action": "subscribe", "id": f"c{c + 1}", "streams": ["chart"],
                                          "ppr": 1 + c % 2, "interval": INTERVALS[c % len(INTERVALS)]}))
            while time.time() < until:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, until - time.time()))
                except asyncio.TimeoutError:
                    break
                now_ms = time.time() * 1000
                stats.bytes += len(raw)
                msg = json.loads(raw)
                t = msg.get("type")
                if t == "welcome":
                    stats.welcome_ms = (time.perf_counter() - started) * 1000
                elif t == "snapshot":
                    stats.snapshots[msg["id"]] = stats.snapshots.get(msg["id"], 0) + 1
                    stats.lags_ms.append(now_ms - msg["ts"])
                elif t == "ping":
                    await ws.send('{"action":"pong"}')
                elif t == "alert":
                    stats.alerts += 1
                elif t == "error":
                    stats.error = msg["data"].get("code")
    except websockets.ConnectionClosed as e:
        stats.close_code = e.code
    except Exception as e:                                  # refused, timeout, ...
        stats.error = f"{type(e).__name__}: {e}"[:200]


def _process_rss(pid):
    """Server memory when /metrics does not export it (e.g. Windows): needs --server-pid and psutil."""
    if not pid:
        return None
    try:
        import psutil
        return psutil.Process(pid).memory_info().rss
    except Exception:
        return None


def parse_metrics(text: str) -> dict:
    out = {}
    for line in text.splitlines():
        if line and not line.startswith("#"):
            name, _, value = line.rpartition(" ")
            try:
                out[name] = float(value)
            except ValueError:
                pass
    return out


def histogram_quantile(before: dict, after: dict, base: str, q: float):
    buckets = []
    for k, v in after.items():
        if k.startswith(base + "_bucket{") and 'le="' in k:
            le = k.split('le="')[1].split('"')[0]
            buckets.append((float("inf") if le == "+Inf" else float(le), v - before.get(k, 0.0)))
    buckets.sort()
    if not buckets or buckets[-1][1] <= 0:
        return None
    target = q * buckets[-1][1]
    for le, count in buckets:
        if count >= target:
            return le
    return None


def pct(values, p):
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(p / 100 * len(s)))]


async def main_async(a) -> dict:
    origin = a.base_url.rstrip("/")
    ws_url = origin.replace("http", "ws", 1) + "/ws/v1/stream"
    async with httpx.AsyncClient(base_url=origin, timeout=30) as client:
        m_before = parse_metrics((await client.get("/metrics")).text)
        print(f"signing in {a.users} users...", flush=True)
        t0 = time.perf_counter()
        tokens = [await login(client, email_for(i), a.password) for i in range(a.users)]
        login_sec = time.perf_counter() - t0
        until = time.time() + a.duration
        conns = [(tok, ConnStats()) for tok in tokens for _ in range(a.conns_per_user)]
        print(f"opening {len(conns)} connections x {a.charts} charts for {a.duration}s...", flush=True)
        rss_samples, ws_samples = [], []

        async def sample_server():
            while time.time() < until:
                await asyncio.sleep(5)
                m = parse_metrics((await client.get("/metrics")).text)
                ws_samples.append(m.get("ofmp_ws_connections", 0))
                rss = m.get("process_resident_memory_bytes") or _process_rss(a.server_pid)
                if rss:
                    rss_samples.append(rss)

        sampler = asyncio.create_task(sample_server())
        await asyncio.gather(*(run_connection(ws_url, origin, tok, a.charts, until, s) for tok, s in conns))
        sampler.cancel()
        m_after = parse_metrics((await client.get("/metrics")).text)

    ok = [s for _, s in conns if s.welcome_ms is not None]
    subs = [n for s in ok for n in s.snapshots.values()]
    lags = [x for s in ok for x in s.lags_ms]
    closes = {}
    for _, s in conns:
        if s.close_code is not None or s.error:
            key = str(s.close_code or s.error)
            closes[key] = closes.get(key, 0) + 1
    tick_count = (m_after.get("ofmp_tick_processing_seconds_count", 0)
                  - m_before.get("ofmp_tick_processing_seconds_count", 0))
    server_errors = sum(v - m_before.get(k, 0) for k, v in m_after.items()
                        if k.startswith("ofmp_http_requests_total{") and 'status="5' in k)
    result = {
        "config": {"users": a.users, "conns_per_user": a.conns_per_user, "charts": a.charts,
                   "duration_sec": a.duration},
        "login_sec": round(login_sec, 1),
        "connections": {"attempted": len(conns), "opened": len(ok), "problems": closes,
                        "welcome_ms_p50": pct([s.welcome_ms for s in ok], 50),
                        "welcome_ms_p99": pct([s.welcome_ms for s in ok], 99)},
        "snapshots": {
            "subscriptions": len(subs),
            "per_sub_per_sec_mean": round(statistics.mean(subs) / a.duration, 2) if subs else None,
            "per_sub_per_sec_min": round(min(subs) / a.duration, 2) if subs else None,
            "lag_ms_p50": pct(lags, 50), "lag_ms_p99": pct(lags, 99), "lag_ms_max": max(lags) if lags else None,
            "received_mb": round(sum(s.bytes for _, s in conns) / 2**20, 1),
        },
        "server": {
            "ticks_processed": int(tick_count),
            "tick_processing_p50_s": histogram_quantile(m_before, m_after, "ofmp_tick_processing_seconds", 0.5),
            "tick_processing_p99_s": histogram_quantile(m_before, m_after, "ofmp_tick_processing_seconds", 0.99),
            "snapshots_coalesced": int(m_after.get("ofmp_ws_snapshots_coalesced_total", 0)
                                       - m_before.get("ofmp_ws_snapshots_coalesced_total", 0)),
            "peak_ws_connections_seen": max(ws_samples) if ws_samples else None,
            "rss_mb_max": round(max(rss_samples) / 2**20, 1) if rss_samples else None,
            "http_5xx": int(server_errors),
        },
    }
    for k in ("welcome_ms_p50", "welcome_ms_p99"):
        if result["connections"][k] is not None:
            result["connections"][k] = round(result["connections"][k], 1)
    for k in ("lag_ms_p50", "lag_ms_p99", "lag_ms_max"):
        if result["snapshots"][k] is not None:
            result["snapshots"][k] = round(result["snapshots"][k], 1)
    return result


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--base-url", default="http://127.0.0.1:8010")
    p.add_argument("--users", type=int, default=10)
    p.add_argument("--conns-per-user", type=int, default=2, help="at most 5 (the server's per-user limit)")
    p.add_argument("--charts", type=int, default=4, help="subscriptions per connection, at most 6")
    p.add_argument("--duration", type=int, default=60, help="seconds")
    p.add_argument("--password", default=os.environ.get("LOADTEST_PASSWORD", DEFAULT_PASSWORD))
    p.add_argument("--create-users", action="store_true", help="create the users in DATABASE_URL and exit")
    p.add_argument("--out", help="write the result as JSON here")
    p.add_argument("--server-pid", type=int, help="sample this process's memory (psutil) if /metrics has none")
    a = p.parse_args(argv)
    if a.create_users:
        asyncio.run(create_users(a.users, a.password))
        return 0
    if not (1 <= a.conns_per_user <= 5 and 1 <= a.charts <= 6):
        p.error("--conns-per-user must be 1-5 and --charts 1-6 (server limits)")
    result = asyncio.run(main_async(a))
    print(json.dumps(result, indent=2))
    if a.out:
        Path(a.out).write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
