# RuView offsite backup (WP1.1)

Every night the `backup` service dumps the TimescaleDB database and the Apple
Health data in InfluxDB, and restic encrypts the dump and uploads it to a
Backblaze B2 bucket. Old snapshots are pruned, a share of the stored data is
read back and verified once a week, and the result goes to the NAS bus, where
the data watchdog raises **Backup** in Home Assistant when the last successful
run is too old.

The service is off until `COMPOSE_PROFILES` in `docker/.env` includes
`backup`. Raw CSI (`docker/data/csi`), Grafana and the bus are not part of it.

## What a run does

1. Checks that the restic repository exists and its password opens it, and
   removes the stale lock of an interrupted run.
2. Builds a new dump in `<data>/dump.partial`, then puts it in place of the
   previous one:

   | Path | Contents |
   |---|---|
   | `db/` | `pg_dump --format=directory`, made by the read-only `backup` role |
   | `roles.sql` | the roles and their grants, without passwords (`pg_dumpall --roles-only`) |
   | `health/<measurement>.csv` | `apple_health`, `apple_sleep_stage`, `apple_sleep_daily`, `apple_workout` as annotated CSV (the format of an InfluxDB query) |
   | `manifest.json` | time, Postgres and TimescaleDB versions, sizes, health rows per measurement |

   The dump is not compressed: restic compresses what it stores and only
   uploads the parts that changed since the night before.
3. `restic backup` of the dump (tag `nightly`, host `ruview`).
4. `restic forget --prune`: keeps 7 daily, 5 weekly and 6 monthly snapshots.
5. Once a week, `restic check --read-data-subset 5%`.

A step that fails ends the run; nothing is uploaded, and the run is retried
an hour later, at most three times a night. After a restart the service runs
within 10 minutes when the last success is more than 26 hours old.

The dump stays on the NAS (`docker/data/backup/dump`) until the next night's
replaces it. It holds health data, so its files are readable only by the
service's user (uid 65534).

## Setup

### 1. Backblaze B2

In the Backblaze web console:

1. **Buckets → Create a Bucket**: a private bucket, encryption off (restic
   encrypts before uploading), no Object Lock.
2. The bucket's **Lifecycle Settings**: *Keep prior versions for this number
   of days*, 7. Over the S3 API, restic's prune only hides the files it no
   longer needs; B2's default, *Keep all versions*, would keep and bill them
   forever. Seven days leave time to undo a mistaken prune.
3. **Application Keys → Add a New Application Key**: access to this bucket
   only, *Read and Write*. Note the keyID and the applicationKey (shown once).
4. Note the bucket's endpoint, for example `s3.us-west-004.backblazeb2.com`;
   its middle part (`us-west-004`) is the region.

### 2. Settings in `docker/.env`

```bash
COMPOSE_PROFILES=backup

# The bucket, and a folder in it for this repository.
RESTIC_REPOSITORY=s3:https://s3.us-west-004.backblazeb2.com/<bucket>/ruview
# Generate with: openssl rand -hex 32. Without it the backups cannot be
# read: keep a copy outside the NAS (the family's iCloud Passwords).
RESTIC_PASSWORD=
# The application key: keyID and applicationKey, and the bucket's region.
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_DEFAULT_REGION=us-west-004

# The database role the dump is made with: openssl rand -hex 24
PG_BACKUP_PASSWORD=
# A read-only InfluxDB token for the health data (below); empty: no health data.
INFLUXDB_BACKUP_TOKEN=
```

A token that can only read the `sensing` bucket:

```bash
cd /volume1/docker/RuView/docker
bucket=$(docker compose exec -T influxdb influx bucket list --org ruview --name sensing --hide-headers | cut -f1)
docker compose exec -T influxdb influx auth create --org ruview --read-bucket "$bucket" \
  --description ruview-backup
# The Token column goes into INFLUXDB_BACKUP_TOKEN.
```

Optional: `BACKUP_AT` (daily run time in UTC, default `07:30`, which is
03:30 in New York in summer and 02:30 in winter), `BACKUP_KEEP_DAILY`,
`BACKUP_KEEP_WEEKLY`, `BACKUP_KEEP_MONTHLY`, `BACKUP_CHECK_SUBSET` (share of
the data the weekly check reads back, e.g. `5%`; empty checks the structure
only) and `BACKUP_DATA_DIR` (host directory, default `./data/backup`).

### 3. First run

```bash
cd /volume1/docker/RuView/docker
# The data directory must be writable by uid 65534; without sudo:
docker run --rm -v "$PWD/data:/d" alpine:3.20 sh -c 'mkdir -p /d/backup && chown 65534:65534 /d/backup'
docker compose run --rm db-migrate        # gives the backup role its password
docker compose pull backup
docker compose run --rm backup init       # creates the encrypted repository in the bucket
docker compose run --rm backup run        # one backup now
docker compose up -d backup
```

Check:

```bash
docker compose logs --tail 20 backup      # "backup done: snapshot ..., dump ... MB, ... new bytes stored"
docker compose run --rm backup status     # the last result, also on ruview/backup/state
docker compose run --rm --entrypoint restic backup snapshots
```

`docker compose run --rm backup check` verifies the repository at any time.

## Monitoring

The service publishes the result of each run, retained and again every hour,
on `ruview/backup/state`: `ok`, `step` and `error` of a failed run,
`last_ok_ts`, `next_run_ts`, the snapshot, the dump size, the health rows and
the bytes the upload added. The data watchdog (`docker/analytics`) raises
**Backup** when the last success is older than `BACKUP_MAX_AGE_H` (48 hours),
or when the service has run that long without one, and names the failed step.

## Restore

Every restore starts by copying a snapshot back to the NAS:

```bash
cd /volume1/docker/RuView/docker
docker compose run --rm --entrypoint restic backup snapshots
docker compose run --rm --entrypoint restic backup restore latest --target /backup/restore
# The dump is now in data/backup/restore/backup/dump (use a snapshot id instead of latest
# for an older one).
```

### The database

`restore-db.sh` restores into an empty database: a new `timescaledb-data`
volume after the old one was lost, or a scratch server for a drill (below).
It needs the TimescaleDB version that made the dump (`manifest.json`), creates
the roles, restores the database, and leaves the role passwords to db-migrate.

A damaged volume has to go first, which cannot be undone:
`docker compose rm --stop --force timescaledb && docker volume rm docker_timescaledb-data`.

```bash
docker compose stop telegraf grafana analytics
docker compose up -d timescaledb          # the server only: db-migrate must not run first
# The superuser password from .env, passed through the environment, not the command line.
PGPASSWORD="$(grep '^POSTGRES_PASSWORD=' .env | cut -d= -f2-)" docker compose run --rm -e PGPASSWORD \
  --entrypoint /app/restore-db.sh backup /backup/restore/backup/dump
docker compose run --rm db-migrate        # role passwords from .env
docker compose up -d
```

Rows written after the snapshot are lost, except what InfluxDB still holds
until WP1.5.

**Drill**, without touching the running database: restore into a scratch
server on the same network, compare, remove it.

```bash
docker run -d --name ruview-restore-drill --network docker_ruview-data \
  -e POSTGRES_DB=ruview -e POSTGRES_PASSWORD=drill-only-password timescale/timescaledb:2.30.2-pg17
docker compose run --rm -e PGHOST=ruview-restore-drill -e PGPASSWORD=drill-only-password \
  --entrypoint /app/restore-db.sh backup /backup/restore/backup/dump
docker rm -f ruview-restore-drill
```

The script prints the restored row counts of `ingest.state`,
`ingest.annotation` and `agg.state_1h`.

### The health data

Each CSV goes back into InfluxDB as it is; a point that is already there is
overwritten with the same value.

```bash
for m in apple_health apple_sleep_stage apple_sleep_daily apple_workout; do
  docker compose run -T --rm --entrypoint cat backup /backup/restore/backup/dump/health/$m.csv |
    docker compose exec -T influxdb influx write --org ruview --bucket sensing --format csv
done
```

The files are readable only by uid 65534, hence the `cat` in the backup
container. Read them from stdin as above: `influx write --file` waits for
stdin when it is not a terminal.

When InfluxDB is retired (WP1.5), the health data lives in TimescaleDB and
the database dump covers it: remove `INFLUXDB_BACKUP_TOKEN` and run
`docker compose up -d backup`.

Remove `data/backup/restore` when done.

## Size and cost

The database's raw rows are kept 14 days, so each monthly snapshot holds its
own 14 days of them; the aggregates and the health data are shared by all
snapshots. With 7 daily, 5 weekly and 6 monthly snapshots the repository
holds about four months of raw rows. What it holds now:

```bash
docker compose run --rm --entrypoint restic backup stats --mode raw-data
```

B2 bills the stored bytes, including hidden files for the 7 days of the
lifecycle rule. CLAIMED (Backblaze's list price, October 2026): the first
10 GB are free, then $6.95 per TB per month.

## Security

- restic encrypts and authenticates everything before it leaves the NAS; B2
  sees only random-looking files. Whoever holds `RESTIC_PASSWORD` and the key
  can read the backups: neither goes into git or a chat.
- The application key reaches one bucket. It can delete there, so the
  lifecycle rule is a safety net against mistakes, not against someone who
  has the key.
- The `backup` database role reads everything (`pg_read_all_data`) and
  cannot write: no table privilege, and its sessions are read-only.
- restic is given only the `RESTIC_*`, `AWS_*` and `B2_*` variables, the
  Postgres tools only their `PG*` settings.
- The container runs as uid 65534 with no capabilities and a read-only root
  file system; it writes only to its data directory.

## Upgrading

The image is built on `timescale/timescaledb`: keep the tag in
[Dockerfile](Dockerfile) in step with the `timescaledb` service, because
pg_dump refuses a server newer than itself and `restore-db.sh` needs the
TimescaleDB version of the dump.

## Tests

```bash
cd docker/backup
python -m pip install -r requirements.txt pytest
python -m pytest -q
```

The tests replace the database, InfluxDB and restic with fakes.
[`.github/workflows/backup.yml`](../../.github/workflows/backup.yml) runs the
image against the compose TimescaleDB and InfluxDB with sample data
(`tests/seed.sql`, `tests/health_samples.py`) and a local restic repository,
then follows the restore steps above: the drill, a new `timescaledb-data`
volume and a new InfluxDB bucket, each compared with the original
(`tests/digest.sql`: every table and aggregate, the roles, privileges,
chunks and policies).
