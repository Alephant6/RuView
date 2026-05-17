# RuView deployment TODOs

Per-deployment punch list that the codebase can't track for you. Tick items
off and update notes as you work through them.

## Current state (2026-05-17 evening)

**Opportunity window:** partner is solo in the apartment, expected to stay
solo for a while. This is the right time to do a long, high-quality
enrollment capture for the partner (5–10 min of `n_persons=1` data)
instead of the rushed 90 s we tried earlier. User doesn't need to step
out — partner is already alone.

What today's measurements established (do not re-litigate):

- HR pipeline is **real**, not FFT bin centre:
  - User-dominated capture (both home): 57.3 ± 0.6 bpm, tight single peak
  - Partner-solo capture (90 s): 93.7 ± 7.91 bpm; std inflated by motion
    contamination, centre is plausible
  - Two household members clearly separable on HR alone (~6 σ)
- BR is **still bin-centre** (std = 0.0 across 36 samples at 19.4/18.4 bpm)
  — SNR not enough for breathing yet
- `conf` median 0.46–0.47 — borderline; field is `confidence`, not
  `breathing_confidence` as item #1 below currently writes
- RSSI improved: node 1 −60 dBm, node 2 −51 dBm, node 3 still −78 dBm —
  consider relocating node 3 before next enrollment round
- CSI rate bottleneck is **ESP32 firmware throttle, not RSSI / traffic**:
  ping flood at ~150 pps lifted UDP from 884 → 1419 /30 s (+60 %) but
  per-node CSI stuck near 16 Hz — far below the 30 Hz target in item #2.
  More traffic won't move the needle much; firmware tuning required.
- `present_still` was only 1.2 % of partner-solo samples — either partner
  moved more than expected, or the `motion_level=still` threshold in
  sensing-server is tight. Check before next capture.

## Open

### 1. Enroll alice / bob profiles _(partner-solo window open now, see "Current state" above)_

Without these JSON files, single-person ticks fall back to numeric
`person_1` / `person_2` slots in Home Assistant. The bridge will auto-promote
to named entities (`sensor.alice_present`, ...) the moment a profile matches.

**Pre-reqs:** RSSI must be high enough that `breathing_confidence` and
`heart_rate_bpm` look stable — otherwise the baselines you write down will
be FFT bin centres, not real measurements, and matching will go nowhere.

**Steps:**

1. Have alice alone in the apartment for ~3 min, then bob alone.
2. For each:
   ```bash
   mosquitto_sub -h 192.168.1.227 -p 1883 \
     -u mqtt-bridge -P 'mqtt-bridge-pass' \
     -t 'ruview/state' -v | head -120
   ```
   Skip the first ~10 s (transient), then read off the typical
   `heart_rate_bpm` and `breathing_rate_bpm` over 60 s.
3. `mkdir -p /volume1/docker/RuView/docker/data/profiles`
4. Write the JSON (replace values, leave timestamps as the current time):
   ```json
   {
     "name": "alice",
     "hr_baseline_bpm": 72.0,
     "hr_std_bpm": 3.0,
     "br_baseline_bpm": 14.5,
     "br_std_bpm": 1.2,
     "sample_count": 60,
     "enrolled_at": "2026-05-17T22:00:00Z",
     "last_updated_at": "2026-05-17T22:00:00Z"
   }
   ```
   If unsure about std, use the defaults above (3.0 HR / 1.2 BR) —
   the matcher will adapt.
5. Repeat for bob.json.
6. `sudo docker compose restart sensing-server` — server log should show
   `Loaded 2 enrolled profile(s) ... -> ["alice", "bob"]`.

### 2. Measure real CSI frame rate to decide whether step B is worth doing

Current state from the recent diagnostic:
- 3 nodes balanced (TDM works) — good
- RSSI -77 to -90 dBm — too low for reliable per-track DSP
- HR/BR show as FFT bin centres (12.4 / 16.2 / 26.6 bpm repeating) — confirms SNR is the limiter

**Run two measurements:**

```bash
# A. Bridge-side rate (sensing-server tick rate, smoothed)
timeout 30 mosquitto_sub -h 192.168.1.227 -p 1883 \
  -u mqtt-bridge -P 'mqtt-bridge-pass' \
  -t 'ruview/nodes/+' -v 2>/dev/null | \
  awk -F'/' '{print $3}' | awk '{print $1}' | sort | uniq -c

# B. Raw UDP rate (ESP32 → sensing-server, per-node sum)
timeout 30 sudo tcpdump -i any -n 'udp port 5005' 2>/dev/null | wc -l
```

**Decision matrix:**

| Per-node 30 s msgs (A) | UDP packets 30 s (B) | Conclusion |
|------------------------|----------------------|------------|
| ≥ 900 (≥ 30 Hz)        | ≥ 3000               | Step B viable — start D1 of ADR-100 |
| 300–900                | 1500–3000            | Borderline — improve RSSI first, then retry |
| < 300                  | < 1500               | Too slow — fix RSSI (better antenna / closer AP / continuous traffic source) before step B |

**Most likely first fix:** add a continuous-traffic source on the AP
(`iperf3 -c <AP-IP> -t 0 -b 500K -u` from any always-on device). Pushes
CSI frame rate from beacon-only (~10 Hz) to 30+ Hz with no hardware change.

### 3. Polish Grafana dashboards

The auto-provisioned dashboards work end-to-end (data flows from MQTT
through Telegraf into InfluxDB and Grafana renders it), but several panels
need query/visualisation tweaks. None of these block usage — Node Health
and the time-series panels in Vital Signs already cover the day-to-day
diagnostic need — but cleanup will make screenshots and shared-link views
make sense.

#### Known issues (observed 2026-05-17)

**Presence & People dashboard:**
- `Presence (global)` (state-timeline) → "Data does not have a time field".
  The bool `presence` field needs to be coerced to 0/1 with `_time` kept
  explicitly before feeding a `state-timeline` panel.
- `Per-slot / per-name occupancy` → same root cause as above (bool field +
  state-timeline panel).
- `Fall events (last 24h)` → "No data". `fall_detected` is bool;
  the `filter(fn:(r) => r._value > 0)` step probably needs a `.map`
  to convert bool to int first.
- `Source` → "No data". `source` is configured as a **tag** in
  `telegraf.conf` (`tag_keys = ["motion_level", "source"]`), so it does not
  appear as `_field == "source"`. Panel needs a `schema.tagValues` query
  instead, or remove the panel.
- `Person count` legend lists 4 series split by `motion_level`. Either
  drop the `motion_level` split (it's nonsensical for `n_persons`) or
  add `group()` to merge.

**Vital Signs dashboard:**
- Heart rate / Breathing rate line panels show 3 series split by
  `motion_level` (absent / present_moving / present_still). This is
  correct but the legend is unreadable — apply a label override so the
  legend shows just "absent" / "moving" / "still" instead of the full
  tag dictionary.
- `Current HR` / `Current BR` stat panels show one value per motion_level
  (3 boxes). The medically meaningful one is `present_still`; either
  filter to that tag only, or `group()` and take the last value across
  motion states.
- Apply a colour scheme: `present_still` green (trusted),
  `present_moving` yellow (motion artefacts), `absent` grey (ignore).
- Add a text panel at the top explaining "HR/BR are still **global** —
  per-person values arrive with ADR-100 / step B". Prevents the
  3-lines-look-like-3-people confusion.

#### Approach

These are all dashboard JSON edits under `docker/grafana/dashboards/`.
Bundle into one PR; Grafana picks up provisioning changes within 30 s
without a restart, so iteration is fast.

### 4. Build personal digital fingerprint from collected data

Once InfluxDB has 5-7 days of household data, extract a multi-dimensional
fingerprint per household member that's stable over time but discriminative
between people. This is the "completed" version of step C: today's
`EnrolledProfile` carries only HR/BR baselines (2 dimensions), the
fingerprint expands that to ~10-15 dimensions that survive day-to-day drift.

**Pre-reqs:**
- TODO #1 done (so per-person labels are flowing into InfluxDB)
- TODO #2 done with conclusion "step B viable" (per-track vitals available
  so fingerprint isn't blurred across multiple people)
- RSSI stable σ ≤ 2 dB on Node Health dashboard (otherwise features are
  noise, not biology)

**Fingerprint dimensions (priority-ordered):**

Tier 1 — most stable + most discriminative:
- `hr_rest_mean`, `hr_rest_std` (during `present_still` solo periods)
- `br_rest_mean`, `br_rest_std`
- `hr_motion_slope` — linear fit `HR = a + b·motion_energy`
- `hr_recovery_seconds` — time for HR to return to baseline+5 bpm after
  motion drops
- `presence_hour_hist` — 24-element vector of presence probability per
  hour of day (very personal: bedtime, wakeup, work-from-home pattern)
- `motion_freq_peak_hz` — FFT main peak of `motion_energy` during
  `present_moving`, proxy for walking cadence

Tier 2 — useful adds:
- Spatial heatmap centroid + spread on (x, y) when solo
- Top-3 zone occupancy percentages
- Median session duration
- Per-node RSSI shadow: `mean(rssi | present) - mean(rssi | absent)`,
  one number per node — your body's RF cross-section

Tier 3 — needs step B and/or improved DSP:
- HRV (RMSSD) from per-track HR
- Breathing rhythm entropy
- Gait signature from keypoint sequences (requires real 3D pose)

**Approach:**

1. Add `analysis/` directory at repo root with a Jupyter notebook
   `fingerprint_explore.ipynb`:
   - Pull last 7 days from InfluxDB via `influxdb_client` Python lib
   - Compute every Tier 1 + 2 feature per labeled person
   - Plot per-dimension distributions, do t-test alice vs bob
   - PCA + clustering visualisation to confirm separability
2. Pick the 8-15 features with highest separability (Mann-Whitney U or
   t-test p < 0.01 between household members)
3. Extend `EnrolledProfile` JSON schema with the new fields (all
   `#[serde(default)]` for back-compat)
4. Add a `fingerprint_builder` CLI that takes the date range +
   profile name and computes/writes the extended fingerprint JSON
5. Update `profile_loader::distance()` to use weighted Mahalanobis on
   the extended vector (weight per dimension = 1/std from training data)

**Acceptance:**

- Fingerprint built from week 1 data correctly classifies day-8 readings
  with > 90 % accuracy on a held-out test split.
- Adding the fingerprint doesn't break single-person matching that
  already works today (HR/BR baseline subset acts as fallback).


## Done

- _(none yet — fill in as items move from Open)_
