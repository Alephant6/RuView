# RuView analytics — data watchdog (v0)

Watches the data path and tells Home Assistant when something is wrong:
nodes dropping out, the CSI source going stale while the server keeps
re-sending old data, heart/breathing rates frozen at one value, missing or
expiring empty-room calibration, and a room that reads "occupied" while
every phone is away. Built after September 2026, when nodes 1 and 2 were
down for 30 days and the heart rate sat at 83.37 bpm without anyone
noticing. Work package WP0.4 in the architecture plan.

It reads MQTT topics from the bridge, the sensing-server REST API and,
optionally, `person.*` states from the HA REST API. Its only writes are the
calibration calls of [automatic calibration](#automatic-empty-room-calibration),
which is off unless `AUTOCAL_SOURCE_NODE` is set. It keeps 15–30 minute
windows in memory and writes nothing to disk.

## Inputs

| Source | What |
|---|---|
| `ruview/availability` | bridge LWT |
| `ruview/bridge` | bridge stream health: WS connected, source, age of the last new tick, rates |
| `ruview/state` | room state: HR/BR (null when withheld), presence, calibrated person count, motion level |
| `ruview/nodes/<id>` | per-node fps / stale flag (fallback for old servers) |
| `ruview/recorder/status`, `ruview/recorder/availability` | csi-recorder health (WP0.5) |
| `ruview/backup/state` | result of the last offsite backup ([docker/backup](../backup/README.md)) |
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
| Calibration | stale (info), or none/expired/bootstrap-only, or a CSI sequence fault on a bound node (warning) | info / warning | — |
| Recorder | csi-recorder offline, status silent > 90 s, disk low (recording paused), write errors, or no datagrams for > 60 s while nodes are sending | warning | 120 s |
| Backup | the last successful offsite backup is older than `BACKUP_MAX_AGE_H` (48 h), or the backup service has run that long without one; names the step the last run failed at | warning | — |
| Presence vs Phones | all phones away ≥ 1 h but occupied > 80 % of the last 30 min | warning | — |

Node checks are held while the server or the source is down, so one outage
sends one notification. Every check also needs its condition to be gone for
a while before it clears. The Recorder check stays unknown until a
csi-recorder has published once, the Backup check until the backup service
has.

When Vitals Frozen, Vitals Range or Presence vs Phones is raised and a
csi-recorder is running, the watchdog asks it (`ruview/recorder/keep`) to
keep the raw CSI of the previous `KEEP_ON_RAISE_MIN` minutes, so the data
behind the problem can be replayed after a fix.

## Automatic empty-room calibration

The sensing-server publishes CSI heart and breathing rates only while an
explicit empty-room calibration is fresh (12 h) and exactly one person is in
the room. With `AUTOCAL_SOURCE_NODE` set (plan §5.2, WP1.2), the watchdog
renews that calibration whenever the home is empty:

- **When.** The calibration is missing, stale, expired, only a startup
  baseline restored after a restart, broken by a CSI sequence fault, or
  fresh but older than `AUTOCAL_REFRESH_H`. Every `HA_PERSONS` entity has
  been away (not `home`, not unknown) for `AUTOCAL_AWAY_MIN`, at most
  `AUTOCAL_MAX_MOVING` of the room states of the last 10 minutes were
  `present_moving`, and the source node is streaming.
- **How.** A fresh calibration is first reset (`POST
  /api/v1/calibration/reset`, which the server requires before a new one).
  The capture binds `AUTOCAL_SOURCE_NODE` alone: the model is built from
  that node's CSI either way, and binding more nodes would only let a
  reboot of any of them block the vitals. The watchdog finalizes it
  (`stop`) once the server's targets are met, 1000 frames and 10 minutes.
- **Cancel.** Someone comes home, more than `AUTOCAL_MAX_MOVING` of the last
  2 minutes are `present_moving`, a sequence fault, no frames after 2
  minutes, or no result after 30 minutes. The next try comes 30 minutes
  later at the earliest.
- **Never.** It does not touch a capture it did not start (another binding
  digest) and does not reset while a promoted startup baseline is stored,
  since a reset would delete it; such a calibration is renewed once it is
  stale. Servers from before the bound calibration API are left alone.

Each capture publishes an annotation on `ruview/annotation` when it starts
and ends (`kind` `calibration`, `label` `empty_room`, `action` `start`,
`done`, `cancelled` or `failed`, `source` `watchdog`), which Telegraf stores
next to the label buttons' annotations. The **Auto Calibration** entity shows
what it is doing or waiting for.

`AUTOCAL_MAX_MOVING` = 0.25 is a starting point, not a measured value: how
often the uncalibrated motion level reads `present_moving` in an empty room
is not known yet. While a calibration is due, the log names what it waits
for, with the moving share of the last 10 minutes
(`docker logs ruview-analytics | grep "calibration due"`).

## Outputs

- `ruview/watchdog/state` (retained): the full report — status, open
  issues, each check's state, summary, severity and start time.
- `ruview/watchdog/metrics`: flat numbers for Telegraf / Grafana; with
  automatic calibration also `autocal_collecting` and `calibration_age_h`.
- `ruview/watchdog/availability`: `online` / `offline` (LWT).
- `ruview/recorder/keep`: keep requests for raised vitals/presence problems.
- `ruview/annotation`: start and end of each automatic calibration.
- Home Assistant entities under the device **RuView Watchdog**: Problem,
  Status, Open Issues, Top Issue, CSI Vitals, Last Check, Auto Calibration
  (with `AUTOCAL_SOURCE_NODE`), and one problem sensor per check. All but
  Last Check go unavailable when no report arrives for 5 minutes
  (`expire_after`), so a stalled watchdog shows like a stopped one.
- **NAS Link** in the same device: whether the NAS bus is bridged to Home
  Assistant's broker. It reads `ruview/bus/ha_connected`, which that broker
  sets to 0 itself when the bridge drops, so it also works while nothing
  from the NAS arrives ([../mosquitto/README.md](../mosquitto/README.md)).

`ha/watchdog-notify.yaml` has example automations that push new problems
and recoveries to a phone, and alert when the watchdog itself is offline or
stalled, or the NAS link has been down for 5 minutes. While the NAS link is
down only the link alert fires.
`ha/ruview-labels.yaml` is the Home Assistant package with the label
buttons (alcohol, sick, quiet sitting, change log, ...), see WP0.6.

## Configuration

| Variable | Default | |
|---|---|---|
| `RUVIEW_HTTP` | `http://192.168.1.195:3000` | sensing-server REST base URL; its host must be in the server's `SENSING_ALLOWED_HOSTS`, or the server answers HTTP 421 |
| `RUVIEW_API_TOKEN` | — | same token as the server |
| `MQTT_HOST` / `MQTT_PORT` / `MQTT_USER` / `MQTT_PASS` | | broker the bridge publishes to (the NAS bus in docker-compose) |
| `MQTT_PREFIX` / `HA_DISCOVERY_PREFIX` | `ruview` / `homeassistant` | |
| `EXPECTED_NODES` | `1,2,3` | node ids that should be online |
| `MIN_NODE_FPS` | `5` | |
| `CHECK_INTERVAL_S` | `30` | |
| `HA_URL` / `HA_TOKEN` / `HA_PERSONS` | — | enable the phone check, e.g. `person.alice,person.bob` |
| `WATCHDOG_LANG` | `zh` | `zh` or `en` for summaries |
| `KEEP_ON_RAISE_MIN` | `20` | raw CSI minutes to keep on a vitals/presence problem; `0` = off |
| `BACKUP_MAX_AGE_H` | `48` | hours since the last successful offsite backup before Backup is raised (1–720) |
| `AUTOCAL_SOURCE_NODE` | — | node to take the automatic calibration from, one of `EXPECTED_NODES`; unset = off. Needs `HA_URL` / `HA_TOKEN` / `HA_PERSONS` |
| `AUTOCAL_AWAY_MIN` | `10` | minutes every phone must be away before a capture (1–240) |
| `AUTOCAL_REFRESH_H` | `6` | replace a fresh calibration this old (1–12; `12` = only once it is stale) |
| `AUTOCAL_MAX_MOVING` | `0.25` | largest share of `present_moving` room states before and during a capture |

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
