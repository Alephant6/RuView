//! Read-only enrolled-profile loader for the sensing-server runtime.
//!
//! This module deserialises the same JSON schema that
//! `wifi_densepose_mat::tracking::profile::EnrolledProfile` writes from the
//! `wifi-densepose` CLI (`enroll` subcommand). It deliberately re-implements
//! the minimal struct + matching logic here rather than pulling in the full
//! `wifi-densepose-mat` crate, because mat brings in the ONNX runtime through
//! `wifi-densepose-nn`, which would force the sensing-server build to depend
//! on a heavyweight transitive that isn't needed for this feature.
//!
//! The on-disk JSON file is the contract; either side may add fields as long
//! as `#[serde(default)]` makes them tolerant on read.

use std::collections::HashMap;
use std::fs;
use std::io;
use std::path::{Path, PathBuf};

use serde::{Deserialize, Serialize};

/// Weight for the heart-rate term in the matching distance.
const W_HR: f32 = 0.5;
/// Weight for the breathing-rate term in the matching distance.
const W_BR: f32 = 0.5;
/// Floor used in z-score denominators to avoid division by zero.
const MIN_STD_BPM: f32 = 0.5;

/// Default cutoff above which a candidate match is rejected.
///
/// **Must match `wifi_densepose_mat::tracking::profile::DEFAULT_MATCH_THRESHOLD`.**
pub const DEFAULT_MATCH_THRESHOLD: f32 = 1.5;

// ---------------------------------------------------------------------------
// EnrolledProfile (mirror of the mat-crate struct)
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EnrolledProfile {
    pub name: String,
    pub hr_baseline_bpm: f32,
    pub hr_std_bpm: f32,
    pub br_baseline_bpm: f32,
    pub br_std_bpm: f32,
    #[serde(default)]
    pub sample_count: u32,
    // Reserved / step-B fields. Tolerated but unused by this matcher.
    #[serde(default)]
    pub embedding_mean: Option<Vec<f32>>,
    #[serde(default)]
    pub embedding_std: Option<Vec<f32>>,
    #[serde(default)]
    pub height_m: Option<f32>,
    // Timestamps left as raw strings — the sensing-server doesn't care about
    // their semantics, only that they round-trip if we ever rewrite a profile.
    #[serde(default)]
    pub enrolled_at: Option<String>,
    #[serde(default)]
    pub last_updated_at: Option<String>,
}

// ---------------------------------------------------------------------------
// MatchObservation
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default)]
pub struct MatchObservation {
    pub hr_bpm: Option<f32>,
    pub br_bpm: Option<f32>,
}

impl MatchObservation {
    pub fn is_empty(&self) -> bool {
        self.hr_bpm.is_none() && self.br_bpm.is_none()
    }
}

// ---------------------------------------------------------------------------
// Distance metric (mirror of mat-crate logic)
// ---------------------------------------------------------------------------

pub fn distance(profile: &EnrolledProfile, obs: &MatchObservation) -> f32 {
    let mut num = 0.0_f32;
    let mut wsum = 0.0_f32;

    if let Some(hr) = obs.hr_bpm {
        let z = (hr - profile.hr_baseline_bpm).abs() / profile.hr_std_bpm.max(MIN_STD_BPM);
        num += W_HR * z;
        wsum += W_HR;
    }
    if let Some(br) = obs.br_bpm {
        let z = (br - profile.br_baseline_bpm).abs() / profile.br_std_bpm.max(MIN_STD_BPM);
        num += W_BR * z;
        wsum += W_BR;
    }

    if wsum <= 1e-6 {
        return f32::INFINITY;
    }
    num / wsum
}

// ---------------------------------------------------------------------------
// ProfileStore
// ---------------------------------------------------------------------------

#[derive(Debug, Clone, Default)]
pub struct ProfileStore {
    profiles: HashMap<String, EnrolledProfile>,
    root: PathBuf,
}

impl ProfileStore {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        Self {
            profiles: HashMap::new(),
            root: root.into(),
        }
    }

    /// Load every `*.json` file under `root` into memory.
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

    pub fn len(&self) -> usize {
        self.profiles.len()
    }

    pub fn is_empty(&self) -> bool {
        self.profiles.is_empty()
    }

    pub fn names(&self) -> Vec<&str> {
        let mut out: Vec<&str> = self.profiles.keys().map(String::as_str).collect();
        out.sort_unstable();
        out
    }

    pub fn get(&self, name: &str) -> Option<&EnrolledProfile> {
        self.profiles.get(name)
    }

    pub fn root(&self) -> &Path {
        &self.root
    }

    /// Match an observation against every profile. Returns `(name, distance)`
    /// for the closest profile whose distance is below `threshold`.
    pub fn match_observation(
        &self,
        obs: &MatchObservation,
        threshold: f32,
    ) -> Option<(String, f32)> {
        if obs.is_empty() || self.profiles.is_empty() {
            return None;
        }
        let mut best: Option<(String, f32)> = None;
        for (name, profile) in &self.profiles {
            let d = distance(profile, obs);
            if d.is_finite() && d <= threshold {
                let take = match &best {
                    Some((_, best_d)) => d < *best_d,
                    None => true,
                };
                if take {
                    best = Some((name.clone(), d));
                }
            }
        }
        best
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    fn alice() -> EnrolledProfile {
        EnrolledProfile {
            name: "alice".into(),
            hr_baseline_bpm: 72.0,
            hr_std_bpm: 3.0,
            br_baseline_bpm: 14.5,
            br_std_bpm: 1.2,
            sample_count: 60,
            embedding_mean: None,
            embedding_std: None,
            height_m: None,
            enrolled_at: None,
            last_updated_at: None,
        }
    }

    fn bob() -> EnrolledProfile {
        EnrolledProfile {
            name: "bob".into(),
            hr_baseline_bpm: 64.0,
            hr_std_bpm: 3.5,
            br_baseline_bpm: 12.0,
            br_std_bpm: 1.0,
            sample_count: 60,
            embedding_mean: None,
            embedding_std: None,
            height_m: None,
            enrolled_at: None,
            last_updated_at: None,
        }
    }

    #[test]
    fn distance_zero_for_self() {
        let p = alice();
        let obs = MatchObservation {
            hr_bpm: Some(p.hr_baseline_bpm),
            br_bpm: Some(p.br_baseline_bpm),
        };
        assert!(distance(&p, &obs).abs() < 1e-5);
    }

    #[test]
    fn empty_observation_unmatchable() {
        assert!(distance(&alice(), &MatchObservation::default()).is_infinite());
    }

    #[test]
    fn closest_profile_wins() {
        let mut s = ProfileStore::new(PathBuf::from("/tmp/__unused__"));
        s.profiles.insert("alice".into(), alice());
        s.profiles.insert("bob".into(), bob());
        let obs = MatchObservation {
            hr_bpm: Some(72.0),
            br_bpm: Some(14.5),
        };
        let (name, d) = s.match_observation(&obs, 5.0).expect("match");
        assert_eq!(name, "alice");
        assert!(d < 0.1);
    }

    #[test]
    fn no_match_above_threshold() {
        let mut s = ProfileStore::new(PathBuf::from("/tmp/__unused__"));
        s.profiles.insert("alice".into(), alice());
        let obs = MatchObservation {
            hr_bpm: Some(92.0),
            br_bpm: Some(20.0),
        };
        assert!(s.match_observation(&obs, DEFAULT_MATCH_THRESHOLD).is_none());
    }

    #[test]
    fn load_missing_dir_yields_empty_store() {
        let dir = std::env::temp_dir().join("ruview_profile_loader_no_such_dir__");
        let _ = fs::remove_dir_all(&dir);
        let s = ProfileStore::load(&dir).expect("load");
        assert!(s.is_empty());
    }

    #[test]
    fn save_load_roundtrip() {
        let dir = std::env::temp_dir().join("ruview_profile_loader_roundtrip");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();

        // Write alice.json in the shared schema.
        let alice_json = serde_json::to_vec_pretty(&alice()).unwrap();
        fs::write(dir.join("alice.json"), alice_json).unwrap();

        let loaded = ProfileStore::load(&dir).expect("load");
        assert_eq!(loaded.len(), 1);
        assert!(loaded.get("alice").is_some());

        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn ignores_non_json_files() {
        let dir = std::env::temp_dir().join("ruview_profile_loader_mixed");
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();

        fs::write(dir.join("alice.json"), serde_json::to_vec(&alice()).unwrap()).unwrap();
        fs::write(dir.join("readme.txt"), b"not a profile").unwrap();

        let loaded = ProfileStore::load(&dir).expect("load");
        assert_eq!(loaded.len(), 1);

        let _ = fs::remove_dir_all(&dir);
    }
}
