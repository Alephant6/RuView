"""The `.csi.raw.gz` container: received UDP datagrams, byte for byte.

A file is one gzip stream holding

    header  b"RVCSIRAW" | u16 version | u32 n | n bytes of JSON metadata
    record  u64 receive time (ns since the epoch) | u32 n | n bytes datagram
    record  ...

all little-endian. The writer issues a zlib sync flush every few seconds, so
a file that is still being written, or was cut short by a crash or power
loss, reads cleanly up to its last flush. The datagrams are stored exactly
as the sensing-server admitted them (ADR-018 CSI frames, sync packets, edge
vitals, ...), so any later parser can decode them.

Raw CSI is person data (ADR-299): these files live on the NAS data volume
and never belong in git.
"""

from __future__ import annotations

import calendar
import gzip
import json
import logging
import re
import struct
import time
import zlib
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

MAGIC = b"RVCSIRAW"
VERSION = 1
SUFFIX = ".csi.raw.gz"
META_SUFFIX = ".csi.meta.json"
FORMAT = "ruview-csi-raw"

_HEADER = struct.Struct("<8sHI")
_RECORD = struct.Struct("<QI")
MAX_HEADER_BYTES = 64 * 1024
MAX_DATAGRAM_BYTES = 65535
NS = 1_000_000_000

LOG = logging.getLogger("csi-recorder.rawfile")

_NAME = re.compile(r"^(\d{8}T\d{6})Z(?:-(\d{1,3}))?" + re.escape(SUFFIX) + "$")


class RawFormatError(ValueError):
    pass


def stamp(ts_ns: int) -> str:
    """UTC timestamp used in file names, e.g. 20261015T040000Z."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(ts_ns // NS))


def day(ts_ns: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(ts_ns // NS))


def parse_name(name: str) -> int | None:
    """Creation time (ns) encoded in a file name, or None if it is not ours."""
    m = _NAME.match(name)
    if not m:
        return None
    return calendar.timegm(time.strptime(m.group(1), "%Y%m%dT%H%M%S")) * NS


def unique_path(directory: Path, base: str) -> Path:
    """`<base>.csi.raw.gz`, or `<base>-1.csi.raw.gz`, ... if taken."""
    for n in range(1000):
        name = f"{base}{'-' + str(n) if n else ''}{SUFFIX}"
        path = directory / name
        if not path.exists() and not Path(f"{path}.part").exists():
            return path
    raise FileExistsError(f"no free file name for {base} in {directory}")


class RawWriter:
    """Append datagrams to a new `.csi.raw.gz` file."""

    def __init__(self, path: Path, meta: dict[str, Any], level: int = 6) -> None:
        self.path = path
        header = json.dumps({"format": FORMAT, "version": VERSION, **meta},
                            separators=(",", ":")).encode()
        if len(header) > MAX_HEADER_BYTES:
            raise ValueError("header metadata too large")
        self._fh: BinaryIO = open(path, "xb")
        try:
            self._gz = gzip.GzipFile(filename="", fileobj=self._fh, mode="wb",
                                     compresslevel=level, mtime=0)
            self._gz.write(_HEADER.pack(MAGIC, VERSION, len(header)) + header)
        except BaseException:
            self._fh.close()
            raise
        self.records = 0
        self.payload_bytes = 0
        self.first_ns: int | None = None
        self.last_ns: int | None = None

    def write(self, ts_ns: int, data: bytes) -> None:
        if len(data) > MAX_DATAGRAM_BYTES:
            raise ValueError("datagram too large")
        self._gz.write(_RECORD.pack(ts_ns, len(data)) + data)
        self.records += 1
        self.payload_bytes += len(data)
        if self.first_ns is None:
            self.first_ns = ts_ns
        self.last_ns = ts_ns

    def flush(self) -> None:
        """Make everything written so far readable from disk (zlib sync flush)."""
        self._gz.flush()
        self._fh.flush()

    def close(self) -> None:
        try:
            self._gz.close()
        finally:
            self._fh.close()

    def __enter__(self) -> RawWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _read_header(fp: BinaryIO) -> dict[str, Any]:
    head = fp.read(_HEADER.size)
    if len(head) < _HEADER.size:
        raise RawFormatError("truncated header")
    magic, version, n = _HEADER.unpack(head)
    if magic != MAGIC:
        raise RawFormatError("not a RuView CSI raw file")
    if version != VERSION:
        raise RawFormatError(f"unsupported version {version}")
    if n > MAX_HEADER_BYTES:
        raise RawFormatError("header too large")
    blob = fp.read(n)
    if len(blob) < n:
        raise RawFormatError("truncated header")
    meta = json.loads(blob)
    if not isinstance(meta, dict):
        raise RawFormatError("header is not a JSON object")
    return meta


def read_meta(path: Path) -> dict[str, Any]:
    try:
        with gzip.open(path, "rb") as fp:
            return _read_header(fp)
    except (EOFError, gzip.BadGzipFile, zlib.error, json.JSONDecodeError) as exc:
        raise RawFormatError(f"{path.name}: {exc}") from exc


def iter_records(path: Path) -> Iterator[tuple[int, bytes]]:
    """(receive time ns, datagram) in file order. A missing gzip trailer or a
    partial last record (file still open, or cut short) ends the iteration
    quietly; a wrong magic raises RawFormatError."""
    with gzip.open(path, "rb") as fp:
        try:
            _read_header(fp)
        except (EOFError, zlib.error, gzip.BadGzipFile, json.JSONDecodeError) as exc:
            raise RawFormatError(f"{path.name}: unreadable header ({exc})") from exc
        try:
            while True:
                rec = fp.read(_RECORD.size)
                if len(rec) < _RECORD.size:
                    return
                ts_ns, n = _RECORD.unpack(rec)
                if n > MAX_DATAGRAM_BYTES:
                    return  # corrupt length: nothing after it can be trusted
                data = fp.read(n)
                if len(data) < n:
                    return
                yield ts_ns, data
        except (EOFError, zlib.error, gzip.BadGzipFile):
            return


@dataclass(frozen=True)
class FileEntry:
    path: Path
    created_ns: int
    size: int
    mtime_ns: int


def list_files(root: Path) -> list[FileEntry]:
    """Every `.csi.raw.gz` below `root`, oldest first."""
    out = []
    if not root.is_dir():
        return out
    for path in root.rglob("*" + SUFFIX):
        created = parse_name(path.name)
        if created is None:
            continue
        try:
            st = path.stat()
        except FileNotFoundError:   # removed by retention meanwhile
            continue
        out.append(FileEntry(path, created, st.st_size, st.st_mtime_ns))
    out.sort(key=lambda e: (e.created_ns, e.path.name))
    return out


def iter_range(entries: Iterable[FileEntry], start_ns: int, end_ns: int,
               slack_ns: int = 10 * NS) -> Iterator[tuple[int, bytes]]:
    """Records with start_ns <= t < end_ns. A file holds data from its
    creation time up to its last write (mtime), which is used to skip files
    that cannot overlap. Records are in receive order, so reading a file
    stops a little after `end_ns` (the slack covers small clock steps)."""
    for entry in entries:
        if entry.created_ns > end_ns or entry.mtime_ns + slack_ns < start_ns:
            continue
        try:
            for ts_ns, data in iter_records(entry.path):
                if start_ns <= ts_ns < end_ns:
                    yield ts_ns, data
                elif ts_ns >= end_ns + slack_ns:
                    break
        except FileNotFoundError:
            continue
        except RawFormatError as exc:
            LOG.warning("skipping %s", exc)
