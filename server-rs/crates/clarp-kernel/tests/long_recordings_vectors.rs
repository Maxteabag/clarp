#![allow(clippy::unwrap_used)]
use clarp_kernel::{
    call_transcription::{self, Policy},
    heard::duration,
    python,
};
use serde_json::{Value, json};

#[test]
fn clip_lengths_and_engine_choices_match_the_reference() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/long-recordings-vectors.json")).unwrap();
    let mut counts = [0; 3];
    for row in fixture["wav"].as_array().unwrap() {
        let bytes = python::b64decode(row["audio"].as_str().unwrap()).unwrap();
        assert_eq!(
            duration::wav_seconds(&bytes).unwrap_or(0.),
            row["seconds"].as_f64().unwrap(),
            "{row}"
        );
        counts[0] += 1;
    }
    for row in fixture["switches"].as_array().unwrap() {
        assert_eq!(
            duration::long_model(
                row["model"].as_str().unwrap(),
                row["threshold"].as_i64().unwrap(),
                row["seconds"].as_f64()
            ),
            row["selected"]
        );
        counts[1] += 1;
    }
    for row in fixture["calls"].as_array().unwrap() {
        let policy = &row["policy"];
        let engines: Vec<String> = serde_json::from_value(policy["engines"].clone()).unwrap();
        let policy = Policy::read(
            policy["languages"].as_str().unwrap(),
            &engines.join(","),
            "deepgram:nova-3",
            "faster-whisper:small.en",
        );
        let own: Vec<String> = serde_json::from_value(row["own"].clone()).unwrap();
        let mut resolved = policy.resolve(None, &own, "en", row["requested"].as_str().unwrap());
        resolved.use_long_model(duration::long_model(
            row["long"].as_str().unwrap(),
            30,
            row["duration"].as_f64(),
        ));
        let lang = resolved.fields["language"]["value"].as_str().unwrap();
        let steps = call_transcription::plan(
            &resolved.order,
            policy.configured().then_some(lang),
            &policy.local,
        );
        let first = steps.iter().find(|s| s.skip.is_none());
        if let Some(first) = first {
            let mut visible = json!(steps);
            for (i, step) in steps.iter().enumerate() {
                if step.local_default && resolved.fields["engines"]["source"] == "device" {
                    visible[i].as_object_mut().unwrap().remove("local_default");
                    visible[i]["model"] = json!(resolved.order[i]);
                }
            }
            assert_eq!(visible, row["steps"], "{row}");
            assert_eq!(first.engine, row["first"]);
            assert_eq!(resolved.fields["engines"], row["engines"]);
            assert_eq!(resolved.requested, row["device"]);
        } else {
            assert_eq!(row["refusal"]["error"], "no compatible engine");
            assert_eq!(
                json!(steps.iter().map(|s| s.engine.clone()).collect::<Vec<_>>()),
                row["refusal"]["engines"]
            );
        }
        counts[2] += 1;
    }
    assert_eq!(counts, [115, 54, 144]);
}
