"""Home Assistant MQTT discovery for the watchdog entities.

These entities read the one retained report topic (`ruview/watchdog/state`):

- binary_sensor  Problem      ON when any warning/critical issue is open
- sensor         Status       ok / info / warning / critical
- sensor         Open Issues  number of open warning/critical issues
- sensor         Top Issue    summary of the most severe open issue
- sensor         CSI Vitals   whether CSI heart/breathing rates are published
- sensor         Last Check   when the watchdog last ran (heartbeat)
- binary_sensor  one per check (Bridge, CSI Source, Node 1, ...), with the
                 summary, severity and start time as attributes

NAS Link (connectivity) reads the NAS bus bridge's own state topic instead
(`ruview/bus/ha_connected`): Home Assistant's broker turns it off by itself
when the bridge from the NAS drops, so it shows the outage while nothing
from the NAS reaches Home Assistant.
"""

from __future__ import annotations

import json
from typing import Any

from . import __version__
from .checks import Check
from .config import Config


def device(cfg: Config) -> dict[str, Any]:
    return {
        "identifiers": [cfg.device_id],
        "name": cfg.device_name,
        "manufacturer": "RuView",
        "model": "Data watchdog",
        "sw_version": __version__,
    }


def discovery_messages(cfg: Config, checks: list[Check]) -> list[tuple[str, str]]:
    topics = cfg.topics
    state_topic = topics["report"]
    base = {
        "device": device(cfg),
        "availability_topic": topics["avail"],
        "payload_available": "online",
        "payload_not_available": "offline",
        "state_topic": state_topic,
    }

    def disc(component: str, object_id: str, payload: dict[str, Any]) -> tuple[str, str]:
        topic = f"{cfg.ha_prefix}/{component}/{cfg.device_id}/{object_id}/config"
        body = {**base, "unique_id": f"{cfg.device_id}_{object_id}", **payload}
        return topic, json.dumps(body, ensure_ascii=False)

    msgs = [
        disc("binary_sensor", "problem", {
            "name": "Problem",
            "device_class": "problem",
            "value_template": "{{ 'ON' if value_json.status in ['warning', 'critical'] else 'OFF' }}",
            "json_attributes_topic": state_topic,
            "json_attributes_template":
                "{{ {'status': value_json.status, 'top_issue': value_json.top_issue,"
                " 'issues': value_json.issues} | tojson }}",
        }),
        disc("sensor", "status", {
            "name": "Status",
            "device_class": "enum",
            "options": ["ok", "info", "warning", "critical"],
            "value_template": "{{ value_json.status }}",
            "icon": "mdi:shield-check-outline",
        }),
        disc("sensor", "open_issues", {
            "name": "Open Issues",
            "value_template": "{{ value_json.open_issues }}",
            "state_class": "measurement",
            "icon": "mdi:alert-circle-outline",
        }),
        disc("sensor", "top_issue", {
            "name": "Top Issue",
            "value_template": "{{ value_json.top_issue }}",
            "icon": "mdi:message-alert-outline",
        }),
        disc("sensor", "csi_vitals", {
            "name": "CSI Vitals",
            "value_template": "{{ value_json.vitals.text }}",
            "json_attributes_topic": state_topic,
            "json_attributes_template": "{{ value_json.vitals | tojson }}",
            "icon": "mdi:heart-pulse",
        }),
        disc("sensor", "last_check", {
            "name": "Last Check",
            "device_class": "timestamp",
            "value_template": "{{ value_json.checked_at }}",
            "entity_category": "diagnostic",
        }),
    ]
    # No availability: the link state must show while the watchdog is
    # unreachable, which is exactly when the link is down.
    msgs.append((f"{cfg.ha_prefix}/binary_sensor/{cfg.device_id}/nas_link/config", json.dumps({
        "device": device(cfg),
        "unique_id": f"{cfg.device_id}_nas_link",
        "name": "NAS Link",
        "state_topic": topics["ha_link"],
        "payload_on": "1",
        "payload_off": "0",
        "device_class": "connectivity",
        "entity_category": "diagnostic",
    }, ensure_ascii=False)))
    for check in checks:
        ref = f"value_json.checks['{check.id}']"
        msgs.append(disc("binary_sensor", f"check_{check.id}", {
            "name": check.name,
            "device_class": "problem",
            # "None" renders the entity unknown while the check has no data.
            "value_template": (
                "{% set c = " + ref + " %}"
                "{{ 'ON' if c.state == 'problem' else ('OFF' if c.state == 'ok' else None) }}"
            ),
            "json_attributes_topic": state_topic,
            "json_attributes_template": "{{ " + ref + " | tojson }}",
        }))
    return msgs
