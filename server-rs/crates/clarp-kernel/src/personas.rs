//! Who an agent can be: the personas a Host offers.
//!
//! A persona is a name with its voices, a way of speaking, the kind of tool it runs on and a
//! face. A Host has many built in: the names of its roster, each with the voices the Host's
//! own lists or the owner's file give it. An app reads them when an agent is made
//! (`personas` in `GET /agents/snapshot`).
//!
//! The lists are the Python Host's, carried as data (`persona-lists.json`), and the rules by
//! which the owner's file adds to them are its own (`server/lib/config.py`, `personas.py`,
//! `roster.py`). Both are held to what that Host lists under several made-up owner's files
//! (`tests/fixtures/persona-vectors.json`, written by `server-rs/tools/persona-vectors.py`).
//!
//! One thing is not copied: the order. The Python Host lists its built-in personas by the
//! millisecond each was first written and, within one, by their ids, which is an accident
//! of its start. Here they are in the order of the roster, the owner's own names after it.

use std::collections::HashMap;
use std::sync::LazyLock;

use serde_json::{Value, json};

use crate::python;

fn lists() -> &'static Value {
    static LISTS: LazyLock<Value> = LazyLock::new(|| {
        serde_json::from_str(include_str!("persona-lists.json")).unwrap_or(Value::Null)
    });
    &LISTS
}

fn listed(name: &str, key: &str) -> Option<&'static str> {
    lists()[name][key].as_str()
}

/// What the owner's file names, each a table of texts in the file's order: voices of
/// ElevenLabs for names (`[roster]`, which also makes a name a persona), of Cartesia
/// (`[cartesia.voices]`) and of Gemini (`[gemini_tts.voices]`).
#[derive(Debug, Clone, Copy, Default)]
pub struct Owner<'a> {
    pub roster: &'a [(String, String)],
    pub cartesia: &'a [(String, String)],
    pub gemini: &'a [(String, String)],
}

/// A name without the letters' case, as two spellings of one name are compared.
fn folded(name: &str) -> String {
    name.to_lowercase()
}

/// A name without the four hex signs an agent nobody named has at its end (`Claude-5628`):
/// such an agent is its archetype with a number.
pub fn archetype(name: &str) -> &str {
    let Some((before, end)) = name.rsplit_once('-') else {
        return name;
    };
    let hex = end.len() == 4
        && end
            .bytes()
            .all(|sign| matches!(sign, b'0'..=b'9' | b'a'..=b'f'));
    if hex { before } else { name }
}

/// The kind of tool a name belongs to (`claude`, `codex`, `grok`, `gemini`, `deepseek`,
/// `janitor`), or none: by the name itself, or, for a name with one sign or a number after
/// it (`Rex 2`), by the name before that when it is one that is given out in turn.
pub fn tier(name: &str) -> &'static str {
    let name = folded(clarp_speech::listen::strip(name));
    if name.is_empty() {
        return "";
    }
    if let Some(tier) = listed("tiers", &name).or_else(|| listed("minted", &name)) {
        return tier;
    }
    let parts: Vec<&str> = name
        .split(clarp_speech::listen::is_space)
        .filter(|part| !part.is_empty())
        .collect();
    match parts.as_slice() {
        [first, second] if second.chars().count() == 1 || second.chars().all(char::is_numeric) => {
            listed("minted", first).unwrap_or("")
        }
        _ => "",
    }
}

/// How a persona speaks: the clause its agent is told, or nothing.
pub fn personality(name: &str) -> &'static str {
    let name = clarp_speech::listen::strip(name);
    if name.is_empty() {
        return "";
    }
    if let Some(text) = listed("personalities", name) {
        return text;
    }
    let lower = name.to_lowercase();
    lists()["personalities"]
        .as_object()
        .into_iter()
        .flatten()
        .find(|(known, _)| known.to_lowercase() == lower)
        .and_then(|(_, text)| text.as_str())
        .unwrap_or("")
}

/// The personas a Host has built in, as an app is told them: the names of the roster, the
/// owner's own after them, each with its voices.
pub fn builtins(owner: &Owner<'_>) -> Vec<Value> {
    // The roster: the Host's names, with the owner's voice where the file names one, and
    // the owner's own names after them.
    let mut roster: Vec<(String, String)> = lists()["roster"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|pair| Some((pair[0].as_str()?.to_string(), pair[1].as_str()?.to_string())))
        .collect();
    for (name, voice) in owner.roster {
        match roster.iter_mut().find(|(known, _)| known == name) {
            Some(held) => held.1.clone_from(voice),
            None => roster.push((name.clone(), voice.clone())),
        }
    }
    // Cartesia's voices are looked up by the name as it is written; Gemini's without the
    // case of its letters, and what is empty is no voice.
    let mut cartesia: HashMap<&str, &str> = lists()["cartesia"]
        .as_object()
        .into_iter()
        .flatten()
        .filter_map(|(name, voice)| Some((name.as_str(), voice.as_str()?)))
        .collect();
    for (name, voice) in owner.cartesia {
        cartesia.insert(name, voice);
    }
    let mut gemini: HashMap<String, &str> = HashMap::new();
    for (name, voice) in owner.gemini {
        let voice = clarp_speech::listen::strip(voice);
        if !voice.is_empty() {
            gemini.insert(folded(name), voice);
        }
    }
    let voice_of = |voices: &HashMap<&str, &str>, name: &str| -> Option<String> {
        let direct = voices.get(name).copied().filter(|voice| !voice.is_empty());
        let named = archetype(name);
        let of_kind = (named != name)
            .then(|| voices.get(named).copied())
            .flatten();
        direct
            .or(of_kind.filter(|voice| !voice.is_empty()))
            .map(str::to_string)
    };
    roster
        .iter()
        .map(|(name, eleven)| {
            let mut voices = json!({"elevenlabs": eleven, "cartesia": voice_of(&cartesia, name)});
            let spoken = gemini
                .get(&folded(name))
                .or_else(|| gemini.get(&folded(archetype(name))));
            if let Some(voice) = spoken {
                voices["gemini"] = json!(voice);
            }
            json!({
                "id": format!("builtin-{}", folded(name)),
                "name": name,
                "voice_id": python::compact_ascii(&voices),
                "avatar_symbol": "",
                "avatar_url": "",
                "avatar_generation_status": null,
                "personality": personality(name),
                "tier": tier(name),
                "builtin": true,
            })
        })
        .collect()
}
