#!/usr/bin/env python3
"""Run every query of the provisioned RuView dashboards through Grafana (WP1.3).

    GRAFANA_PASSWORD=... python3 check_dashboards.py --url http://127.0.0.1:3030 docker/grafana/dashboards

Logs in as GRAFANA_USER (default admin) with GRAFANA_PASSWORD, waits for
Grafana, checks that the TimescaleDB datasource can log in and that every
dashboard file was provisioned, then sends each panel and annotation query to
/api/ds/query over the dashboard's default time range, the way the browser
does: dashboard variables are filled in here, Grafana's macros by Grafana.

Fails on a query error, on a panel or query that does not use the TimescaleDB
datasource, and on a query that follows the time picker ($__timeFilter) but
returns no rows. Queries with a fixed window (now, last night) may be empty,
depending on the hour the check runs. Standard library only.
"""
import argparse
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DATASOURCE_UID = "ruview-timescaledb"
UNIT_MS = {"s": 1_000, "m": 60_000, "h": 3_600_000, "d": 86_400_000, "w": 604_800_000}
# Grafana's rangeutil.roundInterval: (upper bound, rounded interval), in ms.
NICE_INTERVALS = [
    (1_500, 1_000), (3_500, 2_000), (7_500, 5_000), (12_500, 10_000), (17_500, 15_000),
    (25_000, 20_000), (45_000, 30_000), (90_000, 60_000), (300_000, 120_000),
    (450_000, 300_000), (900_000, 600_000), (1_200_000, 900_000), (1_800_000, 1_200_000),
    (3_600_000, 1_800_000), (7_200_000, 3_600_000), (10_800_000, 7_200_000),
    (21_600_000, 10_800_000), (43_200_000, 21_600_000), (86_400_000, 43_200_000),
    (604_800_000, 86_400_000),
]
# The browser sends one point per pixel at most; a typical panel is ~1000 px.
RESOLUTION = 1000


class Grafana:
    def __init__(self, url, user, password):
        self.url = url.rstrip("/")
        token = base64.b64encode(f"{user}:{password}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Accept": "application/json",
                        "Content-Type": "application/json"}

    def call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=self.headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.status, json.load(resp)
        except urllib.error.HTTPError as err:
            raw = err.read()
            try:
                return err.code, json.loads(raw)
            except ValueError:
                return err.code, {"message": raw.decode(errors="replace")[:500]}

    def wait_healthy(self, seconds):
        deadline = time.monotonic() + seconds
        while True:
            try:
                status, body = self.call("GET", "/api/health")
                if status == 200 and body.get("database") == "ok":
                    return body.get("version", "?")
            except (urllib.error.URLError, OSError, ValueError):
                pass
            if time.monotonic() > deadline:
                sys.exit(f"Grafana at {self.url} was not healthy after {seconds} s")
            time.sleep(2)


def duration_ms(text):
    match = re.fullmatch(r"(\d+)([smhdw])", text or "")
    return int(match.group(1)) * UNIT_MS[match.group(2)] if match else None


def interval_ms(range_ms, min_interval):
    raw = range_ms / RESOLUTION
    nice = next((value for bound, value in NICE_INTERVALS if raw < bound), 2_592_000_000)
    return max(nice, duration_ms(min_interval) or 0)


def fill_variables(sql, variables):
    """Fill in ${name} and $name the way the SQL datasources do for a single value."""
    def replace(match):
        name = match.group(1) or match.group(2)
        if name not in variables:
            return match.group(0)
        return variables[name].replace("'", "''")
    return re.sub(r"\$\{(\w+)\}|\$(\w+)\b", replace, sql)


def walk_panels(panels):
    for panel in panels:
        yield panel
        yield from walk_panels(panel.get("panels", []))


def queries_of(board):
    """(panel title, query dict, min interval) for every panel and annotation query."""
    for panel in walk_panels(board.get("panels", [])):
        if panel.get("type") == "row":
            continue
        for target in panel.get("targets", []):
            yield panel, target
    for annotation in board.get("annotations", {}).get("list", []):
        if annotation.get("builtIn"):
            continue
        yield {"title": "annotation: " + annotation.get("name", "?"),
               "datasource": annotation.get("datasource")}, dict(annotation.get("target", {}), format="table")


def check_board(gf, path, board, failures):
    uid, title = board.get("uid"), board.get("title")
    deadline = time.monotonic() + 60
    while True:
        status, found = gf.call("GET", f"/api/dashboards/uid/{uid}")
        if status == 200:
            break
        if time.monotonic() > deadline:
            failures.append(f"{path.name}: dashboard {uid} was not provisioned (HTTP {status})")
            return
        time.sleep(2)
    # meta.provisioned stays false with allowUiUpdates; the external id names the file.
    external_id = found.get("meta", {}).get("provisionedExternalId") or ""
    if Path(external_id).name != path.name:
        failures.append(f"{path.name}: dashboard {uid} was not provisioned from this file ({external_id!r})")

    variables = {v["name"]: str(v.get("query", "")) for v in board.get("templating", {}).get("list", [])
                 if v.get("type") == "constant"}
    time_from, time_to = board["time"]["from"], board["time"]["to"]
    match = re.fullmatch(r"now-(\d+[smhdw])", time_from)
    if not match or time_to != "now":
        failures.append(f"{path.name}: unexpected default time range {time_from} to {time_to}")
        return
    range_ms = duration_ms(match.group(1))

    print(f"\n{title} ({uid}), {time_from} to {time_to}")
    for panel, target in queries_of(board):
        name = f"{title} / {panel.get('title')} / {target.get('refId', 'A')}"
        for where, ds in (("panel", panel.get("datasource")), ("query", target.get("datasource"))):
            if (ds or {}).get("uid") != DATASOURCE_UID:
                failures.append(f"{name}: the {where} uses datasource {ds}, not {DATASOURCE_UID}")
        sql = target.get("rawSql")
        if not sql:
            failures.append(f"{name}: no rawSql")
            continue
        query = {"refId": target.get("refId", "A"), "datasource": {"uid": DATASOURCE_UID},
                 "rawQuery": True, "editorMode": "code", "format": target.get("format", "time_series"),
                 "rawSql": fill_variables(sql, variables),
                 "intervalMs": interval_ms(range_ms, panel.get("interval")), "maxDataPoints": RESOLUTION}
        started = time.monotonic()
        status, resp = gf.call("POST", "/api/ds/query", {"queries": [query], "from": time_from, "to": time_to})
        took = (time.monotonic() - started) * 1000
        result = (resp.get("results") or {}).get(query["refId"])
        error = None
        if result is None:
            error = resp.get("message") or f"HTTP {status}"
        elif result.get("error"):
            error = result["error"]
        rows = 0
        for frame in (result or {}).get("frames", []):
            values = frame.get("data", {}).get("values") or [[]]
            rows += len(values[0])
        follows_picker = "$__timeFilter(" in sql
        if error:
            verdict = "ERROR"
            failures.append(f"{name}: {error}")
        elif rows == 0 and follows_picker:
            verdict = "EMPTY"
            failures.append(f"{name}: no rows over {time_from} to {time_to} although the panel follows the time picker")
        elif rows == 0:
            verdict = "empty"
        else:
            verdict = "ok"
        print(f"  {verdict:5} {rows:6d} rows {took:7.0f} ms  {panel.get('title')} [{query['refId']}]")
        if error:
            print(f"        {error}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dashboards", type=Path, help="directory with the dashboard JSON files")
    parser.add_argument("--url", default="http://127.0.0.1:3030", help="Grafana base URL")
    parser.add_argument("--wait", type=int, default=120, help="seconds to wait for Grafana")
    args = parser.parse_args()

    password = os.environ.get("GRAFANA_PASSWORD")
    if not password:
        sys.exit("set GRAFANA_PASSWORD to the Grafana admin password")
    gf = Grafana(args.url, os.environ.get("GRAFANA_USER", "admin"), password)
    print(f"Grafana {gf.wait_healthy(args.wait)} at {gf.url}")

    failures = []
    status, health = gf.call("GET", f"/api/datasources/uid/{DATASOURCE_UID}/health")
    print(f"datasource {DATASOURCE_UID}: {health.get('status')} {health.get('message')}")
    if status != 200 or health.get("status") != "OK":
        failures.append(f"datasource {DATASOURCE_UID} health: HTTP {status} {health}")

    files = sorted(args.dashboards.glob("**/*.json"))
    if not files:
        failures.append(f"no dashboard files in {args.dashboards}")
    uids = {}
    for path in files:
        board = json.loads(path.read_text())
        if board.get("uid") in uids:
            failures.append(f"{path.name}: uid {board.get('uid')} is also used by {uids[board.get('uid')]}")
        uids[board.get("uid")] = path.name
        check_board(gf, path, board, failures)

    if failures:
        print(f"\n{len(failures)} problem(s):")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)
    print(f"\nAll queries of {len(files)} dashboard(s) ran.")


if __name__ == "__main__":
    main()
