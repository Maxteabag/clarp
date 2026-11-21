#![allow(clippy::unwrap_used)]

use clarp_kernel::{said, voice_migration, voice_profile::HostView};
use serde_json::Value;

#[test]
fn voice_maps_and_the_audible_migration_match_the_reference() {
    let vectors: Value =
        serde_json::from_str(include_str!("fixtures/voice-map-vectors.json")).unwrap();
    for row in vectors["canonical"].as_array().unwrap() {
        assert_eq!(
            said::canonical_voice(row["input"].as_str().unwrap()),
            row["output"]
        );
    }
    for row in vectors["merges"].as_array().unwrap() {
        assert_eq!(
            said::merge_provider_maps(
                row["stored"].as_str().unwrap(),
                row["incoming"].as_str().unwrap(),
                row["base"].as_str()
            ),
            row["output"],
            "{row}"
        );
    }
    let host = HostView {
        voices: said::Voices {
            provider: "cartesia".into(),
            fallback: "elevenlabs".into(),
            ..Default::default()
        },
        keys: vec!["cartesia".into(), "elevenlabs".into()],
        agent_engines: vec![
            ("one".into(), "elevenlabs:scribe_v2".into()),
            ("a3".into(), "deepgram:nova-3".into()),
        ],
        agent_languages: vec![
            ("two".into(), "no,en".into()),
            ("second".into(), "en".into()),
        ],
        ..Default::default()
    };
    let mut agents = vectors["agents"].as_array().unwrap().clone();
    for row in &mut agents {
        row["name"] = row["persona"].clone();
    }
    let mut contacts = vectors["contacts"].as_array().unwrap().clone();
    for row in &mut contacts {
        row["id"] = row["persona_id"].clone();
    }
    assert_eq!(
        voice_migration::plan(&host, &agents, &contacts),
        vectors["report"]
    );
}
