//! Workspace suggestions from recent launches, zoxide and explicit path completion.
use crate::python;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::cmp::Reverse;
use std::collections::{BTreeMap, HashSet};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Usage {
    pub path: String,
    pub use_count: u64,
    pub first_used_at: i64,
    pub last_used_at: i64,
}

pub type Ledger = BTreeMap<String, Usage>;
pub type Owners = BTreeMap<String, (String, bool)>;

pub fn launched(ledger: &Ledger, path: &str, now: i64) -> Option<Usage> {
    let path = python::strip(path);
    if path.is_empty() {
        return None;
    }
    let old = ledger.get(path);
    Some(Usage {
        path: path.into(),
        use_count: old.map_or(1, |u| u.use_count + 1),
        first_used_at: old.map_or(now, |u| u.first_used_at),
        last_used_at: now,
    })
}

pub fn favorites(ledger: &Ledger, limit: &str) -> Value {
    let n = python::int(limit)
        .filter(|v| *v != 0)
        .unwrap_or(5)
        .clamp(1, 20) as usize;
    let mut rows: Vec<_> = ledger.values().collect();
    rows.sort_unstable_by_key(|u| (Reverse(u.use_count), Reverse(u.last_used_at), &u.path));
    rows.truncate(n);
    json!({"paths":rows})
}

pub fn recent(ledger: &Ledger, owners: &Owners) -> Vec<String> {
    let mut regular = HashSet::new();
    let mut janitor = HashSet::new();
    for (cwd, is_janitor) in owners.values() {
        if *is_janitor {
            janitor.insert(cwd);
        } else {
            regular.insert(cwd);
        }
    }
    let mut rows: Vec<_> = ledger
        .values()
        .filter(|u| !janitor.contains(&u.path) || regular.contains(&u.path))
        .collect();
    rows.sort_unstable_by_key(|u| (Reverse(u.last_used_at), &u.path));
    rows.into_iter().take(30).map(|u| u.path.clone()).collect()
}

pub fn query(raw: &str) -> &str {
    python::first(python::strip(raw), 512)
}

fn fuzzy(path: &str, query: &str) -> bool {
    let folded = python::casefold(path);
    let mut remaining = folded.chars();
    python::casefold(query)
        .chars()
        .filter(|c| *c != ' ')
        .all(|c| remaining.any(|p| p == c))
}

pub fn ranked(stdout: &str, query: &str, fallback: bool) -> Vec<String> {
    python::splitlines(stdout)
        .into_iter()
        .take(if fallback { 1000 } else { 100 })
        .filter(|p| !fallback || fuzzy(p, query))
        .take(100)
        .map(str::to_owned)
        .collect()
}

/// POSIX pathlib spelling retains ".." and a root of exactly two slashes.
pub fn spelling(value: &str) -> String {
    let prefix = if value.starts_with("//") && !value.starts_with("///") {
        "//"
    } else if value.starts_with('/') {
        "/"
    } else {
        ""
    };
    let rest = value
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
        .collect::<Vec<_>>()
        .join("/");
    if rest.is_empty() && prefix.is_empty() {
        ".".into()
    } else {
        format!("{prefix}{rest}")
    }
}

pub fn explicit_path(query: &str, home: &str) -> String {
    let path = if let Some(rest) = query.strip_prefix('~') {
        format!("{home}{rest}")
    } else if query.starts_with('/') {
        query.to_owned()
    } else {
        format!("{}/{query}", home.trim_end_matches('/'))
    };
    spelling(&path)
}

pub fn completions(mut children: Vec<String>, prefix: &str) -> Vec<String> {
    children.sort();
    let prefix = python::casefold(prefix);
    children
        .into_iter()
        .filter(|p| python::casefold(p).starts_with(&prefix))
        .take(30)
        .collect()
}

pub fn lookup(
    home: &str,
    q: &str,
    recent: Vec<String>,
    ranked: Vec<String>,
    explicit: Vec<String>,
    mut expand: impl FnMut(&str) -> Option<String>,
    mut is_dir: impl FnMut(&str) -> bool,
) -> Value {
    let mut candidates = Vec::new();
    if q.is_empty() {
        candidates.push(home.into());
        candidates.extend(recent);
        candidates.extend(ranked);
    } else {
        if q.starts_with(['~', '.']) || q.contains('/') {
            candidates.extend(explicit);
            candidates.extend(ranked);
        } else {
            candidates.extend(ranked);
            candidates.extend(explicit);
        }
        candidates.extend(recent.into_iter().filter(|p| fuzzy(p, q)));
    }
    let mut seen = HashSet::new();
    let mut matches = Vec::new();
    for candidate in candidates {
        let Some(path) = expand(&candidate) else {
            continue;
        };
        if seen.contains(&path) || !is_dir(&path) {
            continue;
        }
        seen.insert(path.clone());
        let label = if path == home || path.starts_with(&format!("{home}/")) {
            format!("~{}", &path[home.len()..])
        } else {
            path.clone()
        };
        matches.push(json!({"path":path,"label":label}));
        if matches.len() == 16 {
            break;
        }
    }
    json!({"home":home,"query":q,"matches":matches})
}
