"""Every field the bridge publishes on ruview/state, ruview/nodes/<id> and
ruview/person/<key> has a column in its TimescaleDB table (WP1.1).

Telegraf may only insert into those tables (docker/timescaledb/migrations),
so a field without a column is dropped until a migration adds it. When a
payload gains a field, add the column in a new migration.
"""

from __future__ import annotations

import os
import pathlib
import re
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import bridge  # noqa: E402

MIGRATIONS = pathlib.Path(__file__).resolve().parents[2] / "timescaledb" / "migrations"

# A server update with every part the bridge reads.
UPDATE = {
    "type": "sensing_update",
    "timestamp": 1_792_000_000.0,
    "source": "esp32",
    "tick": 7,
    "nodes": [{"node_id": 1, "rssi_dbm": -52.0, "position": [0, 0, 0], "subcarrier_count": 64}],
    "node_features": [{"node_id": 1, "stale": False, "frame_rate_hz": 21.7, "last_seen_ms": 40,
                       "rssi_dbm": -50.0, "classification": {"motion_level": "active", "confidence": 0.7}}],
    "features": {"motion_band_power": 0.1},
    "classification": {"motion_level": "present_still", "presence": True, "confidence": 0.8},
    "vital_signs": {"breathing_rate_bpm": 14.2, "heart_rate_bpm": 61.5, "breathing_confidence": 0.7,
                    "heartbeat_confidence": 0.6, "signal_quality": 0.5},
    "calibrated_presence_evidence": {"presence": True, "person_count": 1},
    "room_inference": {"classification": "present_still", "confidence": 0.9},
    "estimated_persons": 1,
    "fall_detected": False,
}


def columns(table: str) -> set[str]:
    """Columns of ingest.<table> after every migration (CREATE TABLE and ADD COLUMN)."""
    cols: set[str] = set()
    for sql in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
        text = sql.read_text()
        create = re.search(rf"CREATE TABLE IF NOT EXISTS ingest\.{table} \((.*?)\n\);", text, re.S)
        if create:
            cols |= {line.split()[0].strip('"') for line in create.group(1).strip().splitlines()}
        for alter in re.findall(rf"ALTER TABLE (?:IF EXISTS )?ingest\.{table}\b([^;]*);", text):
            cols |= set(re.findall(r'ADD COLUMN (?:IF NOT EXISTS )?"?(\w+)', alter))
    return cols


def telegraf_fields(payload: dict, prefix: str = "") -> set[str]:
    """Field names as Telegraf's JSON parser makes them: nested keys joined
    with _, list items numbered (position -> position_0, ...)."""
    names: set[str] = set()
    for key, value in payload.items():
        name = f"{prefix}_{key}" if prefix else key
        if isinstance(value, dict):
            names |= telegraf_fields(value, name)
        elif isinstance(value, list):
            names |= telegraf_fields({str(i): v for i, v in enumerate(value)}, name)
        else:
            names.add(name)
    return names


def test_columns_are_parsed():
    assert {"time", "heart_rate_bpm", "calibration_fresh"} <= columns("state")
    assert {"time", "node_id", "position_2"} <= columns("node")


def test_state_fields_have_columns():
    missing = telegraf_fields(bridge.translate(UPDATE)) - columns("state")
    assert not missing, f"ingest.state has no column for {sorted(missing)}; add them in a new migration"


def test_node_fields_have_columns():
    fields = set().union(*(telegraf_fields(p) for p in bridge.node_payloads(UPDATE).values()))
    missing = fields - columns("node")
    assert not missing, f"ingest.node has no column for {sorted(missing)}; add them in a new migration"


def test_person_fields_have_columns():
    person = {"id": 3, "confidence": 0.8, "zone": "bed",
              "keypoints": [{"z": 0.9, "confidence": 0.9}],
              "bbox": {"x": 1.0, "y": 1.0, "width": 0.4, "height": 1.0}}
    # person_key is the tag Telegraf takes from the topic.
    fields = telegraf_fields(bridge._person_payload(person, label="sq")) | {"person_key"}
    missing = fields - columns("person")
    assert not missing, f"ingest.person has no column for {sorted(missing)}; add them in a new migration"
