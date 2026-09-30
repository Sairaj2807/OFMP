#!/usr/bin/env sh
# Restore a backup made by backup-db.sh into a database (TimescaleDB-aware).
#   deploy/scripts/restore-db.sh <dump-file> [target-db]
#
# Default target is a scratch database (ofmp_restore_test) so restores can be
# rehearsed safely while production keeps running. Restoring over the live
# database ("ofmp") additionally requires CONFIRM_OVERWRITE_LIVE=yes and should
# only happen with the api and worker stopped (docs/disaster-recovery.md).
set -eu
DUMP="${1:?usage: restore-db.sh <dump-file> [target-db]}"
TARGET="${2:-ofmp_restore_test}"
COMPOSE="${COMPOSE:-docker compose -f docker-compose.prod.yml --env-file .env.production}"
PSQL="$COMPOSE exec -T timescaledb psql -U ofmp -v ON_ERROR_STOP=1"

if [ -f "$DUMP.sha256" ]; then
  ( cd "$(dirname "$DUMP")" && sha256sum -c "$(basename "$DUMP").sha256" )
fi
if [ "$TARGET" = "ofmp" ] && [ "${CONFIRM_OVERWRITE_LIVE:-}" != "yes" ]; then
  echo "refusing to overwrite the live database without CONFIRM_OVERWRITE_LIVE=yes" >&2
  exit 2
fi

$PSQL -d postgres -c "DROP DATABASE IF EXISTS \"$TARGET\" WITH (FORCE);"
$PSQL -d postgres -c "CREATE DATABASE \"$TARGET\";"
$PSQL -d "$TARGET" -c "CREATE EXTENSION IF NOT EXISTS timescaledb;"
$PSQL -d "$TARGET" -c "SELECT timescaledb_pre_restore();"
$COMPOSE exec -T timescaledb pg_restore -U ofmp -d "$TARGET" --no-owner --exit-on-error < "$DUMP"
$PSQL -d "$TARGET" -c "SELECT timescaledb_post_restore();"
echo "restored $DUMP into database $TARGET"
