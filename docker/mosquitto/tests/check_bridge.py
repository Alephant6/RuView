"""Checks the bridge between the NAS bus and Home Assistant's broker.

Talks to the bus and to a broker standing in for Home Assistant's, and
checks what ../start.sh bridges:

    route      publishes a probe on every bridged topic and on topics that
               must stay where they are; each probe has to arrive exactly
               once on the other side, or never, and never come back. The
               retained probes are left in place for `retained`.
    retained   every retained probe on the bus is also retained on the Home
               Assistant side, with the same payload.
    link 1|0   waits until the bridge's link state on Home Assistant's
               broker (<prefix>/bus/ha_connected) is 1 or 0.
    cleanup    removes the retained probes from both brokers.

For test brokers only: `route` overwrites the retained messages of real
topic names (entity configs, availability) with probes.

The Home Assistant broker's login comes from HA_MQTT_USER / HA_MQTT_PASS.
Needs paho-mqtt >= 2.0.
"""

from __future__ import annotations

import argparse
import os
import sys
import threading
import time
import uuid

import paho.mqtt.client as mqtt

SETTLE_S = 2.0


def out_topics(p: str, ha: str) -> list[tuple[str, bool]]:
    """Published on the bus, must reach Home Assistant once: (topic, retained)."""
    return [
        (f"{ha}/sensor/wifi_densepose_ruview-node1/heart_rate/config", True),
        (f"{ha}/sensor/wifi_densepose_ruview-node1/heart_rate/state", False),
        (f"{ha}/binary_sensor/wifi_densepose_ruview-node1/presence/availability", True),
        (f"{ha}/binary_sensor/ruview_watchdog/problem/config", True),
        (f"{p}/availability", True),
        (f"{p}/person/1", False),
        (f"{p}/person/1/avail", True),
        (f"{p}/person/sq", False),
        (f"{p}/nodes/2", False),
        (f"{p}/event", False),
        (f"{p}/watchdog/state", True),
        (f"{p}/watchdog/availability", True),
        (f"{p}/recorder/keep/result", False),
    ]


def bus_only_topics(p: str) -> list[str]:
    """Published on the bus, must stay there."""
    return [f"{p}/state", f"{p}/bridge", f"{p}/watchdog/metrics", f"{p}/recorder/status",
            f"{p}/recorder/availability", f"{p}/recorder/keep", f"{p}/annotation"]


def in_topics(p: str) -> list[str]:
    """Published on Home Assistant's broker, must reach the bus once."""
    return [f"{p}/annotation", f"{p}/recorder/keep"]


def ha_only_topics(p: str, ha: str) -> list[str]:
    """Published on Home Assistant's broker, must stay there."""
    return [f"{p}/state", f"{p}/person/1", f"{p}/watchdog/state", f"{ha}/status",
            f"{ha}/sensor/other_device/temperature/state"]


def host_port(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError(f"expected HOST:PORT, got {value!r}")
    return host, int(port)


class Broker:
    """One connection that records every message it receives."""

    def __init__(self, name: str, addr: tuple[str, int], user: str = "", password: str = "") -> None:
        self.name = name
        self.messages: list[tuple[str, bytes, bool]] = []
        self._lock = threading.Lock()
        self._connected = threading.Event()
        self._subscribed = threading.Event()
        self._connect_rc = None
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                                  client_id=f"check-bridge-{name}-{uuid.uuid4().hex[:8]}")
        if user:
            self.client.username_pw_set(user, password)
        self.client.on_connect = self._on_connect
        self.client.on_subscribe = lambda *a: self._subscribed.set()
        self.client.on_message = self._on_message
        self.client.connect(*addr, keepalive=30)
        self.client.loop_start()
        if not self._connected.wait(10) or self._connect_rc.is_failure:
            raise SystemExit(f"{name}: could not connect to {addr[0]}:{addr[1]} ({self._connect_rc})")

    def _on_connect(self, _c, _u, _flags, reason_code, _props=None) -> None:
        self._connect_rc = reason_code
        self._connected.set()

    def _on_message(self, _c, _u, msg) -> None:
        with self._lock:
            self.messages.append((msg.topic, bytes(msg.payload), bool(msg.retain)))

    def subscribe(self, topics: list[str]) -> None:
        self._subscribed.clear()
        self.client.subscribe([(t, 1) for t in topics])
        if not self._subscribed.wait(10):
            raise SystemExit(f"{self.name}: subscribe timed out")

    def publish(self, topic: str, payload: bytes, retain: bool = False) -> None:
        self.client.publish(topic, payload, qos=1, retain=retain).wait_for_publish(10)

    def count(self, topic: str, payload: bytes) -> int:
        with self._lock:
            return sum(1 for t, p, _ in self.messages if t == topic and p == payload)

    def retained(self, topics: list[str], wait_s: float = 1.0) -> dict[str, bytes]:
        """Retained payloads of `topics`, read by a fresh subscription."""
        with self._lock:
            self.messages.clear()
        self.subscribe(topics)
        time.sleep(wait_s)
        with self._lock:
            return {t: p for t, p, r in self.messages if r}

    def close(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()


def connect(args: argparse.Namespace, which: str) -> Broker:
    if which == "bus":
        return Broker("bus", args.bus)
    return Broker("ha", args.ha, os.environ.get("HA_MQTT_USER", ""), os.environ.get("HA_MQTT_PASS", ""))


def link_topic(args: argparse.Namespace) -> str:
    return f"{args.prefix}/bus/ha_connected"


def cmd_route(args: argparse.Namespace) -> list[str]:
    p, ha_prefix = args.prefix, args.ha_prefix
    bus, ha = connect(args, "bus"), connect(args, "ha")
    failures = []
    for broker in (bus, ha):
        state = broker.retained([link_topic(args)]).get(link_topic(args))
        if state != b"1":
            failures.append(f"{broker.name}: {link_topic(args)} is {state!r}, expected b'1'")
    if failures:
        return failures

    run = uuid.uuid4().hex[:12]

    def probe(origin: Broker, topic: str) -> bytes:
        return f"probe {run} from {origin.name} {topic}".encode()

    for broker in (bus, ha):
        broker.subscribe(["#"])
    for topic, retain in out_topics(p, ha_prefix):
        bus.publish(topic, probe(bus, topic), retain)
    for topic in bus_only_topics(p):
        bus.publish(topic, probe(bus, topic))
    for topic in in_topics(p):
        ha.publish(topic, probe(ha, topic))
    for topic in ha_only_topics(p, ha_prefix):
        ha.publish(topic, probe(ha, topic))
    time.sleep(SETTLE_S)

    def expect(origin: Broker, topic: str, broker: Broker, want: int) -> None:
        got = broker.count(topic, probe(origin, topic))
        if got != want:
            failures.append(f"{topic} from {origin.name}: {got} on {broker.name}, expected {want}")

    rows = []
    for topic, _ in out_topics(p, ha_prefix):
        expect(bus, topic, ha, 1)
        expect(bus, topic, bus, 1)
        rows.append(("bus -> ha", topic))
    for topic in bus_only_topics(p):
        expect(bus, topic, ha, 0)
        expect(bus, topic, bus, 1)
        rows.append(("bus only", topic))
    for topic in in_topics(p):
        expect(ha, topic, bus, 1)
        expect(ha, topic, ha, 1)
        rows.append(("ha -> bus", topic))
    for topic in ha_only_topics(p, ha_prefix):
        expect(ha, topic, bus, 0)
        expect(ha, topic, ha, 1)
        rows.append(("ha only", topic))

    retained_topics = [t for t, r in out_topics(p, ha_prefix) if r]
    got = ha.retained(retained_topics)
    for topic in retained_topics:
        if got.get(topic) != probe(bus, topic):
            failures.append(f"{topic}: retained on ha as {got.get(topic)!r}")
    bus.close()
    ha.close()
    for direction, topic in rows:
        print(f"  {direction:10} {topic}")
    print(f"{len(rows)} topics checked, {len(retained_topics)} retained")
    return failures


def cmd_retained(args: argparse.Namespace) -> list[str]:
    topics = [t for t, r in out_topics(args.prefix, args.ha_prefix) if r]
    bus, ha = connect(args, "bus"), connect(args, "ha")
    on_bus, on_ha = bus.retained(topics), ha.retained(topics)
    bus.close()
    ha.close()
    failures = []
    for topic in topics:
        if not on_bus.get(topic, b"").startswith(b"probe "):
            failures.append(f"{topic}: no retained probe on the bus (run `route` first)")
        elif on_ha.get(topic) != on_bus[topic]:
            failures.append(f"{topic}: retained on ha as {on_ha.get(topic)!r}, on the bus as {on_bus[topic]!r}")
    print(f"{len(topics) - len(failures)}/{len(topics)} retained probes match")
    return failures


def cmd_link(args: argparse.Namespace) -> list[str]:
    want = args.value.encode()
    deadline = time.monotonic() + args.timeout
    state, error = None, ""
    while time.monotonic() < deadline:
        # A new connection each time: Home Assistant's broker may be restarting.
        try:
            ha = connect(args, "ha")
        except (OSError, SystemExit) as exc:
            error = f" (last connect error: {exc})"
        else:
            try:
                state = ha.retained([link_topic(args)], wait_s=0.5).get(link_topic(args))
            finally:
                ha.close()
            if state == want:
                print(f"{link_topic(args)} = {args.value} on Home Assistant's broker")
                return []
        time.sleep(1)
    return [f"{link_topic(args)} is {state!r} after {args.timeout:.0f} s, expected {want!r}{error}"]


def cmd_cleanup(args: argparse.Namespace) -> list[str]:
    topics = [t for t, r in out_topics(args.prefix, args.ha_prefix) if r]
    bus = connect(args, "bus")
    # Only the probes: anything else retained there is not ours to remove.
    probes = [t for t, p in bus.retained(topics).items() if p.startswith(b"probe ")]
    for topic in probes:
        # An empty retained message clears the topic here and, bridged, on Home Assistant's broker.
        bus.publish(topic, b"", retain=True)
    bus.close()
    time.sleep(SETTLE_S)
    ha = connect(args, "ha")
    left = [t for t, p in ha.retained(topics).items() if p.startswith(b"probe ")]
    ha.close()
    print(f"{len(probes)} retained probes removed")
    return [f"{topic}: probe still retained on ha" for topic in left]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("command", choices=["route", "retained", "link", "cleanup"])
    ap.add_argument("value", nargs="?", choices=["0", "1"], help="link state to wait for")
    ap.add_argument("--bus", type=host_port, default=("127.0.0.1", 1883))
    ap.add_argument("--ha", type=host_port, default=("127.0.0.1", 18840))
    ap.add_argument("--prefix", default="ruview")
    ap.add_argument("--ha-prefix", default="homeassistant")
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()
    if args.command == "link" and args.value is None:
        ap.error("link needs 1 or 0")
    commands = {"route": cmd_route, "retained": cmd_retained, "link": cmd_link, "cleanup": cmd_cleanup}
    failures = commands[args.command](args)
    for failure in failures:
        print(f"FAIL {failure}")
    if not failures:
        print(f"{args.command}: ok")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
