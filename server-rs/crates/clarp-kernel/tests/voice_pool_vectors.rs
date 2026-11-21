//! The Python Host's V2b assignment and review cases.
#![allow(clippy::unwrap_used)]

use clarp_kernel::said::{Voices, Who};
use clarp_kernel::voice_names::Snapshot;
use clarp_kernel::voice_pool::{self, Pool, User};
use clarp_kernel::voice_profile::{self, Agent, HostView, Row};
use serde_json::{Value, json};

fn pairs(value: &Value) -> Vec<(String, String)> {
    value
        .as_object()
        .into_iter()
        .flatten()
        .map(|(key, value)| (key.clone(), value.as_str().unwrap().to_owned()))
        .collect()
}

fn text<'a>(value: &'a Value, key: &str) -> &'a str {
    value[key].as_str().unwrap_or("")
}

fn words(value: &Value) -> Vec<String> {
    value
        .as_array()
        .into_iter()
        .flatten()
        .map(|value| value.as_str().unwrap().to_owned())
        .collect()
}

#[test]
fn the_voice_pool_walk_and_occupied_counts_match_the_reference() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/voice-pool-vectors.json")).unwrap();
    let picks = vectors["picks"].as_array().unwrap();
    for case in picks {
        let pool = words(&case["pool"]);
        let taken = serde_json::from_value(case["taken"].clone()).unwrap();
        let keep = serde_json::from_value(case["keep"].clone()).unwrap();
        let pick = voice_pool::pick(text(case, "name"), &pool, &taken, &keep);
        assert_eq!(json!(pick), case["result"], "{case}");
    }

    let assignments = vectors["assignments"].as_array().unwrap();
    for case in assignments {
        let settings = &case["settings"];
        let agents = settings["tts_agent_overrides"]
            .as_object()
            .into_iter()
            .flatten()
            .map(|(name, entry)| (name.clone(), pairs(entry)))
            .collect();
        let view = HostView {
            voices: Voices {
                provider: text(settings, "tts_provider").into(),
                fallback: text(settings, "tts_fallback").into(),
                cartesia_voices: pairs(&settings["cartesia_voices"]),
                gemini_voices: pairs(&settings["gemini_voices"]),
                agents,
                ..Default::default()
            },
            ..Default::default()
        };
        let configured = words(&settings["cartesia_pool"]);
        let known = Snapshot::from_file(&json!({"_catalogue": {"cartesia":
            words(&case["catalogue"]).into_iter().map(|id| (id, json!({"language":"en","public":true})))
                .collect::<serde_json::Map<String, Value>>()
        }}));
        let roster = pairs(&settings["roster"]);
        let pool = Pool {
            host: &view,
            configured_cartesia: &configured,
            catalogue: &known,
            roster: &roster,
        };
        let agents = case["agents"]
            .as_array()
            .unwrap()
            .iter()
            .filter(|a| a["archived_at"].is_null() && a["is_janitor"].is_null())
            .map(|a| User {
                name: text(a, "persona"),
                stored: text(a, "voice_id"),
                builtin: false,
            });
        let contacts = case["contacts"].as_array().unwrap().iter().map(|a| User {
            name: text(a, "name"),
            stored: text(a, "voice_id"),
            builtin: a["builtin"] == true,
        });
        let users: Vec<_> = agents.chain(contacts).collect();
        let actual = pool.assign(text(case, "name"), text(case, "stored"), &users);
        let logs: Vec<_> = actual
            .exhausted
            .iter()
            .map(|note| {
                json!([
                    "voicePoolExhausted",
                    format!(
                        "provider={} pool={} shared_by={}",
                        note.provider, note.size, note.shared_by
                    )
                ])
            })
            .collect();
        assert_eq!(actual.stored, case["result"], "{case}");
        assert_eq!(json!(logs), case["logs"], "{case}");
    }
    assert_eq!((picks.len(), assignments.len()), (9, 18));

    let view = HostView {
        voices: Voices {
            provider: "cartesia".into(),
            cartesia_voices: vec![("Ada".into(), "c1".into())],
            ..Default::default()
        },
        ..Default::default()
    };
    let pool = Pool {
        host: &view,
        configured_cartesia: &[],
        catalogue: &Snapshot::default(),
        roster: &[],
    };
    assert_eq!(pool.pinned_before_rename("Ada", "{}"), vectors["pinned"]);
    assert_eq!(
        voice_profile::defaults(&view)["speech"]["assignment"],
        vectors["default_assignment"]
    );
    let who = Who {
        persona: "Ada",
        ..Default::default()
    };
    let row = Row {
        agent: Agent { who, stored: None },
        inactive: true,
        janitor: true,
    };
    let mut actual = voice_profile::profiles(&view, &[row]).remove(0);
    actual["speech"]
        .as_object_mut()
        .unwrap()
        .remove("shared_with");
    assert_eq!(actual["speech"], vectors["janitor"]);
}

#[test]
fn cartesia_selection_uses_public_non_owner_catalogue_voices_in_the_agents_language() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/voice-pool-vectors.json")).unwrap();
    let case = &vectors["catalogue"];
    let catalogue = Snapshot::from_file(&case["file"]);
    for row in case["languages"].as_array().unwrap() {
        let (ids, miss) = voice_pool::language_voices(&catalogue, text(row, "language"));
        assert_eq!(json!(ids), row["ids"], "{row}");
        assert_eq!(json!(miss), row["miss"], "{row}");
    }
    for row in case["quiet"].as_array().unwrap() {
        let snapshot = Snapshot::from_file(&row["file"]);
        let (ids, miss) = voice_pool::language_voices(&snapshot, text(row, "language"));
        assert_eq!(json!(ids), row["ids"], "{row}");
        assert_eq!(json!(miss), row["miss"], "{row}");
    }
    let view = HostView {
        voices: Voices {
            provider: "cartesia".into(),
            ..Default::default()
        },
        language: "en".into(),
        agent_languages: vec![("ola".into(), "nn,en".into())],
        ..Default::default()
    };
    let pool = Pool {
        host: &view,
        configured_cartesia: &[],
        catalogue: &catalogue,
        roster: &[],
    };
    let ola = pool.assign("Ola", "{}", &[]);
    assert_eq!(ola.stored, case["ola"]);
    assert!(ola.catalogue_miss.is_none());

    let other_languages = HostView {
        language: "zh".into(),
        ..view.clone()
    };
    let pool = Pool {
        host: &other_languages,
        ..pool
    };
    let fallback = pool.assign("Fixture", "{}", &[]);
    assert_ne!(fallback.stored, "{}");
    assert_eq!(json!(fallback.catalogue_miss), case["languages"][4]["miss"]);
}
