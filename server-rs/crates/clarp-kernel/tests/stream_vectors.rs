//! The stream of speech, against the Python Host's own answers.
//!
//! `fixtures/stream-vectors.json` is written by `server-rs/tools/stream-vectors.py` from
//! `server/lib/stt_stream.py`: the address the engine is reached at for every way a phone
//! tunes the listening, what each message of the engine means, the books of one stream
//! through stories of sound, events and time, and the sound of a turn as a file. The same
//! must come out here.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::speech_stream::{
    self as stream, Keeps, Ledger, Run, Tuning, flux_url, meant, wav,
};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/stream-vectors.json")).unwrap()
}

/// Two values that say the same: fields by their names, numbers by what they count.
fn same(ours: &Value, theirs: &Value) -> bool {
    match (ours, theirs) {
        (Value::Number(ours), Value::Number(theirs)) => ours.as_f64() == theirs.as_f64(),
        (Value::Object(ours), Value::Object(theirs)) => {
            ours.len() == theirs.len()
                && ours
                    .iter()
                    .all(|(name, value)| theirs.get(name).is_some_and(|other| same(value, other)))
        }
        (Value::Array(ours), Value::Array(theirs)) => {
            ours.len() == theirs.len() && ours.iter().zip(theirs).all(|(a, b)| same(a, b))
        }
        _ => ours == theirs,
    }
}

/// Bytes that count up modulo 251 from where the stream is: the generator's sound.
fn sound(count: usize, start: usize) -> Vec<u8> {
    (0..count)
        .map(|at| u8::try_from((start + at) % 251).unwrap())
        .collect()
}

/// The digest of bytes, as the generator writes it.
fn sha256(bytes: &[u8]) -> String {
    use sha2::{Digest, Sha256};
    Sha256::digest(bytes)
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

#[test]
fn the_constants_are_the_python_hosts() {
    let defaults = &vectors()["defaults"];
    assert_eq!(defaults["url"], stream::FLUX_URL);
    assert_eq!(defaults["sample_rate"], stream::SAMPLE_RATE);
    assert_eq!(defaults["eot_threshold"], stream::EOT_THRESHOLD);
    assert_eq!(defaults["eager_eot_threshold"], stream::EAGER_EOT_THRESHOLD);
    assert_eq!(defaults["eot_timeout_ms"], stream::EOT_TIMEOUT_MS);
    assert_eq!(defaults["flush_grace_s"], stream::FLUSH_GRACE_S);
    assert_eq!(defaults["upstream_timeout_s"], stream::OPENS_WITHIN_S);
    assert_eq!(stream::HELD_MOST, 3_840_000);
}

#[test]
fn the_engine_is_reached_at_the_address_the_python_host_reaches_it_at() {
    let vectors = vectors();
    let model = vectors["defaults"]["model"].as_str().unwrap();
    let addresses = vectors["addresses"].as_array().unwrap();
    let (mut wrong, mut failing) = (Vec::new(), 0);
    for row in addresses {
        let said = |name: &str| match name {
            "eot_threshold" => row["threshold"].as_str(),
            "eager_eot_threshold" => row["eager"].as_str(),
            "eot_timeout_ms" => row["timeout"].as_str(),
            other => panic!("{other}"),
        };
        let keyterms: Vec<String> = row["keyterms"]
            .as_array()
            .map(|terms| {
                let terms = terms.iter().map(|term| term.as_str().unwrap().to_string());
                terms.collect()
            })
            .unwrap_or_default();
        let model = row["model"].as_str().unwrap_or(model);
        let url = flux_url(model, &Tuning::of(said), &keyterms);
        // Where the Python Host fails the request, this Host has no address either.
        let theirs = row["url"].as_str();
        failing += usize::from(theirs.is_none());
        if url.as_deref() != theirs {
            wrong.push(format!("{row}: {url:?}"));
        }
    }
    assert!(
        wrong.is_empty(),
        "{} of {}: {:#?}",
        wrong.len(),
        addresses.len(),
        &wrong[..wrong.len().min(6)]
    );
    assert_eq!((addresses.len(), failing), (138, 4));
}

#[test]
fn a_message_of_the_engine_means_what_the_python_host_takes_it_to_mean() {
    let vectors = vectors();
    let messages = vectors["messages"].as_array().unwrap();
    let mut events = 0;
    for row in messages {
        let ours = meant(&row["said"]).map(|event| {
            json!({
                "kind": event.kind, "turn": event.turn, "text": event.text,
                "confidence": event.confidence,
            })
        });
        let theirs = &row["means"];
        events += usize::from(!theirs.is_null());
        assert!(
            same(&ours.clone().unwrap_or(Value::Null), theirs),
            "{}: {ours:?} for {theirs}",
            row["said"]
        );
    }
    assert_eq!((messages.len(), events), (55, 38));
}

/// What a story's Host is asked, and what it answers.
struct Told {
    names: u64,
    /// What the writer of runs answers, in turn. `None`: it fails.
    runs: std::vec::IntoIter<Option<i64>>,
    written: Vec<Value>,
    kept: Vec<Value>,
}

impl Keeps for Told {
    fn new_trace(&mut self) -> String {
        self.names += 1;
        format!("trace-{}", self.names)
    }

    fn record(&mut self, run: &Run<'_>) -> Option<i64> {
        self.written.push(json!({
            "session": run.session, "trace_id": run.trace_id, "provider": run.provider,
            "model": run.model, "keyterms": run.keyterms, "capacity": run.capacity,
            "transcript": run.transcript, "latency_ms": run.latency_ms,
        }));
        self.runs.next().unwrap()
    }

    fn retain(&mut self, trace_id: &str, wav: Vec<u8>) {
        self.kept.push(json!({
            "trace_id": trace_id, "bytes": wav.len(), "sha256": sha256(&wav),
        }));
    }
}

#[test]
fn the_books_of_a_stream_are_kept_as_the_python_host_keeps_them() {
    let vectors = vectors();
    let stories = vectors["stories"].as_array().unwrap();
    let (mut steps, mut said, mut written, mut kept) = (0, 0, 0, 0);
    for story in stories {
        let name = story["story"].as_str().unwrap();
        // What the writer answered there, in turn: a number, nothing, or that it failed.
        let answers = story["answers"].as_array().unwrap().iter();
        let answers: Vec<Option<i64>> = answers.map(Value::as_i64).collect();
        let mut keeps = Told {
            names: 0,
            runs: answers.into_iter(),
            written: Vec::new(),
            kept: Vec::new(),
        };
        let keyterms = vec!["Clarp".to_string(), "two words".to_string()];
        let mut ledger = Ledger::new("a-session", "deepgram", "flux-general-en", keyterms, 50);
        let (mut clock, mut heard) = (1000.0_f64, 0usize);
        for (at, step) in story["steps"].as_array().unwrap().iter().enumerate() {
            steps += 1;
            if let Some(wait) = step["wait"].as_f64() {
                clock += wait;
                continue;
            }
            if let Some(count) = step["heard"].as_u64() {
                let count = usize::try_from(count).unwrap();
                ledger.heard(&sound(count, heard), clock, &mut keeps);
                heard += count;
            } else {
                said += 1;
                let told =
                    meant(&step["said"]).map(|event| ledger.apply(&event, clock, &mut keeps));
                assert!(
                    same(&told.clone().unwrap_or(Value::Null), &step["told"]),
                    "{name}, step {at}: {told:?} for {}",
                    step["told"]
                );
                assert_eq!(
                    json!(ledger.turns_ended),
                    step["ended"],
                    "{name}, step {at}"
                );
            }
            let (open, held) = ledger.open_turn();
            assert_eq!(
                (json!(open), json!(held)),
                (step["trace_id"].clone(), step["held"].clone()),
                "{name}, step {at}"
            );
        }
        assert!(
            same(&json!(keeps.written), &story["written"]),
            "{name}: {:#?}",
            keeps.written
        );
        assert_eq!(json!(keeps.kept), story["kept"], "{name}");
        written += keeps.written.len();
        kept += keeps.kept.len();
    }
    assert_eq!(
        (stories.len(), steps, said, written, kept),
        (9, 78, 41, 14, 10)
    );
}

#[test]
fn the_sound_of_a_turn_is_the_file_the_python_host_makes_of_it() {
    let vectors = vectors();
    let wavs = vectors["wavs"].as_array().unwrap();
    for row in wavs {
        let count = usize::try_from(row["bytes"].as_u64().unwrap()).unwrap();
        let file = wav(&sound(count, 0));
        let head: String = file[..44]
            .iter()
            .map(|byte| format!("{byte:02x}"))
            .collect();
        assert_eq!(json!(head), row["head"], "{count}");
        assert_eq!(
            (json!(file.len()), json!(sha256(&file))),
            (row["whole"].clone(), row["sha256"].clone()),
            "{count}"
        );
    }
    assert_eq!(wavs.len(), 7);
}
