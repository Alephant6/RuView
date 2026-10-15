"""Watchdog rules and the hysteresis engine.

Each rule looks at the current observations and returns a Result:
active=True (problem), False (fine) or None (not enough data to tell).
The engine turns those into raised/cleared checks with per-check delays,
so one bad poll does not page anyone and one good poll does not clear a
real outage.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .config import Config
from .store import RestSnapshot, Store, as_number
from .text import duration, reason, t

LOG = logging.getLogger("ruview-analytics.checks")

LEVELS = {"ok": 0, "info": 1, "warning": 2, "critical": 3}
# The bridge publishes its status every 10 s by default.
BRIDGE_STATUS_MAX_AGE_S = 60.0
# The bridge publishes each node at most once per second.
NODE_MQTT_MAX_AGE_S = 30.0
# The csi-recorder publishes its status every 30 s by default.
RECORDER_STATUS_MAX_AGE_S = 90.0
RECORDER_QUIET_S = 60.0
# A newly raised problem in one of these asks the recorder to keep raw CSI.
KEEP_ON_RAISE = ("vitals_frozen", "vitals_range", "presence")


@dataclass(frozen=True)
class Result:
    active: bool | None
    severity: str = "warning"
    summary: str = ""
    detail: dict[str, Any] = field(default_factory=dict)


UNKNOWN = Result(None)
OK = Result(False)


@dataclass(frozen=True)
class Ctx:
    cfg: Config
    store: Store
    now: float

    def t(self, key: str, **kwargs: object) -> str:
        return t(self.cfg.lang, key, **kwargs)

    def duration(self, seconds: float) -> str:
        return duration(self.cfg.lang, seconds)

    def join(self, parts: list[str]) -> str:
        return ("；" if self.cfg.lang == "zh" else "; ").join(parts)


Rule = Callable[[Ctx], Result]


@dataclass(frozen=True)
class Check:
    id: str
    name: str                      # Home Assistant entity name
    rule: Rule
    raise_after_s: float = 0.0     # condition must hold this long to raise
    clear_after_s: float = 0.0     # and be gone this long to clear
    suppressed_by: tuple[str, ...] = ()


# ── helpers ──────────────────────────────────────────────────────────────

def _rest(ctx: Ctx) -> RestSnapshot | None:
    snap = ctx.store.rest
    if snap is None or ctx.now - snap.at > 3 * ctx.cfg.check_interval_s:
        return None
    return snap


def _bridge_status(ctx: Ctx) -> dict[str, Any] | None:
    status = ctx.store.bridge_status
    ts = as_number((status or {}).get("ts"))
    if status is None or ts is None or ctx.now - ts > BRIDGE_STATUS_MAX_AGE_S:
        return None
    return status


def _server_up(ctx: Ctx) -> bool:
    snap = _rest(ctx)
    return snap is not None and snap.health is not None


# ── rules ────────────────────────────────────────────────────────────────

def rule_server(ctx: Ctx) -> Result:
    snap = _rest(ctx)
    if snap is None:
        return UNKNOWN
    if snap.error:
        error = snap.error[:120]
        return Result(True, "critical", ctx.t("server_down", error=error), {"error": error})
    return OK


def rule_api(ctx: Ctx) -> Result:
    snap = _rest(ctx)
    if snap is None or snap.error:
        return UNKNOWN
    if snap.auth_failed:
        return Result(True, "warning", ctx.t("api_auth"))
    return OK


def rule_bridge(ctx: Ctx) -> Result:
    store, cfg = ctx.store, ctx.cfg
    if store.bridge_avail == "offline":
        return Result(True, "critical", ctx.t("bridge_offline"))
    last = store.bridge_last_seen()
    if last is None:
        if ctx.now - store.started_at < cfg.bridge_silence_s:
            return UNKNOWN
        return Result(True, "critical", ctx.t("bridge_never"))
    age = ctx.now - last
    if age > cfg.bridge_silence_s:
        return Result(True, "critical", ctx.t("bridge_silent", age=ctx.duration(age)),
                      {"silent_s": round(age)})
    status = _bridge_status(ctx)
    if status is not None and status.get("ws_connected") is False and _server_up(ctx):
        return Result(True, "critical", ctx.t("bridge_ws_down"))
    return OK


def rule_source(ctx: Ctx) -> Result:
    evidence = False
    snap = _rest(ctx)
    if snap is not None and snap.health is not None:
        evidence = True
        source = str(snap.health.get("source", ""))
        if source.endswith(":offline"):
            return Result(True, "critical", ctx.t("source_offline", source=source), {"source": source})
    if snap is not None and snap.status is not None:
        state = snap.status.get("source_state")
        if state in ("stale", "disconnected", "synthetic"):
            return Result(True, "critical", ctx.t("source_state", state=state), {"source_state": state})
    status = _bridge_status(ctx)
    if status is not None:
        evidence = True
        if status.get("stale_source"):
            source = str(status.get("source"))
            return Result(True, "critical", ctx.t("source_offline", source=source), {"source": source})
        age = as_number(status.get("last_new_tick_age_s"))
        if status.get("ws_connected") and age is not None and age > ctx.cfg.stall_after_s:
            return Result(True, "critical", ctx.t("source_stalled", age=ctx.duration(age)),
                          {"last_new_tick_age_s": age})
    return OK if evidence else UNKNOWN


def _listed(body: dict[str, Any] | None, key: str = "nodes") -> list[dict[str, Any]] | None:
    items = (body or {}).get(key)
    if not isinstance(items, list):
        return None
    return [item for item in items if isinstance(item, dict)]


def _node_view(ctx: Ctx, node_id: int) -> tuple[bool | None, float | None, float | None, str]:
    """(fresh, csi_fps, last_seen_s, via) for one node, from the best source:
    REST /api/v1/stream/status (fresh nodes with measured fps), then
    /api/v1/nodes (csi_status), then the bridge's ruview/nodes/<id> topic."""
    snap = _rest(ctx)
    last_seen_s = None
    listed = None
    nodes = _listed(snap.nodes if snap else None)
    if nodes is not None:
        listed = next((n for n in nodes if n.get("node_id") == node_id), None)
        if listed is not None:
            ms = as_number(listed.get("csi_last_seen_ms"))
            if ms is None:
                ms = as_number(listed.get("last_seen_ms"))
            last_seen_s = ms / 1000 if ms is not None else None
    stream = _listed(snap.stream if snap else None)
    if stream is not None:
        entry = next((n for n in stream if n.get("node_id") == node_id), None)
        fps = as_number(entry.get("csi_fps")) if entry else None
        return entry is not None, fps, last_seen_s, "stream"
    if nodes is not None:
        if listed is None:
            return False, None, None, "nodes"
        status = listed.get("csi_status") or listed.get("status")
        return status == "active", None, last_seen_s, "nodes"
    seen = ctx.store.nodes_mqtt.get(node_id)
    if seen is None:
        # Only call a node missing once the bridge has reported other nodes.
        if ctx.store.nodes_mqtt and ctx.now - ctx.store.started_at > 2 * NODE_MQTT_MAX_AGE_S:
            return False, None, None, "mqtt"
        return None, None, None, "none"
    at, payload = seen
    age = ctx.now - at
    fresh = age <= NODE_MQTT_MAX_AGE_S and not payload.get("stale", False)
    ms = as_number(payload.get("last_seen_ms"))
    return fresh, as_number(payload.get("csi_fps")), (ms / 1000 if ms is not None else age), "mqtt"


def node_streaming(cfg: Config, store: Store, now: float, node_id: int) -> bool | None:
    """Whether one node is sending CSI at a normal rate (None: no data)."""
    fresh, fps, _, _ = _node_view(Ctx(cfg, store, now), node_id)
    if fresh is None:
        return None
    return fresh and (fps is None or fps >= cfg.min_node_fps)


def node_rule(node_id: int) -> Rule:
    def rule(ctx: Ctx) -> Result:
        fresh, fps, last_seen_s, via = _node_view(ctx, node_id)
        if fresh is None:
            return UNKNOWN
        detail = {"node": node_id, "csi_fps": fps, "last_seen_s": last_seen_s, "via": via}
        if not fresh:
            age = ""
            if last_seen_s is not None:
                d = ctx.duration(last_seen_s)
                age = f"（已 {d}）" if ctx.cfg.lang == "zh" else f" ({d})"
            return Result(True, "warning", ctx.t("node_down", node=node_id, age=age), detail)
        if fps is not None and fps < ctx.cfg.min_node_fps:
            return Result(True, "warning", ctx.t("node_slow", node=node_id, fps=f"{fps:.1f}"), detail)
        return Result(False, detail=detail)

    return rule


_METRICS = (("hr", "metric_hr"), ("br", "metric_br"))


def rule_vitals_frozen(ctx: Ctx) -> Result:
    cfg = ctx.cfg
    problems, detail, known = [], {}, False
    for key, label in _METRICS:
        samples = getattr(ctx.store, key).window(ctx.now, cfg.vitals_window_s)
        if len(samples) < cfg.frozen_min_samples:
            continue
        span = samples[-1][0] - samples[0][0]
        if span < cfg.frozen_min_span_s:
            continue
        known = True
        distinct = sorted({round(v, 2) for _, v in samples})
        detail[key] = {"samples": len(samples), "distinct": len(distinct)}
        if len(distinct) <= cfg.frozen_max_distinct:
            sep = "、" if cfg.lang == "zh" else ", "
            problems.append(ctx.t("vitals_frozen", metric=ctx.t(label), minutes=round(span / 60),
                                  count=len(distinct), values=sep.join(f"{v:g}" for v in distinct)))
    if problems:
        return Result(True, "warning", ctx.join(problems), detail)
    return Result(False, detail=detail) if known else UNKNOWN


def rule_vitals_range(ctx: Ctx) -> Result:
    cfg = ctx.cfg
    problems, detail, known = [], {}, False
    for (key, label), (lo, hi) in zip(_METRICS, (cfg.hr_range, cfg.br_range)):
        values = [v for _, v in getattr(ctx.store, key).window(ctx.now, cfg.vitals_window_s)]
        if len(values) < cfg.range_min_samples:
            continue
        known = True
        outside = sum(1 for v in values if not lo <= v <= hi)
        fraction = outside / len(values)
        detail[key] = {"samples": len(values), "outside": outside}
        if fraction > cfg.range_max_fraction:
            problems.append(ctx.t("vitals_range", metric=ctx.t(label), pct=round(fraction * 100),
                                  lo=f"{lo:g}", hi=f"{hi:g}"))
    if problems:
        return Result(True, "warning", ctx.join(problems), detail)
    return Result(False, detail=detail) if known else UNKNOWN


def rule_calibration(ctx: Ctx) -> Result:
    snap = _rest(ctx)
    if snap is None or snap.calibration is None:
        return UNKNOWN
    cal = snap.calibration
    # Older servers report the Debug name ("Fresh"), newer ones lowercase it.
    status = str(cal.get("status", "none")).lower()
    mode = cal.get("binding_mode")
    detail = {"status": status, "binding_mode": mode}

    def problem(severity: str, key: str, **kwargs: object) -> Result:
        text = ctx.t(key, **kwargs)
        if ctx.cfg.autocal_enabled:
            text += ctx.t("calibration_auto_hint")
        return Result(True, severity, text, detail)

    if status == "collecting":
        return Result(False, detail=detail)
    if status in ("fresh", "stale") and mode == "bootstrap_only":
        return problem("warning", "calibration_bootstrap")
    faults = cal.get("sequence_fault_node_ids")
    if status in ("fresh", "stale") and isinstance(faults, list) and faults:
        detail["sequence_fault_node_ids"] = faults
        return problem("warning", "calibration_fault", nodes=",".join(map(str, faults)))
    if status == "fresh":
        return Result(False, detail=detail)
    if status == "stale":
        return problem("info", "calibration_stale")
    return problem("warning", "calibration_missing", status=status)


def rule_recorder(ctx: Ctx) -> Result:
    store = ctx.store
    if store.recorder_avail is None and store.recorder_status is None:
        return UNKNOWN                  # no csi-recorder deployed
    if store.recorder_avail == "offline":
        return Result(True, "warning", ctx.t("recorder_offline"))
    status = store.recorder_status or {}
    ts = as_number(status.get("ts"))
    if ts is None:
        return UNKNOWN
    age = ctx.now - ts
    if age > RECORDER_STATUS_MAX_AGE_S:
        return Result(True, "warning", ctx.t("recorder_silent", age=ctx.duration(age)),
                      {"status_age_s": round(age)})
    detail = {key: status.get(key) for key in
              ("datagrams_hz", "ring_hours", "disk_free_gb", "keep_pending", "kernel_drops")
              if key in status}
    if status.get("disk_low"):
        free = as_number(status.get("disk_free_gb"))
        return Result(True, "warning",
                      ctx.t("recorder_disk_low", free="?" if free is None else f"{free:.1f}"), detail)
    hz = as_number(status.get("datagrams_hz"))
    if hz and status.get("recording") is False:
        error = str(status.get("last_error") or "?")[:120]
        return Result(True, "warning", ctx.t("recorder_write", error=error), detail)
    quiet = as_number(status.get("last_datagram_age_s"))
    if quiet is None or quiet > RECORDER_QUIET_S:
        # Only a problem while the nodes are sending to the server: the tee
        # copies what the server receives.
        if any(_node_view(ctx, nid)[0] for nid in ctx.cfg.expected_nodes):
            if quiet is None:
                return Result(True, "warning", ctx.t("recorder_no_data_never"), detail)
            return Result(True, "warning", ctx.t("recorder_no_data", age=ctx.duration(quiet)), detail)
    return Result(False, detail=detail)


def rule_presence(ctx: Ctx) -> Result:
    cfg, persons = ctx.cfg, ctx.store.persons
    if not persons:
        return UNKNOWN
    states = list(persons.values())
    if any(p.state in ("unknown", "unavailable") for p in states):
        return UNKNOWN
    if any(p.state == "home" for p in states):
        return OK
    if any(p.since is None for p in states):
        return UNKNOWN
    away = ctx.now - max(p.since for p in states if p.since is not None)
    if away < cfg.away_min_s:
        return OK
    samples = ctx.store.occupied.window(ctx.now, cfg.presence_window_s)
    if len(samples) < cfg.presence_min_samples:
        return UNKNOWN
    fraction = sum(v for _, v in samples) / len(samples)
    detail = {"away_s": round(away), "occupied_fraction": round(fraction, 3), "samples": len(samples)}
    if fraction > cfg.presence_max_fraction:
        return Result(True, "warning", ctx.t(
            "presence_contradiction", away=ctx.duration(away),
            minutes=round(cfg.presence_window_s / 60), pct=round(fraction * 100)), detail)
    return Result(False, detail=detail)


def default_checks(cfg: Config) -> list[Check]:
    """Order matters: a check can only be suppressed by checks listed before it."""
    upstream = ("server", "source")
    checks = [
        Check("server", "Sensing Server", rule_server, raise_after_s=90),
        Check("api", "API Access", rule_api, suppressed_by=("server",)),
        Check("bridge", "Bridge", rule_bridge, raise_after_s=60, clear_after_s=30),
        Check("source", "CSI Source", rule_source, raise_after_s=60, clear_after_s=60,
              suppressed_by=("server",)),
    ]
    checks += [Check(f"node_{nid}", f"Node {nid}", node_rule(nid), raise_after_s=120,
                     clear_after_s=60, suppressed_by=upstream)
               for nid in cfg.expected_nodes]
    checks += [
        Check("vitals_frozen", "Vitals Frozen", rule_vitals_frozen, clear_after_s=300),
        Check("vitals_range", "Vitals Range", rule_vitals_range, clear_after_s=300),
        Check("calibration", "Calibration", rule_calibration, suppressed_by=("server",)),
        Check("recorder", "Recorder", rule_recorder, raise_after_s=120, clear_after_s=60),
    ]
    if cfg.presence_enabled:
        checks.append(Check("presence", "Presence vs Phones", rule_presence, clear_after_s=600))
    return checks


# ── engine ───────────────────────────────────────────────────────────────

@dataclass
class CheckState:
    raised: bool = False
    since: float | None = None          # when the raised problem started
    pending_since: float | None = None  # condition flipped, waiting out the delay
    known: bool = False
    problem: Result | None = None       # latest active result
    latest: Result | None = None        # latest known result


def iso(ts: float | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


class Watchdog:
    def __init__(self, cfg: Config, checks: list[Check] | None = None) -> None:
        self.cfg = cfg
        self.checks = list(checks) if checks is not None else default_checks(cfg)
        self.states = {check.id: CheckState() for check in self.checks}
        self._prior_since: dict[str, float] | None = None

    def evaluate(self, store: Store, now: float) -> tuple[dict[str, Any], list[tuple[str, str]]]:
        """Run every check once. Returns the report and (check id, "raised" |
        "cleared") transitions."""
        if self._prior_since is None and store.prior_report is not None:
            self._prior_since = _prior_since(store.prior_report)
        ctx = Ctx(self.cfg, store, now)
        transitions = []
        for check in self.checks:
            state = self.states[check.id]
            if any(self.states[dep].raised for dep in check.suppressed_by if dep in self.states):
                result = UNKNOWN
            else:
                try:
                    result = check.rule(ctx)
                except Exception:  # a broken rule must not stop the others
                    LOG.exception("check %s failed", check.id)
                    result = UNKNOWN
            change = self._step(check, state, result, now)
            if change:
                transitions.append((check.id, change))
        return self.report(store, now), transitions

    def _step(self, check: Check, st: CheckState, result: Result, now: float) -> str | None:
        if result.active is None:
            st.known = False
            st.pending_since = None
            return None
        st.known = True
        st.latest = result
        if result.active:
            st.problem = result
            if st.raised:
                st.pending_since = None
                return None
            if st.pending_since is None:
                st.pending_since = now
            if now - st.pending_since < check.raise_after_s:
                return None
            st.raised = True
            st.since = self._restored_since(check.id, st.pending_since)
            st.pending_since = None
            return "raised"
        if not st.raised:
            st.pending_since = None
            return None
        if st.pending_since is None:
            st.pending_since = now
        if now - st.pending_since < check.clear_after_s:
            return None
        st.raised = False
        st.since = None
        st.problem = None
        st.pending_since = None
        return "cleared"

    def _restored_since(self, check_id: str, started: float) -> float:
        """Keep the start time of a problem that was already raised before a
        restart, so notifications keyed on it do not fire twice."""
        prior = (self._prior_since or {}).pop(check_id, None)
        return min(prior, started) if prior is not None else started

    def report(self, store: Store, now: float) -> dict[str, Any]:
        lang = self.cfg.lang
        checks: dict[str, Any] = {}
        issues: list[dict[str, Any]] = []
        for check in self.checks:
            st = self.states[check.id]
            if st.raised and st.problem is not None:
                entry = {"state": "problem", "severity": st.problem.severity,
                         "since": iso(st.since), "since_ts": st.since,
                         "summary": st.problem.summary, "detail": st.problem.detail}
                issues.append({"id": check.id, "name": check.name, **entry})
            elif st.known:
                entry = {"state": "ok", "severity": None, "since": None, "since_ts": None,
                         "summary": t(lang, "ok"), "detail": st.latest.detail if st.latest else {}}
            else:
                entry = {"state": "unknown", "severity": None, "since": None, "since_ts": None,
                         "summary": None, "detail": {}}
            checks[check.id] = entry
        issues.sort(key=lambda i: (-LEVELS[i["severity"]], i["since_ts"] or 0))
        status = max((i["severity"] for i in issues), key=LEVELS.__getitem__, default="ok")
        return {
            "ts": now,
            "checked_at": iso(now),
            "status": status,
            "open_issues": sum(1 for i in issues if LEVELS[i["severity"]] >= LEVELS["warning"]),
            "top_issue": (issues[0]["summary"] if issues else t(lang, "ok"))[:250],
            "issues": issues,
            "checks": checks,
            "vitals": vitals_status(self.cfg, store, now),
            "ha_link": store.ha_link,
        }


def _prior_since(report: dict[str, Any]) -> dict[str, float]:
    out = {}
    for check_id, entry in (report.get("checks") or {}).items():
        if isinstance(entry, dict) and entry.get("state") == "problem":
            since = as_number(entry.get("since_ts"))
            if since is not None:
                out[str(check_id)] = since
    return out


def vitals_status(cfg: Config, store: Store, now: float) -> dict[str, Any]:
    """Whether the room-level CSI vitals are currently published, and why not."""
    lang = cfg.lang
    state = store.last_state
    if state is None or store.state_at is None or now - store.state_at > 60:
        return {"state": "unknown", "text": t(lang, "vitals_unknown"), "reason": None}
    hr = as_number(state.get("heart_rate_bpm"))
    if state.get("vitals_present") or (hr is not None and hr > 0):
        return {"state": "published", "text": t(lang, "vitals_published"), "reason": None}
    code = state.get("abstention_reason")
    text = t(lang, "vitals_withheld")
    if code:
        text += ("：" if lang == "zh" else ": ") + str(reason(lang, code))
    return {"state": "withheld", "text": text, "reason": code}


def keep_requests(cfg: Config, store: Store, report: dict[str, Any],
                  transitions: list[tuple[str, str]]) -> list[dict[str, Any]]:
    """csi-recorder keep requests (`ruview/recorder/keep`) for vitals and
    presence problems raised in this round, so the raw CSI that led to them
    can be replayed later. Only when a recorder has been seen."""
    if cfg.keep_on_raise_s <= 0 or (store.recorder_avail is None and store.recorder_status is None):
        return []
    out = []
    for check_id, change in transitions:
        if change != "raised" or check_id not in KEEP_ON_RAISE:
            continue
        entry = report["checks"][check_id]
        since = as_number(entry.get("since_ts")) or report["ts"]
        out.append({
            "id": f"wd-{check_id}-{int(since)}",
            "tag": f"watchdog_{check_id}",
            "last_s": cfg.keep_on_raise_s,
            "source": "watchdog",
            "note": str(entry.get("summary") or "")[:200],
        })
    return out


def metrics(report: dict[str, Any]) -> dict[str, Any]:
    """Flat numbers for Telegraf (`ruview/watchdog/metrics`)."""
    counts = {level: 0 for level in ("critical", "warning", "info")}
    for issue in report["issues"]:
        counts[issue["severity"]] += 1
    out: dict[str, Any] = {"status_level": LEVELS[report["status"]],
                           "open_issues": report["open_issues"], **counts}
    for check_id, entry in report["checks"].items():
        if entry["state"] != "unknown":
            out[f"check_{check_id}"] = 1 if entry["state"] == "problem" else 0
    if report.get("ha_link") is not None:
        out["ha_link"] = 1 if report["ha_link"] else 0
    autocal = report.get("autocal")
    if isinstance(autocal, dict):
        out["autocal_collecting"] = 1 if autocal.get("state") == "collecting" else 0
        if autocal.get("calibration_age_h") is not None:
            out["calibration_age_h"] = autocal["calibration_age_h"]
    return out
