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
