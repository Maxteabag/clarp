//! The language an agent is spoken to in, and the engines that hear it.
//!
//! A Host has one language its agents are spoken to in, and an agent can have its own: one
//! code, or a few the speaker moves between. An agent can also have engines of its own, in
//! the order they are tried: the first hears the clip, and the next only when the one before
//! failed, so that one provider's outage does not lose what was said. Both are settings,
//! kept as the Python Host keeps them (a map from an agent's name, persona or session, in
//! small letters, to a text), and the rules here are that Host's (`stt_language.py`, and the
//! language an engine is handed in `stt_providers.py` and `stt.py`), held by
//! `tests/speech_vectors.rs`.

use clarp_speech::listen::strip;
use serde_json::{Map, Value};

use crate::python;
use crate::said::Who;

/// The language of a Host that was told none.
pub const DEFAULT: &str = "en";

/// Where the settings are kept: the Host's language, and the maps of single agents.
pub const LANGUAGE_KEY: &str = "transcription.language";
pub const AGENT_LANGUAGES_KEY: &str = "transcription.agent_languages";
pub const AGENT_ENGINES_KEY: &str = "transcription.agent_engines";

pub const NO_CODE: &str = "language must be an ISO 639-1 code such as 'en' or 'no'";
pub const NO_SET: &str = "agent language must be a string of ISO language codes";
pub const TOO_MANY: &str = "agent language set accepts at most three language codes";
pub const SET_NEEDS: &str =
    "agent language-set recognition requires Google Chirp or ElevenLabs Scribe";

/// A value as the Python Host writes it into a text (`str(value)`).
fn shown(value: &Value) -> String {
    match value {
        Value::String(text) => text.clone(),
        other => python::repr(other),
    }
}

fn is_code(text: &str) -> bool {
    (2..=3).contains(&text.len()) && text.bytes().all(|sign| sign.is_ascii_lowercase())
}

/// The primary language subtag, including the Norwegian aliases used by speech engines.
pub(crate) fn primary(code: &str) -> String {
    let code = strip(code);
    let code = code.split(['-', '_']).next().unwrap_or("").to_lowercase();
    match code.as_str() {
        "nb" | "nn" => "no".to_string(),
        _ => code,
    }
}

/// One language: two or three letters, written small.
pub fn code(value: &Value) -> Result<String, &'static str> {
    let Value::String(text) = value else {
        return Err(NO_CODE);
    };
    let said = strip(text).to_lowercase();
    if is_code(&said) {
        Ok(said)
    } else {
        Err(NO_CODE)
    }
}

/// An agent's language: one code, or up to three the speaker moves between, each once.
pub fn set(value: &Value) -> Result<String, &'static str> {
    let Value::String(text) = value else {
        return Err(NO_SET);
    };
    let mut codes: Vec<String> = Vec::new();
    for part in text.split(',') {
        let one = code(&Value::String(part.to_string()))?;
        if !codes.contains(&one) {
            codes.push(one);
        }
    }
    if codes.len() > 3 {
        return Err(TOO_MANY);
    }
    Ok(codes.join(","))
}

/// A map of single agents as it is kept: each name in small letters with its text, without
/// the space around either, and without what is empty. What is no map is an empty one.
pub fn stored(text: Option<&str>) -> Vec<(String, String)> {
    let text = text.filter(|text| !text.is_empty()).unwrap_or("{}");
    let Ok(Value::Object(raw)) = serde_json::from_str::<Value>(text) else {
        return Vec::new();
    };
    let mut entries: Vec<(String, String)> = Vec::new();
    for (name, value) in &raw {
        let name = strip(name).to_lowercase();
        let value = strip(&shown(value)).to_string();
        if name.is_empty() || value.is_empty() {
            continue;
        }
        match entries.iter_mut().find(|(known, _)| *known == name) {
            Some(entry) => entry.1 = value,
            None => entries.push((name, value)),
        }
    }
    entries
}

/// The languages of single agents: the entries of the map that are languages.
pub fn languages(text: Option<&str>) -> Vec<(String, String)> {
    let entries = stored(text).into_iter();
    entries
        .filter_map(|(name, value)| Some((name, set(&Value::String(value)).ok()?)))
        .collect()
}

/// The Host's language as it is kept, or `otherwise` when none that is one is kept.
pub fn selected(text: Option<&str>, otherwise: &str) -> String {
    let kept = strip(text.unwrap_or("")).to_lowercase();
    if is_code(&kept) {
        kept
    } else {
        otherwise.to_string()
    }
}

/// An agent's own entry in a map: under its name, its persona or its session, in that order.
pub fn of_agent<'a>(entries: &'a [(String, String)], who: Who<'_>) -> Option<&'a str> {
    [who.name, who.persona, who.session].iter().find_map(|key| {
        let key = strip(key).to_lowercase();
        let found = entries
            .iter()
            .find(|(known, _)| !key.is_empty() && *known == key);
        found.map(|(_, value)| value.as_str())
    })
}

/// The engines an entry names, in the order they are tried.
pub fn engines(value: &str) -> Vec<String> {
    let parts = value.split(',').map(strip);
    parts
        .filter(|part| !part.is_empty())
        .map(str::to_string)
        .collect()
}

/// A map as it is kept: its names in order, nothing between its signs, in ASCII.
fn kept(entries: Vec<(String, String)>) -> String {
    let fields: Map<String, Value> = entries
        .into_iter()
        .map(|(name, value)| (name, Value::String(value)))
        .collect();
    python::sorted_ascii(&Value::Object(fields))
}

fn set_entry(entries: &mut Vec<(String, String)>, name: String, value: Option<String>) {
    entries.retain(|(known, _)| *known != name);
    if let Some(value) = value {
        entries.push((name, value));
    }
}

/// The engines of single agents after a change: `{agent: engines}`, where nothing or an
/// empty text takes an agent's entry away. Answers the map as it is kept, or why the
/// change is refused. `is_valid` says whether an engine is one this Host knows.
pub fn engines_changed(
    kept_now: Option<&str>,
    change: &Value,
    is_valid: &dyn Fn(&str) -> bool,
) -> Result<String, String> {
    let Value::Object(change) = change else {
        return Err("agent_engines must be an object of agent -> engine".to_string());
    };
    let mut entries = stored(kept_now);
    for (agent, engine) in change {
        let name = strip(agent).to_lowercase();
        if name.is_empty() {
            return Err("agent_engines keys must be agent names".to_string());
        }
        let unknown = || format!("unknown transcription engine: {}", shown(engine));
        let named = match engine {
            Value::Null => None,
            Value::String(text) if text.is_empty() => None,
            Value::String(text) => {
                let listed = engines(text);
                let known = listed.iter().all(|engine| is_valid(engine));
                if !known || text.trim_matches([' ', ',']).is_empty() {
                    return Err(unknown());
                }
                Some(listed.join(","))
            }
            _ => return Err(unknown()),
        };
        set_entry(&mut entries, name, named);
    }
    Ok(kept(entries))
}

/// The languages of single agents after a change: `{agent: language}`, where nothing or an
/// empty text takes an agent's entry away.
pub fn languages_changed(kept_now: Option<&str>, change: &Value) -> Result<String, String> {
    let Value::Object(change) = change else {
        return Err("agent_languages must be an object of agent -> language".to_string());
    };
    let mut entries = languages(kept_now);
    for (agent, language) in change {
        let name = strip(agent).to_lowercase();
        if name.is_empty() {
            return Err("agent_languages keys must be agent names".to_string());
        }
        let named = match language {
            Value::Null => None,
            Value::String(text) if text.is_empty() => None,
            other => Some(set(other)?),
        };
        set_entry(&mut entries, name, named);
    }
    Ok(kept(entries))
}

/// The providers whose engines over the network can be handed a set of languages.
pub const SET_PROVIDERS: [&str; 2] = ["elevenlabs", "google"];

/// The language an engine over the network is handed for a clip. A set of languages is
/// something only two of them take: Google's Chirp is told the set, and ElevenLabs Scribe
/// is told nothing and finds out by itself. Any other engine cannot hear such an agent.
pub fn handed(language: &str, provider: &str) -> Result<Option<String>, &'static str> {
    let several = language.contains(',');
    if several && !SET_PROVIDERS.contains(&provider) {
        return Err(SET_NEEDS);
    }
    let finds_out = provider == "elevenlabs" && several;
    Ok((!finds_out).then(|| language.to_string()))
}

/// The language the Host's own engine is told: English for a model that knows only
/// English, none for one that finds out. An agent's language is for the engines over the
/// network.
pub fn own_language(model: &str) -> Option<&'static str> {
    model.ends_with(".en").then_some("en")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn an_agent_is_found_under_its_name_its_persona_or_its_session() {
        let entries = stored(Some(r#"{"Koko": "no,en", "mochi-12ab": "pt", "": "x"}"#));
        let who = |name, persona, session| Who {
            name,
            persona,
            session,
        };
        assert_eq!(
            of_agent(&entries, who("Koko", "Koko", "koko-8255")),
            Some("no,en")
        );
        assert_eq!(
            of_agent(&entries, who("", " KOKO ", "koko-8255")),
            Some("no,en")
        );
        assert_eq!(
            of_agent(&entries, who("Mochi", "Mochi", "mochi-12ab")),
            Some("pt")
        );
        assert_eq!(of_agent(&entries, who("Echo", "Echo", "echo")), None);
        assert_eq!(of_agent(&entries, who("", "", "")), None);
        assert_eq!(of_agent(&[], who("Koko", "Koko", "koko")), None);
    }

    #[test]
    fn an_engine_is_handed_the_language_it_can_take() {
        assert_eq!(handed("no", "elevenlabs"), Ok(Some("no".to_string())));
        assert_eq!(handed("no,en", "elevenlabs"), Ok(None));
        assert_eq!(handed("no,en", "google"), Ok(Some("no,en".to_string())));
        assert_eq!(handed("no,en", "deepgram"), Err(SET_NEEDS));
        assert_eq!(handed("no,en", "cartesia"), Err(SET_NEEDS));
        assert_eq!(handed("en", "deepgram"), Ok(Some("en".to_string())));
        assert_eq!(own_language("small.en"), Some("en"));
        assert_eq!(own_language("large-v3-turbo"), None);
    }
}
