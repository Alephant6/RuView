# NAS message bus (Mosquitto, WP1.1)

Every RuView service on the NAS publishes and subscribes to this broker
instead of Home Assistant's. A bridge passes on only the topics Home Assistant
uses, so:

- ingest (Telegraf, the watchdog, the csi-recorder) keeps running while Home
  Assistant restarts or upgrades;
- Home Assistant's broker no longer carries `ruview/state` (up to 10 messages
  a second) or the health topics that only the NAS reads.

`mosquitto.conf` is the broker. `start.sh` adds the bridge to Home Assistant's
broker from `MQTT_HOST`, `MQTT_PORT`, `MQTT_USER` and `MQTT_PASS` in
`docker/.env`, the login the services used before the bus, after checking that
the values cannot change the generated config. An empty `MQTT_HOST` runs the
bus without the bridge.

Both files are baked into the `ruview-mosquitto` image (`Dockerfile`, built
by `fork-images.yml`), so the broker reads nothing from the NAS checkout and a
change reaches the NAS with the next `IMAGE_TAG`.

## What crosses the bridge

| Direction | Topics |
|---|---|
| NAS → Home Assistant | `homeassistant/#` (entity configs, the sensing-server's states and availability), `ruview/availability`, `ruview/person/#`, `ruview/nodes/#` (QoS 0), `ruview/event`, `ruview/watchdog/state`, `ruview/watchdog/availability`, `ruview/recorder/keep/result` |
| Home Assistant → NAS | `ruview/annotation` and `ruview/recorder/keep` (the label buttons) |
| Stays on the NAS | everything else, notably `ruview/state`, `ruview/bridge`, `ruview/watchdog/metrics`, `ruview/recorder/status` |

Retained messages are re-sent each time the bridge connects, so the entity
configs come back even if Home Assistant's broker lost them.

To bridge another topic, add a `topic` line to `start.sh` and the topic to the
matching list in `tests/check_bridge.py`; CI checks both.

## Link state: "NAS Link"

The bridge keeps `ruview/bus/ha_connected` retained on both brokers: `1` while
connected, `0` otherwise. Home Assistant's broker writes the `0` itself when
the connection drops, whether the NAS, the bus or the network failed. The
watchdog shows it in Home Assistant as `binary_sensor.ruview_watchdog_nas_link`,
and `analytics/ha/watchdog-notify.yaml` pushes a notification after 5 minutes
down. While the link is down, RuView entities in Home Assistant keep their last
values instead of going unavailable.

## Access

Clients connect without a login. The port is published on the NAS loopback
only (`127.0.0.1:${MQTT_BUS_PORT:-1883}`, used by the host-network
sensing-server and csi-recorder), so other machines on the LAN cannot reach
it. Inside Docker, only the containers on the `ruview-bus` network
(mqtt-bridge, analytics, telegraf) reach it, as `mosquitto:1883`. The
generated config, which holds the bridge password, is in the container's
`/tmp` and readable by the broker's user only.

## Checks

On the NAS, from `docker/`:

```bash
sudo docker compose ps mosquitto        # healthy
sudo docker compose logs mosquitto | tail -n 20
sudo docker compose exec mosquitto \
  mosquitto_sub -t ruview/bus/ha_connected -C 1 -W 5    # prints 1
```

The log shows `Connecting bridge ha (<host>:<port>)` at each attempt.
`Connection Refused: not authorised` after it means `MQTT_USER` / `MQTT_PASS`
do not match a login on Home Assistant's broker.

`tests/check_bridge.py` probes the bridge end to end against a broker standing
in for Home Assistant's; CI runs it in `.github/workflows/mqtt-bus.yml`. It
overwrites retained messages of real topic names, so use it on test brokers
only.
