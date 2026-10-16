# RuView docker stack

Nine containers (db-migrate exits once the database migrations are
applied), the host network for the sensing-server and the csi-recorder, and
one bridge network (`ruview-data`) for the persistence + visualisation layer.

```
┌─ host network ─────────────────────────────────────────┐
│  ESP32 nodes ──UDP:5005──▶ sensing-server (3000/3001)  │
│  csi-recorder ◀── udp tee :5006 ─┤                     │
│                                  ▼ WebSocket           │
│  mqtt-bridge ───────────▶ HA Mosquitto (LAN: 1883)     │
│  analytics (watchdog) ◀── REST 3000 + MQTT ──▶ HA       │
└────────────────────────────────────│────────────────────┘
                                     │ MQTT subscribe
                                     ▼
                              ┌─ ruview-data ─────────────────────┐
                              │  telegraf                         │
                              │     │ writes both (until WP1.5)   │
                              │     ▼                             │
                              │  influxdb:8086 ──────────────────┼──▶ exposed on host:8086
                              │  timescaledb:5432 ◀── db-migrate ┼──▶ NAS loopback PG_PORT
                              │     ▲                             │
                              │     │ reads                       │
                              │  grafana:3000  ──────────────────┼──▶ exposed on host:3030
                              └───────────────────────────────────┘
```

## Services

| Service | Image | Host port | What it does |
|---------|-------|-----------|--------------|
| `sensing-server` | local build (Dockerfile.rust) | 3000, 3001, 5005/udp | Rust DSP pipeline, eats CSI, emits SensingUpdate on WS |
| `mqtt-bridge` | local build (bridge/Dockerfile) | — | WS → MQTT translator + HA Discovery |
| `analytics` | local build (analytics/Dockerfile) | — | Data watchdog: nodes, CSI source, frozen vitals, calibration, recorder → HA "RuView Watchdog" entities |
| `csi-recorder` | local build (csi-recorder/Dockerfile) | — (loopback 5006/udp) | Raw datagrams from the server's UDP tee: 72 h rolling files plus permanent labelled clips (`docker/data/csi`) |
| `influxdb` | `influxdb:2.7` | 8086 | Time-series store, retired after WP1.5 |
| `timescaledb` | `timescale/timescaledb:2.30.2-pg17` | 127.0.0.1:`PG_PORT` (5432) | Long-term SQL store: raw rows 14 days, per-minute and per-hour aggregates forever ([timescaledb/README.md](timescaledb/README.md)) |
| `db-migrate` | same image | — | Applies `timescaledb/migrations` and the roles' passwords, then exits |
| `telegraf` | `telegraf:1.39.3` | — | MQTT → InfluxDB + TimescaleDB ingest |
| `grafana` | `grafana/grafana:11.2.0` | 3030 | Dashboards (auto-provisioned) |

## First-time bringup

```bash
cd /volume1/docker/RuView/docker

# 1. Optional: override defaults via .env
cat > .env <<'EOF'
# MQTT (already used by mqtt-bridge)
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

| Topic | Writer | Rate | Notes |
|---|---|---|---|
| `ruview/state` | mqtt-bridge | one per new server tick, ≤ `STATE_MAX_HZ` (10) | withheld HR/BR are `null`, never 0; quality fields `vitals_present`, `stale_source`, `abstention_reason`, `cal_person_count`, ... |
| `ruview/nodes/<id>` | mqtt-bridge | ≤ `NODE_MAX_HZ` (1) per node | `csi_fps`, `stale`, `last_seen_ms`, RSSI |
| `ruview/person/<slot or label>` | mqtt-bridge | on change, ≤ 1 Hz, 30 s heartbeat | availability subtopics only on transitions |
| `ruview/bridge` (retained) | mqtt-bridge | every 10 s and on change | WS connected, source, age of the last new tick, message rates |
| `ruview/watchdog/state` (retained) | analytics | every 30 s | full watchdog report; HA entities read it |
| `ruview/watchdog/metrics` | analytics | every 30 s | flat numbers for Telegraf (`watchdog` measurement) |
| `ruview/recorder/status` (retained) | csi-recorder | every 30 s | datagram rates per kind and node, ring size and age, free disk, keep budget (`recorder` measurement) |
| `ruview/recorder/keep` | HA label buttons, analytics | on request | keep a time range of raw CSI permanently |
| `ruview/recorder/keep/result` | csi-recorder | per request | accepted / rejected / kept / empty / failed (`recorder_keep` measurement) |
| `ruview/annotation` | HA label buttons | on press | alcohol, sick, travel, quiet sitting, changes, ... (`annotation` measurement, stored at the press time) |

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

InfluxDB, TimescaleDB and Grafana use **named docker volumes**
(`influxdb-data`, `influxdb-config`, `timescaledb-data`, `grafana-data`) so a
`docker compose down` keeps your data. `docker compose down -v` will wipe them — don't do that if you care
about history.

## Troubleshooting

- **Telegraf can't reach MQTT** — check `MQTT_HOST` resolves from inside the
  `ruview-data` network. From the NAS: `sudo docker compose exec telegraf
  getent hosts ${MQTT_HOST}`. If it fails, use the broker's IP literally.
- **InfluxDB rejects the Telegraf token** — they must agree on
  `INFLUXDB_TOKEN`. Bounce both: `sudo docker compose restart influxdb
  telegraf`.
- **Grafana shows empty graphs** — first check Telegraf is writing:
  `sudo docker compose logs telegraf | tail -50`. If it has lines like
  `wrote N metrics`, the issue is the dashboard query, not the pipeline.
- **Port 3030 conflict** — change `GRAFANA_PORT` in `.env`.
