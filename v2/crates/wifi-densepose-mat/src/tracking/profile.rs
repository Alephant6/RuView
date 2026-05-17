//! Per-person enrolled profiles for re-identifying household members.
//!
//! A profile is a small JSON document keyed by a human-friendly name
//! ("alice", "bob"). At runtime the sensing-server matches detected tracks
//! against the loaded profiles and writes the winning name back onto
//! `PersonDetection.label`, so downstream consumers (Home Assistant via the
//! MQTT bridge) can display real names instead of opaque slot indices.
//!
//! # Available features
//!
//! The profile holds slots for several discriminative features. Today the
//! pipeline only populates **HR / BR baselines** —  embedding and height
//! fields are reserved for future work (see ADR-046 / "step B" of the
//! Home-Assistant health display plan):
//!
//! | Feature             | State    | Notes                                       |
//! |---------------------|----------|---------------------------------------------|
//! | `hr_baseline_bpm`   | active   | Heart rate during the enrolling sample      |
//! | `br_baseline_bpm`   | active   | Breathing rate during the enrolling sample  |
//! | `embedding_mean`    | reserved | AETHER 128-d body-shape vector (step B)     |
//! | `height_m`          | reserved | Keypoint-derived height in metres (step B)  |
//!
//! Reserved fields are tolerated by both the on-disk schema (`#[serde(default)]`)
//! and the [`distance`] metric (weight redistributes among present features),
//! so a profile enrolled today will still be usable once step B lands.

use std::collections::HashMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};

// ---------------------------------------------------------------------------
// Tunables
// ---------------------------------------------------------------------------

/// Weight for the heart-rate term in the matching distance.
const W_HR: f32 = 0.5;
/// Weight for the breathing-rate term in the matching distance.
const W_BR: f32 = 0.5;
/// Reserved for step B — AETHER cosine-distance term.
const W_EMBEDDING: f32 = 0.0;
/// Reserved for step B — keypoint height term.
const W_HEIGHT: f32 = 0.0;

/// Minimum std-dev used when computing the z-score, to avoid division by zero
/// for profiles enrolled from very short, very steady captures.
const MIN_STD_BPM: f32 = 0.5;

/// Default cutoff above which a candidate match is rejected (in normalised
/// distance units). Tuned for the HR+BR-only case where each feature
/// contributes up to ~2 std-devs of distance under normal day-to-day drift.
pub const DEFAULT_MATCH_THRESHOLD: f32 = 1.5;

// ---------------------------------------------------------------------------
// EnrolledProfile
// ---------------------------------------------------------------------------

/// A persisted per-person profile. Loaded from / saved to a JSON file.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EnrolledProfile {
    /// Human-friendly name. Used as the JSON file stem and as the HA entity
    /// suffix (`sensor.heart_rate_alice`).
    pub name: String,
    /// Mean heart rate observed during enrollment, in beats per minute.
    pub hr_baseline_bpm: f32,
    /// Standard deviation of HR samples during enrollment.
    pub hr_std_bpm: f32,
    /// Mean breathing rate observed during enrollment, in breaths per minute.
    pub br_baseline_bpm: f32,
    /// Standard deviation of BR samples during enrollment.
    pub br_std_bpm: f32,
    /// Number of samples averaged into the baselines.
    pub sample_count: u32,
    /// When the profile was first created.
    pub enrolled_at: DateTime<Utc>,
    /// When the profile was last refreshed by drift-correction updates.
    pub last_updated_at: DateTime<Utc>,
    /// AETHER 128-d body-shape embedding mean. Reserved — `None` today.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub embedding_mean: Option<Vec<f32>>,
    /// AETHER 128-d body-shape embedding stddev. Reserved — `None` today.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub embedding_std: Option<Vec<f32>>,
    /// Keypoint-derived height in metres. Reserved — `None` today.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub height_m: Option<f32>,
}

impl EnrolledProfile {
    /// Create a profile from HR/BR statistics, with reserved features unset.
    pub fn from_hr_br(
        name: impl Into<String>,
        hr_baseline_bpm: f32,
        hr_std_bpm: f32,
        br_baseline_bpm: f32,
        br_std_bpm: f32,
        sample_count: u32,
    ) -> Self {
        let now = Utc::now();
        Self {
            name: name.into(),
            hr_baseline_bpm,
            hr_std_bpm: hr_std_bpm.max(MIN_STD_BPM),
            br_baseline_bpm,
            br_std_bpm: br_std_bpm.max(MIN_STD_BPM),
            sample_count,
            enrolled_at: now,
            last_updated_at: now,
            embedding_mean: None,
            embedding_std: None,
            height_m: None,
        }
    }

    /// EMA drift-correction update from a fresh observation. The `alpha`
    /// should be small (e.g. 0.02) so an occasional mismatched observation
    /// doesn't yank the profile.
    pub fn update_from_observation(&mut self, obs: &MatchObservation, alpha: f32) {
        let alpha = alpha.clamp(0.0, 1.0);
        if let Some(hr) = obs.hr_bpm {
            self.hr_baseline_bpm = (1.0 - alpha) * self.hr_baseline_bpm + alpha * hr;
        }
        if let Some(br) = obs.br_bpm {
            self.br_baseline_bpm = (1.0 - alpha) * self.br_baseline_bpm + alpha * br;
        }
        self.sample_count = self.sample_count.saturating_add(1);
        self.last_updated_at = Utc::now();
    }
}

// ---------------------------------------------------------------------------
// MatchObservation
// ---------------------------------------------------------------------------

/// Snapshot of a single detection's discriminative features, used as input
/// to [`ProfileStore::match_observation`].
#[derive(Debug, Clone, Default)]
pub struct MatchObservation {
    pub hr_bpm: Option<f32>,
    pub br_bpm: Option<f32>,
    pub embedding: Option<Vec<f32>>,
    pub height_m: Option<f32>,
}

impl MatchObservation {
    pub fn from_vitals(hr_bpm: Option<f32>, br_bpm: Option<f32>) -> Self {
        Self {
            hr_bpm,
            br_bpm,
            embedding: None,
            height_m: None,
        }
    }

    /// Returns true if no feature is populated — matching will be impossible.
    pub fn is_empty(&self) -> bool {
        self.hr_bpm.is_none()
            && self.br_bpm.is_none()
            && self.embedding.is_none()
            && self.height_m.is_none()
    }
}

// ---------------------------------------------------------------------------
// Distance metric
// ---------------------------------------------------------------------------

/// Weighted normalised distance between a profile and a fresh observation.
///
/// Each feature contributes a z-score-like term (deviation / std). Weights of
/// features that are missing on **either** side are redistributed across the
/// remaining present features, so a profile enrolled today (HR/BR only)
/// still produces a sensible distance.
///
/// Returns `f32::INFINITY` when no feature is comparable.
pub fn distance(profile: &EnrolledProfile, obs: &MatchObservation) -> f32 {
    let mut numerator = 0.0_f32;
    let mut weight_sum = 0.0_f32;

    if let Some(hr) = obs.hr_bpm {
        let z = (hr - profile.hr_baseline_bpm).abs() / profile.hr_std_bpm.max(MIN_STD_BPM);
        numerator += W_HR * z;
        weight_sum += W_HR;
    }

    if let Some(br) = obs.br_bpm {
        let z = (br - profile.br_baseline_bpm).abs() / profile.br_std_bpm.max(MIN_STD_BPM);
        numerator += W_BR * z;
        weight_sum += W_BR;
    }

    // Step B contributions — present today only when both sides agree.
    if let (Some(emb_obs), Some(emb_mean)) = (obs.embedding.as_ref(), profile.embedding_mean.as_ref()) {
        if emb_obs.len() == emb_mean.len() && !emb_obs.is_empty() {
            let cos = cosine_similarity(emb_obs, emb_mean);
            numerator += W_EMBEDDING * (1.0 - cos);
            weight_sum += W_EMBEDDING;
        }
    }
    if let (Some(h_obs), Some(h_p)) = (obs.height_m, profile.height_m) {
        // 0.30 m normalisation: a 30 cm height delta is "very different".
        let d = (h_obs - h_p).abs() / 0.30;
        numerator += W_HEIGHT * d;
        weight_sum += W_HEIGHT;
    }

    if weight_sum <= 1e-6 {
        return f32::INFINITY;
    }
    numerator / weight_sum
}

fn cosine_similarity(a: &[f32], b: &[f32]) -> f32 {
    let mut dot = 0.0_f32;
    let mut na = 0.0_f32;
    let mut nb = 0.0_f32;
    for (&x, &y) in a.iter().zip(b.iter()) {
        dot += x * y;
        na += x * x;
        nb += y * y;
    }
    let denom = (na.sqrt() * nb.sqrt()).max(1e-9);
    dot / denom
}

// ---------------------------------------------------------------------------
// ProfileStore
// ---------------------------------------------------------------------------

/// In-memory map of `name -> EnrolledProfile`, backed by a directory of
/// JSON files. Cheap to clone: profiles are typically O(10s of bytes) and
/// the household has 2-3 entries.
#[derive(Debug, Clone, Default)]
pub struct ProfileStore {
    profiles: HashMap<String, EnrolledProfile>,
    root: PathBuf,
}

impl ProfileStore {
    /// Create an empty store rooted at `root`. The directory is created on
    /// the next save call if it does not exist yet.
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self {
            profiles: HashMap::new(),
            root: root.into(),
        }
    }

    /// Load every `*.json` file under `root` into memory. Missing directory
    /// is **not** an error — it just yields an empty store.
    pub fn load(root: impl AsRef<Path>) -> io::Result<Self> {
        let root = root.as_ref().to_path_buf();
        let mut profiles = HashMap::new();
        if !root.exists() {
            return Ok(Self { profiles, root });
        }

        for entry in fs::read_dir(&root)? {
            let entry = entry?;
            let path = entry.path();
            if path.extension().and_then(|s| s.to_str()) != Some("json") {
                continue;
            }
            let bytes = fs::read(&path)?;
            match serde_json::from_slice::<EnrolledProfile>(&bytes) {
                Ok(profile) => {
                    profiles.insert(profile.name.clone(), profile);
                }
                Err(e) => {
                    tracing::warn!("Skipping malformed profile {:?}: {}", path, e);
                }
            }
        }
        Ok(Self { profiles, root })
    }

    /// Persist a single profile to disk. The directory is created if needed.
    pub fn save_one(&self, profile: &EnrolledProfile) -> io::Result<PathBuf> {
        if !self.root.exists() {
            fs::create_dir_all(&self.root)?;
        }
        let path = self.path_for(&profile.name);
        let json = serde_json::to_vec_pretty(profile)
            .map_err(|e| io::Error::new(io::ErrorKind::Other, e))?;
        fs::write(&path, json)?;
        Ok(path)
    }

    /// Persist every profile currently in the store.
    pub fn save_all(&self) -> io::Result<()> {
        for profile in self.profiles.values() {
            self.save_one(profile)?;
        }
        Ok(())
    }

    /// Insert or replace a profile (does not write to disk — call [`save_one`]).
    pub fn upsert(&mut self, profile: EnrolledProfile) {
        self.profiles.insert(profile.name.clone(), profile);
    }

    /// Remove a profile from memory and from disk. Returns true if it existed.
    pub fn delete(&mut self, name: &str) -> io::Result<bool> {
        let existed = self.profiles.remove(name).is_some();
        let path = self.path_for(name);
        if path.exists() {
            fs::remove_file(path)?;
        }
        Ok(existed)
    }

    /// Sorted list of profile names for stable display.
    pub fn names(&self) -> Vec<&str> {
        let mut out: Vec<&str> = self.profiles.keys().map(String::as_str).collect();
        out.sort_unstable();
        out
    }

    pub fn len(&self) -> usize {
        self.profiles.len()
    }

    pub fn is_empty(&self) -> bool {
        self.profiles.is_empty()
    }

    pub fn get(&self, name: &str) -> Option<&EnrolledProfile> {
        self.profiles.get(name)
    }

    pub fn get_mut(&mut self, name: &str) -> Option<&mut EnrolledProfile> {
        self.profiles.get_mut(name)
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Match an observation against every profile. Returns `(name, distance)`
    /// for the closest profile whose distance falls below `threshold`, or
    /// `None` when the observation has no usable features or no profile is
    /// within range.
    pub fn match_observation(
        &self,
        obs: &MatchObservation,
        threshold: f32,
    ) -> Option<(&str, f32)> {
        if obs.is_empty() || self.profiles.is_empty() {
            return None;
        }
        let mut best: Option<(&str, f32)> = None;
        for (name, profile) in &self.profiles {
            let d = distance(profile, obs);
            if d.is_finite() && d <= threshold {
                match best {
                    Some((_, best_d)) if best_d <= d => {}
                    _ => best = Some((name.as_str(), d)),
                }
            }
        }
        best
    }

    /// Self-check: pairwise distance between every pair of profiles using
    /// each as a synthetic observation for the other. Useful for spotting
    /// two profiles that are too close to discriminate reliably.
    pub fn pairwise_separations(&self) -> Vec<((String, String), f32)> {
        let names: Vec<&String> = self.profiles.keys().collect();
        let mut out = Vec::with_capacity(names.len().saturating_sub(1));
        for i in 0..names.len() {
            for j in (i + 1)..names.len() {
                let a = &self.profiles[names[i]];
                let b = &self.profiles[names[j]];
                let obs = MatchObservation::from_vitals(
                    Some(b.hr_baseline_bpm),
                    Some(b.br_baseline_bpm),
                );
                let d = distance(a, &obs);
                out.push(((names[i].clone(), names[j].clone()), d));
            }
        }
        out
    }

    fn path_for(&self, name: &str) -> PathBuf {
        // Sanitise: replace path separators / whitespace just in case.
        let safe: String = name
            .chars()
            .map(|c| match c {
                '/' | '\\' | ':' | ' ' | '\t' => '_',
                other => other,
            })
            .collect();
        self.root.join(format!("{safe}.json"))
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn alice() -> EnrolledProfile {
        EnrolledProfile::from_hr_br("alice", 72.0, 3.0, 14.5, 1.2, 60)
    }

    fn bob() -> EnrolledProfile {
        EnrolledProfile::from_hr_br("bob", 64.0, 3.5, 12.0, 1.0, 60)
    }

    #[test]
    fn distance_zero_for_self() {
        let p = alice();
        let obs = MatchObservation::from_vitals(Some(p.hr_baseline_bpm), Some(p.br_baseline_bpm));
        assert!(distance(&p, &obs).abs() < 1e-5);
    }

    #[test]
    fn distance_infinite_for_empty_observation() {
        let d = distance(&alice(), &MatchObservation::default());
        assert!(d.is_infinite(), "empty observation should be unmatchable");
    }

    #[test]
    fn weight_redistributes_when_only_hr_present() {
        let p = alice();
        // Provide HR exactly on baseline, no BR — distance should still be 0.
        let obs = MatchObservation::from_vitals(Some(p.hr_baseline_bpm), None);
        assert!(distance(&p, &obs).abs() < 1e-5);
    }

    #[test]
    fn closest_profile_wins() {
        let mut store = ProfileStore::new(PathBuf::from("/tmp/__unused__"));
        store.upsert(alice());
        store.upsert(bob());

        // Observation right at alice's baseline.
        let obs = MatchObservation::from_vitals(Some(72.0), Some(14.5));
        let (name, d) = store.match_observation(&obs, 5.0).expect("should match");
        assert_eq!(name, "alice");
        assert!(d < 0.1);
    }

    #[test]
    fn no_match_above_threshold() {
        let mut store = ProfileStore::new(PathBuf::from("/tmp/__unused__"));
        store.upsert(alice());
        // Observation 20 bpm away from alice's baseline => >>1 z-score.
        let obs = MatchObservation::from_vitals(Some(92.0), Some(20.0));
        assert!(store.match_observation(&obs, DEFAULT_MATCH_THRESHOLD).is_none());
    }

    #[test]
    fn min_std_floor_applied() {
        // A profile with zero std should still produce finite distances.
        let p = EnrolledProfile::from_hr_br("flat", 72.0, 0.0, 14.0, 0.0, 1);
        assert!(p.hr_std_bpm >= MIN_STD_BPM);
        assert!(p.br_std_bpm >= MIN_STD_BPM);
        let obs = MatchObservation::from_vitals(Some(72.5), Some(14.5));
        assert!(distance(&p, &obs).is_finite());
    }

    #[test]
    fn ema_update_drifts_toward_observation() {
        let mut p = alice();
        let original = p.hr_baseline_bpm;
        let obs = MatchObservation::from_vitals(Some(80.0), Some(15.5));
        p.update_from_observation(&obs, 0.5);
        assert!(p.hr_baseline_bpm > original);
        assert!(p.hr_baseline_bpm < 80.0); // didn't overshoot
        assert_eq!(p.sample_count, 61);
    }

    #[test]
    fn pairwise_separations_reports_alice_vs_bob() {
        let mut store = ProfileStore::new(PathBuf::from("/tmp/__unused__"));
        store.upsert(alice());
        store.upsert(bob());
        let pairs = store.pairwise_separations();
        assert_eq!(pairs.len(), 1);
        let (_, d) = &pairs[0];
        // Alice and bob differ by ~2.5 std HR + ~2.5 std BR -> distance ~2.5
        assert!(*d > 1.0, "expected meaningful separation, got {d}");
    }

    #[test]
    fn save_load_roundtrip() {
        let dir = std::env::temp_dir().join("ruview_profile_test_roundtrip");
        let _ = fs::remove_dir_all(&dir);

        let store = {
            let mut s = ProfileStore::new(&dir);
            s.upsert(alice());
            s.upsert(bob());
            s.save_all().expect("save");
            s
        };

        let loaded = ProfileStore::load(&dir).expect("load");
        assert_eq!(loaded.len(), store.len());
        assert!(loaded.get("alice").is_some());
        assert!(loaded.get("bob").is_some());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn delete_removes_disk_file() {
        let dir = std::env::temp_dir().join("ruview_profile_test_delete");
        let _ = fs::remove_dir_all(&dir);

        let mut s = ProfileStore::new(&dir);
        s.upsert(alice());
        s.save_all().expect("save");
        assert!(dir.join("alice.json").exists());

        let removed = s.delete("alice").expect("delete");
        assert!(removed);
        assert!(!dir.join("alice.json").exists());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn load_missing_dir_yields_empty_store() {
        let dir = std::env::temp_dir().join("ruview_profile_test_no_such_dir__");
        let _ = fs::remove_dir_all(&dir);
        let store = ProfileStore::load(&dir).expect("load");
        assert!(store.is_empty());
    }

    #[test]
    fn path_sanitisation_blocks_separators() {
        let store = ProfileStore::new(PathBuf::from("/tmp/__unused__"));
        let path = store.path_for("../weird name");
        assert_eq!(path.file_name().unwrap().to_str().unwrap(), ".._weird_name.json");
    }
}
