#!/usr/bin/env sh
# Logical backup of the production database (custom format, compressed).
#   deploy/scripts/backup-db.sh [backup-dir] [retention-days]
# Schedule daily from cron on the host, e.g.:
#   15 17 * * * cd /opt/ofmp && deploy/scripts/backup-db.sh /var/backups/ofmp 14 >> /var/log/ofmp-backup.log 2>&1
# Copy the resulting files off the server (object storage with versioning) — see docs/disaster-recovery.md.
set -eu
DIR="${1:-backups}"
KEEP_DAYS="${2:-14}"
COMPOSE="${COMPOSE:-docker compose -f docker-compose.prod.yml --env-file .env.production}"

mkdir -p "$DIR"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
OUT="$DIR/ofmp-$STAMP.dump"

# -Fc: compressed, restorable selectively with pg_restore. Written to .partial first
# so an interrupted run never leaves a truncated file that looks complete.
$COMPOSE exec -T timescaledb pg_dump -U ofmp -d ofmp -Fc --no-owner > "$OUT.partial"
mv "$OUT.partial" "$OUT"
( cd "$DIR" && sha256sum "$(basename "$OUT")" > "$(basename "$OUT").sha256" )

find "$DIR" -name 'ofmp-*.dump*' -type f -mtime +"$KEEP_DAYS" -delete
echo "backup written: $OUT ($(wc -c < "$OUT") bytes)"
