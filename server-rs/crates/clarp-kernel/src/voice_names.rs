//! The names of provider voices, kept independently of provider transport.

use std::collections::{BTreeMap, BTreeSet};
use std::time::Duration;

use serde_json::Value;

pub type Names = BTreeMap<String, BTreeMap<String, String>>;

pub const FIRST_AFTER: Duration = Duration::from_secs(60);
pub const REFRESH_EVERY: Duration = Duration::from_secs(24 * 3600);
pub const MAX_LOOKUPS: usize = 100;

/// The startup refresh depends on the file's age; later passes run daily.
pub fn start_due(age: Option<Duration>) -> bool {
    age.is_none_or(|age| age >= REFRESH_EVERY)
}

pub fn from_file(raw: &Value) -> Names {
    let mut names = Names::new();
    for (provider, rows) in raw.as_object().into_iter().flatten() {
        let Some(rows) = rows.as_object() else {
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
    names
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
