//! The names of provider voices, kept independently of provider transport.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

use serde::Serialize;
use serde_json::Value;
use sha2::{Digest, Sha256};

use crate::said::{CARTESIA, ELEVENLABS};

pub type Names = BTreeMap<String, BTreeMap<String, String>>;
pub type Catalogue = BTreeMap<String, BTreeMap<String, CatalogueVoice>>;
pub type Gone = BTreeMap<String, BTreeSet<String>>;

#[derive(Debug, Default, Clone, PartialEq, Eq, Serialize)]
pub struct CatalogueVoice {
    pub language: String,
    pub public: bool,
    pub owner: bool,
}

impl CatalogueVoice {
    pub fn from_cartesia(row: &Value) -> Self {
        let label = |key| crate::python::strip(row[key].as_str().unwrap_or_default());
        Self {
            language: row["language"].as_str().unwrap_or_default().to_owned(),
            public: label("access").eq_ignore_ascii_case("public")
                || label("visibility").eq_ignore_ascii_case("all")
                || label("visibility").eq_ignore_ascii_case("public")
                || row["is_public"].as_bool() == Some(true),
            owner: row["is_owner"].as_bool() != Some(false),
        }
    }
}

#[derive(Debug, Default, Clone, PartialEq, Eq, Serialize)]
pub struct Snapshot {
    #[serde(flatten)]
    pub names: Names,
    #[serde(rename = "_format", skip_serializing_if = "Option::is_none")]
    pub format: Option<i64>,
    #[serde(rename = "_catalogue", skip_serializing_if = "BTreeMap::is_empty")]
    pub catalogue: Catalogue,
    #[serde(rename = "_gone", skip_serializing_if = "BTreeMap::is_empty")]
    pub gone: Gone,
    #[serde(rename = "_refused", skip_serializing_if = "BTreeMap::is_empty")]
    pub refused: BTreeMap<String, Refusal>,
}

pub const FIRST_AFTER: Duration = Duration::from_secs(60);
pub const REFRESH_EVERY: Duration = Duration::from_secs(24 * 3600);
pub const RETRY_AFTER: Duration = Duration::from_secs(15 * 60);
const BACKOFF: [Duration; 3] = [
    RETRY_AFTER,
    Duration::from_secs(60 * 60),
    Duration::from_secs(4 * 60 * 60),
];
pub const MAX_LOOKUPS: usize = 100;

const FORMAT: i64 = 2;

/// Startup refreshes an older format even if the file was written today.
pub fn start_due(age: Option<Duration>, format: Option<i64>) -> bool {
    age.is_none_or(|age| age >= REFRESH_EVERY) || format.is_some_and(|format| format < FORMAT)
}

#[derive(Debug, Clone, PartialEq, Eq, Serialize)]
pub struct Refusal {
    pub key: String,
    pub status: u16,
}

pub fn key_mark(key: &str) -> String {
    format!("{:x}", Sha256::digest(key.as_bytes()))[..12].to_owned()
}

/// A Cartesia status here is from a catalogue read. ElevenLabs only has by-ID reads.
pub fn refuses_key(provider: &str, status: u16) -> bool {
    matches!(
        (provider, status),
        (CARTESIA, 401 | 403) | (ELEVENLABS, 401)
    )
}

#[derive(Default)]
pub struct Backoff(usize);

impl Backoff {
    /// Return how long to wait and whether the next pass is a catalogue retry.
    pub fn next_pass(&mut self, due: bool) -> (Duration, bool) {
        if !due {
            self.0 = 0;
            return (REFRESH_EVERY, false);
        }
        let wait = BACKOFF.get(self.0).copied().unwrap_or(REFRESH_EVERY);
        self.0 = (self.0 + 1).min(BACKOFF.len());
        (wait, true)
    }
}

impl Snapshot {
    pub fn key_changed(&self, provider: &str, mark: &str) -> bool {
        self.refused
            .get(provider)
            .is_some_and(|old| old.key != mark)
    }

    pub fn key_refused(&self, provider: &str, mark: &str) -> bool {
        self.refused
            .get(provider)
            .is_some_and(|old| old.key == mark)
    }

    pub fn from_file(raw: &Value) -> Self {
        let mut names = Names::new();
        for (provider, rows) in raw.as_object().into_iter().flatten() {
            let Some(rows) = rows.as_object().filter(|_| !provider.starts_with('_')) else {
                continue;
            };
            let provider_names = rows
                .iter()
                .filter_map(|(id, name)| {
                    let name = name.as_str()?;
                    (!id.is_empty() && !crate::python::strip(name).is_empty())
                        .then(|| (id.clone(), name.to_string()))
                })
                .collect();
            names.insert(provider.clone(), provider_names);
        }
        let catalogue = raw["_catalogue"]
            .as_object()
            .into_iter()
            .flatten()
            .filter_map(|(provider, rows)| {
                let voices = rows.as_object()?.iter().filter_map(|(id, row)| {
                    (!id.is_empty() && row.is_object()).then(|| {
                        (
                            id.clone(),
                            CatalogueVoice {
                                language: row["language"].as_str().unwrap_or_default().to_owned(),
                                public: row["public"].as_bool() == Some(true),
                                owner: row["owner"].as_bool() != Some(false),
                            },
                        )
                    })
                });
                Some((provider.clone(), voices.collect()))
            })
            .collect();
        let gone = raw["_gone"]
            .as_object()
            .into_iter()
            .flatten()
            .filter_map(|(provider, ids)| {
                let ids = ids.as_array()?.iter().filter_map(Value::as_str);
                Some((
                    provider.clone(),
                    ids.filter(|id| !id.is_empty()).map(str::to_owned).collect(),
                ))
            })
            .collect();
        let refused = raw["_refused"]
            .as_object()
            .into_iter()
            .flatten()
            .filter_map(|(provider, mark)| {
                let key = mark["key"].as_str()?;
                let status = u16::try_from(mark["status"].as_u64()?).ok()?;
                (key.len() == 12
                    && key
                        .bytes()
                        .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
                    && refuses_key(provider, status))
                .then(|| {
                    (
                        provider.clone(),
                        Refusal {
                            key: key.to_owned(),
                            status,
                        },
                    )
                })
            })
            .collect();
        Self {
            names,
            format: Some(raw["_format"].as_i64().unwrap_or(1)),
            catalogue,
            gone,
            refused,
        }
    }

    /// Update the cache after a refresh. None means the file can stay untouched.
    pub fn refresh(
        &mut self,
        fetched: Self,
        has_key: bool,
        failed: bool,
    ) -> Option<Vec<(String, String)>> {
        let old_format = self.format.is_some_and(|format| format < FORMAT);
        if !has_key {
            if !old_format && self.format.is_some() {
                return None;
            }
            self.catalogue.clear();
            self.format = Some(FORMAT);
            return Some(Vec::new());
        }
        if fetched.names.is_empty() && fetched.refused.is_empty() && !old_format {
            return None;
        }
        if old_format {
            self.catalogue.clear();
        }
        if !failed {
            self.format = Some(FORMAT);
        }
        for provider in fetched.names.keys() {
            self.refused.remove(provider);
        }
        self.refused.extend(fetched.refused);
        merge(&mut self.names, fetched.names);
        for (provider, rows) in &fetched.catalogue {
            if let Some(gone) = self.gone.get_mut(provider) {
                gone.retain(|id| !rows.contains_key(id));
            }
        }
        self.catalogue.extend(fetched.catalogue);
        let mut newly_gone = Vec::new();
        for (provider, ids) in fetched.gone {
            let previous = self.gone.entry(provider.clone()).or_default();
            let names = self.names.entry(provider.clone()).or_default();
            for id in &ids {
                names.remove(id);
                if !previous.contains(id) {
                    newly_gone.push((provider.clone(), id.clone()));
                }
            }
            *previous = ids;
        }
        Some(newly_gone)
    }
}

/// Missing in-use IDs, in the provider-independent order of the lookup budget.
pub fn lookups<'a>(used: &'a BTreeSet<String>, listed: &BTreeMap<String, String>) -> Vec<&'a str> {
    used.iter()
        .filter(|id| !listed.contains_key(id.as_str()))
        .take(MAX_LOOKUPS)
        .map(String::as_str)
        .collect()
}

/// Merge provider responses by ID, preserving the old names of omitted or failed voices.
pub fn merge(current: &mut Names, fetched: Names) {
    for (provider, names) in fetched {
        current.entry(provider).or_default().extend(names);
    }
}
