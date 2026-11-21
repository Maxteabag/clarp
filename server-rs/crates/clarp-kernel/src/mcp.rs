//! MCP selections at agent creation and in roster edits.

use std::collections::HashSet;

use serde_json::{Map, Value};

pub fn selection(raw: Option<&Value>) -> Result<Option<Vec<String>>, &'static str> {
    let Some(raw) = raw else {
        return Ok(None);
    };
    let invalid = "mcp_servers must be a list of names";
    let mut seen = HashSet::new();
    let mut names = Vec::new();
    for item in raw.as_array().ok_or(invalid)? {
        let name = crate::python::strip(item.as_str().ok_or(invalid)?);
        if !name.is_empty() && seen.insert(name) {
            names.push(name.to_owned());
        }
    }
    Ok(Some(names))
}

/// An edit keeps the known names in request order. The Python route stringifies each
/// item, then strips it before looking it up.
pub fn edited_selection(requested: &[Value], catalog: &Map<String, Value>) -> Vec<String> {
    let mut seen = HashSet::new();
    requested
        .iter()
        .filter_map(|item| {
            let name = match item {
                Value::String(name) => crate::python::strip(name).to_owned(),
                other => crate::python::repr(other),
            };
            (catalog.contains_key(&name) && seen.insert(name.clone())).then_some(name)
        })
        .collect()
}

pub fn check(
    names: &[String],
    supports_mcp: bool,
    label: &str,
    catalog: &Map<String, Value>,
) -> Result<(), (&'static str, String)> {
    if names.is_empty() {
        return Ok(());
    }
    if !supports_mcp {
        return Err((
            "mcp servers unsupported for backend",
            format!("MCP server selection is unavailable for {label}."),
        ));
    }
    let unknown: Vec<_> = names
        .iter()
        .filter(|name| !catalog.contains_key(*name))
        .map(String::as_str)
        .collect();
    if !unknown.is_empty() {
        return Err((
            "unknown mcp server",
            format!("Unknown MCP server: {}", unknown.join(", ")),
        ));
    }
    Ok(())
}
