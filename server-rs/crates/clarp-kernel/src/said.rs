//! Speech out: who says a line aloud, in which voice, and what its clip is called.
//!
//! An agent says something for the ear. Which engine speaks it is the owner's setting, for
//! every agent or for one; which voice is the agent's own for that engine, or its persona's
//! in the owner's lists. This file holds those rules, which are no engine's, and their
//! words are the Python Host's (`voice.py`, `tts_mode.py`, `tts_registry.py`, the voice
//! methods of `config.py`, `tts_engine.py`), held by `tests/voice_vectors.rs`.

use clarp_speech::listen::strip;
use serde_json::{Map, Value};

use crate::media::Part;
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
    /// The same personas' voices as Google's other door knows them: a voice that was
    /// designed exists only behind the door it was made at.
    pub gemini_vertex_voices: Vec<(String, String)>,
}

/// Cartesia's voices for the personas the Python Host ships with. An owner's own list adds
/// to them and never takes their place.
pub const BUILT_IN_CARTESIA_VOICES: [(&str, &str); 74] = [
    ("Rachel", "db6b0ed5-d5d3-463d-ae85-518a07d3c2b4"),
    ("Josh", "630ed21c-2c5c-41cf-9d82-10a7fd668370"),
    ("Domi", "2a12b36c-7f9b-4c3a-9f7a-72731b15323a"),
    ("Mike", "a167e0f3-df7e-4d52-a9c3-f949145efdab"),
    ("Sam", "a5136bf9-224c-4d76-b823-52bd5efcffcc"),
    ("Antoni", "4bc3cb8c-adb9-4bb8-b5d5-cbbef950b991"),
    ("Elli", "dc30854e-e398-4579-9dc8-16f6cb2c19b9"),
    ("Arnold", "49743b08-0f5d-4741-839c-b12933853780"),
    ("Bella", "d7bf7d75-64b7-4c1e-86c0-79d647366587"),
    ("Adam", "f4a3a8e4-694c-4c45-9ca0-27caf97901b5"),
    ("Marcus", "ed81fd13-2016-4a49-8fe3-c0d2761695fc"),
    ("Caleb", "41468051-3a85-4b68-92ad-64add250d369"),
    ("Nadia", "6ccbfb76-1fc6-48f7-b71d-91ac6298247b"),
    ("Priya", "9626c31c-bec5-4cca-baa8-f8ba9e84c8bc"),
    ("Diego", "86e30c1d-714b-4074-a1f2-1cb6b552fb49"),
    ("Lena", "5c42302c-194b-4d0c-ba1a-8cb485c84ab9"),
    ("Theo", "1ec736fa-db96-4eea-9299-235ce2cb7a0e"),
    ("Yuki", "263b9cc0-0d99-44e7-ae92-3d4ad5d2ad18"),
    ("Omar", "729651dc-c6c3-4ee5-97fa-350da1f88600"),
    ("Freya", "62ae83ad-4f6a-430b-af41-a9bede9286ca"),
    ("Archie", "ef191366-f52f-447a-a398-ed8c0f2943a1"),
    ("Tristan", "56c7989e-7a5f-4d12-838f-e0f910e7356e"),
    ("Ronan", "d3e3d5d5-07b0-484f-9967-dbc8f15b60d5"),
    ("Wyatt", "5fc5c797-12c5-4f2b-ac9b-d4e53c08098f"),
    ("Jasper", "e98bd614-9b9d-4031-b930-ed72482af858"),
    ("Siobhan", "d79d2b77-9192-4e10-9407-5d43ca034803"),
    ("Bronte", "391f4c0a-f1a8-4c21-9aa2-7a07f0a4b0dc"),
    ("Pearl", "d6c52d6f-6478-47a2-ad54-dbc8f3335a2b"),
    ("Imogen", "5a93ae96-9e3e-4b9d-8575-5f62b7de6d0f"),
    ("Kira", "57dcab65-68ac-45a6-8480-6c4c52ec1cd1"),
    ("Claude", "f4a3a8e4-694c-4c45-9ca0-27caf97901b5"),
    ("Codex", "ed81fd13-2016-4a49-8fe3-c0d2761695fc"),
    ("Grok", "49743b08-0f5d-4741-839c-b12933853780"),
    ("Gemini", "d7bf7d75-64b7-4c1e-86c0-79d647366587"),
    ("Janitor", ""),
    ("Clarp", "a167e0f3-df7e-4d52-a9c3-f949145efdab"),
    ("Axel", "12e85709-099c-480a-ba3e-875c41a9611a"),
    ("Iris", "f6ff7c0c-e396-40a9-a70b-f7607edb6937"),
    ("Felix", "1fcd23d0-bf12-4896-8f60-4f21ef5c9b98"),
    ("Gordon", "4b31d090-8d2d-4bcd-8a32-1c135301e26e"),
    ("Marsy", "e8e5fffb-252c-436d-b842-8879b84445b6"),
    ("Tigo", "f24ae0b7-a3d2-4dd1-89df-959bdc4ab179"),
    ("Wren", "5a93ae96-9e3e-4b9d-8575-5f62b7de6d0f"),
    ("Lezo", "30894953-bcce-41fe-892c-15ce19c843ff"),
    ("Vesper", "cfce9402-0067-458b-95a7-95846f469406"),
    ("Avana", "efddb3d2-4464-45e0-9f8a-fcd5fd4fc54f"),
    ("Aura", "daf747c6-6bc2-4083-bd59-aa94dce23f5d"),
    ("Orion", "42b39f37-515f-4eee-8546-73e841679c1d"),
    ("Cipher", "565510e8-6b45-45de-8758-13588fbaec73"),
    ("Spectra", "59cb0f89-5d66-49f8-b965-f72b252789e0"),
    ("Solu", "8b233a59-b1f4-42f2-895a-c9417ae41df8"),
    ("Lyra", "9329fbdb-e285-4fba-95ec-592e15f14476"),
    ("Echo", "eb7d0d3b-e427-483b-bbca-1c009c33f8a7"),
    ("Nova", "f0377496-2708-4cc9-b2f8-1b7fdb5e1a2a"),
    ("Margrok", "79f8b5fb-2cc8-479a-80df-29f7a7cf1a3e"),
    ("Roxy", "c2ad7092-0447-47ea-948b-61fbb6faf153"),
    ("Vance", "7e19344f-9f17-47d7-a13a-4366ad06ebf3"),
    ("Fang", "db69127a-dbaf-4fa9-b425-2fe67680c348"),
    ("Riot", "a01c369f-6d2d-4185-bc20-b32c225eab70"),
    ("Raven", "ca566b43-944e-4474-b494-7d9f0695f307"),
    ("Jax", "79d2cf27-444a-4c3a-9eed-2ad5cf795a3b"),
    ("Dagger", "5319c0b1-3dd1-4c00-b721-bfd2ec88ef56"),
    ("Blaze", "a33f7a4c-100f-41cf-a1fd-5822e8fc253f"),
    ("Spike", "ef191366-f52f-447a-a398-ed8c0f2943a1"),
    ("Pip", "d6905573-8e91-4e32-b103-fd4d1205cd87"),
    ("Bloop", "73693e3c-11ef-40e9-b5b7-786d790d562b"),
    ("Noodle", "c961b81c-a935-4c17-bfb3-ba2239de8c2f"),
    ("Ziggy", "997b6be7-b4d6-419b-8647-7fd1b45eb3ef"),
    ("Mochi", "694f9389-aac1-45b6-b726-9d9369183238"),
    ("Fathom", "ed82c17b-4704-4d34-be43-5d19065acdf1"),
    ("Coral", "58fbaf73-d7de-4e82-a6b3-118180e7057c"),
    ("Nautilus", "3f04e815-3260-4f50-8fd9-af9c657be4c2"),
    ("Luma", "1f575487-6f3d-40e0-862a-814f55b5fb15"),
    ("Trench", "23112795-d54e-4560-9568-791a87c30201"),
];

/// The voices one clip can be spoken in, worked out once for the clip.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct ClipVoices {
    /// Cartesia's: what the agent's entry names when it speaks with Cartesia, what the
    /// agent has stored, what its persona has in the owner's list.
    pub cartesia: Option<String>,
    /// ElevenLabs': what the agent's entry names, what it has stored for ElevenLabs, or
    /// what it has stored at all.
    pub elevenlabs: Option<String>,
    /// Gemini's, as the owner's lists have it: before it is said as a door knows it.
    pub gemini: Option<String>,
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

/// The voice an agent has for one provider in this clip: what its own entry names for the
/// engine it speaks with, else what it has stored.
pub fn voice_for(stored: Option<&str>, route: &Route, provider: &str) -> Option<String> {
    let named = route.voice.as_deref().filter(|voice| !voice.is_empty());
    match named.filter(|_| provider == route.provider) {
        Some(voice) => Some(voice.to_string()),
        None => resolve_voice(stored, provider),
    }
}

impl Voices {
    /// The owner's own list of Cartesia's voices over the ones the Python Host ships with.
    pub fn with_cartesia(mut self, own: &[(String, String)]) -> Self {
        let mut voices: Vec<(String, String)> = BUILT_IN_CARTESIA_VOICES
            .iter()
            .map(|(persona, voice)| ((*persona).to_string(), (*voice).to_string()))
            .collect();
        for (persona, voice) in own {
            match voices.iter_mut().find(|(known, _)| known == persona) {
                Some(listed) => listed.1.clone_from(voice),
                None => voices.push((persona.clone(), voice.clone())),
            }
        }
        self.cartesia_voices = voices;
        self
    }

    /// The voices one clip can be spoken in.
    pub fn for_clip(&self, stored: Option<&str>, who: Who<'_>, route: &Route) -> ClipVoices {
        let listed = || self.cartesia_voice_for(who.persona).map(str::to_string);
        let cartesia = voice_for(stored, route, CARTESIA).or_else(listed);
        let whole = stored
            .filter(|stored| !stored.is_empty())
            .map(str::to_string);
        let elevenlabs = voice_for(stored, route, ELEVENLABS).or(whole);
        let named = route.voice.as_deref().filter(|_| route.provider == GEMINI);
        let gemini = self.gemini_voice(stored, who, named, cartesia.as_deref());
        ClipVoices {
            cartesia,
            elevenlabs,
            gemini,
        }
    }

    /// A voice of Gemini's as the door a request goes through knows it. Behind Vertex, a
    /// voice that was designed at the Gemini API is the same persona's voice at Vertex;
    /// Google's own voices and what no persona has are the same behind both.
    pub fn gemini_voice_at(&self, voice: &str, vertex: bool) -> String {
        let at_vertex = |known: &&(String, String)| known.1 == voice;
        if !vertex
            || self
                .gemini_vertex_voices
                .iter()
                .any(|known| at_vertex(&known))
        {
            return voice.to_string();
        }
        // Each persona once, as a list holds it: listed twice, the last stands.
        let last = |voices: &[(String, String)], persona: &str| {
            let found = voices.iter().rev().find(|(known, _)| known == persona);
            found.map(|(_, voice)| voice.clone())
        };
        let mut seen: Vec<&str> = Vec::new();
        for (persona, _) in &self.gemini_voices {
            if seen.contains(&persona.as_str()) {
                continue;
            }
            seen.push(persona);
            let here = last(&self.gemini_voices, persona);
            let there = last(&self.gemini_vertex_voices, persona).filter(|voice| !voice.is_empty());
            if let (true, Some(there)) = (here.as_deref() == Some(voice), there) {
                return there;
            }
        }
        voice.to_string()
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

/// The part of a clip a player asks for, read as the Python Host reads it for a clip
/// (`clip_stream.py`, `_parse_range_header`): from one byte on, to another or to the end.
/// The last so many bytes, several parts at once and what cannot be read as a part are no
/// part at all: the player is given the clip from its start.
pub fn clip_range(header: &str) -> Option<(u64, Option<u64>)> {
    let value = strip(header);
    let unit = value.get(..6)?;
    if !unit.eq_ignore_ascii_case("bytes=") {
        return None;
    }
    let spec = strip(&value[6..]);
    if spec.contains(',') {
        return None;
    }
    let (first, last) = spec.split_once('-')?;
    let (first, last) = (strip(first), strip(last));
    if first.is_empty() {
        return None;
    }
    let first = python::int(first)?;
    let last = match last {
        "" => None,
        last => Some(python::int(last)?),
    };
    if first < 0 || last.is_some_and(|last| last < first) {
        return None;
    }
    let byte = |number: i128| u64::try_from(number).unwrap_or(u64::MAX);
    Some((byte(first), last.map(byte)))
}

/// Which bytes of a clip that has `there` bytes a player is given for the part it asked
/// for: all of them when it asked for no part, the part as far as the clip goes, or
/// nothing when the part begins where the clip has no byte.
pub fn clip_part(range: Option<(u64, Option<u64>)>, there: u64) -> Part {
    let Some((first, last)) = range else {
        return Part::Whole;
    };
    if first >= there {
        return Part::Missing;
    }
    let last = last.map_or(there - 1, |last| last.min(there - 1));
    Part::Bytes { first, last }
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
    fn the_part_of_a_clip_a_player_asks_for() {
        assert_eq!(clip_range("bytes=0-1"), Some((0, Some(1))));
        assert_eq!(clip_range("  Bytes= 42 - "), Some((42, None)));
        assert_eq!(clip_range("bytes=+5-1_0"), Some((5, Some(10))));
        assert_eq!(clip_range("bytes=7-7"), Some((7, Some(7))));
        let huge = format!("bytes={}-", "9".repeat(50));
        assert_eq!(clip_range(&huge), Some((u64::MAX, None)));
        for none in [
            "",
            "bytes",
            "items=0-1",
            "bytes=-5",
            "bytes=0-1,4-5",
            "bytes=5",
            "bytes=9-3",
            "bytes=a-3",
            "bytes=3-b",
            "bytes=5--3",
            "bytes=1.5-3",
        ] {
            assert_eq!(clip_range(none), None, "{none:?}");
        }
        // No part asked for: the whole clip, also one that has nothing yet.
        assert_eq!(clip_part(None, 100), Part::Whole);
        assert_eq!(clip_part(None, 0), Part::Whole);
        // A part is given as far as the clip goes.
        let bytes = |first, last| Part::Bytes { first, last };
        assert_eq!(clip_part(Some((0, Some(1))), 100), bytes(0, 1));
        assert_eq!(clip_part(Some((42, None)), 100), bytes(42, 99));
        assert_eq!(clip_part(Some((42, Some(500))), 100), bytes(42, 99));
        assert_eq!(clip_part(Some((99, Some(99))), 100), bytes(99, 99));
        // And is missing where the clip has no byte.
        assert_eq!(clip_part(Some((100, None)), 100), Part::Missing);
        assert_eq!(clip_part(Some((0, Some(1))), 0), Part::Missing);
        assert_eq!(clip_part(Some((u64::MAX, None)), 100), Part::Missing);
    }

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
