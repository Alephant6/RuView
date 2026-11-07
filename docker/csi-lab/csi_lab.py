#!/usr/bin/env python3
"""csi_lab: offline accuracy lab for RuView raw CSI recordings.

One file, numpy only, so it can be piped into a throwaway container on the
NAS, where the raw CSI lives:

    docker run --rm -i -v "$PWD/data/csi:/data:ro" python:3.12-slim \
      sh -c 'pip -q install numpy && python - evaluate --labels-json "$L"' < csi_lab.py

It reads csi-recorder `.csi.raw.gz` files (ring/ and keep/) read-only and
prints aggregates only: frame rates, RSSI quantiles, false-alarm and
detection rates, AUCs. No frame, I/Q value or per-window series leaves the
process (ADR-299: raw CSI is person data).

What it measures, for a set of labelled time segments:

1. Transmitter separation. A node in promiscuous mode hears several
   transmitters (the access point, the other nodes, neighbours, phones).
   Each one is a different radio link with its own static channel shape, so
   mixing them makes the "empty room" look noisy. Frames of one node and
   width are clustered by gain-normalised amplitude shape plus RSSI (the
   frame header carries no source address); every cluster is scored as its
   own link.
2. Per-link features over short windows, each relative to the empty-home
   reference: `static` (shift of the mean amplitude shape, after projecting
   out the main empty-home variation), `motion` (within-window variance),
   `phase` (shift of the detrended phase shape) and `rssi`.
3. Honest evaluation. Empty-home data is cut into chunks; each chunk is held
   out in turn while the others form the reference (baseline and threshold
   at the reference's 99th percentile), so the false-alarm rate is measured
   on data the detector has never seen. Detection rates and per-room AUCs
   use the labelled occupied segments. Longer horizons average consecutive
   windows.

Every number this prints is MEASURED on the given segments only; a handful
of segments from one evening is a screen, not a validation.
"""

from __future__ import annotations

import argparse
import calendar
import gzip
import json
import math
import re
import struct
import sys
import time
import warnings
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

import numpy as np

VERSION = 2
NS = 1_000_000_000

# --- csi-recorder container (docker/csi-recorder/csi_recorder/rawfile.py) ---
RAW_MAGIC = b"RVCSIRAW"
RAW_SUFFIX = ".csi.raw.gz"
_RAW_HEADER = struct.Struct("<8sHI")
_RAW_RECORD = struct.Struct("<QI")
_RAW_NAME = re.compile(r"^(\d{8}T\d{6})Z.*" + re.escape(RAW_SUFFIX) + "$")
MAX_DATAGRAM = 65535

# --- ADR-018 CSI frame ---
ADR018_MAGIC = 0xC5110001
_ADR018 = struct.Struct("<IBBHIIbbBB")   # magic node n_ant n_sub freq seq rssi nf ppdu flags
FLAG_FIRST_WORD_SANITIZED = 1 << 5

SCORES = ("static", "motion", "phase", "rssi")
# Combined scores: `fused` = mean exceedance of the four, `fused_mr` = of
# motion and rssi only (the two that do not compare against a stored channel
# shape), `fused_med` = median over links instead of the mean (node and
# home only; one drifting link cannot carry it).
COMBINED = ("fused", "fused_mr", "fused_med")


class LabError(ValueError):
    pass


# ---------------------------------------------------------------- raw files

def parse_utc(value: Any) -> float:
    """Epoch seconds from epoch seconds or ISO 8601 with a timezone."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str):
        raise LabError(f"bad time {value!r}")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise LabError(f"bad time {value!r}") from exc
    if dt.tzinfo is None:
        raise LabError(f"time without timezone: {value!r}")
    return dt.timestamp()


def iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def list_raw(root: Path) -> list[tuple[float, float, Path]]:
    """(created s, last write s, path) of every raw file below root."""
    out = []
    for path in root.rglob("*" + RAW_SUFFIX):
        m = _RAW_NAME.match(path.name)
        if not m or not path.is_file():
            continue
        created = calendar.timegm(time.strptime(m.group(1), "%Y%m%dT%H%M%S"))
        try:
            mtime = path.stat().st_mtime
        except FileNotFoundError:
            continue
        out.append((float(created), mtime, path))
    out.sort()
    return out


def iter_raw(path: Path) -> Iterator[tuple[int, bytes]]:
    """(receive time ns, datagram); a cut-short file ends quietly."""
    with gzip.open(path, "rb") as fp:
        head = fp.read(_RAW_HEADER.size)
        if len(head) < _RAW_HEADER.size:
            return
        magic, _version, n = _RAW_HEADER.unpack(head)
        if magic != RAW_MAGIC or n > 64 * 1024:
            raise LabError(f"{path.name}: not a RuView CSI raw file")
        fp.read(n)
        try:
            while True:
                rec = fp.read(_RAW_RECORD.size)
                if len(rec) < _RAW_RECORD.size:
                    return
                ts_ns, size = _RAW_RECORD.unpack(rec)
                if size > MAX_DATAGRAM:
                    return
                data = fp.read(size)
                if len(data) < size:
                    return
                yield ts_ns, data
        except (EOFError, zlib.error, gzip.BadGzipFile):
            return


def parse_frame(data: bytes) -> tuple[int, int, int, int, int, bytes] | None:
    """(node, n_sub, rssi, noise floor, flags, int8 I/Q pairs as bytes) of an
    ADR-018 single-antenna CSI frame, or None."""
    if len(data) < _ADR018.size:
        return None
    magic, node, n_ant, n_sub, _freq, _seq, rssi, nf, _ppdu, flags = _ADR018.unpack_from(data)
    if magic != ADR018_MAGIC or n_ant != 1 or not 8 <= n_sub <= 512:
        return None
    end = _ADR018.size + 2 * n_sub
    if len(data) < end:
        return None
    # A bytes copy of just the I/Q (not a view that keeps the whole datagram
    # and an array object alive per frame): about half the memory per frame.
    return node, n_sub, rssi, nf, flags, data[_ADR018.size:end]


# ------------------------------------------------------------------ labels

@dataclass
class Segment:
    name: str
    start: float
    end: float
    occupied: list[str] | None      # [] = empty home, None = unknown
    note: str = ""
    parent: str = ""                # chunk of which labelled segment
    tags: tuple[str, ...] = ()      # confounders, e.g. "vacuum:kitchen"
    at_least: bool = False          # occupied lists rooms known to be occupied;
                                    # the other rooms are unknown (event labels)

    @property
    def role(self) -> str:
        # A tagged segment has something else moving in it (the robot vacuum,
        # a fan): scored and reported, never part of the empty-home reference,
        # the AUCs or the localization.
        if self.tags:
            return "confounded"
        if self.occupied is None:
            return "unknown"
        return "empty" if not self.occupied else "occupied"


@dataclass
class Labels:
    rooms: dict[int, str]
    segments: list[Segment]
    exclude: list[tuple[float, float, str]]


_NAME_OK = re.compile(r"^[A-Za-z0-9_.:+-]{1,48}$")


def parse_labels(obj: Any) -> Labels:
    if not isinstance(obj, dict):
        raise LabError("labels must be a JSON object")
    rooms_raw = obj.get("rooms")
    if not isinstance(rooms_raw, dict) or not rooms_raw:
        raise LabError('labels need "rooms": {"<node id>": "<room>"}')
    rooms: dict[int, str] = {}
    for key, room in rooms_raw.items():
        try:
            node = int(key)
        except (TypeError, ValueError) as exc:
            raise LabError(f"bad node id {key!r}") from exc
        if not 0 <= node <= 255 or not isinstance(room, str) or not _NAME_OK.match(room):
            raise LabError(f"bad room for node {key!r}")
        rooms[node] = room
    known = set(rooms.values())
    segs = []
    names = set()
    for raw in obj.get("segments") or []:
        if not isinstance(raw, dict):
            raise LabError("each segment must be an object")
        name = raw.get("name")
        if not isinstance(name, str) or not _NAME_OK.match(name) or name in names:
            raise LabError(f"bad or repeated segment name {name!r}")
        names.add(name)
        start, end = parse_utc(raw.get("start")), parse_utc(raw.get("end"))
        if not end > start:
            raise LabError(f"segment {name}: end must be after start")
        if end - start > 12 * 3600:
            raise LabError(f"segment {name}: longer than 12 h")
        occ = raw.get("occupied", None)
        if occ is not None:
            if not isinstance(occ, list) or any(r not in known for r in occ):
                raise LabError(f"segment {name}: occupied must list rooms from {sorted(known)}")
            occ = sorted(set(occ))
        tags = raw.get("tags") or []
        if not isinstance(tags, list) or len(tags) > 4 or any(
                not isinstance(t, str) or not _NAME_OK.match(t) for t in tags):
            raise LabError(f"segment {name}: tags must be up to 4 short names")
        at_least = raw.get("at_least", False)
        if not isinstance(at_least, bool) or (at_least and not occ):
            raise LabError(f"segment {name}: at_least needs a non-empty occupied list")
        segs.append(Segment(name, start, end, occ, str(raw.get("note", ""))[:200],
                            tags=tuple(tags), at_least=at_least))
    if not segs:
        raise LabError("labels need at least one segment")
    segs.sort(key=lambda s: s.start)
    for a, b in zip(segs, segs[1:]):
        if b.start < a.end:
            raise LabError(f"segments {a.name} and {b.name} overlap")
    exclude = []
    for raw in obj.get("exclude") or []:
        if not isinstance(raw, dict):
            raise LabError("each exclude must be an object")
        s, e = parse_utc(raw.get("start")), parse_utc(raw.get("end"))
        if not e > s:
            raise LabError("exclude: end must be after start")
        exclude.append((s, e, str(raw.get("why", ""))[:80]))
    return Labels(rooms, segs, exclude)


def chunk_empty(segments: list[Segment], chunk_s: float) -> list[Segment]:
    """Cut empty segments into folds of about chunk_s (at least two in total)."""
    out = []
    empties = [s for s in segments if s.role == "empty"]
    total = sum(s.end - s.start for s in empties)
    for seg in segments:
        if seg.role != "empty":
            out.append(seg)
            continue
        dur = seg.end - seg.start
        n = max(1, int(dur // chunk_s))
        if len(empties) == 1:
            n = max(n, 2)
        edges = np.linspace(seg.start, seg.end, n + 1)
        for i in range(n):
            out.append(Segment(f"{seg.name}#{i}", float(edges[i]), float(edges[i + 1]),
                               [], seg.note, parent=seg.name))
    if total and sum(1 for s in out if s.role == "empty") < 2:
        raise LabError("need at least two empty-home chunks")
    return out


# ------------------------------------------------------------------ frames

@dataclass
class Stream:
    """All frames of one node and one subcarrier count, in time order."""
    node: int
    width: int
    t: np.ndarray            # float64 epoch seconds
    seg: np.ndarray          # int16 index into the evaluated segments
    rssi: np.ndarray         # float32 dBm
    nla: np.ndarray          # float32 (n, K) gain-normalised log amplitude
    ph: np.ndarray           # float16 (n, K) detrended phase, radians in [-pi, pi]
    valid: np.ndarray        # indices of the K kept subcarriers in the frame
    nf: np.ndarray           # int8 noise floor from the frame header

    @property
    def key(self) -> str:
        return f"n{self.node}w{self.width}"


def subcarrier_freq(width: int) -> list[np.ndarray]:
    """Per 64-entry block, the signed subcarrier index of every entry
    (ESP-IDF order 0..31, -32..-1). Other widths: one centred block."""
    if width % 64 == 0:
        base = np.where(np.arange(64) < 32, np.arange(64), np.arange(64) - 64)
        return [base.copy() for _ in range(width // 64)]
    return [np.arange(width) - width // 2]


def prepare(node: int, width: int, t: list[float], seg: list[int], rssi: list[int],
            iq: list[bytes], nf: list[int] | None = None) -> Stream | None:
    n = len(t)
    if n < 20:
        return None
    raw = np.frombuffer(b"".join(iq), dtype=np.int8).reshape(n, width, 2).astype(np.float32)
    # ESP-IDF stores (imaginary, real); amplitude does not care, and the
    # phase features only use shape changes, so the sign convention is moot.
    c = raw[:, :, 1] + 1j * raw[:, :, 0]
    amp = np.abs(c)
    # Null (guard/DC) subcarriers: median amplitude far below the rest.
    sample = amp[:: max(1, n // 4000)]
    med = np.median(sample, axis=0)
    good = med >= 0.25 * np.median(med[med > 0]) if np.any(med > 0) else med > 0
    blocks = subcarrier_freq(width)
    offset = 0
    valid_idx = []
    phase_parts = []
    for freq in blocks:
        size = len(freq)
        idx = np.arange(offset, offset + size)
        keep = good[idx].copy()
        keep[:2] = False                 # DC entry and the sanitised first word
        idx = idx[keep]
        f = freq[keep]
        order = np.argsort(f)
        idx, f = idx[order], f[order]
        offset += size
        if len(idx) < 8:
            continue
        valid_idx.append(idx)
        phi = np.unwrap(np.angle(c[:, idx]), axis=1)
        design = np.stack([f.astype(np.float64), np.ones(len(f))], axis=1)
        coef = phi @ np.linalg.pinv(design).T
        resid = phi - coef @ design.T
        phase_parts.append(np.angle(np.exp(1j * resid)).astype(np.float16))
    if not valid_idx:
        return None
    valid = np.concatenate(valid_idx)
    la = np.log(amp[:, valid] + 1.0)
    nla = (la - la.mean(axis=1, keepdims=True)).astype(np.float32)
    return Stream(node, width, np.asarray(t), np.asarray(seg, dtype=np.int16),
                  np.asarray(rssi, dtype=np.float32), nla,
                  np.concatenate(phase_parts, axis=1), valid,
                  np.asarray(nf if nf is not None else np.zeros(n), dtype=np.int8))


def load_streams(root: Path, segments: list[Segment],
                 exclude: list[tuple[float, float, str]]) -> tuple[list[Stream], dict[str, int]]:
    starts = np.array([s.start for s in segments])
    ends = np.array([s.end for s in segments])
    lo, hi = float(starts.min()), float(ends.max())
    spans = list(zip(starts.tolist(), ends.tolist()))
    acc: dict[tuple[int, int], tuple[list, list, list, list, list]] = {}
    counts = {"datagrams": 0, "csi_frames": 0, "excluded": 0, "multi_antenna_or_bad": 0}
    for created, mtime, path in list_raw(root):
        if created > hi + 10 or mtime + 10 < lo:
            continue
        # Short event segments spread over days: open only the files that
        # overlap one of them.
        if not any(created <= e + 10 and mtime + 10 >= a for a, e in spans):
            continue
        try:
            for ts_ns, data in iter_raw(path):
                ts = ts_ns / NS
                if ts < lo or ts >= hi:
                    continue
                counts["datagrams"] += 1
                i = int(np.searchsorted(starts, ts, side="right")) - 1
                if i < 0 or ts >= ends[i]:
                    continue
                if any(a <= ts < b for a, b, _ in exclude):
                    counts["excluded"] += 1
                    continue
                frame = parse_frame(data)
                if frame is None:
                    if len(data) >= 4 and int.from_bytes(data[:4], "little") == ADR018_MAGIC:
                        counts["multi_antenna_or_bad"] += 1
                    continue
                node, n_sub, rssi, nf, _flags, iq = frame
                counts["csi_frames"] += 1
                bucket = acc.setdefault((node, n_sub), ([], [], [], [], []))
                bucket[0].append(ts)
                bucket[1].append(i)
                bucket[2].append(rssi)
                bucket[3].append(iq)
                bucket[4].append(nf)
        except (OSError, LabError) as exc:
            print(f"warning: skipping {path.name}: {exc}", file=sys.stderr)
    streams = []
    counts["duplicates"] = 0
    for (node, width), (t, seg, rssi, iq, nf) in sorted(acc.items()):
        # A kept clip is a copy of a ring range: the same datagram can appear
        # in ring/ and keep/. Same node and same receive time = same datagram.
        ta = np.asarray(t)
        order = np.argsort(ta, kind="stable")
        first = np.concatenate([[True], np.diff(ta[order]) != 0])
        counts["duplicates"] += int((~first).sum())
        order = order[first]
        stream = prepare(node, width, [t[k] for k in order], [seg[k] for k in order],
                         [rssi[k] for k in order], [iq[k] for k in order], [nf[k] for k in order])
        if stream is not None:
            streams.append(stream)
    return streams, counts


# -------------------------------------------------------------- clustering

def kmeans(x: np.ndarray, k: int, rng: np.random.Generator, iters: int = 40,
           restarts: int = 3) -> tuple[np.ndarray, np.ndarray, float]:
    best = None
    for _ in range(restarts):
        cent = [x[rng.integers(len(x))]]
        for _ in range(1, k):
            d2 = np.min([np.sum((x - c) ** 2, axis=1) for c in cent], axis=0)
            p = d2 / d2.sum() if d2.sum() > 0 else None
            cent.append(x[rng.choice(len(x), p=p)])
        cent = np.array(cent)
        for _ in range(iters):
            d = ((x[:, None, :] - cent[None]) ** 2).sum(-1)
            lab = d.argmin(1)
            new = np.array([x[lab == j].mean(0) if np.any(lab == j) else cent[j] for j in range(k)])
            if np.allclose(new, cent):
                break
            cent = new
        d = ((x[:, None, :] - cent[None]) ** 2).sum(-1)
        lab = d.argmin(1)
        inertia = float(d[np.arange(len(x)), lab].sum())
        if best is None or inertia < best[2]:
            best = (cent, lab, inertia)
    assert best is not None
    return best


def silhouette(x: np.ndarray, lab: np.ndarray) -> float:
    sq = (x ** 2).sum(1)
    d = np.sqrt(np.maximum(sq[:, None] + sq[None] - 2 * x @ x.T, 0))
    ks = np.unique(lab)
    if len(ks) < 2:
        return 0.0
    mean_to = np.stack([d[:, lab == k].mean(1) for k in ks], axis=1)
    own = np.searchsorted(ks, lab)
    sizes = np.array([(lab == k).sum() for k in ks])
    a = mean_to[np.arange(len(x)), own] * sizes[own] / np.maximum(sizes[own] - 1, 1)
    mean_to[np.arange(len(x)), own] = np.inf
    b = mean_to.min(1)
    s = (b - a) / np.maximum(np.maximum(a, b), 1e-12)
    return float(np.mean(s))


@dataclass
class Clusters:
    mu: np.ndarray
    sd: np.ndarray
    rssi_w: float
    cent: np.ndarray
    gate: np.ndarray
    silhouette: float

    @property
    def k(self) -> int:
        return len(self.cent)


def _signature(stream: Stream, idx: np.ndarray, c: "Clusters | None" = None,
               mu=None, sd=None, w=None) -> np.ndarray:
    feat = np.concatenate([stream.nla[idx], stream.rssi[idx, None]], axis=1).astype(np.float64)
    mu = c.mu if c is not None else mu
    sd = c.sd if c is not None else sd
    w = c.rssi_w if c is not None else w
    z = (feat - mu) / sd
    z[:, -1] *= w
    return z


def fit_clusters(stream: Stream, ref_idx: np.ndarray, max_k: int, seed: int = 7) -> Clusters:
    rng = np.random.default_rng(seed + stream.node * 1000 + stream.width)
    sub = ref_idx if len(ref_idx) <= 4000 else rng.choice(ref_idx, 4000, replace=False)
    feat = np.concatenate([stream.nla[sub], stream.rssi[sub, None]], axis=1).astype(np.float64)
    mu, sd = feat.mean(0), feat.std(0) + 1e-6
    w = math.sqrt(stream.nla.shape[1]) / 2.0     # RSSI ~ a quarter of the distance
    z = _signature(stream, sub, mu=mu, sd=sd, w=w)
    best_k, best_s, best = 1, 0.0, None
    if max_k >= 2 and len(z) >= 60:
        small = z if len(z) <= 1500 else z[rng.choice(len(z), 1500, replace=False)]
        for k in range(2, max_k + 1):
            cent, _lab, _ = kmeans(z, k, rng)
            lab_small = (((small[:, None] - cent[None]) ** 2).sum(-1)).argmin(1)
            shares = np.bincount(lab_small, minlength=k) / len(small)
            if shares.min() < 0.03:
                continue
            s = silhouette(small, lab_small)
            if s > best_s:
                best_k, best_s, best = k, s, cent
    if best is None or best_s < 0.4:
        best_k, best = 1, z.mean(0, keepdims=True)
        best_s = 0.0
    d = np.sqrt((((z[:, None] - best[None]) ** 2).sum(-1)))
    lab = d.argmin(1)
    gate = np.array([3.0 * np.quantile(d[lab == j, j], 0.995) if np.any(lab == j) else np.inf
                     for j in range(best_k)])
    return Clusters(mu, sd, w, best, gate, best_s)


def assign(stream: Stream, c: Clusters) -> tuple[np.ndarray, np.ndarray]:
    """(cluster id, far flag) per frame. Every frame goes to its nearest
    reference transmitter: a person near the receiver also changes the
    amplitude shape, so dropping "far" frames would drop exactly the frames
    with a person in them. `far` (beyond 3x the reference p99.5 distance)
    is reported as a diagnostic only."""
    lab_out = np.empty(len(stream.t), dtype=np.int16)
    far_out = np.zeros(len(stream.t), dtype=bool)
    for lo in range(0, len(stream.t), 4000):
        idx = np.arange(lo, min(lo + 4000, len(stream.t)))
        z = _signature(stream, idx, c)
        d = np.sqrt(((z[:, None] - c.cent[None]) ** 2).sum(-1))
        lab = d.argmin(1)
        far_out[idx] = d[np.arange(len(idx)), lab] > c.gate[lab]
        lab_out[idx] = lab
    return lab_out, far_out


# ---------------------------------------------------------------- features

@dataclass
class WindowSummary:
    """Per-window summaries of one link; rows align with the global window list."""
    m: np.ndarray        # (W, K) mean normalised log amplitude
    v: np.ndarray        # (W, K) within-window variance
    z: np.ndarray        # (W, K) complex mean phasor
    r: np.ndarray        # (W,) mean RSSI
    n: np.ndarray        # (W,) frames


def make_windows(segments: list[Segment], window_s: float, hop_s: float,
                 exclude: list[tuple[float, float, str]]) -> tuple[np.ndarray, np.ndarray]:
    """(start times, segment index) of every window fully inside a segment and
    clear of every excluded range."""
    starts, segs = [], []
    for i, seg in enumerate(segments):
        t = seg.start
        while t + window_s <= seg.end + 1e-9:
            if not any(a < t + window_s and t < b for a, b, _ in exclude):
                starts.append(t)
                segs.append(i)
            t += hop_s
    return np.asarray(starts), np.asarray(segs, dtype=np.int32)


def summarise(stream: Stream, frames: np.ndarray, w_start: np.ndarray, window_s: float,
              min_frames: int) -> WindowSummary:
    t = stream.t[frames]
    k = stream.nla.shape[1]
    nw = len(w_start)
    m = np.full((nw, k), np.nan, np.float32)
    v = np.full((nw, k), np.nan, np.float32)
    z = np.full((nw, k), np.nan, np.complex64)
    r = np.full(nw, np.nan, np.float32)
    n = np.zeros(nw, np.int32)
    lo = np.searchsorted(t, w_start, side="left")
    hi = np.searchsorted(t, w_start + window_s, side="left")
    for w in range(nw):
        cnt = hi[w] - lo[w]
        n[w] = cnt
        if cnt < min_frames:
            continue
        sel = frames[lo[w]:hi[w]]
        x = stream.nla[sel]
        m[w] = x.mean(0)
        v[w] = x.var(0)
        z[w] = np.exp(1j * stream.ph[sel].astype(np.float32)).mean(0)
        r[w] = stream.rssi[sel].mean()
    return WindowSummary(m, v, z, r, n)


@dataclass
class LinkModel:
    mu: np.ndarray
    sd: np.ndarray
    modes: np.ndarray
    nu: np.ndarray
    zmu: np.ndarray
    zsd2: np.ndarray
    rmu: float
    rsd: float


def fit_link(ws: WindowSummary, ref: np.ndarray, n_modes: int) -> LinkModel | None:
    ok = ref[~np.isnan(ws.r[ref])]
    if len(ok) < 8:
        return None
    m = ws.m[ok].astype(np.float64)
    mu = m.mean(0)
    sd = m.std(0) + 1e-3
    zs = (m - mu) / sd
    n_modes = min(n_modes, len(ok) - 2)
    modes = np.linalg.svd(zs, full_matrices=False)[2][:n_modes] if n_modes > 0 else np.zeros((0, len(mu)))
    nu = np.median(ws.v[ok].astype(np.float64), axis=0) + 1e-5
    zz = ws.z[ok].astype(np.complex128)
    zmu = zz.mean(0)
    zsd2 = np.mean(np.abs(zz - zmu) ** 2, axis=0) + 1e-4
    r = ws.r[ok].astype(np.float64)
    return LinkModel(mu, sd, modes, nu, zmu, zsd2, float(r.mean()), float(r.std() + 0.5))


def link_scores(ws: WindowSummary, lm: LinkModel) -> dict[str, np.ndarray]:
    """log10 scores per window (NaN where the window had too few frames)."""
    zs = (ws.m.astype(np.float64) - lm.mu) / lm.sd
    if len(lm.modes):
        zs = zs - (zs @ lm.modes.T) @ lm.modes
    static = np.mean(zs ** 2, axis=1)
    motion = np.median(ws.v.astype(np.float64) / lm.nu, axis=1)
    phase = np.mean(np.abs(ws.z.astype(np.complex128) - lm.zmu) ** 2 / lm.zsd2, axis=1)
    rssi = np.abs(ws.r.astype(np.float64) - lm.rmu) / lm.rsd
    with np.errstate(divide="ignore", invalid="ignore"):
        return {
            "static": np.log10(np.maximum(static, 1e-6)),
            "motion": np.log10(np.maximum(motion, 1e-6)),
            "phase": np.log10(np.maximum(phase, 1e-6)),
            "rssi": np.log10(1.0 + rssi),
        }


# -------------------------------------------------------------- evaluation

def rolling(values: np.ndarray, wseg: np.ndarray, a: int) -> np.ndarray:
    """Mean of `a` consecutive windows of the same segment (NaN-aware; needs
    at least half of them)."""
    if a <= 1:
        return values.copy()
    out = np.full_like(values, np.nan)
    for s in np.unique(wseg):
        idx = np.flatnonzero(wseg == s)
        x = values[idx]
        ok = ~np.isnan(x)
        cs = np.concatenate([[0.0], np.cumsum(np.where(ok, x, 0.0))])
        cn = np.concatenate([[0], np.cumsum(ok)])
        for j in range(a - 1, len(idx)):
            cnt = cn[j + 1] - cn[j + 1 - a]
            if cnt >= (a + 1) // 2:
                out[idx[j]] = (cs[j + 1] - cs[j + 1 - a]) / cnt
    return out


def auc(pos: np.ndarray, neg: np.ndarray) -> float | None:
    pos, neg = pos[~np.isnan(pos)], neg[~np.isnan(neg)]
    if len(pos) < 3 or len(neg) < 3:
        return None
    allv = np.concatenate([pos, neg])
    order = allv.argsort(kind="mergesort")
    ranks = np.empty(len(allv))
    ranks[order] = np.arange(1, len(allv) + 1)
    # average ranks over ties
    sorted_v = allv[order]
    i = 0
    while i < len(sorted_v):
        j = i
        while j + 1 < len(sorted_v) and sorted_v[j + 1] == sorted_v[i]:
            j += 1
        if j > i:
            ranks[order[i:j + 1]] = (i + j + 2) / 2.0
        i = j + 1
    r_pos = ranks[: len(pos)].sum()
    return float((r_pos - len(pos) * (len(pos) + 1) / 2) / (len(pos) * len(neg)))


def _fold_plan(segments: list[Segment], guard: bool) -> tuple[list[tuple[int, list[int]]], bool]:
    empty = [i for i, s in enumerate(segments) if s.role == "empty"]
    plans, relaxed = [], False
    for i in empty:
        ref = [j for j in empty if j != i]
        if guard:
            same = [j for j in ref if segments[j].parent == segments[i].parent
                    and (abs(segments[j].start - segments[i].end) < 1 or abs(segments[i].start - segments[j].end) < 1)]
            guarded = [j for j in ref if j not in same]
            if len(guarded) >= 2:
                ref = guarded
            else:
                relaxed = True
        plans.append((i, ref))
    return plans, relaxed


def _quant(x: np.ndarray, q: float) -> float:
    x = x[~np.isnan(x)]
    return float(np.quantile(x, q)) if len(x) else float("nan")


def _r(x: Any, nd: int = 3) -> Any:
    if x is None:
        return None
    if isinstance(x, float) and (math.isnan(x) or math.isinf(x)):
        return None
    return round(float(x), nd)


@dataclass
class EvalConfig:
    window_s: float = 10.0
    hop_s: float = 5.0
    min_frames: int = 4
    max_k: int = 4
    n_modes: int = 3
    horizons: tuple[float, ...] = (10.0, 30.0, 60.0)
    q: float = 0.99
    guard: bool = True
    chunk_s: float = 600.0


def _oos_scores(ws: WindowSummary, ref_segs: list[int], w_seg: np.ndarray,
                n_modes: int) -> dict[str, np.ndarray] | None:
    """Scores of every window. Reference windows get out-of-sample scores
    (model fitted without their own chunk), so a threshold taken from them
    reflects how a baseline from another time behaves, not how well the model
    fits its own data. Other windows are scored by the model of all chunks."""
    ref_w = np.flatnonzero(np.isin(w_seg, ref_segs))
    lm = fit_link(ws, ref_w, n_modes)
    if lm is None:
        return None
    sc = link_scores(ws, lm)
    if len(ref_segs) < 2:
        return sc
    for j in ref_segs:
        inner = np.flatnonzero(np.isin(w_seg, [k for k in ref_segs if k != j]))
        own = np.flatnonzero(w_seg == j)
        lmj = fit_link(ws, inner, n_modes)
        if lmj is None:
            for v in sc.values():
                v[own] = np.nan
            continue
        scj = link_scores(ws, lmj)
        for name, v in sc.items():
            v[own] = scj[name][own]
    return sc


def _agg(rows: list[np.ndarray], how: str = "mean") -> np.ndarray:
    """NaN-aware mean or median across rows (all-NaN columns stay NaN, quietly)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        m = np.vstack(rows)
        return np.nanmedian(m, axis=0) if how == "median" else np.nanmean(m, axis=0)


def global_links(streams: list[Stream], segments: list[Segment], max_k: int) -> dict[str, tuple[Clusters, np.ndarray, np.ndarray]]:
    """Transmitter clusters per stream fitted on every empty-home frame: the
    names links keep across folds (and in the inventory table)."""
    empty = [i for i, s in enumerate(segments) if s.role == "empty"]
    out = {}
    for stream in streams:
        ref = np.flatnonzero(np.isin(stream.seg, empty)) if empty else np.arange(len(stream.t))
        if len(ref) >= 50:
            c = fit_clusters(stream, ref, max_k)
            lab, far = assign(stream, c)
            out[stream.key] = (c, lab, far)
    return out


def evaluate(streams: list[Stream], labels: Labels, segments: list[Segment],
             cfg: EvalConfig, links: dict[str, tuple[Clusters, np.ndarray, np.ndarray]] | None = None
             ) -> dict[str, Any]:
    w_start, w_seg = make_windows(segments, cfg.window_s, cfg.hop_s, labels.exclude)
    if not len(w_start):
        raise LabError("no complete window inside the segments")
    plans, relaxed = _fold_plan(segments, cfg.guard)
    if len(plans) < 2:
        raise LabError("need at least two empty-home chunks for held-out false alarms")
    room_of = labels.rooms
    node_of_room = {room: node for node, room in room_of.items()}
    horizons = [h for h in cfg.horizons if h >= cfg.window_s]
    acc: dict[str, dict[str, Any]] = {}
    loc: dict[str, dict[str, Any]] = {}
    cluster_info: dict[str, list[int]] = {}
    known = [i for i, sg in enumerate(segments) if sg.role == "occupied"]
    scored = [i for i, sg in enumerate(segments) if sg.role in ("occupied", "unknown", "confounded")]
    names = links if links is not None else global_links(streams, segments, cfg.max_k)

    def accumulate(values: np.ndarray, h: float) -> np.ndarray:
        return rolling(values, w_seg, 1 + int(round((h - cfg.window_s) / cfg.hop_s)))

    def add(name: str, meta: dict[str, Any], values: np.ndarray, ref_w: np.ndarray,
            held_w: np.ndarray, held_name: str) -> None:
        held_mask = np.zeros(len(values), bool)
        held_mask[held_w] = True
        for h in horizons:
            acc_v = accumulate(values, h)
            tau = _quant(acc_v[ref_w], cfg.q)
            if math.isnan(tau):
                continue
            rec = acc.setdefault(f"{name}@{int(h)}s", {**meta, "horizon_s": h, "fa": [], "fa_chunk": {},
                                                      "det": {}, "excess": {}, "auc_room": [],
                                                      "auc_other": []})
            held = acc_v[held_w]
            held = held[~np.isnan(held)]
            if len(held):
                rec["fa"].append(float(np.mean(held > tau)))
                rec["fa_chunk"][held_name] = rec["fa"][-1]
                rec["excess"].setdefault("(held-out empty)", []).append(float(np.median(held) - tau))
            for si in scored:
                v = acc_v[w_seg == si]
                v = v[~np.isnan(v)]
                if len(v):
                    rec["det"].setdefault(segments[si].name, []).append(float(np.mean(v > tau)))
                    rec["excess"].setdefault(segments[si].name, []).append(float(np.median(v) - tau))
            room = meta.get("room")
            if room:
                # An at-least segment says nothing about the rooms it does not
                # list: positive for its own rooms, never a negative.
                pos = np.isin(w_seg, [i for i in known if room in segments[i].occupied])
                other = np.isin(w_seg, [i for i in known if room not in segments[i].occupied
                                        and not segments[i].at_least])
                a1 = auc(acc_v[pos], acc_v[held_mask | other])
                if a1 is not None:
                    rec["auc_room"].append(a1)
                a2 = auc(acc_v[pos], acc_v[other])
                if a2 is not None:
                    rec["auc_other"].append(a2)

    def localize(score: str, node_vals: dict[int, np.ndarray]) -> None:
        nodes = sorted(n for n in node_vals if n in room_of)
        if len(nodes) < 2:
            return
        for h in horizons:
            mat = np.vstack([accumulate(node_vals[n], h) for n in nodes])
            rec = loc.setdefault(f"{score}@{int(h)}s", {"score": score, "horizon_s": h,
                                                       "one_room": [], "two_rooms": [], "confusion": {}})
            for si in known:
                occ = segments[si].occupied or []
                if segments[si].at_least or not 1 <= len(occ) <= 2 or any(r not in node_of_room for r in occ):
                    continue
                cols = np.flatnonzero((w_seg == si) & ~np.any(np.isnan(mat), axis=0))
                if not len(cols):
                    continue
                order = np.argsort(-mat[:, cols], axis=0)
                if len(occ) == 1:
                    pred = [room_of[nodes[k]] for k in order[0]]
                    rec["one_room"].append((sum(p == occ[0] for p in pred), len(pred)))
                    conf = rec["confusion"].setdefault(occ[0], {})
                    for p in pred:
                        conf[p] = conf.get(p, 0) + 1
                else:
                    top2 = [{room_of[nodes[order[0, c]]], room_of[nodes[order[1, c]]]}
                            for c in range(len(cols))]
                    rec["two_rooms"].append((sum(t == set(occ) for t in top2), len(top2)))

    for held_seg, ref_segs in plans:
        held_name = segments[held_seg].name
        ref_w = np.flatnonzero(np.isin(w_seg, ref_segs))
        held_w = np.flatnonzero(w_seg == held_seg)
        node_ex: dict[tuple[int, str], list[np.ndarray]] = {}
        for stream in streams:
            ref_frames = np.flatnonzero(np.isin(stream.seg, ref_segs))
            if len(ref_frames) < 50:
                continue
            clus = fit_clusters(stream, ref_frames, cfg.max_k)
            cluster_info.setdefault(stream.key, []).append(clus.k)
            lab, _far = assign(stream, clus)
            gid = names[stream.key][1] if stream.key in names else None
            used: set[str] = set()
            for c in range(clus.k):
                frames = np.flatnonzero(lab == c)
                if not len(frames):
                    continue
                # Name the fold's cluster after the all-empty cluster most of
                # its frames belong to, so a link keeps its name across folds.
                g = int(np.bincount(gid[frames]).argmax()) if gid is not None else c
                link = f"{stream.key}c{g}"
                while link in used:
                    link += "+"
                used.add(link)
                ws = summarise(stream, frames, w_start, cfg.window_s, cfg.min_frames)
                sc = _oos_scores(ws, ref_segs, w_seg, cfg.n_modes)
                if sc is None:
                    continue
                meta = {"kind": "link", "node": stream.node, "link": link}
                exs: dict[str, np.ndarray] = {}
                for score, vals in sc.items():
                    tau = _quant(vals[ref_w], cfg.q)
                    if math.isnan(tau):
                        continue
                    exs[score] = vals - tau
                    node_ex.setdefault((stream.node, score), []).append(exs[score])
                    add(f"link {link} {score}", {**meta, "score": score}, vals, ref_w, held_w, held_name)
                if exs:
                    fused = _agg(list(exs.values()))
                    node_ex.setdefault((stream.node, "fused"), []).append(fused)
                    add(f"link {link} fused", {**meta, "score": "fused"}, fused, ref_w, held_w, held_name)
                    mr = [exs[k] for k in ("motion", "rssi") if k in exs]
                    if mr:
                        node_ex.setdefault((stream.node, "fused_mr"), []).append(_agg(mr))
        by_score: dict[str, dict[int, np.ndarray]] = {}
        all_by_score: dict[str, list[np.ndarray]] = {}
        for (node, score), exs in sorted(node_ex.items()):
            all_by_score.setdefault(score, []).extend(exs)
            variants = [(score, _agg(exs))]
            if score == "fused":
                variants.append(("fused_med", _agg(exs, "median")))
            for name, vals in variants:
                by_score.setdefault(name, {})[node] = vals
                add(f"node {node} {name}", {"kind": "node", "node": node, "room": room_of.get(node),
                                            "score": name}, vals, ref_w, held_w, held_name)
        for score, exs in all_by_score.items():
            add(f"home {score}", {"kind": "home", "score": score}, _agg(exs), ref_w, held_w, held_name)
            if score == "fused":
                add("home fused_med", {"kind": "home", "score": "fused_med"}, _agg(exs, "median"),
                    ref_w, held_w, held_name)
        for score, node_vals in by_score.items():
            # Each node relative to its own empty-home threshold at this horizon.
            localize(score, node_vals)

    detectors = []
    for key, rec in acc.items():
        fa = rec["fa"]
        out = {k: v for k, v in rec.items()
               if k not in ("fa", "fa_chunk", "det", "excess", "auc_room", "auc_other")}
        out["name"] = key
        out["folds"] = len(fa)
        out["fa_heldout"] = _r(float(np.mean(fa))) if fa else None
        out["fa_heldout_max"] = _r(float(np.max(fa))) if fa else None
        out["fa_by_chunk"] = {k: _r(v) for k, v in rec["fa_chunk"].items()}
        out["detect"] = {k: _r(float(np.mean(v))) for k, v in rec["det"].items()}
        out["median_excess"] = {k: _r(float(np.nanmean(v))) for k, v in rec["excess"].items()}
        out["auc_room"] = _r(float(np.mean(rec["auc_room"]))) if rec["auc_room"] else None
        out["auc_vs_other_rooms"] = _r(float(np.mean(rec["auc_other"]))) if rec["auc_other"] else None
        detectors.append(out)
    detectors.sort(key=lambda d: d["name"])
    localization = []
    for key, rec in loc.items():
        one = rec["one_room"]
        two = rec["two_rooms"]
        localization.append({
            "name": key, "score": rec["score"], "horizon_s": rec["horizon_s"],
            "one_room_accuracy": _r(sum(a for a, _ in one) / sum(b for _, b in one)) if one else None,
            "one_room_windows": int(sum(b for _, b in one) / max(1, len(plans))) if one else 0,
            "two_room_accuracy": _r(sum(a for a, _ in two) / sum(b for _, b in two)) if two else None,
            "two_room_windows": int(sum(b for _, b in two) / max(1, len(plans))) if two else 0,
            "confusion": {t: {p: int(round(n / max(1, len(plans)))) for p, n in c.items()}
                          for t, c in rec["confusion"].items()},
        })
    localization.sort(key=lambda d: d["name"])
    return {
        "folds": len(plans),
        "guard_relaxed": relaxed,
        "windows": int(len(w_start)),
        "clusters_per_stream": {k: sorted(set(v)) for k, v in cluster_info.items()},
        "detectors": detectors,
        "localization": localization,
        "segments_scored": [sg.name for sg in segments],
        "segment_roles": {sg.name: sg.role for sg in segments},
        "heldout_chunks": [segments[i].name for i, _ in plans],
    }


def inventory(streams: list[Stream], segments: list[Segment], max_k: int,
              links: dict[str, tuple[Clusters, np.ndarray, np.ndarray]] | None = None) -> list[dict[str, Any]]:
    """Rates and transmitter clusters per stream, clustering on all empty frames."""
    dur = {i: s.end - s.start for i, s in enumerate(segments)}
    links = links if links is not None else global_links(streams, segments, max_k)
    out = []
    for stream in streams:
        rec: dict[str, Any] = {"stream": stream.key, "node": stream.node, "width": stream.width,
                               "subcarriers_kept": int(len(stream.valid)), "frames": int(len(stream.t))}
        rec["hz"] = {segments[i].name: _r(np.sum(stream.seg == i) / dur[i], 2) for i in range(len(segments))}
        rec["rssi_p10_p50_p90"] = [_r(float(q), 1) for q in np.quantile(stream.rssi, [0.1, 0.5, 0.9])]
        if stream.key in links:
            c, lab, far = links[stream.key]
            rec["clusters"] = c.k
            rec["silhouette"] = _r(c.silhouette)
            per = []
            for j in range(c.k):
                sel = lab == j
                per.append({
                    "cluster": j,
                    "share": _r(float(np.mean(sel)), 3),
                    "rssi_median": _r(float(np.median(stream.rssi[sel])), 1) if np.any(sel) else None,
                    "hz": {segments[i].name: _r(np.sum(sel & (stream.seg == i)) / dur[i], 2)
                           for i in range(len(segments))},
                })
            rec["per_cluster"] = per
            rec["far_share"] = {segments[i].name: _r(float(np.mean(far[stream.seg == i])), 3)
                                for i in range(len(segments)) if np.any(stream.seg == i)}
            # RSSI of the far frames: a new transmitter (phone, vacuum) has
            # its own level; a person close to the node does not.
            rec["far_rssi_median"] = {
                segments[i].name: _r(float(np.median(stream.rssi[far & (stream.seg == i)])), 1)
                for i in range(len(segments)) if np.sum(far & (stream.seg == i)) >= 20}
        out.append(rec)
    return out


# ------------------------------------------------------------------ report

def _table(header: list[str], rows: list[list[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join("" if v is None else str(v) for v in row) + " |" for row in rows]
    return out


def markdown(report: dict[str, Any]) -> str:
    lines = [f"# csi_lab report (v{VERSION}, MEASURED on the segments below only)", ""]
    lines += _table(["segment", "start (UTC)", "min", "occupied", "tags"], [
        [s["name"], s["start"], s["minutes"],
         "unknown" if s["occupied"] is None else
         (("at least " if s.get("at_least") else "") + ", ".join(s["occupied"]) or "empty"),
         ", ".join(s.get("tags") or [])] for s in report["segments"]])
    lines += ["", f"Frames: {report['counts']}", "", "## Streams", ""]
    lines += _table(["stream", "frames", "kept sc", "RSSI p10/p50/p90", "clusters", "cluster share / RSSI"], [
        [s["stream"], s["frames"], s["subcarriers_kept"], s["rssi_p10_p50_p90"], s.get("clusters", "-"),
         "; ".join(f"c{p['cluster']} {p['share']} @ {p['rssi_median']} dBm" for p in s.get("per_cluster", []))]
        for s in report["inventory"]])
    lines += ["", "Frames far from every empty-home transmitter (new device, or a person very "
              "close to the node), share per segment (RSSI median of those frames where >= 20):", ""]
    for s in report["inventory"]:
        if s.get("far_share"):
            fr = s.get("far_rssi_median", {})
            lines.append(f"- {s['stream']}: " + ", ".join(
                f"{k} {v}" + (f" @ {fr[k]} dBm" if k in fr else "") for k, v in s["far_share"].items()))
    clus = {(s["stream"], p["cluster"]): p for s in report["inventory"] for p in s.get("per_cluster", [])}
    for title, ev in (("Evaluation (transmitters separated)", report["evaluation"]),
                      ("Evaluation (no separation, for comparison)", report.get("no_separation"))):
        if ev:
            lines += _evaluation_md(title, ev, clus if title.endswith("separated)") else {})
    return "\n".join(lines) + "\n"


def _evaluation_md(title: str, ev: dict[str, Any], clus: dict[tuple[str, int], dict]) -> list[str]:
    lines = ["", f"## {title}", "",
             f"Folds: {ev['folds']} held-out empty chunks; guard relaxed: {ev['guard_relaxed']}; "
             f"clusters per stream: {ev['clusters_per_stream']}", ""]
    roles = ev.get("segment_roles", {})
    clean = [s for s in ev["segments_scored"] if roles.get(s) in ("occupied", "unknown")]
    scored = [s for s in ev["segments_scored"] if roles.get(s) in ("occupied", "unknown", "confounded")]
    chunks = ev.get("heldout_chunks", [])
    dets = ev["detectors"]
    top = max(d["horizon_s"] for d in dets) if dets else 0
    agg = [d for d in dets if d["kind"] != "link"]
    # Node/home rows at the longest horizon, plus `fused` at every horizon;
    # the JSON report has everything.
    main = [d for d in agg if d["horizon_s"] == top or d["score"] == "fused"]
    lines += _table(["detector", "FA held-out (max)"] + [f"det {s}" for s in clean]
                    + ["AUC room", "AUC vs other rooms"],
                    [[d["name"], f"{d['fa_heldout']} ({d['fa_heldout_max']})"]
                     + [d["detect"].get(s, "") for s in clean] + [d["auc_room"], d["auc_vs_other_rooms"]]
                     for d in main])
    at_top = [d for d in agg if d["horizon_s"] == top]
    if chunks:
        lines += ["", f"False alarms per held-out empty chunk (@{int(top)}s; a drift or a new "
                  "transmitter shows up as one bad chunk, usually at an end):", ""]
        lines += _table(["detector"] + chunks,
                        [[d["name"]] + [d.get("fa_by_chunk", {}).get(c) for c in chunks] for d in at_top])
    if scored:
        lines += ["", f"Median excess over the empty-home threshold (@{int(top)}s, log10 units; > 0 "
                  "= above the alarm line; tagged segments have a known confounder):", ""]
        lines += _table(["detector", "(held-out empty)"] + scored,
                        [[d["name"], d["median_excess"].get("(held-out empty)")]
                         + [d["median_excess"].get(s) for s in scored] for d in at_top])
    links = [d for d in dets if d["kind"] == "link" and d["horizon_s"] == top]
    if links:
        by = {}
        for d in links:
            by.setdefault(d["link"], {})[d["score"]] = d
        lines += ["", f"Links (@{int(top)}s): held-out FA per score, then the fused score's median "
                  "excess per segment. A link with high FA and excess everywhere is drifting; one "
                  "that only rises in tagged segments carries that confounder.", ""]
        rows = []
        for link in sorted(by):
            m = re.match(r"^(n\d+w\d+)c(\d+)", link)
            p = clus.get((m.group(1), int(m.group(2)))) if m else None
            f = by[link].get("fused", {})
            rows.append([link, p["rssi_median"] if p else None, p["share"] if p else None,
                         f.get("folds")]
                        + [by[link].get(sc, {}).get("fa_heldout") for sc in ("fused",) + SCORES]
                        + [f.get("median_excess", {}).get(s) for s in scored])
        lines += _table(["link", "RSSI", "share", "folds", "FA fused", "FA static", "FA motion",
                         "FA phase", "FA rssi"] + [f"ex {s}" for s in scored], rows)
    if ev.get("localization"):
        lines += ["", "Localization: the node with the largest excess over its own empty-home "
                  "threshold names the room (two-room segments: the top two).", ""]
        lines += _table(["score", "one room acc (windows)", "two rooms acc (windows)",
                         "windows true -> predicted"],
                        [[d["name"], f"{d['one_room_accuracy']} ({d['one_room_windows']})",
                          f"{d['two_room_accuracy']} ({d['two_room_windows']})",
                          "; ".join(f"{t}: " + ", ".join(f"{p} {n}" for p, n in sorted(c.items()))
                                    for t, c in sorted(d["confusion"].items()))]
                         for d in ev["localization"] if d["horizon_s"] == top or d["score"] == "fused"])
    return lines


def build_report(root: Path, labels: Labels, cfg: EvalConfig, compare: bool) -> dict[str, Any]:
    segments = chunk_empty(labels.segments, cfg.chunk_s)
    total_h = sum(s.end - s.start for s in labels.segments) / 3600
    if total_h > 4.0:
        raise LabError(f"segments cover {total_h:.1f} h; run at most 4 h at a time (memory)")
    streams, counts = load_streams(root, segments, labels.exclude)
    if not streams:
        raise LabError("no CSI frames in the labelled segments")
    links = global_links(streams, segments, cfg.max_k)
    report: dict[str, Any] = {
        "tool": "csi_lab", "version": VERSION, "evidence": "MEASURED",
        "params": {k: getattr(cfg, k) for k in cfg.__dataclass_fields__},
        "rooms": {str(k): v for k, v in labels.rooms.items()},
        "segments": [{"name": s.name, "start": iso(s.start), "end": iso(s.end),
                      "minutes": round((s.end - s.start) / 60, 1), "occupied": s.occupied,
                      "at_least": s.at_least, "tags": list(s.tags)}
                     for s in labels.segments],
        "exclude": [{"start": iso(a), "end": iso(b), "why": w} for a, b, w in labels.exclude],
        "counts": counts,
        "inventory": inventory(streams, segments, cfg.max_k, links),
        "evaluation": evaluate(streams, labels, segments, cfg, links),
    }
    if compare and cfg.max_k > 1:
        flat = EvalConfig(**{**report["params"], "max_k": 1})
        report["no_separation"] = evaluate(streams, labels, segments, flat)
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="csi_lab", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    ev = sub.add_parser("evaluate", help="score labelled segments; aggregates only")
    ev.add_argument("--data", default="/data", help="csi-recorder data dir (ring/ and keep/)")
    g = ev.add_mutually_exclusive_group(required=True)
    g.add_argument("--labels", help="labels JSON file")
    g.add_argument("--labels-json", help="labels JSON text")
    ev.add_argument("--window", type=float, default=10.0)
    ev.add_argument("--hop", type=float, default=5.0)
    ev.add_argument("--min-frames", type=int, default=4)
    ev.add_argument("--max-k", type=int, default=4, help="transmitters per stream (1 = no separation)")
    ev.add_argument("--chunk-min", type=float, default=10.0, help="empty-home fold length")
    ev.add_argument("--horizons", default="10,30,60")
    ev.add_argument("--no-guard", action="store_true", help="allow folds next to the held-out chunk")
    ev.add_argument("--compare", action="store_true", help="also run without transmitter separation")
    ev.add_argument("--json", action="store_true", help="print the JSON report instead of markdown")
    args = ap.parse_args(argv)
    root = Path(args.data)
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    try:
        text = args.labels_json if args.labels_json is not None else Path(args.labels).read_text()
        labels = parse_labels(json.loads(text))
        if not 2 <= args.window <= 120 or not 0.5 <= args.hop <= args.window:
            raise LabError("window must be 2-120 s and hop 0.5 s to the window")
        if not 1 <= args.max_k <= 6:
            raise LabError("max-k must be 1-6")
        cfg = EvalConfig(window_s=args.window, hop_s=args.hop, min_frames=args.min_frames,
                         max_k=args.max_k,
                         horizons=tuple(float(h) for h in args.horizons.split(",") if h.strip()),
                         guard=not args.no_guard, chunk_s=max(60.0, args.chunk_min * 60.0))
        report = build_report(root, labels, cfg, args.compare)
    except (LabError, json.JSONDecodeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=1))
    else:
        print(markdown(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
