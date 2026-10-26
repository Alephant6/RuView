"""Schedule, status and command line of the backup service.

    python -m ruview_backup [serve]   run every night at BACKUP_AT (UTC)
    python -m ruview_backup run       one backup now
    python -m ruview_backup init      create the restic repository
    python -m ruview_backup check     verify the repository now
    python -m ruview_backup status    print the last result

The result of the last run is kept in <data_dir>/status.json and published
retained on <prefix>/backup/state, where the data watchdog raises "Backup"
when the last successful run is too old.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import logging
import os
import signal
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from . import __version__, jobs
from .config import Config, ConfigError

LOG = logging.getLogger("ruview-backup")

# Right after a start, run soon if the last success is older than this.
CATCH_UP_AFTER_S = 26 * 3600
STARTUP_DELAY_S = 10 * 60
# A failed run is retried this much later, up to MAX_ATTEMPTS per night.
RETRY_AFTER_S = 3600
MAX_ATTEMPTS = 3
# The status is re-published this often, so it survives a lost retained message.
REPUBLISH_S = 3600
CHECK_EVERY_S = 7 * 86400


@dataclass
class Status:
    since_ts: float                      # first start: no success expected before
    last_run_ts: float | None = None
    last_ok_ts: float | None = None
    ok: bool | None = None               # result of the last run
    step: str | None = None              # where the last run failed
    error: str | None = None
    duration_s: float | None = None
    snapshot: str | None = None          # restic snapshot id, short form
    db_bytes: int | None = None
    health_rows: int | None = None
    added_bytes: int | None = None       # new data stored by the last upload (compressed)
    last_check_ts: float | None = None

    @classmethod
    def load(cls, path: Path, now: float) -> Status:
        try:
            data = json.loads(path.read_text())
        except FileNotFoundError:
            return cls(since_ts=now)
        except (OSError, ValueError) as exc:
            LOG.warning("ignoring unreadable %s: %s", path, exc)
            return cls(since_ts=now)
        if not isinstance(data, dict):
            LOG.warning("ignoring %s: not a JSON object", path)
            return cls(since_ts=now)
        values = {f.name: data[f.name] for f in fields(cls) if f.name in data}
        values.setdefault("since_ts", now)
        bad = sorted(name for name, value in values.items() if not _valid(name, value))
        if bad:
            LOG.warning("ignoring %s: unexpected %s", path, ", ".join(bad))
            return cls(since_ts=now)
        return cls(**values)

    def save(self, path: Path) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self), indent=2) + "\n")
        tmp.replace(path)

    def payload(self, cfg: Config, now: float, next_run_ts: float | None) -> dict[str, Any]:
        return {**asdict(self), "ts": now, "next_run_ts": next_run_ts,
                "health": cfg.health_enabled, "version": __version__}


_TEXT_FIELDS = ("step", "error", "snapshot")


def _valid(name: str, value: Any) -> bool:
    if value is None:
        return name != "since_ts"
    if name == "ok":
        return isinstance(value, bool)
    if name in _TEXT_FIELDS:
        return isinstance(value, str)
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def status_path(cfg: Config) -> Path:
    return Path(cfg.data_dir) / "status.json"


def next_run(now: float, at_utc: tuple[int, int]) -> float:
    """The next daily run time strictly after `now`."""
    current = datetime.fromtimestamp(now, timezone.utc)
    run = current.replace(hour=at_utc[0], minute=at_utc[1], second=0, microsecond=0)
    if run <= current:
        run += timedelta(days=1)
    return run.timestamp()


def first_run(status: Status, now: float, at_utc: tuple[int, int]) -> float:
    """After a start: the scheduled time, or sooner when a night was missed."""
    due = next_run(now, at_utc)
    last = status.last_ok_ts
    if last is None or now - last > CATCH_UP_AFTER_S:
        due = min(due, now + STARTUP_DELAY_S)
    return due


@contextmanager
def exclusive(cfg: Config) -> Iterator[bool]:
    """True while this process holds the run lock (a `run` started by hand
    while the service runs, or the reverse, would share dump.partial)."""
    with open(Path(cfg.data_dir) / ".lock", "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def run_once(cfg: Config, clock=time.time) -> Status | None:
    """One complete backup. The outcome is in the returned status, which is
    also saved; None when another run holds the lock. Raises OSError only
    when the data directory cannot be used at all."""
    with exclusive(cfg) as mine:
        if not mine:
            LOG.warning("another backup run is in progress; not starting a second one")
            return None
        # From the file, so a run started by hand is not overwritten.
        status = Status.load(status_path(cfg), clock())
        return _run(cfg, status, clock)


def _run(cfg: Config, status: Status, clock) -> Status:
    started = clock()
    LOG.info("backup started")
    try:
        jobs.prepare(cfg, status.db_bytes)
        jobs.check_repository(cfg)
        manifest = jobs.make_dump(cfg, started)
        summary = jobs.upload(cfg)
        jobs.forget(cfg)
        checked = False
        if status.last_check_ts is None or started - status.last_check_ts >= CHECK_EVERY_S - 3600:
            jobs.check(cfg)
            checked = True
    except jobs.StepError as exc:
        status.ok, status.step, status.error = False, exc.step, exc.message[:300]
        LOG.error("backup failed at %s: %s", exc.step, exc.message)
    except Exception as exc:  # a bug must not stop the schedule
        LOG.exception("backup failed")
        status.ok, status.step, status.error = False, "internal", f"{type(exc).__name__}: {exc}"[:300]
    else:
        finished = clock()
        status.ok, status.step, status.error = True, None, None
        status.last_ok_ts = finished
        status.snapshot = str(summary.get("snapshot_id", ""))[:8] or None
        status.db_bytes = manifest.get("db_bytes")
        rows = manifest.get("health_rows")
        status.health_rows = sum(rows.values()) if isinstance(rows, dict) else None
        status.added_bytes = summary.get("data_added_packed", summary.get("data_added"))
        if checked:
            status.last_check_ts = finished
        LOG.info("backup done: snapshot %s, dump %.1f MB, %s new bytes stored%s",
                 status.snapshot, (status.db_bytes or 0) / 1e6, status.added_bytes,
                 ", repository checked" if checked else "")
    status.last_run_ts = started
    status.duration_s = round(clock() - started, 1)
    try:
        status.save(status_path(cfg))
    except OSError as exc:
        LOG.error("cannot write %s: %s", status_path(cfg), exc)
    return status


def publish_status(cfg: Config, status: Status, next_run_ts: float | None) -> None:
    if not cfg.mqtt_host:
        return
    payload = json.dumps(status.payload(cfg, time.time(), next_run_ts))
    try:
        import paho.mqtt.publish as publish

        publish.single(cfg.status_topic, payload, qos=1, retain=True,
                       hostname=cfg.mqtt_host, port=cfg.mqtt_port, keepalive=30)
    except Exception as exc:  # the bus being down must not stop the backups
        LOG.warning("cannot publish the status to %s:%s: %s", cfg.mqtt_host, cfg.mqtt_port, exc)


def serve(cfg: Config) -> int:
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    path = status_path(cfg)
    try:
        Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
        status = Status.load(path, time.time())
        status.save(path)
    except OSError as exc:
        LOG.error("cannot write to %s (it must be writable by uid 65534): %s", cfg.data_dir, exc)
        return 1
    due = first_run(status, time.time(), cfg.at_utc)
    LOG.info("next backup at %s", _iso(due))
    publish_status(cfg, status, due)
    attempts = 0
    republish = time.time() + REPUBLISH_S
    while not stop.is_set():
        now = time.time()
        if now >= due:
            attempts += 1
            try:
                status = run_once(cfg) or Status.load(path, now)
            except OSError as exc:
                LOG.error("cannot use %s: %s", cfg.data_dir, exc)
            now = time.time()
            if status.ok or attempts >= MAX_ATTEMPTS:
                attempts = 0
                due = next_run(now, cfg.at_utc)
            else:
                due = min(next_run(now, cfg.at_utc), now + RETRY_AFTER_S)
            LOG.info("next backup at %s", _iso(due))
            publish_status(cfg, status, due)
            republish = now + REPUBLISH_S
        elif now >= republish:
            status = Status.load(path, now)
            publish_status(cfg, status, due)
            republish = now + REPUBLISH_S
        stop.wait(max(1.0, min(60.0, due - now, republish - now)))
    LOG.info("backup service stopped")
    return 0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="minutes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m ruview_backup", description=__doc__.split("\n\n")[0])
    parser.add_argument("command", nargs="?", default="serve",
                        choices=("serve", "run", "init", "check", "status"))
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    try:
        cfg = Config.from_env()
        if args.command != "status":
            cfg.require_restic()
        if args.command in ("serve", "run"):
            cfg.require_database()
    except ConfigError as exc:
        LOG.error("configuration error: %s", exc)
        return 2

    if args.command == "status":
        path = status_path(cfg)
        print(path.read_text() if path.exists() else "no backup has run yet")
        return 0
    if args.command == "init":
        try:
            print(jobs.init(cfg))
        except jobs.StepError as exc:
            LOG.error("%s", exc.message)
            return 1
        return 0
    if args.command == "check":
        try:
            jobs.check(cfg)
        except jobs.StepError as exc:
            LOG.error("repository check failed: %s", exc.message)
            return 1
        LOG.info("repository check passed")
        return 0

    LOG.info("RuView backup %s: %s at %02d:%02d UTC, keep %d daily / %d weekly / %d monthly",
             __version__, _redact(cfg.repository), *cfg.at_utc,
             cfg.keep_daily, cfg.keep_weekly, cfg.keep_monthly)
    LOG.info("  database: %s@%s:%s/%s", cfg.pg_user, cfg.pg_host, cfg.pg_port, cfg.pg_database)
    LOG.info("  health:   %s", f"{', '.join(cfg.health_measurements)} from {cfg.influx_url}"
             if cfg.health_enabled else "off (set INFLUXDB_BACKUP_TOKEN)")
    LOG.info("  status:   %s", f"{cfg.status_topic} on {cfg.mqtt_host}:{cfg.mqtt_port}"
             if cfg.mqtt_host else "not published (MQTT_HOST empty)")
    if args.command == "serve":
        return serve(cfg)
    try:
        Path(cfg.data_dir).mkdir(parents=True, exist_ok=True)
        status = run_once(cfg)
    except OSError as exc:
        LOG.error("cannot write to %s (it must be writable by uid 65534): %s", cfg.data_dir, exc)
        return 1
    if status is None:
        return 1
    publish_status(cfg, status, None)
    return 0 if status.ok else 1


def _redact(repository: str) -> str:
    """The repository URL without any credentials in it."""
    scheme, sep, rest = repository.partition("://")
    if sep and "@" in rest.split("/", 1)[0]:
        rest = "***@" + rest.split("@", 1)[1]
    return scheme + sep + rest


if __name__ == "__main__":
    sys.exit(main())
