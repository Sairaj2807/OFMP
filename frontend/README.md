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
  app/                  routes: / (terminal), /login (sign in, create account, forgot password),
                        /reset-password, /verify-email (email links)
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

## Alerts

The **Alerts** button in the top bar shows the unread count. It opens three tabs:
- **History.** Alerts as they fired, with mark-all-read and an opt-in for desktop notifications.
- **Rules.** Create, pause/resume and delete rules. A price rule's level is prefilled with the last price.
- **Webhooks.** Add, test and delete webhooks. A new webhook's signing secret is shown only once.

Alerts arrive over the same `/ws/v1/stream` connection and appear as toasts (bottom right) for 10 s.
When the tab is hidden and notifications are allowed, they also appear as desktop notifications. The
command palette has **Open alerts**. Rules are evaluated on the server, so they fire with the terminal
closed (history and webhooks).

## Account pages, market session, contracts

- **Account pages.** `/app/login/` handles sign in, create account and forgot password.
  `/app/reset-password/` and `/app/verify-email/` are the targets of the email links. The old `/login`,
  `/reset-password` and `/verify-email` URLs redirect here, keeping the token.
- **Market session.** Outside an NSE session the top bar shows "Market closed" (with the holiday name,
  when there is one) and when the market next opens, in IST. The data comes from the server's exchange
  calendar.
- **Contracts.** An admin (`admin.system`) sees **Make live** on the other contracts in the watchlist.
  It switches the live feed for every user (after a confirmation) and is audited.

## Market profile

The left sidebar switches the **focused** chart between **Order-flow footprint** and **Market profile**.
Each chart keeps its own view and its own profile row size (1–50 points, default 5), both saved with the
workspace.

Each profile chart also has two layouts:
- **Stacked:** letters packed left, the classic profile shape.
- **Split:** one column per 30-minute period, with each period's open and close marked.

The profile chart draws:
- TPO letters per 30-minute period (blocks when rows are small), value area, POC (highlighted row) and
  volume POC (outlined bar);
- the initial balance, single prints and tails;
- volume at price split into buy and sell;
- the previous session's POC, VAH and VAL.

It works live and in replay. Definitions are in `docs/market-profile.md`.

**Sessions** (live only) shows 5, 10 or 20 finished sessions of the active contract beside today:
- every day an independent profile, all on one price axis;
- the wheel scrolls price, Shift+wheel or dragging scrolls days, double-click follows today;
- hover shows the hovered day's own row.

The coordinate model is `src/lib/chart/multiDay.ts` and the renderer is
`components/chart/MultiDayProfileChart.tsx`.

**Levels** (▾ menu, saved per chart):
- V-VA (VAH, VAL, VPOC, VWAP) and T-VA (TPO-VAH, TPO-VAL, TPOC, TWAP);
- HVN, IB extensions, the developing POC trail, weak highs and lows;
- the previous day's levels and naked POCs;
- day structure (shape, double distribution, open drive), the 80% rule and volume clusters.

VPOC/TPOC are pink bands, VWAP/TWAP green and HVN brown. **Composite** adds a merged profile column (5, 10 or 20
days, this week, or this month). The figures bar shows the values and the day's labels, and hovering a row names the
levels on it. Definitions are in `docs/market-profile.md`; the drawing data is `src/lib/chart/profileLevels.ts`.

**Pan and zoom on both axes** (single-day and multi-day):
- drag to pan;
- Ctrl/⌘+wheel or pinch to zoom around the cursor;
- wheel on the price axis zooms price, wheel on the date header zooms days;
- double-click follows today, or fits that axis when on an axis;
- keys `+` `−` `0` `F` and the arrows; on-chart − + Fit Follow buttons.

Zoom levels are saved per chart. Details are in `docs/market-profile.md`.

## Footprint table

Under the candles, one column per candle (owner decisions 2026-10-13):

| Row | Meaning |
|---|---|
| Volume | buy + sell volume |
| VPOC | the price row with the most volume, at the chart's ₹/row (a tie goes to the higher price) |
| VPOC Vol | the volume at that row |
| COT | buy − sell volume: positive = buyers more aggressive (green), negative = sellers (red) |
| OI | closing open interest of the candle |
| dOI | change from the previous candle; the session's first candle compares with the previous session's close |

- The server computes every value (`domain/orderflow/chart.py`), so live, replay and earlier sessions agree.
  OI is stored with each trade.
- Volumes and OI follow Qty/Lots. Unknown values show "—". Narrow columns use 12.4K / 18.4M forms, and a
  colour dot when there is no room for text. Hover a cell for the candle's exact values.
- **Table** turns it on or off; ▾ chooses rows. Both are saved per chart (layout format v8). The table
  replaces the per-candle Volume/Delta card.
- POC (= VPOC) is drawn as a pink outline on each candle.

The drawing layer is `src/lib/chart/footprintTable.ts`.

## Multi-day footprint

**Footprint sessions** (live only) shows finished sessions of the active contract to the left of today:
- the choice is capped by interval: 1 m → 5 days, 3 m → 10, 5 m and above → 20;
- each session is its own footprint layer, so **CVD restarts at 0 at every session start** (the bottom bar
  says "Session CVD");
- a dashed separator and a date label (sticky while the day is in view, TODAY for the live session) mark
  each session;
- finished days load one at a time over HTTP as you scroll left (browser-cached, immutable); the status in
  the corner says when the start of recorded history or the cap is reached, and lists excluded days on hover;
- today = today's bars from the server merged with every live snapshot window, so bars older than the
  60-bar live window stay on the chart.

Replay shows one session. The pure logic is `src/lib/chart/footprintSessions.ts`; the controller gives each
session its own layer (`src/lib/chart/controller.ts`), and `src/lib/chart/sessionMarks.ts` draws the marks.
The setting is saved per chart (layout format v7).

## Keyboard

| Key | Action |
|---|---|
| `Ctrl/Cmd + K` | command palette |
| `1`–`5` | interval of the focused chart |
| `P` / `V` / `I` | toggle POC / value area / stacked imbalances |
| `M` | focused chart: footprint ↔ market profile |
| `S` | focused chart: market profile stacked ↔ split by period |
