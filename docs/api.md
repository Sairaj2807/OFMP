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
- `type` is one of `welcome`, `subscribed`, `snapshot`, `ping`, `pong`, `unsubscribed`, `error`.
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
