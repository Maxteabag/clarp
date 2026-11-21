//! The catalogue of voices: which engines speak, and which voices each has.
//!
//! An app's voice picker asks the Host for it: the engine every agent speaks with and the
//! one tried after it, whether each engine can be used, and for each voice whether another
//! agent has it and whether it is the voice of the agent that is being looked at. The
//! answer is the Python Host's (`voice_catalog.py`, `tts_providers.py`), field for field,
//! held by that Host's own answers (`tests/catalog_vectors.rs`).
//!
//! The lists of voices that Host ships with are carried as data (`voice-lists.json`,
//! written from its lists by `tools/catalog-vectors.py`): Gemini's own voices, a snapshot of
//! Deepgram's, the ElevenLabs voices of its settings screen, Google's Chirp voices, and the
//! table of engines. Cartesia's voices are Cartesia's own list, fetched by the Host with the
//! owner's key; here it is an input.

use std::sync::OnceLock;

use serde_json::{Map, Value, json};

use crate::said::{CARTESIA, ELEVENLABS, GEMINI, Voices, voice_map};

/// The lists the Python Host ships with.
fn lists() -> &'static Value {
    static LISTS: OnceLock<Value> = OnceLock::new();
    LISTS.get_or_init(|| {
        serde_json::from_str(include_str!("voice-lists.json")).unwrap_or(Value::Null)
    })
}

/// An agent as the catalogue needs it.
#[derive(Debug, Clone, Copy)]
pub struct Agent<'a> {
    pub session: &'a str,
    /// What it is called. Empty: it has no name, and a voice it has is said to be its
    /// session's.
    pub name: &'a str,
    /// What it is like: whose voices it speaks in when it was given none of its own and
    /// has no name to go by.
    pub persona: &'a str,
    /// The voices it was given, as they are stored.
    pub voice_id: &'a str,
}

/// What the catalogue is built from besides the agents.
pub struct Speech<'a> {
    /// The engine every agent speaks with, and the one tried after it (`none`: none).
    pub provider: &'a str,
    pub fallback: &'a str,
    /// Whether the Host has an engine's key.
    pub has_key: &'a dyn Fn(&str) -> bool,
    /// Why Google's voices cannot be used, in the Python Host's code for it. `None`: they
    /// can.
    pub google_error: Option<&'a str>,
    /// The owner's lists of voices for his personas.
    pub voices: &'a Voices,
    /// Cartesia's own list of voices, as its interface gave it. Empty: none was fetched.
    pub cartesia: &'a [Value],
}

/// A text as it stands in an address.
fn in_address(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'_' | b'.' | b'-' | b'~' => {
                out.push(char::from(byte));
            }
            other => out.push_str(&format!("%{other:02X}")),
        }
    }
    out
}

/// The voice an agent was given for an engine.
fn stored(agent: &Agent<'_>, provider: &str) -> Option<String> {
    let given = voice_map(Some(agent.voice_id)).into_iter();
    let found = given.rev().find(|(known, _)| known == provider);
    found
        .map(|(_, voice)| voice)
        .filter(|voice| !voice.is_empty())
}

/// Who has which voice of an engine, the agent that is looked at left out: a voice with
/// what its agent is called.
fn owners(agents: &[Agent<'_>], provider: &str, current: &str) -> Vec<(String, String)> {
    let mut owned: Vec<(String, String)> = Vec::new();
    for agent in agents.iter().filter(|agent| agent.session != current) {
        let Some(voice) = stored(agent, provider) else {
            continue;
        };
        let called = if agent.name.is_empty() {
            agent.session
        } else {
            agent.name
        };
        match owned.iter_mut().find(|(known, _)| *known == voice) {
            Some(entry) => entry.1 = called.to_string(),
            None => owned.push((voice, called.to_string())),
        }
    }
    owned
}

/// The rows of one engine's voices: each with who has it, and whether it is the voice of
/// the agent that is looked at.
fn rows(
    provider: &str,
    voices: &[(String, String, String)],
    agents: &[Agent<'_>],
    current: &str,
    current_voice: Option<&str>,
) -> Vec<Value> {
    let owned = owners(agents, provider, current);
    let voices = voices.iter();
    voices
        .map(|(id, name, description)| {
            let taken_by = owned.iter().find(|(voice, _)| voice == id);
            json!({
                "id": id,
                "name": name,
                "description": description,
                "provider": provider,
                "taken_by": taken_by.map(|(_, called)| called),
                "current": current_voice == Some(id.as_str()),
                "preview_url": format!(
                    "/voice-preview?provider={}&id={}",
                    in_address(provider),
                    in_address(id)
                ),
            })
        })
        .collect()
}

/// A list of the data as (id, name, description).
fn listed(name: &str) -> Vec<(String, String, String)> {
    let text = |value: &Value| value.as_str().unwrap_or("").to_string();
    let rows = lists()[name].as_array().into_iter().flatten();
    rows.map(|row| (text(&row[0]), text(&row[1]), text(&row[2])))
        .collect()
}

/// Gemini's voices: its own, and the ones that were designed for the owner's personas,
/// each under the first persona that has it.
pub fn gemini_voices(owners: &Voices) -> Vec<(String, String, String)> {
    let own = lists()["gemini"].as_array().into_iter().flatten();
    let mut voices: Vec<(String, String, String)> = own
        .map(|row| {
            let name = row[0].as_str().unwrap_or("").to_string();
            let description = row[1].as_str().unwrap_or("").to_string();
            (name.clone(), name, description)
        })
        .collect();
    let prebuilt = voices.len();
    // Each persona once, as a map holds it (listed twice, the last stands), in the order
    // of the personas' names.
    let mut personas: Vec<(&str, &str)> = Vec::new();
    for (persona, voice) in &owners.gemini_voices {
        match personas.iter_mut().find(|(known, _)| known == persona) {
            Some(entry) => entry.1 = voice,
            None => personas.push((persona, voice)),
        }
    }
    personas.sort_by(|a, b| a.0.cmp(b.0));
    for (persona, voice) in personas {
        let known = |row: &(String, String, String)| row.0 == voice;
        if voices[..prebuilt].iter().any(known) || voices[prebuilt..].iter().any(known) {
            continue;
        }
        let name = format!("{persona} (designed)");
        voices.push((voice.to_string(), name, "Voice Design".to_string()));
    }
    voices
}

/// The catalogue as an app is told it, for the agent with this session (empty: for no agent
/// in particular).
pub fn catalog(agents: &[Agent<'_>], current: &str, speech: &Speech<'_>) -> Value {
    let looked_at = agents.iter().find(|agent| agent.session == current);
    let stored_for = |provider: &str| looked_at.and_then(|agent| stored(agent, provider));
    let persona = looked_at.map_or("", |agent| match agent.name {
        "" => agent.persona,
        name => name,
    });

    // The voices of each engine.
    let eleven: Vec<(String, String, String)> = lists()["elevenlabs"]
        .as_array()
        .into_iter()
        .flatten()
        .map(|row| {
            let (id, label) = (
                row["id"].as_str().unwrap_or(""),
                row["label"].as_str().unwrap_or(""),
            );
            let name = label.split(" — ").next().unwrap_or(label);
            (id.to_string(), name.to_string(), label.to_string())
        })
        .collect();
    let mut deepgram = listed("flux");
    deepgram.extend(listed("aura2"));
    let google: Vec<(String, String, String)> = lists()["google"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
        .map(|voice| {
            let name = voice.rsplit('-').next().unwrap_or(voice);
            (voice.to_string(), name.to_string(), String::new())
        })
        .collect();
    let current_gemini =
        stored_for(GEMINI).or_else(|| speech.voices.gemini_voice_for(persona).map(str::to_string));
    let current_cartesia = stored_for(CARTESIA).or_else(|| {
        speech
            .voices
            .cartesia_voice_for(persona)
            .map(str::to_string)
    });
    let cartesia: Vec<(String, String, String)> = speech
        .cartesia
        .iter()
        .map(|row| {
            let text = |name: &str| row[name].as_str().unwrap_or("");
            let description = match text("description") {
                "" => text("tagline"),
                description => description,
            };
            (
                text("id").to_string(),
                text("name").to_string(),
                description.to_string(),
            )
        })
        .collect();
    let mut google_rows = rows(
        "google",
        &google,
        agents,
        current,
        stored_for("google").as_deref(),
    );
    for row in &mut google_rows {
        // Which language a voice speaks is the first part of its name.
        let id = row["id"].as_str().unwrap_or("").to_string();
        let language = id
            .rsplit_once("-Chirp3-HD-")
            .map_or(id.as_str(), |(language, _)| language);
        row["language"] = Value::String(language.to_string());
    }
    let voices_of = |provider: &str| -> Vec<Value> {
        match provider {
            ELEVENLABS => rows(
                provider,
                &eleven,
                agents,
                current,
                stored_for(ELEVENLABS).as_deref(),
            ),
            "deepgram" => rows(
                provider,
                &deepgram,
                agents,
                current,
                stored_for("deepgram").as_deref(),
            ),
            GEMINI => rows(
                provider,
                &gemini_voices(speech.voices),
                agents,
                current,
                looked_at.and(current_gemini.as_deref()),
            ),
            CARTESIA => rows(
                provider,
                &cartesia,
                agents,
                current,
                looked_at.and(current_cartesia.as_deref()),
            ),
            "google" => google_rows.clone(),
            _ => Vec::new(),
        }
    };

    let mut providers = Vec::new();
    for row in lists()["providers"].as_array().into_iter().flatten() {
        let provider = row["id"].as_str().unwrap_or("");
        if provider == "none" {
            continue;
        }
        let error = match provider {
            "google" => speech.google_error,
            _ => None,
        };
        let usable = match provider {
            "google" => error.is_none(),
            provider => (speech.has_key)(provider),
        };
        let truth = |name: &str, otherwise: bool| row[name].as_bool().unwrap_or(otherwise);
        let mut entry = Map::new();
        entry.insert("id".to_string(), json!(provider));
        entry.insert("name".to_string(), row["name"].clone());
        entry.insert("description".to_string(), json!(""));
        entry.insert("kind".to_string(), row["kind"].clone());
        entry.insert("custom".to_string(), json!(truth("custom", false)));
        entry.insert(
            "supports_preview".to_string(),
            json!(truth("supports_preview", true)),
        );
        entry.insert(
            "allows_custom_voice".to_string(),
            json!(truth("allows_custom_voice", false)),
        );
        entry.insert("available".to_string(), json!(usable && error.is_none()));
        entry.insert("installed".to_string(), json!(true));
        entry.insert("selected".to_string(), json!(provider == speech.provider));
        entry.insert("fallback".to_string(), json!(provider == speech.fallback));
        entry.insert("voices".to_string(), Value::Array(voices_of(provider)));
        entry.insert("error".to_string(), json!(error));
        if let Some(capabilities) = row.get("capabilities").filter(|can| can.is_object()) {
            entry.insert("capabilities".to_string(), capabilities.clone());
        }
        providers.push(Value::Object(entry));
    }
    let speaks = |row: &Value| {
        row["available"] == true && (row["selected"] == true || row["fallback"] == true)
    };
    json!({
        "provider": speech.provider,
        "fallback": speech.fallback,
        "voice_available": providers.iter().any(speaks),
        "providers": providers,
    })
}

/// The engines that speak, as the old settings screen reads them (`GET /tts/providers`,
/// the Python Host's `tts_providers.status`): each as the Host's list has it, with whether
/// it can be used, whether it is the one every agent speaks with and whether it is the one
/// tried after that; and "no voice" last. `google_source` is which of the owner's sections
/// Google would sign in by (`google_tts` or `google_stt`), never a file or a project.
pub fn status(speech: &Speech<'_>, google_source: &str) -> Value {
    let rows = lists()["providers"].as_array().into_iter().flatten();
    let providers: Vec<Value> = rows
        .map(|row| {
            let provider = row["id"].as_str().unwrap_or("");
            let mut entry = row.as_object().cloned().unwrap_or_default();
            let available = match provider {
                "google" => speech.google_error.is_none(),
                CARTESIA | ELEVENLABS | GEMINI | "deepgram" => (speech.has_key)(provider),
                _ => row["installed"].as_bool().unwrap_or(true),
            };
            if provider == "google" {
                entry.insert("credential_source".to_string(), json!(google_source));
                entry.insert("error".to_string(), json!(speech.google_error));
            }
            entry.insert("available".to_string(), json!(available));
            entry.insert("installed".to_string(), json!(true));
            entry.insert("selected".to_string(), json!(provider == speech.provider));
            entry.insert("fallback".to_string(), json!(provider == speech.fallback));
            Value::Object(entry)
        })
        .collect();
    json!({
        "provider": speech.provider,
        "fallback": speech.fallback,
        "voice": "",
        "providers": providers,
    })
}

/// The ElevenLabs voices of the settings screen, each with who has it: `GET /voices`. A
/// voice is somebody's when it is what that agent has stored, whole; the agent that is
/// looked at does not take its own voice.
pub fn settings_voices(agents: &[Agent<'_>], current: &str) -> Vec<Value> {
    let own = agents.iter().find(|agent| agent.session == current);
    let own = own.map(|agent| agent.voice_id);
    let listed = lists()["elevenlabs"].as_array().into_iter().flatten();
    listed
        .map(|row| {
            let id = row["id"].as_str().unwrap_or("");
            let holder = agents.iter().find(|agent| agent.voice_id == id);
            let taken_by = holder.filter(|_| own != Some(id)).map(|agent| {
                if agent.name.is_empty() {
                    agent.session
                } else {
                    agent.name
                }
            });
            json!({ "id": id, "label": row["label"], "taken_by": taken_by })
        })
        .collect()
}

/// Google's voice for every agent without a Google voice of its own.
pub const GOOGLE_DEFAULT_VOICE: &str = "nb-NO-Chirp3-HD-Kore";

/// A provider of speech as the catalogue names it, and whether its first audio comes before
/// the whole clip is made. `None`: the catalogue does not list it.
pub fn provider_row(provider: &str) -> Option<(String, bool)> {
    let rows = lists()["providers"].as_array().into_iter().flatten();
    let mut rows = rows.filter(|row| row["id"] == provider);
    let row = rows.next()?;
    let name = row["name"].as_str().unwrap_or(provider).to_string();
    Some((name, row["streaming"].as_bool().unwrap_or(false)))
}

/// The names of the voices the Host ships with, by provider and voice: ElevenLabs' (the
/// label before its description), Deepgram's, and Google's (the last part of its id).
pub fn shipped_labels()
-> std::collections::BTreeMap<String, std::collections::BTreeMap<String, String>> {
    let lists = lists();
    let mut named = std::collections::BTreeMap::new();
    let eleven = lists["elevenlabs"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|row| {
            let label = row["label"].as_str()?;
            let name = label.split(" \u{2014} ").next().unwrap_or(label);
            Some((row["id"].as_str()?.to_string(), name.to_string()))
        });
    named.insert(ELEVENLABS.to_string(), eleven.collect());
    let deepgram = ["flux", "aura2"].into_iter().flat_map(|list| {
        let rows = lists[list].as_array().into_iter().flatten();
        rows.filter_map(|row| Some((row[0].as_str()?.to_string(), row[1].as_str()?.to_string())))
    });
    named.insert("deepgram".to_string(), deepgram.collect());
    let google = lists["google"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(Value::as_str);
    let google = google.map(|voice| {
        let name = voice.rsplit_once('-').map_or(voice, |(_, name)| name);
        (voice.to_string(), name.to_string())
    });
    named.insert("google".to_string(), google.collect());
    named
}

/// Whether an engine has a voice with this id for an agent to be given: for Google, whose
/// voices are a closed list.
pub fn google_has(voice: &str) -> bool {
    let voices = lists()["google"].as_array().into_iter().flatten();
    voices.filter_map(Value::as_str).any(|known| known == voice)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_lists_the_python_host_ships_with_are_carried_whole() {
        let lists = lists();
        let count = |name: &str| lists[name].as_array().map_or(0, Vec::len);
        assert_eq!(
            (
                count("providers"),
                count("gemini"),
                count("flux"),
                count("aura2"),
                count("elevenlabs"),
                count("google")
            ),
            (6, 30, 36, 41, 29, 30)
        );
        assert!(google_has("nb-NO-Chirp3-HD-Kore") && !google_has("Kore"));
        assert_eq!(in_address("voice one/2"), "voice%20one%2F2");
    }
}
