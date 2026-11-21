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
use sha2::{Digest, Sha256};

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

/// Every name of the roster's tiers, without the letters' case: names a contact is never
/// minted with (`roster.CONTACT_TIERS`).
pub fn tier_names() -> impl Iterator<Item = &'static str> {
    lists()["tiers"]
        .as_object()
        .into_iter()
        .flat_map(|tiers| tiers.keys().map(String::as_str))
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
                "voice_id": crate::said::canonical_voice(&python::compact_ascii(&voices)),
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
    /// The digest of its own picture, when it has one: what the picture is kept under, and
    /// the beginning of it is what the picture's address carries ([`version`]).
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub avatar: Option<String>,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub personality: String,
    pub created_at_ms: i64,
}

/// What a picture's address carries of the picture's digest: its first sixteen signs, as the
/// Python Host's addresses do.
pub fn version(digest: &str) -> &str {
    digest.get(..16).unwrap_or(digest)
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
        Some(digest) => portraits::address(AVATARS, &made.persona_id, version(digest)),
        None => String::new(),
    };
    json!({
        "id": made.persona_id,
        "name": made.name,
        "voice_id": made.voice_id,
        "avatar_symbol": made.avatar_symbol,
        "avatar_url": avatar_url,
        // Said by the Host where the picture is under way (`pictures::under_way`).
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

/// How a persona that is built in looks where its owner changed that: its symbol, and what
/// is in its own picture. Nothing else of such a persona can be changed.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct Look {
    /// The built-in persona's id (`builtin-mike`).
    pub persona_id: String,
    #[serde(default, skip_serializing_if = "String::is_empty")]
    pub avatar_symbol: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub avatar: Option<String>,
}

/// What a maker says of a persona. `picture` is the picture it is given now, in base64;
/// empty: none came with it, and the one it has stays.
#[derive(Debug, Clone, Copy, Default)]
pub struct Wanted<'a> {
    pub name: &'a str,
    pub voice_id: &'a str,
    pub avatar_symbol: &'a str,
    pub personality: &'a str,
    pub picture: &'a str,
}

/// What a change left to keep: a persona of the owner's, or the look of a built-in one.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Kept {
    Made(Made),
    Look(Look),
}

/// A change that was made: what is to be kept for it, and the picture that came with it,
/// to be kept under the persona's id.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Change {
    pub kept: Kept,
    pub picture: Option<Vec<u8>>,
}

/// What a picture that is no base64 is refused with. (The Python Host says what its decoder
/// said, in that decoder's own words, which differ from fault to fault.)
pub const NO_PICTURE: &str = "Avatar is not base64.";

/// A picture as it is handed over, in base64, with its digest. `Ok(None)`: none was handed
/// over.
pub fn picture(base64: &str) -> Result<Option<(Vec<u8>, String)>, &'static str> {
    if base64.is_empty() {
        return Ok(None);
    }
    let bytes = python::b64decode(base64).ok_or(NO_PICTURE)?;
    if bytes.len() > AVATAR_MOST {
        return Err(AVATAR_TOO_LARGE);
    }
    let digest: String = Sha256::digest(&bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect();
    Ok(Some((bytes, digest)))
}

/// Two names that are one name in a table that folds the letters of ASCII alone, as the
/// Python Host's does (SQLite's `NOCASE`): `zoë` is `Zoë`, and `ZOË` is another.
fn one_name(a: &str, b: &str) -> bool {
    a.eq_ignore_ascii_case(b)
}

/// The personas of one Host: the ones that are built in, as its owner made them look, and
/// the ones its owner made.
///
/// A name is one persona's. A persona the owner made under a name his file later gives to a
/// built-in one sleeps behind that one: it is not listed, changed or removed until the file
/// lets the name go again.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Book {
    made: Vec<Made>,
    looks: Vec<Look>,
}

impl Book {
    pub fn new(mut made: Vec<Made>, looks: Vec<Look>) -> Self {
        made.sort_by(|a, b| {
            (a.created_at_ms, &a.persona_id).cmp(&(b.created_at_ms, &b.persona_id))
        });
        Self { made, looks }
    }

    /// Apply a reviewed migration to a saved contact.
    pub fn set_stored_voice(&mut self, id: &str, voice: &str) -> Option<&Made> {
        let contact = self
            .made
            .iter_mut()
            .find(|contact| contact.persona_id == id)?;
        contact.voice_id = voice.to_owned();
        Some(contact)
    }

    /// A built-in persona as an app is told it, with the look its owner gave it.
    fn dressed(&self, mut persona: Value) -> Value {
        let id = persona["id"].as_str().unwrap_or("").to_string();
        if let Some(look) = self.looks.iter().find(|look| look.persona_id == id) {
            persona["avatar_symbol"] = json!(look.avatar_symbol);
            if let Some(digest) = &look.avatar {
                let address = portraits::address(AVATARS, &id, version(digest));
                persona["avatar_url"] = json!(address);
            }
        }
        persona
    }

    /// The built-in persona of this name, as an app is told it.
    fn built_in(&self, built_in: &[Value], name: &str) -> Option<Value> {
        let named = |persona: &&Value| {
            persona["name"]
                .as_str()
                .is_some_and(|known| one_name(known, name))
        };
        built_in
            .iter()
            .find(named)
            .map(|persona| self.dressed(persona.clone()))
    }

    /// A picture made for a persona of the owner's, by its id: the digest it is kept under set
    /// and nothing else changed (`personas.update_avatar`: only how it looks). What is to be
    /// kept for it; `None` where no persona of the owner's that is awake has that id.
    pub fn set_picture(
        &mut self,
        owner: &Owner<'_>,
        persona_id: &str,
        digest: &str,
    ) -> Option<Made> {
        let built_in = builtins(owner);
        let at = self.made.iter().position(|made| {
            made.persona_id == persona_id && self.built_in(&built_in, &made.name).is_none()
        })?;
        self.made[at].avatar = Some(digest.to_string());
        Some(self.made[at].clone())
    }

    /// The personas the owner made that are awake: no built-in persona has their name.
    fn awake<'a>(&'a self, built_in: &'a [Value]) -> impl Iterator<Item = &'a Made> {
        self.made
            .iter()
            .filter(|made| self.built_in(built_in, &made.name).is_none())
    }

    fn made_at(&self, built_in: &[Value], name: &str) -> Option<usize> {
        let at = self
            .made
            .iter()
            .position(|made| one_name(&made.name, name))?;
        self.built_in(built_in, name).is_none().then_some(at)
    }

    /// Every persona, as an app is told them: the built-in ones in the roster's order, then
    /// the owner's in the order he made them.
    pub fn listed(&self, owner: &Owner<'_>) -> Vec<Value> {
        let built_in = builtins(owner);
        let own: Vec<Value> = self.awake(&built_in).map(told).collect();
        let dressed = built_in.into_iter().map(|persona| self.dressed(persona));
        dressed.chain(own).collect()
    }

    /// The persona of this name as it is kept, whoever made it: its voices, how it speaks,
    /// its symbol and what is in its picture. What an agent made under the name takes on.
    pub fn named(&self, owner: &Owner<'_>, name: &str) -> Option<Value> {
        let name = clarp_speech::listen::strip(name);
        let built_in = builtins(owner);
        if let Some(persona) = self.built_in(&built_in, name) {
            return Some(persona);
        }
        self.made_at(&built_in, name).map(|at| told(&self.made[at]))
    }

    /// The digest of the picture a persona has of its own, by the persona's id.
    pub fn picture_of(&self, owner: &Owner<'_>, persona_id: &str) -> Option<String> {
        let built_in = builtins(owner);
        if built_in.iter().any(|persona| persona["id"] == persona_id) {
            let look = self
                .looks
                .iter()
                .find(|look| look.persona_id == persona_id)?;
            return look.avatar.clone();
        }
        let mut awake = self.awake(&built_in);
        awake
            .find(|made| made.persona_id == persona_id)?
            .avatar
            .clone()
    }

    /// The voices of Cartesia's that personas have: what a new persona is not given first.
    pub fn voices(&self, owner: &Owner<'_>) -> HashSet<String> {
        let built_in = builtins(owner);
        let stored = built_in
            .iter()
            .filter_map(|persona| persona["voice_id"].as_str());
        let of_made = self.awake(&built_in).map(|made| made.voice_id.as_str());
        stored.chain(of_made).filter_map(cartesia_of).collect()
    }

    /// The owner makes a persona. `new` is the id it gets and the voice it is given when
    /// its maker names none ([`picked_voice`]). Refused in the Python Host's words.
    pub fn create(
        &mut self,
        owner: &Owner<'_>,
        wanted: &Wanted<'_>,
        new: (&str, &str),
        now_ms: i64,
    ) -> Result<Change, String> {
        let (id, voice) = new;
        let built_in = builtins(owner);
        let name = named(wanted.name).ok_or(NAME_NEEDED)?;
        let taken_by_made = self.made.iter().any(|made| one_name(&made.name, &name));
        if self.built_in(&built_in, &name).is_some() || taken_by_made {
            return Err(taken(&name));
        }
        let picture = picture(wanted.picture)?;
        let made = Made {
            persona_id: id.to_string(),
            name,
            voice_id: match wanted.voice_id {
                "" => voice.to_string(),
                named => named.to_string(),
            },
            avatar_symbol: python::first(wanted.avatar_symbol, SYMBOL_MOST).to_string(),
            avatar: picture.as_ref().map(|(_, digest)| digest.clone()),
            personality: python::first(wanted.personality, PERSONALITY_MOST).to_string(),
            created_at_ms: now_ms,
        };
        self.made.push(made.clone());
        Ok(Change {
            kept: Kept::Made(made),
            picture: picture.map(|(bytes, _)| bytes),
        })
    }

    /// The owner changes a persona, named as it was. Of a built-in one only its look is
    /// changed, whatever else is said. `voice` is the one a persona without any is given.
    pub fn update(
        &mut self,
        owner: &Owner<'_>,
        original: &str,
        wanted: &Wanted<'_>,
        voice: &str,
    ) -> Result<Change, String> {
        let original = clarp_speech::listen::strip(original);
        let built_in = builtins(owner);
        if let Some(persona) = self.built_in(&built_in, original) {
            let id = persona["id"].as_str().unwrap_or("").to_string();
            let picture = picture(wanted.picture)?;
            let before = self.looks.iter().find(|look| look.persona_id == id);
            let kept = before.and_then(|look| look.avatar.clone());
            let look = Look {
                avatar_symbol: python::first(wanted.avatar_symbol, SYMBOL_MOST).to_string(),
                avatar: picture.as_ref().map(|(_, digest)| digest.clone()).or(kept),
                persona_id: id,
            };
            self.looks.retain(|held| held.persona_id != look.persona_id);
            self.looks.push(look.clone());
            return Ok(Change {
                kept: Kept::Look(look),
                picture: picture.map(|(bytes, _)| bytes),
            });
        }
        let at = self.made_at(&built_in, original).ok_or(NOT_CHANGEABLE)?;
        let name = named(wanted.name).ok_or(NAME_NEEDED_TO_CHANGE)?;
        let other = self
            .made
            .iter()
            .position(|made| one_name(&made.name, &name));
        if other.is_some_and(|other| other != at) || self.built_in(&built_in, &name).is_some() {
            return Err(taken(&name));
        }
        let picture = picture(wanted.picture)?;
        let held = &mut self.made[at];
        held.name = name;
        held.voice_id = match (wanted.voice_id, held.voice_id.as_str()) {
            ("", "") => voice.to_string(),
            ("", kept) => kept.to_string(),
            (named, _) => named.to_string(),
        };
        held.avatar_symbol = python::first(wanted.avatar_symbol, SYMBOL_MOST).to_string();
        if let Some((_, digest)) = &picture {
            held.avatar = Some(digest.clone());
        }
        held.personality = python::first(wanted.personality, PERSONALITY_MOST).to_string();
        Ok(Change {
            kept: Kept::Made(held.clone()),
            picture: picture.map(|(bytes, _)| bytes),
        })
    }

    /// The owner removes a persona of his own. `None`: there is none of that name, or it
    /// is built in.
    pub fn delete(&mut self, owner: &Owner<'_>, name: &str) -> Option<Made> {
        let at = self.made_at(&builtins(owner), clarp_speech::listen::strip(name))?;
        Some(self.made.remove(at))
    }

    /// A persona as an app is told it, by what a change left to keep.
    pub fn told(&self, owner: &Owner<'_>, kept: &Kept) -> Value {
        match kept {
            Kept::Made(made) => told(made),
            Kept::Look(look) => {
                let all = builtins(owner).into_iter();
                let mut found = all.filter(|persona| persona["id"] == look.persona_id.as_str());
                found
                    .next()
                    .map_or(Value::Null, |persona| self.dressed(persona))
            }
        }
    }
}
