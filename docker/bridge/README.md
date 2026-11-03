# RuView -> Home Assistant MQTT Bridge

Subscribes to `sensing-server`'s WebSocket (`/ws/sensing`), translates each
`SensingUpdate` into MQTT topics, and publishes Home Assistant MQTT
**Discovery** configs so HA auto-creates entities (presence, fall, vitals,
person count, per-person slots, ...) without any YAML edits.

## Environment variables

| Variable               | Default                                 | Notes                                              |
|------------------------|-----------------------------------------|----------------------------------------------------|
| `RUVIEW_WS`            | `ws://sensing-server:3001/ws/sensing`   | Sensing server WebSocket URL                       |
| `MQTT_HOST`            | `homeassistant.local`                   | MQTT broker host (Mosquitto add-on, etc.)          |
| `MQTT_PORT`            | `1883`                                  |                                                    |
| `MQTT_USER` / `MQTT_PASS` | (empty)                              | Set when the broker requires auth                  |
| `MQTT_PREFIX`          | `ruview`                                | Topic root (`<prefix>/state`, `<prefix>/event`, ...) |
| `HA_DISCOVERY_PREFIX`  | `homeassistant`                         | Match HA's MQTT integration setting                |
| `DEVICE_NAME`          | `RuView Sensing`                        | Friendly name in HA                                |
| `DEVICE_ID`            | `ruview`                                | Unique device id (also used in entity unique_ids)  |
| `LOG_LEVEL`            | `INFO`                                  | `DEBUG` for full WS frame tracing                  |
| `MAX_TRACKED_PERSONS`  | `2`                                     | Number of fixed per-person slots in HA Discovery   |
| `Z_MIN`                | `-100.0`                                | Drop persons whose median keypoint z < Z_MIN (m)   |
| `Z_MAX`                | `100.0`                                 | Drop persons whose median keypoint z > Z_MAX (m). Defaults effectively disable gating until sensing-server emits real floor-relative z. |
| `STATE_MAX_HZ`         | `10`                                    | Max rate of `<prefix>/state` (one message per new server tick at most); `0` = no limit |
| `NODE_MAX_HZ`          | `1`                                     | Max rate of each `<prefix>/nodes/<id>`             |
| `PERSON_MAX_HZ`        | `0.2`                                   | Max rate of each person topic, whatever changed (a person appearing or leaving included); a change inside the interval goes out at its end if it still holds |
| `PERSON_HEARTBEAT_S`   | `30`                                    | Re-send an unchanged person payload this often     |
| `STATUS_INTERVAL_S`    | `10`                                    | Period of the retained `<prefix>/bridge` status    |
| `EDGE_VITALS_TTL_S`    | `60`                                    | How long an `edge_vitals` message describes the vitals gate |
| `HA_PERSON_DETAIL`     | `false`                                 | Announce per-person Confidence and X/Y/Z entities; off retracts them (the values stay on the person topics) |
| `HA_NODE_RSSI`         | `false`                                 | Announce a `Node <id> RSSI` entity per node; off retracts it (the sensing-server's built-in publisher has one) |

## Topics published

| Topic                              | Retained | Payload                                         |
|------------------------------------|----------|-------------------------------------------------|
| `<prefix>/availability`            | yes      | `online` / `offline` (LWT)                       |
| `<prefix>/state`                   | no       | Flat JSON consumed by all entity templates; one message per new server tick, at most `STATE_MAX_HZ` |
| `<prefix>/nodes/<id>`              | no       | Per-node health: RSSI, `csi_fps`, `stale`, `last_seen_ms`, position |
| `<prefix>/event`                   | no       | One-shot fall events (HA `event` entity)         |
| `<prefix>/person/<slot>`           | no       | Per-slot person state (occupied, track id, x/y/z, conf, zone), sent on change at most `PERSON_MAX_HZ`, plus a heartbeat |
| `<prefix>/person/<slot>/avail`     | yes      | `online` when slot occupied, `offline` otherwise; sent with the state when it flips |
| `<prefix>/person/<label>`          | no       | Per-name state (e.g. `ruview/person/alice`) when an enrolled profile matched |
| `<prefix>/person/<label>/avail`    | yes      | `online` when that named person is detected      |
| `<prefix>/bridge`                  | yes      | Bridge stream health: WS connected, source, age of the last new tick, message rates |
| `homeassistant/<comp>/<id>/.../config` | yes  | HA Discovery configs                             |

The sensing-server re-broadcasts its latest update at 10 Hz, also after the
ESP32s stop sending (the source then reads `esp32:offline`). Repeats of a
tick are dropped, so `<prefix>/state` goes quiet when the CSI source stalls
while `<prefix>/bridge` keeps reporting `new_ticks_hz: 0`.

Heart and breathing rate are `null` whenever the server withholds them
(no fresh calibration, not exactly one person, quality gate); Home Assistant
then shows the sensors as unknown, and Telegraf stores no point, instead of
a fake 0 bpm. `abstention_reason` says why, when the server reports it.

## Load on Home Assistant

Home Assistant's recorder writes a database row for every new state, and its
MQTT integration renders every template of every message. Person tracks from
the server flicker and change their id often, which used to send each slot
topic, and its availability, up to 10 times a second, the likely cause of
Home Assistant's disk filling up in June 2026. So:

- every change of a person topic waits for `PERSON_MAX_HZ` (5 s by default);
  a change that is undone within the interval is never sent;
- the availability of a slot or name follows its published state, so it
  cannot change faster than the state;
- only Present and Zone are HA entities by default; Confidence and X/Y/Z
  (positions of the server's synthesised pose, not a located person) need
  `HA_PERSON_DETAIL=true`, and `Node <id> RSSI` needs `HA_NODE_RSSI=true`;
- positions are rounded to 0.01 and confidence to 0.001 before the change
  check, so noise below what HA shows is not a change;
- every person template renders `None` (unknown) when a field is missing,
  as in the `{"occupied": false}` payload of an empty slot, instead of
  logging a template warning.

The compose file also slows the sensing-server's built-in publisher (motion,
presence score, fall risk and person count to every 5 s per node), see
`RUVIEW_MQTT_RATE_MOTION` and `RUVIEW_MQTT_RATE_COUNT` in `docker/.env.example`.

## Enrolled profiles (named entities)

When `SENSING_PROFILES_DIR` is set on the sensing-server, the bridge automatically promotes single-person ticks from `person_1` / `person_2` to real names — `alice`, `bob`, etc.

A profile is a small JSON file matching the schema below. Today (step A), it can be hand-crafted; a CLI helper is planned (step B). Drop the file under `data/profiles/<name>.json` (the path mounted via docker-compose) and restart the sensing-server.

```json
{
  "name": "alice",
  "hr_baseline_bpm": 72.0,
  "hr_std_bpm": 3.0,
  "br_baseline_bpm": 14.5,
  "br_std_bpm": 1.2,
  "sample_count": 60,
  "enrolled_at": "2026-05-17T14:32:00Z",
  "last_updated_at": "2026-05-17T14:32:00Z"
}
```

**Enrollment workflow (manual today)**:

1. Have the person alone in the apartment for ~3 minutes.
2. Watch the sensing-server's `vital_signs` output (HA `sensor.heart_rate` / `sensor.breathing_rate`, or `mosquitto_sub -t 'ruview/state' -v`).
3. Note the typical resting values and roughly their spread over 60 s.
4. Write a JSON file like the one above, save as `data/profiles/<name>.json`.
5. Restart sensing-server: `docker compose restart sensing-server`.
6. The bridge will register "<Name> Present" and "<Name> Zone" (plus Confidence and X/Y with `HA_PERSON_DETAIL=true`) the first time that person is matched.

**Matching today** runs only when **exactly one person** is in the sensing zone, because the upstream vitals are still a single global reading. Multi-person matching arrives with step B (ADR-100).

## Run standalone

```bash
docker run --rm \
  -e MQTT_HOST=192.168.1.10 \
  -e MQTT_USER=ha -e MQTT_PASS=secret \
  -e RUVIEW_WS=ws://192.168.1.20:3001/ws/sensing \
  ruvnet/ruview-mqtt-bridge:latest
```

Inside the project's `docker-compose.yml`, the bridge is wired to the
internal network and the WS URL defaults to `sensing-server:3001`.

## Tests

```bash
cd docker/bridge
python -m pip install -r requirements.txt pytest jinja2
python -m pytest -q
```
