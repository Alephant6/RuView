# RuView TimescaleDB — the long-term store (WP1.1)

PostgreSQL 17 with TimescaleDB 2.30, one database `ruview`. It replaces
InfluxDB: Telegraf writes every MQTT topic to both until InfluxDB is retired
after two weeks of comparison (WP1.5 in the architecture plan). Raw rows are
kept for 14 days, per-minute and per-hour aggregates forever, and the facts
that analytics derives (episodes, daily summaries, baselines; WP1.2) live in
the same database. All times are UTC (`timestamptz`).

The compose file runs two services from the same image:
`timescaledb` (the server, volume `timescaledb-data`) and `db-migrate`, which
applies [migrations](migrations) and sets the service roles' passwords, then
exits.

## Layers and retention

| Schema | Layer | Contents | Kept |
|---|---|---|---|
| `ingest` | L1 | `state` (≤ 10 Hz), `node`, `person`, `ha_sensor` | 14 days, compressed after 1 day |
| `ingest` | L1 | `ha_binary`, `bridge`, `watchdog`, `recorder` | 90 days, compressed after 7 days |
| `ingest` | L1 | `ha_event`, `recorder_keep`, `annotation` | forever |
| `agg` | L2 | `state_1m`/`_1h`, `node_1m`/`_1h`, `ha_sensor_1m`/`_1h` (continuous aggregates) | forever; per-minute compressed after 30 days |
| `public` | L3/L4 | facts written by analytics (WP1.2) | forever |
| `llm` | — | read-only views for the reports (WP2) | — |

The aggregates are refreshed every 5 minutes (per minute) and every 30 minutes
(per hour) over the last 3 days, well inside the 14 days of raw rows, and the
newest buckets are computed on the fly, so a query sees the current minute.
Both are computed from the raw rows, so hourly medians are exact. Vitals
statistics count only live ticks (`stale_source = 0`); `hr_distinct` and
`br_distinct` are the number of distinct values in the bucket: a handful
over hundreds of samples means frozen vitals, like the September incident.

## What Telegraf writes

[`../telegraf/telegraf.conf`](../telegraf/telegraf.conf) has two
`outputs.postgresql` blocks. A table is the measurement name in schema
`ingest`.

- **Typed tables** `state`, `node`, `person`, `ha_binary`: one column per
  payload field. JSON booleans are stored as 1/0, withheld values (JSON
  `null`) as NULL. `ha_binary` stores the ON/OFF text in `value` and 1/0 in
  `"on"` (a reserved word, so quote it).
- **JSONB tables** `ha_sensor`, `ha_event`, `bridge`, `watchdog`,
  `recorder`, `recorder_keep`, `annotation`: columns `time`, `tags` and
  `fields`. `agg.ha_value(fields)` returns the number of a built-in publisher
  sensor whatever its key (`bpm`, `n_persons`, `dbm`, ...).

The `telegraf` role may only insert, so Telegraf never creates or alters a
table. A payload field without a column is dropped, and Telegraf logs
`missing columns (omitting fields)`. When the bridge gains a field, add the
column in a new migration; `docker/bridge/tests/test_timescaledb_contract.py`
fails until you do.

## Roles

| Role | Can | Password in `docker/.env` |
|---|---|---|
| `postgres` | everything; runs the migrations | `POSTGRES_PASSWORD` (fixed when the volume is created) |
| `telegraf` | insert into `ingest` | `PG_TELEGRAF_PASSWORD` |
| `analytics` | read `ingest` and `agg`, write `public` (WP1.2) | `PG_ANALYTICS_PASSWORD` |
| `grafana` | read `ingest`, `agg` and `public`; read-only sessions, 60 s statement timeout | `PG_GRAFANA_PASSWORD` |
| `llm_ro` | read the views in `llm`; read-only, 30 s timeout | `PG_LLM_RO_PASSWORD` |

A role whose password is empty cannot log in; a set password needs at least
16 characters. db-migrate re-applies the passwords at every run, so changing
one in `.env` and running `docker compose run --rm db-migrate` rotates it.
Only these roles may connect to the database.

## First deploy on the NAS

```bash
cd /volume1/docker/RuView && git pull
cd docker
# Add to .env, each generated with: openssl rand -hex 24
#   POSTGRES_PASSWORD=...
#   PG_TELEGRAF_PASSWORD=...
#   PG_GRAFANA_PASSWORD=...
# Optional, before the first start: PG_MEMORY=1GB, PG_CPUS=2 (see Sizing)
docker compose pull timescaledb telegraf
docker compose up -d timescaledb db-migrate telegraf
```

Check:

```bash
docker compose logs db-migrate     # "... migration(s) applied, schema at version N"
docker compose logs --tail 20 telegraf | grep -i 'postgresql' || echo "no postgresql errors"
docker compose exec timescaledb psql -U postgres ruview \
  -c "SELECT count(*), max(time) FROM ingest.state" \
  -c "SELECT bucket, n, n_live, hr_med, br_med FROM agg.state_1m ORDER BY bucket DESC LIMIT 5"
```

If the database is down, Telegraf keeps writing InfluxDB and retries
TimescaleDB at every flush; rows are buffered in the meantime (up to
`metric_buffer_limit` per output).

## Changing the schema

Add `migrations/NNN_short_name.sql` with the next number. Each file runs once,
in one transaction, and is recorded with its SHA-256 in
`public.schema_migration`. Never edit a file that has been applied: db-migrate
stops when a recorded file changed. Inside the transaction a continuous
aggregate must be created `WITH NO DATA`. db-migrate runs at every
`docker compose up -d`, or on its own with `docker compose run --rm db-migrate`.

## Querying

On the NAS: `docker compose exec timescaledb psql -U postgres ruview`.
From another machine, the port listens on the NAS loopback only
(`PG_BIND=127.0.0.1`), so use a tunnel and the read-only role:

```bash
# The NAS end is PG_PORT from docker/.env (5434 on the home NAS, whose own
# PostgreSQL holds 5432 and 5433).
ssh -N -L 5432:127.0.0.1:5434 <user>@192.168.1.195
psql "host=127.0.0.1 dbname=ruview user=grafana"
```

```sql
-- Heart rate per minute over the last 6 hours, live ticks only
SELECT bucket, hr_n, hr_med, hr_sd, hr_distinct, vitals_authorized
FROM agg.state_1m WHERE bucket > now() - interval '6 hours' ORDER BY bucket;

-- Built-in publisher sensors per node, last hour
SELECT bucket, device, entity, value_med FROM agg.ha_sensor_1m
WHERE bucket > now() - interval '1 hour' ORDER BY bucket, device, entity;

-- Label button presses
SELECT time, tags ->> 'label' AS label, tags ->> 'person' AS person, fields ->> 'text' AS text
FROM ingest.annotation ORDER BY time DESC LIMIT 20;
```

## Sizing

SYNTHETIC (random values in a local test, not measured on the NAS): a full
day of `ingest.state` at 10 Hz (864,000 rows) takes 241 MB and 42 MB once
compressed; a day of `ingest.ha_sensor` (1.17 million rows) 262 MB and 14 MB.
With 14 days kept, the raw tables stay around 1–1.5 GB. The per-minute
aggregates add about 5 MB per day before compression.

timescaledb-tune sizes Postgres for `PG_MEMORY` and `PG_CPUS` when the volume
is first created (1 GB gives 256 MB of shared buffers). To resize later:

```bash
docker compose exec -u postgres timescaledb timescaledb-tune --yes --quiet \
  --memory=2GB --cpus=4 --max-conns=40 --conf-path=/var/lib/postgresql/data/postgresql.conf
docker compose restart timescaledb
```

## Upgrading the image

A newer TimescaleDB in the same Postgres major version: change the tag of
both services, stop the writers, and let db-migrate update the extension
(`ALTER EXTENSION timescaledb UPDATE`) before they reconnect:

```bash
docker compose stop telegraf grafana analytics
docker compose up -d timescaledb db-migrate
docker compose up -d
```

A new Postgres major version (the `-pg17` suffix) needs a dump and restore.
The data lives in the `timescaledb-data` volume: `docker compose down -v`
deletes it.
