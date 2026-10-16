# RuView analytics — data watchdog (v0)

Watches the data path and tells Home Assistant when something is wrong:
nodes dropping out, the CSI source going stale while the server keeps
re-sending old data, heart/breathing rates frozen at one value, missing or
expiring empty-room calibration, and a room that reads "occupied" while
every phone is away. Built after September 2026, when nodes 1 and 2 were
down for 30 days and the heart rate sat at 83.37 bpm without anyone
noticing. Work package WP0.4 in the architecture plan.

It only reads: MQTT topics from the bridge, the sensing-server REST API
(GET only) and, optionally, `person.*` states from the HA REST API. It keeps
15–30 minute windows in memory and writes nothing to disk.

## Inputs

| Source | What |
|---|---|
| `ruview/availability` | bridge LWT |
| `ruview/bridge` | bridge stream health: WS connected, source, age of the last new tick, rates |
| `ruview/state` | room state: HR/BR (null when withheld), presence, calibrated person count |
| `ruview/nodes/<id>` | per-node fps / stale flag (fallback for old servers) |
| `ruview/recorder/status`, `ruview/recorder/availability` | csi-recorder health (WP0.5) |
| `GET /health`, `/api/v1/status`, `/api/v1/stream/status`, `/api/v1/nodes`, `/api/v1/calibration/status` | server reachability, source state, per-node CSI fps, calibration state |
| `GET <HA_URL>/api/states/<person>` | optional: who is home |

## Checks

| Check | Raises when | Severity | Delay |
|---|---|---|---|
| Sensing Server | `/health` unreachable | critical | 90 s |
| API Access | `/api/v1/*` answers 401/403 | warning | — |
| Bridge | bridge LWT offline, silent > 60 s, or its WebSocket is down while the server is up | critical | 60 s |
| CSI Source | source `…:offline`, source_state stale/disconnected/synthetic, or no new tick for > 60 s | critical | 60 s |
| Node N | node missing from the fresh-node list, or CSI below `MIN_NODE_FPS` | warning | 120 s |
| Vitals Frozen | ≥ 100 samples over ≥ 10 min with ≤ 2 distinct values | warning | — |
| Vitals Range | > 5 % of HR outside 35–180 or BR outside 4–40 | warning | — |
| Calibration | stale (info), or none/expired/bootstrap-only (warning) | info / warning | — |
| Recorder | csi-recorder offline, status silent > 90 s, disk low (recording paused), write errors, or no datagrams for > 60 s while nodes are sending | warning | 120 s |
| Presence vs Phones | all phones away ≥ 1 h but occupied > 80 % of the last 30 min | warning | — |

Node checks are held while the server or the source is down, so one outage
sends one notification. Every check also needs its condition to be gone for
a while before it clears. The Recorder check stays unknown until a
csi-recorder has published once.

When Vitals Frozen, Vitals Range or Presence vs Phones is raised and a
csi-recorder is running, the watchdog asks it (`ruview/recorder/keep`) to
keep the raw CSI of the previous `KEEP_ON_RAISE_MIN` minutes, so the data
behind the problem can be replayed after a fix.

## Outputs

- `ruview/watchdog/state` (retained): the full report — status, open
  issues, each check's state, summary, severity and start time.
- `ruview/watchdog/metrics`: flat numbers for Telegraf / Grafana.
- `ruview/watchdog/availability`: `online` / `offline` (LWT).
- `ruview/recorder/keep`: keep requests for raised vitals/presence problems.
- Home Assistant entities under the device **RuView Watchdog**: Problem,
  Status, Open Issues, Top Issue, CSI Vitals, Last Check, and one problem
  sensor per check.

`ha/watchdog-notify.yaml` has example automations that push new problems
and recoveries to a phone, and alert when the watchdog itself is offline.
`ha/ruview-labels.yaml` is the Home Assistant package with the label
buttons (alcohol, sick, quiet sitting, change log, ...), see WP0.6.

## Configuration

| Variable | Default | |
|---|---|---|
| `RUVIEW_HTTP` | `http://192.168.1.195:3000` | sensing-server REST base URL; its host must be in the server's `SENSING_ALLOWED_HOSTS`, or the server answers HTTP 421 |
| `RUVIEW_API_TOKEN` | — | same token as the server |
| `MQTT_HOST` / `MQTT_PORT` / `MQTT_USER` / `MQTT_PASS` | | broker the bridge publishes to |
| `MQTT_PREFIX` / `HA_DISCOVERY_PREFIX` | `ruview` / `homeassistant` | |
| `EXPECTED_NODES` | `1,2,3` | node ids that should be online |
| `MIN_NODE_FPS` | `5` | |
| `CHECK_INTERVAL_S` | `30` | |
| `HA_URL` / `HA_TOKEN` / `HA_PERSONS` | — | enable the phone check, e.g. `person.alice,person.bob` |
| `WATCHDOG_LANG` | `zh` | `zh` or `en` for summaries |
| `KEEP_ON_RAISE_MIN` | `20` | raw CSI minutes to keep on a vitals/presence problem; `0` = off |

`HA_TOKEN` is a long-lived access token; keep it in `docker/.env` only.

## Deploy

CI builds the image as `ghcr.io/alephant6/ruview-analytics`
(`.github/workflows/fork-images.yml`). On the NAS, with `RUVIEW_API_TOKEN`
(and optionally the `HA_*` settings) in `docker/.env`, after `git pull` in the
checkout and with `IMAGE_TAG` in `.env` set to the merge commit's `sha-<7>`
once `fork-images.yml` has finished for it:

```bash
docker compose pull mqtt-bridge analytics
docker compose up -d mqtt-bridge analytics
docker compose restart telegraf   # picks up the bridge / watchdog inputs
```

Then add the automations from `ha/watchdog-notify.yaml` to Home Assistant.

## Tests

```bash
cd docker/analytics
python -m pip install paho-mqtt pytest jinja2
python -m pytest -q
```
