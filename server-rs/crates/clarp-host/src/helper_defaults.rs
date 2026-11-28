use std::collections::HashSet;
use std::path::Path;

use serde::{Deserialize, Serialize};

use crate::fleet::Directory;

#[derive(Clone, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) struct Selection {
    pub backend: String,
    pub model: String,
    pub effort: String,
}

#[derive(Clone, Debug, Deserialize, Eq, PartialEq)]
pub(crate) struct ScopedDefaults {
    pub parent_ids: Vec<String>,
    pub selection: Selection,
}

impl ScopedDefaults {
    pub fn read(config: Option<&Path>) -> Result<Option<Self>, String> {
        let Some(dir) = config else { return Ok(None) };
        let file = dir.join("helper-defaults.json");
        match std::fs::read(&file) {
            Ok(bytes) => serde_json::from_slice(&bytes)
                .map(Some)
                .map_err(|e| format!("{}: {e}", file.display())),
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => Ok(None),
            Err(e) => Err(format!("{}: {e}", file.display())),
        }
    }

    pub fn for_parent(&self, directory: &Directory, parent: &str) -> Option<&Selection> {
        let mut record = &directory.named(parent.trim())?.record;
        let mut seen = HashSet::new();
        loop {
            if record.is_test || record.bound() || !seen.insert(&record.agent_id) {
                return None;
            }
            if self.parent_ids.contains(&record.agent_id) {
                return Some(&self.selection);
            }
            if !record.is_helper() {
                return None;
            }
            record = &directory.by_id(record.parent_agent_id.as_deref()?)?.record;
        }
    }
}
