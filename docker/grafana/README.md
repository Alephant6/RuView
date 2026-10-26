# Grafana dashboards (WP1.3)

Grafana (port 3030 on the NAS) reads TimescaleDB through the read-only
`grafana` role. Its datasource and dashboards are provisioned from this
directory at every start, into the **RuView** folder:

| Dashboard | Default range | What it answers |
|---|---|---|
| RuView — Data Health | last 24 h | Is the data flowing and trustworthy? Newest row, live CSI share, fresh nodes, the watchdog's checks over time, the bridge stream, CSI rate, RSSI and offline share per node, the raw CSI recorder, why vitals were withheld, rows per ingest table, storage and TimescaleDB's jobs. |
| RuView — Now and Last Night | last 24 h | The room now (state, people estimate, heart and breathing rate when published, why not, calibration), the day as timelines, person slots, the server's own states per node, a summary of last night (21:00 to 09:00 at home) and the newest events, labels and kept clips. |
| RuView — Trends | last 30 days | Day by day from the hourly aggregates, which are kept for good: hours with live CSI and with someone present, time by room state, night and day heart and breathing rates, vitals availability, hours asleep per the server, people, watchdog problems and node health. |

Heart and breathing rates are room-level CSI estimates, published only with
exactly one person in the room and a fresh empty-room calibration; they are
not medical measurements. Days and nights are counted in the home's time zone,
the hidden `tz` variable of the Now and Trends dashboards
(America/New_York).

A comparison with the bedside radar, the reference for night heart and
breathing rates, comes once its data is in TimescaleDB (WP1.4, WP2.2).

## Files

- `provisioning/datasources/timescaledb.yml`: the default datasource, uid
  `ruview-timescaledb`. The password is `PG_GRAFANA_PASSWORD` from
  `docker/.env`; db-migrate gives the role the same one.
- `provisioning/datasources/influxdb.yml`: InfluxDB, kept for Explore until
  it is retired (WP1.5). No dashboard uses it.
- `provisioning/dashboards/dashboards.yml`: loads every JSON file in
  `dashboards/`, re-read every 30 s.
- `dashboards/*.json`: the dashboards. Every query is SQL on the `ingest`
  tables or the `agg` continuous aggregates
  ([timescaledb/README.md](../timescaledb/README.md)).
- `tests/seed.sql`: 35 days of SYNTHETIC household rows in every ingest
  table, for the CI check and for trying a change locally. Not measured data.
- `tests/check_dashboards.py`: runs every panel and annotation query through
  Grafana's query API.

## Changing a dashboard

Edit it in Grafana, then *Edit → Settings → JSON Model*, copy it into the
file under `dashboards/` and open a pull request. Edits saved only in the UI
are lost the next time the file changes. A new panel must use the
`ruview-timescaledb` datasource, or the check fails.

CI ([.github/workflows/grafana.yml](../../.github/workflows/grafana.yml))
starts the compose `timescaledb` and `grafana` services, loads the seed and
fails on any query error, and on any panel that follows the time picker but
finds nothing in the seed. The check also runs against the NAS (read-only),
where a quiet table can make a panel legitimately empty:

```bash
GRAFANA_PASSWORD=... python3 docker/grafana/tests/check_dashboards.py \
  --url http://127.0.0.1:3030 docker/grafana/dashboards
```

Load the seed only into a test database: it adds rows to the ingest tables.
