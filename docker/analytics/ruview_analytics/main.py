"""Watchdog service entry point: MQTT in, REST polls, report out."""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time

import paho.mqtt.client as mqtt

from .checks import Watchdog, keep_requests, metrics
from .config import Config, ConfigError
from .ha import discovery_messages
from .rest import poll_ha_persons, poll_sensing_server
from .store import Store

LOG = logging.getLogger("ruview-analytics")

# Give retained MQTT messages (bridge status, last report) time to arrive
# before the first round.
STARTUP_GRACE_S = 5.0


def make_client(cfg: Config, store: Store, lock: threading.Lock, watchdog: Watchdog) -> mqtt.Client:
    topics = cfg.topics
    client = mqtt.Client(client_id=cfg.device_id,
                         callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.will_set(topics["avail"], "offline", retain=True)
    if cfg.mqtt_user:
        client.username_pw_set(cfg.mqtt_user, cfg.mqtt_pass)

    def on_connect(c, _userdata, _flags, reason_code, _props=None):
        if reason_code != 0:
            LOG.error("MQTT connect failed, reason=%s", reason_code)
            return
        LOG.info("MQTT connected to %s:%s", cfg.mqtt_host, cfg.mqtt_port)
        c.publish(topics["avail"], "online", qos=1, retain=True)
        for topic, payload in discovery_messages(cfg, watchdog.checks):
            c.publish(topic, payload, qos=1, retain=True)
        c.subscribe([(topics[key], 0) for key in
                     ("bridge_avail", "bridge_status", "state", "nodes", "report",
                      "recorder_avail", "recorder_status")])

    def on_disconnect(_c, _userdata, _flags, reason_code, _props=None):
        LOG.warning("MQTT disconnected, reason=%s; paho will reconnect", reason_code)

    def on_message(_c, _userdata, msg):
        with lock:
            store.on_message(msg.topic, msg.payload, time.time())

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.connect_async(cfg.mqtt_host, cfg.mqtt_port, keepalive=30)
    client.loop_start()
    return client


def run_round(cfg: Config, store: Store, lock: threading.Lock, watchdog: Watchdog,
              client: mqtt.Client) -> dict:
    snap = poll_sensing_server(cfg, time.time())
    persons, persons_error = poll_ha_persons(cfg) if cfg.presence_enabled else (None, None)
    with lock:
        store.rest = snap
        if cfg.presence_enabled:
            store.persons, store.persons_error = persons, persons_error
        report, transitions = watchdog.evaluate(store, time.time())
        keeps = keep_requests(cfg, store, report, transitions)
    for check_id, change in transitions:
        entry = report["checks"][check_id]
        if change == "raised":
            LOG.warning("RAISED %s [%s]: %s", check_id, entry["severity"], entry["summary"])
        else:
            LOG.info("cleared %s", check_id)
    if persons_error:
        LOG.debug("HA persons: %s", persons_error)
    client.publish(cfg.topics["report"], json.dumps(report, ensure_ascii=False), qos=1, retain=True)
    client.publish(cfg.topics["metrics"], json.dumps(metrics(report)))
    for request in keeps:
        LOG.info("asking the csi-recorder to keep raw CSI: %s", request["tag"])
        client.publish(cfg.topics["recorder_keep"], json.dumps(request, ensure_ascii=False), qos=1)
    return report


def run() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        LOG.error("configuration error: %s", exc)
        sys.exit(2)

    LOG.info("RuView watchdog starting")
    LOG.info("  MQTT:    %s:%s prefix=%r", cfg.mqtt_host, cfg.mqtt_port, cfg.mqtt_prefix)
    LOG.info("  server:  %s (token %s)", cfg.ruview_http, "set" if cfg.api_token else "not set")
    LOG.info("  nodes:   %s", ",".join(map(str, cfg.expected_nodes)) or "-")
    LOG.info("  phones:  %s", ",".join(cfg.ha_persons) if cfg.presence_enabled else "off (set HA_URL, HA_TOKEN, HA_PERSONS)")

    store = Store(cfg, started_at=time.time())
    lock = threading.Lock()
    watchdog = Watchdog(cfg)
    client = make_client(cfg, store, lock, watchdog)

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    stop.wait(STARTUP_GRACE_S)
    while not stop.is_set():
        try:
            run_round(cfg, store, lock, watchdog, client)
        except Exception:  # keep watching; the next round may succeed
            LOG.exception("watchdog round failed")
        stop.wait(cfg.check_interval_s)

    # A clean disconnect does not fire the will, so say goodbye explicitly.
    info = client.publish(cfg.topics["avail"], "offline", qos=1, retain=True)
    try:
        info.wait_for_publish(timeout=2)
    except (RuntimeError, ValueError):
        pass
    client.disconnect()
    client.loop_stop()
    LOG.info("watchdog stopped")
