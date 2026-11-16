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

use std::collections::{HashMap, HashSet};
use std::sync::LazyLock;

use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{portraits, python, said};

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

// ---- the personas the owner makes himself -------------------------------------------------

/// The longest name a persona can have.
pub const NAME_MOST: usize = 60;
/// The largest picture a persona can be given, in bytes.
pub const AVATAR_MOST: usize = 512_000;
/// The most of a symbol and of a personality that is kept.
pub const SYMBOL_MOST: usize = 64;
pub const PERSONALITY_MOST: usize = 4_000;
/// What a new persona without a name, or with too long a one, is refused with.
pub const NAME_NEEDED: &str = "A personality name is required (maximum 60 characters).";
/// The same when a persona is changed.
pub const NAME_NEEDED_TO_CHANGE: &str = "A Contact name is required (maximum 60 characters).";
pub const AVATAR_TOO_LARGE: &str = "Avatar is too large.";
pub const NOT_CHANGEABLE: &str = "Only saved custom Contacts can be edited.";
pub const NO_SUCH: &str = "No such Contact.";
/// What removing a persona that is built in, or that is not there, is refused with.
pub const NOT_REMOVABLE: &str = "personality not removable";
/// Where a persona's own picture is fetched from.
pub const AVATARS: &str = "/persona-avatars";

/// A persona the owner made, as it is kept.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Made {
    pub persona_id: String,
    pub name: String,
    /// Its voices: a text of JSON by engine, or the id of a voice of ElevenLabs alone.
    pub voice_id: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub avatar_symbol: String,
    /// What is in its own picture, when it has one: what the picture's address carries.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub avatar: Option<String>,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub personality: String,
    pub created_at_ms: i64,
}

/// What a name that is taken is refused with.
pub fn taken(name: &str) -> String {
    format!("{name} already exists.")
}

/// A persona's name as it is kept: without the space around it. `None`: nothing is left of
/// it, or too much.
pub fn named(said: &str) -> Option<String> {
    let name = clarp_speech::listen::strip(said);
    (!name.is_empty() && name.chars().count() <= NAME_MOST).then(|| name.to_string())
}

/// Whether two names are one persona: without the case of their letters.
pub fn same(a: &str, b: &str) -> bool {
    folded(a) == folded(b)
}

/// A persona the owner made, as an app is told it.
pub fn told(made: &Made) -> Value {
    let avatar_url = match &made.avatar {
        Some(version) => portraits::address(AVATARS, &made.persona_id, version),
        None => String::new(),
    };
    json!({
        "id": made.persona_id,
        "name": made.name,
        "voice_id": made.voice_id,
        "avatar_symbol": made.avatar_symbol,
        "avatar_url": avatar_url,
        // Nothing makes pictures here: none is ever on its way.
        "avatar_generation_status": null,
        "personality": made.personality,
        "tier": tier(&made.name),
        "builtin": false,
    })
}

/// The voice a persona is given when its maker names none: the first voice of Cartesia's
/// that no agent and no persona has; when every one is taken, the first that no agent has;
/// and when agents have them all, one by turns. `candidates` are the voices this Host knows
/// of, in order. With none to choose from, no voice (`{}`).
pub fn picked_voice(
    candidates: &[String],
    of_agents: &HashSet<String>,
    of_personas: &HashSet<String>,
) -> String {
    let free = |taken: &dyn Fn(&String) -> bool| candidates.iter().find(|voice| !taken(voice));
    let voice = free(&|voice| of_agents.contains(voice) || of_personas.contains(voice))
        .or_else(|| free(&|voice| of_agents.contains(voice)))
        .or_else(|| {
            let taken: HashSet<&String> = of_agents.union(of_personas).collect();
            candidates.get(taken.len() % candidates.len().max(1))
        });
    match voice {
        Some(voice) => python::compact_ascii(&json!({ "cartesia": voice })),
        None => "{}".to_string(),
    }
}

/// The voices of Cartesia's this Host knows of without asking anybody, in the order a voice
/// is picked from: Cartesia's own list as it was last fetched, then the Host's voices for
/// its names with the owner's in their places and the owner's own after them.
pub fn candidate_voices(fetched: &[String], owner: &Owner<'_>) -> Vec<String> {
    let mut named: Vec<(&str, &str)> = lists()["cartesia"]
        .as_object()
        .into_iter()
        .flatten()
        .filter_map(|(name, voice)| Some((name.as_str(), voice.as_str()?)))
        .collect();
    let own: Vec<&str> = named.iter().map(|(_, voice)| *voice).collect();
    for (name, voice) in owner.cartesia {
        match named.iter_mut().find(|(known, _)| *known == name.as_str()) {
            Some(held) => held.1 = voice,
            None => named.push((name, voice)),
        }
    }
    let of_names = named.iter().map(|(_, voice)| *voice);
    let mut seen = HashSet::new();
    let mut ordered = Vec::new();
    for voice in fetched
        .iter()
        .map(String::as_str)
        .chain(of_names)
        .chain(own)
    {
        let voice = clarp_speech::listen::strip(voice);
        if !voice.is_empty() && seen.insert(voice) {
            ordered.push(voice.to_string());
        }
    }
    ordered
}

/// The voice of Cartesia's a stored voice names, when it names one.
pub fn cartesia_of(voice_id: &str) -> Option<String> {
    said::resolve_voice(Some(voice_id), "cartesia")
}
