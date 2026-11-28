use std::collections::{HashMap, HashSet};
use std::path::{Path, PathBuf};

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
    #[serde(default)]
    pub mixed_parents: HashMap<String, Vec<PathBuf>>,
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

    pub fn for_parent(
        &self,
        directory: &Directory,
        parent: &str,
        child_cwd: Option<&str>,
    ) -> Option<&Selection> {
        let mut record = &directory.named(parent.trim())?.record;
        let mut cwd = child_cwd;
        let mut seen = HashSet::new();
        loop {
            if record.is_test || record.bound() || !seen.insert(&record.agent_id) {
                return None;
            }
            if let Some(roots) = self.mixed_parents.get(&record.agent_id) {
                let path = Path::new(cwd?).canonicalize().ok()?;
                return roots
                    .iter()
                    .any(|root| root.is_absolute() && path.starts_with(root))
                    .then_some(&self.selection);
            }
            if self.parent_ids.contains(&record.agent_id) {
                return Some(&self.selection);
            }
            if !record.is_helper() {
                return None;
            }
            cwd = Some(&record.cwd);
            record = &directory.by_id(record.parent_agent_id.as_deref()?)?.record;
        }
    }
}
