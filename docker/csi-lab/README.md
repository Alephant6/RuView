# csi-lab — offline accuracy lab for raw CSI (WP2.2 screening)

`csi_lab.py` scores labelled stretches of the csi-recorder's raw CSI
(`ring/` and `keep/`, see `docker/csi-recorder`) and prints aggregates only:
frame rates, RSSI quantiles, transmitter clusters, held-out false-alarm
rates, detection rates, per-room AUCs and room-localization accuracy. It is
one file with numpy as its only dependency, so it runs in a throwaway
container next to the data. It opens recordings read-only and writes
nothing. No frame, I/Q value or per-window series leaves the process: raw
CSI is person data (ADR-299) and stays on the NAS.

Every number it prints is MEASURED on the segments it was given. A few
segments from one evening are a screen, not a validation: report them with
the segment list, and never as camera-grade or general accuracy.

## What it does

1. **Transmitter separation.** A node in promiscuous mode hears several
   transmitters on its channel (the access point, the other nodes,
   neighbours, phones, the robot vacuum). Each is a different radio path
   with its own static channel shape, and the ADR-018 header carries no
   source address, so one node's stream mixes links. Per node and frame
   width, frames are clustered by gain-normalised log-amplitude shape plus
   RSSI (k-means, k chosen by silhouette on the empty-home reference, k = 1
   unless the clusters are clearly separate). Each cluster is scored as its
   own link. Frames are never dropped for looking unusual: a person next to
   the receiver changes the shape too. Frames far from every empty-home
   cluster are counted per segment as a diagnostic (a new device, or a
   person very close to the node).
2. **Per-link features** over windows (default 10 s, hop 5 s), each against
   the empty-home reference:
   - `static`: shift of the window-mean amplitude shape, per subcarrier in
     units of the reference spread, after projecting out the three main
     empty-home variation modes (finds a person who is sitting still);
   - `motion`: within-window variance relative to the reference (moving);
   - `phase`: shift of the per-frame linearly detrended phase (removes the
     random timing slope and common phase of each frame);
   - `rssi`: shift of the mean RSSI;
   - `fused`: mean exceedance of the four;
   - `fused_mr`: mean exceedance of `motion` and `rssi` only, the two that
     do not compare against a stored channel shape and so drift least.
   Gain normalisation (log amplitude minus its per-frame mean) removes the
   receiver's AGC steps; null (guard/DC) subcarriers are dropped by their
   amplitude. Node scores are the mean over the node's links; `home` is
   the mean over every link. `fused_med` is the median over the links
   instead, so one link that drifts (or one transmitter that changed)
   cannot raise a node or the home on its own.
3. **Honest evaluation.** Empty-home segments are cut into chunks
   (`--chunk-min`, default 10). Each chunk is held out in turn; the rest
   form the reference. The threshold is the reference's 99th percentile of
   *out-of-sample* scores (each reference chunk scored by a model fitted
   without it), so it reflects a baseline taken at another time. By default
   the chunks adjacent to the held-out one are also left out of the
   reference when enough remain (`guard_relaxed` says when they were not).
   Longer horizons (`--horizons`, default 10,30,60 s) average consecutive
   windows; each horizon has its own threshold. False alarms are also
   reported per held-out chunk: a baseline that drifts, or a transmitter
   that appears or changes, shows up as one bad chunk (usually at an end
   of the empty stretch) rather than as noise spread over all of them.
4. **Rooms.** With `rooms` mapping node ids to rooms, every node detector
   gets an AUC for "its room occupied" against held-out empty plus other
   occupied rooms, and against other occupied rooms only (cross-room
   leakage). Localization picks the node with the largest excess over its
   own threshold (the top two for two-room segments).
5. **Links.** Each fold re-clusters its own reference, and a fold's cluster
   is named after the all-empty cluster most of its frames fall in
   (`n2w128c3` = node 2, 128-wide frames, cluster 3 of the streams table),
   so a link keeps its name across folds. A name ending in `+` is a second
   cluster for the same transmitter in one fold only: that transmitter's
   channel changed during the empty stretch. The links table gives every
   link's held-out false alarms per score and its median excess in every
   segment.
6. **Confounders.** A segment with `tags` (for example
   `["vacuum:kitchen"]`) is scored and reported, including in the
   median-excess and links tables, but never used as empty-home
   reference, for AUCs or for localization. Comparing a tagged segment
   with an untagged one that has the same people in the same rooms shows
   what the confounder adds, node by node and link by link. Frames far
   from every empty-home transmitter are reported per segment with their
   RSSI: a new transmitter (a phone, the vacuum's own Wi-Fi) has a level
   of its own.
7. **Drift and stillness.** On one link a still person and a slow drift of
   the empty channel look the same: both move the channel away from the
   empty-home shape. Two scores attack that from different sides.
   - `fused_dr` (always on; `--drift-rank`, default 2, 0 = off): the
     per-link `fused` excess of all links, with the `rank` directions in
     which the links moved together during the empty-home reference
     projected out (learned without the window's own chunk for reference
     windows). A swing in temperature or humidity moves every link at
     once and is removed; one room's links rising more than the drift
     pattern predicts is kept. Node = mean residual of its links (signed),
     home = RMS over all links. It does not remove a drift that only one
     link has (that looks exactly like a person near that link; the
     median over links, `fused_med`, is the guard there), and it can only
     remove what the reference showed.
   - `breath` (`--breath`, slower; `--breath-span`, default 60 s): over
     the span ending with each window, the amplitudes are detrended per
     subcarrier, reduced to their two main directions of change, and
     searched for the strongest sinusoid between 0.1 and 0.5 Hz with a
     least-squares (Lomb-Scargle) fit that copes with uneven frame times.
     Score = log10(1 + F), F the usual periodogram statistic (around 1 for
     noise). A slow drift cannot raise it; a still, breathing person can,
     while a moving person mostly does not (that is `motion`'s job). Needs
     at least 1.5 frames/s on the link; it is not part of `fused`.
     Wi-Fi frames come in bursts, and two frame types alternating in bursts
     make the amplitude periodic with nobody there. So the spectral window
     of the frame times, W(f) = |sum exp(-2 pi i f t_k)|^2 / N^2, is computed
     for every span, and frequencies with N * W(f) > 10 (about 1 for
     irregular arrivals), and their harmonics, are not used. The report
     gives, per link, the share of windows whose best frequency was such a
     frequency ("Frame timing"), and per node and segment the median
     breathing rate of the windows above the empty-home line (breaths/min).
8. **Room patterns** (`pattern`, `pattern_gls` in the localization table).
   "The node with the largest excess" fails when one node reacts to every
   room (the real study node does). The cone model instead: with someone
   in room r the per-link `fused` excess moves along a fixed direction g_r
   (which links rise, relative to each other), while its size depends on
   posture and activity and is left free (alpha_r >= 0). Maximising over
   the size gives, in whitened units, the matched-subspace (GLRT) statistic
   max(0, g'e)^2 / g'g for one occupied room and the energy of a
   two-variable non-negative least-squares fit for two rooms. The room
   patterns are least-squares fits of the labelled segments' mean excess
   on their occupied-room indicators (rooms without a node included), and
   each labelled segment is classified with patterns fitted on the other
   segments only (leave one segment out; a room seen in no other segment is
   untestable). The number of occupied rooms comes from the label, as for
   the node localization. `pattern` whitens the drift-removed excess
   (`fused_dr`'s residual) by each link's empty-home spread; `pattern_gls`
   whitens the plain excess with a low-rank-plus-diagonal empty-home
   covariance (the `--drift-rank` drift directions with their variances
   plus independent link noise), which weights the drift directions down
   instead of removing them. The report also gives the cosine between the
   whitened room patterns: near 1 means two rooms point the same way and
   cannot be told apart whatever the size (merge them).
9. `--compare` reruns everything without transmitter separation.

## Labels

```json
{
  "rooms": {"1": "bedroom", "2": "study", "3": "living"},
  "segments": [
    {"name": "empty", "start": "2026-01-01T10:00:00Z", "end": "2026-01-01T10:40:00Z", "occupied": []},
    {"name": "study_sit", "start": "2026-01-01T11:00:00Z", "end": "2026-01-01T11:20:00Z", "occupied": ["study"]},
    {"name": "walkabout", "start": "2026-01-01T11:30:00Z", "end": "2026-01-01T11:40:00Z", "occupied": null},
    {"name": "vac_kitchen", "start": "2026-01-01T11:40:00Z", "end": "2026-01-01T11:50:00Z", "occupied": ["study"], "tags": ["vacuum:kitchen"]}
  ],
  "exclude": [{"start": "2026-01-01T10:20:00Z", "end": "2026-01-01T10:25:00Z", "why": "vacuum"}]
}
```

- `occupied`: rooms with someone in them; `[]` = nobody home (reference
  and held-out data); `null` = unknown (scored and reported, but not used
  for AUC or localization). Times are UTC ISO 8601 or epoch seconds.
- `tags`: up to four short names of something else that moves or
  transmits in the segment (see Confounders above).
- `at_least: true`: the listed rooms were occupied and the others are
  unknown. Use it for event labels taken from devices fixed in a room (a
  wall panel, a TV remote): a minute or two around a command sent from
  the living-room panel is "at least living". Such a segment is a positive
  for its rooms' AUCs, never a negative for the others, and is not used
  for localization. Only the raw files that overlap a segment are read,
  so short events spread over the 72 h ring are cheap.
- At least two empty-home chunks are required (one 20-minute empty segment
  is enough with the default chunk length). At most 4 hours per run.
- Keep anything that moves and is not a person (the robot vacuum, a fan)
  out of "empty" and out of the clean segments: tag it, or `exclude` it.
- Real labels hold household presence times: pass them on the command
  line, never commit them.

## Run on the NAS

From the deployment's `docker/` directory, with the script fed on stdin
(the container sees the recordings read-only):

```bash
L='{"rooms": {...}, "segments": [...]}'
docker run --rm -i -e L="$L" -v "$PWD/data/csi:/data:ro" python:3.12-slim \
  sh -c 'pip -q install --no-cache-dir numpy >/dev/null 2>&1 && nice -n 19 python - evaluate --labels-json "$L" --compare' \
  < /path/to/csi_lab.py
```

Add `--json` for the full report (every link, every horizon). An hour of
three nodes at about 12 Hz takes well under a minute.

## Tests

```bash
cd docker/csi-lab
python -m pip install -r requirements.txt pytest
python -m pytest -q
```

The tests use synthetic recordings only (`tests/synth.py`: three nodes,
an access point, a neighbour, AGC jitter, random per-frame phase, a body
path that couples most strongly to the receiver in the occupied room;
optionally a house-wide drift, an oversensitive node, a room without a
node, and a transmitter sending in periodic bursts).
