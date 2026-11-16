//! Live items, against the Python Host's.
//!
//! `fixtures/live-vectors.json` is written by `server-rs/tools/live-vectors.py`: the recorded
//! streams of the contract (`contract/live/*.json`), and what the Python Host's own hub, reducer
//! and helpers answered for scripted turns, random walks, texts and edits. The same must be
//! answered here, event for event.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::difflib;
use clarp_kernel::live::{Hub, ToolStart, View, diff_stats, output_tail, preview, reasoning_title};
use clarp_kernel::python;
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/live-vectors.json")).unwrap()
}

fn list<'a>(value: &'a Value, name: &str) -> &'a Vec<Value> {
    value[name]
        .as_array()
        .unwrap_or_else(|| panic!("no {name}"))
}

/// What is expected is in what there is: every field of an object, and other values whole.
fn within(actual: &Value, expected: &Value, path: &str) {
    match expected {
        Value::Object(fields) => {
            assert!(actual.is_object(), "{path}: an object, not {actual}");
            for (key, value) in fields {
                assert!(actual.get(key).is_some(), "{path}.{key} is missing");
                within(&actual[key], value, &format!("{path}.{key}"));
            }
        }
        other => assert_eq!(actual, other, "{path}"),
    }
}

#[test]
fn the_recorded_streams_of_the_contract_reach_the_state_they_name() {
    let vectors = vectors();
    let fixtures = list(&vectors, "fixtures");
    assert!(fixtures.len() >= 7);
    for fixture in fixtures {
        let name = fixture["name"].as_str().unwrap();
        let mut view = View::new();
        let mut effects: Vec<&str> = Vec::new();
        for step in list(fixture, "steps") {
            if let Some(snapshot) = step.get("snapshot") {
                view.apply_snapshot(snapshot);
            } else {
                effects.extend(view.apply_event(&step["event"]));
            }
        }
        let expect = &fixture["expect"];
        assert_eq!(json!(effects), expect["effects"], "{name}: effects");
        assert_eq!(json!(view.lseq()), expect["lseq"], "{name}: lseq");
        within(
            view.activity(),
            &expect["activity"],
            &format!("{name}: activity"),
        );
        let turn = view.turn().cloned().map_or(Value::Null, Value::Object);
        within(&turn, &expect["turn"], &format!("{name}: turn"));
        let items = view.items();
        let ids: Vec<&Value> = items.iter().map(|item| &item["id"]).collect();
        let expected: Vec<&Value> = list(expect, "items")
            .iter()
            .map(|item| &item["id"])
            .collect();
        assert_eq!(ids, expected, "{name}: items");
        for (actual, expected) in items.iter().zip(list(expect, "items")) {
            within(
                actual,
                expected,
                &format!("{name}: item {}", expected["id"]),
            );
        }
    }
}

/// One scripted step, done to a hub. Answers with the events it sent and, for a snapshot,
/// what it answered.
fn done_to(hub: &mut Hub, step: &Value) -> (Vec<Value>, Value) {
    let now = step["at"].as_i64().unwrap();
    let args = &step["args"];
    let text = |name: &str| args[name].as_str().unwrap_or("");
    let turn = args["turn_id"].as_str();
    let id = text("id");
    let mut answer = Value::Null;
    let events = match step["call"].as_str().unwrap() {
        "fire" => hub.fire(now),
        "snapshot" => match hub.snapshot(now) {
            Some((snapshot, events)) => {
                answer = snapshot;
                events
            }
            None => Vec::new(),
        },
        "begin_turn" => hub.begin_turn(
            text("session"),
            text("conv"),
            text("turn_id"),
            args["started_at_ms"].as_i64(),
            now,
        ),
        "end_turn" => hub.end_turn(
            args["status"].as_str().unwrap_or("completed"),
            args.get("error").filter(|error| !error.is_null()),
            now,
        ),
        "account_recovery" => hub.account_recovery(args["message"].as_str(), now),
        "message_text" => hub.message_text(
            id,
            text("text"),
            (args["phase"].as_str(), args["row_id"].as_str()),
            turn,
            now,
        ),
        "reasoning_text" => hub.reasoning_text(id, text("text"), turn, now),
        "tool_start" => {
            let start = ToolStart {
                name: text("name"),
                call_id: text("call_id"),
                category: text("category"),
                label: text("label"),
                command: args["command"].as_str(),
                input_preview: args["input_preview"].as_object(),
            };
            let status = args["status"].as_str().unwrap_or("running");
            hub.tool_start(id, &start, status, turn, now)
        }
        "tool_output" => {
            let lines: Vec<String> = list(args, "lines")
                .iter()
                .map(|line| line.as_str().unwrap().to_string())
                .collect();
            hub.tool_output(id, &lines, args["total_lines"].as_i64().unwrap(), turn, now);
            Vec::new()
        }
        "patch" => hub.patch(id, &args["fields"], turn, now),
        "item" => hub.item(id, text("kind"), &args["fields"], turn, now),
        "done" => hub.done(
            id,
            args["status"].as_str().unwrap_or("completed"),
            args.get("patch").filter(|patch| !patch.is_null()),
            turn,
            now,
        ),
        other => panic!("a call this test does not know: {other}"),
    };
    (events, answer)
}

#[test]
fn the_hub_sends_what_the_python_hub_sends() {
    let vectors = vectors();
    let cases = list(&vectors, "hub");
    assert!(cases.len() >= 19);
    let mut events_held = 0;
    for case in cases {
        let name = case["name"].as_str().unwrap();
        let mut hub = Hub::new("epoch-1", "agent-1", "");
        for (place, step) in list(case, "steps").iter().enumerate() {
            let call = step["call"].as_str().unwrap();
            let said = format!("{name}, step {place} ({call} {})", step["args"]);
            let (events, answer) = done_to(&mut hub, step);
            let expected = list(step, "events");
            assert_eq!(
                events.len(),
                expected.len(),
                "{said}: how many events\n{events:#?}"
            );
            for (event, expected) in events.iter().zip(expected) {
                assert_eq!(event, expected, "{said}");
                events_held += 1;
            }
            assert_eq!(
                json!(hub.due_ms()),
                step["due"],
                "{said}: what is held back until when"
            );
            if call == "snapshot" {
                assert_eq!(answer, step["answer"], "{said}: the snapshot");
            }
        }
    }
    assert!(events_held > 500, "{events_held} events were compared");
}

#[test]
fn a_client_that_saw_every_event_holds_what_the_hub_answers() {
    // The other half of the promise: every event the hub sends, applied by the reducer a
    // client runs, gives what `GET /live` says.
    let vectors = vectors();
    for case in list(&vectors, "hub") {
        let name = case["name"].as_str().unwrap();
        let mut hub = Hub::new("epoch-1", "agent-1", "");
        let mut client = View::new();
        let mut conv = Value::Null;
        for step in list(case, "steps") {
            let (events, _) = done_to(&mut hub, step);
            for event in events {
                if event["conv"] != conv {
                    // Another conversation: a client opens it with its empty snapshot.
                    conv = event["conv"].clone();
                    client.apply_snapshot(&json!({"epoch": "epoch-1", "lseq": 0}));
                }
                assert!(client.apply_event(&event).is_empty(), "{name}: {event}");
            }
            let now = step["at"].as_i64().unwrap();
            // A snapshot sends what is held back; that is another step's business here.
            if hub.due_ms().is_some() {
                continue;
            }
            let Some((snapshot, _)) = hub.snapshot(now) else {
                continue;
            };
            assert_eq!(json!(client.lseq()), snapshot["lseq"], "{name}: lseq");
            assert_eq!(client.activity(), &snapshot["activity"], "{name}: activity");
            let items: Vec<Value> = client.items().into_iter().cloned().collect();
            assert_eq!(json!(items), snapshot["items"], "{name}: items");
        }
    }
}

#[test]
fn a_headline_a_preview_a_tail_and_a_count_are_the_python_hosts() {
    let vectors = vectors();
    for case in list(&vectors, "titles") {
        let text = case["text"].as_str().unwrap();
        assert_eq!(
            json!(reasoning_title(text)),
            case["title"],
            "title of {text:?}"
        );
    }
    for case in list(&vectors, "previews") {
        let shown = Value::Object(preview(&case["input"]));
        assert_eq!(shown, case["preview"], "preview of {}", case["input"]);
    }
    for case in list(&vectors, "tails") {
        let (tail, total) = output_tail(&case["content"]);
        assert_eq!(json!(tail), case["tail"], "tail of {}", case["content"]);
        assert_eq!(json!(total), case["total"], "lines of {}", case["content"]);
    }
    let diffs = list(&vectors, "diffs");
    assert!(diffs.len() >= 60);
    for case in diffs {
        let name = case["name"].as_str().unwrap();
        let counted = diff_stats(name, &case["input"]).unwrap_or(Value::Null);
        assert_eq!(counted, case["diff"], "diff of {name} {}", case["input"]);
    }
}

#[test]
fn two_texts_differ_as_pythons_difflib_says() {
    let vectors = vectors();
    let pairs = list(&vectors, "unified");
    assert!(pairs.len() >= 40);
    let mut long = 0;
    for (place, case) in pairs.iter().enumerate() {
        let (a, b) = (case["a"].as_str().unwrap(), case["b"].as_str().unwrap());
        let (a, b) = (python::splitlines(a), python::splitlines(b));
        long += usize::from(b.len() >= 200);
        let lines = difflib::unified(&a, &b, 1);
        assert_eq!(
            json!(lines),
            case["lines"],
            "pair {place}: {} and {} lines",
            a.len(),
            b.len()
        );
    }
    assert!(
        long >= 8,
        "{long} pairs are long enough for common lines to be set apart"
    );
}
