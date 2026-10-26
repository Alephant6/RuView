# RuView docker stack

Ten containers (db-migrate exits once the database migrations are
applied), and an eleventh, `backup`, when `COMPOSE_PROFILES` includes
`backup`. The sensing-server and the csi-recorder use the host network. The
NAS message bus and its clients share the `ruview-bus` network, the databases
and Grafana the `ruview-data` network; Telegraf is on both.

```
ESP32 nodes ──UDP:5005──▶ sensing-server ──udp tee :5006──▶ csi-recorder
                           │         │                          │
                 WebSocket │         │ built-in MQTT publisher  │
                           ▼         ▼                          │
          mqtt-bridge ──▶ mosquitto: the NAS bus ◀──────────────┘
          analytics   ◀─▶  │   ║
                           │   ║ bridge: only the topics HA uses,
                           │   ║ label buttons come back
                           │   ╚══▶ HA Mosquitto (LAN) ──▶ Home Assistant
                           ▼
                       telegraf ──▶ influxdb:8086 (until WP1.5)
                                └─▶ timescaledb:5432 ◀── db-migrate
                                          ▲
                                       grafana
```

The host-network services reach the bus on the NAS loopback
(`127.0.0.1`, port `MQTT_BUS_PORT`, 1883 by default), the containers as
`mosquitto:1883`. InfluxDB and Grafana are published on the host (8086,
3030), TimescaleDB on the NAS loopback only (`PG_PORT`, 5432 by default).

## Services

| Service | Image | Host port | What it does |
|---------|-------|-----------|--------------|
| `mosquitto` | `eclipse-mosquitto:2.0.22` | 127.0.0.1:`MQTT_BUS_PORT` (1883) | NAS message bus; bridges the topics Home Assistant uses to its broker ([mosquitto/README.md](mosquitto/README.md)) |
| `sensing-server` | local build (Dockerfile.rust) | 3000, 3001, 5005/udp | Rust DSP pipeline, eats CSI, emits SensingUpdate on WS |
| `mqtt-bridge` | local build (bridge/Dockerfile) | — | WS → MQTT translator + HA Discovery |
| `analytics` | local build (analytics/Dockerfile) | — | Data watchdog: nodes, CSI source, frozen vitals, calibration, recorder → HA "RuView Watchdog" entities; optional automatic empty-room calibration while nobody is home |
| `csi-recorder` | local build (csi-recorder/Dockerfile) | — (loopback 5006/udp) | Raw datagrams from the server's UDP tee: 72 h rolling files plus permanent labelled clips (`docker/data/csi`) |
| `influxdb` | `influxdb:2.7` | 8086 | Time-series store, retired after WP1.5 |
| `timescaledb` | `timescale/timescaledb:2.30.2-pg17` | 127.0.0.1:`PG_PORT` (5432) | Long-term SQL store: raw rows 14 days, per-minute and per-hour aggregates forever ([timescaledb/README.md](timescaledb/README.md)) |
| `db-migrate` | same image | — | Applies `timescaledb/migrations` and the roles' passwords, then exits |
| `telegraf` | `telegraf:1.39.3` | — | MQTT → InfluxDB + TimescaleDB ingest |
| `grafana` | `grafana/grafana:11.2.0` | 3030 | Dashboards (auto-provisioned) |
| `backup` | local build (backup/Dockerfile), profile `backup` | — | Nightly dump of TimescaleDB and the Apple Health data, encrypted by restic and uploaded to Backblaze B2 ([backup/README.md](backup/README.md)) |

## First-time bringup

```bash
cd /volume1/docker/RuView/docker

# 1. Optional: override defaults via .env
cat > .env <<'EOF'
# Home Assistant's broker: the bus bridges to it with this login
MQTT_HOST=192.168.1.227
MQTT_USER=mqtt-bridge
MQTT_PASS=mqtt-bridge-pass

# InfluxDB credentials — CHANGE THESE before exposing beyond LAN
INFLUXDB_USER=admin
INFLUXDB_PASSWORD=ruview-admin-CHANGE-ME
INFLUXDB_ORG=ruview
INFLUXDB_BUCKET=sensing
INFLUXDB_RETENTION=30d
INFLUXDB_TOKEN=ruview-local-dev-token-CHANGE-ME

# Grafana
GRAFANA_USER=admin
GRAFANA_PASSWORD=ruview-admin-CHANGE-ME
GRAFANA_PORT=3030

# TimescaleDB (required): generate each with `openssl rand -hex 24`
POSTGRES_PASSWORD=
PG_TELEGRAF_PASSWORD=
PG_GRAFANA_PASSWORD=
EOF

# 2. Bring everything up
sudo docker compose up -d --build

# 3. First-boot verification
sudo docker compose ps -a           # all "running" except db-migrate, "exited (0)"
sudo docker compose exec mosquitto mosquitto_sub -t ruview/bus/ha_connected -C 1 -W 5   # 1: bridged to HA
sudo docker compose logs telegraf | grep -i "Successfully connected"
sudo docker compose logs influxdb | grep -i "Setup complete"

# 4. Open Grafana at http://<nas-ip>:3030
#    Default login: admin / value of GRAFANA_PASSWORD
#    Dashboards live under "RuView" folder (provisioned automatically):
#      - Node Health   (use this NOW for diagnosing RSSI)
#      - Vital Signs
#      - Presence & People
```

## MQTT topics written by the stack

All of them go through the NAS bus. "To HA" marks the ones the bus bridges to
Home Assistant's broker (→) or from it (←); the list lives in
[mosquitto/start.sh](mosquitto/start.sh).

| Topic | Writer | Rate | To HA | Notes |
|---|---|---|---|---|
| `homeassistant/…` | sensing-server, mqtt-bridge, analytics | per entity | → | entity configs (retained); the sensing-server's built-in publisher also sends its states and availability here |
| `ruview/availability` (retained) | mqtt-bridge | on connect, will | → | online / offline |
| `ruview/state` | mqtt-bridge | one per new server tick, ≤ `STATE_MAX_HZ` (10) | | withheld HR/BR are `null`, never 0; quality fields `vitals_present`, `stale_source`, `abstention_reason`, `cal_person_count`, ... |
| `ruview/nodes/<id>` | mqtt-bridge | ≤ `NODE_MAX_HZ` (1) per node | → | `csi_fps`, `stale`, `last_seen_ms`, RSSI |
| `ruview/person/<slot or label>` | mqtt-bridge | on change, ≤ 1 Hz, 30 s heartbeat | → | availability subtopics only on transitions |
| `ruview/event` | mqtt-bridge | per fall event | → | |
| `ruview/bridge` (retained) | mqtt-bridge | every 10 s and on change | | WS connected, source, age of the last new tick, message rates |
| `ruview/watchdog/state` (retained) | analytics | every 30 s | → | full watchdog report; HA entities read it |
| `ruview/watchdog/availability` (retained) | analytics | on connect, will | → | |
| `ruview/watchdog/metrics` | analytics | every 30 s | | flat numbers for Telegraf (`watchdog` measurement) |
| `ruview/recorder/status` (retained) | csi-recorder | every 30 s | | datagram rates per kind and node, ring size and age, free disk, keep budget (`recorder` measurement) |
| `ruview/recorder/keep` | HA label buttons, analytics | on request | ← | keep a time range of raw CSI permanently |
| `ruview/recorder/keep/result` | csi-recorder | per request | → | accepted / rejected / kept / empty / failed (`recorder_keep` measurement) |
| `ruview/backup/state` (retained) | backup | after each run, and hourly | | result of the last offsite backup, last success, next run; the watchdog's Backup check reads it |
| `ruview/annotation` | HA label buttons, analytics | on press | ← | alcohol, sick, travel, quiet sitting, changes, ...; the start and end of each automatic calibration (`annotation` measurement, stored at the press time) |
| `ruview/bus/ha_connected` (retained) | the bus bridge | on connect and drop | both | 1 / 0, shown in HA as "NAS Link"; HA's broker writes the 0 itself |

The sensing-server re-sends its latest update at 10 Hz even when the ESP32s
are gone (as `esp32:offline`). The bridge drops those repeats, so a stalled
source shows up as `ruview/state` going quiet while `ruview/bridge` keeps
reporting `new_ticks_hz: 0` — which the watchdog turns into a "CSI Source"
problem in Home Assistant.

## Querying InfluxDB directly

```bash
# Token is in your .env (or the docker-compose default).
curl -sG --data-urlencode 'org=ruview' \
     --data-urlencode 'q=from(bucket:"sensing") |> range(start:-5m) |> filter(fn:(r) => r._measurement=="node") |> last()' \
     -H 'Accept: application/csv' \
     -H "Authorization: Token ${INFLUXDB_TOKEN}" \
     http://<nas-ip>:8086/api/v2/query
```

## Querying TimescaleDB

```bash
sudo docker compose exec timescaledb psql -U postgres ruview \
  -c "SELECT bucket, n_live, hr_med, br_med FROM agg.state_1m ORDER BY bucket DESC LIMIT 10"
```

Tables, roles, retention and remote access: [timescaledb/README.md](timescaledb/README.md).

## Adding a custom dashboard

1. Build it in Grafana UI.
2. *Settings → JSON Model → Copy*, paste into a new file under
   `docker/grafana/dashboards/<name>.json`.
3. Commit to the branch — Grafana picks it up within 30 s on next deploy
   (provisioning re-scans the directory). No restart needed.

## Persistence

InfluxDB, TimescaleDB, Grafana and the bus use **named docker volumes**
(`influxdb-data`, `influxdb-config`, `timescaledb-data`, `grafana-data`,
`mosquitto-data`) so a `docker compose down` keeps your data.
`docker compose down -v` will wipe them — don't do that if you care about
history. The database and the health data also go offsite every night; the
restore is in [backup/README.md](backup/README.md).

## Troubleshooting

- **Telegraf can't reach MQTT** — it reads the NAS bus, not Home Assistant's
  broker: check `sudo docker compose ps mosquitto` shows it healthy.
- **HA shows old RuView values, "NAS Link" is off** — the bus cannot reach
  Home Assistant's broker. `sudo docker compose logs mosquitto | tail` shows
  `Connecting bridge ha (<host>:<port>)` at each attempt. `Connection Refused:
  not authorised` after it means `MQTT_USER` / `MQTT_PASS` are wrong; attempts
  without any answer mean `MQTT_HOST` is unreachable (use the broker's IP).
  The database keeps recording meanwhile. See
  [mosquitto/README.md](mosquitto/README.md).
- **Port 1883 already used on the NAS** — set `MQTT_BUS_PORT` in `.env`.
- **InfluxDB rejects the Telegraf token** — they must agree on
  `INFLUXDB_TOKEN`. Bounce both: `sudo docker compose restart influxdb
  telegraf`.
- **Grafana shows empty graphs** — first check Telegraf is writing:
  `sudo docker compose logs telegraf | tail -50`. If it has lines like
  `wrote N metrics`, the issue is the dashboard query, not the pipeline.
- **Port 3030 conflict** — change `GRAFANA_PORT` in `.env`.
