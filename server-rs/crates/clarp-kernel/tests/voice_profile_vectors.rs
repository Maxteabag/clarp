//! An agent's voice and hearing as the Host resolves them, against the Python Host's.
//!
//! `fixtures/voice-profile-vectors.json` is written by `server-rs/tools/voice-profile-vectors.py`
//! from the Python Host's own `voice_profile.py`: what each agent's next clip is spoken with or
//! why nothing is heard, the voice each provider is handed and where it came from, how an agent
//! is heard, what a new agent gets, who shares a voice, and the words of a clip not spoken. The
//! same must be resolved here, and what the speaker hands over must be what the profile shows.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::heard;
use clarp_kernel::languages;
use clarp_kernel::said::{Voices, Who};
use clarp_kernel::voice_profile::{
    self, Agent, HostView, Row, defaults, elevenlabs_id_usable, engine_label, hearing,
    not_spoken_words, profiles, provider_row, provider_voice, speaking_order, speech,
};
use clarp_kernel::voices::{GOOGLE_DEFAULT_VOICE, google_has};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/voice-profile-vectors.json")).unwrap()
}

fn pairs(map: &Value) -> Vec<(String, String)> {
    let fields = map.as_object().unwrap().iter();
    fields
        .map(|(key, value)| (key.clone(), value.as_str().unwrap().to_string()))
        .collect()
}

fn labels(vectors: &Value) -> BTreeMap<String, BTreeMap<String, String>> {
    let named = vectors["labels"].as_object().unwrap().iter();
    named
        .map(|(provider, voices)| (provider.clone(), pairs(voices).into_iter().collect()))
        .collect()
}

/// The Host as a vector says it is.
fn host(view: &Value, labels: &BTreeMap<String, BTreeMap<String, String>>) -> HostView {
    let set = &view["settings"];
    let text = |key: &str| set[key].as_str().unwrap().to_string();
    let entries: Vec<(String, Vec<(String, String)>)> = set["tts_agent_overrides"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(name, entry)| (name.clone(), pairs(entry)))
        .collect();
    let borrowed: Vec<(&str, Vec<(&str, &str)>)> = entries
        .iter()
        .map(|(name, entry)| {
            (
                name.as_str(),
                entry
                    .iter()
                    .map(|(k, v)| (k.as_str(), v.as_str()))
                    .collect(),
            )
        })
        .collect();
    let given: Vec<(&str, &[(&str, &str)])> = borrowed
        .iter()
        .map(|(name, entry)| (*name, entry.as_slice()))
        .collect();
    let voices = Voices {
        provider: text("tts_provider"),
        fallback: text("tts_fallback"),
        gemini_voices: pairs(&set["gemini_voices"]),
        cartesia_voices: pairs(&set["cartesia_voices"]),
        gemini_voice: text("gemini_voice"),
        ..Voices::default()
    }
    .with_agents(&given);
    let keys = view["keys"].as_object().unwrap().iter();
    let engine = view["engine"].as_str().unwrap();
    let own_engine = if set["whisper_enabled"] == true {
        format!("{}:{}", text("whisper_provider"), text("whisper_model"))
    } else {
        String::new()
    };
    HostView {
        voices,
        deepgram_model: text("deepgram_model"),
        keys: keys
            .filter(|(_, has)| **has == true)
            .map(|(key, _)| key.clone())
            .collect(),
        google_refusal: view["google_refusal"].as_str().map(str::to_string),
        labels: labels.clone(),
        cloud_engine: if heard::is_cloud(engine) {
            engine.to_string()
        } else {
            String::new()
        },
        own_engine,
        language: languages::selected(view["language"].as_str(), "en"),
        agent_engines: languages::stored(Some(&view["agent_engines"].to_string())),
        agent_languages: languages::languages(Some(&view["agent_languages"].to_string())),
    }
}

/// An agent as a vector has it: under the names the settings may know it by.
fn agent(row: &Value) -> Agent<'_> {
    let named = |key: &str| row.get(key).and_then(Value::as_str).unwrap_or("");
    Agent {
        who: Who {
            name: named("name"),
            persona: named("persona"),
            session: named("session"),
        },
        stored: row["voice_id"].as_str(),
    }
}

fn told(shown: &voice_profile::Speech) -> Value {
    Value::Object(shown.told())
}

#[test]
fn an_agent_speaks_with_the_voice_the_python_host_would_show() {
    let vectors = vectors();
    let labels = labels(&vectors);
    let mut checked = 0;
    let mut handed = 0;
    for case in vectors["speech"].as_array().unwrap() {
        let view = host(&case["view"], &labels);
        let agent = agent(&case["agent"]);
        assert_eq!(
            told(&speech(&view, agent)),
            case["speech"],
            "{}",
            case["name"]
        );
        let route = view.voices.route(agent.who);
        for (provider, expected) in case["handed"].as_object().unwrap() {
            let choice = provider_voice(&view, agent, &route, provider);
            let made = json!({ "voice": choice.voice, "origin": choice.origin });
            assert_eq!(&made, expected, "{provider} of {}", case["name"]);
            handed += 1;
        }
        checked += 1;
    }
    assert_eq!((checked, handed), (576, 2880));
}

/// What the speaker hands over is the profile's voice, and it hands over nothing exactly when
/// the profile says why: for every case of the vectors.
#[test]
fn the_speaker_hands_over_what_the_profile_shows_and_nothing_when_it_is_silent() {
    let vectors = vectors();
    let labels = labels(&vectors);
    let (mut spoken, mut silent) = (0, 0);
    for case in vectors["speech"].as_array().unwrap() {
        let view = host(&case["view"], &labels);
        let agent = agent(&case["agent"]);
        let shown = speech(&view, agent);
        match (speaking_order(&view, agent), &shown.voice_id) {
            (Ok(order), Some(voice)) => {
                assert!(shown.silent_reason.is_none(), "{}", case["name"]);
                assert_eq!(
                    order[0],
                    (shown.provider.clone(), voice.clone()),
                    "{}",
                    case["name"]
                );
                spoken += 1;
            }
            (Err(unheard), None) => {
                assert_eq!(
                    Some(unheard.reason),
                    shown.silent_reason,
                    "{}",
                    case["name"]
                );
                assert_eq!(unheard.provider, shown.provider, "{}", case["name"]);
                silent += 1;
            }
            (order, _) => panic!(
                "{}: the speaker {order:?}, the profile {shown:?}",
                case["name"]
            ),
        }
    }
    assert_eq!((spoken, silent), (SPOKEN, SILENT));
}

const SPOKEN: usize = 410;
const SILENT: usize = 166;

/// ElevenLabs is handed only an id it can take. An agent that spoke with ElevenLabs before
/// (it was handed a usable id) still speaks with the same voice; one that was handed a JSON
/// text, a uuid or nothing, which ElevenLabs refused, is silent now and says so.
#[test]
fn every_stored_voice_that_spoke_with_elevenlabs_still_does() {
    let vectors = vectors();
    let labels = labels(&vectors);
    let (mut still, mut refused) = (0, 0);
    let table = vectors["speech"].as_array().unwrap().iter();
    for case in table.filter(|case| {
        case["name"]
            .as_str()
            .unwrap()
            .starts_with("table elevenlabs / none / ")
    }) {
        let view = host(&case["view"], &labels);
        let agent = agent(&case["agent"]);
        let route = view.voices.route(agent.who);
        // What the speaker was handed before: what said.rs works out for a clip.
        let before = view
            .voices
            .for_clip(agent.stored, agent.who, &route)
            .elevenlabs;
        let shown = speech(&view, agent);
        if elevenlabs_id_usable(before.as_deref()) {
            assert_eq!(
                (shown.provider.as_str(), shown.voice_id.clone()),
                ("elevenlabs", before),
                "{}",
                case["name"]
            );
            still += 1;
        } else {
            assert_eq!(
                shown.silent_reason,
                Some(voice_profile::NO_VOICE),
                "{}",
                case["name"]
            );
            refused += 1;
        }
    }
    assert_eq!((still, refused), (STILL, REFUSED));
}

const STILL: usize = 12;
const REFUSED: usize = 24;

#[test]
fn elevenlabs_takes_the_ids_the_python_host_takes() {
    let vectors = vectors();
    let cases = vectors["usable"].as_array().unwrap();
    for case in cases {
        let usable = elevenlabs_id_usable(case["text"].as_str());
        assert_eq!(usable, case["usable"].as_bool().unwrap(), "{case}");
    }
    assert_eq!(cases.len(), 19);
}

#[test]
fn an_agent_is_heard_as_the_python_host_would_show() {
    let vectors = vectors();
    let labels = labels(&vectors);
    let cases = vectors["hearing"].as_array().unwrap();
    for case in cases {
        let view = host(&case["view"], &labels);
        assert_eq!(
            hearing(&view, agent(&case["agent"]).who),
            case["hearing"],
            "{case}"
        );
    }
    assert_eq!(cases.len(), 1517);
    let named = vectors["engine_labels"].as_object().unwrap();
    for (engine, label) in named {
        assert_eq!(engine_label(engine), label.as_str().unwrap(), "{engine}");
    }
    assert_eq!(named.len(), 7);
}

#[test]
fn a_new_agent_gets_what_the_python_host_would_show() {
    let vectors = vectors();
    let labels = labels(&vectors);
    let cases = vectors["defaults"].as_array().unwrap();
    for case in cases {
        assert_eq!(
            defaults(&host(&case["view"], &labels)),
            case["defaults"],
            "{case}"
        );
    }
    assert_eq!(cases.len(), 12);
}

#[test]
fn who_shares_a_voice_is_counted_as_the_python_host_counts_it() {
    let vectors = vectors();
    let labels = labels(&vectors);
    let mut rows_checked = 0;
    for roster in vectors["rosters"].as_array().unwrap() {
        let view = host(&roster["view"], &labels);
        let agents = roster["agents"].as_array().unwrap();
        let rows: Vec<Row<'_>> = agents
            .iter()
            .map(|row| Row {
                agent: agent(row),
                inactive: !row["archived_at"].is_null()
                    || row["is_janitor"].as_i64().unwrap_or(0) != 0,
            })
            .collect();
        let made = profiles(&view, &rows);
        assert_eq!(
            made,
            roster["profiles"].as_array().unwrap().clone(),
            "{roster}"
        );
        rows_checked += made.len();
    }
    assert_eq!(rows_checked, 23);
}

#[test]
fn a_clip_not_spoken_is_said_in_the_python_hosts_words() {
    let vectors = vectors();
    let cases = vectors["words"].as_array().unwrap();
    for case in cases {
        let said = |key: &str| case[key].as_str().unwrap();
        let words = not_spoken_words(
            said("reason"),
            said("provider_label"),
            said("name"),
            case["partly"] == true,
        );
        assert_eq!(words, said("words"), "{case}");
    }
    assert_eq!(cases.len(), 48);
    for (code, reason) in vectors["google_reasons"].as_object().unwrap() {
        assert_eq!(
            voice_profile::google_reason(code),
            reason.as_str(),
            "{code}"
        );
    }
    assert_eq!(voice_profile::google_reason("google_quota"), None);
    for (code, words) in vectors["google_words"].as_object().unwrap() {
        assert_eq!(voice_profile::google_words(code), words.as_str(), "{code}");
    }
}

/// The catalogue the profile names providers and voices from is the Python Host's.
#[test]
fn the_providers_and_voices_are_named_as_the_python_host_names_them() {
    let vectors = vectors();
    let rows = vectors["providers"].as_array().unwrap();
    for row in rows {
        let id = row["id"].as_str().unwrap();
        let named = (
            row["name"].as_str().unwrap().to_string(),
            row["streaming"].as_bool().unwrap(),
        );
        assert_eq!(provider_row(id), named, "{id}");
    }
    assert_eq!(rows.len(), 6);
    assert_eq!(vectors["google_default"], GOOGLE_DEFAULT_VOICE);
    let google = vectors["google_voices"].as_array().unwrap();
    assert!(
        google
            .iter()
            .all(|voice| google_has(voice.as_str().unwrap()))
    );
    // The names of the voices the Host ships with are the stand-in catalogue's where that
    // takes them from the same lists (Google's: the last part of the id).
    let shipped = voice_profile::shipped_labels();
    for (voice, name) in vectors["labels"]["google"].as_object().unwrap() {
        assert_eq!(
            shipped["google"].get(voice).map(String::as_str),
            name.as_str(),
            "{voice}"
        );
    }
    assert_eq!(
        shipped["elevenlabs"]
            .get("nPczCjzI2devNBz1zQrb")
            .map(String::as_str),
        Some("Brian")
    );
    assert_eq!(
        shipped["deepgram"].get("flux-haley-en").map(String::as_str),
        Some("Haley")
    );
    let sets = vectors["language_set_providers"].as_array().unwrap();
    let ours: Vec<Value> = clarp_kernel::languages::SET_PROVIDERS
        .iter()
        .map(|id| json!(id))
        .collect();
    assert_eq!(sets, &ours);
}
