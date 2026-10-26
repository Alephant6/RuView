"""The steps of one backup run.

    <data_dir>/dump.partial   built by a run: roles.sql, db/ (pg_dump
                              directory format), health/*.csv, manifest.json
    <data_dir>/dump           the last complete dump, which restic uploads
    <data_dir>/cache, tmp     restic's cache and temporary files

A step that fails raises StepError; the previous dump stays in place and
nothing is uploaded.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config

LOG = logging.getLogger("ruview-backup.jobs")

DUMP_TIMEOUT_S = 2 * 3600
QUERY_TIMEOUT_S = 30 * 60
UPLOAD_TIMEOUT_S = 6 * 3600
PRUNE_TIMEOUT_S = 2 * 3600
CHECK_TIMEOUT_S = 3 * 3600
SHORT_TIMEOUT_S = 10 * 60
# Free space wanted before dumping: twice the last dump, at least this much.
MIN_FREE_BYTES = 1 << 30
# Tag of the snapshots this service makes and prunes.
TAG = "nightly"
# Only these variables reach the child processes.
_BASE_ENV = ("PATH", "HOME", "LANG", "TZ")
_RESTIC_ENV_PREFIXES = ("RESTIC_", "AWS_", "B2_")


class StepError(Exception):
    def __init__(self, step: str, message: str) -> None:
        super().__init__(f"{step}: {message}")
        self.step = step
        self.message = message


def _tail(text: str, lines: int = 3, limit: int = 400) -> str:
    kept = [line.strip() for line in text.strip().splitlines() if line.strip()][-lines:]
    return " | ".join(kept)[-limit:]


def run_command(args: list[str], env: dict[str, str], step: str,
                timeout: float) -> subprocess.CompletedProcess:
    LOG.debug("running %s", " ".join(args[:3]))
    try:
        proc = subprocess.run(args, env=env, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise StepError(step, f"{args[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise StepError(step, f"{args[0]} did not finish in {timeout / 60:.0f} min") from exc
    if proc.returncode != 0:
        raise StepError(step, _tail(proc.stderr) or f"{args[0]} exited with code {proc.returncode}")
    return proc


def _child_env(extra: dict[str, str]) -> dict[str, str]:
    env = {key: os.environ[key] for key in _BASE_ENV if key in os.environ}
    env.update(extra)
    return env


def pg_env(cfg: Config) -> dict[str, str]:
    return _child_env({
        "PGHOST": cfg.pg_host,
        "PGPORT": str(cfg.pg_port),
        "PGDATABASE": cfg.pg_database,
        "PGUSER": cfg.pg_user,
        "PGPASSWORD": cfg.pg_password,
        "PGAPPNAME": "ruview-backup",
        "PGCONNECT_TIMEOUT": "30",
    })


def restic_env(cfg: Config) -> dict[str, str]:
    env = _child_env({key: value for key, value in os.environ.items()
                      if key.startswith(_RESTIC_ENV_PREFIXES)})
    data = Path(cfg.data_dir)
    env.setdefault("RESTIC_CACHE_DIR", str(data / "cache"))
    env["TMPDIR"] = str(data / "tmp")
    return env


def restic(cfg: Config, args: list[str], step: str, timeout: float = SHORT_TIMEOUT_S,
           ) -> subprocess.CompletedProcess:
    return run_command(["restic", *args], restic_env(cfg), step, timeout)


# ── preparation ──────────────────────────────────────────────────────────

def prepare(cfg: Config, last_dump_bytes: int | None) -> None:
    """Create the working directories, remove what an interrupted run left
    and make sure the next dump fits."""
    data = Path(cfg.data_dir)
    try:
        for sub in ("cache", "tmp"):
            (data / sub).mkdir(parents=True, exist_ok=True)
        for leftover in ("dump.partial", "dump.old"):
            shutil.rmtree(data / leftover, ignore_errors=True)
        for item in (data / "tmp").iterdir():
            if item.is_dir():
                shutil.rmtree(item, ignore_errors=True)
            else:
                item.unlink(missing_ok=True)
        free = shutil.disk_usage(data).free
    except OSError as exc:
        raise StepError("disk", f"cannot use {data}: {exc.strerror or exc}") from exc
    need = max(MIN_FREE_BYTES, 2 * (last_dump_bytes or 0))
    if free < need:
        raise StepError("disk", f"only {free / 2**30:.1f} GiB free in {data}, need {need / 2**30:.1f} GiB")


def check_repository(cfg: Config) -> None:
    """The repository exists and the password opens it; stale locks of an
    interrupted run are removed."""
    try:
        restic(cfg, ["cat", "config"], "repository")
    except StepError as exc:
        if "does not exist" in exc.message or "Is there a repository" in exc.message:
            raise StepError("repository", "no restic repository at RESTIC_REPOSITORY yet; create it with "
                                          "`docker compose run --rm backup init`") from exc
        raise
    restic(cfg, ["unlock"], "repository")


# ── dump ─────────────────────────────────────────────────────────────────

def dump_database(cfg: Config, target: Path) -> dict[str, Any]:
    """pg_dump in directory format, uncompressed so that restic can find the
    unchanged parts of each night's dump (restic compresses what it stores),
    and the role definitions without passwords (pg_dump has no roles)."""
    env = pg_env(cfg)
    versions = run_command(
        ["psql", "-X", "-At", "-c",
         "SELECT current_setting('server_version'), "
         "coalesce((SELECT extversion FROM pg_extension WHERE extname = 'timescaledb'), '')"],
        env, "pg_dump", SHORT_TIMEOUT_S).stdout.strip()
    server_version, _, timescaledb_version = versions.partition("|")
    run_command(["pg_dump", "--format=directory", "--compress=0", "--no-password",
                 f"--file={target / 'db'}"], env, "pg_dump", DUMP_TIMEOUT_S)
    roles = run_command(["pg_dumpall", "--roles-only", "--no-role-passwords", "--no-password",
                         "-l", cfg.pg_database], env, "pg_dumpall", SHORT_TIMEOUT_S).stdout
    (target / "roles.sql").write_text(roles)
    return {
        "server_version": server_version,
        "timescaledb_version": timescaledb_version,
        "db_bytes": _size(target / "db"),
    }


def flux_query(bucket: str, measurement: str) -> str:
    # Both names are validated as plain names in config.py. Without the
    # query's own _start/_stop columns an unchanged row reads the same every
    # night, so restic stores only what is new.
    return (f'from(bucket: "{bucket}")\n'
            f'  |> range(start: 1970-01-01T00:00:00Z)\n'
            f'  |> filter(fn: (r) => r._measurement == "{measurement}")\n'
            f'  |> drop(columns: ["_start", "_stop"])\n')


def export_measurement(cfg: Config, measurement: str, path: Path) -> int:
    """One measurement as annotated CSV (the format InfluxDB returns for a
    query). Returns the number of data rows."""
    url = f"{cfg.influx_url}/api/v2/query?" + urllib.parse.urlencode({"org": cfg.influx_org})
    body = json.dumps({
        "query": flux_query(cfg.influx_bucket, measurement),
        "type": "flux",
        "dialect": {"header": True, "annotations": ["datatype", "group", "default"]},
    }).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Token {cfg.influx_token}",
        "Content-Type": "application/json",
        "Accept": "application/csv",
    })
    rows = 0
    try:
        with urllib.request.urlopen(request, timeout=QUERY_TIMEOUT_S) as resp, path.open("wb") as out:
            for line in resp:
                out.write(line)
                # Data rows start with an empty annotation column; a table's
                # header row starts with ",result,".
                if line.startswith(b",") and not line.startswith(b",result,"):
                    rows += 1
    except urllib.error.HTTPError as exc:
        raise StepError("health", f"InfluxDB answered {exc.code} for {measurement}: {_influx_error(exc)}") from exc
    except (urllib.error.URLError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        raise StepError("health", f"cannot query InfluxDB at {cfg.influx_url}: {reason}") from exc
    return rows


def _influx_error(exc: urllib.error.HTTPError) -> str:
    try:
        return str(json.loads(exc.read(4096)).get("message", ""))[:200] or exc.reason
    except (ValueError, OSError, AttributeError):
        return str(exc.reason)


def export_health(cfg: Config, target: Path) -> dict[str, int]:
    folder = target / "health"
    folder.mkdir()
    return {m: export_measurement(cfg, m, folder / f"{m}.csv") for m in cfg.health_measurements}


def make_dump(cfg: Config, now: float) -> dict[str, Any]:
    """Build a complete dump in dump.partial, then put it in place of the
    previous one. Returns the manifest."""
    data = Path(cfg.data_dir)
    partial, current, old = data / "dump.partial", data / "dump", data / "dump.old"
    partial.mkdir()
    manifest: dict[str, Any] = {
        "created": datetime.fromtimestamp(now, timezone.utc).isoformat(timespec="seconds"),
        "database": cfg.pg_database,
        **dump_database(cfg, partial),
    }
    if cfg.health_enabled:
        manifest["health_rows"] = export_health(cfg, partial)
        manifest["health_bytes"] = _size(partial / "health")
    (partial / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    try:
        if current.exists():
            current.rename(old)
        partial.rename(current)
        shutil.rmtree(old, ignore_errors=True)
    except OSError as exc:
        raise StepError("disk", f"cannot replace the previous dump: {exc.strerror or exc}") from exc
    return manifest


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


# ── restic ───────────────────────────────────────────────────────────────

def upload(cfg: Config) -> dict[str, Any]:
    """New snapshot of the dump. Returns restic's summary."""
    proc = restic(cfg, ["backup", "--json", "--quiet", "--host", cfg.restic_host, "--tag", TAG,
                        str(Path(cfg.data_dir) / "dump")], "upload", UPLOAD_TIMEOUT_S)
    for line in reversed(proc.stdout.splitlines()):
        try:
            message = json.loads(line)
        except ValueError:
            continue
        if isinstance(message, dict) and message.get("message_type") == "summary":
            return message
    raise StepError("upload", "restic printed no summary")


def forget(cfg: Config) -> None:
    """Apply the retention policy to this service's snapshots and delete the
    data no snapshot needs any more."""
    args = ["forget", "--quiet", "--prune", "--host", cfg.restic_host, "--tag", TAG]
    for flag, count in (("--keep-daily", cfg.keep_daily), ("--keep-weekly", cfg.keep_weekly),
                        ("--keep-monthly", cfg.keep_monthly)):
        if count:
            args += [flag, str(count)]
    restic(cfg, args, "forget", PRUNE_TIMEOUT_S)


def check(cfg: Config) -> None:
    args = ["check"]
    if cfg.check_subset:
        args += ["--read-data-subset", cfg.check_subset]
    restic(cfg, args, "check", CHECK_TIMEOUT_S)


def init(cfg: Config) -> str:
    Path(cfg.data_dir, "cache").mkdir(parents=True, exist_ok=True)
    Path(cfg.data_dir, "tmp").mkdir(parents=True, exist_ok=True)
    return restic(cfg, ["init"], "init").stdout
