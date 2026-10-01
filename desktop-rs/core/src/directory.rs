//! Contacts (personas not yet running) and the voice picker. Ports of
//! the C++ client's `ContactListModel` and `VoiceListModel`; both are
//! replaced wholesale on each response.

use std::collections::{HashMap, HashSet};

use serde_json::Value;

use crate::json::{self, Object};
use crate::text::name_order;

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Contact {
    pub id: String,
    pub name: String,
    pub description: String,
    pub avatar_symbol: String,
    pub avatar_url: String,
    pub builtin: bool,
}

/// Personas from `/agents/snapshot` that no active agent already uses.
/// `active_names` are case-folded persona names.
pub fn contacts_from_snapshot(snapshot: &Object, active_names: &HashSet<String>) -> Vec<Contact> {
    let mut contacts: Vec<Contact> = json::array(snapshot, "personas")
        .iter()
        .filter_map(Value::as_object)
        .filter_map(|object| {
            let name = json::string(object, "name");
            if name.is_empty() || active_names.contains(&name.to_lowercase()) {
                return None;
            }
            let personality = json::string(object, "personality");
            Some(Contact {
                id: json::string(object, "id"),
                description: personality.strip_prefix("Personality: ").unwrap_or(&personality).to_owned(),
                avatar_symbol: json::string(object, "avatar_symbol"),
                avatar_url: json::string(object, "avatar_url"),
                builtin: json::boolean(object, "builtin"),
                name,
            })
        })
        .collect();
    // sort_by is stable, like the C++ stable_sort.
    contacts.sort_by(|a, b| name_order(&a.name, &b.name));
    contacts
}

pub fn avatar_urls_by_name(contacts: &[Contact]) -> HashMap<String, String> {
    contacts
        .iter()
        .filter(|c| !c.avatar_url.is_empty())
        .map(|c| (c.name.clone(), c.avatar_url.clone()))
        .collect()
}

#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Voice {
    pub id: String,
    pub label: String,
    /// Who else uses this voice; empty for the current agent's own voice.
    pub taken_by: String,
    pub current: bool,
}

pub fn voices_from_response(response: &Object, current_voice_id: &str) -> Vec<Voice> {
    json::array(response, "voices")
        .iter()
        .filter_map(Value::as_object)
        .filter_map(|object| {
            let id = json::string(object, "id");
            if id.is_empty() {
                return None;
            }
            let current = id == current_voice_id;
            Some(Voice {
                label: object.get("label").and_then(Value::as_str).unwrap_or(&id).to_owned(),
                taken_by: if current { String::new() } else { json::string(object, "taken_by") },
                current,
                id,
            })
        })
        .collect()
}
