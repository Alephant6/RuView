"""Environment configuration, validated at startup."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

_ENTITY_ID = re.compile(r"^[a-z_]+\.[a-z0-9_]+$")


class ConfigError(ValueError):
    pass


def _http_url(name: str, value: str) -> str:
    value = value.strip().rstrip("/")
    if value and not re.match(r"^https?://[^\s/]+", value):
        raise ConfigError(f"{name} must be an http(s) URL, got {value!r}")
    return value


def _float(env: Mapping[str, str], name: str, default: float, lo: float, hi: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo} and {hi}, got {value}")
    return value


def _nodes(raw: str) -> tuple[int, ...]:
    nodes = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit() or not 0 <= int(part) <= 255:
            raise ConfigError(f"EXPECTED_NODES entries must be node ids 0-255, got {part!r}")
        nodes.append(int(part))
    return tuple(sorted(set(nodes)))


def _entities(raw: str) -> tuple[str, ...]:
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not _ENTITY_ID.match(part):
            raise ConfigError(f"HA_PERSONS entries must be entity ids like person.alice, got {part!r}")
        out.append(part)
    return tuple(out)


@dataclass(frozen=True)
class Config:
    mqtt_host: str = "homeassistant.local"
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_pass: str = ""
    mqtt_prefix: str = "ruview"
    ha_prefix: str = "homeassistant"

    ruview_http: str = "http://192.168.1.195:3000"
    api_token: str = ""
    http_timeout_s: float = 5.0

    # Optional Home Assistant REST access for the phone-presence check.
    ha_url: str = ""
    ha_token: str = ""
    ha_persons: tuple[str, ...] = ()

    expected_nodes: tuple[int, ...] = (1, 2, 3)
    check_interval_s: float = 30.0
    lang: str = "zh"
    device_id: str = "ruview_watchdog"
    device_name: str = "RuView Watchdog"

    # ── thresholds ──────────────────────────────────────────────────────
    bridge_silence_s: float = 60.0      # no bridge status / state for this long
    stall_after_s: float = 60.0         # WS up but no new tick for this long
    min_node_fps: float = 5.0           # nodes normally run ~20+ fps
    vitals_window_s: float = 900.0      # frozen / range checks look back 15 min
    frozen_min_samples: int = 100       # at most one sample per second is kept
    frozen_min_span_s: float = 600.0
    frozen_max_distinct: int = 2
    range_min_samples: int = 50
    range_max_fraction: float = 0.05
    hr_range: tuple[float, float] = (35.0, 180.0)
    br_range: tuple[float, float] = (4.0, 40.0)
    away_min_s: float = 3600.0          # both phones away at least this long
    presence_window_s: float = 1800.0
    presence_min_samples: int = 600
    presence_max_fraction: float = 0.8

    @property
    def topics(self) -> dict[str, str]:
        p = self.mqtt_prefix
        return {
            "bridge_avail": f"{p}/availability",
            "bridge_status": f"{p}/bridge",
            "state": f"{p}/state",
            "nodes": f"{p}/nodes/+",
            "avail": f"{p}/watchdog/availability",
            "report": f"{p}/watchdog/state",
            "metrics": f"{p}/watchdog/metrics",
        }

    @property
    def presence_enabled(self) -> bool:
        return bool(self.ha_url and self.ha_token and self.ha_persons)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        lang = env.get("WATCHDOG_LANG", "zh").strip().lower() or "zh"
        if lang not in ("zh", "en"):
            raise ConfigError(f"WATCHDOG_LANG must be zh or en, got {lang!r}")
        port_raw = env.get("MQTT_PORT", "1883").strip() or "1883"
        if not port_raw.isdigit() or not 0 < int(port_raw) < 65536:
            raise ConfigError(f"MQTT_PORT must be a TCP port, got {port_raw!r}")
        return cls(
            mqtt_host=env.get("MQTT_HOST", cls.mqtt_host).strip() or cls.mqtt_host,
            mqtt_port=int(port_raw),
            mqtt_user=env.get("MQTT_USER", ""),
            mqtt_pass=env.get("MQTT_PASS", ""),
            mqtt_prefix=env.get("MQTT_PREFIX", cls.mqtt_prefix).strip() or cls.mqtt_prefix,
            ha_prefix=env.get("HA_DISCOVERY_PREFIX", cls.ha_prefix).strip() or cls.ha_prefix,
            ruview_http=_http_url("RUVIEW_HTTP", env.get("RUVIEW_HTTP", cls.ruview_http)) or cls.ruview_http,
            api_token=env.get("RUVIEW_API_TOKEN", "").strip(),
            http_timeout_s=_float(env, "HTTP_TIMEOUT_S", cls.http_timeout_s, 0.5, 60),
            ha_url=_http_url("HA_URL", env.get("HA_URL", "")),
            ha_token=env.get("HA_TOKEN", "").strip(),
            ha_persons=_entities(env.get("HA_PERSONS", "")),
            expected_nodes=_nodes(env.get("EXPECTED_NODES", "1,2,3")),
            check_interval_s=_float(env, "CHECK_INTERVAL_S", cls.check_interval_s, 5, 600),
            lang=lang,
            min_node_fps=_float(env, "MIN_NODE_FPS", cls.min_node_fps, 0, 1000),
        )
