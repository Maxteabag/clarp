//! Compare tutorial lines, voice selection and cache keys with the Python Host.
#![allow(clippy::unwrap_used)]
use clarp_kernel::{controller_narration as narration, said::Voices};
use serde_json::Value;

#[test]
fn tutorial_lines_match_the_reference() {
    let rows: Vec<Value> =
        serde_json::from_str(include_str!("fixtures/controller-narration-vectors.json")).unwrap();
    let mut keys = 0;
    for row in &rows {
        let line = narration::text(row[0].as_str().unwrap());
        assert_eq!(line.as_deref(), row[3].as_str(), "{row}");
        let voices =
            Voices::default().with_cartesia(&[("Rachel".into(), row[1].as_str().unwrap().into())]);
        let voice = voices.cartesia_voice_for(narration::PERSONA).unwrap_or("");
        assert_eq!(voice, row[4].as_str().unwrap());
        if let (Some(line), Some(expected)) = (line, row[5].as_str()) {
            assert_eq!(
                narration::cache_key(&line, voice, row[2].as_str().unwrap()),
                expected
            );
            keys += 1;
        }
    }
    assert_eq!((rows.len(), keys), (156, 93));
}
