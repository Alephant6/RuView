"""The recorder process: tee socket -> ring, keep worker, MQTT status."""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
import signal
import socket
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config, ConfigError
from .keep import Interrupted, KeepError, KeepRequest, KeepStore, parse_request
from .rawfile import NS
from .ring import Ring
from .stats import Stats

LOG = logging.getLogger("csi-recorder")

RCVBUF_BYTES = 4 * 1024 * 1024
MAX_DATAGRAM_BYTES = 65535
INBOX_MAX = 100
KEEP_RETRIES = 3
KEEP_RETRY_NS = 60 * NS
SNAPSHOT_EVERY_NS = 60 * NS

Publish = Callable[[str, str, int, bool], None]


def open_socket(cfg: Config) -> socket.socket:
    family = socket.AF_INET6 if ":" in cfg.listen_host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_DGRAM)
    try:
        # Room for a few seconds of bursts; the kernel caps it at rmem_max.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, RCVBUF_BYTES)
    except OSError:
        pass
    sock.bind((cfg.listen_host, cfg.listen_port))
    sock.settimeout(1.0)
    return sock


def kernel_drops(sock: socket.socket | None) -> int | None:
    """Datagrams the kernel dropped because this socket's receive buffer
    was full (Linux /proc/net/udp `drops`), or None if unknown."""
    if sock is None:
        return None
    try:
        inode = str(os.fstat(sock.fileno()).st_ino)
    except OSError:
        return None
    for name in ("/proc/net/udp", "/proc/net/udp6"):
        try:
            lines = Path(name).read_text().splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            parts = line.split()
            if len(parts) >= 13 and parts[9] == inode:
                try:
                    return int(parts[12])
                except ValueError:
                    return None
    return None


def check_writable(path: Path) -> str | None:
    """None if the recorder can create files in `path`, else the reason."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".write-test-{os.getpid()}"
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        return str(exc)
    return None


class KeepWorker(threading.Thread):
    """Validates keep requests and writes clips, off the receive thread."""

    def __init__(self, cfg: Config, store: KeepStore, publish: Publish | None,
                 stop: threading.Event, clock: Callable[[], int] = time.time_ns) -> None:
        super().__init__(name="keep-worker", daemon=True)
        self.cfg = cfg
        self.store = store
        self.publish = publish
        self.stop = stop
        self.clock = clock
        self.inbox: queue.Queue[bytes] = queue.Queue(maxsize=INBOX_MAX)
        self._failures: dict[str, int] = {}
        self._retry_at: dict[str, int] = {}
        self._last_snapshot_ns = 0

    def submit(self, payload: bytes) -> None:
        try:
            self.inbox.put_nowait(payload)
        except queue.Full:
            LOG.warning("keep request dropped: %d requests already waiting", INBOX_MAX)

    def run(self) -> None:
        while not self.stop.is_set():
            try:
                payload: bytes | None = self.inbox.get(timeout=1.0)
            except queue.Empty:
                payload = None
            try:
                self.step(payload)
            except Exception:  # keep serving; the next step may succeed
                LOG.exception("keep worker step failed")

    def step(self, payload: bytes | None) -> None:
        now = self.clock()
        if payload is not None:
            self.handle(payload, now)
        for req in self.store.due(now):
            if self.stop.is_set():
                return
            if self._retry_at.get(req.id, 0) > now:
                continue
            self.process(req)
        if now - self._last_snapshot_ns >= SNAPSHOT_EVERY_NS:
            self._last_snapshot_ns = now
            self.store.refresh(now)   # the UTC day may have changed

    def handle(self, payload: bytes, now: int) -> None:
        try:
            req = parse_request(payload, now, self.cfg)
            status = self.store.add(req, now)
        except KeepError as exc:
            LOG.warning("keep request rejected: %s", exc)
            self.emit(KeepStore.result(None, "rejected", str(exc), **_echo(payload)))
            return
        LOG.info("keep request %s (%s) %s: %s for %.0f s", req.id, req.tag, status,
                 time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(req.start_ns // NS)),
                 req.duration_s)
        self.emit(KeepStore.result(req, status))

    def process(self, req: KeepRequest) -> None:
        existing = self.store.kept_clip(req)
        if existing is not None:      # written before a crash or full disk
            self.store.done(req, self.clock())
            self.emit(KeepStore.result(req, "kept", file=f"keep/{req.day}/{existing.get('file')}",
                                       records=existing.get("records")))
            return
        try:
            result = self.store.extract(req, self.clock(), self.stop, self.clock)
        except Interrupted:
            LOG.info("keep %s interrupted by shutdown; it stays pending", req.id)
            return
        except Exception as exc:  # disk errors, damaged files; retried with a pause
            failures = self._failures.get(req.id, 0) + 1
            self._failures[req.id] = failures
            LOG.error("keep %s failed (attempt %d of %d): %s", req.id, failures, KEEP_RETRIES, exc)
            if failures < KEEP_RETRIES:
                self._retry_at[req.id] = self.clock() + KEEP_RETRY_NS
                return
            result = KeepStore.result(req, "failed", str(exc))
        self._failures.pop(req.id, None)
        self._retry_at.pop(req.id, None)
        self.store.done(req, self.clock())
        self.emit(result)

    def emit(self, result: dict[str, Any]) -> None:
        if self.publish is not None:
            self.publish(self.cfg.topics["keep_result"],
                         json.dumps(result, ensure_ascii=False), 1, False)


def _echo(payload: bytes) -> dict[str, str]:
    """id and tag of a rejected request, when readable, so the sender can
    match the result."""
    try:
        obj = json.loads(payload[:4096])
    except (UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(obj, dict):
        return {}
    return {k: obj[k] for k in ("id", "tag")
            if isinstance(obj.get(k), str) and 0 < len(obj[k]) <= 64 and obj[k].isprintable()}


class Recorder:
    """Receive loop: every datagram goes to the ring and the counters."""

    def __init__(self, cfg: Config, publish: Publish | None = None,
                 clock: Callable[[], int] = time.time_ns,
                 disk_usage: Callable[[Path], Any] = shutil.disk_usage) -> None:
        self.cfg = cfg
        self.publish = publish
        self.clock = clock
        meta = {"recorder": __version__, "listen": f"{cfg.listen_host}:{cfg.listen_port}"}
        self.ring = Ring(cfg, meta, disk_usage)
        self.stats = Stats(clock())
        self.keeps = KeepStore(cfg)
        self.sock: socket.socket | None = None
        self._last_status_ns = 0

    def on_datagram(self, now_ns: int, data: bytes) -> None:
        self.ring.write(now_ns, data)
        self.stats.add(now_ns, data)

    def tick(self, now_ns: int) -> None:
        self.ring.tick(now_ns)
        if now_ns - self._last_status_ns >= self.cfg.status_interval_s * NS:
            self._last_status_ns = now_ns
            status = self.status(now_ns)
            LOG.debug("status %s", status)
            if self.publish is not None:
                self.publish(self.cfg.topics["status"], json.dumps(status), 0, True)

    def status(self, now_ns: int) -> dict[str, Any]:
        cfg, ring = self.cfg, self.ring
        current = ring.current_path
        keep = self.keeps.snapshot
        return {
            "ts": round(now_ns / NS, 3),
            "version": __version__,
            "listen": f"{cfg.listen_host}:{cfg.listen_port}",
            "recording": current is not None,
            "file": None if current is None else str(current.relative_to(cfg.data_dir)),
            **self.stats.snapshot(now_ns),
            "ring_files": ring.files,
            "ring_bytes": ring.bytes,
            "ring_hours": None if ring.oldest_ns is None
            else round((now_ns - ring.oldest_ns) / NS / 3600, 2),
            "ring_limit_hours": cfg.ring_hours,
            "ring_limit_gb": cfg.ring_max_gb,
            "disk_free_gb": None if ring.disk_free_bytes is None
            else round(ring.disk_free_bytes / 1e9, 2),
            "disk_low": ring.disk_low,
            "dropped": ring.dropped,
            "write_errors": ring.write_errors,
            "last_error": ring.last_error,
            "kernel_drops": kernel_drops(self.sock),
            "keep_pending": keep["pending"],
            "keep_today_min": keep["today_min"],
            "keep_budget_min": round(cfg.keep_max_h_per_day * 60, 1),
        }

    def serve(self, sock: socket.socket, stop: threading.Event) -> None:
        self.sock = sock
        self.ring.maintain(self.clock())
        try:
            while not stop.is_set():
                try:
                    data: bytes | None = sock.recv(MAX_DATAGRAM_BYTES)
                except TimeoutError:
                    data = None
                now = self.clock()
                if data:
                    self.on_datagram(now, data)
                self.tick(now)
        finally:
            self.ring.close()


def make_mqtt(cfg: Config, on_keep: Callable[[bytes], None]) -> Any:
    import paho.mqtt.client as mqtt

    topics = cfg.topics
    # Persistent session: keep requests (QoS 1) sent while the recorder
    # restarts are delivered once it is back.
    client = mqtt.Client(client_id=cfg.client_id, clean_session=False,
                         callback_api_version=mqtt.CallbackAPIVersion.VERSION2)
    client.will_set(topics["avail"], "offline", qos=1, retain=True)
    if cfg.mqtt_user:
        client.username_pw_set(cfg.mqtt_user, cfg.mqtt_pass)

    def on_connect(c, _userdata, _flags, reason_code, _props=None):
        if reason_code != 0:
            LOG.error("MQTT connect failed, reason=%s", reason_code)
            return
        LOG.info("MQTT connected to %s:%s", cfg.mqtt_host, cfg.mqtt_port)
        c.publish(topics["avail"], "online", qos=1, retain=True)
        c.subscribe(topics["keep"], qos=1)

    def on_disconnect(_c, _userdata, _flags, reason_code, _props=None):
        LOG.warning("MQTT disconnected, reason=%s; paho will reconnect", reason_code)

    warned = []

    def on_message(_c, _userdata, msg):
        if msg.retain:
            # A retained request would be replayed on every reconnect.
            (LOG.debug if warned else LOG.warning)(
                "ignoring a retained keep request; publish keep requests without retain")
            warned.append(True)
            return
        on_keep(msg.payload)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.connect_async(cfg.mqtt_host, cfg.mqtt_port, keepalive=30)
    client.loop_start()
    return client


def run() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s :: %(message)s",
    )
    try:
        cfg = Config.from_env()
    except ConfigError as exc:
        LOG.error("configuration error: %s", exc)
        return 2
    for path in (cfg.ring_dir, cfg.keep_dir):
        problem = check_writable(path)
        if problem:
            LOG.error("cannot write to %s (%s). The recorder runs as uid %d; give it the "
                      "data directory on the host, e.g. `sudo chown -R %d:%d <CSI_DATA_DIR>`.",
                      path, problem, os.getuid(), os.getuid(), os.getgid())
            return 2
    try:
        sock = open_socket(cfg)
    except OSError as exc:
        LOG.error("cannot listen on %s:%d: %s", cfg.listen_host, cfg.listen_port, exc)
        return 1

    LOG.info("RuView CSI recorder %s", __version__)
    LOG.info("  listen:  udp %s:%d (sensing-server RUVIEW_UDP_TEE)", cfg.listen_host, cfg.listen_port)
    LOG.info("  data:    %s (ring %g h / %g GB, stop below %g GB free)",
             cfg.data_dir, cfg.ring_hours, cfg.ring_max_gb, cfg.min_free_gb)
    LOG.info("  keep:    up to %g min per clip, %g h per day", cfg.keep_max_min, cfg.keep_max_h_per_day)
    LOG.info("  MQTT:    %s", f"{cfg.mqtt_host}:{cfg.mqtt_port} prefix={cfg.mqtt_prefix!r}"
             if cfg.mqtt_host else "off (MQTT_HOST is empty): no status, no keep requests")

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    recorder = Recorder(cfg)
    recorder.keeps.load()
    # The worker also runs without MQTT: requests pending from before a
    # restart still get written.
    worker = KeepWorker(cfg, recorder.keeps, None, stop)
    client = None
    if cfg.mqtt_host:
        client = make_mqtt(cfg, worker.submit)

        def publish(topic: str, payload: str, qos: int, retain: bool) -> None:
            client.publish(topic, payload, qos=qos, retain=retain)

        recorder.publish = worker.publish = publish
    worker.start()

    recorder.serve(sock, stop)
    sock.close()
    worker.join(timeout=5)
    if client is not None:
        # A clean disconnect does not fire the will, so say goodbye explicitly.
        info = client.publish(cfg.topics["avail"], "offline", qos=1, retain=True)
        try:
            info.wait_for_publish(timeout=2)
        except (RuntimeError, ValueError):
            pass
        client.disconnect()
        client.loop_stop()
    LOG.info("recorder stopped")
    return 0
