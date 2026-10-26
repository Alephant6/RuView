"""Unit tests for the WS -> MQTT exporter (no broker or server needed).

Run from docker/bridge:  python -m pytest tests -q
"""

from __future__ import annotations

import json
import os
import sys

import pytest

os.environ.setdefault("STATE_MAX_HZ", "10")
os.environ.setdefault("NODE_MAX_HZ", "1")
os.environ.setdefault("PERSON_MAX_HZ", "0.2")
os.environ.setdefault("PERSON_HEARTBEAT_S", "30")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bridge  # noqa: E402


class FakeClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool]] = []

    def publish(self, topic, payload, retain=False, qos=0):
        self.published.append((topic, payload, retain))

    def topics(self, topic: str) -> list[str]:
        return [p for t, p, _ in self.published if t == topic]

    def clear(self) -> None:
        self.published.clear()


def update(tick: int, *, source: str = "esp32", vitals: dict | None = None, **extra) -> str:
    msg = {
        "type": "sensing_update",
        "timestamp": 1_792_000_000.0 + tick / 10,
        "source": source,
        "tick": tick,
        "nodes": [{"node_id": 1, "rssi_dbm": -52.0, "position": [0, 0, 0],
                   "amplitude": [], "subcarrier_count": 64}],
        "features": {"motion_band_power": 0.1},
        "classification": {"motion_level": "present_still", "presence": True, "confidence": 0.8},
        "signal_field": {"grid_size": [1, 1, 1], "values": [0.0]},
    }
    if vitals is not None:
        msg["vital_signs"] = vitals
    msg.update(extra)
    return json.dumps(msg)


VITALS = {
    "breathing_rate_bpm": 14.2,
    "heart_rate_bpm": 61.5,
    "breathing_confidence": 0.7,
    "heartbeat_confidence": 0.6,
    "signal_quality": 0.5,
}


# ── translate ────────────────────────────────────────────────────────────

def test_missing_vitals_are_null_not_zero():
    state = bridge.translate(json.loads(update(1)))
    assert state["heart_rate_bpm"] is None
    assert state["breathing_rate_bpm"] is None
    assert state["signal_quality"] is None
    assert state["vitals_present"] is False
    assert '"heart_rate_bpm": null' in json.dumps(state)


def test_published_vitals_carry_confidence():
    state = bridge.translate(json.loads(update(1, vitals=VITALS)))
    assert state["heart_rate_bpm"] == 61.5
    assert state["breathing_rate_bpm"] == 14.2
    assert state["hr_conf"] == 0.6
    assert state["br_conf"] == 0.7
    assert state["vitals_present"] is True


def test_zero_or_nan_rate_is_missing():
    vitals = dict(VITALS, heart_rate_bpm=0.0, breathing_rate_bpm=float("nan"))
    state = bridge.translate(json.loads(update(1)) | {"vital_signs": vitals})
    assert state["heart_rate_bpm"] is None
    assert state["breathing_rate_bpm"] is None
    assert state["hr_conf"] is None
    assert state["vitals_present"] is False


def test_quality_fields():
    msg = json.loads(update(
        1,
        source="esp32:offline",
        calibrated_presence_evidence={"presence": True, "person_count": 1},
        room_inference={"classification": "present_still", "confidence": 0.9, "contributing_nodes": 2},
        node_features=[
            {"node_id": 1, "stale": False, "frame_rate_hz": 22.0},
            {"node_id": 2, "stale": True, "frame_rate_hz": 0.0},
        ],
    ))
    state = bridge.translate(msg)
    assert state["stale_source"] is True
    assert state["calibrated"] is True
    assert state["cal_person_count"] == 1
    assert state["room_class"] == "present_still"
    assert state["fresh_nodes"] == 1
    assert state["abstention_reason"] is None


# ── edge_vitals gate ─────────────────────────────────────────────────────

def edge(node_id: int, authorized: bool, reason: str | None, fresh: bool = True) -> dict:
    return {"type": "edge_vitals", "node_id": node_id, "numeric_vitals_authorized": authorized,
            "abstention_reason": reason, "calibrated_evidence_authorized": fresh}


def test_edge_vitals_reason_and_expiry():
    cache = bridge.EdgeVitalsCache(ttl_s=60)
    assert cache.summary(0)["abstention_reason"] is None
    cache.observe(edge(1, False, "exactly_one_occupant_required"), now=10)
    summary = cache.summary(20)
    assert summary == {"abstention_reason": "exactly_one_occupant_required",
                       "numeric_vitals_authorized": False, "calibration_fresh": True}
    cache.observe(edge(2, True, None), now=30)
    assert cache.summary(31)["abstention_reason"] is None
    assert cache.summary(31)["numeric_vitals_authorized"] is True
    assert cache.summary(200) == bridge.EDGE_FIELDS_UNKNOWN


def test_old_edge_vitals_without_gate_fields_are_ignored():
    cache = bridge.EdgeVitalsCache(ttl_s=60)
    cache.observe({"type": "edge_vitals", "node_id": 1, "heartrate_bpm": 70.0}, now=1)
    assert cache.summary(2) == bridge.EDGE_FIELDS_UNKNOWN


# ── Exporter: dedupe, throttle, events ───────────────────────────────────

def test_rebroadcast_of_same_tick_is_dropped_and_source_flip_flagged():
    client = FakeClient()
    exp = bridge.Exporter(client)
    exp.handle(update(5), now=0.0)
    exp.handle(update(5), now=0.5)
    exp.handle(update(5, source="esp32:offline"), now=6.0)
    assert len(client.topics(bridge.STATE_TOPIC)) == 1
    assert exp.stats.counts["duplicates"] == 2
    assert exp.stats.source == "esp32:offline"
    assert exp.stats.source_flipped is True
    # A restarted server starts counting again: a lower tick is new data.
    exp.handle(update(1), now=7.0)
    assert len(client.topics(bridge.STATE_TOPIC)) == 2


def test_state_is_rate_limited():
    client = FakeClient()
    exp = bridge.Exporter(client)
    for i in range(70):  # 70 new ticks in one second
        exp.handle(update(i + 1), now=i / 70)
    published = len(client.topics(bridge.STATE_TOPIC))
    assert published == 10
    assert exp.stats.counts["state_throttled"] == 60


def test_fall_on_throttled_tick_still_fires():
    client = FakeClient()
    exp = bridge.Exporter(client)
    exp.handle(update(1), now=0.0)
    exp.handle(update(2, fall_detected=True), now=0.01)  # throttled state
    events = client.topics(bridge.EVENT_TOPIC)
    assert len(events) == 1 and json.loads(events[0])["tick"] == 2
    exp.handle(update(3), now=0.2)  # next published state reports the fall
    states = [json.loads(s) for s in client.topics(bridge.STATE_TOPIC)]
    assert [s["fall_detected"] for s in states] == [False, True]


def test_ticks_without_tick_field_are_not_deduplicated():
    client = FakeClient()
    exp = bridge.Exporter(client)
    msg = json.loads(update(1))
    del msg["tick"]
    exp.handle(json.dumps(msg), now=0.0)
    exp.handle(json.dumps(msg), now=1.0)
    assert len(client.topics(bridge.STATE_TOPIC)) == 2


def test_edge_vitals_message_feeds_state():
    client = FakeClient()
    exp = bridge.Exporter(client)
    exp.handle(json.dumps(edge(1, False, "explicit_calibration_required", fresh=False)), now=0.0)
    exp.handle(update(1), now=0.1)
    state = json.loads(client.topics(bridge.STATE_TOPIC)[0])
    assert state["abstention_reason"] == "explicit_calibration_required"
    assert state["calibration_fresh"] is False


def test_bad_message_types_are_ignored():
    client = FakeClient()
    exp = bridge.Exporter(client)
    exp.handle("not json", now=0.0)
    exp.handle("[1, 2]", now=0.0)
    exp.handle(json.dumps({"type": "pose_data", "tick": 1}), now=0.0)
    assert client.published == []


# ── nodes ────────────────────────────────────────────────────────────────

def test_node_payload_merges_features_and_caps_last_seen():
    msg = json.loads(update(1, node_features=[
        {"node_id": 1, "stale": False, "frame_rate_hz": 21.7, "last_seen_ms": 40,
         "rssi_dbm": -50.0, "classification": {"motion_level": "active", "confidence": 0.7}},
        {"node_id": 3, "stale": True, "frame_rate_hz": 0.0, "last_seen_ms": 18446744073709551615,
         "rssi_dbm": 0.0, "classification": {"motion_level": "absent", "confidence": 0.0}},
    ]))
    nodes = bridge.node_payloads(msg)
    assert nodes[1]["csi_fps"] == 21.7 and nodes[1]["subcarrier_count"] == 64
    assert nodes[1]["stale"] is False and nodes[1]["rssi_dbm"] == -52.0
    assert nodes[3]["stale"] is True and nodes[3]["last_seen_ms"] is None


def test_node_topics_limited_to_one_hz():
    client = FakeClient()
    exp = bridge.Exporter(client)
    for i in range(30):
        exp.handle(update(i + 1), now=i / 30)
    assert len(client.topics(f"{bridge.MQTT_PREFIX}/nodes/1")) == 1


# ── persons ──────────────────────────────────────────────────────────────

def person(pid: int, x: float, zone: str = "bed") -> dict:
    return {"id": pid, "confidence": 0.8, "zone": zone, "keypoints": [],
            "bbox": {"x": x, "y": 1.0, "width": 0.4, "height": 1.0}}


def test_person_changes_wait_for_the_interval():
    client = FakeClient()
    exp = bridge.Exporter(client)
    slot1 = bridge.person_state_topic(1)
    avail1 = bridge.person_avail_topic(1)
    for i in range(20):  # nobody, for two seconds
        exp.handle(update(i + 1), now=i / 10)
    assert client.topics(avail1) == ["offline"]
    assert len(client.topics(slot1)) == 1

    # Someone appears: sent 1 / PERSON_MAX_HZ = 5 s after the last message.
    exp.handle(update(100, persons=[person(7, 1.0)]), now=2.0)
    assert client.topics(avail1) == ["offline"]
    exp.handle(update(101, persons=[person(7, 1.0)]), now=5.0)
    assert client.topics(avail1) == ["offline", "online"]
    assert json.loads(client.topics(slot1)[-1])["occupied"] is True

    # Moved: again at most once per interval.
    exp.handle(update(102, persons=[person(7, 1.1)]), now=6.0)
    assert len(client.topics(slot1)) == 2
    exp.handle(update(103, persons=[person(7, 1.2)]), now=10.0)
    assert len(client.topics(slot1)) == 3

    # Unchanged payload: only the heartbeat.
    exp.handle(update(104, persons=[person(7, 1.2)]), now=20.0)
    assert len(client.topics(slot1)) == 3
    exp.handle(update(105, persons=[person(7, 1.2)]), now=40.0)
    assert len(client.topics(slot1)) == 4

    # A zone change waits like any other change.
    exp.handle(update(106, persons=[person(7, 1.2, zone="desk")]), now=41.0)
    assert len(client.topics(slot1)) == 4
    exp.handle(update(107, persons=[person(7, 1.2, zone="desk")]), now=45.0)
    assert json.loads(client.topics(slot1)[-1])["zone"] == "desk"
    assert client.topics(avail1) == ["offline", "online"]


def test_track_churn_and_ghosts_cost_one_message_per_interval():
    client = FakeClient()
    exp = bridge.Exporter(client)
    # 30 s at 10 Hz: the track id changes on every tick, and a ghost second
    # track comes and goes on alternate ticks.
    for i in range(300):
        persons = [person(i, 1.0)] + ([person(1000 + i, 3.0)] if i % 2 else [])
        exp.handle(update(i + 1, persons=persons), now=i / 10)
    for topic in (bridge.person_state_topic(1), bridge.person_state_topic(2),
                  bridge.person_avail_topic(2)):
        assert 1 <= len(client.topics(topic)) <= 7, topic


def test_flicker_inside_the_interval_is_never_sent():
    client = FakeClient()
    exp = bridge.Exporter(client)
    exp.handle(update(1, persons=[person(7, 1.0)]), now=0.0)
    exp.handle(update(2), now=1.0)  # gone for one tick
    # Back, with jitter below the 0.01 the payload is rounded to.
    exp.handle(update(3, persons=[person(7, 1.001)]), now=1.1)
    exp.handle(update(4, persons=[person(7, 1.0)]), now=6.0)
    assert client.topics(bridge.person_avail_topic(1)) == ["online"]
    assert len(client.topics(bridge.person_state_topic(1))) == 1


def test_resync_republishes_availability():
    client = FakeClient()
    exp = bridge.Exporter(client)
    exp.handle(update(1), now=0.0)
    exp.handle(update(2), now=0.2)
    avail1 = bridge.person_avail_topic(1)
    assert client.topics(avail1) == ["offline"]
    exp._resync.set()
    exp.handle(update(3), now=0.4)
    assert client.topics(avail1) == ["offline", "offline"]


# ── status + discovery ───────────────────────────────────────────────────

def test_status_snapshot_rates():
    stats = bridge.BridgeStats()
    first = stats.snapshot(wall=1000.0, mono=0.0)
    assert first["new_ticks_hz"] is None
    for _ in range(50):
        stats.bump("msgs_in")
        stats.bump("new_ticks")
    stats.last_new_tick_at = 9.0
    snap = stats.snapshot(wall=1010.0, mono=10.0)
    assert snap["msgs_in_hz"] == 5.0 and snap["new_ticks_hz"] == 5.0
    assert snap["last_new_tick_age_s"] == 1.0


def test_optional_number_templates_render_unknown_for_null():
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment()
    configs = {json.loads(p)["unique_id"]: json.loads(p)
               for _, p in bridge.room_discovery_messages()}
    hr = env.from_string(configs[f"{bridge.DEVICE_ID}_heart_rate"]["value_template"])
    sq = env.from_string(configs[f"{bridge.DEVICE_ID}_signal_quality"]["value_template"])
    assert hr.render(value_json={"heart_rate_bpm": None}) == "None"
    assert hr.render(value_json={}) == "None"
    assert hr.render(value_json={"heart_rate_bpm": 61.6}) == "62.0"
    assert sq.render(value_json={"signal_quality": 0.4567}) == "45.7"
    assert sq.render(value_json={"signal_quality": None}) == "None"


def config_topic(component: str, object_id: str) -> str:
    return f"{bridge.HA_PREFIX}/{component}/{bridge.DEVICE_ID}/{object_id}/config"


def test_person_templates_never_touch_a_missing_field(monkeypatch):
    jinja2 = pytest.importorskip("jinja2")
    monkeypatch.setattr(bridge, "HA_PERSON_DETAIL", True)
    # StrictUndefined raises wherever HA's LoggingUndefined logs a warning or
    # an error: printing, testing, iterating or computing with a missing field.
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    client = FakeClient()
    bridge.LabelRegistry(client).ensure_registered("sq")
    configs = [json.loads(p) for _, p in bridge.slot_discovery_messages()]
    configs += [json.loads(p) for _, p, _ in client.published]
    # Every payload the bridge sends on a slot or label topic.
    payloads = [{"occupied": False}, {"occupied": False, "label": "sq"},
                bridge._person_payload(person(7, 1.0), label="sq")]
    rendered = {c["unique_id"]: [env.from_string(c["value_template"]).render(value_json=p)
                                 for p in payloads] for c in configs}
    prefix = f"{bridge.DEVICE_ID}_person"
    assert rendered[f"{prefix}_1_present"] == ["OFF", "OFF", "ON"]
    assert rendered[f"{prefix}_1_zone"] == ["None", "None", "bed"]
    assert rendered[f"{prefix}_1_x"] == ["None", "None", "1.2"]
    assert rendered[f"{prefix}_sq_confidence"] == ["None", "None", "80.0"]


def test_detail_and_node_rssi_entities_are_opt_in(monkeypatch):
    monkeypatch.setattr(bridge, "HA_PERSON_DETAIL", False)
    monkeypatch.setattr(bridge, "HA_NODE_RSSI", False)
    configs = dict(bridge.slot_discovery_messages())
    assert json.loads(configs[config_topic("binary_sensor", "person_1_present")])
    assert json.loads(configs[config_topic("sensor", "person_1_zone")])
    for suffix in ("confidence", "x", "y", "z"):
        assert configs[config_topic("sensor", f"person_1_{suffix}")] == ""
    client = FakeClient()
    bridge.LabelRegistry(client).ensure_registered("sq")
    assert client.topics(config_topic("sensor", "person_sq_x")) == [""]
    client = FakeClient()
    bridge.Exporter(client).handle(update(1), now=0.0)
    assert client.topics(config_topic("sensor", "node_1_rssi")) == [""]
    # The node topic keeps flowing for Telegraf and the watchdog.
    assert len(client.topics(f"{bridge.MQTT_PREFIX}/nodes/1")) == 1

    monkeypatch.setattr(bridge, "HA_PERSON_DETAIL", True)
    monkeypatch.setattr(bridge, "HA_NODE_RSSI", True)
    configs = dict(bridge.slot_discovery_messages())
    assert json.loads(configs[config_topic("sensor", "person_1_x")])["entity_category"] == "diagnostic"
    client = FakeClient()
    bridge.Exporter(client).handle(update(1), now=0.0)
    assert json.loads(client.topics(config_topic("sensor", "node_1_rssi"))[0])["unit_of_measurement"] == "dBm"
