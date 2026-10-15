"""Offline commands: info, export, replay.

    python -m csi_recorder info   [--data-dir /data] [--json]
    python -m csi_recorder export PATH... [--start T] [--end T] [--all] [-o out.csi.jsonl]
    python -m csi_recorder replay PATH... --target 127.0.0.1:5005 [--speed 1]

PATH is a .csi.raw.gz file or a directory searched recursively (ring or
keep). Times are epoch seconds or ISO 8601 with a timezone.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import socket
import sys
import time
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from .config import Config, ConfigError
from .keep import KeepError, KeepStore, iso, to_ns
from .packets import classify, parse_adr018
from .rawfile import (
    NS,
    SUFFIX,
    RawFormatError,
    iter_records,
    list_files,
    read_meta,
)


def collect(paths: list[str]) -> list[Path]:
    """Recording files under `paths`, in recording order."""
    found: list[tuple[int, str, Path]] = []
    for raw in paths:
        path = Path(raw)
        candidates = sorted(path.rglob("*" + SUFFIX)) if path.is_dir() else [path]
        for candidate in candidates:
            try:
                meta = read_meta(candidate)
            except (OSError, RawFormatError) as exc:
                print(f"skipping {candidate}: {exc}", file=sys.stderr)
                continue
            created = meta.get("created_ns")
            found.append((created if isinstance(created, int) else 0, candidate.name, candidate))
    found.sort(key=lambda item: (item[0], item[1]))
    return [path for _, _, path in found]


def records(paths: list[Path], start_ns: int | None, end_ns: int | None) -> Iterator[tuple[int, bytes]]:
    for path in paths:
        try:
            for ts_ns, data in iter_records(path):
                if start_ns is not None and ts_ns < start_ns:
                    continue
                if end_ns is not None and ts_ns >= end_ns:
                    continue
                yield ts_ns, data
        except RawFormatError as exc:
            print(f"skipping {exc}", file=sys.stderr)


def _utc_ms(ts_ns: int) -> str:
    dt = datetime.fromtimestamp(ts_ns / NS, timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def export_lines(stream: Iterator[tuple[int, bytes]], include_all: bool) -> Iterator[dict[str, Any]]:
    """ADR-018 CSI frames in the JSONL shape of scripts/record-csi-udp.py
    (with the original receive time); other datagrams as hex with --all."""
    for ts_ns, data in stream:
        frame = parse_adr018(data)
        if frame is not None:
            yield {"type": "raw_csi", "timestamp": _utc_ms(ts_ns), "ts_ns": ts_ns, **frame}
        elif include_all:
            kind, node = classify(data)
            yield {"type": "datagram", "timestamp": _utc_ms(ts_ns), "ts_ns": ts_ns,
                   "kind": kind, "node_id": node, "len": len(data), "hex": data.hex()}


def _opt_time(value: str | None, name: str) -> int | None:
    if value is None:
        return None
    try:
        number = float(value)
    except ValueError:
        return to_ns(value, name)
    return to_ns(number, name)


def cmd_export(args: argparse.Namespace) -> int:
    start_ns = _opt_time(args.start, "--start")
    end_ns = _opt_time(args.end, "--end")
    files = collect(args.paths)
    if not files:
        print("no recording files found", file=sys.stderr)
        return 1
    try:
        out: TextIO = open(args.output, "x", encoding="utf-8") if args.output else sys.stdout
    except FileExistsError:
        print(f"{args.output} exists; not overwriting it", file=sys.stderr)
        return 1
    count = 0
    try:
        for line in export_lines(records(files, start_ns, end_ns), args.all):
            out.write(json.dumps(line) + "\n")
            count += 1
    except BrokenPipeError:
        # Piped into `head` or similar, which stopped reading.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    finally:
        if out is not sys.stdout:
            out.close()
    print(f"exported {count} lines from {len(files)} file(s)", file=sys.stderr)
    return 0


def parse_target(raw: str, allow_remote: bool) -> tuple[str, int]:
    host, sep, port = raw.strip().rpartition(":")
    host = host.strip("[]")
    if not sep or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError(f"--target must be ip:port, got {raw!r}")
    ip = ipaddress.ip_address(host)
    if not ip.is_loopback and not allow_remote:
        raise ValueError(f"{host} is not a loopback address; add --allow-remote to send "
                         "recorded person data over the network")
    return host, int(port)


def cmd_replay(args: argparse.Namespace) -> int:
    try:
        host, port = parse_target(args.target, args.allow_remote)
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.speed < 0:
        print("--speed must be >= 0", file=sys.stderr)
        return 2
    files = collect(args.paths)
    if not files:
        print("no recording files found", file=sys.stderr)
        return 1
    start_ns = _opt_time(args.start, "--start")
    end_ns = _opt_time(args.end, "--end")
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sent = 0
    with socket.socket(family, socket.SOCK_DGRAM) as sock:
        t0_rec: int | None = None
        t0_wall = time.monotonic()
        for ts_ns, data in records(files, start_ns, end_ns):
            if t0_rec is None:
                t0_rec = ts_ns
            if args.speed > 0:
                due = t0_wall + (ts_ns - t0_rec) / NS / args.speed
                delay = due - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
            sock.sendto(data, (host, port))
            sent += 1
    print(f"replayed {sent} datagrams to {host}:{port}", file=sys.stderr)
    return 0


def cmd_info(args: argparse.Namespace) -> int:
    try:
        cfg = Config.from_env({"CSI_DATA_DIR": str(Path(args.data_dir).resolve()),
                               "MQTT_HOST": ""})
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    ring = list_files(cfg.ring_dir)
    store = KeepStore(cfg)
    clips = []
    if cfg.keep_dir.is_dir():
        for day_dir in sorted(p for p in cfg.keep_dir.iterdir() if p.is_dir()):
            clips.extend(store.clips(day_dir.name))
    pending = []
    try:
        raw = json.loads((cfg.keep_dir / "pending.json").read_text(encoding="utf-8"))
        pending = raw.get("pending", []) if isinstance(raw, dict) else []
    except (OSError, ValueError):
        pass
    summary = {
        "ring": {
            "files": len(ring),
            "bytes": sum(e.size for e in ring),
            "oldest": iso(ring[0].created_ns) if ring else None,
            "newest_write": iso(max(e.mtime_ns for e in ring)) if ring else None,
        },
        "clips": [{k: c.get(k) for k in ("file", "tag", "person", "start", "duration_s",
                                          "records", "bytes", "coverage", "note")}
                  for c in clips],
        "pending": pending,
    }
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
        return 0
    r = summary["ring"]
    print(f"ring:    {r['files']} files, {r['bytes'] / 1e9:.2f} GB, "
          f"from {r['oldest'] or '-'} to {r['newest_write'] or '-'}")
    print(f"clips:   {len(clips)} ({sum(c.get('bytes') or 0 for c in clips) / 1e9:.2f} GB)")
    for c in summary["clips"]:
        print(f"  {c['start']}  {c['tag']:<16} {float(c['duration_s'] or 0) / 60:6.1f} min  "
              f"{(c['bytes'] or 0) / 1e6:8.1f} MB  {c['person'] or '-':<6} {c['file']}")
    print(f"pending: {len(pending)}")
    for p in pending:
        if isinstance(p, dict):
            print(f"  {p.get('id')} {p.get('tag')} until {iso(int(p.get('end_ns', 0)))}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m csi_recorder",
                                     description="RuView raw CSI recorder (WP0.5)")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="record the sensing-server UDP tee (default)")

    info = sub.add_parser("info", help="summarise the ring, clips and pending requests")
    info.add_argument("--data-dir", default="/data")
    info.add_argument("--json", action="store_true")

    export = sub.add_parser("export", help="write CSI frames as JSONL (record-csi-udp.py format)")
    export.add_argument("paths", nargs="+")
    export.add_argument("--start")
    export.add_argument("--end")
    export.add_argument("--all", action="store_true", help="also non-CSI datagrams, as hex")
    export.add_argument("-o", "--output", help="new file (name it *.csi.jsonl); default stdout")

    replay = sub.add_parser("replay", help="re-send recorded datagrams with their timing")
    replay.add_argument("paths", nargs="+")
    replay.add_argument("--target", required=True, help="ip:port, e.g. 127.0.0.1:5005")
    replay.add_argument("--speed", type=float, default=1.0, help="1 = real time, 0 = no pauses")
    replay.add_argument("--start")
    replay.add_argument("--end")
    replay.add_argument("--allow-remote", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "info":
            return cmd_info(args)
        if args.command == "export":
            return cmd_export(args)
        if args.command == "replay":
            return cmd_replay(args)
    except KeepError as exc:   # a bad --start / --end
        print(exc, file=sys.stderr)
        return 2
    from .service import run
    return run()

