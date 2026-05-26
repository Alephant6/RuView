# RuView -> Home Assistant MQTT Bridge

Subscribes to `sensing-server`'s WebSocket (`/ws/sensing`), translates each
`SensingUpdate` into MQTT topics, and publishes Home Assistant MQTT
**Discovery** configs so HA auto-creates entities (presence, fall, vitals,
person count, per-node RSSI, ...) without any YAML edits.

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

## Topics published

| Topic                              | Retained | Payload                                         |
|------------------------------------|----------|-------------------------------------------------|
| `<prefix>/availability`            | yes      | `online` / `offline` (LWT)                       |
| `<prefix>/state`                   | no       | Flat JSON consumed by all entity templates       |
| `<prefix>/nodes/<id>`              | no       | Per-node RSSI / position                         |
| `<prefix>/event`                   | no       | One-shot fall events (HA `event` entity)         |
| `<prefix>/person/<slot>`           | no       | Per-slot person state (occupied, x/y/z, conf)    |
| `<prefix>/person/<slot>/avail`     | yes      | `online` when slot occupied, `offline` otherwise |
| `<prefix>/person/<label>`          | no       | Per-name state (e.g. `ruview/person/alice`) when an enrolled profile matched |
| `<prefix>/person/<label>/avail`    | yes      | `online` when that named person is detected      |
| `homeassistant/<comp>/<id>/.../config` | yes  | HA Discovery configs                             |

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
6. The bridge will register `sensor.<name>_present`, `sensor.<name>_x` etc the first time that person is matched.

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
