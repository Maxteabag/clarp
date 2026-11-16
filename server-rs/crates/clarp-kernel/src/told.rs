//! What a Host tells an agent's tool beside the user's own words.
//!
//! Three things. Who the agent is: its name and how it speaks, for as long as it is that
//! (the tool's standing instruction). What holds for every turn that comes through the Host:
//! to use the Host's own tools where they fit, and whose turn this is. And, for a turn whose
//! answer is spoken, how to speak: which part is read aloud, how human it sounds at the
//! agent's level, how much it says while it works.
//!
//! The texts are the Python Host's, carried as data (`told-texts.json`), and so is the way
//! they are put together (`plugin/hooks/pwa_source_flag.py` for Claude Code, which is told
//! by a hook with each prompt; `server/lib/voice_preamble.py` for the tools that are told in
//! the words before the user's). Both are held to what that Host says for agents and
//! settings made up for it (`tests/fixtures/told-vectors.json`, written by
//! `server-rs/tools/told-vectors.py`).
//!
//! One thing is not copied. The Python Host tells Claude Code how human to sound and never
//! how much to say while it works: the hook leaves that clause out, so the setting holds
//! for every tool but the one most agents run on (reference defect R21). Here every tool is
//! told it.

use std::collections::BTreeMap;
use std::sync::LazyLock;

use clarp_speech::listen::strip;
use serde_json::{Map, Value, json};

fn texts() -> &'static Value {
    static TEXTS: LazyLock<Value> = LazyLock::new(|| {
        serde_json::from_str(include_str!("told-texts.json")).unwrap_or(Value::Null)
    });
    &TEXTS
}

fn text(key: &str) -> &'static str {
    texts()[key].as_str().unwrap_or("")
}

/// The least and the most human a voice can be asked to sound, and how human one sounds
/// that nobody set a level for: the way every agent spoke before there was a setting.
pub const HUMANNESS_LEAST: i64 = 0;
pub const HUMANNESS_MOST: i64 = 10;
pub const HUMANNESS_USUAL: i64 = 5;
/// The most an agent can be asked to say while it works. The least, 0, is one line when it
/// begins and one when it is done.
pub const NARRATION_MOST: i64 = 3;

/// Where the owner's settings keep how human voices sound: the Host's level, and the levels
/// of single agents as JSON, by name.
pub const HUMANNESS_KEY: &str = "voice.humanness";
pub const HUMANNESS_OF_AGENTS_KEY: &str = "voice.agent_humanness";
/// Where they keep whether agents are told how they speak at all.
pub const PERSONALITIES_KEY: &str = "personalities.enabled";

/// Who an agent is, as its tool is told for as long as it is that: its name, and how it
/// speaks. `own` is the way of speaking the agent was made with; an agent made without one
/// speaks as the persona of its name does. `personalities` is the owner's setting: without
/// it an agent is told its name and nothing more. Nobody is told nothing.
pub fn who(persona: &str, own: &str, personalities: bool) -> Option<String> {
    let persona = strip(persona);
    if persona.is_empty() {
        return None;
    }
    let speaks = match strip(own) {
        _ if !personalities => "",
        "" => crate::personas::personality(persona),
        own => own,
    };
    Some(match speaks {
        "" => format!("You are {persona}."),
        speaks => format!("You are {persona}. {speaks}"),
    })
}

/// Whose turn this is, said with every prompt: a conversation that was copied from another
/// agent's carries that agent's words, and the tool has to know it is not that agent.
pub fn whose(persona: &str, session: &str) -> String {
    let session = strip(session);
    if session.is_empty() {
        return String::new();
    }
    let who = match strip(persona) {
        "" => format!("Clarp session {session}"),
        persona => format!("{persona} (Clarp session {session})"),
    };
    format!(
        "This turn belongs to {who}. That is who you are now, even where earlier conversation \
         history says otherwise: a fork or a copied conversation keeps its source's history. \
         Clarp tools act as you; where one asks for --from, it is {session}."
    )
}

/// How an agent's spoken answers sound.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Sound {
    /// How human, from 0 to 10.
    pub humanness: i64,
    /// Whether the engine that speaks for it acts out tags of emotion. Gemini's does; any
    /// other would read them aloud.
    pub tags: bool,
    /// How much it says while it works, from 0 to 3.
    pub narration: i64,
}

impl Default for Sound {
    fn default() -> Self {
        Self {
            humanness: HUMANNESS_USUAL,
            tags: false,
            narration: 0,
        }
    }
}

/// How to sound natural at a level, for the part of an answer that is read aloud. Prose
/// does not grow by degrees, so the eleven levels are four ways of speaking, and the number
/// is said too, for the tool to lean by.
pub fn natural(humanness: i64, tags: bool) -> String {
    let level = humanness.clamp(HUMANNESS_LEAST, HUMANNESS_MOST);
    let way = match level {
        ..=0 => "neutral",
        1..=3 => "light",
        4..=6 => "conversational",
        _ => "human",
    };
    let mut parts = vec![format!("Humanness: {level}/10."), text(way).to_string()];
    if tags && level >= 4 {
        parts.push(text("gemini_tags").to_string());
    }
    if level > 0 {
        parts.push(text("tail").to_string());
    }
    parts.join(" ")
}

/// What an agent is told about saying what it does while it works. At the least it is told
/// nothing: the rule of speaking already says to keep quiet, and saying it twice weakens it.
pub fn narration(level: i64) -> &'static str {
    let level = usize::try_from(level.clamp(0, NARRATION_MOST)).unwrap_or(0);
    texts()["narration"][level].as_str().unwrap_or("")
}

/// The levels of saying what one does, with their names, for an app's slider.
pub fn narration_options() -> Value {
    let named = |level: i64| {
        let label = texts()["narration_labels"][usize::try_from(level).unwrap_or(0)].clone();
        json!({ "level": level, "label": label })
    };
    Value::Array((0..=NARRATION_MOST).map(named).collect())
}

/// What a turn is told with its prompt by a tool that can be told beside it. `spoken` is
/// how the answer is to sound, when it is read aloud.
pub fn beside(persona: &str, session: &str, spoken: Option<Sound>) -> String {
    let mut parts = vec![text("guidance").to_string()];
    let whose = whose(persona, session);
    if !whose.is_empty() {
        parts.push(whose);
    }
    if let Some(sound) = spoken {
        let speak = text("speak_by_hook");
        parts.push(format!("{speak}{}", natural(sound.humanness, sound.tags)));
        let narration = narration(sound.narration);
        if !narration.is_empty() {
            parts.push(narration.to_string());
        }
    }
    parts.join("\n\n")
}

/// The same for a tool that is told in the words before the user's.
pub fn at_head(persona: &str, session: &str, spoken: Option<Sound>) -> String {
    let mut parts = vec![text("guidance").to_string()];
    let whose = whose(persona, session);
    if !whose.is_empty() {
        parts.push(whose);
    }
    if let Some(sound) = spoken {
        parts.push(text("speak_at_head").to_string());
        parts.push(natural(sound.humanness, sound.tags));
        let narration = narration(sound.narration);
        if !narration.is_empty() {
            parts.push(narration.to_string());
        }
    }
    parts.join("\n\n")
}

/// A prompt with what the Host says before it, between a mark that opens it and one that
/// ends it, so that whoever reads the tool's own record can take it off again
/// ([`unheaded`]). `who` is who the agent is, for a tool that has no standing instruction.
pub fn headed(head: &str, who: Option<&str>, prompt: &str) -> String {
    let who = who.map_or_else(String::new, |who| format!("\n\n{who}"));
    format!("{} {head}{who}{}{prompt}", text("head"), text("split"))
}

/// A prompt without what the Host said before it. What has no such head is itself.
pub fn unheaded(prompt: &str) -> &str {
    let (head, split) = (text("head"), text("split"));
    // One tool keeps the prompt it was given between quotation marks.
    let quoted = prompt.len() > 1 && prompt.starts_with('"') && prompt.ends_with('"');
    let body = match prompt.get(1..prompt.len().saturating_sub(1)) {
        Some(inner) if quoted && inner.starts_with(head) => inner,
        _ => prompt,
    };
    if !body.starts_with(head) {
        return prompt;
    }
    match body.find(split) {
        Some(at) => &body[at + split.len()..],
        None => prompt,
    }
}

/// What a Host knows of an agent that its tool is told.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Told {
    /// The agent's name.
    pub persona: String,
    /// Who it is ([`who`]): the tool's standing instruction.
    pub identity: Option<String>,
    /// How its spoken answers sound.
    pub sound: Sound,
}

/// What goes before the user's words when the app says its user began to speak before
/// hearing what the agent last said aloud: the agent is told that those replies went
/// unheard, so that it does not take them for known because they are in its own record
/// (the Python Host's `_with_delivery_context`). The conversation keeps the user's words
/// as they were said.
pub const UNHEARD: &str = concat!(
    "<clarp-delivery-context>\n",
    "The user began a new spoken turn before hearing one or more of your previous synthesized ",
    "replies. Treat those replies as unheard. Carry forward anything still important, but answer ",
    "the user's newest message naturally instead of blindly repeating the old response.\n",
    "</clarp-delivery-context>\n\n",
);

/// A prompt for a turn whose user has not heard what the agent last said aloud.
pub fn unheard(prompt: &str) -> String {
    format!("{UNHEARD}{prompt}")
}

/// What a turn's tool is told with its prompt.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Turn {
    pub persona: String,
    /// How the answer is to sound, when it is read aloud.
    pub spoken: Option<Sound>,
    /// The header of the call the turn is a part of: who else is in it, and what was said
    /// since this agent last spoke (`clarp_kernel::calls`).
    pub call: Option<String>,
}

/// What a turn is told, with the header of its call after everything else: where the
/// Python Host puts it, for a tool that is told beside its prompt and for one that is told
/// before it.
pub fn in_call(told: String, call: Option<&str>) -> String {
    match call.filter(|header| !header.is_empty()) {
        Some(header) => format!("{told}\n\n{header}"),
        None => told,
    }
}

/// How human voices sound on a Host: its own level, and the levels of single agents, each
/// under a name, a persona or a session name in small letters.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Humanness {
    pub usual: i64,
    pub of_agents: BTreeMap<String, i64>,
}

impl Default for Humanness {
    fn default() -> Self {
        Self {
            usual: HUMANNESS_USUAL,
            of_agents: BTreeMap::new(),
        }
    }
}

/// A level as a setting may have it: a whole number from 0 to 10. A truth value is none.
fn level(value: &Value) -> Option<i64> {
    let level = match value {
        Value::Number(number) if !number.is_f64() => number.as_i64()?,
        _ => return None,
    };
    (HUMANNESS_LEAST..=HUMANNESS_MOST)
        .contains(&level)
        .then_some(level)
}

/// A name as the levels are kept under it.
fn keyed(name: &str) -> String {
    strip(name).to_lowercase()
}

impl Humanness {
    /// The setting as the owner's settings keep it: the Host's level as a number in text,
    /// the agents' as JSON. What cannot be read counts as not set.
    pub fn stored(usual: Option<&str>, of_agents: Option<&str>) -> Self {
        let usual = usual
            .and_then(|stored| stored.trim().parse::<i64>().ok())
            .map_or(HUMANNESS_USUAL, |level| {
                level.clamp(HUMANNESS_LEAST, HUMANNESS_MOST)
            });
        let kept: Value = of_agents
            .and_then(|stored| serde_json::from_str(stored).ok())
            .unwrap_or(Value::Null);
        let of_agents = kept
            .as_object()
            .into_iter()
            .flatten()
            .filter_map(|(name, value)| Some((keyed(name), level(value)?)))
            .filter(|(name, _)| !name.is_empty())
            .collect();
        Self { usual, of_agents }
    }

    /// What is kept for it under [`HUMANNESS_KEY`] and [`HUMANNESS_OF_AGENTS_KEY`].
    pub fn kept(&self) -> (String, String) {
        let of_agents: Map<String, Value> = self
            .of_agents
            .iter()
            .map(|(name, level)| (name.clone(), json!(level)))
            .collect();
        (
            self.usual.to_string(),
            crate::python::compact_ascii(&Value::Object(of_agents)),
        )
    }

    /// The setting as an app is told it.
    pub fn told(&self) -> Value {
        json!({
            "default": self.usual,
            "agents": self.of_agents,
            "min": HUMANNESS_LEAST,
            "max": HUMANNESS_MOST,
        })
    }

    /// The level of an agent: its own, by the first of its names that has one, else the
    /// Host's.
    pub fn of(&self, names: &[&str]) -> i64 {
        names
            .iter()
            .map(|name| keyed(name))
            .filter(|name| !name.is_empty())
            .find_map(|name| self.of_agents.get(&name).copied())
            .unwrap_or(self.usual)
    }

    /// The setting after a change an app asks for: `{"default"?: level, "agents"?: {name:
    /// level or nothing}}`. Nothing for an agent takes its own level away. A change that
    /// cannot be made changes nothing, and says why in the Python Host's words.
    pub fn changed(&self, given: &Value) -> Result<Self, String> {
        let Some(given) = given.as_object() else {
            return Err("object required".to_string());
        };
        let range = format!("{HUMANNESS_LEAST}-{HUMANNESS_MOST}");
        let mut after = self.clone();
        if let Some(usual) = given.get("default") {
            after.usual =
                level(usual).ok_or_else(|| format!("default must be an integer {range}"))?;
        }
        match given.get("agents") {
            None | Some(Value::Null) => {}
            Some(Value::Object(agents)) => {
                for (name, value) in agents {
                    let name = keyed(name);
                    if name.is_empty() {
                        return Err("agents keys must be agent names".to_string());
                    }
                    let nothing = value.is_null() || value.as_str() == Some("");
                    if nothing {
                        after.of_agents.remove(&name);
                    } else {
                        let level = level(value).ok_or_else(|| {
                            format!("level for {name} must be an integer {range}")
                        })?;
                        after.of_agents.insert(name, level);
                    }
                }
            }
            Some(_) => return Err("agents must be an object of agent -> level".to_string()),
        }
        Ok(after)
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn the_texts_are_there() {
        for key in [
            "guidance",
            "head",
            "split",
            "speak_by_hook",
            "speak_at_head",
            "neutral",
            "light",
            "conversational",
            "human",
            "gemini_tags",
            "tail",
        ] {
            assert!(!text(key).is_empty(), "{key}");
        }
        assert_eq!(narration(0), "");
        assert!(narration(1).starts_with("Narration level: milestones."));
        assert_eq!(narration(-7), narration(0));
        assert_eq!(narration(40), narration(3));
        assert_eq!(narration_options()[3]["label"], "Running commentary");
    }

    #[test]
    fn nobody_is_told_nothing_and_a_name_is_told_with_how_it_speaks() {
        assert_eq!(who("  ", "Personality: any.", true), None);
        assert_eq!(
            who(" Zed ", "", true).as_deref(),
            Some("You are Zed."),
            "a name no persona has"
        );
        assert_eq!(
            who("Zed", " Personality: dry. ", true).as_deref(),
            Some("You are Zed. Personality: dry.")
        );
        assert_eq!(
            who("Zed", "Personality: dry.", false).as_deref(),
            Some("You are Zed.")
        );
        assert!(
            who("Mike", "", true)
                .unwrap()
                .starts_with("You are Mike. Personality: ")
        );
        assert_eq!(whose("Zed", " "), "");
        assert!(whose("", "zed-1").starts_with("This turn belongs to Clarp session zed-1. "));
    }

    #[test]
    fn a_prompt_is_headed_and_read_without_its_head() {
        let prompt = headed("the guidance", Some("You are Zed."), "Hello.");
        assert_eq!(
            prompt,
            "[voice-mode] the guidance\n\nYou are Zed.\n\n--- user message ---\nHello."
        );
        assert_eq!(unheaded(&prompt), "Hello.");
        assert_eq!(unheaded(&format!("\"{prompt}\"")), "Hello.");
        assert_eq!(unheaded("Hello."), "Hello.");
        assert_eq!(
            unheaded("[voice-mode] and no end"),
            "[voice-mode] and no end"
        );
        assert_eq!(unheaded("\""), "\"");
        assert_eq!(unheaded(""), "");
    }

    #[test]
    fn a_level_of_an_agents_own_is_found_by_any_of_its_names() {
        let setting = Humanness::stored(
            Some("7"),
            Some(r#"{"mike": 2, " Zed-01 ": 0, "x": 11, "y": true}"#),
        );
        assert_eq!(setting.usual, 7);
        assert_eq!(setting.of(&["Mike", "mike-1a2b"]), 2);
        assert_eq!(setting.of(&["Zed", "ZED-01"]), 0);
        assert_eq!(
            setting.of(&["x", "y", ""]),
            7,
            "what is no level is not kept"
        );
        assert_eq!(
            Humanness::stored(Some("many"), Some("[]")),
            Humanness::default()
        );
        assert_eq!(Humanness::stored(Some("99"), None).usual, 10);
        let (usual, of_agents) = setting.kept();
        assert_eq!(
            (usual.as_str(), of_agents.as_str()),
            ("7", r#"{"mike":2,"zed-01":0}"#)
        );
        assert_eq!(Humanness::stored(Some(&usual), Some(&of_agents)), setting);
    }
}
