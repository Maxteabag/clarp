//! The Host user's MCP catalog and the private subset handed to a Claude turn.

use std::fs::{self, OpenOptions};
use std::os::unix::fs::OpenOptionsExt as _;
use std::path::{Path, PathBuf};

use clarp_actor::ShimOrder;
use serde_json::{Map, Value, json};

use crate::fleet::Told;

fn catalog() -> Map<String, Value> {
    let file = crate::config::home().join(".claude.json");
    let Some(Value::Object(mut settings)) = fs::read(file)
        .ok()
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
    else {
        return Map::new();
    };
    match settings.remove("mcpServers") {
        Some(Value::Object(servers)) => servers,
        _ => Map::new(),
    }
}

pub(crate) fn available() -> Vec<String> {
    names(&catalog())
}

fn names(catalog: &Map<String, Value>) -> Vec<String> {
    let mut names: Vec<_> = catalog.keys().cloned().collect();
    names.sort_unstable();
    names
}

pub(crate) fn edit(requested: &[Value]) -> (Vec<String>, Vec<String>) {
    let catalog = catalog();
    (
        clarp_kernel::mcp::edited_selection(requested, &catalog),
        names(&catalog),
    )
}

pub(crate) fn check(backend: &str, names: Option<&[String]>) -> Result<(), Told> {
    let Some(names) = names.filter(|names| !names.is_empty()) else {
        return Ok(());
    };
    let declared = clarp_backend::catalogue::declared(backend);
    let shown = &declared["shown"];
    let supports = shown["supports_mcp"].as_bool().unwrap_or_default();
    let label = shown["label"].as_str().unwrap_or(backend);
    clarp_kernel::mcp::check(names, supports, label, &catalog())
        .map_err(|(code, detail)| Told::new(400, code, detail))
}

/// This file can hold credentials from the catalog. Its path is passed to Claude, and
/// the launch thread holds it for the lifetime of the attempt.
pub(crate) struct ScopedConfig(PathBuf);

impl Drop for ScopedConfig {
    fn drop(&mut self) {
        if let Err(error) = fs::remove_file(&self.0)
            && error.kind() != std::io::ErrorKind::NotFound
        {
            eprintln!("scopedMcpRemoveFail: {error}");
        }
    }
}

fn scoped(input: &Path, names: &[String]) -> std::io::Result<Option<ScopedConfig>> {
    // The file is read afresh: a server removed from the catalog can no longer be granted.
    let mut catalog = catalog();
    let chosen: Map<_, _> = names
        .iter()
        .filter_map(|name| catalog.remove(name).map(|server| (name.clone(), server)))
        .collect();
    if chosen.is_empty() {
        return Ok(None);
    }
    let path = input.with_extension(format!("mcp-{}.json", crate::ids::hex(6)));
    let file = OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&path)?;
    let kept = ScopedConfig(path);
    serde_json::to_writer(file, &json!({ "mcpServers": chosen }))?;
    Ok(Some(kept))
}

pub(crate) fn configure(
    order: &mut ShimOrder,
    strict: bool,
    names: Option<&[String]>,
) -> Option<ScopedConfig> {
    if !strict {
        order.args.retain(|arg| arg != "--strict-mcp-config");
        return None;
    }
    let names = names.filter(|names| !names.is_empty())?;
    match scoped(&order.stdin_file, names) {
        Ok(Some(kept)) => {
            order
                .args
                .extend(["--mcp-config".into(), kept.0.to_string_lossy().into_owned()]);
            Some(kept)
        }
        Ok(None) => None,
        Err(error) => {
            eprintln!("scopedMcpWriteFail: {error}");
            None
        }
    }
}
