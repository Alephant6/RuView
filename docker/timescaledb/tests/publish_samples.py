"""Publish one payload of every topic Telegraf stores in TimescaleDB, then a
short ruview/state stream at 10 Hz with a stretch where the CSI source is
offline. Used by the ingest test (.github/workflows/timescaledb.yml).
SYNTHETIC data: the values are made up and measure nothing.

    python publish_samples.py [--host 127.0.0.1] [--port 1883] [--seconds 5]
"""
from __future__ import annotations

import argparse
import json
import time

import paho.mqtt.client as mqtt

# Annotations are stored at their press time (ts, epoch ms); check.sql looks
# for this one.
ANNOTATION_TS_MS = 1_790_000_000_000  # 2026-09-21T14:13:20Z


def state(now: float, tick: int, stale: bool) -> dict:
    """Every field of the mqtt-bridge state payload (bridge.translate)."""
    hr = None if stale else 62.0 + tick % 7
    return {
        "presence": True, "confidence": 0.8, "motion_level": "present_still",
        "motion_energy": 0.1 * (tick % 5), "breathing_rate_bpm": None if stale else 15.0,
        "heart_rate_bpm": hr, "signal_quality": 0.6, "hr_conf": 0.5 if hr else None,
        "br_conf": None if stale else 0.6, "vitals_present": not stale, "n_persons": 1,
        "calibrated": True, "cal_person_count": 1, "room_class": "occupied", "room_conf": 0.7,
        "source": "esp32:offline" if stale else "esp32", "stale_source": stale,
        "active_nodes": 3, "fresh_nodes": 0 if stale else 3, "fall_detected": False,
        "tick": tick, "ts": now,
        "abstention_reason": "exactly_one_occupant_required" if stale else None,
        "numeric_vitals_authorized": not stale, "calibration_fresh": True,
    }


def one_of_each(now: float) -> list[tuple[str, dict | str]]:
    dev = "wifi_densepose_ruview-node1"
    return [
        ("ruview/nodes/2", {"node_id": 2, "rssi_dbm": -51.0, "csi_fps": 22.5, "stale": False,
                            "last_seen_ms": 40, "motion_level": "low", "person_score": 0.4,
                            "subcarrier_count": 128, "position": [6.45, 0.2, 0.7]}),
        ("ruview/person/1", {"occupied": True, "track_id": 7, "confidence": 0.8, "x": 1.2,
                             "y": 0.4, "z": 0.9, "zone": "bed"}),
        ("ruview/person/sq", {"occupied": True, "label": "sq", "track_id": 7, "confidence": 0.8,
                              "x": 1.2, "y": 0.4, "z": 0.9, "zone": "bed"}),
        ("ruview/person/2", {"occupied": False}),
        ("ruview/bridge", {"ts": now, "ws_connected": True, "source": "esp32", "stale_source": False,
                           "last_tick": 1, "last_update_ts": now, "last_new_tick_age_s": 0.1,
                           "msgs_in_hz": 70.0, "new_ticks_hz": 10.0, "duplicates_hz": 60.0,
                           "state_published_hz": 10.0, "state_throttled_hz": 0.0,
                           "edge_vitals_in_hz": 3.0, "state_max_hz": 10.0}),
        ("ruview/watchdog/metrics", {"status_level": 1, "critical": 0, "warning": 1, "info": 0,
                                     "check_node_2": 0, "check_vitals_frozen": 1}),
        ("ruview/recorder/status", {"ts": now, "recording": True, "datagrams_hz": 70.1,
                                    "kinds_hz": {"csi": 69.0, "sync": 1.1},
                                    "nodes": {"1": {"hz": 23.0, "csi_hz": 23.0, "age_s": 0.1}},
                                    "disk_low": False, "last_error": None, "ring_files": 3}),
        ("ruview/recorder/keep/result", {"status": "kept", "id": "ci", "tag": "gt_session",
                                         "start": "2026-10-15T04:00:00+00:00",
                                         "end": "2026-10-15T04:30:00+00:00", "duration_s": 1800.0,
                                         "person": "sq", "source": "ha", "file": "keep/ci.csi.raw.gz",
                                         "records": 1000, "bytes": 12345, "coverage": 0.99, "ts": now}),
        ("ruview/annotation", {"kind": "tag", "label": "alcohol", "action": "mark",
                               "ts": ANNOTATION_TS_MS, "person": "sq", "source": "ha", "value": 1,
                               "text": "ci sample"}),
        # The end of an automatic calibration (analytics autocal.py).
        ("ruview/annotation", {"kind": "calibration", "label": "empty_room", "action": "done",
                               "ts": ANNOTATION_TS_MS + 600_000, "person": "", "source": "watchdog",
                               "value": 1, "source_node": 1, "text": "ci calibration", "elapsed_s": 600,
                               "frame_count": 12000, "variance_explained": 0.93,
                               "baseline_eigenvalue_count": 2}),
        (f"homeassistant/sensor/{dev}/heart_rate/state", {"bpm": 61.0, "confidence": 0.7, "ts": now}),
        (f"homeassistant/sensor/{dev}/person_count/state", {"n_persons": 1, "ts": now}),
        (f"homeassistant/sensor/{dev}/rssi/state", {"dbm": -60.0, "ts": now}),
        # Another integration's sensor: Telegraf must not store it.
        ("homeassistant/sensor/other_device/temperature/state", {"t": 21.0}),
        (f"homeassistant/binary_sensor/{dev}/someone_sleeping/state", "ON"),
        (f"homeassistant/binary_sensor/{dev}/room_active/state", "OFF"),
        (f"homeassistant/event/{dev}/fall/state", {"event_type": "fall", "ts": now, "confidence": 0.5}),
    ]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=1883)
    ap.add_argument("--seconds", type=float, default=5.0)
    args = ap.parse_args()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="ruview-samples")
    client.connect(args.host, args.port)
    client.loop_start()
    now = time.time()
    for topic, payload in one_of_each(now):
        data = payload if isinstance(payload, str) else json.dumps(payload)
        client.publish(topic, data, qos=1).wait_for_publish(5)
    # ~10 Hz state; the source is offline for the middle fifth.
    n = int(args.seconds * 10)
    start = time.time()
    for i in range(n):
        stale = 2 * n // 5 <= i < 3 * n // 5
        client.publish("ruview/state", json.dumps(state(time.time(), 1000 + i, stale)), qos=1).wait_for_publish(5)
        time.sleep(max(0.0, start + (i + 1) * 0.1 - time.time()))
    client.loop_stop()
    client.disconnect()
    print(f"published {len(one_of_each(now))} samples and {n} state ticks")


if __name__ == "__main__":
    main()
