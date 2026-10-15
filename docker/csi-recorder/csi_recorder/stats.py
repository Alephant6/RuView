"""Datagram counters for the status topic."""

from __future__ import annotations

from collections import Counter
from typing import Any

from .packets import classify
from .rawfile import NS


class Stats:
    """Totals plus per-window rates (overall, per kind, per node). Each
    `snapshot` closes the window; rates need at least one second of it."""

    def __init__(self, now_ns: int) -> None:
        self.total = 0
        self.total_bytes = 0
        self.last_ns: int | None = None
        self.node_last_ns: dict[str, int] = {}
        self._reset(now_ns)

    def _reset(self, now_ns: int) -> None:
        self._since_ns = now_ns
        self._count = 0
        self._bytes = 0
        self._kinds: Counter[str] = Counter()
        self._nodes: Counter[str] = Counter()
        self._node_csi: Counter[str] = Counter()

    def add(self, now_ns: int, data: bytes) -> None:
        kind, node = classify(data)
        self.total += 1
        self.total_bytes += len(data)
        self.last_ns = now_ns
        self._count += 1
        self._bytes += len(data)
        self._kinds[kind] += 1
        if node is not None:
            key = str(node)
            self._nodes[key] += 1
            if kind == "csi":
                self._node_csi[key] += 1
            self.node_last_ns[key] = now_ns

    def snapshot(self, now_ns: int) -> dict[str, Any]:
        elapsed = (now_ns - self._since_ns) / NS
        out: dict[str, Any] = {
            "datagrams_total": self.total,
            "last_datagram_age_s": None if self.last_ns is None
            else round((now_ns - self.last_ns) / NS, 1),
        }
        if elapsed < 1.0:
            return out
        out["datagrams_hz"] = round(self._count / elapsed, 2)
        out["bytes_hz"] = round(self._bytes / elapsed, 1)
        out["kinds_hz"] = {k: round(v / elapsed, 2) for k, v in sorted(self._kinds.items())}
        out["nodes"] = {
            key: {"hz": round(self._nodes[key] / elapsed, 2),
                  "csi_hz": round(self._node_csi[key] / elapsed, 2),
                  "age_s": round((now_ns - last) / NS, 1)}
            for key, last in sorted(self.node_last_ns.items(), key=lambda kv: int(kv[0]))
        }
        self._reset(now_ns)
        return out
