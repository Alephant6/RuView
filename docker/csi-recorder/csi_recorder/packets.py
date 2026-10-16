"""Just enough datagram decoding for statistics and export.

Every RuView packet starts with a little-endian u32 magic in the 0xC511xxxx
family and carries the node id in byte 4 (ADR-018 CSI frames, ADR-039 edge
vitals, ADR-081 feature state, ADR-110 sync packets, ...). The recorder
stores datagrams undecoded; this module only labels them.
"""

from __future__ import annotations

import struct
from typing import Any

ADR018_MAGIC = 0xC5110001
SYNC_MAGIC = 0xC511A110

KINDS = {
    0xC5110001: "csi",
    0xC5110002: "vitals",
    0xC5110003: "features",
    0xC5110004: "fused_vitals",
    0xC5110005: "csi_compressed",
    0xC5110006: "feature_state",
    0xC5110007: "temporal",
    SYNC_MAGIC: "sync",
}

# ADR-018 header, as in scripts/record-csi-udp.py and esp32_parser.rs:
# magic u32, node_id u8, n_antennas u8, n_subcarriers u16, freq_mhz u32,
# sequence u32, rssi i8, noise_floor i8, ppdu_type u8, flags u8, then
# n_antennas * n_subcarriers int8 (I, Q) pairs.
_ADR018 = struct.Struct("<IBBHIIbbBB")


def classify(data: bytes) -> tuple[str, int | None]:
    """(kind, node id) of one datagram."""
    if len(data) >= 5:
        magic = int.from_bytes(data[:4], "little")
        if magic >> 16 == 0xC511:
            return KINDS.get(magic, "ruview_other"), data[4]
    if data[:1] == b"{":
        return "json", None
    return "other", None


def freq_to_channel(freq_mhz: int) -> int:
    """802.11 channel for a 2.4/5 GHz centre frequency (0 if unknown)."""
    if freq_mhz == 2484:
        return 14
    if 2412 <= freq_mhz <= 2472:
        return (freq_mhz - 2407) // 5
    if 5000 <= freq_mhz <= 5900:
        return (freq_mhz - 5000) // 5
    return 0


def parse_adr018(data: bytes) -> dict[str, Any] | None:
    """Decode an ADR-018 CSI frame into the fields `scripts/record-csi-udp.py`
    writes (minus the timestamps), or None if it is not one."""
    if len(data) < _ADR018.size:
        return None
    (magic, node_id, n_antennas, n_sub, freq_mhz, seq,
     rssi, noise_floor, ppdu_type, flags) = _ADR018.unpack_from(data)
    if magic != ADR018_MAGIC or not 1 <= n_antennas <= 4 or n_sub > 256:
        return None
    n_pairs = n_antennas * n_sub
    iq = data[_ADR018.size:_ADR018.size + 2 * n_pairs]
    if len(iq) < 2 * n_pairs:
        return None
    signed = struct.unpack(f"<{2 * n_pairs}b", iq)
    amplitudes = [round((i * i + q * q) ** 0.5, 2) for i, q in zip(signed[0::2], signed[1::2])]
    return {
        "node_id": node_id,
        "seq": seq,
        "rssi": rssi,
        "noise_floor": noise_floor,
        "freq_mhz": freq_mhz,
        "channel": freq_to_channel(freq_mhz),
        "ppdu_type": ppdu_type,
        "flags": flags,
        "n_antennas": n_antennas,
        "n_subcarriers": n_sub,
        "subcarriers": n_pairs,
        "amplitudes": amplitudes,
        "iq_hex": iq.hex(),
    }
