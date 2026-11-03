"""Watchdog rules, hysteresis, report and HA discovery (no broker needed).

Run from docker/analytics:  python -m pytest -q
"""

from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ruview_analytics import checks as C  # noqa: E402
from ruview_analytics.config import Config, ConfigError  # noqa: E402
from ruview_analytics.ha import discovery_messages  # noqa: E402
from ruview_analytics.rest import poll_ha_persons, poll_sensing_server  # noqa: E402
from ruview_analytics.store import PersonState, RestSnapshot, Store  # noqa: E402

T0 = 1_792_000_000.0


def make(cfg: Config | None = None, **cfg_kwargs) -> tuple[Config, Store, C.Watchdog]:
    cfg = cfg or Config(**cfg_kwargs)
    store = Store(cfg, started_at=T0)
    return cfg, store, C.Watchdog(cfg)


def healthy_rest(now: float, nodes=(1, 2, 3), fps=22.0, cal="fresh", **over) -> RestSnapshot:
    snap = RestSnapshot(
        at=now,
        health={"status": "ok", "source": "esp32", "tick": 100},
        status={"source": "esp32", "source_state": "live_unverified"},
        stream={"nodes": [{"node_id": n, "csi_fps": fps} for n in nodes]},
        nodes={"nodes": [{"node_id": n, "csi_status": "active", "csi_last_seen_ms": 30} for n in nodes]},
        calibration={"status": cal, "binding_mode": "bound"},
    )
    for key, value in over.items():
        setattr(snap, key, value)
    return snap


def bridge_alive(store: Store, now: float, **status) -> None:
    store.bridge_avail = "online"
    store.bridge_status = {"ts": now, "ws_connected": True, "source": "esp32",
                           "stale_source": False, "last_new_tick_age_s": 0.1, **status}


def run(wd: C.Watchdog, store: Store, now: float):
    report, transitions = wd.evaluate(store, now)
    return report, dict(transitions)


# ── config ───────────────────────────────────────────────────────────────

def test_config_from_env_validates_inputs():
    cfg = Config.from_env({"EXPECTED_NODES": "3, 1,1", "HA_PERSONS": "person.alice,person.bob",
                           "HA_URL": "http://192.168.1.227:8123/", "HA_TOKEN": "x"})
    assert cfg.expected_nodes == (1, 3)
    assert cfg.ha_url == "http://192.168.1.227:8123"
    assert cfg.presence_enabled
    with pytest.raises(ConfigError):
        Config.from_env({"RUVIEW_HTTP": "file:///etc/passwd"})
    with pytest.raises(ConfigError):
        Config.from_env({"EXPECTED_NODES": "1,abc"})
    with pytest.raises(ConfigError):
        Config.from_env({"HA_PERSONS": "person.x/../../api"})
    with pytest.raises(ConfigError):
        Config.from_env({"WATCHDOG_LANG": "fr"})


# ── healthy baseline ─────────────────────────────────────────────────────

def test_healthy_system_reports_ok():
    cfg, store, wd = make()
    now = T0 + 120
    store.rest = healthy_rest(now)
    bridge_alive(store, now)
    report, transitions = run(wd, store, now)
    assert report["status"] == "ok" and report["issues"] == [] and transitions == {}
    assert report["checks"]["node_2"]["state"] == "ok"
    assert report["checks"]["vitals_frozen"]["state"] == "unknown"  # no vitals yet
    assert report["top_issue"] == "一切正常"
    assert "presence" not in report["checks"]  # HA access not configured


# ── hysteresis ───────────────────────────────────────────────────────────

def test_server_down_raises_after_delay_and_clears():
    cfg, store, wd = make()
    t = T0 + 100
    bridge_alive(store, t, ws_connected=False)
    store.rest = RestSnapshot(at=t, error="connection refused")
    _, tr = run(wd, store, t)
    assert tr == {}  # raise_after_s = 90
    bridge_alive(store, t + 60, ws_connected=False)
    store.rest = RestSnapshot(at=t + 60, error="connection refused")
    _, tr = run(wd, store, t + 60)
    assert tr == {}
    bridge_alive(store, t + 95, ws_connected=False)
    store.rest = RestSnapshot(at=t + 95, error="connection refused")
    report, tr = run(wd, store, t + 95)
    assert tr == {"server": "raised"}
    assert report["status"] == "critical"
    assert report["checks"]["server"]["since_ts"] == t  # problem start, not raise time
    assert "connection refused" in report["top_issue"]
    store.rest = healthy_rest(t + 125)
    bridge_alive(store, t + 125)
    _, tr = run(wd, store, t + 125)
    assert tr == {"server": "cleared"}


def test_unknown_result_holds_raised_state():
    cfg, store, wd = make()
    t = T0 + 100
    for dt in (0, 100):
        store.rest = RestSnapshot(at=t + dt, error="timeout")
        run(wd, store, t + dt)
    assert wd.states["server"].raised
    store.rest = None  # no poll result at all
    report, tr = run(wd, store, t + 130)
    assert tr == {} and report["checks"]["server"]["state"] == "problem"


def test_prior_since_survives_restart():
    cfg, store, wd = make()
    store.prior_report = {"checks": {"server": {"state": "problem", "since_ts": T0 - 3600}}}
    t = T0 + 100
    for dt in (0, 100):
        store.rest = RestSnapshot(at=t + dt, error="timeout")
        report, _ = run(wd, store, t + dt)
    assert report["checks"]["server"]["since_ts"] == T0 - 3600


# ── bridge / source ──────────────────────────────────────────────────────

def test_bridge_offline_and_silent():
    cfg, store, wd = make()
    now = T0 + 10
    store.rest = healthy_rest(now)
    report, _ = run(wd, store, now)
    assert report["checks"]["bridge"]["state"] == "unknown"  # startup grace
    later = T0 + 200
    store.rest = healthy_rest(later)
    run(wd, store, later)
    report, tr = run(wd, store, later + 61)
    assert tr.get("bridge") == "raised"
    assert "一直没收到" in report["checks"]["bridge"]["summary"]

    cfg, store, wd = make()
    store.bridge_avail = "offline"
    store.rest = healthy_rest(later)
    run(wd, store, later)
    report, tr = run(wd, store, later + 60)
    assert report["checks"]["bridge"]["summary"] == "数据桥（mqtt-bridge）已离线"


def test_source_offline_from_server_suppresses_node_checks():
    cfg, store, wd = make()
    t = T0 + 100
    for dt in (0, 70, 140):
        now = t + dt
        store.rest = healthy_rest(now, nodes=(), health={"status": "ok", "source": "esp32:offline"})
        bridge_alive(store, now, stale_source=True, source="esp32:offline")
        report, tr = run(wd, store, now)
    assert report["checks"]["source"]["state"] == "problem"
    assert "esp32:offline" in report["checks"]["source"]["summary"]
    for nid in (1, 2, 3):
        assert report["checks"][f"node_{nid}"]["state"] == "unknown"
    assert report["open_issues"] == 1


def test_stalled_ticks_count_as_source_offline():
    cfg, store, wd = make()
    t = T0 + 100
    for dt in (0, 61):
        now = t + dt
        store.rest = healthy_rest(now)
        bridge_alive(store, now, last_new_tick_age_s=300.0)
        report, tr = run(wd, store, now)
    assert tr == {"source": "raised"}
    assert "5 分钟" in report["checks"]["source"]["summary"]


# ── nodes ────────────────────────────────────────────────────────────────

def test_node_down_and_slow_via_stream_status():
    cfg, store, wd = make()
    t = T0 + 100
    for dt in (0, 60, 121):
        now = t + dt
        snap = healthy_rest(now, nodes=(1, 3))
        snap.stream["nodes"][1]["csi_fps"] = 2.0  # node 3 slow
        snap.nodes["nodes"].append({"node_id": 2, "csi_status": "stale", "csi_last_seen_ms": 2_592_000_000})
        store.rest = snap
        bridge_alive(store, now)
        report, tr = run(wd, store, now)
    assert tr == {"node_2": "raised", "node_3": "raised"}
    assert report["checks"]["node_2"]["summary"] == "节点 2 掉线（已 30.0 天）"
    assert report["checks"]["node_3"]["summary"].startswith("节点 3 帧率只有 2.0 fps")
    assert report["checks"]["node_1"]["state"] == "ok"
    assert report["status"] == "warning"


def test_node_falls_back_to_csi_status_on_old_servers():
    cfg, store, wd = make(expected_nodes=(1, 2))
    now = T0 + 100
    snap = healthy_rest(now, nodes=(1, 2), stream={"active": True, "fps": 10})  # no per-node list
    snap.nodes["nodes"][1]["csi_status"] = "stale"
    store.rest = snap
    bridge_alive(store, now)
    report, _ = run(wd, store, now)
    assert wd.states["node_2"].pending_since == now
    assert report["checks"]["node_1"]["state"] == "ok"


def test_node_falls_back_to_mqtt_without_rest():
    cfg, store, wd = make(expected_nodes=(1, 2))
    now = T0 + 300
    store.on_message("ruview/nodes/1", json.dumps({"node_id": 1, "stale": False, "csi_fps": 21.0}).encode(), now - 1)
    store.on_message("ruview/nodes/2", json.dumps({"node_id": 2, "stale": True, "csi_fps": 0.0,
                                                   "last_seen_ms": 400_000}).encode(), now - 1)
    bridge_alive(store, now)
    run(wd, store, now)
    assert wd.states["node_1"].latest.active is False
    assert wd.states["node_2"].latest.active is True
    assert wd.states["node_2"].latest.detail["via"] == "mqtt"


# ── vitals ───────────────────────────────────────────────────────────────

def feed_vitals(store: Store, start: float, seconds: int, hr, br) -> None:
    for i in range(seconds):
        store.on_state({"heart_rate_bpm": hr(i), "breathing_rate_bpm": br(i), "presence": True}, start + i)


def test_frozen_vitals_like_september_incident():
    cfg, store, wd = make()
    start = T0
    feed_vitals(store, start, 900, hr=lambda i: 83.37, br=lambda i: 19.28)
    now = start + 900
    report, tr = run(wd, store, now)
    assert tr.get("vitals_frozen") == "raised"
    summary = report["checks"]["vitals_frozen"]["summary"]
    assert "心率过去 15 分钟只有 1 个不同的值（83.37）" in summary
    assert "呼吸率" in summary


def test_varying_vitals_are_not_frozen_and_zero_is_missing():
    cfg, store, wd = make()
    feed_vitals(store, T0, 900, hr=lambda i: 60 + (i % 17) * 0.37, br=lambda i: 0.0)
    report, _ = run(wd, store, T0 + 900)
    assert report["checks"]["vitals_frozen"]["state"] == "ok"
    assert len(store.br) == 0  # 0.0 from the old bridge means "missing"


def test_short_vitals_window_is_unknown():
    cfg, store, wd = make()
    feed_vitals(store, T0, 200, hr=lambda i: 70.0, br=lambda i: 15.0)  # 200 samples, 200 s span
    report, _ = run(wd, store, T0 + 200)
    assert report["checks"]["vitals_frozen"]["state"] == "unknown"


def test_vitals_out_of_range():
    cfg, store, wd = make()
    feed_vitals(store, T0, 100, hr=lambda i: 200.0 if i % 5 == 0 else 70.0 + i % 3,
                br=lambda i: 14.0 + (i % 4) * 0.5)
    report, tr = run(wd, store, T0 + 100)
    assert tr.get("vitals_range") == "raised"
    assert report["checks"]["vitals_range"]["summary"] == "心率有 20% 的读数超出 35–180"


# ── calibration ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("status,mode,state,severity", [
    ("fresh", "bound", "ok", None),
    ("Fresh", None, "ok", None),            # pre-2026-10 server
    ("collecting", "bound", "ok", None),
    ("stale", "bound", "problem", "info"),
    ("expired", "none", "problem", "warning"),
    ("none", "none", "problem", "warning"),
    ("fresh", "bootstrap_only", "problem", "warning"),
])
def test_calibration_states(status, mode, state, severity):
    cfg, store, wd = make()
    now = T0 + 100
    store.rest = healthy_rest(now, calibration={"status": status, "binding_mode": mode})
    bridge_alive(store, now)
    report, _ = run(wd, store, now)
    entry = report["checks"]["calibration"]
    assert (entry["state"], entry["severity"]) == (state, severity)
    if severity == "info":
        assert report["status"] == "info" and report["open_issues"] == 0


# ── presence vs phones ───────────────────────────────────────────────────

def presence_setup(person_states, occupied=True):
    cfg, store, wd = make(ha_url="http://ha:8123", ha_token="x", ha_persons=("person.sq", "person.ml"))
    now = T0 + 7200
    store.persons = person_states
    for i in range(1800):
        store.on_state({"calibrated": True, "cal_person_count": 1 if occupied else 0}, now - 1800 + i)
    store.rest = healthy_rest(now)
    bridge_alive(store, now)
    return cfg, store, wd, now


def test_presence_contradiction_when_everyone_is_away():
    away = {"person.sq": PersonState("not_home", T0), "person.ml": PersonState("work", T0 + 1000)}
    cfg, store, wd, now = presence_setup(away)
    report, tr = run(wd, store, now)
    assert tr.get("presence") == "raised"
    assert report["checks"]["presence"]["summary"].startswith("所有手机都离家 103 分钟，但 CSI 过去 30 分钟有 100% 的时间判断有人")


def test_presence_ok_when_someone_home_or_room_empty():
    home = {"person.sq": PersonState("home", T0), "person.ml": PersonState("not_home", T0)}
    cfg, store, wd, now = presence_setup(home)
    report, _ = run(wd, store, now)
    assert report["checks"]["presence"]["state"] == "ok"
    away = {"person.sq": PersonState("not_home", T0), "person.ml": PersonState("not_home", T0)}
    cfg, store, wd, now = presence_setup(away, occupied=False)
    report, _ = run(wd, store, now)
    assert report["checks"]["presence"]["state"] == "ok"
    unknown = {"person.sq": PersonState("unknown", None), "person.ml": PersonState("not_home", T0)}
    cfg, store, wd, now = presence_setup(unknown)
    report, _ = run(wd, store, now)
    assert report["checks"]["presence"]["state"] == "unknown"


# ── report / vitals status / metrics ─────────────────────────────────────

def test_report_sorting_metrics_and_vitals_status():
    cfg, store, wd = make(lang="en")
    t = T0 + 100
    store.on_state({"heart_rate_bpm": None, "vitals_present": False,
                    "abstention_reason": "exactly_one_occupant_required"}, t)
    for dt in (0, 100):
        snap = healthy_rest(t + dt, nodes=(1, 2), calibration={"status": "stale", "binding_mode": "bound"})
        snap.nodes["nodes"].append({"node_id": 3, "csi_status": "stale"})
        store.rest = snap
        bridge_alive(store, t + dt)
        report, _ = run(wd, store, t + dt)
    assert [i["id"] for i in report["issues"]] == ["calibration"]  # node_3 waits 120 s
    report, _ = run(wd, store, t + 125)
    assert [i["id"] for i in report["issues"]] == ["node_3", "calibration"]
    assert report["status"] == "warning" and report["open_issues"] == 1
    assert report["top_issue"] == "Node 3 is down"
    assert report["vitals"]["state"] == "unknown"  # last state is too old now
    m = C.metrics(report)
    assert m["status_level"] == 2 and m["warning"] == 1 and m["info"] == 1
    assert m["check_node_3"] == 1 and m["check_node_1"] == 0
    assert "check_vitals_frozen" not in m  # unknown checks are left out
    assert report["ha_link"] is None and "ha_link" not in m  # no NAS bus

    store.on_state({"vitals_present": False, "abstention_reason": "exactly_one_occupant_required"}, t + 125)
    status = C.vitals_status(cfg, store, t + 126)
    assert status == {"state": "withheld", "text": "withheld: not exactly one person",
                      "reason": "exactly_one_occupant_required"}


def test_broken_rule_does_not_stop_other_checks():
    cfg = Config()

    def boom(_ctx):
        raise RuntimeError("bug")

    checks = [C.Check("boom", "Boom", boom), *C.default_checks(cfg)]
    store = Store(cfg, started_at=T0)
    wd = C.Watchdog(cfg, checks)
    store.rest = healthy_rest(T0 + 100)
    report, _ = wd.evaluate(store, T0 + 100)
    assert report["checks"]["boom"]["state"] == "unknown"
    assert report["checks"]["server"]["state"] == "ok"


# ── raw CSI recorder ─────────────────────────────────────────────────────

def recorder_status(now: float, **over) -> dict:
    return {"ts": now, "recording": True, "datagrams_hz": 70.0, "last_datagram_age_s": 0.1,
            "ring_hours": 12.5, "disk_free_gb": 250.0, "disk_low": False, "keep_pending": 0,
            "kernel_drops": 0, "last_error": None, **over}


def recorder_round(store: Store, wd: C.Watchdog, now: float, **status):
    store.rest = healthy_rest(now)
    bridge_alive(store, now)
    store.on_message("ruview/recorder/status", json.dumps(recorder_status(now, **status)).encode(), now)
    return run(wd, store, now)


def test_recorder_check_is_unknown_until_a_recorder_appears():
    cfg, store, wd = make()
    report, _ = run(wd, store, T0 + 100)
    assert report["checks"]["recorder"]["state"] == "unknown"
    store.on_message("ruview/recorder/availability", b"online", T0 + 100)
    report, _ = recorder_round(store, wd, T0 + 110)
    assert report["checks"]["recorder"]["state"] == "ok"
    assert report["checks"]["recorder"]["detail"]["ring_hours"] == 12.5


@pytest.mark.parametrize("status, text", [
    ({"disk_low": True, "disk_free_gb": 1.4}, "1.4 GB"),
    ({"recording": False, "last_error": "write failed: [Errno 5] I/O error"}, "Errno 5"),
    ({"datagrams_hz": 0.0, "last_datagram_age_s": 300.0}, "RUVIEW_UDP_TEE"),
    ({"datagrams_hz": 0.0, "last_datagram_age_s": None}, "一个数据包都没收到"),
])
def test_recorder_problems_raise_after_delay(status, text):
    cfg, store, wd = make()
    store.on_message("ruview/recorder/availability", b"online", T0)
    for dt in (100, 160):
        report, transitions = recorder_round(store, wd, T0 + dt, **status)
        assert report["checks"]["recorder"]["state"] == "ok" and transitions == {}
    report, transitions = recorder_round(store, wd, T0 + 225, **status)
    assert transitions == {"recorder": "raised"}
    assert text in report["checks"]["recorder"]["summary"]


def test_recorder_quiet_is_fine_when_nodes_are_quiet_too():
    cfg, store, wd = make()
    store.on_message("ruview/recorder/availability", b"online", T0)
    for dt in (100, 200, 300):
        store.rest = healthy_rest(T0 + dt, nodes=())
        bridge_alive(store, T0 + dt)
        store.recorder_status = recorder_status(T0 + dt, datagrams_hz=0.0, last_datagram_age_s=500.0)
        report, _ = run(wd, store, T0 + dt)
    assert report["checks"]["recorder"]["state"] == "ok"


def test_recorder_offline_and_silent():
    cfg, store, wd = make()
    store.on_message("ruview/recorder/availability", b"offline", T0)
    for dt in (100, 230):
        report, _ = run(wd, store, T0 + dt)
    assert report["checks"]["recorder"]["state"] == "problem"
    assert "离线" in report["checks"]["recorder"]["summary"]
    cfg, store, wd = make(lang="en")
    store.on_message("ruview/recorder/availability", b"online", T0)
    store.recorder_status = recorder_status(T0)
    for dt in (100, 230):
        report, _ = run(wd, store, T0 + dt)
    assert report["checks"]["recorder"]["summary"].startswith("No status from the raw CSI recorder")


# ── offsite backup ───────────────────────────────────────────────────────

def backup_status(**over) -> bytes:
    status = {"since_ts": T0 - 10 * 86400, "last_ok_ts": T0 - 3600, "ok": True, "step": None,
              "error": None, "snapshot": "0123abcd", "next_run_ts": T0 + 20 * 3600, **over}
    return json.dumps(status).encode()


def test_backup_check_is_unknown_until_the_service_publishes():
    cfg, store, wd = make()
    report, _ = run(wd, store, T0)
    assert report["checks"]["backup"]["state"] == "unknown"
    store.on_message("ruview/backup/state", backup_status(), T0)
    report, _ = run(wd, store, T0)
    assert report["checks"]["backup"]["state"] == "ok"
    assert report["checks"]["backup"]["detail"]["snapshot"] == "0123abcd"
    # One failed night is not a problem yet: the service retries.
    store.on_message("ruview/backup/state", backup_status(ok=False, step="upload", error="timeout"), T0)
    report, _ = run(wd, store, T0 + 1)
    assert report["checks"]["backup"]["state"] == "ok"


def test_backup_too_old_raises_and_clears():
    cfg, store, wd = make()
    store.on_message("ruview/backup/state", backup_status(
        last_ok_ts=T0 - 49 * 3600, ok=False, step="upload", error="Fatal: unable to open repository"), T0)
    report, transitions = run(wd, store, T0)
    assert transitions == {"backup": "raised"}
    assert report["checks"]["backup"]["severity"] == "warning"
    assert report["checks"]["backup"]["summary"] == (
        "距上次成功的异地备份已有 2.0 天（最近一次在 upload 这一步出错：Fatal: unable to open repository）")
    store.on_message("ruview/backup/state", backup_status(last_ok_ts=T0 + 50), T0 + 60)
    _, transitions = run(wd, store, T0 + 60)
    assert transitions == {"backup": "cleared"}


def test_backup_that_never_succeeded():
    cfg, store, wd = make(lang="en", backup_max_age_s=24 * 3600)
    store.on_message("ruview/backup/state", backup_status(
        since_ts=T0 - 3600, last_ok_ts=None, ok=False, step="repository", error="no restic repository"), T0)
    report, _ = run(wd, store, T0)
    assert report["checks"]["backup"]["state"] == "ok"  # started an hour ago
    report, _ = run(wd, store, T0 + 24 * 3600)
    assert report["checks"]["backup"]["summary"] == (
        "The offsite backup service has run for 25.0 h without a single success"
        " (the last run failed at repository: no restic repository)")
    assert Config.from_env({"BACKUP_MAX_AGE_H": "72"}).backup_max_age_s == 72 * 3600
    with pytest.raises(ConfigError):
        Config.from_env({"BACKUP_MAX_AGE_H": "0"})


def test_keep_requests_for_new_vitals_problems_only_with_a_recorder():
    cfg, store, wd = make()
    report = {"ts": T0, "checks": {"vitals_frozen": {"since_ts": T0 - 30, "summary": "心率冻结"},
                                   "node_1": {"since_ts": T0, "summary": "x"}}}
    transitions = [("vitals_frozen", "raised"), ("node_1", "raised")]
    assert C.keep_requests(cfg, store, report, transitions) == []      # no recorder seen
    store.recorder_avail = "online"
    (req,) = C.keep_requests(cfg, store, report, transitions)
    assert req == {"id": f"wd-vitals_frozen-{int(T0 - 30)}", "tag": "watchdog_vitals_frozen",
                   "last_s": 1200.0, "source": "watchdog", "note": "心率冻结"}
    assert C.keep_requests(cfg, store, report, [("vitals_frozen", "cleared")]) == []
    off = Config.from_env({"KEEP_ON_RAISE_MIN": "0"})
    assert C.keep_requests(off, store, report, transitions) == []


# ── REST polling ─────────────────────────────────────────────────────────

def fake_get(routes):
    def get(url, token, timeout):
        for suffix, response in routes.items():
            if url.endswith(suffix):
                return response
        return 404, None, "HTTP 404"
    return get


def test_poll_handles_auth_failure_and_missing_endpoints():
    cfg = Config(api_token="t")
    snap = poll_sensing_server(cfg, T0, fake_get({
        "/health": (200, {"source": "esp32"}, None),
        "/api/v1/status": (401, None, "HTTP 401"),
    }))
    assert snap.health == {"source": "esp32"} and snap.auth_failed and snap.status is None
    snap = poll_sensing_server(cfg, T0, fake_get({
        "/health": (200, {"source": "esp32"}, None),
        "/api/v1/status": (200, {"source_state": "live_verified"}, None),
    }))
    assert not snap.auth_failed and snap.status == {"source_state": "live_verified"} and snap.stream is None
    snap = poll_sensing_server(cfg, T0, fake_get({"/health": (None, None, "timed out")}))
    assert snap.error == "timed out"
    snap = poll_sensing_server(Config(ruview_http="http://192.0.2.10:3000"), T0,
                               fake_get({"/health": (421, None, "HTTP 421")}))
    assert snap.error == "HTTP 421: 192.0.2.10 is not in the server's SENSING_ALLOWED_HOSTS"


def test_poll_ha_persons():
    cfg = Config(ha_url="http://ha:8123", ha_token="x", ha_persons=("person.sq",))
    persons, error = poll_ha_persons(cfg, fake_get({
        "/api/states/person.sq": (200, {"state": "home", "last_changed": "2026-10-15T03:00:00+00:00"}, None),
    }))
    assert error is None and persons["person.sq"].state == "home"
    assert persons["person.sq"].since == pytest.approx(1792033200.0)
    persons, error = poll_ha_persons(cfg, fake_get({}))
    assert persons is None and "person.sq" in error


def test_store_ignores_oversized_and_non_object_payloads():
    cfg, store, _ = make()
    store.on_message("ruview/state", b"[1,2]", T0)
    store.on_message("ruview/state", b"{" + b" " * 70_000 + b"}", T0)
    assert store.last_state is None
    store.on_message("ruview/nodes/x", b"{}", T0)
    assert store.nodes_mqtt == {}


def test_ha_link_from_the_bus_bridge():
    cfg, store, wd = make()
    store.rest = healthy_rest(T0)
    bridge_alive(store, T0)
    store.on_message("ruview/bus/ha_connected", b"1", T0)
    report, _ = run(wd, store, T0)
    assert report["ha_link"] is True and C.metrics(report)["ha_link"] == 1
    store.on_message("ruview/bus/ha_connected", b"0", T0 + 1)
    report, _ = run(wd, store, T0 + 1)
    assert report["ha_link"] is False and C.metrics(report)["ha_link"] == 0
    # A cleared retained topic (empty payload) means no bus bridge any more.
    store.on_message("ruview/bus/ha_connected", b"", T0 + 2)
    assert store.ha_link is None


# ── Home Assistant discovery ─────────────────────────────────────────────

def test_discovery_templates_render():
    jinja2 = pytest.importorskip("jinja2")
    env = jinja2.Environment()
    env.filters["tojson"] = lambda v: json.dumps(v)
    cfg, store, wd = make()
    # An empty payload removes an entity (Auto Calibration, off here).
    configs = {json.loads(p)["unique_id"]: json.loads(p) for _, p in discovery_messages(cfg, wd.checks) if p}
    assert len(configs) == 7 + len(wd.checks)
    link = configs["ruview_watchdog_nas_link"]
    assert link["state_topic"] == "ruview/bus/ha_connected"
    assert (link["payload_on"], link["payload_off"]) == ("1", "0")
    assert link["device_class"] == "connectivity"
    # Must not go unavailable with the watchdog: it matters most when the
    # watchdog's own messages cannot reach Home Assistant.
    assert "availability_topic" not in link and "availability" not in link
    node2 = configs["ruview_watchdog_check_node_2"]
    assert node2["name"] == "Node 2" and node2["device_class"] == "problem"
    tpl = env.from_string(node2["value_template"])
    value = {"checks": {"node_2": {"state": "problem"}}}
    assert tpl.render(value_json=value) == "ON"
    assert tpl.render(value_json={"checks": {"node_2": {"state": "ok"}}}) == "OFF"
    assert tpl.render(value_json={"checks": {"node_2": {"state": "unknown"}}}) == "None"
    attrs = env.from_string(node2["json_attributes_template"])
    assert json.loads(attrs.render(value_json=value)) == {"state": "problem"}
    problem = env.from_string(configs["ruview_watchdog_problem"]["value_template"])
    assert problem.render(value_json={"status": "info"}) == "OFF"
    assert problem.render(value_json={"status": "critical"}) == "ON"
