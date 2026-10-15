"""Automatic empty-room calibration (architecture plan §5.2, WP1.2).

The sensing-server publishes CSI heart and breathing rates only while an
explicit empty-room calibration is fresh (12 hours) and exactly one person
is in the room. Recalibrating by hand every day is not realistic, so the
watchdog does it while the home is empty:

1. It starts a capture once every phone has been away for AUTOCAL_AWAY_MIN
   minutes, the room has been quiet for the last 10 minutes, the source
   node is streaming, and the server's calibration is missing, stale, only
   a startup baseline restored after a restart, broken by a CSI sequence
   fault, or older than AUTOCAL_REFRESH_H hours. The server refuses a new
   capture while one is fresh, so a fresh one is reset first, unless a
   promoted startup baseline is stored: the reset would delete it too.
2. It binds the capture to AUTOCAL_SOURCE_NODE alone and finalizes it once
   the server's frame and duration targets are met (1000 frames, 600 s).
   The model is built from that node's CSI either way; binding more nodes
   only makes the finalize wait for all of them and lets a reboot of any
   of them block the vitals.
3. It cancels when someone comes home, the room gets busy, or the capture
   faults, stalls or runs too long, and tries again 30 minutes later at
   the earliest.
4. Each capture publishes an annotation (`ruview/annotation`) when it
   starts and when it ends.

The rest of the watchdog only reads; these calls are the analytics
service's only writes to the sensing-server.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

from .checks import iso, node_streaming
from .config import Config
from .rest import Poster, http_post_json
from .store import PersonState, Store, as_number
from .text import duration, t

LOG = logging.getLogger("ruview-analytics.autocal")

# The server accepts any SHA-256 as the room binding. This fixed one marks
# the captures the watchdog starts, so it picks its own capture up again
# after a restart and leaves manual ones alone.
BINDING_DIGEST = hashlib.sha256(b"ruview-analytics/auto-calibration/v1").hexdigest()

QUIET_WINDOW_S = 600.0     # movement looked at before a start
RECENT_WINDOW_S = 120.0    # and during a capture
MIN_QUIET_SAMPLES = 300    # the store keeps at most one sample per second
MIN_RECENT_SAMPLES = 60
RETRY_AFTER_S = 1800.0     # after a cancelled or failed capture
MAX_CAPTURE_S = 1800.0     # the server needs 600 s
NO_FRAMES_S = 120.0
FRESH_S = 12 * 3600.0      # the server's fresh window; a refresh this late is off
# Stop refusals that a later round can get past: the source node paused
# (grid stale) or has not contributed yet.
STOP_LATER = ("calibration_grid_stale", "calibration_source_nodes_missing")


@dataclass(frozen=True)
class View:
    """What one round looks at, copied out of the store under its lock."""

    calibration: dict[str, Any] | None      # GET /api/v1/calibration/status
    token_refused: bool                     # the server answered 401/403
    persons: dict[str, PersonState] | None
    quiet: tuple[float | None, int]         # share of "moving" samples, count
    recent: tuple[float | None, int]
    source_streaming: bool | None


@dataclass
class Session:
    boot_epoch: str
    session_id: str
    source_node_ids: list[int]
    started_at: float

    def identity(self) -> dict[str, Any]:
        return {"boot_epoch": self.boot_epoch, "session_id": self.session_id,
                "binding_digest": BINDING_DIGEST, "source_node_ids": self.source_node_ids}


def moving_share(store: Store, now: float, window_s: float) -> tuple[float | None, int]:
    samples = store.moving.window(now, window_s)
    if not samples:
        return None, 0
    return sum(v for _, v in samples) / len(samples), len(samples)


def _who(entity: str) -> str:
    return entity.split(".", 1)[-1]


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _bootstrap_stored(cal: dict[str, Any]) -> bool:
    """Whether a promoted startup baseline is stored on the server, which a
    reset would delete."""
    stored = cal.get("bootstrap_baseline")
    return isinstance(stored, dict) and stored.get("stored") is True


def _node_ids(value: Any) -> list[int] | None:
    if isinstance(value, list) and value and all(isinstance(v, int) and not isinstance(v, bool) for v in value):
        return sorted(set(value))
    return None


class AutoCalibrator:
    def __init__(self, cfg: Config, post: Poster = http_post_json) -> None:
        if cfg.autocal_source_node is None:
            raise ValueError("automatic calibration needs AUTOCAL_SOURCE_NODE")
        self.cfg = cfg
        self.node = cfg.autocal_source_node
        self._post = post
        self.session: Session | None = None
        self.retry_at = 0.0
        self.state = "unknown"          # ok | waiting | collecting | unknown
        self.waiting: list[str] = []
        self.progress: dict[str, Any] | None = None
        self.last: dict[str, Any] | None = None      # the latest finished capture
        self.calibration_age_s: float | None = None
        self._logged_wait: tuple[str, ...] = ()

    def t(self, key: str, **kwargs: object) -> str:
        return t(self.cfg.lang, key, **kwargs)

    def duration(self, seconds: float) -> str:
        return duration(self.cfg.lang, seconds)

    # ── inputs ───────────────────────────────────────────────────────────

    def observe(self, store: Store, now: float) -> View:
        snap = store.rest
        recent = snap is not None and now - snap.at <= 3 * self.cfg.check_interval_s
        calibration = None
        if recent and not snap.error and not snap.auth_failed:
            calibration = snap.calibration
        return View(
            calibration=calibration,
            token_refused=recent and snap.auth_failed,
            persons=dict(store.persons) if store.persons is not None else None,
            quiet=moving_share(store, now, QUIET_WINDOW_S),
            recent=moving_share(store, now, RECENT_WINDOW_S),
            source_streaming=node_streaming(self.cfg, store, now, self.node),
        )

    # ── one round ────────────────────────────────────────────────────────

    def step(self, view: View, now: float) -> list[dict[str, Any]]:
        """Advance by one round. Returns the annotations to publish."""
        if self.session is not None:
            return self._collecting(view, now)
        return self._idle(view, now)

    def _idle(self, view: View, now: float) -> list[dict[str, Any]]:
        self.progress = None
        cal = view.calibration
        if cal is None:
            self.state = "unknown"
            self.waiting = [self.t("api_auth" if view.token_refused else "autocal_wait_server")]
            return []
        if not _text(cal.get("boot_epoch")):
            # Servers before the bound calibration API (2026-10) have no
            # boot identity; their capture calls work differently.
            self.state = "unknown"
            self.waiting = [self.t("autocal_wait_old_server")]
            return []
        status = str(cal.get("status", "none")).lower()
        mode = cal.get("binding_mode")
        self.calibration_age_s = self._age(cal, now) if mode == "bound" else None
        if status in ("uncalibrated", "collecting") and mode == "bound":
            return self._adopt(cal, now)
        due, reset = self._due(cal, status, mode)
        if due is None:
            self.state = "ok"
            self.waiting = []
            self._logged_wait = ()
            return []
        blockers = self._blockers(view, now)
        if reset and _bootstrap_stored(cal):
            blockers.insert(0, ("bootstrap", self.t("autocal_wait_bootstrap")))
        if now < self.retry_at:
            blockers.insert(0, self._retry_reason(now))
        if blockers:
            return self._wait(blockers, view)
        return self._start(cal, now, due, reset)

    def _age(self, cal: dict[str, Any], now: float) -> float | None:
        receipt = cal.get("model_receipt")
        done_ms = as_number(receipt.get("completed_at_unix_ms")) if isinstance(receipt, dict) else None
        return max(0.0, now - done_ms / 1000) if done_ms is not None else None

    def _due(self, cal: dict[str, Any], status: str, mode: Any) -> tuple[str | None, bool]:
        """(why a new capture is needed or None, whether to reset first)."""
        if mode == "bootstrap_only":
            return "bootstrap", False       # a start replaces the restored baseline
        if status == "fresh":
            faults = cal.get("sequence_fault_node_ids")
            if isinstance(faults, list) and faults:
                return "fault", True
            age = self.calibration_age_s
            # A reset would also delete a stored startup baseline: with one
            # stored, a fresh calibration is renewed once it turns stale.
            if (age is not None and age >= self.cfg.autocal_refresh_s
                    and self.cfg.autocal_refresh_s < FRESH_S and not _bootstrap_stored(cal)):
                return "refresh", True
            return None, False
        if status in ("stale", "expired", "none", "uncalibrated"):
            return status, False
        return None, False                  # unknown status: leave it alone

    def _blockers(self, view: View, now: float) -> list[tuple[str, str]]:
        """(code, text) of everything that keeps a due capture from starting."""
        out = []
        persons = view.persons
        if not persons:
            out.append(("phones", self.t("autocal_wait_phones_unknown")))
        else:
            home = [_who(e) for e, p in persons.items() if p.state == "home"]
            unknown = [p for p in persons.values()
                       if p.state in ("unknown", "unavailable") or p.since is None]
            if home:
                out.append(("home", self.t("autocal_wait_home", who=self._names(home))))
            elif unknown:
                out.append(("phones", self.t("autocal_wait_phones_unknown")))
            else:
                away = now - max(p.since for p in persons.values() if p.since is not None)
                if away < self.cfg.autocal_away_s:
                    out.append(("away", self.t("autocal_wait_away", away=self.duration(away),
                                               need=self.duration(self.cfg.autocal_away_s))))
        share, count = view.quiet
        if share is None or count < MIN_QUIET_SAMPLES:
            out.append(("no_state", self.t("autocal_wait_no_state")))
        elif share > self.cfg.autocal_max_moving:
            out.append(("moving", self.t("autocal_wait_moving", pct=round(share * 100))))
        if view.source_streaming is not True:
            out.append(("node", self.t("autocal_wait_node", node=self.node)))
        return out

    def _retry_reason(self, now: float) -> tuple[str, str]:
        result = (self.last or {}).get("result")
        return ("retry", self.t("autocal_wait_retry",
                                result=self.t(f"autocal_result_{'cancelled' if result == 'cancelled' else 'failed'}"),
                                left=self.duration(self.retry_at - now)))

    def _wait(self, reasons: list[tuple[str, str]], view: View) -> list[dict[str, Any]]:
        self.state = "waiting"
        self.waiting = [text for _, text in reasons]
        # Log when the kinds of reasons change, with the movement share for
        # tuning AUTOCAL_MAX_MOVING.
        codes = tuple(code for code, _ in reasons)
        if codes != self._logged_wait:
            self._logged_wait = codes
            share, count = view.quiet
            LOG.info("calibration due, waiting: %s (moving %s of %d samples)", "; ".join(self.waiting),
                     "-" if share is None else f"{share:.0%}", count)
        return []

    def _adopt(self, cal: dict[str, Any], now: float) -> list[dict[str, Any]]:
        boot, session_id = _text(cal.get("boot_epoch")), _text(cal.get("session_id"))
        nodes = _node_ids(cal.get("source_node_ids"))
        if cal.get("binding_digest") != BINDING_DIGEST or not (boot and session_id and nodes):
            self.state = "waiting"
            self.waiting = [self.t("autocal_wait_manual")]
            self._logged_wait = ()
            return []
        elapsed = as_number(cal.get("elapsed_s")) or 0.0
        self.session = Session(boot, session_id, nodes, started_at=now - elapsed)
        self.state = "collecting"
        LOG.info("continuing the automatic calibration %s on node %s", session_id, nodes)
        return []

    def _start(self, cal: dict[str, Any], now: float, due: str, reset: bool) -> list[dict[str, Any]]:
        note = ""
        if reset:
            boot = _text(cal.get("boot_epoch"))
            ok, _, error = self._call("reset", {"boot_epoch": boot})
            if not ok:
                return self._failed_start(now, error, reset=False)
            age = self.calibration_age_s
            note = self.t("autocal_reset_fault") if due == "fault" else self.t(
                "autocal_reset_age", age=self.duration(age or 0))
            LOG.info("reset the calibration (%s) before a new capture", due)
        ok, body, error = self._call(f"start?source_node_id={self.node}",
                                     {"binding_digest": BINDING_DIGEST, "source_node_ids": [self.node]})
        if not ok or body is None:
            return self._failed_start(now, error, reset=reset)
        boot, session_id = _text(body.get("boot_epoch")), _text(body.get("session_id"))
        if not (boot and session_id):
            return self._failed_start(now, "start response without a session", reset=reset)
        self.session = Session(boot, session_id, _node_ids(body.get("source_node_ids")) or [self.node],
                               started_at=now)
        self.calibration_age_s = None      # a start replaces the server's model
        self.state = "collecting"
        self.waiting = []
        self._logged_wait = ()
        LOG.info("started automatic calibration %s on node %d (%s)", session_id, self.node, due)
        return [self._note("start", now, self.t("autocal_started", node=self.node) + note)]

    def _failed_start(self, now: float, error: str | None, reset: bool) -> list[dict[str, Any]]:
        reason = self.t("autocal_reason_refused", error=error or "?")
        self.retry_at = now + RETRY_AFTER_S
        self.last = {"result": "failed", "at": iso(now), "reason": reason}
        self.state = "waiting"
        self.waiting = [self._retry_reason(now)[1]]
        if reset:
            self.calibration_age_s = None
        LOG.warning("automatic calibration could not start: %s", error)
        if not reset:
            return []
        # The old calibration is gone: worth a note, unlike a refused start.
        return [self._note("failed", now, self.t("autocal_failed_after_reset", reason=reason))]

    def _collecting(self, view: View, now: float) -> list[dict[str, Any]]:
        s = self.session
        assert s is not None
        cal = view.calibration
        if cal is not None:
            if cal.get("boot_epoch") != s.boot_epoch:
                return self._end(now, "failed", self.t("autocal_reason_restarted"), cancel=False)
            receipt = cal.get("model_receipt")
            if (str(cal.get("status", "")).lower() == "fresh" and isinstance(receipt, dict)
                    and receipt.get("session_id") == s.session_id):
                # Finalized, though the stop call did not get its answer back.
                return self._done(now, {"frame_count": receipt.get("frame_count"),
                                        "variance_explained": receipt.get("variance_explained"),
                                        "baseline_eigenvalue_count": receipt.get("baseline_eigenvalue_count")})
            if cal.get("session_id") != s.session_id:
                return self._end(now, "failed", self.t("autocal_reason_lost"), cancel=False)
        reason = self._abort_reason(view, now, cal)
        if reason:
            return self._end(now, "cancelled", reason, cancel=True)
        if cal is None:
            return []                       # server unreachable: MAX_CAPTURE_S bounds the wait
        frames = int(as_number(cal.get("frame_count")) or 0)
        need = int(as_number(cal.get("min_frames")) or 0)
        elapsed = as_number(cal.get("elapsed_s")) or 0.0
        need_s = as_number(cal.get("min_duration_s")) or 0.0
        self.progress = {"frame_count": frames, "frames_needed": need,
                         "elapsed_s": round(elapsed), "duration_needed_s": round(need_s)}
        if frames == 0 and now - s.started_at > NO_FRAMES_S:
            return self._end(now, "cancelled", self.t("autocal_reason_no_frames", node=s.source_node_ids[0]),
                             cancel=True)
        if need <= 0 or frames < need or elapsed < need_s:
            return []
        ok, body, error = self._call("stop", s.identity())
        if ok and body is not None:
            return self._done(now, body)
        if body is None or "frames_needed" in body or body.get("error_code") in STOP_LATER:
            # No answer (the next status shows whether it finalized), not
            # complete yet, or the source paused: try again next round.
            # MAX_CAPTURE_S bounds the retries.
            return []
        return self._end(now, "failed", self.t("autocal_reason_refused", error=error or "?"), cancel=True)

    def _abort_reason(self, view: View, now: float, cal: dict[str, Any] | None) -> str | None:
        s = self.session
        assert s is not None
        if view.persons:
            home = [_who(e) for e, p in view.persons.items() if p.state == "home"]
            if home:
                return self.t("autocal_reason_home", who=self._names(home))
        share, count = view.recent
        if share is not None and count >= MIN_RECENT_SAMPLES and share > self.cfg.autocal_max_moving:
            return self.t("autocal_reason_moving", pct=round(share * 100))
        faults = cal.get("sequence_fault_node_ids") if cal else None
        if isinstance(faults, list) and faults:
            return self.t("autocal_reason_fault", nodes=",".join(map(str, faults)))
        if now - s.started_at > MAX_CAPTURE_S:
            return self.t("autocal_reason_timeout", minutes=round(MAX_CAPTURE_S / 60))
        return None

    def _end(self, now: float, result: str, reason: str, cancel: bool) -> list[dict[str, Any]]:
        s = self.session
        assert s is not None
        if cancel:
            ok, _, error = self._call("cancel", s.identity())
            if not ok:
                LOG.warning("could not cancel calibration %s: %s", s.session_id, error)
        self.session = None
        self.progress = None
        self.retry_at = now + RETRY_AFTER_S
        self.last = {"result": result, "at": iso(now), "reason": reason}
        self.calibration_age_s = None
        self.state = "waiting"
        self.waiting = [self._retry_reason(now)[1]]
        LOG.warning("automatic calibration %s %s: %s", s.session_id, result, reason)
        key = "autocal_cancelled" if result == "cancelled" else "autocal_failed"
        return [self._note(result, now, self.t(key, reason=reason), node=s.source_node_ids[0],
                           elapsed_s=round(now - s.started_at))]

    def _done(self, now: float, body: dict[str, Any]) -> list[dict[str, Any]]:
        s = self.session
        assert s is not None
        frames = as_number(body.get("frame_count"))
        elapsed = as_number(body.get("elapsed_s"))
        if elapsed is None:
            elapsed = now - s.started_at
        self.session = None
        self.progress = None
        self.retry_at = 0.0
        self.calibration_age_s = 0.0
        self.last = {"result": "done", "at": iso(now), "reason": None}
        self.state = "ok"
        LOG.info("automatic calibration %s done: %s frames in %.0f s", s.session_id, frames, elapsed)
        text = self.t("autocal_done", minutes=round(elapsed / 60),
                      frames="?" if frames is None else int(frames))
        return [self._note("done", now, text, node=s.source_node_ids[0], elapsed_s=round(elapsed),
                           frame_count=frames,
                           variance_explained=as_number(body.get("variance_explained")),
                           baseline_eigenvalue_count=as_number(body.get("baseline_eigenvalue_count")))]

    # ── helpers ──────────────────────────────────────────────────────────

    def _call(self, path: str, body: dict[str, Any]) -> tuple[bool, dict[str, Any] | None, str | None]:
        url = f"{self.cfg.ruview_http}/api/v1/calibration/{path}"
        _, data, error = self._post(url, self.cfg.api_token, body, self.cfg.http_timeout_s)
        data = data if isinstance(data, dict) else None
        if error or data is None:
            return False, data, error or "unexpected response"
        if data.get("success") is True:
            return True, data, None
        return False, data, str(data.get("error_code") or data.get("error") or "refused")[:200]

    def _note(self, action: str, now: float, text: str, node: int | None = None,
              **numbers: float | None) -> dict[str, Any]:
        """One `ruview/annotation` message, in the HA label buttons' format."""
        note: dict[str, Any] = {
            "kind": "calibration", "label": "empty_room", "action": action,
            "ts": int(now * 1000), "person": "", "source": "watchdog", "value": 1,
            "source_node": self.node if node is None else node, "text": text,
        }
        note.update({key: value for key, value in numbers.items() if value is not None})
        return note

    def _names(self, names: list[str]) -> str:
        return ("、" if self.cfg.lang == "zh" else ", ").join(names)

    def status(self, now: float) -> dict[str, Any]:
        """The `autocal` part of the watchdog report."""
        age = self.calibration_age_s
        if self.state == "collecting":
            p = self.progress or {}
            done = (p.get("elapsed_s") or 0) / 60
            need = (p.get("duration_needed_s") or 600) / 60
            text = self.t("autocal_state_collecting", done=int(done), need=round(need))
        elif self.state == "ok":
            text = self.t("autocal_state_ok") if age is None else self.t(
                "autocal_state_ok_age", age=self.duration(age))
        else:
            sep = "；" if self.cfg.lang == "zh" else "; "
            key = "autocal_state_waiting" if self.state == "waiting" else "autocal_state_unknown"
            text = self.t(key, why=sep.join(self.waiting) or "-")
        return {
            "state": self.state,
            "text": text[:250],
            "source_node": self.node,
            "waiting_for": list(self.waiting) if self.state != "ok" else [],
            "progress": self.progress,
            "last": self.last,
            "retry_at": iso(self.retry_at) if self.retry_at > now else None,
            "calibration_age_h": None if age is None else round(age / 3600, 2),
        }
