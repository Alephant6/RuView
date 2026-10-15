#!/usr/bin/env python3
"""Backfill one person's Apple Health history from the Health app export.

On the iPhone: Health app -> profile picture -> Export All Health Data.
Copy the resulting export.zip (or the export.xml inside it) to the NAS and run:

  docker compose -f docker-compose.yml -f docker-compose.health.yml run --rm \
      -u root -v /path/to/dir:/import:ro --entrypoint python health-ingest \
      /app/import_export.py --person sq /import/export.zip

Only vitals and sleep are imported by default (steps and energy add millions
of rows and little ground truth); pass --types all to import every record.
Re-running is safe: points with the same series and timestamp overwrite.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import zipfile
import xml.etree.ElementTree as ET
from typing import IO, Iterator

import ingest

DEFAULT_TYPES = {
    "HKQuantityTypeIdentifierHeartRate": "heart_rate",
    "HKQuantityTypeIdentifierRestingHeartRate": "resting_heart_rate",
    "HKQuantityTypeIdentifierWalkingHeartRateAverage": "walking_heart_rate_average",
    "HKQuantityTypeIdentifierHeartRateVariabilitySDNN": "heart_rate_variability",
    "HKQuantityTypeIdentifierOxygenSaturation": "blood_oxygen_saturation",
    "HKQuantityTypeIdentifierRespiratoryRate": "respiratory_rate",
    "HKQuantityTypeIdentifierAppleSleepingWristTemperature": "apple_sleeping_wrist_temperature",
    "HKQuantityTypeIdentifierBodyTemperature": "body_temperature",
    "HKCategoryTypeIdentifierSleepAnalysis": "sleep_analysis",
}
_PREFIX = re.compile(r"^HK(Quantity|Category|Correlation)TypeIdentifier")


def metric_name(hk_type: str) -> str:
    return DEFAULT_TYPES.get(hk_type) or ingest._snake(_PREFIX.sub("", hk_type))


def open_xml(path: str) -> IO[bytes]:
    if path.endswith(".zip"):
        z = zipfile.ZipFile(path)
        name = next(n for n in z.namelist() if n.endswith("/export.xml") or n == "export.xml")
        return z.open(name)
    return open(path, "rb")


def records(fh: IO[bytes], all_types: bool, since: int | None) -> Iterator[tuple[str, int, int | None, str, str, str]]:
    for _, el in ET.iterparse(fh, events=("end",)):
        if el.tag != "Record":
            if el.tag in ("Workout", "ActivitySummary", "Correlation"):
                el.clear()
            continue
        a = el.attrib
        hk = a.get("type", "")
        if all_types or hk in DEFAULT_TYPES:
            start = ingest.parse_date(a.get("startDate"))
            if start is not None and (since is None or start >= since):
                yield (metric_name(hk), start, ingest.parse_date(a.get("endDate")),
                       a.get("value", ""), a.get("unit", ""), a.get("sourceName", ""))
        el.clear()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("export", help="export.zip or export.xml")
    ap.add_argument("--person", required=True, help="person tag, e.g. sq or ml")
    ap.add_argument("--types", choices=("vitals", "all"), default="vitals")
    ap.add_argument("--since", help="only records on or after this date (YYYY-MM-DD)")
    ap.add_argument("--dry-run", action="store_true", help="print a few lines and counts, write nothing")
    args = ap.parse_args()
    since = int(time.mktime(time.strptime(args.since, "%Y-%m-%d"))) if args.since else None

    batch: list[str] = []
    total = 0
    with open_xml(args.export) as fh:
        for metric, start, end, value, unit, source in records(fh, args.types == "all", since):
            l = ingest.sample_line(args.person, metric, start, end, value, unit, source)
            if not l:
                continue
            total += 1
            if args.dry_run:
                if total <= 5:
                    print(l)
                continue
            batch.append(l)
            if len(batch) >= ingest.WRITE_CHUNK:
                ingest.write_influx(batch)
                batch.clear()
                print(f"{total} points written", file=sys.stderr)
    if batch and not args.dry_run:
        ingest.write_influx(batch)
    print(f"done: {total} points for {args.person}{' (dry run)' if args.dry_run else ''}")


if __name__ == "__main__":
    main()
