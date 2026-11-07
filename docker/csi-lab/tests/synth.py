"""Synthetic RuView CSI recordings for csi_lab tests (SYNTHETIC, not data).

Three single-antenna nodes (bedroom, study, living room) each hear the access
point and the other two nodes on 128-entry HT frames, plus a neighbour on
64-entry legacy frames. Every transmitter-receiver pair has its own static
multipath channel and RSSI, so a node's 128-wide stream mixes three links.
Per frame: AGC gain jitter, random common phase and timing slope, complex
noise, int8 quantisation, and a slow drift. A person in a room adds a body
path to the links that touch the room: strongly where the receiver is in the
room, less where the transmitter is, weakly elsewhere. "moving" scrambles the
body path phase frame by frame; "still" keeps it fixed with a breathing wobble.
"""

from __future__ import annotations

import gzip
import zlib
import json
import struct
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np

T0 = 1_792_000_800.0                 # 2026-10-14T18:00:00Z
ROOMS = {1: "bedroom", 2: "study", 3: "living"}
AP_ROOM = "living"
DF_NS = 312.5e3 * 1e-9               # subcarrier spacing x 1 ns


@dataclass
class Activity:
    start: float                      # seconds after T0
    end: float
    room: str
    kind: str                         # "moving" or "still"


def _freq_block() -> np.ndarray:
    i = np.arange(64)
    return np.where(i < 32, i, i - 64)


def _channel(rng: np.random.Generator, paths: int = 5) -> tuple[np.ndarray, np.ndarray]:
    gains = (rng.normal(size=paths) + 1j * rng.normal(size=paths)) * np.exp(-np.arange(paths) * 0.5)
    delays = np.sort(rng.uniform(0, 250, size=paths))
    return gains, delays


def _response(gains: np.ndarray, delays: np.ndarray, f: np.ndarray) -> np.ndarray:
    return (gains[None, :] * np.exp(-2j * np.pi * f[:, None] * DF_NS * delays[None, :])).sum(1)


def _coupling(room: str, rx_room: str, tx_room: str | None) -> float:
    if rx_room == room:
        return 1.0
    if tx_room == room:
        return 0.5
    return 0.12


def generate(path: Path, duration_s: float, activities: list[Activity], seed: int = 1,
             body: float = 0.35) -> dict:
    """Write one .csi.raw.gz file; return frames per receiver<-transmitter link.
    The noise-floor byte carries -90 - transmitter index (ground truth for
    the clustering test; csi_lab does not use it)."""
    rng = np.random.default_rng(seed)
    body_gain = body
    f = _freq_block()
    null = (f == 0) | (np.abs(f) > 26)
    null_ht = (f == 0) | (np.abs(f) > 28)
    records: list[tuple[int, bytes]] = []
    truth: dict[str, int] = {}
    seq = {n: 0 for n in ROOMS}
    transmitters = [("ap", AP_ROOM, 6.0, 128, -52.0)] + \
        [(f"node{n}", ROOMS[n], 2.0, 128, None) for n in ROOMS] + \
        [("neighbour", None, 2.0, 64, -80.0)]
    for rx, rx_room in ROOMS.items():
        for tx_index, (tx, tx_room, rate, width, rssi0) in enumerate(transmitters):
            if tx == f"node{rx}":
                continue
            rssi_level = rssi0 if rssi0 is not None else float(rng.uniform(-72, -62))
            gains, delays = _channel(rng)
            n = rng.poisson(rate * duration_s)
            t = np.sort(rng.uniform(0, duration_s, size=n))
            h0 = _response(gains, delays, f)
            drift = 1.0 + 0.01 * np.sin(2 * np.pi * t / 1800.0 + rng.uniform(0, 6.28))
            h = h0[None, :] * drift[:, None]
            rssi = rssi_level + rng.normal(0, 0.8, size=n)
            for act in activities:
                c = _coupling(act.room, rx_room, tx_room)
                inside = (t >= act.start) & (t < act.end)
                if not np.any(inside):
                    continue
                bg, bd = _channel(np.random.default_rng(zlib.crc32(f"{seed}:{rx}:{tx}:{act.room}".encode())), paths=2)
                body = _response(bg, bd + 5.0, f)
                body *= body_gain * c * np.abs(h0).mean() / max(np.abs(body).mean(), 1e-9)
                if act.kind == "moving":
                    theta = rng.uniform(0, 2 * np.pi, size=inside.sum())
                    scale = rng.uniform(0.5, 1.5, size=inside.sum())
                else:
                    tt = t[inside]
                    theta = 1.0 + 0.4 * np.sin(2 * np.pi * 0.25 * tt)
                    scale = np.ones(inside.sum())
                h[inside] += (scale * np.exp(1j * theta))[:, None] * body[None, :]
                rssi[inside] -= 2.0 * c + (rng.normal(0, 1.0 * c, size=inside.sum())
                                           if act.kind == "moving" else 0.0)
            # receiver chain: AGC to a fixed level, jitter, phase garbage, noise
            level = 22.0 / np.abs(h).mean(axis=1, keepdims=True)
            agc = np.exp(rng.normal(0, 0.08, size=(n, 1)))
            beta = rng.uniform(0, 2 * np.pi, size=(n, 1))
            slope = rng.normal(0, 0.05, size=(n, 1))
            meas = h * level * agc * np.exp(1j * (beta + slope * f[None, :]))
            blocks = []
            nb = width // 64
            for b in range(nb):
                noisy = meas + rng.normal(0, 1.0, size=meas.shape) + 1j * rng.normal(0, 1.0, size=meas.shape)
                noisy[:, null if b == 0 else null_ht] = 0
                blocks.append(noisy)
            full = np.concatenate(blocks, axis=1)
            iq = np.empty((n, width, 2), dtype=np.int8)
            iq[:, :, 0] = np.clip(np.round(full.imag), -127, 127)
            iq[:, :, 1] = np.clip(np.round(full.real), -127, 127)
            for k in range(n):
                header = struct.pack("<IBBHIIbbBB", 0xC5110001, rx, 1, width, 2437, seq[rx],
                                     int(np.clip(round(rssi[k]), -100, -10)), -90 - tx_index, 0, 0)
                seq[rx] += 1
                records.append((int((T0 + t[k]) * 1e9), header + iq[k].tobytes()))
            truth[f"{rx}<-{tx}"] = int(n)
    records.sort(key=lambda r: r[0])
    meta = json.dumps({"format": "ruview-csi-raw", "version": 1, "synthetic": True}).encode()
    with gzip.open(path, "wb", compresslevel=1) as fp:
        fp.write(struct.pack("<8sHI", b"RVCSIRAW", 1, len(meta)) + meta)
        for ts_ns, data in records:
            fp.write(struct.pack("<QI", ts_ns, len(data)) + data)
    return truth


def ring_path(root: Path) -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(T0))
    day = time.strftime("%Y-%m-%d", time.gmtime(T0))
    d = root / "ring" / day
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{stamp}.csi.raw.gz"


def iso(offset_s: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(T0 + offset_s))
