"""Read-only polling of the sensing-server and Home Assistant REST APIs."""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime
from typing import Any

from .config import Config
from .store import PersonState, RestSnapshot

LOG = logging.getLogger("ruview-analytics.rest")

MAX_BODY_BYTES = 2 * 1024 * 1024

# Both APIs are on the LAN: ignore any proxy configured in the environment.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# (status code or None, parsed JSON or None, error text or None)
Response = tuple[int | None, Any, str | None]
Getter = Callable[[str, str, float], Response]


def http_get_json(url: str, token: str, timeout_s: float) -> Response:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with _OPENER.open(request, timeout=timeout_s) as resp:
            body = resp.read(MAX_BODY_BYTES + 1)
            status = resp.status
    except urllib.error.HTTPError as exc:
        return exc.code, None, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        return None, None, str(reason) or exc.__class__.__name__
    if len(body) > MAX_BODY_BYTES:
        return status, None, "response too large"
    try:
        return status, json.loads(body), None
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, None, "invalid JSON"


def poll_sensing_server(cfg: Config, now: float, get: Getter = http_get_json) -> RestSnapshot:
    base = cfg.ruview_http
    snap = RestSnapshot(at=now)
    status, body, error = get(f"{base}/health", cfg.api_token, cfg.http_timeout_s)
    if status == 421:
        # The server's Host allowlist (DNS-rebinding guard) does not list us.
        host = urllib.parse.urlsplit(base).hostname or base
        snap.error = f"HTTP 421: {host} is not in the server's SENSING_ALLOWED_HOSTS"
        return snap
    if error or not isinstance(body, dict):
        snap.error = error or "unexpected /health response"
        return snap
    snap.health = body

    for attr, path in (("status", "/api/v1/status"),
                       ("stream", "/api/v1/stream/status"),
                       ("nodes", "/api/v1/nodes"),
                       ("calibration", "/api/v1/calibration/status")):
        status, body, error = get(f"{base}{path}", cfg.api_token, cfg.http_timeout_s)
        if status in (401, 403):
            snap.auth_failed = True
            break
        if isinstance(body, dict):
            setattr(snap, attr, body)
        elif status != 404:
            LOG.debug("%s: %s", path, error)
    return snap


def _epoch(iso: Any) -> float | None:
    if not isinstance(iso, str):
        return None
    try:
        return datetime.fromisoformat(iso).timestamp()
    except ValueError:
        return None


def poll_ha_persons(cfg: Config, get: Getter = http_get_json) -> tuple[dict[str, PersonState] | None, str | None]:
    """States of the configured `person.*` entities, or (None, error)."""
    out: dict[str, PersonState] = {}
    for entity in cfg.ha_persons:
        url = f"{cfg.ha_url}/api/states/{urllib.parse.quote(entity)}"
        status, body, error = get(url, cfg.ha_token, cfg.http_timeout_s)
        if error or not isinstance(body, dict):
            return None, f"{entity}: {error or 'unexpected response'}"
        out[entity] = PersonState(state=str(body.get("state", "unknown")),
                                  since=_epoch(body.get("last_changed")))
    return out, None
