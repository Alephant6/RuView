"""csi_lab on SYNTHETIC recordings (tests/synth.py); no real CSI involved.

Run from docker/csi-lab:  python -m pytest -q
"""

from __future__ import annotations

import gzip
import json
import os
import struct
import sys
from pathlib import Path

import numpy as np
import pytest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)

import csi_lab as L  # noqa: E402
import synth  # noqa: E402

ROOMS = {"1": "bedroom", "2": "study", "3": "living"}


def labels_for(segments: list[tuple[str, float, float, list[str] | None]]) -> dict:
    return {"rooms": ROOMS, "segments": [
        {"name": n, "start": synth.iso(a), "end": synth.iso(b), "occupied": occ}
        for n, a, b, occ in segments]}


@pytest.fixture(scope="module")
def recording(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("csi")
    acts = [synth.Activity(16 * 60, 22 * 60, "study", "moving"),
            synth.Activity(22 * 60, 28 * 60, "living", "still")]
    synth.generate(synth.ring_path(root), 28 * 60, acts, body=0.2)
    return root


LABELS = labels_for([("empty", 0, 16 * 60, []), ("study_moving", 16 * 60, 22 * 60, ["study"]),
                     ("living_still", 22 * 60, 28 * 60, ["living"])])


def run_json(root: Path, labels: dict, *extra: str, capsys=None) -> dict:
    rc = L.main(["evaluate", "--data", str(root), "--labels-json", json.dumps(labels),
                 "--chunk-min", "4", "--json", *extra])
    assert rc == 0
    return json.loads(capsys.readouterr().out)


def detector(ev: dict, name: str) -> dict:
    return next(d for d in ev["detectors"] if d["name"] == name)


def test_parse_frame_and_raw_reader(tmp_path: Path) -> None:
    header = struct.pack("<IBBHIIbbBB", 0xC5110001, 2, 1, 64, 2437, 9, -60, -91, 0, 0)
    frame = header + bytes(range(128))
    path = tmp_path / "ring" / "20261014T180000Z.csi.raw.gz"
    path.parent.mkdir(parents=True)
    meta = b"{}"
    with gzip.open(path, "wb") as fp:
        fp.write(struct.pack("<8sHI", b"RVCSIRAW", 1, len(meta)) + meta)
        fp.write(struct.pack("<QI", 5, len(frame)) + frame)
        fp.write(struct.pack("<QI", 6, 100))       # cut short: ends quietly
    records = list(L.iter_raw(path))
    assert records == [(5, frame)]
    node, n_sub, rssi, nf, flags, iq = L.parse_frame(frame)
    assert (node, n_sub, rssi, nf, flags, len(iq)) == (2, 64, -60, -91, 0, 128)
    assert L.parse_frame(frame[:30]) is None
    two_ant = struct.pack("<IBBHIIbbBB", 0xC5110001, 2, 2, 64, 2437, 9, -60, -91, 0, 0) + bytes(256)
    assert L.parse_frame(two_ant) is None


def test_ring_and_keep_copies_count_once(recording: Path, tmp_path: Path) -> None:
    import shutil
    src = next((recording / "ring").rglob("*.csi.raw.gz"))
    (tmp_path / "ring").mkdir()
    (tmp_path / "keep").mkdir()
    shutil.copy(src, tmp_path / "ring" / src.name)
    shutil.copy(src, tmp_path / "keep" / src.name.replace("Z.csi", "Z_empty_home.csi"))
    lab = L.parse_labels(labels_for([("empty", 0, 8 * 60, [])]))
    segs = L.chunk_empty(lab.segments, 240)
    once, c1 = L.load_streams(recording, segs, [])
    twice, c2 = L.load_streams(tmp_path, segs, [])
    assert sum(len(s.t) for s in twice) == sum(len(s.t) for s in once)
    assert c2["duplicates"] == c1["csi_frames"] and c1["duplicates"] == 0


def test_labels_are_validated() -> None:
    with pytest.raises(L.LabError):
        L.parse_labels({"rooms": ROOMS, "segments": [
            {"name": "a", "start": synth.iso(0), "end": synth.iso(60), "occupied": ["garage"]}]})
    with pytest.raises(L.LabError):
        L.parse_labels({"rooms": ROOMS, "segments": [
            {"name": "a", "start": synth.iso(0), "end": synth.iso(60), "occupied": []},
            {"name": "b", "start": synth.iso(30), "end": synth.iso(90), "occupied": []}]})
    with pytest.raises(L.LabError):
        L.parse_labels({"rooms": ROOMS, "segments": [
            {"name": "a", "start": "2026-10-14T18:00:00", "end": synth.iso(60), "occupied": []}]})
    with pytest.raises(L.LabError):
        L.parse_labels({"rooms": ROOMS, "segments": [
            {"name": "a", "start": synth.iso(0), "end": synth.iso(60), "occupied": [], "tags": ["no spaces"]}]})
    with pytest.raises(L.LabError):                     # a node room is not "other"
        L.parse_labels({"rooms": ROOMS, "other_rooms": ["study"], "segments": [
            {"name": "a", "start": synth.iso(0), "end": synth.iso(60), "occupied": []}]})
    dining = L.parse_labels({"rooms": ROOMS, "other_rooms": ["dining"], "segments": [
        {"name": "a", "start": synth.iso(0), "end": synth.iso(60), "occupied": ["dining"]}]})
    assert dining.segments[0].role == "occupied"
    tagged = L.parse_labels({"rooms": ROOMS, "segments": [
        {"name": "a", "start": synth.iso(0), "end": synth.iso(60), "occupied": [], "tags": ["vacuum:kitchen"]}]})
    assert tagged.segments[0].role == "confounded"      # never an empty-home reference
    lab = L.parse_labels(LABELS)
    assert [s.role for s in lab.segments] == ["empty", "occupied", "occupied"]
    chunks = L.chunk_empty(lab.segments, 240)
    assert [s.name for s in chunks][:4] == ["empty#0", "empty#1", "empty#2", "empty#3"]


def test_clusters_recover_transmitters(recording: Path) -> None:
    lab = L.parse_labels(LABELS)
    segs = L.chunk_empty(lab.segments, 240)
    streams, counts = L.load_streams(recording, segs, [])
    assert counts["csi_frames"] > 30000
    wide = {s.node: s for s in streams if s.width == 128}
    for node, stream in wide.items():
        ref = np.flatnonzero(stream.seg <= 3)
        clus = L.fit_clusters(stream, ref, 4)
        assert clus.k == 3, node                       # AP + the two other nodes
        lab_id, _far = L.assign(stream, clus)
        # purity against the transmitter encoded in the noise-floor byte,
        # including the occupied segments (no frame may be dropped)
        purity = 0
        for c in range(clus.k):
            truth = stream.nf[lab_id == c]
            purity += np.bincount(-truth - 90).max()
        assert purity / len(stream.t) > 0.97, node


def test_heldout_false_alarms_and_detection(recording: Path, capsys) -> None:
    rep = run_json(recording, LABELS, capsys=capsys)
    ev = rep["evaluation"]
    assert ev["folds"] == 4
    for node in (1, 2, 3):
        d = detector(ev, f"node {node} fused@60s")
        assert d["fa_heldout"] <= 0.1, d
    study = detector(ev, "node 2 fused@60s")
    living = detector(ev, "node 3 fused@60s")
    assert study["detect"]["study_moving"] >= 0.9
    assert living["detect"]["living_still"] >= 0.9
    # the room's own node separates its room from the other room's occupant
    assert study["auc_vs_other_rooms"] >= 0.8
    assert living["auc_vs_other_rooms"] >= 0.8
    loc = next(d for d in ev["localization"] if d["name"] == "fused@60s")
    assert loc["one_room_accuracy"] >= 0.9


def test_links_keep_their_names_across_folds(recording: Path, capsys) -> None:
    rep = run_json(recording, LABELS, capsys=capsys)
    links = {d["link"]: d["folds"] for d in rep["evaluation"]["detectors"]
             if d["kind"] == "link" and d["score"] == "fused" and d["horizon_s"] == 60}
    inv = {s["stream"]: s["clusters"] for s in rep["inventory"]}
    expected = {f"{k}c{j}" for k, n in inv.items() for j in range(n)}
    assert set(links) == expected
    assert set(links.values()) == {4}            # the same transmitter in every fold


@pytest.fixture(scope="module")
def stepped(tmp_path_factory) -> Path:
    """One link (node 1 heard at node 2) changes its static channel in the
    last empty chunk; the study is occupied afterwards, its second half
    tagged as confounded."""
    root = tmp_path_factory.mktemp("csi_step")
    acts = [synth.Activity(16 * 60, 22 * 60, "study", "moving")]
    steps = [synth.Step(12 * 60, 16 * 60, 2, "node1", 0.3)]
    synth.generate(synth.ring_path(root), 22 * 60, acts, body=0.2, steps=steps)
    return root


STEP_LABELS = {"rooms": ROOMS, "segments": [
    {"name": "empty", "start": synth.iso(0), "end": synth.iso(16 * 60), "occupied": []},
    {"name": "study_moving", "start": synth.iso(16 * 60), "end": synth.iso(19 * 60), "occupied": ["study"]},
    {"name": "study_vac", "start": synth.iso(19 * 60), "end": synth.iso(22 * 60), "occupied": ["study"],
     "tags": ["vacuum:study"]}]}


def test_one_drifting_link_is_found_and_outvoted(stepped: Path, capsys) -> None:
    rep = run_json(stepped, STEP_LABELS, capsys=capsys)
    ev = rep["evaluation"]
    node2 = detector(ev, "node 2 fused@60s")
    # the bad chunk is named, and it is the one with the step
    assert max(node2["fa_by_chunk"], key=node2["fa_by_chunk"].get) == "empty#3"
    assert node2["fa_by_chunk"]["empty#3"] >= 0.5
    # the link table names the link: node 1 heard at node 2
    lab = L.parse_labels(STEP_LABELS)
    segs = L.chunk_empty(lab.segments, 240)
    streams, _ = L.load_streams(stepped, segs, [])
    stream = next(s for s in streams if s.key == "n2w128")
    _c, gl, _far = L.global_links(streams, segs, 4)["n2w128"]
    # (A fold whose reference holds the step may split that transmitter in
    # two; the extra cluster is named "...+" and exists in that fold only.)
    static = {d["link"]: d["fa_by_chunk"].get("empty#3", 0.0) for d in ev["detectors"]
              if d["kind"] == "link" and d["score"] == "static" and d["horizon_s"] == 60
              and d["link"].startswith("n2w128") and not d["link"].endswith("+")}
    worst = max(static, key=static.get)
    g = int(worst.split("c")[-1])
    assert np.bincount(-stream.nf[gl == g] - 90).argmax() == 1      # transmitter node1
    assert static[worst] >= 0.5
    assert all(v <= 0.15 for k, v in static.items() if k != worst), static
    # the median over links is not carried by one link; detection survives
    for who in ("home", "node 2"):
        mean, med = detector(ev, f"{who} fused@60s"), detector(ev, f"{who} fused_med@60s")
        assert med["fa_heldout"] < mean["fa_heldout"], (who, med, mean)
        assert med["detect"]["study_moving"] >= 0.9
    assert detector(ev, "home fused_med@60s")["fa_heldout"] <= 0.1
    # the tagged segment is scored but kept out of AUC and localization
    assert ev["segment_roles"]["study_vac"] == "confounded"
    assert "study_vac" in node2["detect"] and "study_vac" in node2["median_excess"]
    loc = next(d for d in ev["localization"] if d["name"] == "fused@10s")
    assert loc["one_room_windows"] == 35          # study_moving's windows only


@pytest.fixture(scope="module")
def drifting(tmp_path_factory) -> Path:
    """40 min with a house-wide shape drift that keeps growing: empty for
    24 min, someone moving then still in the study, empty again at the end
    (by then the drift is past anything the reference saw)."""
    root = tmp_path_factory.mktemp("drift")
    acts = [synth.Activity(24 * 60, 30 * 60, "study", "moving"),
            synth.Activity(30 * 60, 36 * 60, "study", "still")]
    synth.generate(synth.ring_path(root), 40 * 60, acts, body=0.2, shape_drift=0.3, seed=3)
    return root


DRIFT_LABELS = labels_for([("empty", 0, 24 * 60, []), ("study_moving", 24 * 60, 30 * 60, ["study"]),
                           ("study_still", 30 * 60, 36 * 60, ["study"]), ("late", 36 * 60, 40 * 60, [])])


def test_drift_subspace_and_breathing(drifting: Path, capsys) -> None:
    rep = run_json(drifting, DRIFT_LABELS, "--breath", capsys=capsys)
    ev = rep["evaluation"]
    # the fixture works: shape scores alarm all through the late empty chunk
    assert detector(ev, "home static@60s")["fa_by_chunk"]["late#0"] >= 0.9
    for n in (1, 2):
        assert detector(ev, f"node {n} fused@60s")["fa_by_chunk"]["late#0"] >= 0.9
        # with the house-wide drift directions removed, these nodes stay quiet
        assert detector(ev, f"node {n} fused_dr@60s")["fa_by_chunk"]["late#0"] <= 0.1
    # and the person still shows, where the plain fused score drowns in the
    # thresholds the drift pushed up
    dr = detector(ev, "home fused_dr@60s")
    assert dr["detect"]["study_moving"] >= 0.9 and dr["detect"]["study_still"] >= 0.9
    assert detector(ev, "home fused@60s")["detect"]["study_moving"] < 0.5
    loc = next(d for d in ev["localization"] if d["name"] == "fused_dr@60s")
    assert loc["one_room_accuracy"] >= 0.9
    # breathing band: blind to the drift, sees the still person, not the moving one
    br = detector(ev, "home breath@60s")
    assert br["fa_by_chunk"]["late#0"] <= 0.1
    assert br["detect"]["study_still"] >= 0.9
    assert br["detect"]["study_moving"] <= 0.3
    text = L.markdown(rep)
    assert "FA breath" in text and "home fused_dr@60s" in text


def test_at_least_labels_are_positives_only(recording: Path, capsys) -> None:
    """An event label ("someone pressed the living-room panel") says the room
    was occupied and nothing about the others."""
    with pytest.raises(L.LabError):
        L.parse_labels({"rooms": ROOMS, "segments": [
            {"name": "a", "start": synth.iso(0), "end": synth.iso(60), "occupied": [], "at_least": True}]})
    lab = json.loads(json.dumps(LABELS))
    lab["segments"][2]["at_least"] = True              # living_still
    ev = run_json(recording, lab, capsys=capsys)["evaluation"]
    # study_moving has no full-knowledge negative left, living_still is still a positive
    assert detector(ev, "node 2 fused@60s")["auc_vs_other_rooms"] is None
    assert detector(ev, "node 3 fused@60s")["auc_room"] >= 0.8
    loc = next(d for d in ev["localization"] if d["name"] == "fused@60s")
    assert set(loc["confusion"]) == {"study"}            # living_still not localized
    # Everyone in a room without a node: a negative for every node's room.
    lab = json.loads(json.dumps(LABELS))
    lab["other_rooms"] = ["dining"]
    lab["segments"][2]["occupied"] = ["dining"]
    ev = run_json(recording, lab, capsys=capsys)["evaluation"]
    assert detector(ev, "node 2 fused@60s")["auc_vs_other_rooms"] is not None
    loc = next(d for d in ev["localization"] if d["name"] == "fused@60s")
    assert set(loc["confusion"]) == {"study"}


def test_separation_beats_mixed_links(recording: Path, capsys) -> None:
    rep = run_json(recording, LABELS, "--compare", capsys=capsys)
    sep, mixed = rep["evaluation"], rep["no_separation"]
    assert set(mixed["clusters_per_stream"]["n2w128"]) == {1}
    # With the three transmitters mixed, the living-room node misses a moving
    # person in the study that its separated links still see.
    a = detector(sep, "node 3 fused@60s")["detect"]["study_moving"]
    b = detector(mixed, "node 3 fused@60s")["detect"]["study_moving"]
    assert a > b + 0.2, (a, b)


def test_report_holds_aggregates_only(recording: Path, capsys) -> None:
    rep = run_json(recording, LABELS, capsys=capsys)

    def walk(x, depth=0):
        if isinstance(x, list):
            numbers = [v for v in x if isinstance(v, (int, float)) and not isinstance(v, bool)]
            assert len(numbers) <= 8, "no per-frame or per-window series in the report"
            for v in x:
                walk(v, depth + 1)
        elif isinstance(x, dict):
            for v in x.values():
                walk(v, depth + 1)
        else:
            assert x is None or isinstance(x, (int, float, str, bool))
    walk(rep)
    assert rep["evidence"] == "MEASURED"


def test_markdown_and_errors(recording: Path, capsys, tmp_path: Path) -> None:
    rc = L.main(["evaluate", "--data", str(recording), "--labels-json", json.dumps(LABELS),
                 "--chunk-min", "4"])
    out = capsys.readouterr().out
    assert rc == 0 and "## Streams" in out and "Localization" in out
    assert "False alarms per held-out empty chunk" in out and "Links (@60s)" in out
    assert "Median excess over the empty-home threshold" in out
    one_empty = labels_for([("empty", 0, 3 * 60, [])])
    rc = L.main(["evaluate", "--data", str(tmp_path), "--labels-json", json.dumps(one_empty)])
    assert rc == 2
    assert L.main(["evaluate", "--data", str(tmp_path / "missing"), "--labels-json", "{}"]) == 2


@pytest.fixture(scope="module")
def rooms4(tmp_path_factory) -> Path:
    """50 min: the study node is three times as sensitive to people in every
    room (like the real study node), a dining room has no node of its own,
    and the neighbour sends in bursts five seconds apart with two frame
    types alternating (a breathing-band periodicity from the traffic)."""
    root = tmp_path_factory.mktemp("rooms4")
    M = 60
    acts = [synth.Activity(20 * M, 23 * M, "living", "moving"), synth.Activity(23 * M, 26 * M, "study", "still"),
            synth.Activity(26 * M, 29 * M, "dining", "moving"), synth.Activity(29 * M, 32 * M, "bedroom", "still"),
            synth.Activity(32 * M, 35 * M, "living", "still"), synth.Activity(35 * M, 38 * M, "study", "moving"),
            synth.Activity(38 * M, 41 * M, "dining", "still"), synth.Activity(41 * M, 44 * M, "bedroom", "moving"),
            synth.Activity(44 * M, 47 * M, "study", "moving"), synth.Activity(44 * M, 47 * M, "living", "still"),
            synth.Activity(47 * M, 50 * M, "bedroom", "still"), synth.Activity(47 * M, 50 * M, "living", "moving")]
    synth.generate(synth.ring_path(root), 50 * M, acts, body=0.2, seed=5, rx_gain={2: 8.0},
                   room_coupling={"dining": {3: 0.6}}, burst=("neighbour", 0.2))
    return root


ROOMS4 = labels_for([("empty", 0, 20 * 60, []),
                     ("living_m", 20 * 60, 23 * 60, ["living"]), ("study_s", 23 * 60, 26 * 60, ["study"]),
                     ("dining_m", 26 * 60, 29 * 60, ["dining"]), ("bedroom_s", 29 * 60, 32 * 60, ["bedroom"]),
                     ("living_s", 32 * 60, 35 * 60, ["living"]), ("study_m", 35 * 60, 38 * 60, ["study"]),
                     ("dining_s", 38 * 60, 41 * 60, ["dining"]), ("bedroom_m", 41 * 60, 44 * 60, ["bedroom"]),
                     ("study_living", 44 * 60, 47 * 60, ["living", "study"]),
                     ("bedroom_living", 47 * 60, 50 * 60, ["bedroom", "living"])])
ROOMS4["other_rooms"] = ["dining"]


def test_room_patterns_and_breath_timing(rooms4: Path, capsys) -> None:
    rep = run_json(rooms4, ROOMS4, "--breath", capsys=capsys)
    ev = rep["evaluation"]
    loc = {d["name"]: d for d in ev["localization"]}
    # the fixture works: "the node with the largest excess" picks the
    # oversensitive study node for other rooms
    assert loc["fused_dr@60s"]["one_room_accuracy"] < 0.6
    assert loc["fused@60s"]["confusion"]["living"].get("study", 0) >= 40
    # the cone-model match is not fooled by one node's sensitivity ...
    for name in ("pattern@60s", "pattern_gls@60s"):
        conf = loc[name]["confusion"]
        for room in ("study", "bedroom"):
            assert conf[room].get(room, 0) >= 0.9 * sum(conf[room].values())
        # ... and finds the room without a node, but here dining only
        # couples to the living-room node, so its pattern points the same
        # way as living's: told apart from the other rooms, not from living
        both = sum(conf[r].get(p, 0) for r in ("living", "dining") for p in ("living", "dining"))
        assert both >= 0.9 * sum(sum(conf[r].values()) for r in ("living", "dining"))
        pat = ev["room_patterns"][name.split("@")[0]]
        cos = {(a, b): pat["cos"][i][j] for i, a in enumerate(pat["rooms"]) for j, b in enumerate(pat["rooms"])}
        assert cos[("dining", "living")] >= 0.9
        assert max(v for (a, b), v in cos.items() if a < b and {a, b} != {"dining", "living"}) < 0.9
    # breathing rate of the still people (synthetic wobble at 0.25 Hz = 15/min)
    assert 13 <= ev["breath_rate_bpm"]["1"]["bedroom_s"]["median"] <= 17
    # the neighbour's bursts every 5 s are flagged on node 1, and the
    # flagged frequencies do not alarm in the empty home
    assert ev["breath_timing_flag"]["n1w64c0"] >= 0.5
    assert detector(ev, "link n1w64c0 breath@60s")["fa_heldout"] <= 0.1
    text = L.markdown(rep)
    assert "Room patterns (pattern, @60s)" in text and "Breathing rate" in text and "Frame timing" in text


def _breath_stream(t: np.ndarray, nla: np.ndarray) -> L.Stream:
    n = len(t)
    return L.Stream(node=1, width=64, t=t, seg=np.zeros(n, np.int16), rssi=np.full(n, -60.0, np.float32),
                    nla=nla.astype(np.float32), ph=np.zeros(nla.shape, np.float16),
                    valid=np.arange(nla.shape[1]), nf=np.zeros(n, np.int8))


def test_breath_ignores_periodic_frame_timing(monkeypatch) -> None:
    """Two frame types alternating in bursts every 5 s make the amplitude
    periodic at 0.2 Hz with nobody there; the spectral window of the frame
    times shows it, so that frequency is not used. A real 0.25 Hz wobble on
    irregular frame times is kept."""
    rng = np.random.default_rng(11)
    dur, k = 600.0, 8
    t = np.sort(rng.uniform(0, dur, size=int(12 * dur)))
    t = t[rng.uniform(size=len(t)) < (1 + 0.9 * np.cos(2 * np.pi * 0.2 * t)) / 2]
    burst = np.cos(2 * np.pi * 0.2 * t) > 0
    shape = rng.normal(size=k)
    bursty = _breath_stream(t, 0.3 * burst[:, None] * shape + rng.normal(0, 0.1, size=(len(t), k)))
    t2 = np.sort(rng.uniform(0, dur, size=int(6 * dur)))
    breathing = _breath_stream(t2, 0.05 * np.sin(2 * np.pi * 0.25 * t2)[:, None] * shape
                               + rng.normal(0, 0.1, size=(len(t2), k)))
    seg = [L.Segment("x", 0.0, dur, [])]
    w_start = np.arange(60.0, dur - 10.0, 10.0)
    w_seg = np.zeros(len(w_start), np.int32)

    def run(stream):
        return L.breath_scores(stream, np.arange(len(stream.t)), w_start, w_seg, seg, 10.0, 60.0, [])

    score, peak, flag = run(bursty)
    assert np.nanmean(flag) >= 0.9 and np.nanmedian(score) < 1.0
    monkeypatch.setattr(L, "WINDOW_FLAG", 1e12)          # without the check
    raw, raw_peak, _ = run(bursty)
    assert np.nanmedian(raw) > 2.0 and abs(np.nanmedian(raw_peak) - 0.2) < 0.02
    monkeypatch.undo()
    score, peak, flag = run(breathing)
    assert np.nanmean(flag) <= 0.05 and np.nanmedian(score) > 1.5
    assert abs(np.nanmedian(peak) - 0.25) < 0.02
