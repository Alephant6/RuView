#!/usr/bin/env python3
"""RuView -> Home Assistant MQTT bridge.

Subscribes to the sensing-server WebSocket (`/ws/sensing`), translates
SensingUpdate messages into MQTT topics under `${MQTT_PREFIX}/...`, and
publishes Home Assistant MQTT Discovery configs so HA auto-creates
entities (binary_sensor, sensor, event) without manual YAML.

Reconnects with exponential backoff on both WS and MQTT failures.
Marks all entities `unavailable` when WS drops via the LWT topic.

The sensing-server re-broadcasts its latest update at 10 Hz even after the
hardware goes quiet (with `source` turned into `<source>:offline`), and
recent builds emit one update per CSI frame. The bridge therefore:

- publishes `${MQTT_PREFIX}/state` only for new ticks, at most STATE_MAX_HZ;
- writes missing heart/breathing rates as JSON null instead of 0.0, and
  adds quality fields (vitals_present, stale_source, abstention_reason, ...);
- publishes per-node health at most NODE_MAX_HZ and person slots only when
  they change (plus a heartbeat), and availability topics only on transitions;
- reports its own view of the stream on `${MQTT_PREFIX}/bridge` (retained)
  every STATUS_INTERVAL_S, so a stalled source is visible even though the
  state topic goes quiet.

Configurable via environment variables (see README of this folder).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import signal
import threading
import time
from typing import Any

import paho.mqtt.client as mqtt
import websockets
from websockets.exceptions import ConnectionClosed, WebSocketException

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
WS_URL = os.getenv("RUVIEW_WS", "ws://sensing-server:3001/ws/sensing")
# Upstream sensing-server gates /ws/sensing behind the same bearer token as
# /api/v1/* when RUVIEW_API_TOKEN is set (ADR-272). Empty = no header.
WS_TOKEN = os.getenv("RUVIEW_API_TOKEN", "")
MQTT_HOST = os.getenv("MQTT_HOST", "homeassistant.local")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))
MQTT_USER = os.getenv("MQTT_USER", "")
MQTT_PASS = os.getenv("MQTT_PASS", "")
MQTT_PREFIX = os.getenv("MQTT_PREFIX", "ruview")
HA_PREFIX = os.getenv("HA_DISCOVERY_PREFIX", "homeassistant")
DEVICE_NAME = os.getenv("DEVICE_NAME", "RuView Sensing")
DEVICE_ID = os.getenv("DEVICE_ID", "ruview")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
# Room-level HA entities (presence, HR/BR, person count, motion, fall, ...).
# Set ROOM_ENTITIES=false when the sensing-server's built-in MQTT publisher
# (`--mqtt`, ADR-115) owns those entities: the bridge then retracts its own
# retained room-level discovery configs and only announces the per-person
# slot / enrolled-label entities, which upstream does not publish. The
# ruview/* state topics keep flowing either way for Telegraf.
ROOM_ENTITIES = os.getenv("ROOM_ENTITIES", "true").strip().lower() in ("1", "true", "yes", "on")

# Per-person fixed-slot publishing. `MAX_TRACKED_PERSONS` slots are registered
# in HA Discovery; each tick the bridge sorts detected persons by `bbox.x`
# (leftmost → slot 1) and publishes a per-slot state topic. Spatial gating via
# Z_MIN/Z_MAX is wired up but currently a no-op for ESP32-derived poses (which
# emit body-lean z, not real floor height) — once the sensing-server exposes
# multistatic-localized z, just tighten Z_MIN/Z_MAX to filter downstairs.
MAX_TRACKED_PERSONS = int(os.getenv("MAX_TRACKED_PERSONS", "2"))
Z_MIN = float(os.getenv("Z_MIN", "-100.0"))
Z_MAX = float(os.getenv("Z_MAX", "100.0"))

# Publish-rate limits. 0 disables the limit for that topic family.
STATE_MAX_HZ = float(os.getenv("STATE_MAX_HZ", "10"))
NODE_MAX_HZ = float(os.getenv("NODE_MAX_HZ", "1"))
PERSON_MAX_HZ = float(os.getenv("PERSON_MAX_HZ", "1"))
# Unchanged person payloads are re-sent this often so consumers can tell
# "nothing changed" from "bridge gone".
PERSON_HEARTBEAT_S = float(os.getenv("PERSON_HEARTBEAT_S", "30"))
STATUS_INTERVAL_S = float(os.getenv("STATUS_INTERVAL_S", "10"))
# How long an `edge_vitals` message still describes the current vitals gate.
EDGE_VITALS_TTL_S = float(os.getenv("EDGE_VITALS_TTL_S", "60"))

logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
)
LOG = logging.getLogger("ruview-bridge")

AVAIL_TOPIC = f"{MQTT_PREFIX}/availability"
STATE_TOPIC = f"{MQTT_PREFIX}/state"
EVENT_TOPIC = f"{MQTT_PREFIX}/event"
BRIDGE_STATUS_TOPIC = f"{MQTT_PREFIX}/bridge"


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
def _optional_number(field: str, expr: str, prefix: str = "") -> str:
    """Value template that renders `None` (HA: unknown) when `field` is null.

    `expr` is appended to `value_json.<field>`; `prefix` goes before it, so
    `("signal_quality", "* 100) | round(1)", "(")` renders
    `(value_json.signal_quality * 100) | round(1)`.
    """
    ref = f"value_json.{field}"
    return f"{{{{ {prefix}{ref} {expr} if {ref} is number else None }}}}"


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
    """Return all entity discovery configs to publish on connect.

    With ROOM_ENTITIES off, room-level configs are replaced by empty retained
    payloads, which makes Home Assistant remove those entities.
    """
    room = room_discovery_messages()
    if not ROOM_ENTITIES:
        room = [(topic, "") for topic, _ in room]
    return room + slot_discovery_messages()


def room_discovery_messages() -> list[tuple[str, str]]:
    """Room-level entities fed from ruview/state and ruview/event."""
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
    # HR / BR / signal quality are null while the server withholds vitals
    # (calibration, occupancy or quality gate). Rendering "None" makes HA
    # show the sensor as unknown instead of a fake 0 bpm.
    msgs.append(_discovery("sensor", "breathing_rate", {
        "name": "Breathing Rate",
        "state_topic": STATE_TOPIC,
        "value_template": _optional_number("breathing_rate_bpm", "| round(1)"),
        "unit_of_measurement": "bpm",
        "icon": "mdi:lungs",
        "state_class": "measurement",
    }))
    msgs.append(_discovery("sensor", "heart_rate", {
        "name": "Heart Rate",
        "state_topic": STATE_TOPIC,
        "value_template": _optional_number("heart_rate_bpm", "| round(0)"),
        "unit_of_measurement": "bpm",
        "icon": "mdi:heart-pulse",
        "state_class": "measurement",
    }))
    msgs.append(_discovery("sensor", "signal_quality", {
        "name": "Signal Quality",
        "state_topic": STATE_TOPIC,
        "value_template": _optional_number("signal_quality", "* 100) | round(1)", prefix="("),
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

    return msgs


def slot_discovery_messages() -> list[tuple[str, str]]:
    """Per-person slot entities (person_1 .. person_N)."""
    msgs: list[tuple[str, str]] = []

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
def _num(value: Any) -> float | None:
    """Finite float, or None for null / missing / non-numeric / NaN."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _rate(value: Any) -> float | None:
    """A physiological rate; the server never reports a real rate <= 0."""
    out = _num(value)
    return out if out is not None and out > 0 else None


def is_stale_source(source: str) -> bool:
    """`effective_source()` appends `:offline` once frames stop arriving."""
    return source.endswith(":offline")


# Fields describing the server's vitals gate, taken from `edge_vitals`
# messages. None when no recent message carried them.
EDGE_FIELDS_UNKNOWN: dict[str, Any] = {
    "abstention_reason": None,
    "numeric_vitals_authorized": None,
    "calibration_fresh": None,
}


def translate(update: dict[str, Any], edge: dict[str, Any] | None = None) -> dict[str, Any]:
    """Flatten the WS SensingUpdate JSON into a state dict for MQTT.

    Missing values are None (JSON null), never 0.0: Telegraf skips null
    fields, so a withheld heart rate leaves a gap instead of a fake reading.
    """
    classification = update.get("classification") or {}
    features = update.get("features") or {}
    vitals = update.get("vital_signs") or {}
    nodes = update.get("nodes") or []
    node_features = update.get("node_features") or []
    room = update.get("room_inference") or {}
    evidence = update.get("calibrated_presence_evidence") or {}
    source = str(update.get("source", "unknown"))

    # Fall detection: presence + abrupt motion energy spike. Sensing-server
    # emits per-frame `fall_detected` upstream; if absent, derive a soft
    # heuristic from motion band power so HA still sees something useful.
    fall = bool(update.get("fall_detected", False))

    hr = _rate(vitals.get("heart_rate_bpm"))
    br = _rate(vitals.get("breathing_rate_bpm"))
    if node_features:
        fresh_nodes = sum(1 for n in node_features if not n.get("stale", False))
    else:
        fresh_nodes = len(nodes)
    cal_count = evidence.get("person_count")

    state = {
        "presence": bool(classification.get("presence", False)),
        "confidence": float(classification.get("confidence", 0.0)),
        "motion_level": str(classification.get("motion_level", "unknown")),
        "motion_energy": float(features.get("motion_band_power", 0.0)),
        "breathing_rate_bpm": br,
        "heart_rate_bpm": hr,
        "signal_quality": _num(vitals.get("signal_quality")) if vitals else None,
        "hr_conf": _num(vitals.get("heartbeat_confidence")) if hr is not None else None,
        "br_conf": _num(vitals.get("breathing_confidence")) if br is not None else None,
        "vitals_present": hr is not None or br is not None,
        "n_persons": int(update.get("estimated_persons") or len(update.get("persons") or []) or 0),
        # Occupancy from the calibrated empty-room model, when one is bound.
        "calibrated": bool(evidence),
        "cal_person_count": int(cal_count) if isinstance(cal_count, int) else None,
        "room_class": room.get("classification"),
        "room_conf": _num(room.get("confidence")),
        "source": source,
        "stale_source": is_stale_source(source),
        "active_nodes": len(nodes),
        "fresh_nodes": fresh_nodes,
        "fall_detected": fall,
        "tick": int(update.get("tick", 0)),
        "ts": float(update.get("timestamp", time.time())),
    }
    state.update(edge if edge is not None else EDGE_FIELDS_UNKNOWN)
    return state


class EdgeVitalsCache:
    """Latest `edge_vitals` gate fields per node, with a freshness window.

    The server sends `edge_vitals` alongside sensing updates; since 2026-10
    upstream it says whether numeric vitals were authorized and, if not, why
    (`bootstrap_empty_background`, `explicit_calibration_required`,
    `exactly_one_occupant_required`, `vital_quality_gate_failed`).
    """

    def __init__(self, ttl_s: float) -> None:
        self._ttl_s = ttl_s
        self._latest: dict[Any, tuple[float, dict[str, Any]]] = {}

    def observe(self, msg: dict[str, Any], now: float) -> None:
        # Older servers send edge_vitals without the gate fields.
        if "numeric_vitals_authorized" not in msg:
            return
        self._latest[msg.get("node_id")] = (now, msg)

    def summary(self, now: float) -> dict[str, Any]:
        fresh = [(t, m) for t, m in self._latest.values() if now - t <= self._ttl_s]
        if not fresh:
            return dict(EDGE_FIELDS_UNKNOWN)
        authorized = any(bool(m.get("numeric_vitals_authorized")) for _, m in fresh)
        _, newest = max(fresh, key=lambda item: item[0])
        return {
            "abstention_reason": None if authorized else newest.get("abstention_reason"),
            "numeric_vitals_authorized": authorized,
            "calibration_fresh": any(bool(m.get("calibrated_evidence_authorized")) for _, m in fresh),
        }


def _interval(max_hz: float) -> float:
    return 1.0 / max_hz if max_hz > 0 else 0.0


class RateGate:
    """At most one publish per key every `min_interval_s` seconds."""

    def __init__(self, min_interval_s: float) -> None:
        self._min_interval_s = min_interval_s
        self._last: dict[Any, float] = {}

    def allow(self, key: Any, now: float) -> bool:
        last = self._last.get(key)
        if last is not None and now - last < self._min_interval_s:
            return False
        self._last[key] = now
        return True

    def reset(self) -> None:
        self._last.clear()


class ChangeGate:
    """Publish a topic when its payload changes, rate-limited, plus a heartbeat.

    A change in `key` (occupied / track / zone) goes out immediately; other
    changes (positions, confidence) at most every `min_interval_s`; an
    unchanged payload only every `heartbeat_s`.
    """

    def __init__(self, min_interval_s: float, heartbeat_s: float) -> None:
        self._min_interval_s = min_interval_s
        self._heartbeat_s = heartbeat_s
        self._last: dict[str, tuple[float, str, Any]] = {}

    def allow(self, topic: str, payload: str, key: Any, now: float) -> bool:
        prev = self._last.get(topic)
        if prev is not None:
            last_t, last_payload, last_key = prev
            if key == last_key:
                wait = self._heartbeat_s if payload == last_payload else self._min_interval_s
                if now - last_t < wait:
                    return False
        self._last[topic] = (now, payload, key)
        return True

    def reset(self) -> None:
        self._last.clear()


class BridgeStats:
    """Counters behind the retained `${MQTT_PREFIX}/bridge` status topic."""

    COUNTERS = ("msgs_in", "new_ticks", "duplicates", "state_published",
                "state_throttled", "edge_vitals_in")

    def __init__(self) -> None:
        self.ws_connected = False
        self.source: str | None = None
        self.last_tick: int | None = None
        self.last_new_tick_at: float | None = None  # monotonic
        self.last_update_ts: float | None = None    # server wall clock
        self.counts = dict.fromkeys(self.COUNTERS, 0)
        self._prev_counts = dict(self.counts)
        self._prev_at: float | None = None
        self.source_flipped = False

    def bump(self, name: str) -> None:
        self.counts[name] += 1

    def observe_source(self, source: str) -> None:
        if self.source is not None and is_stale_source(source) != is_stale_source(self.source):
            self.source_flipped = True
        self.source = source

    def snapshot(self, wall: float, mono: float) -> dict[str, Any]:
        elapsed = mono - self._prev_at if self._prev_at is not None else None
        # An early snapshot (WS just connected) would turn a burst into a
        # huge rate; report no rate and keep measuring from the old baseline.
        measured = elapsed is not None and elapsed >= 1.0
        rates = {}
        for name in self.COUNTERS:
            delta = self.counts[name] - self._prev_counts[name]
            rates[f"{name}_hz"] = round(delta / elapsed, 2) if measured else None
        if measured or self._prev_at is None:
            self._prev_counts = dict(self.counts)
            self._prev_at = mono
        age = mono - self.last_new_tick_at if self.last_new_tick_at is not None else None
        return {
            "ts": wall,
            "ws_connected": self.ws_connected,
            "source": self.source,
            "stale_source": is_stale_source(self.source) if self.source else None,
            "last_tick": self.last_tick,
            "last_update_ts": self.last_update_ts,
            "last_new_tick_age_s": round(age, 1) if age is not None else None,
            **rates,
            "state_max_hz": STATE_MAX_HZ,
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


# `last_seen_ms` is u64::MAX for a node that never sent a frame.
_MAX_LAST_SEEN_MS = 7 * 24 * 3600 * 1000


def node_payloads(update: dict[str, Any]) -> dict[int, dict[str, Any]]:
    """Per-node health, merged from `nodes` (fresh nodes only) and
    `node_features` (every node the server knows, with `stale`)."""
    out: dict[int, dict[str, Any]] = {}
    for feat in update.get("node_features") or []:
        nid = int(feat.get("node_id", 0))
        cls = feat.get("classification") or {}
        last_seen = _num(feat.get("last_seen_ms"))
        out[nid] = {
            "node_id": nid,
            "rssi_dbm": _num(feat.get("rssi_dbm")),
            "csi_fps": _num(feat.get("frame_rate_hz")),
            "stale": bool(feat.get("stale", False)),
            "last_seen_ms": int(last_seen) if last_seen is not None and last_seen < _MAX_LAST_SEEN_MS else None,
            "motion_level": cls.get("motion_level"),
            "person_score": _num(cls.get("confidence")),
        }
    for node in update.get("nodes") or []:
        nid = int(node.get("node_id", 0))
        entry = out.setdefault(nid, {"node_id": nid, "stale": False})
        entry["rssi_dbm"] = _num(node.get("rssi_dbm"))
        entry["subcarrier_count"] = int(node.get("subcarrier_count", 0))
        entry["position"] = node.get("position", [0, 0, 0])
    return out


class NodeRegistry:
    """Auto-publishes discovery configs the first time a node id appears."""

    def __init__(self, client: mqtt.Client) -> None:
        self._client = client
        self._seen: set[int] = set()

    def publish_node(self, payload: dict[str, Any]) -> None:
        nid = int(payload.get("node_id", 0))
        if nid not in self._seen:
            self._seen.add(nid)
            object_id = f"node_{nid}_rssi"
            topic, discovery = _discovery("sensor", object_id, {
                "name": f"Node {nid} RSSI",
                "state_topic": f"{MQTT_PREFIX}/nodes/{nid}",
                "value_template": _optional_number("rssi_dbm", "| round(0)"),
                "unit_of_measurement": "dBm",
                "device_class": "signal_strength",
                "state_class": "measurement",
                "entity_category": "diagnostic",
            })
            self._client.publish(topic, discovery, retain=True)
            LOG.info("registered node %s with HA discovery", nid)

        self._client.publish(f"{MQTT_PREFIX}/nodes/{nid}", json.dumps(payload))


# ---------------------------------------------------------------------------
# MQTT client setup with reconnect
# ---------------------------------------------------------------------------
def make_mqtt(on_connected: threading.Event | None = None) -> mqtt.Client:
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
            LOG.info(
                "HA discovery configs published (room entities %s)",
                "on" if ROOM_ENTITIES else "retracted",
            )
            # Change-only topics are re-sent in full after a (re)connect.
            if on_connected is not None:
                on_connected.set()
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
def ws_connect():
    """Open the sensing WebSocket, sending the bearer token when configured."""
    kwargs: dict[str, Any] = {"ping_interval": 20, "ping_timeout": 20}
    if WS_TOKEN:
        headers = {"Authorization": f"Bearer {WS_TOKEN}"}
        # websockets >= 14 renamed extra_headers to additional_headers.
        major = int(websockets.__version__.split(".")[0])
        kwargs["additional_headers" if major >= 14 else "extra_headers"] = headers
    return websockets.connect(WS_URL, **kwargs)


def _person_payload(person: dict[str, Any], label: str | None = None) -> dict[str, Any]:
    cx, cy = _person_center(person)
    z = _person_z(person)
    payload: dict[str, Any] = {"occupied": True}
    if label is not None:
        payload["label"] = label
    payload.update({
        "track_id": int(person.get("id", 0)),
        "confidence": float(person.get("confidence", 0.0)),
        "x": cx,
        "y": cy,
        "z": 0.0 if z is None else z,
        "zone": str(person.get("zone", "")),
    })
    return payload


class Exporter:
    """Turns sensing-server WebSocket messages into MQTT publishes."""

    def __init__(self, client: mqtt.Client, stats: BridgeStats | None = None,
                 resync: threading.Event | None = None) -> None:
        self._client = client
        self.stats = stats or BridgeStats()
        self._resync = resync or threading.Event()
        self._nodes = NodeRegistry(client)
        self._labels = LabelRegistry(client)
        self._edge = EdgeVitalsCache(EDGE_VITALS_TTL_S)
        self._state_gate = RateGate(_interval(STATE_MAX_HZ))
        self._node_gate = RateGate(_interval(NODE_MAX_HZ))
        self._person_gate = ChangeGate(_interval(PERSON_MAX_HZ), PERSON_HEARTBEAT_S)
        self._avail: dict[str, str] = {}
        self._last_tick: int | None = None
        self._last_fall_tick = -1
        # A fall seen on a throttled tick is carried into the next state.
        self._pending_fall = False

    def _publish_avail(self, topic: str, value: str) -> None:
        if self._avail.get(topic) != value:
            self._avail[topic] = value
            self._client.publish(topic, value, retain=True)

    def _publish_person(self, topic: str, payload: dict[str, Any], now: float) -> None:
        body = json.dumps(payload)
        key = (payload.get("occupied"), payload.get("track_id"), payload.get("zone"))
        if self._person_gate.allow(topic, body, key, now):
            self._client.publish(topic, body)

    def handle(self, raw: str | bytes, now: float) -> None:
        """Process one WebSocket message. `now` is a monotonic clock."""
        if self._resync.is_set():
            self._resync.clear()
            self._avail.clear()
            self._person_gate.reset()
            self._node_gate.reset()
        self.stats.bump("msgs_in")
        try:
            update = json.loads(raw)
        except json.JSONDecodeError:
            LOG.debug("non-JSON frame: %r", raw[:80])
            return
        if not isinstance(update, dict):
            return
        msg_type = update.get("type")
        if msg_type == "edge_vitals":
            self.stats.bump("edge_vitals_in")
            self._edge.observe(update, now)
            return
        if msg_type and msg_type != "sensing_update":
            return

        self.stats.observe_source(str(update.get("source", "unknown")))
        # The server re-broadcasts its latest update at 10 Hz with the same
        # tick; only a new tick carries new data. A server restart resets the
        # counter, so any change counts as new. Updates without a tick (old
        # servers) are never deduplicated.
        if "tick" in update:
            tick = int(update["tick"])
            if tick == self._last_tick:
                self.stats.bump("duplicates")
                return
            self._last_tick = tick
            self.stats.last_tick = tick
        self.stats.bump("new_ticks")
        self.stats.last_new_tick_at = now
        self.stats.last_update_ts = _num(update.get("timestamp"))

        state = translate(update, self._edge.summary(now))
        self._pending_fall = self._pending_fall or state["fall_detected"]
        if self._state_gate.allow("state", now):
            state["fall_detected"] = self._pending_fall
            self._pending_fall = False
            self._client.publish(STATE_TOPIC, json.dumps(state))
            self.stats.bump("state_published")
        else:
            self.stats.bump("state_throttled")

        for nid, payload in node_payloads(update).items():
            if self._node_gate.allow(nid, now):
                self._nodes.publish_node(payload)

        self._publish_persons(update, now)

        # Edge-trigger fall event: only fire once per tick where
        # `fall_detected` flips from false->true. Evaluated on every new tick,
        # so throttling the state topic never drops a fall.
        if state["fall_detected"] and state["tick"] != self._last_fall_tick:
            self._last_fall_tick = state["tick"]
            self._client.publish(EVENT_TOPIC, json.dumps({
                "event_type": "fall",
                "tick": state["tick"],
                "ts": state["ts"],
                "source": state["source"],
                "confidence": state["confidence"],
            }))
            LOG.warning("FALL event published (tick=%s)", state["tick"])

    def _publish_persons(self, update: dict[str, Any], now: float) -> None:
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
            self._labels.ensure_registered(label)
            self._publish_avail(label_avail_topic(label), "online")
            self._publish_person(label_state_topic(label), _person_payload(person, label), now)
            published_labels.add(label)

        # Any previously-registered label not seen this tick goes offline.
        for label in self._labels._seen - published_labels:
            self._publish_avail(label_avail_topic(label), "offline")
            self._publish_person(label_state_topic(label), {"occupied": False, "label": label}, now)

        # Fixed slots — only used for unlabeled detections (multi-person
        # ticks today, or any tick before profiles are enrolled).
        unlabeled.sort(key=lambda p: _person_center(p)[0])
        slot_assignments = {
            i + 1: p for i, p in enumerate(unlabeled[:MAX_TRACKED_PERSONS])
        }
        for slot in range(1, MAX_TRACKED_PERSONS + 1):
            person = slot_assignments.get(slot)
            if person is None:
                self._publish_avail(person_avail_topic(slot), "offline")
                self._publish_person(person_state_topic(slot), {"occupied": False}, now)
                continue
            self._publish_avail(person_avail_topic(slot), "online")
            self._publish_person(person_state_topic(slot), _person_payload(person), now)


async def consume(stop: asyncio.Event, exporter: Exporter, status_now: asyncio.Event) -> None:
    backoff = 1.0
    stats = exporter.stats

    while not stop.is_set():
        try:
            LOG.info("connecting to %s", WS_URL)
            async with ws_connect() as ws:
                LOG.info("WS connected")
                stats.ws_connected = True
                status_now.set()
                backoff = 1.0

                async for raw in ws:
                    if stop.is_set():
                        break
                    try:
                        exporter.handle(raw, time.monotonic())
                    except Exception:  # one odd message must not kill the bridge
                        LOG.exception("failed to handle message: %r", raw[:200])
                    if stats.source_flipped:
                        stats.source_flipped = False
                        LOG.warning("source is now %s", stats.source)
                        status_now.set()
        except (ConnectionClosed, WebSocketException, OSError) as exc:
            LOG.warning("WS error: %s — retrying in %.1fs", exc, backoff)
        finally:
            if stats.ws_connected:
                stats.ws_connected = False
                status_now.set()
        if stop.is_set():
            break
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff)
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, 30.0)


async def report_status(stop: asyncio.Event, client: mqtt.Client, stats: BridgeStats,
                        status_now: asyncio.Event) -> None:
    """Publish the retained bridge status every STATUS_INTERVAL_S, and right
    away when the WebSocket connects/drops or the source goes (un)stale."""
    while not stop.is_set():
        payload = stats.snapshot(time.time(), time.monotonic())
        client.publish(BRIDGE_STATUS_TOPIC, json.dumps(payload), retain=True)
        status_now.clear()
        try:
            await asyncio.wait_for(status_now.wait(), timeout=STATUS_INTERVAL_S)
        except asyncio.TimeoutError:
            pass


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
    LOG.info("  Rates: state<=%.1f Hz node<=%.1f Hz person<=%.1f Hz (heartbeat %.0fs)",
             STATE_MAX_HZ, NODE_MAX_HZ, PERSON_MAX_HZ, PERSON_HEARTBEAT_S)

    stop = asyncio.Event()
    status_now = asyncio.Event()

    def _shutdown() -> None:
        stop.set()
        status_now.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _shutdown)

    resync = threading.Event()
    client = make_mqtt(resync)
    exporter = Exporter(client, resync=resync)
    try:
        await asyncio.gather(
            consume(stop, exporter, status_now),
            report_status(stop, client, exporter.stats, status_now),
        )
    finally:
        client.publish(AVAIL_TOPIC, "offline", retain=True)
        client.loop_stop()
        client.disconnect()
        LOG.info("bridge stopped")


if __name__ == "__main__":
    asyncio.run(main())
