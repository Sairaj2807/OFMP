# Deployment

**Target:** one Ubuntu VPS running Docker Compose (`docker-compose.prod.yml`). The stack was
rehearsed end to end on 2026-09-30 with the synthetic feed. That covered HTTPS through nginx with the
strict CSP, the API, Redis, the ingest worker, TimescaleDB, Prometheus and Grafana, and all six
Playwright tests passed against it.

## Topology

```
internet ──443──> nginx ──> api (uvicorn, single process; live engine) ──> timescaledb
                   │  /app static terminal     │                         └> redis <── worker ──> Angel One
                   │  /ws/  (1 h upgrade)       └──────── prometheus <──────┘ (metrics)
                   └─ security headers, CSP, rate limits          grafana (127.0.0.1 only)
```

| Network | Members | Internet |
|---|---|---|
| `edge` | nginx, api | yes |
| `data` | api, worker, timescaledb, redis | **internal only** |
| `monitoring` | prometheus, grafana, api, worker | yes |
| `egress` | worker (broker connection) | yes |

Only nginx publishes ports. Grafana binds to `127.0.0.1`; reach it over an SSH tunnel.

## Server requirements

- Ubuntu 22.04/24.04, 2 vCPU, 4 GB RAM, 40 GB SSD (grow the disk with the raw tick archive).
- Docker Engine 27+ with the Compose plugin.
- A DNS A record pointing at the server.
- Firewall: allow 22, 80 and 443 only:

  ```bash
  ufw allow OpenSSH && ufw allow 80,443/tcp && ufw enable
  ```

## First deployment

```bash
git clone https://github.com/Sairaj2807/OFMP.git /opt/ofmp && cd /opt/ofmp
cp .env.production.example .env.production && chmod 600 .env.production
# edit .env.production: every secret (python -c "import secrets; print(secrets.token_urlsafe(48))"),
# PUBLIC_BASE_URL, Angel One credentials.
```

### TLS certificate (Let's Encrypt, webroot)

nginx needs a certificate to start, so bootstrap with a temporary self-signed one, then replace it:

```bash
deploy/scripts/selfsigned-cert.sh charts.example.com deploy/certs
docker compose -f docker-compose.prod.yml --env-file .env.production up -d nginx
docker compose -f docker-compose.prod.yml --env-file .env.production --profile certbot run --rm certbot \
  certonly --webroot -w /var/www/certbot -d charts.example.com --email you@example.com --agree-tos --no-eff-email
# point nginx at the issued files:
ln -sf live/charts.example.com/fullchain.pem deploy/certs/fullchain.pem
ln -sf live/charts.example.com/privkey.pem   deploy/certs/privkey.pem
docker compose -f docker-compose.prod.yml --env-file .env.production exec nginx nginx -s reload
```

Renew twice daily from cron. Certbot only renews certificates within 30 days of expiry:

```
0 3,15 * * * cd /opt/ofmp && docker compose -f docker-compose.prod.yml --env-file .env.production --profile certbot run --rm certbot renew --quiet && docker compose -f docker-compose.prod.yml --env-file .env.production exec nginx nginx -s reload
```

### Start

```bash
make build ENV_FILE=.env.production
make up    ENV_FILE=.env.production           # migrate runs once, then api/worker start
docker compose -f docker-compose.prod.yml --env-file .env.production exec api \
  python -m backend.app.cli create-user --email you@example.com --role super_admin   # prompts for a password
```

Check:
- `curl -I https://charts.example.com/ready` returns 200.
- The terminal is at `https://charts.example.com/app/`.

The production default is `ALLOW_REGISTRATION=0` (accounts are created by admins). Every page and API
requires login.

### Existing data

To bring over the JSONL sessions recorded before the database existed:

```bash
docker compose ... exec api python -m backend.app.cli import-sessions
docker compose ... exec api python -m backend.app.cli reconcile
```

First copy the `data/sessions` directory into the `ofmp-data` volume.

## Updates

```bash
git pull
make build ENV_FILE=.env.production
deploy/scripts/backup-db.sh /var/backups/ofmp                            # always back up before migrating
docker compose -f docker-compose.prod.yml --env-file .env.production up -d --no-deps migrate api nginx
```

- **The worker keeps running during an API/frontend update.** The broker connection and the
  raw-tick archive are unaffected, and Redis buffers ticks while the API restarts. After it restarts,
  the API rebuilds today's footprint from stored trades.
- **Restart the worker only when its code changed:**
  `up -d --no-deps worker`.
- **Downtime.** The API is a single process, so its restart causes a brief (seconds) interruption for
  connected browsers. They reconnect automatically.

## Rollback

| What | How |
|---|---|
| Application | `git checkout <previous tag>`, then `make build` and `up -d --no-deps api worker nginx`. Tag images per release (`OFMP_VERSION`) to switch back without rebuilding. |
| Database | Migrations have downgrades (`python -m alembic downgrade <rev>`). If a migration changed data, restore the pre-deploy backup instead (see `docs/disaster-recovery.md`). |
| Frontend | Ships inside the nginx image; roll back with the image. |

## Environments

Use development (`.env`), staging (`.env.staging`) and production (`.env.production`) with **separate
secrets and databases**; never copy credentials between them.
- `ENVIRONMENT=production` refuses a weak `JWT_SECRET` and the synthetic feed, and hides the API docs.
- Staging can use the synthetic feed and a self-signed certificate, with `HTTPS_PORT` set if needed.
