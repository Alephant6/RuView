# RuView deployment TODOs

Per-deployment punch list that the codebase can't track for you. Tick items
off and update notes as you work through them.

## Open

### 1. Enroll alice / bob profiles _(deferred to tomorrow)_

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

## Done

- _(none yet — fill in as items move from Open)_
