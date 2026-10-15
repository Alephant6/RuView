"""Rolling store of raw datagrams: one file per UTC hour under ring/.

Files older than RING_HOURS (by last write) are deleted, then the oldest
ones until the ring fits RING_MAX_GB. Below MIN_FREE_GB of free disk the
recording pauses instead of filling the volume, and resumes once 1 GB more
is free again.
"""

from __future__ import annotations

import logging
import shutil
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .config import Config
from .rawfile import NS, FileEntry, RawWriter, day, list_files, stamp, unique_path

LOG = logging.getLogger("csi-recorder.ring")

HOUR_NS = 3600 * NS
MAINTAIN_EVERY_NS = 60 * NS
ERROR_LOG_EVERY_NS = 60 * NS


class Ring:
    def __init__(self, cfg: Config, meta: dict[str, Any],
                 disk_usage: Callable[[Path], Any] = shutil.disk_usage) -> None:
        self.cfg = cfg
        self.root = cfg.ring_dir
        self._meta = meta
        self._disk_usage = disk_usage
        self.writer: RawWriter | None = None
        self._hour: int | None = None
        self._last_flush_ns = 0
        self._last_maintain_ns = 0
        self._last_error_log_ns = 0
        # Observable state for the status topic.
        self.disk_low = False
        self.disk_free_bytes: int | None = None
        self.dropped = 0            # datagrams not stored (disk low, write error)
        self.write_errors = 0
        self.last_error: str | None = None
        self.deleted_files = 0
        self.files = 0
        self.bytes = 0
        self.oldest_ns: int | None = None

    # ── writing ───────────────────────────────────────────────────────────
    def write(self, ts_ns: int, data: bytes) -> bool:
        if self.disk_low:
            self.dropped += 1
            return False
        hour = ts_ns // HOUR_NS
        if self.writer is None or hour != self._hour:
            self._open(ts_ns, hour)
            if self.writer is None:
                self.dropped += 1
                return False
        try:
            self.writer.write(ts_ns, data)
        except (OSError, ValueError) as exc:
            self._error("write failed", exc, ts_ns)
            self._close()
            self.dropped += 1
            return False
        return True

    def tick(self, now_ns: int) -> None:
        """Call often (every datagram and at least once a second)."""
        if self.writer is not None:
            if now_ns // HOUR_NS != self._hour:
                self._close()   # the hour ended with no datagram since: finish the file
            elif now_ns - self._last_flush_ns >= self.cfg.flush_interval_s * NS:
                self._last_flush_ns = now_ns
                try:
                    self.writer.flush()
                except OSError as exc:
                    self._error("flush failed", exc, now_ns)
                    self._close()
        if now_ns - self._last_maintain_ns >= MAINTAIN_EVERY_NS:
            self.maintain(now_ns)

    def close(self) -> None:
        self._close()

    @property
    def current_path(self) -> Path | None:
        return self.writer.path if self.writer is not None else None

    def _open(self, ts_ns: int, hour: int) -> None:
        self._close()
        directory = self.root / day(ts_ns)
        try:
            directory.mkdir(parents=True, exist_ok=True)
            path = unique_path(directory, stamp(ts_ns))
            self.writer = RawWriter(path, {**self._meta, "created_ns": ts_ns}, self.cfg.gzip_level)
        except OSError as exc:
            self._error("cannot open a ring file", exc, ts_ns)
            self.writer = None
            return
        self._hour = hour
        self._last_flush_ns = ts_ns
        LOG.info("recording to %s", path.relative_to(self.root))

    def _close(self) -> None:
        writer, self.writer = self.writer, None
        if writer is None:
            return
        try:
            writer.close()
        except OSError as exc:
            self._error("close failed", exc, time.time_ns())

    def _error(self, what: str, exc: Exception, now_ns: int) -> None:
        self.write_errors += 1
        self.last_error = f"{what}: {exc}"
        if now_ns - self._last_error_log_ns >= ERROR_LOG_EVERY_NS:
            self._last_error_log_ns = now_ns
            LOG.error("%s (errors so far: %d)", self.last_error, self.write_errors)

    # ── retention ─────────────────────────────────────────────────────────
    def maintain(self, now_ns: int) -> None:
        self._last_maintain_ns = now_ns
        current = self.current_path
        cutoff = now_ns - int(self.cfg.ring_hours * HOUR_NS)
        kept: list[FileEntry] = []
        for entry in list_files(self.root):
            if entry.path != current and entry.mtime_ns < cutoff:
                self._delete(entry)
            else:
                kept.append(entry)
        budget = int(self.cfg.ring_max_gb * 1e9)
        total = sum(e.size for e in kept)
        for entry in list(kept):
            if total <= budget:
                break
            if entry.path == current:
                continue
            self._delete(entry)
            kept.remove(entry)
            total -= entry.size
        self._remove_empty_dirs(current)
        self.files, self.bytes = len(kept), total
        self.oldest_ns = kept[0].created_ns if kept else None
        self._check_disk(now_ns)

    def _delete(self, entry: FileEntry) -> None:
        try:
            entry.path.unlink(missing_ok=True)
        except OSError as exc:
            self._error("cannot delete an old ring file", exc, time.time_ns())
            return
        self.deleted_files += 1
        LOG.debug("deleted %s", entry.path.name)

    def _remove_empty_dirs(self, current: Path | None) -> None:
        if not self.root.is_dir():
            return
        for directory in self.root.iterdir():
            if not directory.is_dir() or (current is not None and current.parent == directory):
                continue
            try:
                next(directory.iterdir())
            except StopIteration:
                try:
                    directory.rmdir()
                except OSError:
                    pass
            except OSError:
                pass

    def _check_disk(self, now_ns: int) -> None:
        try:
            free = int(self._disk_usage(self.root).free)
        except OSError as exc:
            self._error("cannot read free disk space", exc, now_ns)
            return
        self.disk_free_bytes = free
        low = self.cfg.min_free_gb * 1e9
        if not self.disk_low and free < low:
            self.disk_low = True
            self._close()
            LOG.error("only %.1f GB free on the data volume; recording paused", free / 1e9)
        elif self.disk_low and free >= low + 1e9:
            self.disk_low = False
            LOG.warning("%.1f GB free again; recording resumed", free / 1e9)
