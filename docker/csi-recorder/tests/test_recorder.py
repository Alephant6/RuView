"""Recorder file format, ring retention, keep requests and tools.

Synthetic datagrams only, written to pytest tmp dirs; no broker or
sensing-server needed. Run from docker/csi-recorder:  python -m pytest -q
"""

from __future__ import annotations

import gzip
import importlib.util
import json
import os
import socket
import struct
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from csi_recorder import rawfile as R  # noqa: E402
from csi_recorder.config import Config, ConfigError, parse_listen  # noqa: E402
from csi_recorder.keep import (  # noqa: E402
    KeepError,
    KeepRequest,
    KeepStore,
    parse_request,
)
from csi_recorder.packets import classify, parse_adr018  # noqa: E402
from csi_recorder.ring import HOUR_NS, Ring  # noqa: E402
from csi_recorder.service import KeepWorker, Recorder, _echo  # noqa: E402
from csi_recorder.stats import Stats  # noqa: E402
from csi_recorder import tools  # noqa: E402

NS = R.NS
T0 = 1_792_000_800 * NS          # 2026-10-14T18:00:00Z, on an hour boundary
REPO = Path(__file__).resolve().parents[3]


def csi_frame(node: int = 1, seq: int = 0, n_sub: int = 64, n_ant: int = 1) -> bytes:
    header = struct.pack("<IBBHIIbbBB", 0xC5110001, node, n_ant, n_sub, 2437, seq, -52, -91, 1, 0)
    iq = bytes(((seq + i) * 7) % 256 for i in range(2 * n_ant * n_sub))
    return header + iq


def sync_packet(node: int = 2) -> bytes:
    return struct.pack("<IB", 0xC511A110, node) + bytes(27)


def cfg_for(tmp_path: Path, **kw) -> Config:
    return Config(data_dir=str(tmp_path), mqtt_host="", **kw)


class FreeDisk:
    def __init__(self, free_gb: float = 100.0) -> None:
        self.free = int(free_gb * 1e9)

    def __call__(self, _path):
        return SimpleNamespace(free=self.free)


def fill_ring(cfg: Config, start_ns: int, seconds: int, hz: int = 10, node: int = 1) -> Ring:
    ring = Ring(cfg, {"test": True}, FreeDisk())
    step = NS // hz
    for i in range(seconds * hz):
        ts = start_ns + i * step
        ring.write(ts, csi_frame(node, i))
        ring.tick(ts)
    return ring


# ── config ───────────────────────────────────────────────────────────────
def test_listen_must_be_loopback():
    assert parse_listen("127.0.0.1:5006") == ("127.0.0.1", 5006)
    assert parse_listen("[::1]:5007") == ("::1", 5007)
    for bad in ("0.0.0.0:5006", "192.0.2.10:5006", "localhost:5006", "127.0.0.1", "127.0.0.1:0"):
        with pytest.raises(ConfigError):
            parse_listen(bad)


def test_config_from_env():
    cfg = Config.from_env({})
    assert (cfg.listen_host, cfg.listen_port, cfg.ring_hours) == ("127.0.0.1", 5006, 72)
    assert cfg.topics["keep"] == "ruview/recorder/keep"
    cfg = Config.from_env({"CSI_DATA_DIR": "/srv/csi", "RING_HOURS": "24", "MQTT_HOST": "",
                           "MQTT_PREFIX": "rv", "GZIP_LEVEL": "1"})
    assert cfg.ring_dir == Path("/srv/csi/ring") and cfg.mqtt_host == "" and cfg.gzip_level == 1
    assert cfg.topics["status"] == "rv/recorder/status"
    for env in ({"CSI_DATA_DIR": "data/csi"}, {"RING_HOURS": "0"}, {"RING_MAX_GB": "x"},
                {"MQTT_PORT": "70000"}, {"RECORDER_LISTEN": "0.0.0.0:5006"}):
        with pytest.raises(ConfigError):
            Config.from_env(env)


# ── file format ──────────────────────────────────────────────────────────
def test_raw_roundtrip_and_names(tmp_path):
    path = R.unique_path(tmp_path, R.stamp(T0))
    assert path.name == R.stamp(T0) + R.SUFFIX and R.parse_name(path.name) == T0 // NS * NS
    datagrams = [csi_frame(1, i) for i in range(5)] + [b"{\"x\":1}", sync_packet()]
    with R.RawWriter(path, {"created_ns": T0, "k": "v"}) as w:
        for i, d in enumerate(datagrams):
            w.write(T0 + i, d)
    assert R.read_meta(path)["k"] == "v"
    assert list(R.iter_records(path)) == [(T0 + i, d) for i, d in enumerate(datagrams)]
    assert R.unique_path(tmp_path, R.stamp(T0)).name.endswith("-1" + R.SUFFIX)
    assert R.parse_name("notes.txt") is None


def test_open_file_reads_up_to_last_flush(tmp_path):
    path = tmp_path / ("x" + R.SUFFIX)
    w = R.RawWriter(path, {})
    for i in range(100):
        w.write(T0 + i, csi_frame(1, i))
    w.flush()
    for i in range(100, 103):            # not flushed: may or may not be readable
        w.write(T0 + i, csi_frame(1, i))
    got = list(R.iter_records(path))
    assert len(got) >= 100 and got[:100] == [(T0 + i, csi_frame(1, i)) for i in range(100)]
    w.close()
    assert len(list(R.iter_records(path))) == 103


def test_cut_short_file_and_bad_files(tmp_path):
    path = tmp_path / ("x" + R.SUFFIX)
    with R.RawWriter(path, {}) as w:
        for i in range(200):
            w.write(T0 + i, csi_frame(1, i))
    blob = path.read_bytes()
    cut = tmp_path / ("cut" + R.SUFFIX)
    cut.write_bytes(blob[: len(blob) // 2])          # power loss mid-write
    got = list(R.iter_records(cut))
    assert 0 < len(got) < 200 and got == [(T0 + i, csi_frame(1, i)) for i in range(len(got))]

    not_ours = tmp_path / ("other" + R.SUFFIX)
    with gzip.open(not_ours, "wb") as fh:
        fh.write(b"hello world, not a recording")
    with pytest.raises(R.RawFormatError):
        list(R.iter_records(not_ours))
    plain = tmp_path / ("plain" + R.SUFFIX)
    plain.write_bytes(b"definitely not gzip")
    with pytest.raises(R.RawFormatError):
        list(R.iter_records(plain))
    with pytest.raises(R.RawFormatError):
        R.read_meta(plain)
    with pytest.raises(ValueError):
        with R.RawWriter(tmp_path / ("big" + R.SUFFIX), {}) as w:
            w.write(T0, bytes(70_000))


# ── packets ──────────────────────────────────────────────────────────────
def test_classify():
    assert classify(csi_frame(3)) == ("csi", 3)
    assert classify(sync_packet(2)) == ("sync", 2)
    assert classify(struct.pack("<IB", 0xC5110002, 1) + bytes(10)) == ("vitals", 1)
    assert classify(struct.pack("<IB", 0xC51100FF, 4)) == ("ruview_other", 4)
    assert classify(b'{"event":"x"}') == ("json", None)
    assert classify(b"\x00\x01") == ("other", None)


def test_parse_adr018():
    frame = parse_adr018(csi_frame(2, 7, n_sub=64, n_ant=2))
    assert frame["node_id"] == 2 and frame["seq"] == 7 and frame["channel"] == 6
    assert frame["subcarriers"] == 128 and len(frame["amplitudes"]) == 128
    assert parse_adr018(csi_frame(2)[:30]) is None             # truncated I/Q
    assert parse_adr018(sync_packet()) is None
    bad_ant = bytearray(csi_frame(1))
    bad_ant[5] = 0
    assert parse_adr018(bytes(bad_ant)) is None


def _load_record_script():
    path = REPO / "scripts" / "record-csi-udp.py"
    if not path.exists():
        pytest.skip("scripts/record-csi-udp.py not in this checkout")
    spec = importlib.util.spec_from_file_location("record_csi_udp", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_export_matches_record_csi_udp():
    script = _load_record_script()
    data = csi_frame(3, 11, n_sub=52, n_ant=1)
    theirs = script.parse_csi_packet(data)
    (ours,) = tools.export_lines(iter([(T0, data)]), include_all=False)
    assert set(ours) == set(theirs)
    for key in theirs:
        if key not in ("timestamp", "ts_ns"):
            assert ours[key] == theirs[key], key
    assert ours["ts_ns"] == T0 and ours["timestamp"].endswith("Z")
    lines = list(tools.export_lines(iter([(T0, sync_packet())]), include_all=True))
    assert lines[0]["type"] == "datagram" and lines[0]["kind"] == "sync"


# ── ring ─────────────────────────────────────────────────────────────────
def test_ring_rotates_hourly_and_closes_idle_hour(tmp_path):
    cfg = cfg_for(tmp_path)
    ring = Ring(cfg, {}, FreeDisk())
    hour0 = (T0 // HOUR_NS) * HOUR_NS
    ring.write(hour0 + 10 * NS, csi_frame())
    first = ring.current_path
    ring.write(hour0 + HOUR_NS + 5 * NS, csi_frame())
    second = ring.current_path
    assert first != second and first.exists()
    assert first.parent.name == R.day(hour0) and R.parse_name(second.name) == hour0 + HOUR_NS + 5 * NS
    ring.tick(hour0 + 2 * HOUR_NS + NS)       # idle into the next hour: file finished
    assert ring.current_path is None
    assert len(list(R.iter_records(second))) == 1


def test_ring_retention_by_age_and_size(tmp_path):
    cfg = cfg_for(tmp_path, ring_hours=2)
    ring = Ring(cfg, {}, FreeDisk())
    hour0 = (T0 // HOUR_NS) * HOUR_NS
    for h in range(5):
        for i in range(200):
            ring.write(hour0 + h * HOUR_NS + i * NS // 10, csi_frame(1, i + h))
    files = R.list_files(cfg.ring_dir)
    assert len(files) == 5
    now = hour0 + 5 * HOUR_NS
    for h, entry in enumerate(files):      # last write at the end of each hour
        os.utime(entry.path, ns=(hour0 + (h + 1) * HOUR_NS - NS,) * 2)
    ring.maintain(now)
    left = R.list_files(cfg.ring_dir)
    assert all(e.mtime_ns >= now - 2 * HOUR_NS for e in left)
    assert ring.current_path in [e.path for e in left]            # never deletes the open file
    assert len(left) == 2 and ring.deleted_files == 3 and ring.files == 2
    ring.close()                              # finishes the gzip stream: size changes
    newest = R.list_files(cfg.ring_dir)[-1]
    capped = Ring(cfg_for(tmp_path, ring_hours=2, ring_max_gb=newest.size / 1e9), {}, FreeDisk())
    capped.maintain(now)                      # oldest go first until the newest fits
    assert [e.path for e in R.list_files(cfg.ring_dir)] == [newest.path]


def test_ring_pauses_when_disk_is_low(tmp_path):
    cfg = cfg_for(tmp_path, min_free_gb=2)
    disk = FreeDisk(free_gb=1.5)
    ring = Ring(cfg, {}, disk)
    assert ring.write(T0, csi_frame())
    ring.maintain(T0)
    assert ring.disk_low and ring.current_path is None
    assert not ring.write(T0 + NS, csi_frame()) and ring.dropped == 1
    disk.free = int(2.5e9)                   # needs 1 GB of headroom to resume
    ring.maintain(T0 + 2 * NS)
    assert ring.disk_low
    disk.free = int(3.5e9)
    ring.maintain(T0 + 3 * NS)
    assert not ring.disk_low and ring.write(T0 + 4 * NS, csi_frame())


def test_ring_write_error_is_counted(tmp_path):
    cfg = cfg_for(tmp_path)
    (tmp_path / "ring").write_text("a file where the ring directory should be")
    ring = Ring(cfg, {}, FreeDisk())
    assert not ring.write(T0, csi_frame())
    assert ring.write_errors == 1 and ring.dropped == 1 and "cannot open" in ring.last_error


# ── keep requests ────────────────────────────────────────────────────────
def test_parse_request_forms(tmp_path):
    cfg = cfg_for(tmp_path)
    now = T0
    req = parse_request(b'{"tag": "keep_raw", "last_s": 600, "source": "ha"}', now, cfg)
    assert (req.start_ns, req.end_ns, req.source) == (now - 600 * NS, now, "ha")
    req = parse_request(json.dumps({"tag": "gt_session", "start": T0 / NS - 3600, "end": T0 / NS,
                                    "person": "sq", "note": "sat\nstill", "id": "ha-1"}).encode(),
                        now, cfg)
    assert req.duration_s == 3600 and req.person == "sq" and req.note == "satstill" and req.id == "ha-1"
    iso_now = int(datetime(2026, 10, 15, 5, tzinfo=timezone.utc).timestamp()) * NS
    iso_req = parse_request(json.dumps({"tag": "x", "start": "2026-10-15T04:00:00Z",
                                        "end": "2026-10-15T12:30:00+08:00"}).encode(),
                            iso_now, cfg)
    assert iso_req.duration_s == 1800 and iso_req.start_ns == iso_now - HOUR_NS
    future = parse_request(json.dumps({"tag": "next", "start": T0 / NS, "end": T0 / NS + 1200}).encode(),
                           now, cfg)
    assert future.end_ns > now


@pytest.mark.parametrize("payload, reason", [
    (b"not json", "not JSON"),
    (b"[1]", "object"),
    (b'{"tag": "Bad Tag", "last_s": 60}', "tag"),
    (b'{"tag": "x"}', "last_s"),
    (b'{"tag": "x", "last_s": -5}', "positive"),
    (b'{"tag": "x", "last_s": true}', "positive"),
    (b'{"tag": "x", "last_s": 60, "start": 1}', "either"),
    (b'{"tag": "x", "last_s": 7201}', "KEEP_MAX_MIN"),
    (b'{"tag": "x", "start": "2026-10-15T04:00:00", "end": "2026-10-15T05:00:00"}', "timezone"),
    (b'{"tag": "x", "start": 100, "end": 50}', "after start"),
    (b'{"tag": "x", "last_s": 60, "person": "Alice B"}', "person"),
    (b'{"tag": "x", "last_s": 60, "id": "has space"}', "id"),
    (b'{"tag": "x", "last_s": 1e300}', "KEEP_MAX_MIN"),
    (b'{"tag": "x", "start": 1e300, "end": 2e300}', "epoch seconds"),
    (b'{"tag": "x", "start": 1792000000000, "end": 1792000060000}', "epoch seconds"),
])
def test_parse_request_rejects(tmp_path, payload, reason):
    with pytest.raises(KeepError, match=reason):
        parse_request(payload, T0, cfg_for(tmp_path))


def test_parse_request_range_limits(tmp_path):
    cfg = cfg_for(tmp_path, ring_hours=72)
    t = T0 / NS
    with pytest.raises(KeepError, match="future"):
        parse_request(json.dumps({"tag": "x", "start": t + 7000, "end": t + 7300}).encode(), T0, cfg)
    with pytest.raises(KeepError, match="ring"):
        parse_request(json.dumps({"tag": "x", "start": t - 73 * 3600, "end": t - 72.5 * 3600}).encode(),
                      T0, cfg)
    with pytest.raises(KeepError, match="4096"):
        parse_request(json.dumps({"tag": "x", "last_s": 1, "note": "n" * 5000}).encode(), T0, cfg)


def test_budget_duplicates_and_persistence(tmp_path):
    cfg = cfg_for(tmp_path, keep_max_h_per_day=1)
    store = KeepStore(cfg)
    store.load()
    t = (T0 // (24 * HOUR_NS)) * 24 * HOUR_NS + 12 * HOUR_NS      # midday UTC
    a = KeepRequest("a", "gt_session", t - 40 * 60 * NS, t, t)
    assert store.add(a, t) == "accepted"
    assert store.add(KeepRequest("a2", "gt_session", a.start_ns, a.end_ns, t), t) == "duplicate"
    assert store.add(KeepRequest("a", "other", t - 60 * NS, t, t), t) == "duplicate"   # same id
    with pytest.raises(KeepError, match="budget"):
        store.add(KeepRequest("b", "alcohol", t - 30 * 60 * NS, t, t), t)
    assert store.add(KeepRequest("c", "alcohol", t - 20 * 60 * NS, t, t), t) == "accepted"
    assert store.snapshot == {"pending": 2, "today_min": 60.0}

    again = KeepStore(cfg)
    again.load()
    assert [r.id for r in again.pending] == ["a", "c"]
    assert again.due(t) == [] and [r.id for r in again.due(t + 8 * NS)] == ["a", "c"]
    off = KeepStore(cfg_for(tmp_path / "off", keep_max_h_per_day=0))
    off.load()
    with pytest.raises(KeepError, match="off"):
        off.add(KeepRequest("d", "x", t - NS, t, t), t)


def test_pending_file_damage_is_survived(tmp_path):
    cfg = cfg_for(tmp_path)
    cfg.keep_dir.mkdir(parents=True)
    (cfg.keep_dir / "pending.json").write_text(json.dumps({"pending": [
        {"id": "ok", "tag": "x", "start_ns": 1, "end_ns": 2, "received_ns": 2},
        {"id": "bad", "tag": "Bad Tag", "start_ns": 1, "end_ns": 2, "received_ns": 2},
        {"nonsense": True},
    ]}))
    leftover = cfg.keep_dir / "2026-10-15" / ("x" + R.SUFFIX + ".part")
    leftover.parent.mkdir()
    leftover.write_bytes(b"half a clip")
    store = KeepStore(cfg)
    store.load()
    assert [r.id for r in store.pending] == ["ok"] and not leftover.exists()
    (cfg.keep_dir / "pending.json").write_text("{broken")
    store = KeepStore(cfg)
    store.load()
    assert store.pending == []


def test_extract_clip_from_ring_including_open_file(tmp_path):
    cfg = cfg_for(tmp_path)
    hour0 = (T0 // HOUR_NS) * HOUR_NS
    start = hour0 + HOUR_NS - 60 * NS          # spans two ring files; the second stays open
    ring = fill_ring(cfg, start, seconds=120, hz=10)
    ring.write(start + 121 * NS, sync_packet(2))
    ring.tick(start + 130 * NS)                 # flush
    assert ring.current_path is not None
    store = KeepStore(cfg)
    store.load()
    req = KeepRequest("r1", "gt_session", start + 30 * NS, start + 90 * NS, start, person="sq",
                      note="quiet sitting", source="ha")
    result = store.extract(req, start + 130 * NS)
    assert result["status"] == "kept" and result["records"] == 600
    clip = tmp_path / result["file"]
    assert clip.parent == cfg.keep_dir / R.day(req.start_ns) and "_gt_session" in clip.name
    got = list(R.iter_records(clip))
    assert len(got) == 600 and all(req.start_ns <= ts < req.end_ns for ts, _ in got)
    meta = json.loads(clip.with_name(clip.name.replace(R.SUFFIX, R.META_SUFFIX)).read_text())
    assert meta["tag"] == "gt_session" and meta["person"] == "sq" and meta["records"] == 600
    assert meta["duration_s"] == 60 and meta["coverage"] == 1.0 and meta["kinds"] == {"csi": 600}
    assert R.read_meta(clip)["clip"]["note"] == "quiet sitting"
    assert store.clips(R.day(req.start_ns))[0]["file"] == clip.name
    empty = store.extract(KeepRequest("r2", "x", start - 600 * NS, start - 300 * NS, start), start)
    assert empty["status"] == "empty" and not list(cfg.keep_dir.rglob("*.part"))
    ring.close()


def test_worker_accepts_processes_and_reports(tmp_path):
    cfg = cfg_for(tmp_path)
    start = (T0 // HOUR_NS) * HOUR_NS + 100 * NS
    ring = fill_ring(cfg, start, seconds=60, hz=5)
    ring.close()
    clock = {"now": start + 50 * NS}
    sent = []
    store = KeepStore(cfg)
    store.load()
    worker = KeepWorker(cfg, store, lambda t, p, q, r: sent.append((t, json.loads(p))),
                        threading.Event(), clock=lambda: clock["now"])
    worker.step(b'{"tag": "keep_raw", "last_s": 20, "id": "k1"}')
    worker.step(b'{"tag": "bad tag", "last_s": 20, "id": "k2"}')
    assert [m["status"] for _, m in sent] == ["accepted", "rejected"]
    assert sent[1][1]["id"] == "k2" and "tag" in sent[1][1]["error"]
    clock["now"] += 10 * NS
    worker.step(None)
    topic, kept = sent[-1]
    assert topic == "ruview/recorder/keep/result" and kept["status"] == "kept"
    assert kept["id"] == "k1" and kept["records"] == 100 and store.pending == []


def test_worker_skips_a_clip_written_before_a_crash(tmp_path):
    cfg = cfg_for(tmp_path)
    start = (T0 // HOUR_NS) * HOUR_NS + 100 * NS
    fill_ring(cfg, start, seconds=20, hz=5).close()
    store = KeepStore(cfg)
    store.load()
    req = KeepRequest("c1", "x", start, start + 10 * NS, start)
    store.add(req, start)
    store.extract(req, start + 20 * NS)          # clip written, then "crash"
    again = KeepStore(cfg)
    again.load()
    sent = []
    worker = KeepWorker(cfg, again, lambda t, p, q, r: sent.append(json.loads(p)),
                        threading.Event(), clock=lambda: start + 30 * NS)
    worker.step(None)
    assert sent[0]["status"] == "kept" and sent[0]["records"] == 50 and again.pending == []
    assert len(list(cfg.keep_dir.rglob("*" + R.SUFFIX))) == 1


def test_unsaveable_request_is_refused(tmp_path, monkeypatch):
    store = KeepStore(cfg_for(tmp_path))
    store.load()

    def full(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "_save", full)
    with pytest.raises(KeepError, match="cannot save"):
        store.add(KeepRequest("s1", "x", T0 - NS, T0, T0), T0)
    assert store.pending == []


def test_worker_retries_then_fails(tmp_path, monkeypatch):
    cfg = cfg_for(tmp_path)
    clock = {"now": T0}
    sent = []
    store = KeepStore(cfg)
    store.load()
    worker = KeepWorker(cfg, store, lambda t, p, q, r: sent.append(json.loads(p)),
                        threading.Event(), clock=lambda: clock["now"])
    worker.step(b'{"tag": "x", "last_s": 10, "id": "f1"}')

    def boom(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(store, "extract", boom)
    for _ in range(3):
        clock["now"] += 61 * NS
        worker.step(None)
    assert sent[-1]["status"] == "failed" and "No space" in sent[-1]["error"]
    assert store.pending == [] and len(sent) == 2


def test_echo_only_returns_safe_fields():
    assert _echo(b'{"id": "a", "tag": "b", "x": 1}') == {"id": "a", "tag": "b"}
    assert _echo(b'{"id": 5}') == {} and _echo(b"nope") == {}


# ── stats, status, receive loop ──────────────────────────────────────────
def test_stats_rates():
    stats = Stats(T0)
    for i in range(50):
        stats.add(T0 + i * NS // 25, csi_frame(1, i))
    for i in range(4):
        stats.add(T0 + i * NS // 2, sync_packet(2))
    assert "datagrams_hz" not in stats.snapshot(T0 + NS // 2)       # window too short
    snap = stats.snapshot(T0 + 2 * NS)
    assert snap["datagrams_hz"] == 27 and snap["kinds_hz"] == {"csi": 25, "sync": 2}
    assert snap["nodes"]["1"]["csi_hz"] == 25 and snap["nodes"]["2"]["csi_hz"] == 0
    assert snap["datagrams_total"] == 54


def test_status_message(tmp_path):
    cfg = cfg_for(tmp_path, status_interval_s=5)
    published = []
    rec = Recorder(cfg, publish=lambda *a: published.append(a), clock=lambda: T0,
                   disk_usage=FreeDisk(50))
    rec.keeps.load()
    for i in range(30):
        rec.on_datagram(T0 + i * NS // 10, csi_frame(1, i))
    rec.ring.maintain(T0 + 3 * NS)
    rec.tick(T0 + 3 * NS)
    topic, payload, qos, retain = published[0]
    status = json.loads(payload)
    assert topic == "ruview/recorder/status" and retain and qos == 0
    assert status["recording"] and status["file"].startswith("ring/")
    assert status["disk_free_gb"] == 50 and not status["disk_low"] and status["keep_budget_min"] == 120
    assert status["nodes"]["1"]["csi_hz"] == 10 and status["ring_files"] == 1
    rec.tick(T0 + 4 * NS)
    assert len(published) == 1                                          # next one after 5 s
    rec.ring.close()


def test_serve_records_real_udp(tmp_path):
    cfg = cfg_for(tmp_path)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(0.2)
    port = sock.getsockname()[1]
    rec = Recorder(cfg, disk_usage=FreeDisk())
    stop = threading.Event()
    thread = threading.Thread(target=rec.serve, args=(sock, stop))
    thread.start()
    sent = [csi_frame(1, i) for i in range(50)] + [sync_packet(3)]
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as tx:
        for data in sent:
            tx.sendto(data, ("127.0.0.1", port))
    deadline = time.time() + 5
    while rec.stats.total < len(sent) and time.time() < deadline:
        time.sleep(0.02)
    stop.set()
    thread.join(timeout=5)
    sock.close()
    files = R.list_files(cfg.ring_dir)
    got = [d for f in files for _, d in R.iter_records(f.path)]
    assert got == sent


def test_export_and_replay_tools(tmp_path, capsys):
    cfg = cfg_for(tmp_path)
    ring = fill_ring(cfg, T0, seconds=3, hz=10)
    ring.write(T0 + 4 * NS, sync_packet(2))
    ring.close()
    out = tmp_path / "out.csi.jsonl"
    assert tools.main(["export", str(cfg.ring_dir), "-o", str(out),
                       "--start", str(T0 / NS + 1), "--end", str(T0 / NS + 2)]) == 0
    lines = [json.loads(line) for line in out.read_text().splitlines()]
    assert len(lines) == 10 and {line["type"] for line in lines} == {"raw_csi"}
    assert tools.main(["export", str(cfg.ring_dir), "-o", str(out)]) == 1      # never overwrites

    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(2)
    port = rx.getsockname()[1]
    assert tools.main(["replay", str(cfg.ring_dir), "--target", f"127.0.0.1:{port}",
                       "--speed", "0"]) == 0
    received = [rx.recv(65535) for _ in range(31)]
    rx.close()
    assert received[0] == csi_frame(1, 0) and received[-1] == sync_packet(2)
    assert tools.main(["replay", str(cfg.ring_dir), "--target", "192.0.2.10:5005"]) == 2
    capsys.readouterr()
    assert tools.main(["info", "--data-dir", str(tmp_path), "--json"]) == 0
    info = json.loads(capsys.readouterr().out)
    assert info["ring"]["files"] == 1 and info["clips"] == [] and info["pending"] == []
