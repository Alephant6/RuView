"""In-memory view of what the watchdog has observed.

Only short sliding windows are kept (15-30 min, at most one sample per
second per series); nothing is written to disk.
"""

from __future__ import annotations

import json
import logging
import math
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from .config import Config

LOG = logging.getLogger("ruview-analytics.store")

# MQTT payloads are small JSON objects; anything bigger is not ours.
MAX_PAYLOAD_BYTES = 64 * 1024

# The sensing-server's room motion levels (`ruview/state` motion_level).
MOTION_LEVELS = ("absent", "present_still", "present_moving")
# Automatic calibration looks back at most this far for movement.
MOVING_WINDOW_S = 900.0


def as_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


class Series:
    """Samples in a sliding window, at most one per `min_dt_s` seconds."""

    def __init__(self, window_s: float, min_dt_s: float = 1.0) -> None:
        self._window_s = window_s
        self._min_dt_s = min_dt_s
        self._samples: deque[tuple[float, float]] = deque()

    def add(self, t: float, value: float) -> None:
        if self._samples and t - self._samples[-1][0] < self._min_dt_s:
            return
        self._samples.append((t, value))
        self._trim(t)

    def _trim(self, now: float) -> None:
        while self._samples and now - self._samples[0][0] > self._window_s:
            self._samples.popleft()

    def window(self, now: float, window_s: float) -> list[tuple[float, float]]:
        self._trim(now)
        return [(t, v) for t, v in self._samples if now - t <= window_s]

    def __len__(self) -> int:
        return len(self._samples)


@dataclass
class RestSnapshot:
    """One poll of the sensing-server REST API."""

    at: float
    health: dict[str, Any] | None = None
    error: str | None = None            # /health unreachable or not JSON
    auth_failed: bool = False           # /api/v1/* answered 401/403
    status: dict[str, Any] | None = None
    stream: dict[str, Any] | None = None
    nodes: dict[str, Any] | None = None
    calibration: dict[str, Any] | None = None


@dataclass
class PersonState:
    state: str
    since: float | None                 # last_changed, epoch seconds


@dataclass
class Store:
    cfg: Config
    started_at: float
    bridge_avail: str | None = None
    bridge_status: dict[str, Any] | None = None
    state_at: float | None = None
    last_state: dict[str, Any] | None = None
    nodes_mqtt: dict[int, tuple[float, dict[str, Any]]] = field(default_factory=dict)
    rest: RestSnapshot | None = None
    persons: dict[str, PersonState] | None = None
    persons_error: str | None = None
    recorder_avail: str | None = None
    recorder_status: dict[str, Any] | None = None
    ha_link: bool | None = None         # bus bridge to HA's broker up (None: no bus)
    hr: Series = field(init=False)
    br: Series = field(init=False)
    occupied: Series = field(init=False)
    moving: Series = field(init=False)  # 1 while the room motion level is "present_moving"
    # Previous run's report (retained topic), used to keep `since` stable
    # across watchdog restarts.
    prior_report: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        self.hr = Series(self.cfg.vitals_window_s)
        self.br = Series(self.cfg.vitals_window_s)
        self.occupied = Series(self.cfg.presence_window_s)
        self.moving = Series(MOVING_WINDOW_S)

    # ── MQTT ──────────────────────────────────────────────────────────────
    def on_message(self, topic: str, payload: bytes, now: float) -> None:
        topics = self.cfg.topics
        if len(payload) > MAX_PAYLOAD_BYTES:
            LOG.warning("dropping oversized payload on %s (%d bytes)", topic, len(payload))
            return
        if topic == topics["bridge_avail"]:
            self.bridge_avail = payload.decode("utf-8", "replace").strip()
            return
        if topic == topics["recorder_avail"]:
            self.recorder_avail = payload.decode("utf-8", "replace").strip()
            return
        if topic == topics["ha_link"]:
            self.ha_link = {b"1": True, b"0": False}.get(payload.strip())
            return
        if topic == topics["report"]:
            if self.prior_report is None:
                self.prior_report = _json_object(payload) or {}
            return
        data = _json_object(payload)
        if data is None:
            return
        if topic == topics["bridge_status"]:
            self.bridge_status = data
        elif topic == topics["recorder_status"]:
            self.recorder_status = data
        elif topic == topics["state"]:
            self.on_state(data, now)
        elif topic.startswith(f"{self.cfg.mqtt_prefix}/nodes/"):
            suffix = topic.rsplit("/", 1)[-1]
            if suffix.isdigit():
                self.nodes_mqtt[int(suffix)] = (now, data)

    def on_state(self, state: dict[str, Any], now: float) -> None:
        self.state_at = now
        self.last_state = state
        # The pre-2026-10 bridge wrote missing rates as 0.0: treat <= 0 as missing.
        hr = as_number(state.get("heart_rate_bpm"))
        if hr is not None and hr > 0:
            self.hr.add(now, hr)
        br = as_number(state.get("breathing_rate_bpm"))
        if br is not None and br > 0:
            self.br.add(now, br)
        occ = occupancy(state)
        if occ is not None:
            self.occupied.add(now, 1.0 if occ else 0.0)
        level = state.get("motion_level")
        if level in MOTION_LEVELS:
            self.moving.add(now, 1.0 if level == "present_moving" else 0.0)

    def bridge_last_seen(self) -> float | None:
        """Newest evidence that the bridge process is alive (epoch seconds)."""
        seen = [t for t in (self.state_at, as_number((self.bridge_status or {}).get("ts"))) if t]
        return max(seen) if seen else None


def occupancy(state: dict[str, Any]) -> bool | None:
    """Best available "someone is in the room" signal from one state message:
    the calibrated person count, else the fused room class, else the legacy
    presence flag."""
    if state.get("calibrated") and isinstance(state.get("cal_person_count"), int):
        return state["cal_person_count"] > 0
    room = state.get("room_class")
    if isinstance(room, str) and room not in ("", "unavailable"):
        return room != "absent"
    if "presence" in state:
        return bool(state["presence"])
    return None


def _json_object(payload: bytes) -> dict[str, Any] | None:
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None
