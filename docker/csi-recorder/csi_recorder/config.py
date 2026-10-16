"""Environment configuration, validated at startup."""

from __future__ import annotations

import ipaddress
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path


class ConfigError(ValueError):
    pass


def _float(env: Mapping[str, str], name: str, default: float, lo: float, hi: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be between {lo:g} and {hi:g}, got {value:g}")
    return value


def _port(name: str, raw: str) -> int:
    raw = raw.strip()
    if not raw.isdigit() or not 0 < int(raw) < 65536:
        raise ConfigError(f"{name} must be a port number, got {raw!r}")
    return int(raw)


def parse_listen(raw: str) -> tuple[str, int]:
    """`127.0.0.1:5006` or `[::1]:5006`. Loopback only: the sensing-server
    tee never sends anywhere else, and a LAN-facing socket would let other
    machines write into the recordings."""
    host, sep, port = raw.strip().rpartition(":")
    if not sep:
        raise ConfigError(f"RECORDER_LISTEN must be ip:port, got {raw!r}")
    host = host.strip("[]")
    try:
        ip = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ConfigError(f"RECORDER_LISTEN must use an IP address, got {host!r}") from exc
    if not ip.is_loopback:
        raise ConfigError(f"RECORDER_LISTEN must be a loopback address, got {host}")
    return host, _port("RECORDER_LISTEN", port)


@dataclass(frozen=True)
class Config:
    listen_host: str = "127.0.0.1"
    listen_port: int = 5006
    data_dir: str = "/data"

    ring_hours: float = 72.0            # rolling window of raw datagrams
    ring_max_gb: float = 20.0           # hard cap on the ring, oldest files go first
    min_free_gb: float = 2.0            # stop writing below this much free disk
    keep_max_min: float = 120.0         # longest single permanent clip
    keep_max_h_per_day: float = 2.0     # permanent clips per UTC day
    flush_interval_s: float = 5.0       # data older than this is readable on disk
    status_interval_s: float = 30.0
    gzip_level: int = 6

    # Empty MQTT_HOST runs without MQTT (no status, no keep requests).
    mqtt_host: str = "homeassistant.local"
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_pass: str = ""
    mqtt_prefix: str = "ruview"
    client_id: str = "ruview-csi-recorder"

    @property
    def ring_dir(self) -> Path:
        return Path(self.data_dir) / "ring"

    @property
    def keep_dir(self) -> Path:
        return Path(self.data_dir) / "keep"

    @property
    def topics(self) -> dict[str, str]:
        p = self.mqtt_prefix
        return {
            "status": f"{p}/recorder/status",
            "avail": f"{p}/recorder/availability",
            "keep": f"{p}/recorder/keep",
            "keep_result": f"{p}/recorder/keep/result",
        }

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        host, port = parse_listen(env.get("RECORDER_LISTEN", "") or f"{cls.listen_host}:{cls.listen_port}")
        data_dir = env.get("CSI_DATA_DIR", "").strip() or cls.data_dir
        if not os.path.isabs(data_dir):
            raise ConfigError(f"CSI_DATA_DIR must be an absolute path, got {data_dir!r}")
        level = int(_float(env, "GZIP_LEVEL", cls.gzip_level, 1, 9))
        return cls(
            listen_host=host,
            listen_port=port,
            data_dir=data_dir,
            ring_hours=_float(env, "RING_HOURS", cls.ring_hours, 1, 24 * 30),
            ring_max_gb=_float(env, "RING_MAX_GB", cls.ring_max_gb, 0.1, 10_000),
            min_free_gb=_float(env, "MIN_FREE_GB", cls.min_free_gb, 0, 10_000),
            keep_max_min=_float(env, "KEEP_MAX_MIN", cls.keep_max_min, 1, 24 * 60),
            keep_max_h_per_day=_float(env, "KEEP_MAX_H_PER_DAY", cls.keep_max_h_per_day, 0, 24),
            flush_interval_s=_float(env, "FLUSH_INTERVAL_S", cls.flush_interval_s, 1, 60),
            status_interval_s=_float(env, "STATUS_INTERVAL_S", cls.status_interval_s, 5, 600),
            gzip_level=level,
            mqtt_host=env.get("MQTT_HOST", cls.mqtt_host).strip(),
            mqtt_port=_port("MQTT_PORT", env.get("MQTT_PORT", "") or str(cls.mqtt_port)),
            mqtt_user=env.get("MQTT_USER", ""),
            mqtt_pass=env.get("MQTT_PASS", ""),
            mqtt_prefix=env.get("MQTT_PREFIX", cls.mqtt_prefix).strip() or cls.mqtt_prefix,
        )
