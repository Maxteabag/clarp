//! What a turn of Antigravity took, against the Python Host's own count.
//!
//! `fixtures/agy-usage-vectors.json` is written by `server-rs/tools/turn-usage-vectors.py`: the
//! Python Host's runner (`AgyBackend._handle_event`, since its PR 413) is handed made-up streams
//! of steps and a result, and what it hands on becomes the turn's `tokens_in` and `tokens_out`
//! as that Host makes them, for a turn that begins its conversation and one that continues it.
//! The same lines are read here as the Host reads a spool, and the turn must have taken the same.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_backend::agy::AgyInterpreter;
use clarp_backend::{Interpreter, Signal};
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/agy-usage-vectors.json")).unwrap()
}

/// What the turn took, as the lines of a stream say it, read one by one and then the exit.
fn took(lines: &[Value]) -> Option<(u64, u64)> {
    let mut interpreter = AgyInterpreter::new(1, "m1");
    let mut out = Vec::new();
    let mut t = 0;
    for line in lines {
        t += 1;
        let entry = Entry::Out(line.as_str().unwrap().to_string());
        interpreter.on_record(&Record { t, entry }, &mut out);
    }
    let gone = Entry::Shim(ShimEvent::Exited {
        code: Some(0),
        signal: None,
    });
    interpreter.on_record(
        &Record {
            t: t + 1,
            entry: gone,
        },
        &mut out,
    );
    let told: Vec<(u64, u64)> = out
        .iter()
        .filter_map(|signal| match signal {
            Signal::Usage {
                tokens_in,
                tokens_out,
            } => Some((*tokens_in, *tokens_out)),
            _ => None,
        })
        .collect();
    assert!(told.len() <= 1, "{told:?}");
    told.first().copied()
}

#[test]
fn a_turn_of_antigravity_took_what_the_python_host_counts() {
    let vectors = vectors();
    let streams = vectors["streams"].as_array().unwrap();
    let (mut counted, mut none) = (0, 0);
    for stream in streams {
        let name = stream["name"].as_str().unwrap();
        let ours = took(stream["lines"].as_array().unwrap());
        // A turn that begins its conversation and one that continues it are counted alike.
        assert_eq!(stream["fresh"], stream["continued"], "{name}");
        let theirs = stream["fresh"]
            .as_array()
            .map(|pair| (pair[0].as_u64().unwrap(), pair[1].as_u64().unwrap()));
        assert_eq!(ours, theirs, "{name}");
        match ours {
            Some(_) => counted += 1,
            None => none += 1,
        }
    }
    assert_eq!((streams.len(), counted, none), (29, 27, 2));
    // The Python Host's own test, as it says it.
    let by_name = |name: &str| {
        let stream = streams.iter().find(|stream| stream["name"] == name);
        stream.unwrap()["fresh"].clone()
    };
    assert_eq!(by_name("the same line twice"), json!([9018, 1]));
    assert_eq!(
        by_name("a step that says nothing of what it took"),
        json!([17823, 3])
    );
    assert_eq!(by_name("and then another step"), json!([9035, 5]));
}
