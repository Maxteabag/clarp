//! Speech out: who says a line aloud, in which voice, and what its clip is called.
//!
//! An agent says something for the ear. Which engine speaks it is the owner's setting, for
//! every agent or for one; which voice is the agent's own for that engine, or its persona's
//! in the owner's lists. This file holds those rules, which are no engine's, and their
//! words are the Python Host's (`voice.py`, `tts_mode.py`, `tts_registry.py`, the voice
//! methods of `config.py`, `tts_engine.py`), held by `tests/voice_vectors.rs`.

use clarp_speech::listen::strip;
use serde_json::{Map, Value};

use crate::python;

pub const ELEVENLABS: &str = "elevenlabs";
pub const CARTESIA: &str = "cartesia";
pub const GEMINI: &str = "gemini";

/// What a value of an agent's stored voices says as a text: a text as it is, anything else
/// as Python shows it.
fn said(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => python::repr(other),
    }
}

/// Whether Python takes a value for something: not nothing, not empty, not zero.
fn something(value: &Value) -> bool {
    match value {
        Value::Null => false,
        Value::Bool(truth) => *truth,
        Value::Number(number) => number.as_f64().is_some_and(|number| number != 0.0),
        Value::String(text) => !text.is_empty(),
        Value::Array(list) => !list.is_empty(),
        Value::Object(fields) => !fields.is_empty(),
    }
}

/// The voices an agent has stored, by provider, in the order they are stored. An agent's
/// voice is one of two things: a plain id, which is ElevenLabs', or an object with a voice
/// for each provider.
pub fn voice_map(stored: Option<&str>) -> Vec<(String, String)> {
    let Some(stored) = stored.filter(|stored| !stored.is_empty()) else {
        return Vec::new();
    };
    let stored = strip(stored);
    if stored.starts_with('{')
        && let Ok(Value::Object(voices)) = serde_json::from_str::<Value>(stored)
    {
        let chosen = voices.iter().filter(|(_, voice)| something(voice));
        return chosen
            .map(|(provider, voice)| (provider.clone(), said(voice)))
            .collect();
    }
    vec![(ELEVENLABS.to_string(), stored.to_string())]
}

/// The voice an agent has stored for this provider, when it has one.
pub fn resolve_voice(stored: Option<&str>, provider: &str) -> Option<String> {
    let found = voice_map(stored)
        .into_iter()
        .rev()
        .find(|(known, _)| known == provider);
    found
        .map(|(_, voice)| voice)
        .filter(|voice| !voice.is_empty())
}

/// An agent's stored voices with one provider's voice set, or taken away when it is given
/// as nothing. What it has for the other providers stays.
pub fn merge_voice(stored: Option<&str>, provider: &str, voice: &str) -> String {
    let provider = strip(provider).to_lowercase();
    let voice = strip(voice);
    let mut voices = Map::new();
    for (known, id) in voice_map(stored) {
        voices.insert(known, Value::String(id));
    }
    if voice.is_empty() {
        voices.remove(&provider);
    } else {
        voices.insert(provider, Value::String(voice.to_string()));
    }
    python::sorted_ascii(&Value::Object(voices))
}

/// An agent as the owner's settings may name it.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub struct Who<'a> {
    pub name: &'a str,
    pub persona: &'a str,
    pub session: &'a str,
}

/// Which engine says an agent's clip, in which voice when the settings name one, and which
/// engine is tried when the first cannot begin.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Route {
    pub provider: String,
    /// The voice the agent's own entry names for that provider.
    pub voice: Option<String>,
    /// The engine that is tried after it. Empty: none.
    pub fallback: String,
}

/// The owner's settings of speech out, as far as they decide who says a clip and in which
/// voice.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Voices {
    /// The engine every agent speaks with, unless it has an entry of its own.
    pub provider: String,
    /// The engine that is tried when that one cannot begin. `none` or empty: none.
    pub fallback: String,
    /// The entries of single agents: under a name, a persona or a session in small letters,
    /// what the entry sets, its keys in small letters.
    pub agents: Vec<(String, Vec<(String, String)>)>,
    /// Gemini's voice for a persona, in the order the owner listed them.
    pub gemini_voices: Vec<(String, String)>,
    /// Cartesia's voice for a persona, in the order the owner listed them.
    pub cartesia_voices: Vec<(String, String)>,
    /// Gemini's voice for an agent that was moved to Gemini on purpose and has none.
    pub gemini_voice: String,
}

/// A persona without the mark of an agent nobody named (`Claude-5628` is a `Claude`).
fn archetype(persona: &str) -> &str {
    let Some((before, mark)) = persona.rsplit_once('-') else {
        return persona;
    };
    let hex = |sign: char| sign.is_ascii_digit() || ('a'..='f').contains(&sign);
    if mark.chars().count() == 4 && mark.chars().all(hex) {
        before
    } else {
        persona
    }
}

impl Voices {
    /// The entries of single agents as they are held once read: names and keys without the
    /// space around them and in small letters, values without the space around them, and
    /// what is set to nothing left out.
    pub fn with_agents(mut self, entries: &[(&str, &[(&str, &str)])]) -> Self {
        self.agents = entries
            .iter()
            .map(|(name, entry)| {
                let set = entry
                    .iter()
                    .filter(|(_, value)| !strip(value).is_empty())
                    .map(|(key, value)| (strip(key).to_lowercase(), strip(value).to_string()))
                    .collect();
                (strip(name).to_lowercase(), set)
            })
            .collect();
        self
    }

    /// The entry the settings have for this agent: by its name, then its persona, then its
    /// session. None: an empty one.
    pub fn entry_for(&self, who: Who<'_>) -> &[(String, String)] {
        for key in [who.name, who.persona, who.session] {
            let key = strip(key).to_lowercase();
            if key.is_empty() {
                continue;
            }
            // A name listed twice: the last one stands.
            if let Some((_, entry)) = self.agents.iter().rev().find(|(name, _)| *name == key) {
                return entry;
            }
        }
        &[]
    }

    /// Which engine says this agent's clip.
    pub fn route(&self, who: Who<'_>) -> Route {
        let entry = self.entry_for(who);
        let set = |key: &str| {
            let found = entry.iter().rev().find(|(known, _)| known == key);
            found.map(|(_, value)| value.clone())
        };
        let provider = set("provider").unwrap_or_else(|| self.provider.clone());
        let fallback = match self.fallback.as_str() {
            "" | "none" => String::new(),
            same if same == provider => String::new(),
            other => other.to_string(),
        };
        Route {
            voice: set(&format!("{provider}_voice")),
            provider,
            fallback,
        }
    }

    /// Gemini's voice for a persona, whatever letters it is written in. An agent nobody
    /// named speaks with the voice of what it is.
    pub fn gemini_voice_for(&self, persona: &str) -> Option<&str> {
        if persona.is_empty() {
            return None;
        }
        let listed = |name: &str| {
            let name = name.to_lowercase();
            let found = self.gemini_voices.iter().rev();
            let mut found = found.filter(|(known, _)| known.to_lowercase() == name);
            found.next().map(|(_, voice)| voice.as_str())
        };
        let own = listed(persona).filter(|voice| !voice.is_empty());
        own.or_else(|| listed(archetype(persona)).filter(|voice| !voice.is_empty()))
    }

    /// Cartesia's voice for a persona, as it is written. An agent nobody named speaks with
    /// the voice of what it is.
    pub fn cartesia_voice_for(&self, persona: &str) -> Option<&str> {
        if persona.is_empty() {
            return None;
        }
        let listed = |name: &str| {
            let found = self.cartesia_voices.iter().rev();
            let mut found = found.filter(|(known, _)| known == name);
            found.next().map(|(_, voice)| voice.as_str())
        };
        if let Some(own) = listed(persona).filter(|voice| !voice.is_empty()) {
            return Some(own);
        }
        let what = archetype(persona);
        (what != persona)
            .then(|| listed(what))
            .flatten()
            .filter(|voice| !voice.is_empty())
    }

    /// Gemini's voice of whoever owns this voice of Cartesia's, when somebody does.
    pub fn gemini_voice_for_cartesia(&self, cartesia_voice: &str) -> Option<&str> {
        if cartesia_voice.is_empty() {
            return None;
        }
        // Each persona once, as the owner's list holds it: listed twice, the last stands.
        let mut owners: Vec<(&str, &str)> = Vec::new();
        for (persona, voice) in &self.cartesia_voices {
            match owners.iter_mut().find(|(known, _)| known == persona) {
                Some(owner) => owner.1 = voice,
                None => owners.push((persona, voice)),
            }
        }
        let owners = owners.into_iter();
        let mut owned = owners.filter(|(_, voice)| *voice == cartesia_voice);
        owned.find_map(|(persona, _)| self.gemini_voice_for(persona))
    }

    /// Which of Gemini's voices one clip is spoken in: what the agent's entry names, what
    /// the agent has stored, what its persona has, what the owner of the Cartesia voice it
    /// speaks with has, and the voice for all only for an agent moved to Gemini on purpose.
    /// An agent without any stays silent, as it would with another engine.
    pub fn gemini_voice(
        &self,
        stored: Option<&str>,
        who: Who<'_>,
        route_voice: Option<&str>,
        cartesia_voice: Option<&str>,
    ) -> Option<String> {
        let named = route_voice.filter(|voice| !voice.is_empty());
        if let Some(voice) = named {
            return Some(voice.to_string());
        }
        if let Some(voice) = resolve_voice(stored, GEMINI) {
            return Some(voice);
        }
        if let Some(voice) = self.gemini_voice_for(who.persona) {
            return Some(voice.to_string());
        }
        if let Some(voice) = self.gemini_voice_for_cartesia(cartesia_voice.unwrap_or("")) {
            return Some(voice.to_string());
        }
        let entry = self.entry_for(who);
        let moved = entry
            .iter()
            .rev()
            .find(|(key, _)| key == "provider")
            .is_some_and(|(_, provider)| provider == GEMINI);
        (moved && !self.gemini_voice.is_empty()).then(|| self.gemini_voice.clone())
    }
}

/// What a clip's file is called: when it was made, and whose it is.
pub fn clip_name(session: Option<&str>, now_ms: i64) -> String {
    match session.filter(|session| !session.is_empty()) {
        Some(session) => {
            let plain = |sign: &char| sign.is_alphanumeric() || matches!(sign, '.' | '_' | '-');
            let safe: String = session.chars().filter(plain).collect();
            let safe = if safe.is_empty() { "anon" } else { &safe };
            format!("{now_ms}__{safe}.mp3")
        }
        None => format!("{now_ms}.mp3"),
    }
}

/// When a clip with this name was made and whose it is, when the name is a clip's.
pub fn clip_named(name: &str) -> Option<(i64, Option<String>)> {
    // The end of the name may be followed by the end of a line.
    let name = name.strip_suffix('\n').unwrap_or(name);
    let stem = name.strip_suffix(".mp3")?;
    let (made, session) = match stem.split_once("__") {
        Some((made, session)) => (made, Some(session)),
        None => (stem, None),
    };
    if made.len() < 10 || !made.bytes().all(|byte| byte.is_ascii_digit()) {
        return None;
    }
    let plain = |byte: u8| byte.is_ascii_alphanumeric() || matches!(byte, b'.' | b'_' | b'-');
    if session.is_some_and(|session| session.is_empty() || !session.bytes().all(plain)) {
        return None;
    }
    Some((made.parse().ok()?, session.map(str::to_string)))
}

/// A piece of what an agent says aloud as it is handed to an engine. The first piece of
/// an agent the user is not looking at begins with who speaks, unless it does already.
pub fn announced(piece: &str, first: bool, persona: &str, looked_at: bool) -> String {
    let says_so = piece.to_lowercase().starts_with(&persona.to_lowercase());
    if first && !persona.is_empty() && !looked_at && !says_so {
        format!("{persona} here. {piece}")
    } else {
        piece.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_agent_nobody_looks_at_says_who_speaks_once() {
        assert_eq!(
            announced("The build is green.", true, "Maximus", false),
            "Maximus here. The build is green."
        );
        assert_eq!(
            announced("The build is green.", true, "Maximus", true),
            "The build is green."
        );
        assert_eq!(
            announced("And the gate too.", false, "Maximus", false),
            "And the gate too."
        );
        assert_eq!(
            announced("maximus here, done.", true, "Maximus", false),
            "maximus here, done."
        );
        assert_eq!(announced("Done.", true, "", false), "Done.");
        assert_eq!(archetype("Claude-5628"), "Claude");
        assert_eq!(archetype("Claude-56289"), "Claude-56289");
        assert_eq!(archetype("Claude-XYZW"), "Claude-XYZW");
        assert_eq!(archetype("Claude"), "Claude");
    }
}
