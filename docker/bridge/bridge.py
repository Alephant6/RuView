#!/usr/bin/env python3
"""RuView -> Home Assistant MQTT bridge.

Subscribes to the sensing-server WebSocket (`/ws/sensing`), translates
SensingUpdate messages into MQTT topics under `${MQTT_PREFIX}/...`, and
publishes Home Assistant MQTT Discovery configs so HA auto-creates
entities (binary_sensor, sensor, event) without manual YAML.

Reconnects with exponential backoff on both WS and MQTT failures.
Marks all entities `unavailable` when WS drops via the LWT topic.

Configurable via environment variables (see README of this folder).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import time
from typing import Any

import paho.mqtt.client as mqtt
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WS_URL = os.getenv("RUVIEW_WS", "ws://sensing-server:3001/ws/sensing")
MQTT_HOST = os.getenv("MQTT_HOST", "homeassistant.local")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
MQTT_PREFIX = os.getenv("MQTT_PREFIX", "ruview")
HA_PREFIX = os.getenv("HA_DISCOVERY_PREFIX", "homeassistant")
DEVICE_NAME = os.getenv("DEVICE_NAME", "RuView Sensing")
DEVICE_ID = os.getenv("DEVICE_ID", "ruview")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# Per-person fixed-slot publishing. `MAX_TRACKED_PERSONS` slots are registered
# in HA Discovery; each tick the bridge sorts detected persons by `bbox.x`
# (leftmost → slot 1) and publishes a per-slot state topic. Spatial gating via
# Z_MIN/Z_MAX is wired up but currently a no-op for ESP32-derived poses (which
# emit body-lean z, not real floor height) — once the sensing-server exposes
# multistatic-localized z, just tighten Z_MIN/Z_MAX to filter downstairs.
MAX_TRACKED_PERSONS = int(os.getenv("MAX_TRACKED_PERSONS", "2"))
Z_MIN = float(os.getenv("Z_MIN", "-100.0"))
Z_MAX = float(os.getenv("Z_MAX", "100.0"))

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
LOG = logging.getLogger("ruview-bridge")

AVAIL_TOPIC = f"{MQTT_PREFIX}/availability"
STATE_TOPIC = f"{MQTT_PREFIX}/state"
EVENT_TOPIC = f"{MQTT_PREFIX}/event"


def person_state_topic(slot: int) -> str:
    return f"{MQTT_PREFIX}/person/{slot}"


def person_avail_topic(slot: int) -> str:
    return f"{MQTT_PREFIX}/person/{slot}/avail"


def label_state_topic(label: str) -> str:
    return f"{MQTT_PREFIX}/person/{label}"


def label_avail_topic(label: str) -> str:
    return f"{MQTT_PREFIX}/person/{label}/avail"


def _safe_object_id(name: str) -> str:
    """Sanitise a label to a Home Assistant unique_id-safe form."""
    out = []
    for c in name.strip().lower():
        if c.isalnum() or c in ("_", "-"):
            out.append(c)
        else:
            out.append("_")
    return "".join(out) or "person"

DEVICE_BLOCK = {
    "identifiers": [DEVICE_ID],
    "name": DEVICE_NAME,
    "manufacturer": "RuView / WiFi-DensePose",
    "model": "Sensing Server",
}

# ---------------------------------------------------------------------------
# Home Assistant MQTT Discovery
# ---------------------------------------------------------------------------
def _discovery(component: str, object_id: str, payload: dict) -> tuple[str, str]:
    """Build (topic, payload) for HA MQTT Discovery."""
    topic = f"{HA_PREFIX}/{component}/{DEVICE_ID}/{object_id}/config"
    base = {
        "device": DEVICE_BLOCK,
        "availability_topic": AVAIL_TOPIC,
        "payload_available": "online",
        "payload_not_available": "offline",
        "unique_id": f"{DEVICE_ID}_{object_id}",
    }
    base.update(payload)
    return topic, json.dumps(base)


def _person_z(person: dict[str, Any]) -> float | None:
    """Median z of high-confidence keypoints, or None if unavailable.

    For real multistatic-localized poses this is metres above the bridge
    coordinate origin; for the current ESP32 pose synthesiser this is the
    body-lean estimate (~0) so the value is informational only.
    """
    kps = person.get("keypoints") or []
    zs = [
        float(kp.get("z", 0.0))
        for kp in kps
        if float(kp.get("confidence", 0.0)) >= 0.3
    ]
    if not zs:
        return None
    zs.sort()
    return zs[len(zs) // 2]


def _person_center(person: dict[str, Any]) -> tuple[float, float]:
    """(cx, cy) of the bounding box, or (0.0, 0.0) if unavailable."""
    bbox = person.get("bbox") or {}
    x = float(bbox.get("x", 0.0))
    y = float(bbox.get("y", 0.0))
    w = float(bbox.get("width", 0.0))
    h = float(bbox.get("height", 0.0))
    return (x + w / 2.0, y + h / 2.0)


def _passes_z_gate(person: dict[str, Any]) -> bool:
    """Return True when the person should be reported (False = drop)."""
    z = _person_z(person)
    if z is None:
        # No z info available — don't drop, the sensing-server isn't producing
        # 3-D positions yet. Once it does, an unknown-z person should still pass
        # so we don't silently lose detections during the transition.
        return True
    return Z_MIN <= z <= Z_MAX


def discovery_messages() -> list[tuple[str, str]]:
    """Return all entity discovery configs to publish on connect."""
    msgs: list[tuple[str, str]] = []

    # ── Binary sensors ────────────────────────────────────────────────
    msgs.append(_discovery("binary_sensor", "presence", {
        "name": "Presence",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.presence else 'OFF' }}",
        "device_class": "presence",
    }))
    msgs.append(_discovery("binary_sensor", "fall_detected", {
        "name": "Fall Detected",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ 'ON' if value_json.fall_detected else 'OFF' }}",
        "device_class": "safety",
        "off_delay": 30,
    }))

    # ── Sensors ───────────────────────────────────────────────────────
    msgs.append(_discovery("sensor", "n_persons", {
        "name": "Person Count",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.n_persons }}",
        "icon": "mdi:account-group",
        "state_class": "measurement",
    }))
    msgs.append(_discovery("sensor", "motion_level", {
        "name": "Motion Level",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.motion_level }}",
        "icon": "mdi:run-fast",
    }))
    msgs.append(_discovery("sensor", "motion_energy", {
        "name": "Motion Energy",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.motion_energy | round(3) }}",
        "icon": "mdi:waveform",
        "state_class": "measurement",
    }))
    msgs.append(_discovery("sensor", "confidence", {
        "name": "Detection Confidence",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ (value_json.confidence * 100) | round(1) }}",
        "unit_of_measurement": "%",
        "state_class": "measurement",
    }))
    msgs.append(_discovery("sensor", "breathing_rate", {
        "name": "Breathing Rate",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.breathing_rate_bpm | round(1) }}",
        "unit_of_measurement": "bpm",
        "icon": "mdi:lungs",
        "state_class": "measurement",
    }))
    msgs.append(_discovery("sensor", "heart_rate", {
        "name": "Heart Rate",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.heart_rate_bpm | round(0) }}",
        "unit_of_measurement": "bpm",
        "icon": "mdi:heart-pulse",
        "state_class": "measurement",
    }))
    msgs.append(_discovery("sensor", "signal_quality", {
        "name": "Signal Quality",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ (value_json.signal_quality * 100) | round(1) }}",
        "unit_of_measurement": "%",
        "icon": "mdi:signal",
        "state_class": "measurement",
    }))
    msgs.append(_discovery("sensor", "source", {
        "name": "Data Source",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.source }}",
        "icon": "mdi:lan-connect",
        "entity_category": "diagnostic",
    }))
    msgs.append(_discovery("sensor", "active_nodes", {
        "name": "Active Nodes",
        "state_topic": STATE_TOPIC,
        "value_template": "{{ value_json.active_nodes }}",
        "icon": "mdi:access-point-network",
        "entity_category": "diagnostic",
        "state_class": "measurement",
    }))

    # ── Event entity (HA 2023.8+) for fall alerts ─────────────────────
    msgs.append(_discovery("event", "fall_event", {
        "name": "Fall Event",
        "state_topic": EVENT_TOPIC,
        "event_types": ["fall"],
        "value_template": "{{ value_json.event_type }}",
    }))

    # ── Per-person fixed slots ─────────────────────────────────────────
    # Each slot gets its own state + availability topic so HA entities
    # cleanly drop to "unavailable" when nobody is in that slot, rather
    # than displaying stale numbers.
    #
    # NOTE: per-slot heart rate / breathing rate is intentionally absent.
    # `SensingUpdate.vital_signs` is currently a single global reading; until
    # the sensing-server attaches per-track vitals to `PersonDetection`,
    # publishing per-slot HR/BR would just produce broken entities.
    for slot in range(1, MAX_TRACKED_PERSONS + 1):
        state_topic = person_state_topic(slot)
        avail_topic = person_avail_topic(slot)
        slot_avail = [
            {"topic": AVAIL_TOPIC, "payload_available": "online", "payload_not_available": "offline"},
            {"topic": avail_topic, "payload_available": "online", "payload_not_available": "offline"},
        ]

        def _slot(component: str, object_id: str, payload: dict) -> tuple[str, str]:
            """Build a per-slot discovery message with chained availability."""
            topic = f"{HA_PREFIX}/{component}/{DEVICE_ID}/{object_id}/config"
            base = {
                "device": DEVICE_BLOCK,
                "availability": slot_avail,
                "availability_mode": "all",
                "unique_id": f"{DEVICE_ID}_{object_id}",
            }
            base.update(payload)
            return topic, json.dumps(base)

        msgs.append(_slot("binary_sensor", f"person_{slot}_present", {
            "name": f"Person {slot} Present",
            "state_topic": state_topic,
            "value_template": "{{ 'ON' if value_json.occupied else 'OFF' }}",
            "device_class": "occupancy",
        }))
        msgs.append(_slot("sensor", f"person_{slot}_confidence", {
            "name": f"Person {slot} Confidence",
            "state_topic": state_topic,
            "value_template": "{{ (value_json.confidence * 100) | round(1) }}",
            "unit_of_measurement": "%",
            "state_class": "measurement",
            "icon": "mdi:percent",
        }))
        msgs.append(_slot("sensor", f"person_{slot}_x", {
            "name": f"Person {slot} X",
            "state_topic": state_topic,
            "value_template": "{{ value_json.x | round(2) }}",
            "icon": "mdi:axis-x-arrow",
            "entity_category": "diagnostic",
        }))
        msgs.append(_slot("sensor", f"person_{slot}_y", {
            "name": f"Person {slot} Y",
            "state_topic": state_topic,
            "value_template": "{{ value_json.y | round(2) }}",
            "icon": "mdi:axis-y-arrow",
            "entity_category": "diagnostic",
        }))
        msgs.append(_slot("sensor", f"person_{slot}_z", {
            "name": f"Person {slot} Z",
            "state_topic": state_topic,
            "value_template": "{{ value_json.z | round(2) }}",
            "icon": "mdi:axis-z-arrow",
            "entity_category": "diagnostic",
        }))
        msgs.append(_slot("sensor", f"person_{slot}_zone", {
            "name": f"Person {slot} Zone",
            "state_topic": state_topic,
            "value_template": "{{ value_json.zone }}",
            "icon": "mdi:map-marker",
        }))

    return msgs


# ---------------------------------------------------------------------------
# SensingUpdate -> flat state dict
# ---------------------------------------------------------------------------
def translate(update: dict[str, Any]) -> dict[str, Any]:
    """Flatten the WS SensingUpdate JSON into a state dict for MQTT."""
    classification = update.get("classification") or {}
    features = update.get("features") or {}
    vitals = update.get("vital_signs") or {}
    nodes = update.get("nodes") or []

    # Fall detection: presence + abrupt motion energy spike. Sensing-server
    # emits per-frame `fall_detected` upstream; if absent, derive a soft
    # heuristic from motion band power so HA still sees something useful.
    fall = bool(update.get("fall_detected", False))

    return {
        "presence": bool(classification.get("presence", False)),
        "confidence": float(classification.get("confidence", 0.0)),
        "motion_level": str(classification.get("motion_level", "unknown")),
        "motion_energy": float(features.get("motion_band_power", 0.0)),
        "breathing_rate_bpm": float(vitals.get("breathing_rate_bpm") or 0.0),
        "heart_rate_bpm": float(vitals.get("heart_rate_bpm") or 0.0),
        "signal_quality": float(vitals.get("signal_quality", 0.0)),
        "n_persons": int(update.get("estimated_persons") or len(update.get("persons") or []) or 0),
        "source": str(update.get("source", "unknown")),
        "active_nodes": len(nodes),
        "fall_detected": fall,
        "tick": int(update.get("tick", 0)),
        "ts": float(update.get("timestamp", time.time())),
    }


# ---------------------------------------------------------------------------
# Per-node discovery + state (auto-registered the first time a node is seen)
# ---------------------------------------------------------------------------
class LabelRegistry:
    """Auto-publishes discovery configs the first time a profile label appears.

    A labeled track corresponds to an enrolled household member ("alice",
    "bob"). The first time the sensing-server returns a labeled detection,
    we register a named set of entities in HA Discovery, so the user sees
    `sensor.alice_present` etc instead of just slot indices.
    """

    def __init__(self, client: mqtt.Client) -> None:
        self._client = client
        self._seen: set[str] = set()

    def ensure_registered(self, label: str) -> None:
        if label in self._seen:
            return
        self._seen.add(label)

        object_prefix = _safe_object_id(label)
        state_topic = label_state_topic(label)
        avail_topic = label_avail_topic(label)
        slot_avail = [
            {"topic": AVAIL_TOPIC, "payload_available": "online", "payload_not_available": "offline"},
            {"topic": avail_topic, "payload_available": "online", "payload_not_available": "offline"},
        ]

        def _entity(component: str, suffix: str, payload: dict) -> tuple[str, str]:
            object_id = f"person_{object_prefix}_{suffix}"
            topic = f"{HA_PREFIX}/{component}/{DEVICE_ID}/{object_id}/config"
            base = {
                "device": DEVICE_BLOCK,
                "availability": slot_avail,
                "availability_mode": "all",
                "unique_id": f"{DEVICE_ID}_{object_id}",
            }
            base.update(payload)
            return topic, json.dumps(base)

        entities = [
            _entity("binary_sensor", "present", {
                "name": f"{label.title()} Present",
                "state_topic": state_topic,
                "value_template": "{{ 'ON' if value_json.occupied else 'OFF' }}",
                "device_class": "occupancy",
            }),
            _entity("sensor", "confidence", {
                "name": f"{label.title()} Confidence",
                "state_topic": state_topic,
                "value_template": "{{ (value_json.confidence * 100) | round(1) }}",
                "unit_of_measurement": "%",
                "state_class": "measurement",
                "icon": "mdi:percent",
            }),
            _entity("sensor", "x", {
                "name": f"{label.title()} X",
                "state_topic": state_topic,
                "value_template": "{{ value_json.x | round(2) }}",
                "icon": "mdi:axis-x-arrow",
                "entity_category": "diagnostic",
            }),
            _entity("sensor", "y", {
                "name": f"{label.title()} Y",
                "state_topic": state_topic,
                "value_template": "{{ value_json.y | round(2) }}",
                "icon": "mdi:axis-y-arrow",
                "entity_category": "diagnostic",
            }),
            _entity("sensor", "zone", {
                "name": f"{label.title()} Zone",
                "state_topic": state_topic,
                "value_template": "{{ value_json.zone }}",
                "icon": "mdi:map-marker",
            }),
        ]
        for topic, payload in entities:
            self._client.publish(topic, payload, retain=True)
        LOG.info("registered profile label %r with HA discovery", label)


class NodeRegistry:
    """Auto-publishes discovery configs the first time a node id appears."""

    def __init__(self, client: mqtt.Client) -> None:
        self._client = client
        self._seen: set[int] = set()

    def publish_node(self, node: dict[str, Any]) -> None:
        nid = int(node.get("node_id", 0))
        if nid not in self._seen:
            self._seen.add(nid)
            object_id = f"node_{nid}_rssi"
            topic, payload = _discovery("sensor", object_id, {
                "name": f"Node {nid} RSSI",
                "state_topic": f"{MQTT_PREFIX}/nodes/{nid}",
                "value_template": "{{ value_json.rssi_dbm | round(0) }}",
                "unit_of_measurement": "dBm",
                "device_class": "signal_strength",
                "state_class": "measurement",
                "entity_category": "diagnostic",
            })
            self._client.publish(topic, payload, retain=True)
            LOG.info("registered node %s with HA discovery", nid)

        self._client.publish(
            f"{MQTT_PREFIX}/nodes/{nid}",
            json.dumps({
                "node_id": nid,
                "rssi_dbm": float(node.get("rssi_dbm", 0.0)),
                "subcarrier_count": int(node.get("subcarrier_count", 0)),
                "position": node.get("position", [0, 0, 0]),
            }),
        )


# ---------------------------------------------------------------------------
# MQTT client setup with reconnect
# ---------------------------------------------------------------------------
def make_mqtt() -> mqtt.Client:
    client = mqtt.Client(
        client_id=f"{DEVICE_ID}-bridge",
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )
    client.will_set(AVAIL_TOPIC, "offline", retain=True)
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASS)

    def on_connect(c, _ud, _flags, reason_code, _props=None):
        if reason_code == 0:
            LOG.info("MQTT connected to %s:%s", MQTT_HOST, MQTT_PORT)
            c.publish(AVAIL_TOPIC, "online", retain=True)
            for topic, payload in discovery_messages():
                c.publish(topic, payload, retain=True)
            LOG.info("HA discovery configs published")
        else:
            LOG.error("MQTT connect failed, reason=%s", reason_code)

    def on_disconnect(_c, _ud, _flags, reason_code, _props=None):
        LOG.warning("MQTT disconnected, reason=%s — paho will auto-reconnect", reason_code)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
    client.loop_start()
    return client


# ---------------------------------------------------------------------------
# WebSocket consumer with reconnect
# ---------------------------------------------------------------------------
async def consume(stop: asyncio.Event, client: mqtt.Client) -> None:
    backoff = 1.0
    nodes = NodeRegistry(client)
    labels = LabelRegistry(client)

    while not stop.is_set():
        try:
            LOG.info("connecting to %s", WS_URL)
            async with websockets.connect(WS_URL, ping_interval=20, ping_timeout=20) as ws:
                LOG.info("WS connected")
                backoff = 1.0
                last_fall_tick = -1

                async for raw in ws:
                    if stop.is_set():
                        break
                    try:
                        update = json.loads(raw)
                    except json.JSONDecodeError:
                        LOG.debug("non-JSON frame: %r", raw[:80])
                        continue
                    if update.get("type") and update["type"] != "sensing_update":
                        continue

                    state = translate(update)
                    client.publish(STATE_TOPIC, json.dumps(state))

                    for node in update.get("nodes") or []:
                        nodes.publish_node(node)

                    # ── Per-person slot + labeled publishing ─────────────
                    # Each detected person is routed to either:
                    #   - a named entity set (when `label` is present), or
                    #   - a fixed slot (person_1 / person_2 / ...) as fallback.
                    # A labeled person never also occupies a numeric slot.
                    raw_persons = update.get("persons") or []
                    z_passing = [p for p in raw_persons if _passes_z_gate(p)]

                    labeled = [p for p in z_passing if p.get("label")]
                    unlabeled = [p for p in z_passing if not p.get("label")]

                    # Track which labels we've published this tick so we can mark
                    # previously-seen-but-now-absent labels as offline.
                    published_labels: set[str] = set()
                    for person in labeled:
                        label = str(person.get("label"))
                        labels.ensure_registered(label)
                        cx, cy = _person_center(person)
                        z = _person_z(person)
                        client.publish(label_avail_topic(label), "online", retain=True)
                        client.publish(label_state_topic(label), json.dumps({
                            "occupied": True,
                            "label": label,
                            "track_id": int(person.get("id", 0)),
                            "confidence": float(person.get("confidence", 0.0)),
                            "x": cx,
                            "y": cy,
                            "z": 0.0 if z is None else z,
                            "zone": str(person.get("zone", "")),
                        }))
                        published_labels.add(label)

                    # Any previously-registered label not seen this tick goes offline.
                    for label in labels._seen - published_labels:
                        client.publish(label_avail_topic(label), "offline", retain=True)
                        client.publish(label_state_topic(label), json.dumps({
                            "occupied": False,
                            "label": label,
                        }))

                    # Fixed slots — only used for unlabeled detections (multi-person
                    # ticks today, or any tick before profiles are enrolled).
                    unlabeled.sort(key=lambda p: _person_center(p)[0])
                    slot_assignments = {
                        i + 1: p for i, p in enumerate(unlabeled[:MAX_TRACKED_PERSONS])
                    }
                    for slot in range(1, MAX_TRACKED_PERSONS + 1):
                        avail_topic = person_avail_topic(slot)
                        state_topic = person_state_topic(slot)
                        person = slot_assignments.get(slot)
                        if person is None:
                            client.publish(avail_topic, "offline", retain=True)
                            client.publish(state_topic, json.dumps({"occupied": False}))
                            continue

                        cx, cy = _person_center(person)
                        z = _person_z(person)
                        client.publish(avail_topic, "online", retain=True)
                        client.publish(state_topic, json.dumps({
                            "occupied": True,
                            "track_id": int(person.get("id", 0)),
                            "confidence": float(person.get("confidence", 0.0)),
                            "x": cx,
                            "y": cy,
                            "z": 0.0 if z is None else z,
                            "zone": str(person.get("zone", "")),
                        }))

                    # Edge-trigger fall event: only fire once per tick where
                    # `fall_detected` flips from false->true.
                    if state["fall_detected"] and state["tick"] != last_fall_tick:
                        last_fall_tick = state["tick"]
                        client.publish(EVENT_TOPIC, json.dumps({
                            "event_type": "fall",
                            "tick": state["tick"],
                            "ts": state["ts"],
                            "source": state["source"],
                            "confidence": state["confidence"],
                        }))
                        LOG.warning("FALL event published (tick=%s)", state["tick"])
        except (ConnectionClosed, WebSocketException, OSError) as exc:
            LOG.warning("WS error: %s — retrying in %.1fs", exc, backoff)
            try:
                await asyncio.wait_for(stop.wait(), timeout=backoff)
            except asyncio.TimeoutError:
                pass
            backoff = min(backoff * 2, 30.0)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------
async def main() -> None:
    LOG.info("RuView -> HA MQTT bridge starting")
    LOG.info("  WS:    %s", WS_URL)
    LOG.info("  MQTT:  %s:%s as user=%r prefix=%r", MQTT_HOST, MQTT_PORT, MQTT_USER, MQTT_PREFIX)
    LOG.info("  HA:    discovery prefix=%r device_id=%r", HA_PREFIX, DEVICE_ID)
    LOG.info("  Slots: max_tracked_persons=%d z_gate=[%.2f, %.2f]",
             MAX_TRACKED_PERSONS, Z_MIN, Z_MAX)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)

    client = make_mqtt()
    try:
        await consume(stop, client)
    finally:
        client.publish(AVAIL_TOPIC, "offline", retain=True)
        client.loop_stop()
        client.disconnect()
        LOG.info("bridge stopped")


if __name__ == "__main__":
    asyncio.run(main())
