# OFMP terminal (frontend)

The trading terminal UI for OFMP: live footprint charts (1, 2 or 4 per layout), the order book, session
figures, a command palette and keyboard shortcuts.

Stack: Next.js 16 (App Router, static export), React 19, TypeScript, Tailwind 4, Zustand. Charts use
[OpenAlgo Charts](https://github.com/marketcalls/openalgo-charts) 2.4.0 (Apache-2.0), vendored in
`src/vendor/openalgo-charts/` with its LICENSE and NOTICE.

> This project uses Next.js 16. Read `AGENTS.md` and the bundled docs in `node_modules/next/dist/docs/`
> before changing framework-level code.

## How it fits together

| Environment | How it runs |
|---|---|
| Production | `npm run build` writes a static export to `out/`. The FastAPI app serves it at **`/app`**, on the same origin as `/api/v1` and `/ws/v1/stream`, so HttpOnly session cookies and the WebSocket need no cross-origin setup. |
| Development | `npm run dev` serves on http://127.0.0.1:3100/app and proxies `/api`, `/health`, `/ready` and `/live` to the backend (`BACKEND_URL`, default http://127.0.0.1:8010). The Next dev server cannot proxy WebSockets, so the stream connects to the backend directly. |

For the direct WebSocket connection in development, set on the frontend:

```
NEXT_PUBLIC_STREAM_URL=ws://127.0.0.1:8010/ws/v1/stream
```

and allow the dev origin on the backend: `CORS_ORIGINS=http://127.0.0.1:3100`.

The session cookie is host-scoped, so it is shared between ports.

## Running the backend without a broker

```bash
# from the repo root
docker compose -f docker-compose.dev.yml up -d
python -m backend.app.cli migrate
python -m backend.app.cli create-user --email demo@example.com --role admin --password-env DEMO_PASSWORD
MARKET_DATA_PROVIDER=synthetic COOKIE_SECURE=0 JWT_SECRET=<random> DATABASE_URL=<see .env.example> \
  python -m uvicorn server:app --port 8010
```

`MARKET_DATA_PROVIDER=synthetic` streams a random-walk demo feed. Nothing from it is recorded.

## Scripts

| Script | What it does |
|---|---|
| `npm run dev` | dev server on :3100 |
| `npm run build` | static export to `out/` |
| `npm run lint` / `npm run typecheck` | ESLint (with React Compiler rules) / `tsc` |
| `npm test` | Vitest unit tests: stream client, API client, adapter, stores |
| `npm run test:e2e` | Playwright against a running backend at http://127.0.0.1:8010 (override with `E2E_BASE_URL`, `E2E_EMAIL`, `E2E_PASSWORD`) |

## Layout

```
src/
  app/                  routes: / (terminal), /login
  components/
    chart/              FootprintChart (thin React wrapper)
    terminal/           TopBar, Watchlist, ChartGrid, ChartPanel, OrderBook, BottomBar, CommandPalette, Terminal
    ui/                 shared controls
  hooks/useLiveStream   the single /ws/v1/stream connection; subscriptions follow the visible charts
  lib/
    api.ts              /api/v1 client (CSRF header, one shared refresh on 401, error envelope)
    stream.ts           WebSocket client (backoff, resubscribe, heartbeats, seq tracking)
    snapshots.ts        latest snapshot per chart; charts redraw from here without React re-renders
    chart/controller.ts imperative chart-library controller
    adapter.ts          engine payload -> chart-library shapes
  stores/               terminal (layout and chart settings, versioned in localStorage), session
  vendor/openalgo-charts/
e2e/                    Playwright specs
```

## Replay and workspaces

**Replay.** Each chart panel has a **Live / Replay** switch. Replay adds a bar with:
- the session date
- step one trade / step to the end of the next candle
- play / pause and speed (1–50×)
- a seek slider
- the IST replay clock and trade progress

Playback runs on the server (see `docs/api.md`), and each chart can replay a different day.

**Workspaces.** The top-bar menu lets you switch, save as, rename, duplicate, make default or delete a
workspace.
- Layout changes autosave to the current workspace after 1.5 s, and immediately when the page closes.
- If another window saved first, the newer copy is loaded and a notice explains why.
- The last-used workspace reopens in the same browser.
- Without a database, the terminal keeps working on local storage only.

## Keyboard

| Key | Action |
|---|---|
| `Ctrl/Cmd + K` | command palette |
| `1`–`5` | interval of the focused chart |
| `P` / `V` / `I` | toggle POC / value area / stacked imbalances |
