# API (v1)

Interactive documentation: `/api/docs` (disabled when `ENVIRONMENT=production`). Schema:
`/api/openapi.json`.

The legacy endpoints (`/api/status`, `/api/contracts`, `/api/contract`, `/api/replay/*`, `/ws/frontend`,
`/ws/chart`) and pages (`/`, `/chart`, `/replay`) are unchanged. They sit behind login only when
`AUTH_REQUIRED=1`.

## Conventions

**Errors.** Every error returns `{"error": {"code": "...", "message": "...", "request_id": "...", "details"?: [...]}}`.
Unexpected failures return `INTERNAL_ERROR` and never include internal details.

**Request IDs.** Every response carries `X-Request-ID`. A well-formed incoming ID (8–64 characters of
`[A-Za-z0-9._-]`) is kept; otherwise one is generated. The ID appears in every log line for that request.

**Limits.**
- Request bodies over `MAX_REQUEST_BYTES` (default 1 MB) get `413 PAYLOAD_TOO_LARGE`.
- Unknown fields in request bodies are rejected (`422 VALIDATION_ERROR`). For example, `role` cannot be
  set at registration.

**Pagination** is by cursor: `?limit=&cursor=`, returning `{"data": [...], "next_cursor": ...}`.

## Authentication

**Browsers.**
- `POST /api/v1/auth/login` sets three cookies:

  | Cookie | Contents | Flags |
  |---|---|---|
  | `ofmp_access` | access JWT, 15 min | HttpOnly, SameSite=Lax |
  | `ofmp_refresh` | opaque refresh token, 30 days | HttpOnly, SameSite=Strict, `Path=/api/v1/auth` |
  | `ofmp_csrf` | CSRF token, readable by JS | — |

- All three are `Secure` unless `COOKIE_SECURE=0`.
- Cookie-authenticated `POST`/`PUT`/`PATCH`/`DELETE` requests must send `X-CSRF-Token`, set to the value
  of the `ofmp_csrf` cookie.

**API clients.** Send `Authorization: Bearer <access_token>`, using the token from the login response. No
CSRF header is needed.

**Refresh.** `POST /api/v1/auth/refresh` (cookie + CSRF header) rotates the refresh token. Presenting an
already-used refresh token revokes the whole session.

Every authenticated request re-checks that its session is still live, so logout and revocation take
effect immediately.

| Method | Path | Auth | Notes |
|---|---|---|---|
| POST | `/api/v1/auth/register` | — | 202 either way (does not reveal whether the email exists); sends a verification email |
| POST | `/api/v1/auth/login` | — | rate limited per IP and per account |
| POST | `/api/v1/auth/refresh` | refresh cookie + CSRF | rotates |
| POST | `/api/v1/auth/logout` | yes | ends this session |
| POST | `/api/v1/auth/logout-all` | yes | ends every session |
| POST | `/api/v1/auth/verify-email` | — | `{token}` from the email |
| POST | `/api/v1/auth/verify-email/resend` | yes | |
| POST | `/api/v1/auth/password-reset/request` | — | 202 either way |
| POST | `/api/v1/auth/password-reset/confirm` | — | `{token, new_password}`; ends every session |
| POST | `/api/v1/auth/password/change` | yes | ends every other session |
| GET | `/api/v1/auth/me` | yes | user and permissions |
| GET | `/api/v1/auth/sessions` | yes | active sessions (device, IP, last used) |
| DELETE | `/api/v1/auth/sessions/{id}` | yes | own sessions only |

**Passwords.**
- At least 10 characters, mixing letters with digits or symbols.
- Stored as Argon2id hashes (RFC 9106 low-memory profile), rehashed on login if the parameters change.

**Email.** No email provider is integrated yet. In development, messages (including their links) are
written to the log. The dev log sender refuses to start when `ENVIRONMENT=production`.

**First admin.**

```
python -m backend.app.cli create-user --email you@example.com --role super_admin
```

The password is prompted for, or read from the env var named by `--password-env`.

## Roles and permissions

| Role | Permissions |
|---|---|
| `user` | `market.read`, `chart.read/write`, `workspace.read/write`, `alerts.create/delete` |
| `support` | user + `admin.users`, `admin.data` |
| `admin` | support + `admin.system` |
| `super_admin` | all |

Checked server-side on every route (`require_permission`). With `AUTH_REQUIRED=1`, switching the global
live contract (`POST /api/contract`) needs `admin.system`.

## Platform endpoints

| Method | Path | Permission | Notes |
|---|---|---|---|
| GET | `/live` | — | process alive |
| GET | `/ready` | — | 503 only if a configured database is unreachable. A disconnected feed (e.g. market closed) does not make the API unready. |
| GET | `/health` | — | summary |
| GET | `/api/v1/market/status` | `market.read` | active contract + feed health |
| GET | `/api/v1/market/contracts` | `market.read` | switchable contracts |
| GET | `/api/v1/admin/users` | `admin.users` | cursor-paginated by email |
| GET | `/api/v1/admin/system` | `admin.system` | feed, database writer, WebSocket stats |

## Workspaces

Saved terminal layouts, per user. Requires `workspace.read` / `workspace.write`.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/workspaces` | your workspaces, default first (no config bodies) |
| POST | `/api/v1/workspaces` | `{name, config, config_version}` → 201; the first becomes default |
| GET | `/api/v1/workspaces/{id}` | full config |
| PATCH | `/api/v1/workspaces/{id}` | `{revision, name?, config?, config_version?, is_default?}` |
| POST | `/api/v1/workspaces/{id}/duplicate` | `{name}` |
| DELETE | `/api/v1/workspaces/{id}` | soft delete; the default moves to the most recently used remaining workspace |

**Ownership.** Another user's workspace returns `404 NOT_FOUND`, exactly like a missing one, so IDs
can't be probed. Ownership fields in request bodies are rejected.

**Concurrency.** Every change must send the `revision` it was based on. A stale write gets
`409 REVISION_CONFLICT`, not a silent overwrite.

**Limits.**
- 50 workspaces per user (`409 LIMIT_REACHED`).
- Config at most 64 KB (`413 CONFIG_TOO_LARGE`).
- Names are 1–80 characters, unique per user (`409 NAME_TAKEN`).

`config` is the terminal's versioned JSON, and newer frontends migrate older saves field by field. Create
and delete go to `audit_logs`.

## Replay sessions

`GET /api/v1/market/replay/sessions` (`market.read`) returns `{"data": [{date, trades}]}`: the stored
sessions available for replay. Trades come from the database when it is configured, otherwise from the
JSONL session folders.

## Alerts

A user's alert rules run against **live** data for the active contract. They never run during replay,
nor on the trades restored at startup. Permissions: `alerts.read`, `alerts.create`, `alerts.delete`.

| Method | Path | Notes |
|---|---|---|
| GET | `/api/v1/alerts/kinds` | condition kinds, intervals, cooldown range, limits (for building a form) |
| GET | `/api/v1/alerts/rules` | your rules |
| POST | `/api/v1/alerts/rules` | `{name, kind, params, mode?, cooldown_sec?, channel_ids?, enabled?}` → 201 |
| GET | `/api/v1/alerts/rules/{id}` | one rule |
| PATCH | `/api/v1/alerts/rules/{id}` | `{revision, ...changes}`; `enabled: false` pauses it; a `kind` change needs `params` |
| DELETE | `/api/v1/alerts/rules/{id}` | soft delete |
| GET | `/api/v1/alerts/events?limit=&before=&unread=` | history, newest first (`before` = an event id, for paging) |
| GET | `/api/v1/alerts/events/unread-count` | `{unread}` |
| POST | `/api/v1/alerts/events/read` | `{ids?}`; omit `ids` to mark everything read |
| GET | `/api/v1/alerts/channels` | your webhooks |
| POST | `/api/v1/alerts/channels` | `{name, url}` → 201 with `secret`, **shown only this once** |
| DELETE | `/api/v1/alerts/channels/{id}` | also removed from every rule that used it |
| POST | `/api/v1/alerts/channels/{id}/test` | a signed test delivery; 10 per hour |

**Conditions** (`kind` and its `params`):

| kind | params | fires when |
|---|---|---|
| `price_above` / `price_below` | `level` | the last traded price crosses the level |
| `cvd_above` / `cvd_below` | `level` | session CVD (closed and forming candles) crosses the level |
| `candle_delta_above` / `candle_delta_below` | `level`, `interval` | a closed candle's delta is ≥ / ≤ the level |
| `candle_volume_above` | `level`, `interval` | a closed candle's volume is ≥ the level |
| `stacked_imbalance` | `interval`, `side` (buy, sell, any) | a closed candle has a stacked imbalance (the engine's rule: ratio 3, stack 3) |
| `value_area_break` | `interval`, `side` (up, down, any) | a candle closes above the previous candle's VAH, or below its VAL |

- **Crossings.** A trade condition fires on a crossing. The first trade after a rule is loaded only
  records which side of the level the market is on, so a new rule never fires on the state it was
  created in.
- **Candle closes.** A candle condition is checked when the candle closes, which is known on the first
  trade of the next candle (the same moment the chart shows it closed). Candles are grouped exactly as
  on the chart.
- **`mode`.** `repeat` (default) can fire again after `cooldown_sec` (10 s to 24 h, default 300) of
  market time. `once` pauses the rule after it fires.

**Delivery:**
- **In-app.** An `alert` message on every open `/ws/v1/stream` connection of the owner.
- **Webhooks.** A rule's `channel_ids` also get a POST of `{"type":"alert.fired", id, rule_id, rule_name,
  kind, message, value, symbol, fired_at, details}`.
  - Verify `X-OFMP-Signature: sha256=HMAC_SHA256(secret, "<X-OFMP-Timestamp>.<body>")`, and reject old
    timestamps. `X-OFMP-Event-Id` identifies the event.
  - In production, webhooks must be `https` on public addresses. The host is resolved and checked on
    every delivery, and the request goes to the checked address. Redirects are not followed.
  - 5 s timeout; up to 3 attempts on network errors, 5xx or 429.

**Guarantees and limits:**
- An event is recorded once per rule and market event (unique on `(rule_id, dedup_key)`), so a second
  API process evaluating the same tape cannot deliver it twice.
- At most 20 deliveries per user per minute. Beyond that, events are still recorded, with
  `suppressed: "rate_limited"`.
- 50 rules and 5 webhooks per user. History is kept for 90 days.
- Ownership is checked like workspaces: another user's rule, channel or event is `404 NOT_FOUND`.
- Rule and channel creates and deletes are audited.

## Real-time stream: `/ws/v1/stream`

**Authenticating.** Use the `ofmp_access` cookie. Clients without cookies instead send
`{"action":"auth","token":"<access token>"}` as the first message, within 5 s.

**Client → server messages:**
- `{"action":"subscribe","streams":["chart"],"ppr":1,"interval":60,"contract":"NIFTY27OCT26FUT"}`
  - `ppr`: 1–5
  - `interval`: one of the allowed chart intervals
  - `contract`: optional; must be the active contract
- `{"action":"unsubscribe"}`
- `{"action":"ping"}` / `{"action":"pong"}`

**Server → client messages** have the form `{"type","seq","ts","data"}`:
- `type` is one of `welcome`, `subscribed`, `snapshot`, `ping`, `pong`, `unsubscribed`, `error`, and
  `alert` (one of your alert rules fired; sent without any subscription).
- `seq` goes up by one per message on the connection.
- `ts` is server time in epoch ms.

**Close codes:**

| Code | Meaning |
|---|---|
| 4401 | not authenticated, or session revoked (re-checked every 15 s heartbeat) |
| 4403 | foreign Origin, or missing `market.read` |
| 4408 | idle for more than 45 s |
| 4429 | connection limit (5 per user, 1000 total) |
| 1009 | client frame over 4 KB |
| 1013 | server not ready |

**Backpressure.** A connection holds at most one pending snapshot. If the client falls behind,
intermediate snapshots are replaced by the newest one, so memory per connection stays constant.

### Replay over the stream

Any chart id can replay a stored session instead of streaming live. The replay runs **on the server**,
through the same aggregation code as live (`replay_engine.apply_record`), using the sides stored with
each trade. Nothing is re-classified, and only snapshots are sent.

**Starting a replay:**

```json
{"action": "replay", "id": "c1", "date": "2026-09-29", "ppr": 1, "interval": 300, "speed": 10, "autoplay": false, "at_ms": null}
```

- The server replies `replay_started` with the session metadata, then snapshots.
- Each snapshot's `data.replay` holds `{date, start_ms, end_ms, cursor_ms, index, total, playing, speed, speeds}`.
- Re-sending `replay` for the same date keeps the position (useful for a row-size or interval change).
- A `subscribe` on the same id switches it back to live.

**Controls:**

```json
{"action": "replay_control", "id": "c1", "command": "play|pause|speed|seek|step", "value": 50, "unit": "trade|candle"}
```

- `speed` accepts 1, 2, 5, 10, 25 or 50.
- `seek` takes an epoch-ms value.
- `step` moves one trade, or to the end of the next candle.

**Limits and errors.** At most 2 replays per connection (within the 6-subscription limit). Error codes:
`REPLAY_UNAVAILABLE`, `INVALID_SPEED`, `INVALID_CONTROL`, `NO_REPLAY`, `TOO_MANY_REPLAYS`.

Loaded days are shared between viewers through a small cache. On the dev machine, a 10,000-trade day
loads in about 0.5 s.
