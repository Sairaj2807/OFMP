# Daily broker session (Angel One)

Angel One tokens expire every day at midnight. A server meant to run for weeks
therefore logs in again every trading morning, starts the day, and turns the feed
off after the close. Nobody has to generate a token by hand.

Code: `backend/app/services/broker_session.py` (the schedule), with the broker calls
in `server.py` (`_broker_login`, `_broker_start_day`, `_stop_feed`, `_system_notify`)
and the login itself in `angel_client.py` / `angelone_autologin.py`.

## Schedule (IST, from the NSE calendar: weekends, holidays and special sessions)

| When | What |
|---|---|
| open − 45 min (08:30) | Full login with TOTP. A saved token is never reused for this. The token is checked with a REST call (`getProfile`) and saved. |
| right after | Fresh instrument master, then the contract: an admin's **Make live** choice while it has not expired, otherwise the front month (auto-roll on expiry). A new live engine is restored from today's stored trades, then the feed connects in SnapQuote mode. |
| on failure | Retry every 2 minutes until open − 5 min (09:10). Each failure is reported, then a "gave up" notice is sent. After that the session waits for **Re-login** or the next morning. |
| open .. close | Watchdog: no tick for 5 minutes while the market is open is reported (once a day). |
| close + 15 min (15:45) | Feed off and database writes flushed. The live charts keep today's data. |
| overnight, weekends, holidays | Feed off, so there are no reconnect loops. The terminal, history, replay and multi-day charts keep working. |

- **Started inside market hours** (a reboot at 10:00): logs in at once, and retries for 40 minutes.
- **Started outside market hours:** the engine is restored for the charts without any login. The contract comes
  from the public instrument master. The feed starts at the next session.

## Safety rules

- **A rejected token is never retried.** HTTP 401/403 on the feed handshake marks the saved token invalid
  (`angel_client.mark_token_invalid`). The next connect does a full login, never a refresh, so a token cancelled
  before its written expiry cannot loop.
- **Lock-out guard.** After 3 failed full logins in a row, automatic logins pause for 15 minutes
  (`LoginBlocked`). This stops a wrong PIN or TOTP secret from locking the Angel account. **Re-login** lifts the
  pause.
- **Secrets.** Every Angel request has a timeout. Tokens and the TOTP secret are never logged or sent to the
  browser. The token file is written owner-only where the OS allows it (`chmod 600`).

## Notices

Notices go to the log, to every signed-in admin's terminal (in-app), and to every admin's webhook channels
(Alerts → Channels). The webhook body is the alert shape with `"type": "system.broker"`, `"kind":
"system.broker"`, a `level` ("info" | "error") and a `message`. Examples:

- `Angel login failed at 08:30 (attempt 1): RuntimeError: [LOGIN] Failed: Invalid totp (AB1050). Retrying in 2 min.`
- `Angel login succeeded at 08:34 after 3 attempts.`
- `Contract rolled to NIFTY24NOV26FUT.`
- `No market data since the open although the market is open.`

## Status and controls

- **Top bar:** "Broker: logged in 08:30", "waiting · next login Wed 08:30 IST", "feed off · …" or "login failed".
  Admins see the last error, attempts and token expiry in the tooltip, plus a **Re-login** button.
- `GET /api/v1/market/status` → `broker`: traders get `state, feed_on, logged_in_at, next_action,
  next_action_at`; admins also get `last_error, attempts, contract, token_expires_at, gave_up_for,
  login_paused_until`.
- `GET /api/v1/admin/system` → `broker_session`.
- `POST /api/v1/admin/broker/relogin` (`admin.system`, audited as `broker.relogin`). Inside market hours it logs
  in and restarts the day, also after giving up. Outside them it logs in once to check the credentials.
- **CLI:**
  - `python -m backend.app.cli broker-login` logs in as 08:30 does.
  - `--check` only verifies the saved token (no new login).
  - It prints the masked client code and the token expiry, never a token.
- **Metrics:** `ofmp_broker_logins_total{result}` and `ofmp_broker_token_expiry_timestamp_seconds`.

## Settings (environment)

| Variable | Default | Meaning |
|---|---|---|
| `BROKER_SCHEDULE` | `1` | `0` keeps the old always-connected feed |
| `BROKER_LOGIN_LEAD_MIN` | `45` | login this long before the open (08:30) |
| `BROKER_GIVE_UP_LEAD_MIN` | `5` | last retry this long before the open (09:10) |
| `BROKER_STOP_AFTER_CLOSE_MIN` | `15` | feed off this long after the close (15:45) |
| `BROKER_RETRY_SEC` | `120` | between failed attempts |

The schedule runs for the live Angel feed inside the API process (`INGEST_MODE=embedded`). It does not run on
the synthetic feed, or in worker mode, where the ingest worker owns the connection. Moving it into the worker
comes with hosting.

## Keeping the process itself running

The schedule covers the broker. The process still has to stay up, which is a hosting decision for later:

- **On this Windows PC:** run it as a service (e.g. NSSM: start at boot, restart on crash), start Docker Desktop
  at login, and turn off sleep.
- **On a Linux server:** use `docker-compose.prod.yml` (`restart: unless-stopped`). See `deployment.md`.

Until then, after a reboot, start Docker Desktop and the server again. The server logs in by itself if it starts
during market hours.
