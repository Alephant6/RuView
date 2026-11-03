"""Configuration, schedule, status and the steps of a backup run.

pg_dump, psql, pg_dumpall and restic are replaced by small scripts on PATH,
InfluxDB by a local HTTP server; the real tools run in
.github/workflows/backup.yml. Run from docker/backup:  python -m pytest -q
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import textwrap
import threading
import types
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ruview_backup import jobs, service  # noqa: E402
from ruview_backup.config import Config, ConfigError  # noqa: E402

SECRET = "s" * 16
BASE_ENV = {"RESTIC_REPOSITORY": "/repo", "RESTIC_PASSWORD": SECRET, "PG_BACKUP_PASSWORD": SECRET}


def ts(text: str) -> float:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()


@pytest.fixture(autouse=True)
def no_proxy(monkeypatch):
    for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY"):
        monkeypatch.delenv(name, raising=False)


# ── configuration ────────────────────────────────────────────────────────

def test_defaults():
    cfg = Config.from_env(BASE_ENV)
    assert cfg.at_utc == (7, 30)
    assert (cfg.keep_daily, cfg.keep_weekly, cfg.keep_monthly) == (7, 5, 6)
    assert cfg.data_dir == "/backup"
    assert cfg.pg_user == "backup" and cfg.pg_host == "timescaledb"
    assert cfg.status_topic == "ruview/backup/state"
    assert not cfg.health_enabled  # no INFLUXDB_BACKUP_TOKEN
    cfg.require_restic()
    cfg.require_database()


@pytest.mark.parametrize("name,value", [
    ("BACKUP_AT", "7:30"),
    ("BACKUP_AT", "24:00"),
    ("BACKUP_KEEP_DAILY", "-1"),
    ("BACKUP_KEEP_WEEKLY", "five"),
    ("BACKUP_CHECK_SUBSET", "5 %"),
    ("BACKUP_CHECK_SUBSET", "0/5"),
    ("BACKUP_DIR", "data/backup"),
    ("BACKUP_HOST", "ruview nas"),
    # These names go into the Flux query.
    ("INFLUXDB_BUCKET", 'sensing") |> drop(columns: ["_value'),
    ("INFLUXDB_ORG", "ruview org"),
    ("HEALTH_MEASUREMENTS", 'apple_health,apple"x'),
    ("INFLUXDB_URL", "http://influxdb:8086/api/v2"),
    ("INFLUXDB_URL", "ftp://influxdb"),
    ("MQTT_PREFIX", "ruview/#"),
    ("PGHOST", "timescaledb;rm"),
    ("PGPORT", "70000"),
])
def test_invalid_values(name, value):
    with pytest.raises(ConfigError, match=name):
        Config.from_env({**BASE_ENV, name: value})


def test_nothing_kept_is_refused():
    env = {**BASE_ENV, "BACKUP_KEEP_DAILY": "0", "BACKUP_KEEP_WEEKLY": "0", "BACKUP_KEEP_MONTHLY": "0"}
    with pytest.raises(ConfigError, match="nothing would be kept"):
        Config.from_env(env)


def test_restic_settings_checked_when_needed():
    cfg = Config.from_env({**BASE_ENV, "RESTIC_PASSWORD": "short"})
    with pytest.raises(ConfigError, match="RESTIC_PASSWORD"):
        cfg.require_restic()
    Config.from_env({**BASE_ENV, "RESTIC_PASSWORD": "", "RESTIC_PASSWORD_FILE": "/run/secrets/x"}).require_restic()
    with pytest.raises(ConfigError, match="RESTIC_REPOSITORY"):
        Config.from_env({**BASE_ENV, "RESTIC_REPOSITORY": ""}).require_restic()
    with pytest.raises(ConfigError, match="PG_BACKUP_PASSWORD"):
        Config.from_env({**BASE_ENV, "PG_BACKUP_PASSWORD": "short"}).require_database()


def test_health_export_switch():
    cfg = Config.from_env({**BASE_ENV, "INFLUXDB_BACKUP_TOKEN": "tkn"})
    assert cfg.health_enabled
    assert cfg.health_measurements == ("apple_health", "apple_sleep_stage", "apple_sleep_daily", "apple_workout")
    cfg = Config.from_env({**BASE_ENV, "INFLUXDB_BACKUP_TOKEN": "tkn", "HEALTH_MEASUREMENTS": " apple_health , "})
    assert cfg.health_measurements == ("apple_health",)
    assert not Config.from_env({**BASE_ENV, "INFLUXDB_BACKUP_TOKEN": "tkn", "HEALTH_MEASUREMENTS": ""}).health_enabled


# ── schedule and status ──────────────────────────────────────────────────

def test_next_run():
    at = (7, 30)
    assert service.next_run(ts("2026-10-26T07:29:59"), at) == ts("2026-10-26T07:30:00")
    assert service.next_run(ts("2026-10-26T07:30:00"), at) == ts("2026-10-27T07:30:00")
    assert service.next_run(ts("2026-10-31T23:00:00"), at) == ts("2026-11-01T07:30:00")


def test_first_run_catches_up_a_missed_night():
    now = ts("2026-10-26T12:00:00")
    scheduled = ts("2026-10-27T07:30:00")
    soon = now + service.STARTUP_DELAY_S
    assert service.first_run(service.Status(since_ts=now), now, (7, 30)) == soon
    assert service.first_run(service.Status(since_ts=0, last_ok_ts=now - 27 * 3600), now, (7, 30)) == soon
    assert service.first_run(service.Status(since_ts=0, last_ok_ts=now - 5 * 3600), now, (7, 30)) == scheduled
    # A scheduled time sooner than the catch-up delay stays.
    early = ts("2026-10-26T07:25:00")
    assert service.first_run(service.Status(since_ts=0), early, (7, 30)) == ts("2026-10-26T07:30:00")


def test_status_file(tmp_path):
    path = tmp_path / "status.json"
    status = service.Status.load(path, 100.0)
    assert status == service.Status(since_ts=100.0)
    status.ok, status.snapshot, status.last_ok_ts = True, "0123abcd", 150.0
    status.save(path)
    assert service.Status.load(path, 200.0) == status
    assert not (tmp_path / "status.tmp").exists()

    path.write_text(json.dumps({**json.loads(path.read_text()), "unknown": 1}))
    assert service.Status.load(path, 200.0) == status
    for broken in ("{not json", "[1, 2]", '{"since_ts": "x"}', '{"since_ts": 1, "ok": "yes"}',
                   '{"since_ts": 1, "error": {"a": 1}}', '{"since_ts": 1, "db_bytes": true}'):
        path.write_text(broken)
        assert service.Status.load(path, 300.0) == service.Status(since_ts=300.0), broken
    path.write_text('{"ok": true, "last_ok_ts": 350}')
    assert service.Status.load(path, 400.0) == service.Status(since_ts=400.0, ok=True, last_ok_ts=350)


def test_payload_and_redaction():
    cfg = Config.from_env({**BASE_ENV, "INFLUXDB_BACKUP_TOKEN": "tkn"})
    payload = service.Status(since_ts=1.0, ok=True).payload(cfg, 5.0, 9.0)
    assert payload["ok"] is True and payload["ts"] == 5.0 and payload["next_run_ts"] == 9.0
    assert payload["health"] is True and payload["version"]
    assert "tkn" not in json.dumps(payload)
    assert service._redact("rest:https://user:pw@nas:8000/ruview") == "rest:https://***@nas:8000/ruview"
    b2 = "s3:https://s3.us-west-004.backblazeb2.com/home-backup/ruview"
    assert service._redact(b2) == b2
    assert service._redact("/srv/restic") == "/srv/restic"


# ── one run, with the steps replaced ─────────────────────────────────────

class FakeSteps:
    def __init__(self, monkeypatch, fail: dict[str, Exception] | None = None):
        self.calls: list[str] = []
        self.fail = fail or {}
        for name, result in (
            ("prepare", None),
            ("check_repository", None),
            ("make_dump", {"db_bytes": 1234, "health_rows": {"apple_health": 10, "apple_workout": 2}}),
            ("upload", {"message_type": "summary", "snapshot_id": "0123456789abcdef", "data_added_packed": 77}),
            ("forget", None),
            ("check", None),
        ):
            monkeypatch.setattr(jobs, name, self._step(name, result))

    def _step(self, name, result):
        def step(*args, **kwargs):
            self.calls.append(name)
            if name in self.fail:
                raise self.fail[name]
            return result
        return step


class Clock:
    def __init__(self, start: float):
        self.now = start

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


def make_cfg(data_dir: Path, **over) -> Config:
    return Config(**{"data_dir": str(data_dir), "repository": "/repo", "pg_password": SECRET, **over})


def test_run_success(tmp_path, monkeypatch):
    steps = FakeSteps(monkeypatch)
    cfg = make_cfg(tmp_path)
    status = service.run_once(cfg, Clock(1000.0))
    assert steps.calls == ["prepare", "check_repository", "make_dump", "upload", "forget", "check"]
    assert status.ok is True and status.step is None and status.error is None
    assert status.snapshot == "01234567"
    assert status.db_bytes == 1234 and status.health_rows == 12 and status.added_bytes == 77
    assert status.last_ok_ts == status.last_check_ts > status.last_run_ts
    assert service.Status.load(tmp_path / "status.json", 0.0) == status


def test_repository_checked_weekly(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    service.Status(since_ts=0.0, last_check_ts=1000.0 - 86400).save(tmp_path / "status.json")
    steps = FakeSteps(monkeypatch)
    service.run_once(cfg, Clock(1000.0))
    assert "check" not in steps.calls

    service.Status(since_ts=0.0, last_check_ts=1000.0 - 7 * 86400).save(tmp_path / "status.json")
    steps = FakeSteps(monkeypatch)
    service.run_once(cfg, Clock(1000.0))
    assert steps.calls[-1] == "check"


def test_failed_step_keeps_the_last_success(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    service.Status(since_ts=0.0, last_ok_ts=500.0, ok=True, snapshot="aaaabbbb").save(tmp_path / "status.json")
    steps = FakeSteps(monkeypatch, fail={"make_dump": jobs.StepError("pg_dump", "connection refused")})
    status = service.run_once(cfg, Clock(1000.0))
    assert steps.calls == ["prepare", "check_repository", "make_dump"]
    assert (status.ok, status.step, status.error) == (False, "pg_dump", "connection refused")
    assert status.last_ok_ts == 500.0 and status.snapshot == "aaaabbbb"
    assert status.last_run_ts > 1000.0

    steps = FakeSteps(monkeypatch, fail={"upload": KeyError("x")})
    status = service.run_once(cfg, Clock(2000.0))
    assert (status.ok, status.step) == (False, "internal")
    assert "KeyError" in status.error


def test_one_run_at_a_time(tmp_path, monkeypatch):
    steps = FakeSteps(monkeypatch)
    cfg = make_cfg(tmp_path)
    with service.exclusive(cfg) as mine:
        assert mine
        assert service.run_once(cfg, Clock(1000.0)) is None
    assert steps.calls == []
    assert service.run_once(cfg, Clock(1000.0)).ok


def test_publish_status(tmp_path, monkeypatch):
    import paho.mqtt

    sent = []
    publish = types.ModuleType("paho.mqtt.publish")
    publish.single = lambda topic, payload, **kw: sent.append((topic, json.loads(payload), kw))
    monkeypatch.setitem(sys.modules, "paho.mqtt.publish", publish)
    monkeypatch.setattr(paho.mqtt, "publish", publish, raising=False)
    status = service.Status(since_ts=1.0, ok=False, step="upload", error="timeout")

    service.publish_status(make_cfg(tmp_path), status, 9.0)
    assert sent == []  # MQTT_HOST empty

    service.publish_status(make_cfg(tmp_path, mqtt_host="mosquitto"), status, 9.0)
    topic, payload, kw = sent[0]
    assert topic == "ruview/backup/state"
    assert payload["ok"] is False and payload["step"] == "upload" and payload["next_run_ts"] == 9.0
    assert kw["retain"] is True and kw["qos"] == 1 and kw["hostname"] == "mosquitto"

    def down(*args, **kwargs):
        raise ConnectionRefusedError("down")
    publish.single = down
    service.publish_status(make_cfg(tmp_path, mqtt_host="mosquitto"), status, 9.0)  # logged, not raised


def test_main(tmp_path, monkeypatch, capsys):
    for name in ("RESTIC_REPOSITORY", "RESTIC_PASSWORD", "RESTIC_PASSWORD_FILE", "PG_BACKUP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path))
    assert service.main(["run"]) == 2
    assert service.main(["status"]) == 0
    assert "no backup has run yet" in capsys.readouterr().out


# ── the steps, with fake tools on PATH ───────────────────────────────────

FAKE_TOOLS = {
    "psql": """
        print("17.11|2.30.2")
    """,
    "pg_dump": """
        record("pg_dump")
        target = Path(next(a for a in sys.argv if a.startswith("--file=")).split("=", 1)[1])
        target.mkdir()
        (target / "toc.dat").write_bytes(b"toc" * 10)
        (target / "3001.dat").write_bytes(b"rows" * 100)
    """,
    "pg_dumpall": """
        print("CREATE ROLE backup;")
    """,
    "restic": """
        record("restic")
        cmd = sys.argv[1]
        if cmd == "cat" and (STATE / "no-repo").exists():
            sys.stderr.write("Fatal: repository does not exist: unable to open config file\\n"
                             "Is there a repository at the following location?\\n")
            sys.exit(10)
        if cmd == "check" and (STATE / "check-fails").exists():
            sys.stderr.write("pack 0123: not referenced in any index\\nFatal: repository contains errors\\n")
            sys.exit(1)
        if cmd == "backup":
            print(json.dumps({"message_type": "status", "percent_done": 1}))
            print(json.dumps({"message_type": "summary", "snapshot_id": "feedc0de" * 8,
                              "data_added": 999, "data_added_packed": 333}))
    """,
}


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """Fake pg and restic tools; each records its arguments and environment."""
    bin_dir, state = tmp_path / "bin", tmp_path / "state"
    bin_dir.mkdir()
    state.mkdir()
    header = textwrap.dedent(f"""\
        #!{sys.executable}
        import json, os, sys
        from pathlib import Path
        STATE = Path({str(state)!r})

        def record(name):
            with open(STATE / (name + ".jsonl"), "a") as fh:
                fh.write(json.dumps({{"args": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
        """)
    for name, body in FAKE_TOOLS.items():
        path = bin_dir / name
        path.write_text(header + textwrap.dedent(body))
        path.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    # Secrets the parent holds; each tool may see only its own.
    monkeypatch.setenv("RESTIC_PASSWORD", SECRET)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    monkeypatch.setenv("PG_BACKUP_PASSWORD", "pg-secret-0000000")
    monkeypatch.setenv("INFLUXDB_BACKUP_TOKEN", "influx-secret")

    def calls(name):
        path = state / f"{name}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    return types.SimpleNamespace(state=state, calls=calls)


def test_dump_upload_and_prune(tmp_path, tools):
    data = tmp_path / "data"
    cfg = make_cfg(data, pg_password="pg-secret-0000000")
    jobs.prepare(cfg, None)
    manifest = jobs.make_dump(cfg, ts("2026-10-26T07:30:00"))
    assert manifest["server_version"] == "17.11" and manifest["timescaledb_version"] == "2.30.2"
    assert manifest["db_bytes"] == 430 and "health_rows" not in manifest
    dump = data / "dump"
    assert json.loads((dump / "manifest.json").read_text()) == manifest
    assert (dump / "roles.sql").read_text() == "CREATE ROLE backup;\n"

    # The next night's dump replaces it; nothing else is left behind.
    jobs.prepare(cfg, manifest["db_bytes"])
    jobs.make_dump(cfg, ts("2026-10-27T07:30:00"))
    assert sorted(p.name for p in data.iterdir()) == ["cache", "dump", "tmp"]

    summary = jobs.upload(cfg)
    assert summary["data_added_packed"] == 333
    jobs.forget(cfg)
    jobs.check(cfg)
    backup, forget, check = tools.calls("restic")
    assert backup["args"] == ["backup", "--json", "--quiet", "--host", "ruview", "--tag", "nightly", str(dump)]
    assert forget["args"] == ["forget", "--quiet", "--prune", "--host", "ruview", "--tag", "nightly",
                              "--keep-daily", "7", "--keep-weekly", "5", "--keep-monthly", "6"]
    assert check["args"] == ["check", "--read-data-subset", "5%"]

    # Each tool gets its own secrets and nothing else.
    restic_env = backup["env"]
    assert restic_env["RESTIC_PASSWORD"] == SECRET and restic_env["AWS_SECRET_ACCESS_KEY"] == "aws-secret"
    assert restic_env["RESTIC_CACHE_DIR"] == str(data / "cache") and restic_env["TMPDIR"] == str(data / "tmp")
    for pg_call in tools.calls("pg_dump"):
        assert pg_call["env"]["PGPASSWORD"] == "pg-secret-0000000" and pg_call["env"]["PGUSER"] == "backup"
    leaked = {"RESTIC_PASSWORD", "AWS_SECRET_ACCESS_KEY", "INFLUXDB_BACKUP_TOKEN", "PG_BACKUP_PASSWORD"}
    assert not leaked & set(tools.calls("pg_dump")[0]["env"])
    assert not {"PGPASSWORD", "PG_BACKUP_PASSWORD", "INFLUXDB_BACKUP_TOKEN"} & set(restic_env)


def test_missing_repository_and_failed_check(tmp_path, tools):
    cfg = make_cfg(tmp_path)
    (tools.state / "no-repo").touch()
    with pytest.raises(jobs.StepError, match="docker compose run --rm backup init") as err:
        jobs.check_repository(cfg)
    assert err.value.step == "repository"
    (tools.state / "no-repo").unlink()
    jobs.check_repository(cfg)
    assert [c["args"][0] for c in tools.calls("restic")] == ["cat", "cat", "unlock"]

    (tools.state / "check-fails").touch()
    with pytest.raises(jobs.StepError, match="repository contains errors"):
        jobs.check(cfg)


def test_run_command_errors(tmp_path):
    with pytest.raises(jobs.StepError, match="not installed"):
        jobs.run_command([str(tmp_path / "nope")], {}, "x", 5)
    script = "import sys; sys.stderr.write('one\\ntwo\\nthree\\nfour\\n'); sys.exit(3)"
    with pytest.raises(jobs.StepError) as err:
        jobs.run_command([sys.executable, "-c", script], {}, "pg_dump", 5)
    assert err.value.step == "pg_dump" and err.value.message == "two | three | four"
    with pytest.raises(jobs.StepError, match="did not finish"):
        jobs.run_command([sys.executable, "-c", "import time; time.sleep(5)"], {}, "upload", 0.2)


def test_prepare(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    for leftover in ("dump.partial/db", "dump.old/db", "tmp/restic-temp-pack"):
        (tmp_path / leftover).mkdir(parents=True)
    (tmp_path / "tmp" / "loose").write_text("x")
    (tmp_path / "dump").mkdir()
    jobs.prepare(cfg, None)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cache", "dump", "tmp"]
    assert list((tmp_path / "tmp").iterdir()) == []

    usage = types.SimpleNamespace(total=10 << 30, used=0, free=3 << 30)
    monkeypatch.setattr(jobs.shutil, "disk_usage", lambda path: usage)
    jobs.prepare(cfg, 1 << 30)
    with pytest.raises(jobs.StepError, match="only 3.0 GiB free") as err:
        jobs.prepare(cfg, 2 << 30)
    assert err.value.step == "disk"


# ── health export, against a fake InfluxDB ───────────────────────────────

CSV = (
    "#datatype,string,long,dateTime:RFC3339,double,string,string,string\r\n"
    "#group,false,false,false,false,true,true,true\r\n"
    "#default,_result,,,,,,\r\n"
    ",result,table,_time,_value,_field,_measurement,person\r\n"
    ",,0,2026-10-25T01:00:00Z,61,qty,apple_health,sq\r\n"
    ",,0,2026-10-25T01:01:00Z,62,qty,apple_health,sq\r\n"
    "\r\n"
    "#datatype,string,long,dateTime:RFC3339,double,string,string,string\r\n"
    "#group,false,false,false,false,true,true,true\r\n"
    "#default,_result,,,,,,\r\n"
    ",result,table,_time,_value,_field,_measurement,person\r\n"
    ",,1,2026-10-25T01:00:00Z,58,qty,apple_health,ml\r\n"
    "\r\n"
)


@pytest.fixture
def influx():
    requests = []
    answer = {"code": 200, "body": CSV.encode()}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            requests.append({"path": self.path, "headers": dict(self.headers), "body": json.loads(body)})
            self.send_response(answer["code"])
            self.send_header("Content-Type", "text/csv" if answer["code"] == 200 else "application/json")
            self.end_headers()
            self.wfile.write(answer["body"])

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield types.SimpleNamespace(url=f"http://127.0.0.1:{server.server_port}", requests=requests, answer=answer)
    server.shutdown()
    server.server_close()


def test_export_measurement(tmp_path, influx):
    cfg = make_cfg(tmp_path, influx_url=influx.url, influx_token="tkn")
    out = tmp_path / "apple_health.csv"
    assert jobs.export_measurement(cfg, "apple_health", out) == 3
    assert out.read_text(newline="") == CSV
    request = influx.requests[0]
    assert request["path"] == "/api/v2/query?org=ruview"
    assert request["headers"]["Authorization"] == "Token tkn"
    assert request["body"]["dialect"]["annotations"] == ["datatype", "group", "default"]
    query = request["body"]["query"]
    assert 'from(bucket: "sensing")' in query
    assert 'r._measurement == "apple_health"' in query
    assert 'drop(columns: ["_start", "_stop"])' in query


def test_export_health_writes_every_measurement(tmp_path, influx):
    cfg = make_cfg(tmp_path, influx_url=influx.url, influx_token="tkn",
                   health_measurements=("apple_health", "apple_workout"))
    target = tmp_path / "dump.partial"
    target.mkdir()
    assert jobs.export_health(cfg, target) == {"apple_health": 3, "apple_workout": 3}
    assert sorted(p.name for p in (target / "health").iterdir()) == ["apple_health.csv", "apple_workout.csv"]


def test_export_errors(tmp_path, influx):
    cfg = make_cfg(tmp_path, influx_url=influx.url, influx_token="wrong")
    influx.answer.update(code=401, body=b'{"code":"unauthorized","message":"unauthorized access"}')
    with pytest.raises(jobs.StepError, match="401 for apple_health: unauthorized access") as err:
        jobs.export_measurement(cfg, "apple_health", tmp_path / "x.csv")
    assert err.value.step == "health"

    closed = make_cfg(tmp_path, influx_url="http://127.0.0.1:9", influx_token="tkn")
    with pytest.raises(jobs.StepError, match="cannot query InfluxDB at http://127.0.0.1:9"):
        jobs.export_measurement(closed, "apple_health", tmp_path / "y.csv")
