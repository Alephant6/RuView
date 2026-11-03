#!/bin/bash
# Apply the pending SQL migrations of the RuView TimescaleDB, then give each
# service role its login (WP1.1). Safe to run on every deploy:
#   docker compose run --rm db-migrate
#
# Each migrations/NNN_name.sql runs once, in one transaction, and is recorded
# with its SHA-256 in public.schema_migration. A recorded migration whose file
# has changed stops the run: add a new migration instead of editing one.
#
# Environment (docker-compose.yml passes these from docker/.env):
#   POSTGRES_PASSWORD     superuser password (required)
#   PGHOST / PGPORT / PGDATABASE   default timescaledb / 5432 / ruview
#   PG_TELEGRAF_PASSWORD, PG_ANALYTICS_PASSWORD, PG_GRAFANA_PASSWORD,
#   PG_LLM_RO_PASSWORD, PG_BACKUP_PASSWORD
#                         a role whose password is empty cannot log in

set -euo pipefail

export PGHOST="${PGHOST:-timescaledb}"
export PGPORT="${PGPORT:-5432}"
export PGUSER="${PGUSER:-postgres}"
export PGDATABASE="${PGDATABASE:-ruview}"
export PGPASSWORD="${POSTGRES_PASSWORD:-}"
export PGAPPNAME=ruview-migrate
export PGOPTIONS="-c client_min_messages=warning"
DIR="$(cd "$(dirname "$0")" && pwd)/migrations"
MIN_PASSWORD_LENGTH=16

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }
die() { log "ERROR: $*" >&2; exit 1; }
psql_() { psql -X -v ON_ERROR_STOP=1 -q "$@"; }

[ -n "$PGPASSWORD" ] || die "POSTGRES_PASSWORD is not set (docker/.env)"
[ -d "$DIR" ] || die "no migrations directory at $DIR"

for _ in $(seq 1 60); do
    pg_isready -q && break
    sleep 2
done
pg_isready -q || die "database server at $PGHOST:$PGPORT is not ready"

if [ -z "$(psql_ -d postgres -Atc "SELECT 1 FROM pg_database WHERE datname = '$PGDATABASE'")" ]; then
    log "creating database $PGDATABASE"
    psql_ -d postgres -c "CREATE DATABASE \"$PGDATABASE\""
fi

# A newer image ships a newer extension: update it first, in its own session.
if [ -n "$(psql_ -Atc "SELECT 1 FROM pg_extension WHERE extname = 'timescaledb'")" ]; then
    psql_ -c "ALTER EXTENSION timescaledb UPDATE"
fi
log "timescaledb $(psql_ -Atc "SELECT coalesce((SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'), 'not installed yet')"), $(psql_ -Atc 'SHOW server_version')"

psql_ -c "CREATE TABLE IF NOT EXISTS public.schema_migration (
    version    integer PRIMARY KEY,
    name       text NOT NULL,
    sha256     text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now())"

applied=0
for file in "$DIR"/[0-9][0-9][0-9]_*.sql; do
    [ -e "$file" ] || continue
    base="$(basename "$file" .sql)"
    version=$((10#${base%%_*}))
    name="${base#*_}"
    sum="$(sha256sum "$file" | cut -d' ' -f1)"
    recorded="$(psql_ -Atc "SELECT sha256 FROM public.schema_migration WHERE version = $version")"
    if [ -n "$recorded" ]; then
        [ "$recorded" = "$sum" ] || die "$base.sql changed after it was applied (add a new migration instead)"
        continue
    fi
    log "applying $base"
    # The file and its record in one transaction (psql expands variables in
    # script input, not in -c).
    { cat "$file"; printf '\nINSERT INTO public.schema_migration (version, name, sha256) VALUES (:mig_version, :%s, :%s);\n' "'mig_name'" "'mig_sum'"; } |
        psql_ --single-transaction -v mig_version="$version" -v mig_name="$name" -v mig_sum="$sum" -f -
    applied=$((applied + 1))
done
log "$applied migration(s) applied, schema at version $(psql_ -Atc 'SELECT coalesce(max(version), 0) FROM public.schema_migration')"

# Logins: the password comes from the environment through \getenv, so it is
# never on a command line.
for role in telegraf analytics grafana llm_ro backup; do
    var="PG_$(printf '%s' "$role" | tr '[:lower:]' '[:upper:]')_PASSWORD"
    password="${!var:-}"
    if [ -z "$password" ]; then
        psql_ -c "ALTER ROLE $role NOLOGIN"
        log "role $role: no login ($var is empty)"
    elif [ "${#password}" -lt "$MIN_PASSWORD_LENGTH" ]; then
        die "$var is shorter than $MIN_PASSWORD_LENGTH characters (openssl rand -hex 24)"
    else
        printf '\\getenv pw %s\nALTER ROLE %s LOGIN PASSWORD :%s;\n' "$var" "$role" "'pw'" | psql_ -f -
        log "role $role: login enabled"
    fi
done
