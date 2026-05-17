# RuView docker stack

Five containers, one network namespace for the sensing-server (host) and
one bridge network (`ruview-data`) for the persistence + visualisation layer.

```
┌─ host network ─────────────────────────────────────────┐
│  ESP32 nodes ──UDP:5005──▶ sensing-server (3000/3001)  │
│                                  │                      │
│                                  ▼ WebSocket            │
│  mqtt-bridge ───────────▶ HA Mosquitto (LAN: 1883)     │
└────────────────────────────────────│────────────────────┘
                                     │ MQTT subscribe
                                     ▼
                              ┌─ ruview-data ─────────┐
                              │  telegraf             │
                              │     │ writes          │
                              │     ▼                 │
                              │  influxdb:8086 ──────┼──▶ exposed on host:8086
                              │     ▲                 │
                              │     │ reads           │
                              │  grafana:3000  ──────┼──▶ exposed on host:3030
                              └───────────────────────┘
```

## Services

| Service | Image | Host port | What it does |
|---------|-------|-----------|--------------|
| `sensing-server` | local build (Dockerfile.rust) | 3000, 3001, 5005/udp | Rust DSP pipeline, eats CSI, emits SensingUpdate on WS |
| `mqtt-bridge` | local build (bridge/Dockerfile) | — | WS → MQTT translator + HA Discovery |
| `influxdb` | `influxdb:2.7` | 8086 | Time-series store |
| `telegraf` | `telegraf:1.30` | — | MQTT → InfluxDB ingest |
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
EOF

# 2. Bring everything up
sudo docker compose up -d --build

# 3. First-boot verification
sudo docker compose ps              # all five should be "running"
sudo docker compose logs telegraf | grep -i "Successfully connected"
sudo docker compose logs influxdb | grep -i "Setup complete"

# 4. Open Grafana at http://<nas-ip>:3030
#    Default login: admin / value of GRAFANA_PASSWORD
#    Dashboards live under "RuView" folder (provisioned automatically):
#      - Node Health   (use this NOW for diagnosing RSSI)
#      - Vital Signs
#      - Presence & People
```

## Querying InfluxDB directly

```bash
# Token is in your .env (or the docker-compose default).
curl -sG --data-urlencode 'org=ruview' \
     --data-urlencode 'q=from(bucket:"sensing") |> range(start:-5m) |> filter(fn:(r) => r._measurement=="node") |> last()' \
     -H 'Accept: application/csv' \
     -H "Authorization: Token ${INFLUXDB_TOKEN}" \
     http://<nas-ip>:8086/api/v2/query
```

## Adding a custom dashboard

1. Build it in Grafana UI.
2. *Settings → JSON Model → Copy*, paste into a new file under
   `docker/grafana/dashboards/<name>.json`.
3. Commit to the branch — Grafana picks it up within 30 s on next deploy
   (provisioning re-scans the directory). No restart needed.

## Persistence

InfluxDB and Grafana use **named docker volumes** (`influxdb-data`,
`influxdb-config`, `grafana-data`) so a `docker compose down` keeps your
data. `docker compose down -v` will wipe them — don't do that if you care
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
