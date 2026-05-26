# ADR-100: Per-track vital signs for multi-person households

| Field | Value |
|-------|-------|
| **Status** | Proposed |
| **Date** | 2026-05-17 |
| **Deciders** | ruv |
| **Codename** | **per-track-vitals** (step B of the HA health-display plan) |
| **Relates to** | ADR-014 (SOTA signal processing pipeline), ADR-024 (AETHER contrastive embedding), ADR-029 (RuvSense multistatic), ADR-031 (RuView sensing-first RF mode) |
| **Supersedes** | — |

---

## 1. Context

### 1.1 What works today

Step A of the Home-Assistant health-display plan landed in `claude/homeassistant-health-display-nHfBQ`:

* `MultistaticFuser` reads `SENSING_NODE_POSITIONS` deterministically (frames sorted by `node_id`).
* MQTT bridge publishes per-slot entities (`sensor.person_1_present`, ...) plus, when a profile JSON exists, named entities (`sensor.alice_present`, ...).
* `profile_loader::ProfileStore` resolves a household member's name when **exactly one person** is tracked, using HR/BR baselines as the discriminator.

The user-visible result is good: one person in the apartment → HA shows "alice present, alice at (x, y)". Multiple people in the apartment → HA shows generic slot entities, no names.

### 1.2 The gap step B closes

The remaining gap is that the **vital signs themselves** are a single global reading regardless of how many people are present:

```rust
// types.rs:111 — vitals attached at the envelope, not per-track:
pub struct SensingUpdate {
    ...
    pub vital_signs: Option<VitalSigns>,  // ← one value, even for n=2
    pub persons: Option<Vec<PersonDetection>>,
    ...
}
```

So even with two named entities (`alice`, `bob`) in HA, both would display the same `heart_rate_bpm` if we naively fanned out — because the upstream DSP only produces one. Multi-person profile matching is also blocked: without per-track HR/BR we can't tell which detection's vitals to compare to which profile.

### 1.3 What's available in the codebase already

Three relevant primitives already exist and are tested:

| Module | What it gives us |
|---|---|
| `wifi-densepose-signal/src/ruvsense/multistatic.rs` | Attention-weighted CSI fusion + per-node amplitude/phase. Provides the spatial diversity needed to attribute signal energy to a specific bbox. |
| `wifi-densepose-signal/src/ruvsense/pose_tracker.rs` | Kalman + AETHER re-ID across frames. Stable `TrackId` per person, which is the natural key for per-track vitals. |
| `wifi-densepose-signal/src/vital_signs/*` (the existing detector chain) | FFT + bandpass on the breathing band (0.1-0.5 Hz) and cardiac band (0.8-2.5 Hz). Currently runs on global fused amplitudes. |

The structural missing piece is a **per-track subcarrier mask** — a way to say "these subcarriers carry mostly track 1's perturbation, those mostly track 2's" — so the existing vitals detector can be re-run per track instead of globally.

---

## 2. Decision

Adopt a **mask-and-rerun** per-track vital-signs pipeline rather than a from-scratch joint estimator. Concretely:

### D1. Add a track-attributed subcarrier mask service in `wifi-densepose-signal`

A new module `wifi-densepose-signal/src/ruvsense/track_attribution.rs` produces, per frame, a `Vec<TrackSubcarrierMask>`:

```rust
pub struct TrackSubcarrierMask {
    pub track_id: TrackId,
    pub mask: Vec<f32>,  // length = n_subcarriers, values in [0, 1]
    pub confidence: f32, // attribution quality, 0..1
}
```

The mask weights each subcarrier by how strongly it varies in sync with that track's motion signature. Two attribution strategies, in priority order:

* **Multistatic geometry** (preferred when ≥ 2 nodes and node positions are known): for each track's predicted bbox centre, compute the expected path-length difference to each node pair. Subcarriers whose phase difference matches that geometry get high mask weight for that track.
* **Motion-correlation fallback** (single-node or unknown positions): correlate per-subcarrier variance with each track's Kalman-predicted motion magnitude. Subcarriers correlated with track A's motion get mask weight for A.

When the per-track masks would overlap substantially (NMS > 0.5 IoU on mask vectors), the attribution falls back to "ambiguous" — the masks are not produced for that frame.

### D2. Per-track vital-signs detector reuse

The existing `VitalSignDetector` in `vital_signs.rs` operates on a single amplitude time-series. We **do not fork it**. Instead, the sensing-server builds one masked amplitude time-series per track:

```rust
let track_amp_history = global_history * mask;  // element-wise
detector.estimate(&track_amp_history)
```

This keeps the breathing-band FFT, the heartbeat lock-in, the median smoother, and the EMA all unchanged — they just operate on a track-specific signal.

### D3. `PersonDetection` gains an optional `vital_signs` field

```rust
pub struct PersonDetection {
    pub id: u32,
    pub confidence: f64,
    pub keypoints: Vec<PoseKeypoint>,
    pub bbox: BoundingBox,
    pub zone: String,
    pub label: Option<String>,                  // step A
    pub vital_signs: Option<VitalSigns>,        // step B  ← new
    pub vital_attribution_confidence: Option<f32>,  // step B  ← new
}
```

The envelope-level `SensingUpdate.vital_signs` is retained (it now means "global / household average") for back-compat. When per-track vitals are available, `PersonDetection.vital_signs` is preferred by downstream consumers.

### D4. Profile matching fans out across all tracks

Once D3 is in place, the profile match helper in `sensing-server/src/main.rs` runs per track instead of only when `persons.len() == 1`:

```rust
for person in &mut tracked {
    if let (Some(vs), Some(store)) = (&person.vital_signs, &profile_store) {
        let obs = MatchObservation {
            hr_bpm: vs.heart_rate_bpm.map(|v| v as f32),
            br_bpm: vs.breathing_rate_bpm.map(|v| v as f32),
        };
        if let Some((name, _d)) = store.match_observation(&obs, threshold) {
            person.label = Some(name);
        }
    }
}
```

When two tracks both match the same profile, the closer one wins; the other is left unlabeled (the bridge then routes it to a numeric slot). This is a greedy assignment — adequate for 2-3 person households. Hungarian is overkill here.

### D5. Bridge fans out HR/BR per labeled entity

`docker/bridge/bridge.py` is updated to read `person.vital_signs` when present and publish:

* `sensor.alice_heart_rate` (bpm, state_class measurement)
* `sensor.alice_breathing_rate` (bpm, state_class measurement)
* `sensor.alice_vital_attribution_confidence` (%, diagnostic — so the user can see when the per-track DSP is unsure)

For unlabeled fallback slots, the same fields appear under `sensor.person_1_heart_rate` etc.

### D6. Quality gating

A per-track vital reading is only published when **all** of the following hold:

* `vital_attribution_confidence >= 0.6`
* the detector's own `breathing_confidence` / `heartbeat_confidence` is above its existing minimums
* the track has been Active (not Tentative or Lost) for at least 3 ticks

When gating fails, the bridge publishes `null` for that sensor and HA shows `unavailable`. Better silent than wrong.

### D7. Performance budget

At the current 10 Hz tick rate and 56 subcarriers × 4 nodes, the per-track mask computation is dominated by an O(n_tracks × n_subcarriers × n_nodes) correlation pass: ~2k multiplications per tick. The per-track FFT for breathing-band (256-sample windows) costs ~6k multiplications per track per tick. Total: <100k multiplications/tick for 2 people, which is comfortably under 1 % of a Pi 5 core. No additional crates required.

### D8. Acceptance tests

Two new integration tests under `v2/crates/wifi-densepose-sensing-server/tests/`:

* `per_track_vitals_single_person.rs` — single track → `person.vital_signs.is_some()`, matches the global vital_signs within 1 bpm.
* `per_track_vitals_two_people.rs` — synthetic two-track scenario with distinct breathing rates → both tracks get vitals, attribution distinguishes them with > 80 % accuracy across 100 frames.

A new unit test in `track_attribution.rs` checks the multistatic-geometry mask is consistent with the path-length formula for a known node geometry.

---

## 3. Consequences

### 3.1 What gets better

* **Two named entities in HA show distinct, real-time HR/BR** — the end state of the health-display plan.
* **Profile matching scales to multi-person ticks**: `alice` and `bob` can both be present and both be correctly labeled, because each has their own HR/BR signature.
* **No new external dependencies**: the change is contained to `wifi-densepose-signal` and `wifi-densepose-sensing-server`. Existing crates (ruvector, midstream, rvcsi) are untouched.

### 3.2 What stays unsolved

* **Heavily overlapping bboxes** (two people on the same couch, < 0.5 m apart) — the attribution masks fuse and we fall back to gating. The HR/BR sensors go `unavailable` for both, which is honest.
* **Couples with very similar baselines** — the matcher in step A already had this ceiling; step B doesn't change it. The AETHER embedding work (still future) is the real fix.
* **Heart rate reliability under motion** — the existing detector struggles when motion dominates breathing; this ADR doesn't change that.

### 3.3 Migration

* No schema break: `PersonDetection.vital_signs` is `Option<VitalSigns>` with `#[serde(default)]`, and `SensingUpdate.vital_signs` is retained.
* MQTT bridge picks up the new field opportunistically; older bridge versions ignore it.
* Profile JSON format is unchanged from step A — same `EnrolledProfile`.

### 3.4 Open questions

* **Per-track vital smoothing window**: the global pipeline uses a 21-sample median + EMA. Should each track have its own state, or share a global filter parameter? Decision deferred until benchmark data is collected.
* **Attribution-aware mode for the existing `vital_signs.rs` smoother**: when track masks shuffle (e.g., people swap positions briefly), the per-track time-series get spliced. EMA across the splice would smear vitals. The acceptance test suite needs to cover this; if smearing is bad, splice detection becomes a step-B-follow-up.

---

## 4. Out of scope

* AETHER-based body-shape re-identification (its own ADR, separate timeline).
* Real 3-D keypoint geometry (would let the bridge enable Z-axis gating against downstairs neighbours — different work item).
* Cross-environment domain generalisation (MERIDIAN / ADR-027 handles that).
