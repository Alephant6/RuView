# RuView csi-recorder — raw CSI ring and labelled clips (WP0.5)

Records every UDP datagram the sensing-server admits, byte for byte, so
that heart rate, breathing, presence and identity can be recomputed when
the algorithms improve. The server copies each admitted datagram to a
loopback port (`RUVIEW_UDP_TEE`, ADR-380); the recorder writes them into
hourly gzip files, keeps the last 72 hours, and copies requested time ranges
into permanent clips: quiet-sitting ground-truth sessions, label buttons,
and the minutes before a watchdog problem. Work package WP0.5 in the
architecture plan.

Raw CSI is person data (ADR-299). Recordings stay on the NAS data volume;
`.gitignore` and `scripts/csi-data-policy-check.sh` keep `*.csi.raw.gz` and
`*.csi.meta.json` out of git.

## Layout

```
<CSI_DATA_DIR>/                         (/data in the container)
  ring/2026-10-15/20261015T040000Z.csi.raw.gz     one file per UTC hour
  keep/2026-10-15/20261015T043000Z_gt_session.csi.raw.gz   permanent clip
  keep/2026-10-15/20261015T043000Z_gt_session.csi.meta.json  its sidecar
  keep/pending.json                     requests waiting for their range to end
```

A `.csi.raw.gz` file is one gzip stream: a header (`RVCSIRAW`, version,
JSON metadata) and then records of `u64 receive time ns | u32 length |
datagram`, little-endian. The writer flushes every `FLUSH_INTERVAL_S`, so a
file that is still open, or was cut short by a power loss, reads cleanly up
to its last flush. Datagrams are stored undecoded: ADR-018 CSI frames
(`0xC5110001`), edge vitals, feature state, sync packets, anything else the
server admitted.

Retention: files whose last write is older than `RING_HOURS` are deleted,
then the oldest files until the ring fits `RING_MAX_GB`. Below
`MIN_FREE_GB` free on the volume recording pauses, and resumes with 1 GB of
headroom. Clips are never deleted automatically.

Size (SYNTHETIC estimate, not measured on the NAS): 3 nodes at about 23 Hz
with 128-subcarrier frames come to 3–5 GB per 72 hours; a 2-hour clip is
about 0.1–0.2 GB. The recorder handled more than 40,000 datagrams/s on one
core in the same synthetic test, against about 70/s at home.

## Keep requests

Publish JSON on `ruview/recorder/keep` (QoS 1, not retained):

```json
{"tag": "gt_session", "start": 1792036800, "end": 1792039200,
 "person": "sq", "note": "quiet sitting", "source": "ha", "id": "optional"}
{"tag": "keep_raw", "last_s": 600}
```

- `tag`: `[a-z0-9][a-z0-9_-]{0,31}`; `person`: `[a-z0-9_.]` (sq, ml);
  `note`: up to 200 characters; `source`: who asked (ha, watchdog, ...).
- `start`/`end`: epoch seconds or ISO 8601 with a timezone. The end may lie
  up to `KEEP_MAX_MIN` in the future: the request waits in
  `keep/pending.json` (kept across restarts) until the range has ended.
- At most `KEEP_MAX_MIN` per clip and `KEEP_MAX_H_PER_DAY` per UTC day
  (by clip start, kept plus pending). A repeated id, or the same tag and
  range, is answered `duplicate`.

Every request gets results on `ruview/recorder/keep/result`: `accepted`,
`duplicate` or `rejected` (with `error`) right away, then `kept` (with
`file`, `records`, `bytes`, `coverage` = share of seconds with data, and
datagram counts per kind and node), `empty`, or `failed` after three
attempts.

Who asks:
- `docker/analytics/ha/ruview-labels.yaml` (Home Assistant package, WP0.6):
  "保留最近 10 分钟原始数据" keeps the last 10 minutes; ending a "独处静坐"
  session keeps the whole session (sessions end automatically after
  1 h 55 min). The same package publishes the label annotations
  (`ruview/annotation`) and shows a notification when a request is refused.
- The watchdog (`docker/analytics`): when Vitals Frozen, Vitals Range or
  Presence vs Phones is raised, it keeps the previous `KEEP_ON_RAISE_MIN`
  (20) minutes.

## Status

`ruview/recorder/status` (retained, every `STATUS_INTERVAL_S`): datagram
and byte rates, rates per kind (`csi`, `sync`, `vitals`, ...) and per node
(`hz`, `csi_hz`, `age_s`), ring files/bytes/hours, free disk, `disk_low`,
dropped datagrams, write errors, kernel socket drops, pending keep requests
and today's kept minutes. `ruview/recorder/availability` is `online` /
`offline` (LWT). Telegraf stores both result and status topics; the
watchdog's **Recorder** check raises when the recorder is offline or
silent, the disk is low, writes fail, or no datagram arrives while the
nodes are sending (usually `RUVIEW_UDP_TEE` missing on the server).

## Tools

The image entrypoint is `python -m csi_recorder`; without arguments it
records. The other commands work on the NAS through the container (paths
under `/data`) or on any machine with Python 3.11+, a copy of
`csi_recorder/` and a copy of the files: they need only the standard library.

```bash
# Summary of the ring, clips and pending requests
docker compose exec csi-recorder python -m csi_recorder info

# CSI frames as JSONL in the format of scripts/record-csi-udp.py (with the
# original receive time); --all adds the other datagrams as hex
docker compose exec csi-recorder python -m csi_recorder export \
  /data/keep/2026-10-15/20261015T043000Z_gt_session.csi.raw.gz -o /data/session.csi.jsonl
python -m csi_recorder export ring/ --start 2026-10-15T04:00:00Z --end 2026-10-15T04:10:00Z \
  -o window.csi.jsonl

# Feed a recording into a local sensing-server with the original timing
# (loopback targets only, unless --allow-remote)
python -m csi_recorder replay keep/2026-10-15/ --target 127.0.0.1:5005 --speed 1
```

## Configuration

| Variable | Default | |
|---|---|---|
| `RECORDER_LISTEN` | `127.0.0.1:5006` | loopback `ip:port` only; must match the server's `RUVIEW_UDP_TEE` |
| `CSI_DATA_DIR` | `/data` | inside the container; compose mounts `${CSI_DATA_DIR:-./data/csi}` there |
| `RING_HOURS` | `72` | rolling window |
| `RING_MAX_GB` | `20` | size cap of the ring |
| `MIN_FREE_GB` | `2` | pause recording below this much free disk |
| `KEEP_MAX_MIN` | `120` | longest clip |
| `KEEP_MAX_H_PER_DAY` | `2` | clip budget per UTC day; `0` turns keeping off |
| `FLUSH_INTERVAL_S` | `5` | data older than this is readable on disk |
| `STATUS_INTERVAL_S` | `30` | |
| `GZIP_LEVEL` | `6` | 1–9 |
| `MQTT_HOST` / `MQTT_PORT` / `MQTT_USER` / `MQTT_PASS` / `MQTT_PREFIX` | | empty `MQTT_HOST` records without status or keep requests |

## Deploy (NAS)

CI builds `ghcr.io/alephant6/ruview-csi-recorder` (`fork-images.yml`, on
main). The container runs as uid 65534 and needs its data directory. After
`git pull` in the checkout, with `IMAGE_TAG` in `.env` set to the merge
commit's `sha-<7>` once `fork-images.yml` has finished for it:

```bash
cd /volume1/docker/RuView/docker
# No sudo needed, and ./data may be root-owned (the sensing-server mounts it).
docker run --rm -v "$PWD/data:/d" alpine:3.20 sh -c 'mkdir -p /d/csi && chown 65534:65534 /d/csi'
docker compose pull csi-recorder sensing-server analytics
docker compose up -d sensing-server csi-recorder analytics   # server picks up RUVIEW_UDP_TEE
docker compose restart telegraf
docker compose logs --tail 20 csi-recorder                    # "recording to ring/..."
docker compose exec csi-recorder python -m csi_recorder info
```

Then install `docker/analytics/ha/ruview-labels.yaml` as a Home Assistant
package (instructions at the top of the file).

## Tests

```bash
cd docker/csi-recorder
python -m pip install -r requirements.txt pytest
python -m pytest -q
```

Synthetic datagrams only, in temporary directories.
