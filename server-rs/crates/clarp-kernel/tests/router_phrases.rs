#![allow(clippy::unwrap_used)]
use clarp_kernel::router::{flow::phrase_key, phrases::*};
use serde_json::Value;

#[test]
fn fixed_words_and_playback_match_the_reference() {
    let fixture: Value = serde_json::from_str(include_str!("fixtures/router-phrases.json")).unwrap();
    for (key, text) in fixture["table"].as_object().unwrap() {
        assert_eq!(phrase_key(text.as_str().unwrap()), Some(key.as_str()));
    }
    let rows = fixture["keys"].as_array().unwrap();
    for row in rows {
        let own = row[3].as_bool().unwrap();
        assert_eq!(phrase_key(row[0].as_str().unwrap()), if own { row[2].as_str() } else { None });
    }
    for row in fixture["events"].as_array().unwrap() {
        assert_eq!(event("fixture.mp3", "clarify_target", row["session"].as_str().unwrap(), row["agent_id"].as_str(), "app-voice"), *row);
    }
    assert_eq!((fixture["table"].as_object().unwrap().len(), rows.len(), fixture["events"].as_array().unwrap().len()), (16, 57, 3));
}

#[test]
fn eviction_keeps_the_newest_fills_within_both_bounds() {
    let row = Clip {
        key: Key { phrase: "clarify_target".into(), voice: Voice {provider:"cartesia".into(),voice:"v".into(),model:"m".into()} },
        text:"Was that for me?".into(), name:"name".into(), size:1,
    };
    let rows = vec![row.clone();1025];
    assert_eq!(excess(&rows[..1024]),0);
    assert_eq!(excess(&rows),1);
    assert_eq!((MAX_CLIP,MAX_BYTES),(262144,67108864));
    let mut rows = vec![Clip {size:262144,..row.clone()};256];
    assert_eq!(excess(&rows),0);
    rows.push(row);
    assert_eq!(excess(&rows),1);
    rows.rotate_right(1);
    assert_eq!(excess(&rows),1);
}
