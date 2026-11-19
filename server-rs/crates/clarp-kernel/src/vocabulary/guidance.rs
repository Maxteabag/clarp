//! Computer-owned transcription guidance and the two previews shown by the settings route.
use crate::{python, vocabulary::estimate_tokens};
use serde_json::{Value, json};
use std::collections::HashSet;

pub const NAMES_KEY: &str = "transcription.guidance.delegation_names";
pub const GLOSSARY_KEY: &str = "transcription.guidance.glossary";
const TOKEN_LIMIT: usize = 223;
const CHAR_LIMIT: usize = 600;
const TRIM: [char; 4] = [' ', ',', ';', '\t'];

pub fn names_enabled(value: Option<&str>) -> bool {
    matches!(
        python::strip(value.unwrap_or("true"))
            .to_ascii_lowercase()
            .as_str(),
        "1" | "true" | "yes" | "on"
    )
}

pub fn clean(raw: &str) -> String {
    let lines = python::splitlines(raw)
        .into_iter()
        .map(|line| line.split('#').next().unwrap_or("").trim_matches(TRIM))
        .filter(|line| !line.is_empty())
        .collect::<Vec<_>>()
        .join("\n");
    python::first(&lines, 2000)
        .trim_end_matches(python::blank)
        .into()
}

pub struct Agent<'a> {
    pub persona: &'a str,
    pub created: i64,
    pub archived: bool,
    pub deleted: bool,
    pub janitor: bool,
}

pub fn active_names<'a>(agents: impl Iterator<Item = Agent<'a>>) -> Vec<String> {
    let mut agents: Vec<_> = agents
        .filter(|a| !a.archived && !a.deleted && !a.janitor)
        .collect();
    agents.sort_by_key(|a| a.created);
    let mut seen = HashSet::new();
    agents
        .into_iter()
        .map(|a| python::strip(a.persona).trim_matches(TRIM))
        .filter(|s| !s.is_empty() && seen.insert(python::casefold(s)))
        .map(str::to_owned)
        .collect()
}

pub fn update(enabled: bool, stored: &str, data: &Value) -> Result<(bool, String), &'static str> {
    let enabled = match data.get("delegation_agent_names_enabled") {
        Some(v) => v
            .as_bool()
            .ok_or("delegation_agent_names_enabled must be boolean")?,
        None => enabled,
    };
    let glossary = match data.get("technical_glossary") {
        None => clean(stored),
        Some(Value::String(s)) => clean(s),
        Some(v) if python::truthy(v) => clean(&python::repr(v)),
        Some(_) => String::new(),
    };
    Ok((enabled, glossary))
}

fn delegation(names: &[String]) -> String {
    let mut kept = String::new();
    for name in names.iter().rev() {
        if name.chars().take(CHAR_LIMIT + 1).count() > CHAR_LIMIT {
            break;
        }
        let joined = if kept.is_empty() {
            name.clone()
        } else {
            format!("{name}, {kept}")
        };
        let prompt = format!("Agent names: {joined}.");
        if prompt.chars().count() > CHAR_LIMIT || estimate_tokens(&prompt) > TOKEN_LIMIT {
            break;
        }
        kept = joined;
    }
    if kept.is_empty() {
        kept
    } else {
        format!("Agent names: {kept}.")
    }
}

fn regular(glossary: &str) -> String {
    let prefix = "Technical vocabulary: ";
    // The reference reserves one separator even with no delegation prompt and one final period.
    let glossary = python::splitlines(&clean(glossary)).join(" ");
    let mut text = python::first(&glossary, CHAR_LIMIT - prefix.len() - 2).trim_end_matches(TRIM);
    while !text.is_empty() {
        let prompt = format!("{prefix}{text}.");
        if estimate_tokens(&prompt) <= TOKEN_LIMIT {
            return prompt;
        }
        text = python::first(text, text.chars().count().saturating_sub(8)).trim_end_matches(TRIM);
    }
    String::new()
}

pub fn payload(enabled: bool, stored: &str, names: &[String]) -> Value {
    let glossary = clean(stored);
    let delegation = if enabled {
        delegation(names)
    } else {
        String::new()
    };
    let regular = regular(&glossary);
    json!({"settings":{"delegation_agent_names_enabled":enabled,"technical_glossary":glossary},
           "defaults":{"delegation_agent_names_enabled":true,"technical_glossary":""},
           "active_agent_names":names,"delegation_effective_prompt":delegation,
           "delegation_estimated_prompt_tokens":estimate_tokens(&delegation),"regular_effective_prompt":regular,
           "regular_estimated_prompt_tokens":estimate_tokens(&regular),"prompt_token_limit":TOKEN_LIMIT})
}
