"""Automatic empty-room calibration against an in-memory sensing-server.

FakeServer mirrors the calibration endpoints of the v2 sensing-server
(crates/wifi-densepose-sensing-server/src/main.rs: calibration_start, _stop,
_cancel, _reset, _status): identities, guards, error codes and the 12 h /
24 h freshness steps. Frames arrive at 20 fps from the bound source node.

Run from docker/analytics:  python -m pytest -q
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ruview_analytics import checks as C  # noqa: E402
from ruview_analytics.autocal import BINDING_DIGEST, AutoCalibrator  # noqa: E402
from ruview_analytics.config import Config, ConfigError  # noqa: E402
from ruview_analytics.ha import discovery_messages  # noqa: E402
from ruview_analytics.rest import http_post_json  # noqa: E402
from ruview_analytics.store import MOVING_WINDOW_S, PersonState, RestSnapshot, Series, Store  # noqa: E402

T0 = 1_792_000_000.0
HOUR = 3600.0
OTHER_DIGEST = "ab" * 32


class FakeServer:
    """The sensing-server's calibration API, in memory."""

    MIN_FRAMES = 1000
    MIN_DURATION_S = 600.0

    def __init__(self, now: float, boot: str = "boot-1") -> None:
        self.now = now
        self.boot = boot
        self.fps = 20.0
        self.known_nodes = {1, 2, 3}
        self.grid_fresh = True              # the source grid has recent frames
        self.bootstrap_active = False
        self.bootstrap_stored = False
        self.calls: list[tuple[str, dict, str]] = []
        self.overrides: dict[str, tuple] = {}   # endpoint -> canned (status, body, error), once
        self.lose_answer: set[str] = set()      # endpoints that run but time out
        self._sessions = 0
        self._clear()

    def _clear(self) -> None:
        self.model: str | None = None       # None | uncalibrated | collecting | final
        self.completed_at: float | None = None
        self.frames = 0
        self.elapsed = 0.0
        self.session_id: str | None = None
        self.digest: str | None = None
        self.nodes: list[int] = []
        self.source: int | None = None
        self.observed: set[int] = set()
        self.receipt: dict | None = None
        self.faults: set[int] = set()

    # ── test setup ───────────────────────────────────────────────────────

    def finalized(self, age_s: float, digest: str = OTHER_DIGEST, nodes=(1,)) -> None:
        """A completed explicit calibration from `age_s` ago."""
        self._clear()
        self._sessions += 1
        self.model, self.completed_at = "final", self.now - age_s
        self.session_id, self.digest = f"manual-{self._sessions}", digest
        self.nodes, self.source, self.observed = list(nodes), nodes[0], set(nodes)
        self.frames = 12000
        self.receipt = {"session_id": self.session_id, "boot_epoch": self.boot,
                        "frame_count": self.frames, "variance_explained": 0.9,
                        "baseline_eigenvalue_count": 2,
                        "completed_at_unix_ms": int(self.completed_at * 1000)}

    def restored_bootstrap(self, age_s: float) -> None:
        """A promoted startup baseline restored after a server restart."""
        self._clear()
        self.model, self.completed_at = "final", self.now - age_s
        self.nodes, self.source = [1], 1
        self.bootstrap_active = self.bootstrap_stored = True

    def restart(self, boot: str = "boot-2") -> None:
        self._clear()
        self.boot = boot
        self.bootstrap_active = False

    def feed(self, dt: float) -> None:
        """Frames from the bound source node over `dt` seconds."""
        if self.model not in ("uncalibrated", "collecting") or not self.grid_fresh or self.source in self.faults:
            return
        self.frames += int(self.fps * dt)
        if self.frames:
            self.model = "collecting"
            self.elapsed += dt
            self.observed.add(self.source)

    # ── status ───────────────────────────────────────────────────────────

    def _model_status(self) -> str | None:
        if self.model in (None, "uncalibrated", "collecting"):
            return self.model
        age = self.now - self.completed_at
        return "fresh" if age < 12 * HOUR else "stale" if age < 24 * HOUR else "expired"

    def status(self) -> dict:
        model = self._model_status()
        bootstrap = self.bootstrap_active and model not in (None, "expired")
        bound = self.session_id is not None
        status = model or "none"
        if not (bootstrap or bound or status == "expired"):
            status = "none"
        return {
            "active": bootstrap or bound,
            "status": status,
            "frame_count": self.frames,
            "min_frames": self.MIN_FRAMES if self.model else 0,
            "elapsed_s": self.elapsed,
            "min_duration_s": self.MIN_DURATION_S if self.model else 0.0,
            "sequence_fault_node_ids": sorted(self.faults),
            "binding_mode": "bootstrap_only" if bootstrap else "bound" if bound else "none",
            "bootstrap_baseline": {"stored": self.bootstrap_stored, "active": bootstrap},
            "boot_epoch": self.boot,
            "session_id": self.session_id if bound else None,
            "binding_digest": self.digest if bound else None,
            "model_receipt": self.receipt if bound else None,
            "source_node_ids": self.nodes if bound else [],
        }

    # ── POST /api/v1/calibration/<name> ──────────────────────────────────

    def post(self, url: str, token: str, body, timeout: float):
        path = url.split("/api/v1/calibration/", 1)[1]
        name, _, query = path.partition("?")
        self.calls.append((name, body, query))
        if name in self.overrides:
            return self.overrides.pop(name)
        if not isinstance(body, dict) or ("boot_epoch" not in body and name != "start"):
            return 422, None, "HTTP 422"    # axum's Json rejection
        response = getattr(self, f"_{name}")(body, query)
        if name in self.lose_answer:
            return None, None, "timed out"
        return 200, response, None

    @staticmethod
    def _valid_ids(ids) -> bool:
        return (isinstance(ids, list) and 0 < len(ids) <= 16
                and all(isinstance(i, int) and 0 <= i <= 255 for i in ids)
                and all(a < b for a, b in zip(ids, ids[1:])))

    def _identity_ok(self, body: dict) -> bool:
        ids = body.get("source_node_ids")
        return (body.get("boot_epoch") == self.boot and body.get("session_id") == self.session_id
                and body.get("binding_digest") == self.digest
                and self._valid_ids(ids) and set(ids) == set(self.nodes))

    IDENTITY_ERROR = {"success": False, "error_code": "calibration_identity_mismatch",
                      "error": "The calibration request does not match the active boot, session, room binding, and node set."}

    def _start(self, body: dict, query: str) -> dict:
        digest, ids = body.get("binding_digest"), body.get("source_node_ids")
        if not (isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest)) or not self._valid_ids(ids):
            return {"success": False, "error_code": "calibration_binding_invalid"}
        q = dict(urllib.parse.parse_qsl(query))
        explicit = int(q["source_node_id"]) if "source_node_id" in q else None
        if explicit is not None and explicit in ids:
            if explicit not in self.known_nodes:
                return {"success": False, "error_code": "calibration_source_unavailable"}
            if not self.grid_fresh:
                return {"success": False, "error_code": "calibration_grid_unavailable"}
            source = explicit
        else:
            eligible = [n for n in ids if n in self.known_nodes] if self.grid_fresh else []
            if not eligible:
                return {"success": False, "error_code": "calibration_source_required"}
            source = eligible[0]
        if self.bootstrap_active:
            self.model, self.bootstrap_active = None, False
        model = self._model_status()
        if model == "collecting":
            return {"success": False, "frame_count": self.frames,
                    "error": "Calibration already in progress. Call /calibration/stop first."}
        if model == "fresh":
            return {"success": False,
                    "error": "A fresh calibration already exists. Call /calibration/stop or wait for expiry."}
        self._clear()
        self._sessions += 1
        self.model = "uncalibrated"
        self.session_id, self.digest = f"cal-session-{self._sessions}", digest
        self.nodes, self.source = list(ids), source
        return {"success": True, "status": "uncalibrated", "frame_count": 0, "boot_epoch": self.boot,
                "session_id": self.session_id, "binding_digest": digest, "source_node_ids": self.nodes}

    def _stop(self, body: dict, _query: str) -> dict:
        if not self._identity_ok(body):
            return self.IDENTITY_ERROR
        if self.faults:
            return {"success": False, "error_code": "calibration_sequence_discontinuity",
                    "sequence_fault_node_ids": sorted(self.faults)}
        missing = sorted(set(self.nodes) - self.observed)
        if missing:
            return {"success": False, "error_code": "calibration_source_nodes_missing",
                    "missing_source_node_ids": missing}
        if not self.grid_fresh:
            return {"success": False, "error_code": "calibration_grid_stale"}
        if self.model is None:
            return {"success": False, "error": "No field model active — call /calibration/start first."}
        if self.frames < self.MIN_FRAMES or self.elapsed < self.MIN_DURATION_S:
            return {"success": False, "error": "Calibration not complete yet", "frame_count": self.frames,
                    "frames_needed": self.MIN_FRAMES, "elapsed_s": self.elapsed,
                    "duration_needed_s": self.MIN_DURATION_S}
        self.model, self.completed_at = "final", self.now
        self.receipt = {"session_id": self.session_id, "boot_epoch": self.boot, "frame_count": self.frames,
                        "variance_explained": 0.93, "baseline_eigenvalue_count": 2,
                        "completed_at_unix_ms": int(self.now * 1000)}
        return {"success": True, "baseline_eigenvalue_count": 2, "variance_explained": 0.93,
                "frame_count": self.frames, "elapsed_s": self.elapsed, "model_receipt": self.receipt,
                "source_node_ids": self.nodes}

    def _cancel(self, body: dict, _query: str) -> dict:
        if not self._identity_ok(body):
            return self.IDENTITY_ERROR
        if self.model not in ("uncalibrated", "collecting"):
            return {"success": False, "error_code": "calibration_not_collecting"}
        cancelled = self.session_id
        self._clear()
        if self.bootstrap_stored:
            self.model, self.completed_at = "final", self.now - HOUR
            self.nodes, self.source, self.bootstrap_active = [1], 1, True
        return {"success": True, "status": "bootstrap" if self.bootstrap_active else "none",
                "boot_epoch": self.boot, "cancelled_session_id": cancelled}

    def _reset(self, body: dict, _query: str) -> dict:
        boot_only = (body.get("boot_epoch") == self.boot
                     and all(body.get(k) is None for k in ("session_id", "binding_digest", "source_node_ids")))
        if not boot_only and not self._identity_ok(body):
            return self.IDENTITY_ERROR
        self._clear()
        removed, self.bootstrap_stored, self.bootstrap_active = self.bootstrap_stored, False, False
        return {"success": True, "status": "none", "bootstrap_removed": removed}


def rest(now: float, calibration: dict | None, streaming: bool = True) -> RestSnapshot:
    nodes = [n for n in (1, 2, 3) if streaming or n != 1]
    return RestSnapshot(
        at=now,
        health={"status": "ok", "source": "esp32", "tick": 100},
        status={"source": "esp32", "source_state": "live_verified"},
        stream={"nodes": [{"node_id": n, "csi_fps": 22.0} for n in nodes]},
        nodes={"nodes": [{"node_id": n, "csi_status": "active", "csi_last_seen_ms": 30} for n in nodes]},
        calibration=calibration,
    )


def autocal_config(**over) -> Config:
    return Config(**{"ha_url": "http://ha:8123", "ha_token": "x", "ha_persons": ("person.sq", "person.ml"),
                     "autocal_source_node": 1, **over})


class World:
    """One watchdog with automatic calibration, its store, and the server,
    advanced in 30 s rounds like the real loop."""

    def __init__(self, t0: float = T0, quiet_history: bool = True, **cfg) -> None:
        self.cfg = autocal_config(**cfg)
        self.now = t0
        self.store = Store(self.cfg, started_at=t0 - 2 * HOUR)
        self.server = FakeServer(t0)
        self.ac = AutoCalibrator(self.cfg, post=self.server.post)
        self.persons: dict[str, PersonState] | None = {
            "person.sq": PersonState("not_home", t0 - HOUR),
            "person.ml": PersonState("work", t0 - 1800),
        }
        self.motion: str | None = "absent"
        self.streaming = True
        self.notes: list[dict] = []
        if quiet_history:
            self.history("absent")

    def history(self, level: str, seconds: int = 600) -> None:
        """The room's motion level over the last `seconds`, and from now on."""
        self.store.moving = Series(MOVING_WINDOW_S)
        for i in range(seconds):
            self.store.on_state({"motion_level": level}, self.now - seconds + i)
        self.motion = level

    def round(self, dt: float = 30.0) -> list[dict]:
        if self.motion is not None:
            for i in range(int(dt)):
                self.store.on_state({"motion_level": self.motion}, self.now + i + 1)
        self.now += dt
        self.server.now = self.now
        if self.streaming:
            self.server.feed(dt)
        self.store.persons = dict(self.persons) if self.persons is not None else None
        self.store.rest = rest(self.now, self.server.status(), streaming=self.streaming)
        notes = self.ac.step(self.ac.observe(self.store, self.now), self.now)
        self.notes += notes
        return notes

    def until_note(self, limit: int = 100) -> dict:
        for _ in range(limit):
            notes = self.round()
            if notes:
                (note,) = notes
                return note
        raise AssertionError("no annotation")

    def calls(self) -> list[str]:
        return [name for name, _, _ in self.server.calls]

    def status(self) -> dict:
        return self.ac.status(self.now)


# ── a full capture ───────────────────────────────────────────────────────

def test_capture_starts_when_everyone_is_away_and_finishes():
    w = World()
    (note,) = w.round()
    name, body, query = w.server.calls[0]
    assert (name, query) == ("start", "source_node_id=1")
    assert body == {"binding_digest": BINDING_DIGEST, "source_node_ids": [1]}
    assert re.fullmatch(r"[0-9a-f]{64}", BINDING_DIGEST)
    assert note == {"kind": "calibration", "label": "empty_room", "action": "start", "ts": int(w.now * 1000),
                    "person": "", "source": "watchdog", "value": 1, "source_node": 1,
                    "text": "自动空房标定开始（节点 1）"}
    assert w.status()["state"] == "collecting"

    w.round()
    assert w.status()["text"] == "采集中 0/10 分钟"
    assert w.status()["progress"] == {"frame_count": 600, "frames_needed": 1000,
                                      "elapsed_s": 30, "duration_needed_s": 600}
    done = w.until_note()
    assert w.calls() == ["start", "stop"]                 # stop only once the targets are met
    assert done["action"] == "done" and done["elapsed_s"] == 600 and done["frame_count"] == 12000
    assert done["variance_explained"] == 0.93 and done["baseline_eigenvalue_count"] == 2
    assert done["text"].startswith("自动空房标定完成：10 分钟，12000 帧")
    assert w.server.status()["status"] == "fresh"
    status = w.status()
    assert status["state"] == "ok" and status["last"]["result"] == "done" and status["calibration_age_h"] == 0.0
    assert status["text"] == "标定有效（0 秒前）" and status["waiting_for"] == []
    w.round()
    assert w.calls() == ["start", "stop"] and w.status()["state"] == "ok"


@pytest.mark.parametrize("setup, reason", [
    (lambda w: w.persons.update({"person.sq": PersonState("home", T0 - HOUR)}), "sq 在家"),
    (lambda w: w.persons.update({"person.ml": PersonState("not_home", T0 - 200)}), "离家才 5 分钟，要满 10 分钟"),
    (lambda w: w.persons.update({"person.ml": PersonState("unknown", None)}), "不知道手机在不在家"),
    (lambda w: setattr(w, "persons", None), "不知道手机在不在家"),
    (lambda w: w.history("present_moving"), "房间里有动静（100% 的时间在动）"),
    (lambda w: setattr(w, "streaming", False), "节点 1 没在正常发数据"),
])
def test_waits_for_an_empty_quiet_room(setup, reason, caplog):
    w = World()
    setup(w)
    with caplog.at_level(logging.INFO, logger="ruview-analytics.autocal"):
        for _ in range(4):
            assert w.round() == []
    assert w.calls() == []
    status = w.status()
    assert status["state"] == "waiting" and status["text"].startswith("需要标定，等待：")
    assert any(reason in why for why in status["waiting_for"]), status["waiting_for"]
    logged = [r.getMessage() for r in caplog.records]
    assert len(logged) == 1 and "calibration due, waiting" in logged[0] and "samples" in logged[0]


def test_waits_for_enough_state_history():
    w = World(quiet_history=False)
    w.round()
    assert w.calls() == [] and w.status()["waiting_for"] == ["CSI 状态数据不够"]
    for _ in range(9):
        w.round()
    assert w.calls() == ["start"]                        # 300 quiet samples after 5 min


def test_fresh_calibration_is_left_alone():
    w = World()
    w.server.finalized(age_s=2 * HOUR)
    w.round()
    assert w.calls() == []
    assert w.status()["text"] == "标定有效（2.0 小时前）" and w.status()["calibration_age_h"] == 2.01


# ── replacing a calibration ──────────────────────────────────────────────

def test_old_fresh_calibration_is_reset_and_renewed():
    w = World()
    w.server.finalized(age_s=6.5 * HOUR)
    (note,) = w.round()
    assert w.calls() == ["reset", "start"]
    assert w.server.calls[0][1] == {"boot_epoch": "boot-1"}   # the boot-only reset body
    assert note["text"] == "自动空房标定开始（节点 1）；先清除了 6.5 小时前的旧标定"
    assert w.until_note()["action"] == "done"


def test_refresh_after_12_hours_means_only_stale_ones():
    w = World(autocal_refresh_s=12 * HOUR)
    w.server.finalized(age_s=12 * HOUR - 45)
    w.round()
    assert w.calls() == [] and w.status()["state"] == "ok"
    w.server.completed_at += 30                           # the server's clock lags: still fresh
    w.round()
    assert w.server.status()["status"] == "fresh" and w.ac.calibration_age_s >= 12 * HOUR
    assert w.calls() == []                                 # 12 h: never a reset
    w.round()
    assert w.calls() == ["start"]                          # stale now: no reset needed


def test_sequence_fault_resets_a_fresh_calibration():
    w = World()
    w.server.finalized(age_s=HOUR, nodes=(1, 2))
    w.server.faults = {2}
    (note,) = w.round()
    assert w.calls() == ["reset", "start"]
    assert note["text"].endswith("；先清除了序号出错的旧标定")
    assert w.server.nodes == [1] and w.server.faults == set()


@pytest.mark.parametrize("status", ["stale", "expired", "none"])
def test_stale_expired_or_missing_calibration_starts_without_reset(status):
    w = World()
    if status != "none":
        w.server.finalized(age_s=(13 if status == "stale" else 25) * HOUR)
    assert w.server.status()["status"] == status
    w.round()
    assert w.calls() == ["start"]


def test_restored_bootstrap_is_replaced_without_deleting_it():
    w = World()
    w.server.restored_bootstrap(age_s=HOUR)
    assert w.server.status()["binding_mode"] == "bootstrap_only"
    w.round()
    assert w.calls() == ["start"]
    assert w.server.bootstrap_stored and w.server.status()["binding_mode"] == "bound"


def test_a_stored_bootstrap_is_never_reset():
    w = World()
    w.server.finalized(age_s=7 * HOUR)
    w.server.bootstrap_stored = True
    w.round()
    assert w.calls() == [] and w.status()["state"] == "ok"
    w.server.faults = {1}
    w.round()
    assert w.calls() == []
    assert w.status()["waiting_for"][0].startswith("重置会删掉服务器上保存的启动基线")
    w.server.completed_at -= 6 * HOUR                      # 13 h old: stale
    w.round()
    assert w.calls() == ["start"] and w.server.bootstrap_stored


# ── cancelling ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("event, reason", [
    (lambda w: w.persons.update({"person.sq": PersonState("home", w.now)}), "sq 回家了"),
    (lambda w: setattr(w, "motion", "present_moving"), "房间里有动静（最近 2 分钟"),
    (lambda w: setattr(w.server, "faults", {1}), "节点 1 的 CSI 序号出错"),
    (lambda w: setattr(w, "streaming", False) or setattr(w.server, "frames", 0), None),
])
def test_capture_is_cancelled(event, reason):
    w = World()
    w.round()
    if reason is None:                                     # no frames ever arrive
        w.streaming = False
        reason = "节点 1 一直没有数据进来"
    else:
        w.round()
        w.round()
        event(w)
    note = w.until_note()
    assert note["action"] == "cancelled" and note["text"].startswith(f"自动空房标定已取消：{reason}")
    assert w.calls() == ["start", "cancel"]
    assert w.server.calls[1][1] == {"boot_epoch": "boot-1", "session_id": "cal-session-1",
                                    "binding_digest": BINDING_DIGEST, "source_node_ids": [1]}
    assert w.server.status()["status"] == "none"
    status = w.status()
    assert status["state"] == "waiting" and status["last"]["result"] == "cancelled"
    assert status["waiting_for"] == ["上次被取消，30 分钟后再试"] and status["retry_at"]


def test_retries_30_minutes_after_a_cancelled_capture():
    w = World()
    w.round()
    w.motion = "present_moving"
    w.until_note()
    cancelled_at = w.now
    w.motion = "absent"
    for _ in range(59):
        w.round()
        assert w.calls() == ["start", "cancel"]
    assert any(why.startswith("上次被取消") for why in w.status()["waiting_for"])
    w.round()
    assert w.now - cancelled_at == 1800 and w.calls() == ["start", "cancel", "start"]


def test_capture_that_runs_too_long_is_cancelled():
    w = World()
    w.server.fps = 0.5                                      # 15 frames a round: never 1000
    w.round()
    note = w.until_note(limit=70)
    assert note["text"] == "自动空房标定已取消：30 分钟还没采完" and note["elapsed_s"] == 1830
    assert w.calls() == ["start", "cancel"]


# ── restarts and foreign captures ────────────────────────────────────────

def test_picks_its_own_capture_up_after_a_watchdog_restart():
    w = World()
    w.round()
    w.round()
    w.ac = AutoCalibrator(w.cfg, post=w.server.post)       # the watchdog restarted
    assert w.round() == []
    assert w.status()["state"] == "collecting" and w.ac.session.session_id == "cal-session-1"
    assert w.until_note()["action"] == "done"
    assert w.calls() == ["start", "stop"]


def test_leaves_a_manual_capture_alone():
    w = World()
    w.server.post("http://x/api/v1/calibration/start", "", {"binding_digest": OTHER_DIGEST,
                                                            "source_node_ids": [1, 2]}, 5)
    for _ in range(3):
        w.round()
    assert w.calls() == ["start"]                           # only the manual one
    assert w.status()["text"] == "需要标定，等待：有一个手动标定正在进行"


def test_server_restart_during_a_capture_fails_it_without_a_cancel():
    w = World()
    w.round()
    w.server.restart()
    note = w.until_note()
    assert note["action"] == "failed" and note["text"] == "自动空房标定失败：感知服务器重启了"
    assert w.calls() == ["start"]


def test_capture_replaced_elsewhere_fails():
    w = World()
    w.round()
    w.server.post("http://x/api/v1/calibration/reset", "", {"boot_epoch": "boot-1"}, 5)
    note = w.until_note()
    assert note["text"] == "自动空房标定失败：标定在别处被停止或替换了"
    assert w.calls() == ["start", "reset"]


# ── server answers ───────────────────────────────────────────────────────

@pytest.mark.parametrize("answer", [
    (200, {"success": False, "error_code": "calibration_grid_stale"}, None),
    (200, {"success": False, "error": "Calibration not complete yet", "frames_needed": 1000}, None),
    (None, None, "timed out"),
])
def test_stop_tries_again_when_the_server_says_later(answer):
    w = World()
    w.round()
    w.server.overrides["stop"] = answer
    assert w.until_note()["action"] == "done"
    assert w.calls() == ["start", "stop", "stop"]


def test_stop_whose_answer_is_lost_completes_from_the_status():
    w = World()
    w.round()
    w.server.lose_answer.add("stop")
    note = w.until_note()
    assert note["action"] == "done" and note["frame_count"] == 12000
    assert w.calls() == ["start", "stop"]


def test_stop_refused_fails_and_cancels():
    w = World()
    w.round()
    w.server.overrides["stop"] = (200, {"success": False, "error_code": "calibration_sequence_discontinuity"}, None)
    note = w.until_note()
    assert note["action"] == "failed"
    assert note["text"] == "自动空房标定失败：服务器拒绝：calibration_sequence_discontinuity"
    assert w.calls() == ["start", "stop", "cancel"]


def test_refused_start_waits_quietly():
    w = World()
    w.server.grid_fresh = False
    assert w.round() == []
    assert w.calls() == ["start"]
    assert w.status()["waiting_for"] == ["上次失败，30 分钟后再试"]
    assert w.status()["last"]["reason"] == "服务器拒绝：calibration_grid_unavailable"
    w.round()
    assert w.calls() == ["start"]


def test_refused_start_after_a_reset_is_announced():
    w = World()
    w.server.finalized(age_s=7 * HOUR)
    w.server.grid_fresh = False
    (note,) = w.round()
    assert w.calls() == ["reset", "start"]
    assert note["action"] == "failed"
    assert note["text"] == "旧标定已清除，但新的标定没能开始：服务器拒绝：calibration_grid_unavailable"


def test_unreachable_or_old_server_is_unknown():
    w = World()
    w.store.rest = RestSnapshot(at=w.now, error="timed out")
    w.ac.step(w.ac.observe(w.store, w.now), w.now)
    assert w.status()["text"] == "状态未知：感知服务器连不上"
    w.store.rest = rest(w.now, None)
    w.store.rest.auth_failed = True
    w.ac.step(w.ac.observe(w.store, w.now), w.now)
    assert w.status()["text"].startswith("状态未知：感知服务器拒绝了看门狗的 API token")
    w.store.rest = rest(w.now, {"status": "Stale"})          # pre-2026-10 server
    w.ac.step(w.ac.observe(w.store, w.now), w.now)
    assert w.status()["text"] == "状态未知：感知服务器版本太旧，不支持自动标定"
    assert w.calls() == []


def test_english_status():
    w = World(lang="en")
    w.persons["person.sq"] = PersonState("home", T0)
    w.round()
    assert w.status()["text"] == "due, waiting: sq at home"


# ── config, store, checks, metrics ───────────────────────────────────────

def test_autocal_config():
    env = {"HA_URL": "http://ha:8123", "HA_TOKEN": "x", "HA_PERSONS": "person.sq,person.ml"}
    cfg = Config.from_env({**env, "AUTOCAL_SOURCE_NODE": "2", "AUTOCAL_AWAY_MIN": "15",
                           "AUTOCAL_REFRESH_H": "12", "AUTOCAL_MAX_MOVING": "0.1"})
    assert cfg.autocal_enabled and cfg.autocal_source_node == 2
    assert (cfg.autocal_away_s, cfg.autocal_refresh_s, cfg.autocal_max_moving) == (900, 12 * HOUR, 0.1)
    off = Config.from_env(env)
    assert not off.autocal_enabled and (off.autocal_away_s, off.autocal_refresh_s) == (600, 6 * HOUR)
    for bad in ({"AUTOCAL_SOURCE_NODE": "4"}, {"AUTOCAL_SOURCE_NODE": "x"},
                {"AUTOCAL_SOURCE_NODE": "1", "AUTOCAL_REFRESH_H": "24"},
                {"AUTOCAL_SOURCE_NODE": "1", "AUTOCAL_AWAY_MIN": "0"},
                {"AUTOCAL_SOURCE_NODE": "1", "AUTOCAL_MAX_MOVING": "2"}):
        with pytest.raises(ConfigError):
            Config.from_env({**env, **bad})
    with pytest.raises(ConfigError, match="HA_PERSONS"):
        Config.from_env({"AUTOCAL_SOURCE_NODE": "1"})
    with pytest.raises(ValueError):
        AutoCalibrator(Config())


def test_store_tracks_moving_states():
    store = Store(Config(), started_at=T0)
    for i, level in enumerate(["absent", "present_still", "present_moving", "present_moving", "low", None]):
        store.on_state({"motion_level": level}, T0 + i)
    assert [v for _, v in store.moving.window(T0 + 6, 900)] == [0.0, 0.0, 1.0, 1.0]


def test_calibration_check_names_sequence_faults_and_auto_calibration():
    for cfg, hint in ((Config(), ""), (autocal_config(), "。家里没人时会自动重新标定")):
        store, wd = Store(cfg, started_at=T0), C.Watchdog(cfg)
        store.rest = rest(T0, {"status": "fresh", "binding_mode": "bound", "sequence_fault_node_ids": [2, 3]})
        report, _ = wd.evaluate(store, T0)
        entry = report["checks"]["calibration"]
        assert (entry["state"], entry["severity"]) == ("problem", "warning")
        assert entry["summary"] == "节点 2,3 的 CSI 序号倒退过（多半是重启了），这次标定已不能放行 CSI 心率呼吸" + hint
        assert entry["detail"]["sequence_fault_node_ids"] == [2, 3]
    store.rest = rest(T0 + 30, {"status": "none", "binding_mode": "none"})
    report, _ = wd.evaluate(store, T0 + 30)
    assert report["checks"]["calibration"]["summary"].endswith("CSI 心率呼吸不会发布。家里没人时会自动重新标定")


def test_report_metrics_include_autocal():
    w = World()
    w.round()
    report, _ = C.Watchdog(w.cfg).evaluate(w.store, w.now)
    report["autocal"] = w.status()
    m = C.metrics(report)
    assert m["autocal_collecting"] == 1 and "calibration_age_h" not in m
    w.until_note()
    report["autocal"] = w.status()
    m = C.metrics(report)
    assert m["autocal_collecting"] == 0 and m["calibration_age_h"] == 0.0


# ── Home Assistant ───────────────────────────────────────────────────────

def test_discovery_has_auto_calibration_and_expiry():
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment()
    env.filters["tojson"] = lambda v: json.dumps(v)
    cfg = autocal_config()
    msgs = discovery_messages(cfg, C.default_checks(cfg))
    configs = {json.loads(p)["unique_id"]: json.loads(p) for _, p in msgs}
    entity = configs["ruview_watchdog_auto_calibration"]
    tpl = env.from_string(entity["value_template"])
    assert tpl.render(value_json={"autocal": {"text": "采集中 3/10 分钟"}}) == "采集中 3/10 分钟"
    assert tpl.render(value_json={"status": "ok"}) == "None"   # report from before autocal
    attrs = env.from_string(entity["json_attributes_template"])
    assert json.loads(attrs.render(value_json={"autocal": {"state": "ok"}})) == {"state": "ok"}
    assert json.loads(attrs.render(value_json={})) == {}
    for unique_id, body in configs.items():
        if unique_id in ("ruview_watchdog_last_check", "ruview_watchdog_nas_link"):
            assert "expire_after" not in body
        else:
            assert body["expire_after"] == 300, unique_id
    slow = autocal_config(check_interval_s=120)
    assert all(json.loads(p).get("expire_after") in (None, 1200)
               for _, p in discovery_messages(slow, C.default_checks(slow)))


def test_discovery_removes_auto_calibration_when_off():
    cfg = Config()
    removed = [t for t, p in discovery_messages(cfg, C.default_checks(cfg)) if p == ""]
    assert removed == ["homeassistant/sensor/ruview_watchdog/auto_calibration/config"]


# ── HTTP and the service loop ────────────────────────────────────────────

def test_http_post_json_sends_json_with_the_token():
    received: dict = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            size = int(self.headers["Content-Length"])
            received.update(path=self.path, auth=self.headers["Authorization"],
                            ctype=self.headers["Content-Type"], body=json.loads(self.rfile.read(size)))
            out = json.dumps({"success": True}).encode()
            self.send_response(403 if self.path.endswith("/denied") else 200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{server.server_port}/api/v1/calibration"
        assert http_post_json(f"{base}/start?source_node_id=1", "tok", {"a": [1]}, 5) == (200, {"success": True}, None)
        assert received == {"path": "/api/v1/calibration/start?source_node_id=1", "auth": "Bearer tok",
                            "ctype": "application/json", "body": {"a": [1]}}
        assert http_post_json(f"{base}/denied", "tok", {}, 5) == (403, None, "HTTP 403")
    finally:
        server.shutdown()
        server.server_close()


class FakeClient:
    def __init__(self) -> None:
        self.published: list[tuple[str, str, int, bool]] = []

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, payload, qos, retain))


def test_run_round_publishes_the_annotation_and_status(monkeypatch):
    pytest.importorskip("paho.mqtt")
    from ruview_analytics import main

    w = World(t0=time.time())
    w.store.persons = w.persons
    monkeypatch.setattr(main, "poll_sensing_server", lambda cfg, now: rest(now, w.server.status()))
    monkeypatch.setattr(main, "poll_ha_persons", lambda cfg: (dict(w.persons), None))
    client = FakeClient()
    report = main.run_round(w.cfg, w.store, threading.Lock(), C.Watchdog(w.cfg), client, w.ac)
    by_topic = {topic: (json.loads(payload), qos, retain) for topic, payload, qos, retain in client.published}
    note, qos, retain = by_topic["ruview/annotation"]
    assert note["action"] == "start" and (qos, retain) == (1, False)
    assert report["autocal"]["state"] == "collecting"
    assert by_topic["ruview/watchdog/state"][0]["autocal"]["state"] == "collecting"
    assert by_topic["ruview/watchdog/metrics"][0]["autocal_collecting"] == 1


def test_run_round_reports_even_when_autocal_breaks(monkeypatch):
    pytest.importorskip("paho.mqtt")
    from ruview_analytics import main

    w = World(t0=time.time())

    def broken(*_args):
        raise RuntimeError("bug")

    w.ac = AutoCalibrator(w.cfg, post=broken)
    monkeypatch.setattr(main, "poll_sensing_server", lambda cfg, now: rest(now, w.server.status()))
    monkeypatch.setattr(main, "poll_ha_persons", lambda cfg: (dict(w.persons), None))
    client = FakeClient()
    report = main.run_round(w.cfg, w.store, threading.Lock(), C.Watchdog(w.cfg), client, w.ac)
    assert "autocal" not in report
    assert [topic for topic, *_ in client.published] == ["ruview/watchdog/state", "ruview/watchdog/metrics"]
