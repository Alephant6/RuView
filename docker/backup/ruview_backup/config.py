"""Environment configuration, validated at startup.

restic reads its own settings from the environment (RESTIC_REPOSITORY,
RESTIC_PASSWORD, AWS_ACCESS_KEY_ID, ...); they are checked here so that a
missing one stops the service at startup instead of failing at night.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass

# Same minimum as the database passwords (docker/timescaledb/migrate.sh).
MIN_SECRET_LENGTH = 16

_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
# InfluxDB names that go into the Flux query: plain names only.
_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$")
_SUBSET = re.compile(r"^(\d{1,2}(\.\d{1,2})?%|100%|[1-9]\d*/[1-9]\d*)$")
_HOST = re.compile(r"^[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    pass


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    if not raw.isdigit() or not lo <= int(raw) <= hi:
        raise ConfigError(f"{name} must be a whole number from {lo} to {hi}, got {raw!r}")
    return int(raw)


def _http_url(name: str, value: str) -> str:
    value = value.strip().rstrip("/")
    if value and not re.match(r"^https?://[^\s/]+$", value):
        raise ConfigError(f"{name} must be an http(s) URL without a path, got {value!r}")
    return value


def _host(name: str, value: str) -> str:
    value = value.strip()
    if value and not _HOST.match(value):
        raise ConfigError(f"{name} must be a host name or address, got {value!r}")
    return value


def _names(name: str, raw: str) -> tuple[str, ...]:
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if not _NAME.match(part):
            raise ConfigError(f"{name} entries must be plain names (letters, digits, _ . -), got {part!r}")
        out.append(part)
    return tuple(out)


@dataclass(frozen=True)
class Config:
    data_dir: str = "/backup"
    # Daily run time, UTC (07:30 UTC is 03:30 in New York in summer).
    at_utc: tuple[int, int] = (7, 30)
    # restic forget policy: daily, weekly and monthly snapshots to keep.
    # Each monthly snapshot keeps its own 14 days of raw rows (README.md).
    keep_daily: int = 7
    keep_weekly: int = 5
    keep_monthly: int = 6
    # Share of the stored data the weekly `restic check` downloads and
    # verifies; empty checks the repository structure only.
    check_subset: str = "5%"
    # Host name recorded in the snapshots; a container's own name changes
    # whenever it is recreated.
    restic_host: str = "ruview"

    pg_host: str = "timescaledb"
    pg_port: int = 5432
    pg_database: str = "ruview"
    pg_user: str = "backup"
    pg_password: str = ""

    # Apple Health export from InfluxDB, on when a read token is set.
    influx_url: str = "http://influxdb:8086"
    influx_token: str = ""
    influx_org: str = "ruview"
    influx_bucket: str = "sensing"
    health_measurements: tuple[str, ...] = (
        "apple_health", "apple_sleep_stage", "apple_sleep_daily", "apple_workout")

    # Status topic on the NAS bus; empty host publishes nothing.
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_prefix: str = "ruview"

    # Checked from the restic variables, which restic itself reads.
    repository: str = ""
    password_error: str = ""

    @property
    def health_enabled(self) -> bool:
        return bool(self.influx_url and self.influx_token and self.health_measurements)

    @property
    def status_topic(self) -> str:
        return f"{self.mqtt_prefix}/backup/state"

    def require_restic(self) -> None:
        if not self.repository:
            raise ConfigError("RESTIC_REPOSITORY is not set (docker/.env; see backup/README.md)")
        if self.password_error:
            raise ConfigError(self.password_error)

    def require_database(self) -> None:
        if len(self.pg_password) < MIN_SECRET_LENGTH:
            raise ConfigError(f"PG_BACKUP_PASSWORD must be set, at least {MIN_SECRET_LENGTH} characters "
                              "(the same value db-migrate gives the backup role)")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Config:
        env = os.environ if env is None else env
        at_raw = env.get("BACKUP_AT", "").strip() or "07:30"
        at = _TIME.match(at_raw)
        if not at:
            raise ConfigError(f"BACKUP_AT must be a UTC time like 07:30, got {at_raw!r}")
        keep = {
            "keep_daily": _int(env, "BACKUP_KEEP_DAILY", cls.keep_daily, 0, 1000),
            "keep_weekly": _int(env, "BACKUP_KEEP_WEEKLY", cls.keep_weekly, 0, 1000),
            "keep_monthly": _int(env, "BACKUP_KEEP_MONTHLY", cls.keep_monthly, 0, 1000),
        }
        if not any(keep.values()):
            raise ConfigError("BACKUP_KEEP_DAILY, _WEEKLY and _MONTHLY are all 0: nothing would be kept")
        subset = env.get("BACKUP_CHECK_SUBSET", cls.check_subset).strip()
        if subset and not _SUBSET.match(subset):
            raise ConfigError(f"BACKUP_CHECK_SUBSET must look like 5% or 1/10, got {subset!r}")
        data_dir = env.get("BACKUP_DIR", cls.data_dir).strip() or cls.data_dir
        if not data_dir.startswith("/"):
            raise ConfigError(f"BACKUP_DIR must be an absolute path, got {data_dir!r}")

        password = env.get("RESTIC_PASSWORD", "")
        password_error = ""
        if not env.get("RESTIC_PASSWORD_FILE", "").strip() and len(password) < MIN_SECRET_LENGTH:
            password_error = (f"RESTIC_PASSWORD must be set, at least {MIN_SECRET_LENGTH} characters "
                              "(openssl rand -hex 32); keep a copy outside the NAS")

        org = env.get("INFLUXDB_ORG", cls.influx_org).strip() or cls.influx_org
        bucket = env.get("INFLUXDB_BUCKET", cls.influx_bucket).strip() or cls.influx_bucket
        for name, value in (("INFLUXDB_ORG", org), ("INFLUXDB_BUCKET", bucket)):
            if not _NAME.match(value):
                raise ConfigError(f"{name} must be a plain name (letters, digits, _ . -), got {value!r}")
        measurements_raw = env.get("HEALTH_MEASUREMENTS")
        measurements = (cls.health_measurements if measurements_raw is None
                        else _names("HEALTH_MEASUREMENTS", measurements_raw))

        prefix = env.get("MQTT_PREFIX", cls.mqtt_prefix).strip() or cls.mqtt_prefix
        if not re.match(r"^[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)*$", prefix):
            raise ConfigError(f"MQTT_PREFIX must be a plain topic prefix, got {prefix!r}")

        return cls(
            data_dir=data_dir.rstrip("/") or "/",
            at_utc=(int(at.group(1)), int(at.group(2))),
            check_subset=subset,
            restic_host=_host("BACKUP_HOST", env.get("BACKUP_HOST", "")) or cls.restic_host,
            pg_host=_host("PGHOST", env.get("PGHOST", "")) or cls.pg_host,
            pg_port=_int(env, "PGPORT", cls.pg_port, 1, 65535),
            pg_database=_host("PGDATABASE", env.get("PGDATABASE", "")) or cls.pg_database,
            pg_user=_host("PGUSER", env.get("PGUSER", "")) or cls.pg_user,
            pg_password=env.get("PG_BACKUP_PASSWORD", ""),
            influx_url=_http_url("INFLUXDB_URL", env.get("INFLUXDB_URL", cls.influx_url)),
            influx_token=env.get("INFLUXDB_BACKUP_TOKEN", "").strip(),
            influx_org=org,
            influx_bucket=bucket,
            health_measurements=measurements,
            mqtt_host=_host("MQTT_HOST", env.get("MQTT_HOST", "")),
            mqtt_port=_int(env, "MQTT_PORT", cls.mqtt_port, 1, 65535),
            mqtt_prefix=prefix,
            repository=env.get("RESTIC_REPOSITORY", "").strip(),
            password_error=password_error,
            **keep,
        )
