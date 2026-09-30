# Backups and disaster recovery

## Targets

| | Target | Basis |
|---|---|---|
| **RPO** (data loss) | ≤ 24 h for the database. For live market data: whatever the raw tick archive holds (written every second) plus the Redis stream buffer. | daily `pg_dump`; the tick archive is on its own volume |
| **RTO** (time to recover) | ≤ 2 h on a fresh server | Compose rebuild plus restore of the latest dump |

A missed trading session cannot be re-fetched from Angel One's streaming API. Market data lost while
the platform is down is lost for good. Protect the raw tick archive (`ofmp-data` volume) the same way
as the database.

## What to back up

| Data | Where | How |
|---|---|---|
| Database: users, sessions, audit, trades, raw ticks, instruments | `ofmp-pgdata` volume | `deploy/scripts/backup-db.sh` daily. Copy the files off the server to object storage **with versioning**. |
| Raw tick archive and JSONL sessions | `ofmp-data` volume | nightly `tar` or `rclone` sync to object storage |
| Secrets and config | `.env.production`, `deploy/certs/` | keep in a password manager or secret store, **not** the repo or the database backup |

Nothing in Redis or Prometheus needs a backup.

## Backup

```bash
deploy/scripts/backup-db.sh /var/backups/ofmp 14      # dump + sha256, 14-day local retention
# cron (after NSE close, 22:45 IST = 17:15 UTC):
15 17 * * * cd /opt/ofmp && deploy/scripts/backup-db.sh /var/backups/ofmp 14 >> /var/log/ofmp-backup.log 2>&1
```

## Restore test (monthly)

The restore path is tested, not assumed. On 2026-09-30, a dump of the rehearsal stack was restored into
a scratch database, and it matched the live one: users, audit rows, schema revision and hypertables.

```bash
deploy/scripts/restore-db.sh /var/backups/ofmp/ofmp-<stamp>.dump      # -> database ofmp_restore_test
docker compose ... exec timescaledb psql -U ofmp -d ofmp_restore_test -c "select count(*) from trades"
```

The script verifies the checksum, runs `timescaledb_pre_restore()` / `timescaledb_post_restore()`, and
refuses to overwrite the live database unless you pass `CONFIRM_OVERWRITE_LIVE=yes`.

## Scenarios

| Failure | Recovery |
|---|---|
| **API crash** | Docker restarts it (`restart: unless-stopped`). It rebuilds today's footprint from stored trades, and browsers reconnect. |
| **Worker crash / broker outage** | The worker reconnects with backoff. The gap shows up in `ofmp_provider_connected`, the data-quality events and the dashboard. Missed ticks are **not** fabricated. |
| **Redis restart** | Transport only. The worker republishes and the API resubscribes. The in-flight stream buffer is lost, but the tick archive and database are unaffected. |
| **Database restart** | The writer buffers rows (up to 500k per table) and retries. `/ready` returns 503 until the database is back. |
| **Database corruption / loss** | 1. Stop the api and worker. 2. `CONFIRM_OVERWRITE_LIVE=yes deploy/scripts/restore-db.sh <latest.dump> ofmp`. 3. Start the stack again. 4. Re-import the JSONL/tick archive written since the dump (`import-sessions`, `import-ticks`), which is idempotent. |
| **Server loss** | 1. Provision a new server (`docs/deployment.md`) and restore `.env.production` and the certificates. 2. Restore the latest dump. 3. Restore the `ofmp-data` archive. 4. Start the stack. 5. Point DNS at the new server. |
| **Object storage loss** | The versioned bucket plus the local 14-day retention cover it. Keep a second copy in another region or provider for critical data. |
| **Compromised credentials** | 1. Rotate the Angel One PIN/TOTP/API key, `JWT_SECRET`, and the database and Redis passwords. 2. Revoke all sessions (a `JWT_SECRET` change already invalidates every access token; refresh tokens are server-side, so revoke their sessions). 3. Review `audit_logs`. |
