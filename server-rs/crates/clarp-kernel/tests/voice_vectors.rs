//! The rules of speech out, against the Python Host's.
//!
//! `fixtures/voice-vectors.json` is written by `server-rs/tools/voice-vectors.py` from the
//! Python Host's own functions: what an agent's stored voice means for each provider, which
//! engine says an agent's clip and which is tried after it, which voice a persona has, and
//! what a clip's file is called. The same must be decided here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::said::{
    BUILT_IN_CARTESIA_VOICES, Voices, Who, clip_name, clip_named, merge_voice, resolve_voice,
    voice_for, voice_map,
};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/voice-vectors.json")).unwrap()
}

fn pairs(map: &Value) -> Vec<(String, String)> {
    let fields = map.as_object().unwrap().iter();
    fields
        .map(|(key, value)| (key.clone(), value.as_str().unwrap().to_string()))
        .collect()
}

/// The vectors are written with their keys in order of the alphabet, so the order in which
/// a map was stored is not in them: what is held to them is what is in a map, in that order.
fn sorted(mut pairs: Vec<(String, String)>) -> Vec<(String, String)> {
    pairs.sort();
    pairs
}

/// An agent as a vector has it: under the names the settings may know it by.
fn who(agent: &Value) -> Who<'_> {
    let named = |key: &str| agent.get(key).and_then(Value::as_str).unwrap_or("");
    Who {
        agent_id: "",
        name: named("name"),
        persona: named("persona"),
        session: named("session"),
    }
}

/// The entries of single agents as a vector has them, in the form they are written in.
fn entries(overrides: &Value) -> Vec<(String, Vec<(String, String)>)> {
    let named = overrides.as_object().unwrap().iter();
    named
        .map(|(name, entry)| (name.clone(), pairs(entry)))
        .collect()
}

fn with_agents(voices: Voices, overrides: &Value) -> Voices {
    let owned = entries(overrides);
    let borrowed: Vec<(&str, Vec<(&str, &str)>)> = owned
        .iter()
        .map(|(name, entry)| {
            let set = entry
                .iter()
                .map(|(key, value)| (key.as_str(), value.as_str()));
            (name.as_str(), set.collect())
        })
        .collect();
    let given: Vec<(&str, &[(&str, &str)])> = borrowed
        .iter()
        .map(|(name, entry)| (*name, entry.as_slice()))
        .collect();
    voices.with_agents(&given)
}

#[test]
fn an_agents_stored_voice_means_what_the_python_host_reads_in_it() {
    let vectors = vectors();
    let stored = &vectors["stored_voices"];
    let cases = stored["maps"].as_array().unwrap();
    assert!(cases.len() >= 19);
    for case in cases {
        assert_eq!(
            sorted(voice_map(case["raw"].as_str())),
            pairs(&case["map"]),
            "{case}"
        );
    }
    let cases = stored["resolved"].as_array().unwrap();
    assert!(cases.len() >= 114);
    for case in cases {
        let voice = resolve_voice(case["raw"].as_str(), case["provider"].as_str().unwrap());
        assert_eq!(voice.as_deref(), case["voice"].as_str(), "{case}");
    }
    let cases = stored["merged"].as_array().unwrap();
    assert!(cases.len() >= 114);
    for case in cases {
        let said = |key: &str| case[key].as_str().unwrap();
        let merged = merge_voice(case["raw"].as_str(), said("provider"), said("voice"));
        assert_eq!(merged, said("merged"), "{case}");
    }
}

#[test]
fn a_clip_is_said_by_the_engine_the_python_host_would_choose() {
    let vectors = vectors();
    let cases = vectors["routes"].as_array().unwrap();
    assert!(cases.len() >= 360);
    for case in cases {
        let said = |key: &str| case[key].as_str().unwrap().to_string();
        let settings = Voices {
            provider: said("provider"),
            fallback: said("fallback"),
            ..Voices::default()
        };
        let settings = with_agents(settings, &case["overrides"]);
        let agent = &case["agent"];
        let agent = if agent.is_null() {
            Who::default()
        } else {
            who(agent)
        };
        assert_eq!(
            sorted(settings.entry_for(agent).to_vec()),
            pairs(&case["override"]),
            "{case}"
        );
        let route = settings.route(agent);
        let expected = &case["route"];
        assert_eq!(
            route.provider,
            expected["provider"].as_str().unwrap(),
            "{case}"
        );
        assert_eq!(route.voice.as_deref(), expected["voice"].as_str(), "{case}");
        assert_eq!(
            route.fallback,
            expected["fallback"].as_str().unwrap(),
            "{case}"
        );
    }
}

#[test]
fn a_persona_has_the_voice_the_python_host_would_give_it() {
    let vectors = vectors();
    let about = &vectors["persona_voices"];
    let listed = Voices {
        gemini_voices: pairs(&about["gemini_voices"]),
        cartesia_voices: pairs(&about["cartesia_voices"]),
        ..Voices::default()
    };
    for case in about["gemini_for"].as_array().unwrap() {
        let persona = case["persona"].as_str().unwrap_or("");
        assert_eq!(
            listed.gemini_voice_for(persona),
            case["voice"].as_str(),
            "{case}"
        );
    }
    for case in about["cartesia_for"].as_array().unwrap() {
        let persona = case["persona"].as_str().unwrap_or("");
        assert_eq!(
            listed.cartesia_voice_for(persona),
            case["voice"].as_str(),
            "{case}"
        );
    }
    for case in about["gemini_for_cartesia"].as_array().unwrap() {
        let voice = case["cartesia_voice"].as_str().unwrap_or("");
        assert_eq!(
            listed.gemini_voice_for_cartesia(voice),
            case["voice"].as_str(),
            "{case}"
        );
    }
    let cases = about["gemini_voice"].as_array().unwrap();
    assert!(cases.len() >= 384);
    for case in cases {
        let settings = Voices {
            gemini_voice: case["default"].as_str().unwrap().to_string(),
            ..listed.clone()
        };
        let settings = with_agents(settings, &case["overrides"]);
        let persona = case["persona"].as_str().unwrap();
        let session = persona.to_lowercase();
        let agent = Who {
            agent_id: "",
            name: "",
            persona,
            session: &session,
        };
        let voice = settings.gemini_voice(
            case["raw"].as_str(),
            agent,
            case["route_voice"].as_str(),
            case["cartesia_voice"].as_str(),
        );
        // No voice is no voice, whether the Python Host says so with nothing or with an
        // empty text.
        assert_eq!(
            voice.as_deref().unwrap_or(""),
            case["voice"].as_str().unwrap_or(""),
            "{case}"
        );
    }
}

#[test]
fn a_clip_is_called_what_the_python_host_calls_it() {
    let vectors = vectors();
    let names = &vectors["clip_names"];
    for case in names["made"].as_array().unwrap() {
        let name = clip_name(case["session"].as_str(), case["now_ms"].as_i64().unwrap());
        assert_eq!(name, case["name"].as_str().unwrap(), "{case}");
    }
    for case in names["parsed"].as_array().unwrap() {
        let read = clip_named(case["name"].as_str().unwrap());
        let expected = case["ts"]
            .as_i64()
            .map(|made| (made, case["session"].as_str().map(str::to_string)));
        assert_eq!(read, expected, "{case}");
    }
}

#[test]
fn the_voices_of_one_clip_are_the_ones_the_python_host_would_work_out() {
    let vectors = vectors();
    let about = &vectors["clip_voices"];
    let listed = Voices {
        gemini_voices: pairs(&about["gemini_voices"]),
        gemini_vertex_voices: pairs(&about["vertex_voices"]),
        cartesia_voices: pairs(&about["cartesia_voices"]),
        ..Voices::default()
    };
    // A voice of Gemini's as each of Google's doors knows it.
    let doors = about["doors"].as_array().unwrap();
    assert!(doors.len() >= 16);
    for case in doors {
        let said = |key: &str| case[key].as_str().unwrap();
        let known = listed.gemini_voice_at(said("voice"), said("backend") == "vertex");
        assert_eq!(known, said("as_known"), "{case}");
    }
    // The voices one clip can be spoken in, by provider.
    let cases = about["chosen"].as_array().unwrap();
    assert!(cases.len() >= 72);
    for case in cases {
        let settings = Voices {
            provider: case["provider"].as_str().unwrap().to_string(),
            ..listed.clone()
        };
        let settings = with_agents(settings, &about["overrides"]);
        let persona = case["persona"].as_str().unwrap();
        let session = persona.to_lowercase();
        let agent = Who {
            agent_id: "",
            name: "",
            persona,
            session: &session,
        };
        let route = settings.route(agent);
        let expected = &case["route"];
        assert_eq!(
            route.provider,
            expected["provider"].as_str().unwrap(),
            "{case}"
        );
        assert_eq!(route.voice.as_deref(), expected["voice"].as_str(), "{case}");
        let stored = case["raw"].as_str();
        for (provider, voice) in case["by_provider"].as_object().unwrap() {
            let found = voice_for(stored, &route, provider);
            assert_eq!(found.as_deref(), voice.as_str(), "{provider} of {case}");
        }
        let voices = settings.for_clip(stored, agent, &route);
        assert_eq!(
            voices.cartesia.as_deref(),
            case["cartesia_voice"].as_str(),
            "{case}"
        );
        assert_eq!(
            voices.elevenlabs.as_deref(),
            case["eleven_voice"].as_str(),
            "{case}"
        );
    }
}

#[test]
fn the_voices_the_python_host_ships_with_are_there_and_an_owners_list_adds_to_them() {
    let vectors = vectors();
    let shipped: Vec<(String, String)> = vectors["built_in_cartesia_voices"]
        .as_array()
        .unwrap()
        .iter()
        .map(|pair| {
            let said = |at: usize| pair[at].as_str().unwrap().to_string();
            (said(0), said(1))
        })
        .collect();
    assert!(shipped.len() >= 74);
    let mine: Vec<(String, String)> = BUILT_IN_CARTESIA_VOICES
        .iter()
        .map(|(persona, voice)| ((*persona).to_string(), (*voice).to_string()))
        .collect();
    assert_eq!(mine, shipped);
    // An owner's own list: a voice of his own for a persona that is shipped, and a persona
    // of his own. The others stay.
    let own = [
        ("Rachel".to_string(), "ca-own-rachel".to_string()),
        ("Maximus".to_string(), "ca-own-maximus".to_string()),
    ];
    let voices = Voices::default().with_cartesia(&own);
    assert_eq!(voices.cartesia_voice_for("Rachel"), Some("ca-own-rachel"));
    assert_eq!(voices.cartesia_voice_for("Maximus"), Some("ca-own-maximus"));
    assert_eq!(
        voices.cartesia_voice_for("Josh"),
        Some(shipped[1].1.as_str())
    );
    assert_eq!(voices.cartesia_voices.len(), shipped.len() + 1);
    // An agent nobody named speaks with the voice of what it is.
    assert_eq!(
        voices.cartesia_voice_for("Claude-5628"),
        voices.cartesia_voice_for("Claude")
    );
    assert!(voices.cartesia_voice_for("Claude").is_some());
}
