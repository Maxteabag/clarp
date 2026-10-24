//! The Hosts this desktop has connected to, so one can switch between them.
//! Kept in settings under `connection/hosts`, most recently used first, as
//! `[{"url": …, "name": …}]`. Device tokens stay in the keyring (per Host
//! URL); none is ever written here.

use serde_json::{Value, json};

use crate::settings::{Settings, normalized_base_url};

pub const KEY: &str = "connection/hosts";
/// The most kept; the least recently used goes first.
pub const MAX: usize = 20;

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct SavedHost {
    pub url: String,
    /// The Host's own name from `/server-info`; empty until it answered.
    pub name: String,
}

impl SavedHost {
    /// What a row shows: the Host's name, else its address.
    pub fn label(&self) -> String {
        if !self.name.is_empty() {
            return self.name.clone();
        }
        url::Url::parse(&self.url)
            .ok()
            .and_then(|u| u.host_str().map(|h| match u.port() {
                Some(port) => format!("{h}:{port}"),
                None => h.to_owned(),
            }))
            .unwrap_or_else(|| self.url.clone())
    }
}

/// The saved Hosts, most recent first; unreadable or repeated rows are left out.
pub fn list(settings: &Settings) -> Vec<SavedHost> {
    let mut hosts: Vec<SavedHost> = Vec::new();
    for item in settings.get(KEY).and_then(Value::as_array).into_iter().flatten() {
        let Some(raw) = item.get("url").and_then(Value::as_str).filter(|u| !u.trim().is_empty()) else { continue };
        let url = normalized_base_url(raw);
        if hosts.iter().any(|h| h.url == url) {
            continue;
        }
        let name = item.get("name").and_then(Value::as_str).unwrap_or("").trim().to_owned();
        hosts.push(SavedHost { url, name });
    }
    hosts
}

fn store(settings: &mut Settings, hosts: &[SavedHost]) {
    let rows: Vec<Value> = hosts.iter().map(|h| json!({"url": h.url, "name": h.name})).collect();
    settings.set(KEY, Value::Array(rows));
}

/// A Host that answered: it moves to the front under `name` (an empty name
/// keeps the one saved before). Nothing is written when nothing changed.
pub fn remember(settings: &mut Settings, url: &str, name: &str) {
    if url.trim().is_empty() {
        return;
    }
    let url = normalized_base_url(url);
    let before = list(settings);
    let mut rest = before.clone();
    let old = rest.iter().position(|h| h.url == url).map(|i| rest.remove(i));
    let name = match name.trim() {
        "" => old.map(|h| h.name).unwrap_or_default(),
        name => name.to_owned(),
    };
    let mut next = vec![SavedHost { url, name }];
    next.extend(rest);
    next.truncate(MAX);
    if next != before {
        store(settings, &next);
    }
}

/// Leaves `url` out of the list (its device token stays in the keyring).
pub fn forget(settings: &mut Settings, url: &str) {
    let url = normalized_base_url(url);
    let mut hosts = list(settings);
    let count = hosts.len();
    hosts.retain(|h| h.url != url);
    if hosts.len() != count {
        store(settings, &hosts);
    }
}
