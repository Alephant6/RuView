"""Permanent clips: copy a time range out of the ring before it rolls off.

Keep requests arrive on MQTT (`<prefix>/recorder/keep`) from the HA label
buttons, the watchdog or a person, as JSON naming a tag and a range:

    {"tag": "gt_session", "start": 1760500000, "end": 1760503600,
     "person": "sq", "note": "...", "source": "ha"}
    {"tag": "keep_raw", "last_s": 600}

`start`/`end` are epoch seconds or ISO 8601 times with a timezone. A range
may end in the future ("keep the next 20 minutes"): requests wait in
keep/pending.json, which survives restarts, until the range has ended and
reached the disk. The clip is then written to
keep/<UTC day>/<start>_<tag>.csi.raw.gz next to a .csi.meta.json sidecar,
and every outcome is published on `<prefix>/recorder/keep/result`.

Clips are never deleted automatically. KEEP_MAX_MIN caps one clip and
KEEP_MAX_H_PER_DAY caps what one UTC day (by clip start) may add, so a
stuck automation cannot fill the disk.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import threading
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config
from .packets import classify
from .rawfile import (
    META_SUFFIX,
    NS,
    SUFFIX,
    RawWriter,
    day,
    iter_range,
    list_files,
    stamp,
    unique_path,
)

LOG = logging.getLogger("csi-recorder.keep")

TAG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
PERSON_RE = re.compile(r"^[a-z0-9_.]{0,64}$")
SOURCE_RE = re.compile(r"^[a-z0-9_.-]{0,32}$")
MAX_PAYLOAD_BYTES = 4096
MAX_NOTE_CHARS = 200
MAX_PENDING = 50
SETTLE_NS = 2 * NS          # margin after the flush that covers `end`
MAX_EPOCH_S = 4_102_444_800  # 2100-01-01; anything later is a unit mistake
PENDING_FILE = "pending.json"


class KeepError(ValueError):
    """A request that cannot be served; the message goes into the result."""


class Interrupted(Exception):
    """Extraction stopped because the recorder is shutting down."""


def iso(ts_ns: int) -> str:
    return datetime.fromtimestamp(ts_ns / NS, timezone.utc).isoformat(timespec="seconds")


def to_ns(value: Any, name: str) -> int:
    """Epoch seconds or an ISO 8601 time with a timezone, as ns."""
    if isinstance(value, bool):
        raise KeepError(f"{name} must be epoch seconds or an ISO 8601 time")
    if isinstance(value, (int, float)):
        if not math.isfinite(value) or not 0 <= value <= MAX_EPOCH_S:
            raise KeepError(f"{name} must be epoch seconds, got {value!r}")
        return int(round(value * NS))
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.strip())
        except ValueError as exc:
            raise KeepError(f"{name} is not an ISO 8601 time: {value[:40]!r}") from exc
        if dt.tzinfo is None:
            raise KeepError(f"{name} needs a timezone, e.g. {value[:19]}Z")
        return int(round(dt.timestamp() * NS))
    raise KeepError(f"{name} must be epoch seconds or an ISO 8601 time")


def _text(obj: dict[str, Any], key: str, pattern: re.Pattern[str] | None, limit: int) -> str:
    value = obj.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        raise KeepError(f"{key} must be a string")
    value = value.strip()
    if pattern is not None and not pattern.match(value):
        raise KeepError(f"{key} has an invalid format: {value[:40]!r}")
    # Notes are free text: drop control characters, cap the length.
    value = "".join(ch for ch in value if ch.isprintable())
    return value[:limit]


@dataclass(frozen=True)
class KeepRequest:
    id: str
    tag: str
    start_ns: int
    end_ns: int
    received_ns: int
    person: str = ""
    note: str = ""
    source: str = ""

    @property
    def duration_s(self) -> float:
        return (self.end_ns - self.start_ns) / NS

    @property
    def day(self) -> str:
        return day(self.start_ns)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, obj: Any) -> KeepRequest:
        if not isinstance(obj, dict):
            raise KeepError("not an object")
        try:
            req = cls(
                id=str(obj["id"]), tag=str(obj["tag"]),
                start_ns=int(obj["start_ns"]), end_ns=int(obj["end_ns"]),
                received_ns=int(obj["received_ns"]),
                person=str(obj.get("person", "")), note=str(obj.get("note", "")),
                source=str(obj.get("source", "")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise KeepError(f"bad pending entry: {exc}") from exc
        if not ID_RE.match(req.id) or not TAG_RE.match(req.tag) or req.end_ns <= req.start_ns:
            raise KeepError("bad pending entry")
        return req


def parse_request(payload: bytes, now_ns: int, cfg: Config) -> KeepRequest:
    """Validate one MQTT keep request. Raises KeepError with a reason."""
    if len(payload) > MAX_PAYLOAD_BYTES:
        raise KeepError(f"payload larger than {MAX_PAYLOAD_BYTES} bytes")
    try:
        obj = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise KeepError("payload is not JSON") from exc
    if not isinstance(obj, dict):
        raise KeepError("payload must be a JSON object")

    tag = obj.get("tag")
    if not isinstance(tag, str) or not TAG_RE.match(tag):
        raise KeepError("tag must match [a-z0-9][a-z0-9_-]{0,31}")

    max_ns = int(cfg.keep_max_min * 60 * NS)
    if "last_s" in obj:
        if "start" in obj or "end" in obj:
            raise KeepError("give either last_s or start/end, not both")
        last_s = obj["last_s"]
        if isinstance(last_s, bool) or not isinstance(last_s, (int, float)) \
                or not math.isfinite(last_s) or last_s <= 0:
            raise KeepError("last_s must be a positive number of seconds")
        if last_s * NS > max_ns:
            raise KeepError(f"longer than KEEP_MAX_MIN ({cfg.keep_max_min:g} min)")
        start_ns, end_ns = now_ns - int(last_s * NS), now_ns
    elif "start" in obj and "end" in obj:
        start_ns, end_ns = to_ns(obj["start"], "start"), to_ns(obj["end"], "end")
    else:
        raise KeepError("give last_s, or start and end")

    if end_ns <= start_ns:
        raise KeepError("end must be after start")
    if end_ns - start_ns > max_ns:
        raise KeepError(f"longer than KEEP_MAX_MIN ({cfg.keep_max_min:g} min)")
    if end_ns > now_ns + max_ns:
        raise KeepError(f"ends more than {cfg.keep_max_min:g} min in the future")
    if end_ns <= now_ns - int(cfg.ring_hours * 3600 * NS):
        raise KeepError(f"older than the {cfg.ring_hours:g} h ring; the data is gone")

    req_id = obj.get("id")
    if req_id is None:
        req_id = uuid.uuid4().hex[:12]
    elif not isinstance(req_id, str) or not ID_RE.match(req_id):
        raise KeepError("id must match [A-Za-z0-9_.:-]{1,64}")
    return KeepRequest(
        id=req_id, tag=tag, start_ns=start_ns, end_ns=end_ns, received_ns=now_ns,
        person=_text(obj, "person", PERSON_RE, 64),
        note=_text(obj, "note", None, MAX_NOTE_CHARS),
        source=_text(obj, "source", SOURCE_RE, 32),
    )


def _write_json_atomic(path: Path, obj: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, ensure_ascii=False, indent=1)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class KeepStore:
    """Pending requests, the daily budget and clip extraction.

    Used from one thread only (the keep worker); `snapshot` is a plain dict
    replaced in one assignment, so the status thread can read it."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.root = cfg.keep_dir
        self.pending: list[KeepRequest] = []
        self.snapshot: dict[str, Any] = {"pending": 0, "today_min": 0.0}

    # ── pending requests ──────────────────────────────────────────────────
    def load(self) -> None:
        """Read pending.json and remove clips left half-written by a crash
        (their requests are still pending, so they are written again)."""
        self.root.mkdir(parents=True, exist_ok=True)
        for part in self.root.rglob(f"*{SUFFIX}.part"):
            LOG.warning("removing an unfinished clip %s", part.name)
            part.unlink(missing_ok=True)
        path = self.root / PENDING_FILE
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raw = {"pending": []}
        except (OSError, ValueError) as exc:
            LOG.error("ignoring an unreadable %s: %s", path, exc)
            raw = {"pending": []}
        entries = raw.get("pending", []) if isinstance(raw, dict) else []
        for entry in entries[:MAX_PENDING]:
            try:
                self.pending.append(KeepRequest.from_json(entry))
            except KeepError as exc:
                LOG.warning("dropping a pending keep request: %s", exc)
        if self.pending:
            LOG.info("%d keep request(s) pending from before the restart", len(self.pending))
        self._update_snapshot(time.time_ns())

    def _save(self) -> None:
        _write_json_atomic(self.root / PENDING_FILE,
                           {"version": 1, "pending": [r.to_json() for r in self.pending]})

    def add(self, req: KeepRequest, now_ns: int) -> str:
        """Queue a validated request. Returns "accepted" or "duplicate";
        raises KeepError when the budget or queue is full."""
        # MQTT QoS 1 may deliver a request twice; HA may fire twice.
        same = (req.tag, req.start_ns, req.end_ns)
        if any(r.id == req.id or (r.tag, r.start_ns, r.end_ns) == same for r in self.pending) or \
                any(c.get("id") == req.id or (c.get("tag"), c.get("start_ns"), c.get("end_ns")) == same
                    for c in self.clips(req.day)):
            return "duplicate"
        if len(self.pending) >= MAX_PENDING:
            raise KeepError(f"{MAX_PENDING} requests already pending")
        budget_s = self.cfg.keep_max_h_per_day * 3600
        if budget_s <= 0:
            raise KeepError("keeping is off (KEEP_MAX_H_PER_DAY=0)")
        used_s = self.used_s(req.day)
        if used_s + req.duration_s > budget_s:
            raise KeepError(
                f"daily budget: {used_s / 60:.0f} of {budget_s / 60:.0f} min for "
                f"{req.day} (UTC) already used or pending")
        self.pending.append(req)
        try:
            self._save()
        except OSError as exc:
            self.pending.remove(req)
            raise KeepError(f"cannot save {PENDING_FILE}: {exc}") from exc
        self._update_snapshot(now_ns)
        return "accepted"

    def kept_clip(self, req: KeepRequest) -> dict[str, Any] | None:
        """The sidecar of a clip already written for this request."""
        return next((c for c in self.clips(req.day) if c.get("id") == req.id), None)

    def due(self, now_ns: int) -> list[KeepRequest]:
        ready_ns = now_ns - int(self.cfg.flush_interval_s * NS) - SETTLE_NS
        return sorted((r for r in self.pending if r.end_ns <= ready_ns),
                      key=lambda r: r.end_ns)

    def done(self, req: KeepRequest, now_ns: int) -> None:
        self.pending = [r for r in self.pending if r.id != req.id]
        self._save()
        self._update_snapshot(now_ns)

    # ── budget ────────────────────────────────────────────────────────────
    def clips(self, day_str: str) -> list[dict[str, Any]]:
        """Sidecars of the clips kept for one UTC day."""
        out = []
        directory = self.root / day_str
        if not directory.is_dir():
            return out
        for path in sorted(directory.glob(f"*{META_SUFFIX}")):
            try:
                meta = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(meta, dict):
                out.append(meta)
        return out

    def used_s(self, day_str: str) -> float:
        kept = sum(float(c.get("duration_s", 0) or 0) for c in self.clips(day_str))
        pending = sum(r.duration_s for r in self.pending if r.day == day_str)
        return kept + pending

    def _update_snapshot(self, now_ns: int) -> None:
        self.snapshot = {"pending": len(self.pending),
                         "today_min": round(self.used_s(day(now_ns)) / 60, 1)}

    def refresh(self, now_ns: int) -> None:
        self._update_snapshot(now_ns)

    # ── extraction ────────────────────────────────────────────────────────
    def extract(self, req: KeepRequest, now_ns: int,
                stop: threading.Event | None = None,
                clock: Callable[[], int] = time.time_ns) -> dict[str, Any]:
        """Copy the request's range from the ring into a clip. Returns the
        result message (status kept or empty); raises OSError on a disk
        problem and Interrupted when `stop` is set."""
        directory = self.root / req.day
        directory.mkdir(parents=True, exist_ok=True)
        final = unique_path(directory, f"{stamp(req.start_ns)}_{req.tag}")
        part = Path(f"{final}.part")
        header = {
            "created_ns": req.start_ns,
            "clip": {"id": req.id, "tag": req.tag, "start_ns": req.start_ns,
                     "end_ns": req.end_ns, "person": req.person, "note": req.note,
                     "source": req.source},
            "recorder": __version__,
        }
        kinds: Counter[str] = Counter()
        nodes: Counter[str] = Counter()
        seconds: set[int] = set()
        try:
            with RawWriter(part, header, self.cfg.gzip_level) as writer:
                for n, (ts_ns, data) in enumerate(
                        iter_range(list_files(self.cfg.ring_dir), req.start_ns, req.end_ns)):
                    if stop is not None and n % 1000 == 0 and stop.is_set():
                        raise Interrupted
                    writer.write(ts_ns, data)
                    kind, node = classify(data)
                    kinds[kind] += 1
                    if node is not None:
                        nodes[str(node)] += 1
                    seconds.add(ts_ns // NS)
                records, first_ns, last_ns = writer.records, writer.first_ns, writer.last_ns
        except BaseException:
            part.unlink(missing_ok=True)
            raise
        result = self.result(req, "empty" if records == 0 else "kept")
        if records == 0:
            part.unlink(missing_ok=True)
            return result
        os.replace(part, final)
        size = final.stat().st_size
        coverage = min(1.0, len(seconds) / max(req.duration_s, 1.0))
        sidecar = {
            "format": "ruview-csi-clip", "version": 1,
            "file": final.name, "id": req.id, "tag": req.tag,
            "person": req.person, "note": req.note, "source": req.source,
            "start": iso(req.start_ns), "end": iso(req.end_ns),
            "start_ns": req.start_ns, "end_ns": req.end_ns,
            "duration_s": round(req.duration_s, 3),
            "first_ns": first_ns, "last_ns": last_ns,
            "records": records, "bytes": size, "coverage": round(coverage, 3),
            "kinds": dict(kinds), "nodes": dict(nodes),
            "requested_ns": req.received_ns, "written_ns": clock(),
            "recorder": __version__,
        }
        meta_path = directory / (final.name[:-len(SUFFIX)] + META_SUFFIX)
        _write_json_atomic(meta_path, sidecar)
        LOG.info("kept %s: %d datagrams, %.1f MB, %.0f%% of seconds with data",
                 final.name, records, size / 1e6, coverage * 100)
        result.update(file=str(final.relative_to(self.cfg.data_dir)), records=records,
                      bytes=size, coverage=round(coverage, 3),
                      kinds=dict(kinds), nodes=dict(nodes))
        return result

    @staticmethod
    def result(req: KeepRequest | None, status: str, error: str | None = None,
               **extra: Any) -> dict[str, Any]:
        out: dict[str, Any] = {"status": status, "ts": round(time.time(), 3)}
        if req is not None:
            out.update(id=req.id, tag=req.tag, start=iso(req.start_ns), end=iso(req.end_ns),
                       duration_s=round(req.duration_s, 3), person=req.person,
                       source=req.source)
        if error:
            out["error"] = error
        out.update(extra)
        return out
