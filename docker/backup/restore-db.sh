#!/bin/bash
# Restore a database dump made by the backup service into an empty
# TimescaleDB (backup/README.md, "Restore"). Runs from the backup image,
# as the superuser:
#   PGPASSWORD=<POSTGRES_PASSWORD> docker compose run --rm -e PGPASSWORD \
#     --entrypoint /app/restore-db.sh backup /backup/restore/backup/dump
#
# The dump directory holds roles.sql (the roles, without passwords), db/
# (pg_dump directory format) and manifest.json. The database must not have
# the RuView tables yet: restore into a new timescaledb volume, then run
# db-migrate, which gives the roles their passwords from docker/.env.
#
# Environment: PGPASSWORD (required), PGHOST / PGPORT / PGUSER / PGDATABASE,
# default timescaledb / 5432 / postgres / ruview.

set -euo pipefail

DUMP="${1:?usage: restore-db.sh <dump directory>}"
export PGHOST="${PGHOST:-timescaledb}"
export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-postgres}"
export PGDATABASE="${PGDATABASE:-ruview}"
export PGAPPNAME=ruview-restore
export PGOPTIONS="-c client_min_messages=warning"

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
psql_() { psql -X -v ON_ERROR_STOP=1 -q "$@"; }

[ -f "$DUMP/db/toc.dat" ] || die "no $DUMP/db/toc.dat: give the dump directory (the one with manifest.json)"
[ -f "$DUMP/roles.sql" ] || die "no $DUMP/roles.sql"
[ -n "${PGPASSWORD:-}" ] || die "PGPASSWORD (POSTGRES_PASSWORD in docker/.env) is not set"
# A server on a new volume first initializes it, without listening on TCP.
for _ in $(seq 1 60); do
    pg_isready -q && break
    sleep 2
done
pg_isready -q || die "database server at $PGHOST:$PGPORT is not ready"

# TimescaleDB restores only into the version that made the dump.
want="$(sed -n 's/.*"timescaledb_version": *"\([^"]*\)".*/\1/p' "$DUMP/manifest.json" 2>/dev/null || true)"
have="$(psql_ -Atc "SELECT coalesce((SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'),
                                    (SELECT default_version FROM pg_available_extensions WHERE name = 'timescaledb'))")"
if [ -n "$want" ] && [ "$want" != "$have" ]; then
    die "the dump is from TimescaleDB $want, this server has $have: use the timescale/timescaledb image of $want"
fi
if [ -n "$(psql_ -Atc "SELECT 1 FROM pg_namespace WHERE nspname = 'ingest'")" ]; then
    die "database $PGDATABASE already has the RuView tables: restore into an empty database (a new timescaledb volume)"
fi

log "creating the roles (an existing role keeps its password; the others get one from db-migrate)"
# CREATE ROLE fails for a role that exists (postgres at least); the ALTER
# ROLE and GRANT lines after it still apply.
psql -X -q -f "$DUMP/roles.sql" 2>&1 | { grep -v 'already exists' || true; }

log "restoring the database from $DUMP/db (TimescaleDB $have)"
psql_ -c "CREATE EXTENSION IF NOT EXISTS timescaledb" -c "SELECT timescaledb_pre_restore()" >/dev/null
rc=0
pg_restore --no-password -d "$PGDATABASE" "$DUMP/db" || rc=$?
psql_ -c "SELECT timescaledb_post_restore()" >/dev/null
[ "$rc" -eq 0 ] || die "pg_restore reported errors (above); the restored database is incomplete"

# Who may connect is a property of the database, which pg_restore applies
# only when it creates the database itself.
pg_restore --create --schema-only -f - "$DUMP/db" |
    grep -E '^(REVOKE|GRANT) [A-Z, ]+ ON DATABASE ' | psql_ -f -

psql_ -c "SELECT 'ingest.state' AS restored, count(*) AS rows FROM ingest.state
          UNION ALL SELECT 'ingest.annotation', count(*) FROM ingest.annotation
          UNION ALL SELECT 'agg.state_1h', count(*) FROM agg.state_1h"
log "done; now run: docker compose run --rm db-migrate (sets the role passwords from docker/.env)"
