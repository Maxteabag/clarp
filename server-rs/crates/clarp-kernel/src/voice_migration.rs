//! The reviewed voice migration. Every comparison uses the speaker's resolver.
use std::collections::BTreeMap;

use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};

use crate::said::Who;
use crate::voice_profile::{Agent, HostView};
use crate::{languages, python, said, voice_profile};

fn text<'a>(row: &'a Value, field: &str) -> &'a str {
    row[field].as_str().unwrap_or("")
}

fn who(row: &Value) -> Who<'_> {
    let name = text(row, "name");
    Who {
        agent_id: text(row, "agent_id"),
        name,
        persona: name,
        session: text(row, "session"),
    }
}

fn only(view: &HostView, provider: &str) -> HostView {
    let mut view = view.clone();
    view.voices.provider = provider.to_owned();
    view.voices.fallback = "none".into();
    view.voices.agents.clear();
    view
}

fn heard(view: &HostView, agent: Agent<'_>) -> Value {
    let speech = voice_profile::speech(view, agent);
    json!({"provider": [speech.provider, speech.voice_id, speech.silent_reason]})
}

fn transcription(view: &HostView, agents: &[Value]) -> Value {
    let mut out =
        json!({"engines": {}, "languages": {}, "shared_keys": [], "agents_with_several_keys": []});
    let mut shared = Vec::new();
    let mut several = Vec::new();
    for (setting, entries) in [
        ("engines", &view.agent_engines),
        ("languages", &view.agent_languages),
    ] {
        let mut holders: BTreeMap<String, Vec<&str>> = BTreeMap::new();
        let mut changes = Map::new();
        for row in agents {
            let who = who(row);
            let mut keys = Vec::new();
            let mut values = std::collections::BTreeSet::new();
            for alias in [who.persona, who.session] {
                let key = python::strip(alias).to_lowercase();
                if !key.is_empty()
                    && let Some((_, value)) = entries.iter().find(|(name, _)| *name == key)
                {
                    holders.entry(key).or_default().push(who.agent_id);
                    keys.push(alias);
                    values.insert(value);
                }
            }
            if values.len() > 1 {
                several.push(json!({"setting": setting, "agent_id": who.agent_id, "keys": keys}));
            }
            if let Some(value) = languages::of_agent(entries, who)
                && !entries
                    .iter()
                    .any(|(key, _)| *key == who.agent_id.to_lowercase())
            {
                changes.insert(who.agent_id.to_owned(), json!(value));
            }
        }
        shared.extend(
            holders
                .into_iter()
                .filter(|(_, ids)| ids.len() > 1)
                .map(|(key, agents)| json!({"setting": setting, "key": key, "agents": agents})),
        );
        out[setting] = Value::Object(changes);
    }
    out["shared_keys"] = json!(shared);
    out["agents_with_several_keys"] = json!(several);
    out
}

/// A report for the current roster and profile view. None of the comparisons calls a provider.
pub fn plan(view: &HostView, agents: &[Value], contacts: &[Value]) -> Value {
    let fallback = &view.voices.fallback;
    let fallback = (!matches!(fallback.as_str(), "" | "none") && fallback != &view.voices.provider)
        .then(|| only(view, fallback));
    let per_provider: Vec<_> = voice_profile::BUILT
        .into_iter()
        .map(|provider| {
            let mut alone = only(view, provider);
            alone.keys = vec![provider.into()];
            alone.google_refusal = None;
            (provider, alone)
        })
        .collect();
    let mut voices = Vec::new();
    for (kind, rows) in [("agent", agents), ("contact", contacts)] {
        for row in rows {
            let old = text(row, "voice_id");
            let new = said::canonical_voice(old);
            if new == old {
                continue;
            }
            let who = who(row);
            let before = Agent {
                who,
                stored: Some(old),
            };
            let moved = Agent {
                who,
                stored: Some(&new),
            };
            let with_route = |agent| {
                let mut heard = heard(view, agent);
                if let Some(fallback) = &fallback {
                    heard["fallback"] = self::heard(fallback, agent)["provider"].clone();
                }
                heard
            };
            let today = with_route(before);
            let after = with_route(moved);
            let mut differences = Map::new();
            for (provider, alone) in &per_provider {
                let sound = |agent| {
                    let speech = voice_profile::speech(alone, agent);
                    json!([speech.voice_id, speech.silent_reason])
                };
                let old = sound(before);
                let new = sound(moved);
                if old != new {
                    differences.insert((*provider).into(), json!({"today": old, "after": new}));
                }
            }
            let mut item = json!({
                "kind":kind, "name":who.persona, "old":old, "new":new,
                "today":today, "after":after, "per_provider":differences,
                "audible": today != after || !differences.is_empty()
            });
            if kind == "agent" {
                item["agent_id"] = json!(who.agent_id);
                item["session"] = json!(who.session);
            } else {
                item["persona_id"] = row["id"].clone();
                item["builtin"] = json!(row["builtin"] == true);
            }
            voices.push(item);
        }
    }
    let mut report = json!({
        "voices":voices, "transcription":transcription(view, agents),
        "provider":view.voices.provider, "fallback":view.voices.fallback
    });
    let hash = format!(
        "{:x}",
        Sha256::digest(python::sorted_ascii(&report).as_bytes())
    );
    report["digest"] = json!(&hash[..16]);
    report
}
