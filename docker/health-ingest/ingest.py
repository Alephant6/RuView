#!/usr/bin/env python3
"""Apple Health (Health Auto Export) -> InfluxDB receiver.

Each household member's iPhone POSTs here, either from an iOS Shortcuts
automation (free; POST /shortcut, one sample per line) or from a Health
Auto Export "REST API" automation (POST /, HAE JSON). The Health app's
full XML export can be backfilled with import_export.py. The bearer token identifies the person,
so the stored points carry a `person` tag that lines up with RuView's
profile names. Points go straight into InfluxDB as line protocol, using
the sample's own timestamp, so late or repeated syncs land in the right
place and overlapping uploads overwrite rather than duplicate.

Measurements written:
  apple_health       one point per sample; tags person, metric, unit, source;
                     fields qty, or min/avg/max (heart_rate), or
                     systolic/diastolic, etc.
  apple_sleep_stage  one point per sleep segment (Summarize Data off);
                     tags person, stage, source; fields duration_h, end_ts
  apple_sleep_daily  one point per night (Summarize Data on); fields in hours
  apple_workout      one point per workout; tags person, name; fields
                     duration_s and any numeric summary values

Stdlib only. Configured by environment variables:
  HEALTH_TOKENS    "person:token,person2:token2" (required)
  INFLUXDB_URL     default http://influxdb:8086
  INFLUXDB_TOKEN, INFLUXDB_ORG, INFLUXDB_BUCKET
  LISTEN_PORT      default 8095
"""

from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable

LOG = logging.getLogger("health-ingest")

INFLUX_URL = os.getenv("INFLUXDB_URL", "http://influxdb:8086").rstrip("/")
INFLUX_TOKEN = os.getenv("INFLUXDB_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUXDB_ORG", "ruview")
INFLUX_BUCKET = os.getenv("INFLUXDB_BUCKET", "sensing")
LISTEN_PORT = int(os.getenv("LISTEN_PORT", "8095"))
MAX_BODY = 64 * 1024 * 1024
WRITE_CHUNK = 5000


def parse_tokens(raw: str) -> dict[str, str]:
    """'sq:abc,ml:def' -> {'abc': 'sq', 'def': 'ml'}"""
    out: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        person, sep, token = item.partition(":")
        if not sep or not person.strip() or len(token.strip()) < 16:
            raise ValueError(f"bad HEALTH_TOKENS entry for {person!r}: need person:token, token >= 16 chars")
        out[token.strip()] = person.strip()
    return out


# ---------------------------------------------------------------------------
# Line protocol
# ---------------------------------------------------------------------------
def _esc_tag(v: str) -> str:
    return re.sub(r"([,= ])", r"\\\1", str(v).replace("\\", "\\\\")).replace("\n", " ")


def _esc_str(v: str) -> str:
    return '"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _field(v: Any) -> str | None:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        if v != v or v in (float("inf"), float("-inf")):
            return None
        return repr(float(v))
    if isinstance(v, str):
        return _esc_str(v)
    return None


def line(measurement: str, tags: dict[str, Any], fields: dict[str, Any], ts: int) -> str | None:
    fs = []
    for k, v in fields.items():
        fv = _field(v)
        if fv is not None:
            fs.append(f"{_esc_tag(k)}={fv}")
    if not fs:
        return None
    ts_part = "".join(f",{_esc_tag(k)}={_esc_tag(v)}" for k, v in sorted(tags.items()) if v not in (None, ""))
    return f"{_esc_tag(measurement)}{ts_part} {','.join(fs)} {ts}"


def parse_date(s: Any) -> int | None:
    """'2026-10-15 03:12:00 +0800' -> unix seconds."""
    if not isinstance(s, str):
        return None
    s = s.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z"):
        try:
            return int(datetime.strptime(s, fmt).timestamp())
        except ValueError:
            pass
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(dt.timestamp()) if dt.tzinfo else None


def _snake(k: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", k).lower()


# ---------------------------------------------------------------------------
# Payload -> points
# ---------------------------------------------------------------------------
_TS_KEYS = {"date", "startDate", "endDate", "sleepStart", "sleepEnd", "inBedStart", "inBedEnd"}


def metric_lines(person: str, metric: dict[str, Any]) -> Iterable[str]:
    name = str(metric.get("name", "unknown"))
    units = str(metric.get("units", ""))
    for dp in metric.get("data") or []:
        if not isinstance(dp, dict):
            continue
        source = dp.get("source", "")
        if name == "sleep_analysis":
            yield from _sleep_lines(person, units, dp, source)
            continue
        ts = parse_date(dp.get("date")) or parse_date(dp.get("startDate"))
        if ts is None:
            continue
        fields: dict[str, Any] = {}
        for k, v in dp.items():
            if k in _TS_KEYS or k == "source":
                continue
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                fields[_snake(k)] = v
            elif isinstance(v, str) and k in ("value", "mealTime", "reason"):
                fields[_snake(k)] = v
        end = parse_date(dp.get("endDate"))
        if end is not None:
            fields["end_ts"] = end
        l = line("apple_health", {"person": person, "metric": name, "unit": units, "source": source}, fields, ts)
        if l:
            yield l


def _sleep_lines(person: str, units: str, dp: dict[str, Any], source: str) -> Iterable[str]:
    if "startDate" in dp and "value" in dp:
        ts = parse_date(dp.get("startDate"))
        end = parse_date(dp.get("endDate"))
        if ts is None:
            return
        fields = {"duration_h": dp.get("qty")}
        if end is not None:
            fields["end_ts"] = end
            if not isinstance(fields["duration_h"], (int, float)):
                fields["duration_h"] = (end - ts) / 3600.0
        l = line("apple_sleep_stage", {"person": person, "stage": dp.get("value"), "source": source}, fields, ts)
        if l:
            yield l
        return
    ts = parse_date(dp.get("sleepStart")) or parse_date(dp.get("inBedStart")) or parse_date(dp.get("date"))
    if ts is None:
        return
    fields = {_snake(k): v for k, v in dp.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}
    for k in ("sleepEnd", "inBedStart", "inBedEnd"):
        t = parse_date(dp.get(k))
        if t is not None:
            fields[_snake(k) + "_ts"] = t
    l = line("apple_sleep_daily", {"person": person, "source": source}, fields, ts)
    if l:
        yield l


def workout_lines(person: str, w: dict[str, Any]) -> Iterable[str]:
    ts = parse_date(w.get("start"))
    if ts is None:
        return
    fields: dict[str, Any] = {}
    end = parse_date(w.get("end"))
    if end is not None:
        fields["end_ts"] = end
        fields["duration_s"] = end - ts
    for k, v in w.items():
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            fields[_snake(k)] = v
        elif isinstance(v, dict) and isinstance(v.get("qty"), (int, float)):
            fields[_snake(k)] = v["qty"]
    l = line("apple_workout", {"person": person, "name": w.get("name", "")}, fields, ts)
    if l:
        yield l


# Sleep stage names as they appear in HAE, Shortcuts (English or Chinese UI)
# and the Health app's XML export, mapped to HAE's spelling.
_STAGES = {
    "core": "Core", "asleepcore": "Core", "核心": "Core", "核心睡眠": "Core",
    "deep": "Deep", "asleepdeep": "Deep", "深度": "Deep", "深度睡眠": "Deep",
    "rem": "REM", "asleeprem": "REM", "快速动眼": "REM", "快速动眼睡眠": "REM", "快速眼动": "REM",
    "awake": "Awake", "清醒": "Awake", "醒着": "Awake",
    "inbed": "In Bed", "卧床": "In Bed", "在床上": "In Bed",
    "asleep": "Asleep", "asleepunspecified": "Asleep", "睡眠": "Asleep", "已入睡": "Asleep",
}


def normalize_stage(v: Any) -> str:
    raw = str(v or "").strip()
    key = raw.replace("HKCategoryValueSleepAnalysis", "").replace(" ", "").lower()
    return _STAGES.get(key, raw or "Unspecified")


def sample_line(person: str, metric: str, start: int, end: int | None, value: str,
                unit: str, source: str) -> str | None:
    """One raw sample (Shortcuts or XML export) in the same schema HAE uses."""
    if metric == "sleep_analysis":
        fields: dict[str, Any] = {}
        if end is not None:
            fields["end_ts"] = end
            fields["duration_h"] = (end - start) / 3600.0
        return line("apple_sleep_stage", {"person": person, "stage": normalize_stage(value), "source": source},
                    fields, start)
    try:
        qty = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    fields = {"qty": qty}
    if end is not None and end != start:
        fields["end_ts"] = end
    return line("apple_health", {"person": person, "metric": metric, "unit": unit, "source": source},
                fields, start)


_METRIC_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def shortcut_lines(person: str, text: str) -> list[str]:
    """Parse the iOS Shortcuts upload: one sample per line,
    metric<TAB>start<TAB>end<TAB>value<TAB>unit<TAB>source (dates ISO 8601).
    Separators may also be '|' since tabs are awkward to type in Shortcuts."""
    out: list[str] = []
    for raw in text.splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        parts = [p.strip() for p in (raw.split("\t") if "\t" in raw else raw.split("|"))]
        parts += [""] * (6 - len(parts))
        metric, start_s, end_s, value, unit, source = parts[:6]
        start = parse_date(start_s)
        if not _METRIC_RE.match(metric) or start is None:
            continue
        l = sample_line(person, metric, start, parse_date(end_s), value, unit, source)
        if l:
            out.append(l)
    return out


def payload_lines(person: str, payload: dict[str, Any]) -> list[str]:
    data = payload.get("data", payload)
    out: list[str] = []
    for m in data.get("metrics") or []:
        if isinstance(m, dict):
            out.extend(metric_lines(person, m))
    for w in data.get("workouts") or []:
        if isinstance(w, dict):
            out.extend(workout_lines(person, w))
    return out


# ---------------------------------------------------------------------------
# InfluxDB + HTTP
# ---------------------------------------------------------------------------
def write_influx(lines: list[str]) -> None:
    q = urllib.parse.urlencode({"org": INFLUX_ORG, "bucket": INFLUX_BUCKET, "precision": "s"})
    for i in range(0, len(lines), WRITE_CHUNK):
        body = "\n".join(lines[i:i + WRITE_CHUNK]).encode()
        req = urllib.request.Request(
            f"{INFLUX_URL}/api/v2/write?{q}", data=body, method="POST",
            headers={"Authorization": f"Token {INFLUX_TOKEN}", "Content-Type": "text/plain; charset=utf-8"},
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp.read()


class Handler(BaseHTTPRequestHandler):
    tokens: dict[str, str] = {}

    def _send(self, code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self._send(200, {"ok": True})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        auth = self.headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        person = self.tokens.get(token)
        if not person:
            self._send(401, {"error": "unknown token"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > MAX_BODY:
            self._send(413 if length > MAX_BODY else 400, {"error": "bad length"})
            return
        try:
            body = self.rfile.read(length)
            if self.path.rstrip("/").endswith("/shortcut"):
                lines = shortcut_lines(person, body.decode("utf-8-sig"))
            else:
                lines = payload_lines(person, json.loads(body))
        except (ValueError, AttributeError) as e:
            self._send(400, {"error": f"bad body: {e}"})
            return
        try:
            if lines:
                write_influx(lines)
        except (urllib.error.URLError, OSError) as e:
            LOG.error("influx write failed for %s: %s", person, e)
            self._send(502, {"error": "influx write failed"})
            return
        LOG.info("%s: %d points (%s, %s)", person, len(lines),
                 self.headers.get("automation-name", "?"), self.headers.get("automation-period", "?"))
        self._send(200, {"ok": True, "person": person, "points": len(lines)})

    def log_message(self, fmt: str, *args: Any) -> None:
        LOG.debug(fmt, *args)


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    Handler.tokens = parse_tokens(os.getenv("HEALTH_TOKENS", ""))
    if not Handler.tokens:
        raise SystemExit("HEALTH_TOKENS is empty; set person:token pairs in docker/.env")
    LOG.info("listening on :%d for %s", LISTEN_PORT, ", ".join(sorted(Handler.tokens.values())))
    ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
