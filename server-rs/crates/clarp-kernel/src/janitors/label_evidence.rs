//! Select the oldest unchanged working labels before reading any conversation.
use super::label_audit::Evidence;
use crate::{janitors::runs::redact, python};
use regex::{Captures, Regex};
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::sync::LazyLock;

#[derive(Debug, Deserialize, Serialize)]
pub struct Candidate {
    pub agent_id: String,
    pub name: String,
    pub busy: bool,
    pub excluded: bool,
    pub idle_since: i64,
    pub label: String,
    pub state: String,
    pub detail: String,
    pub state_at: i64,
    pub ended_at: Option<i64>,
    pub jobs: Vec<Value>,
    pub helpers: Vec<Value>,
}

pub fn selected(candidates: Vec<Candidate>, now: i64) -> Vec<Candidate> {
    let mut eligible: Vec<_> = candidates
        .into_iter()
        .filter(|c| {
            !c.excluded
                && !c.busy
                && now.saturating_sub(c.idle_since) >= 900_000
                && (!c.label.is_empty()
                    || !c.jobs.is_empty()
                    || !c.helpers.is_empty()
                    || c.state == "background")
        })
        .collect();
    eligible.sort_by_key(|c| c.idle_since);
    eligible.truncate(12);
    eligible
}

#[derive(Deserialize)]
struct Entities {
    named: std::collections::HashMap<String, String>,
    numeric: std::collections::HashMap<u32, String>,
}

#[allow(clippy::expect_used)]
fn unescape(text: &str) -> String {
    static TABLE: LazyLock<Entities> = LazyLock::new(|| {
        serde_json::from_str(include_str!("label-entities.json"))
            .expect("HTML character references")
    });
    static ENTITY: LazyLock<Regex> = LazyLock::new(|| {
        Regex::new(r"&(?:#[0-9]+;?|#[xX][0-9a-fA-F]+;?|[^\t\n\x0c <&#;]{1,32};?)")
            .expect("character reference pattern")
    });
    ENTITY
        .replace_all(text, |caps: &Captures<'_>| {
            let name = &caps[0][1..];
            if let Some(digits) = name.strip_prefix('#') {
                let digits = digits.trim_end_matches(';');
                let number = if let Some(hex) = digits.strip_prefix(['x', 'X']) {
                    u32::from_str_radix(hex, 16)
                } else {
                    digits.parse()
                };
                let n = number.unwrap_or(u32::MAX);
                if let Some(mapped) = TABLE.numeric.get(&n) {
                    return mapped.clone();
                }
                if let Some(c) = char::from_u32(n) {
                    if (0xfdd0..=0xfdef).contains(&n)
                        || n & 0xffff >= 0xfffe
                        || (1..=8).contains(&n)
                        || n == 11
                        || (14..=31).contains(&n)
                        || (127..=159).contains(&n)
                    {
                        return String::new();
                    }
                    return c.to_string();
                }
                return char::REPLACEMENT_CHARACTER.to_string();
            }
            for end in name
                .char_indices()
                .map(|(i, _)| i)
                .chain(std::iter::once(name.len()))
                .rev()
            {
                if let Some(found) = TABLE.named.get(&name[..end]) {
                    return format!("{found}{}", &name[end..]);
                }
            }
            caps[0].to_owned()
        })
        .into_owned()
}

#[allow(clippy::expect_used)]
fn clip(text: &str, chars: usize, words: usize) -> String {
    static TAG: LazyLock<Regex> = LazyLock::new(|| Regex::new("<[^>]*>").expect("tag pattern"));
    let clean = redact(&unescape(text));
    let plain = TAG.replace_all(&clean, " ");
    let mut tokens = plain.split(python::blank).filter(|s| !s.is_empty());
    let mut result = tokens.by_ref().take(words).collect::<Vec<_>>().join(" ");
    if tokens.next().is_some() {
        result.push('…');
    }
    if result.chars().count() > chars {
        let prefix = python::first(&result, chars - 1);
        result = format!(
            "{}…",
            prefix.rsplit_once(' ').map_or(prefix, |(left, _)| left)
        );
    }
    result
}

fn word<'a>(row: &'a Value, key: &str) -> &'a str {
    row[key].as_str().unwrap_or_default()
}

pub fn evidence(c: Candidate, now: i64, messages: &[Value]) -> Evidence {
    let minutes = |at: Option<i64>| {
        at.filter(|n| *n != 0)
            .map(|n| now.saturating_sub(n).max(0) / 60_000)
    };
    let jobs:Vec<_>=c.jobs.iter().take(3).map(|j|json!({
        "title":clip(word(j,"title"),120,20),"kind":word(j,"kind"),
        "progress":clip(&redact(word(j,"progress_text")),160,30),
        "progress_age_min":minutes(j["active_at"].as_i64()),"heartbeat_age_min":minutes(j["heartbeat_at"].as_i64())
    })).collect();
    let helpers:Vec<_>=c.helpers.iter().take(4).map(|h|json!({
        "name":h["session"],"activity":h["bucket"],"status":clip(word(h,"text"),120,20),"age_min":minutes(h["ts"].as_i64())
    })).collect();
    let recent: Vec<_> = messages[messages.len().saturating_sub(3)..]
        .iter()
        .map(|m| {
            json!({
                "role":word(m,"role"),"text":clip(&redact(word(m,"text")),300,50)
            })
        })
        .collect();
    Evidence {
        agent_id: c.agent_id,
        name: c.name,
        idle_min: minutes(Some(c.idle_since)),
        packet: json!({
            "label":if c.label.is_empty(){"Working in the background"}else{&c.label},
            "state":c.state,"state_detail":clip(&redact(&c.detail),160,30),"state_age_min":minutes(Some(c.state_at)),
            "turn_ended_min_ago":minutes(c.ended_at),"jobs":jobs,"helpers":helpers,"recent_messages":recent
        }),
    }
}
