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
            args["audience"].as_str().unwrap_or("user"),
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

/// An agent's clients are shown another hub's turn (its conversation fork, issue 718): the
/// next event is numbered past what they hold, so they ask for the snapshot, and an operation
/// a reducer does not know (`main_turn`) changes nothing it holds.
#[test]
fn a_hub_continued_past_another_sends_its_clients_to_the_snapshot() {
    let mut main = Hub::new("e1", "a1", "s1");
    let mut client = View::new();
    let begun = main.begin_turn("s1", "c1", "t-main", Some(5), "agents", 10);
    let (snapshot, _) = main.snapshot(10).unwrap();
    client.apply_snapshot(&snapshot);
    for event in &begun {
        assert!(client.apply_event(event).is_empty());
    }
    let mut fork = Hub::new("e1", "a1", "s1");
    let _ = fork.begin_turn("s1", "c1", "t-fork", Some(20), "user", 20);
    fork.continue_after(main.lseq());
    let told = fork.tell(
        json!({"op": "main_turn", "conv": "c1", "main_turn": null}),
        21,
    );
    assert_eq!(told.len(), 1);
    assert_eq!(client.apply_event(&told[0]), vec!["fetch_live"]);
    let (snapshot, _) = fork.snapshot(22).unwrap();
    client.apply_snapshot(&snapshot);
    assert_eq!(client.turn().unwrap()["turn_id"], json!("t-fork"));
    let more = fork.tell(
        json!({"op": "main_turn", "conv": "c1", "main_turn": {"turn_id": "t-main"}}),
        23,
    );
    assert!(client.apply_event(&more[0]).is_empty());
    assert_eq!(client.turn().unwrap()["turn_id"], json!("t-fork"));
    assert_eq!(client.lseq(), Some(fork.lseq()));
}

/// While the tool asks a provider again that would not answer, the status line is thinking in
/// the words of the retry, until the tool goes on or the turn ends.
#[test]
fn a_provider_asked_again_is_shown_until_it_answers() {
    let mut hub = Hub::new("e1", "a1", "s1");
    let mut client = View::new();
    let begun = hub.begin_turn("s1", "c1", "t-1", Some(5), "user", 10);
    let (snapshot, _) = hub.snapshot(10).unwrap();
    client.apply_snapshot(&snapshot);
    for event in &begun {
        assert!(client.apply_event(event).is_empty());
    }
    let line = |hub: &mut Hub, now: i64| hub.snapshot(now).unwrap().0["activity"].clone();
    let told = hub.provider_retry(Some("API overloaded, retrying 1/10"), 20);
    assert_eq!(told.len(), 1, "the status line is sent");
    for event in &told {
        assert!(client.apply_event(event).is_empty());
    }
    let shown = line(&mut hub, 21);
    assert_eq!(
        (&shown["state"], &shown["headline"]),
        (&json!("thinking"), &json!("API overloaded, retrying 1/10"))
    );
    assert!(
        hub.provider_retry(Some("API overloaded, retrying 1/10"), 22)
            .is_empty(),
        "the same try is told once"
    );
    let next = hub.provider_retry(Some("API overloaded, retrying 2/10"), 30);
    assert_eq!(next.len(), 1);
    assert_eq!(
        line(&mut hub, 31)["since_ms"],
        shown["since_ms"],
        "it has thought since the first try"
    );
    assert_eq!(hub.provider_retry(None, 40).len(), 1, "its end is told");
    let answered = line(&mut hub, 41);
    assert_eq!(
        (&answered["state"], &answered["headline"]),
        (&json!("thinking"), &json!("Thinking"))
    );
    let _ = hub.provider_retry(Some("API rate limited, retrying 1/2"), 1_010);
    let _ = hub.end_turn("failed", None, 1_020);
    assert_eq!(line(&mut hub, 1_021)["state"], "interrupted");
}

/// Issue 771: in a hub whose messages grow into answers, nothing a client was shown as an
/// answer turns into narration. A message is `commentary` until the turn decides it is an
/// answer: other words after it (`close_answer`), a message read into the turn
/// (`close_answers`), or the turn's end with it as the newest words. Work after it (a tool, or
/// a plan that changes) leaves it narration.
#[test]
fn a_message_is_an_answer_once_the_turn_decides_it_and_never_narration_after() {
    let mut hub = Hub::new("e1", "a1", "s1").growing();
    let call = |id| ToolStart {
        name: "Bash",
        call_id: id,
        category: "shell",
        label: "Bash",
        command: None,
        input_preview: None,
    };
    let phase = |hub: &mut Hub, id: &str, now: i64| {
        let (snapshot, _) = hub.snapshot(now).unwrap();
        snapshot["items"]
            .as_array()
            .unwrap()
            .iter()
            .find(|item| item["id"] == id)
            .map(|item| item["phase"].clone())
    };
    let mut sent = Vec::new();
    sent.extend(hub.begin_turn("s1", "c1", "t-1", Some(5), "user", 10));
    // Words that a call follows: never an answer.
    sent.extend(hub.message_text(
        "said",
        "Looking",
        (Some("final"), Some("r-said")),
        Some("t-1"),
        20,
    ));
    assert_eq!(phase(&mut hub, "said", 21), Some(json!("commentary")));
    sent.extend(hub.tool_start("tool-1", &call("c-1"), "running", Some("t-1"), 30));
    sent.extend(hub.done("tool-1", "completed", None, Some("t-1"), 40));
    assert_eq!(phase(&mut hub, "said", 41), Some(json!("commentary")));
    // Words a message read into the turn decides: an answer, whatever work follows.
    sent.extend(hub.message_text(
        "answer",
        "Here it is",
        (None, Some("r-answer")),
        Some("t-1"),
        50,
    ));
    assert_eq!(phase(&mut hub, "answer", 51), Some(json!("commentary")));
    // Still being written: a message read in now does not decide it.
    sent.extend(hub.close_answers(51));
    assert_eq!(phase(&mut hub, "answer", 51), Some(json!("commentary")));
    sent.extend(hub.done("answer", "completed", None, Some("t-1"), 51));
    sent.extend(hub.close_answers(52));
    assert_eq!(phase(&mut hub, "answer", 53), Some(json!("final")));
    sent.extend(hub.tool_start("tool-2", &call("c-2"), "running", Some("t-1"), 60));
    assert_eq!(phase(&mut hub, "answer", 61), Some(json!("final")));
    // Words after words: the first is decided by the second's row.
    sent.extend(hub.message_text("one", "One", (None, Some("r-one")), Some("t-1"), 70));
    sent.extend(hub.message_text("two", "Two", (None, Some("r-two")), Some("t-1"), 80));
    sent.extend(hub.close_answer("r-one", 81));
    assert_eq!(phase(&mut hub, "one", 82), Some(json!("final")));
    assert_eq!(phase(&mut hub, "two", 82), Some(json!("commentary")));
    // A plan that changes is work; one told again with the same steps is not.
    let plan = |steps: &str| clarp_kernel::live::Glimpse::Plan {
        prefix: "p".into(),
        steps: json!([{"text": steps, "status": "pending"}]),
    };
    sent.extend(hub.show(&plan("run the tests"), Some("t-1"), 90));
    sent.extend(hub.close_answers(91));
    assert_eq!(phase(&mut hub, "two", 92), Some(json!("commentary")));
    sent.extend(hub.message_text("three", "Three", (None, Some("r-three")), Some("t-1"), 100));
    sent.extend(hub.done("three", "completed", None, Some("t-1"), 100));
    sent.extend(hub.show(&plan("run the tests"), Some("t-1"), 101));
    sent.extend(hub.close_answers(102));
    assert_eq!(phase(&mut hub, "three", 103), Some(json!("final")));
    // The turn's end decides its newest words since its last answer.
    sent.extend(hub.message_text("last", "Done", (None, Some("r-last")), Some("t-1"), 110));
    sent.extend(hub.end_turn("completed", None, 120));
    assert_eq!(phase(&mut hub, "last", 121), Some(json!("final")));
    // A new turn holds no decision of the one before.
    sent.extend(hub.begin_turn("s1", "c1", "t-2", Some(130), "user", 130));
    sent.extend(hub.message_text(
        "again",
        "Looking again",
        (None, Some("r-again")),
        Some("t-2"),
        140,
    ));
    sent.extend(hub.tool_start("tool-3", &call("c-3"), "running", Some("t-2"), 150));
    assert_eq!(phase(&mut hub, "again", 151), Some(json!("commentary")));
    // A turn begun over one that never settled ends that one first, which decides its
    // newest words as an end does: the events say so (the new turn shows only its own items).
    sent.extend(hub.message_text(
        "left",
        "Left open",
        (None, Some("r-left")),
        Some("t-2"),
        160,
    ));
    sent.extend(hub.begin_turn("s1", "c1", "t-3", Some(170), "user", 170));
    // No event ever told a message it is narration after it was an answer, nor was a message
    // told final before it was decided.
    let mut finals = std::collections::BTreeSet::new();
    for event in &sent {
        for op in event["ops"].as_array().into_iter().flatten() {
            let phase = op["item"]["phase"].as_str();
            let id = op["id"].as_str().unwrap_or_default().to_owned();
            match phase {
                Some("final") => {
                    finals.insert(id);
                }
                Some(_) => assert!(!finals.contains(&id), "{id} left the answers: {op}"),
                None => {}
            }
        }
    }
    let expected: std::collections::BTreeSet<String> = ["answer", "one", "three", "last", "left"]
        .map(str::to_owned)
        .into();
    assert_eq!(finals, expected);
}

/// Issue 808: a message read into the running turn (steered in, or read with its own at its
/// start) is carried on the turn as `read_rows`, oldest first, so a client places the turn's
/// working line below it. Told once per new row, and kept as the turn goes on and ends.
#[test]
fn a_turn_carries_the_rows_read_into_it() {
    let mut hub = Hub::new("e1", "a1", "s1").growing();
    let read = |hub: &mut Hub, now: i64| {
        let (snapshot, _) = hub.snapshot(now).unwrap();
        snapshot["turn"]["read_rows"].clone()
    };
    let _ = hub.begin_turn("s1", "c1", "t-1", Some(5), "user", 10);
    assert_eq!(read(&mut hub, 11), Value::Null, "nothing read yet");
    let told = hub.read_rows(&["u-2".to_string()], 20);
    assert_eq!(told.len(), 1, "{told:?}");
    assert_eq!(told[0]["ops"][0]["turn"]["read_rows"], json!(["u-2"]));
    assert!(
        hub.read_rows(&["u-2".to_string()], 21).is_empty(),
        "told once"
    );
    let _ = hub.read_rows(&["u-3".to_string()], 30);
    assert_eq!(read(&mut hub, 31), json!(["u-2", "u-3"]));
    let call = ToolStart {
        name: "Bash",
        call_id: "c-1",
        category: "shell",
        label: "Bash",
        command: None,
        input_preview: None,
    };
    let _ = hub.tool_start("tool-1", &call, "running", Some("t-1"), 40);
    assert_eq!(read(&mut hub, 41), json!(["u-2", "u-3"]));
    let _ = hub.end_turn("completed", None, 50);
    assert_eq!(read(&mut hub, 51), json!(["u-2", "u-3"]));
    // A new turn reads nothing of the last one.
    let _ = hub.begin_turn("s1", "c1", "t-2", Some(60), "user", 60);
    assert_eq!(read(&mut hub, 61), Value::Null);
    // A turn the user keeps writing into carries the newest of what it read, and says so.
    let rows: Vec<String> = (0..25).map(|n| format!("u-r{n}")).collect();
    let _ = hub.read_rows(&rows, 70);
    let (snapshot, _) = hub.snapshot(71).unwrap();
    let kept: Vec<String> = (5..25).map(|n| format!("u-r{n}")).collect();
    assert_eq!(snapshot["turn"]["read_rows"], json!(kept));
    assert_eq!(kept.len(), clarp_kernel::live::READ_ROWS_KEPT);
    assert_eq!(snapshot["turn"]["read_rows_more"], json!(true));
    // Told the whole list again (a second show, a replay): nothing moves, the newest stays last.
    assert!(hub.read_rows(&rows, 72).is_empty(), "nothing new is told");
    let (again, _) = hub.snapshot(73).unwrap();
    assert_eq!(again["turn"]["read_rows"], json!(kept));
    let _ = hub.read_rows(&["u-r25".to_string()], 74);
    let (newer, _) = hub.snapshot(75).unwrap();
    let last = newer["turn"]["read_rows"]
        .as_array()
        .unwrap()
        .last()
        .cloned();
    assert_eq!(last, Some(json!("u-r25")));

/// A live message is shown as its row will be (issue 824): a hidden block never shows, not even
/// while it is still being written, and half a tag at its end waits until it is whole. What is
/// text stays text.
#[test]
fn a_live_message_never_shows_a_hidden_block_or_half_a_tag() {
    let mut hub = Hub::new("e1", "a1", "s1");
    let shown = |hub: &mut Hub, now: i64| {
        let (snapshot, _) = hub.snapshot(now).unwrap();
        snapshot["items"]
            .as_array()
            .unwrap()
            .iter()
            .find(|item| item["id"] == "said")
            .map(|item| item["text"].as_str().unwrap_or("").to_string())
            .unwrap_or_default()
    };
    hub.begin_turn("s1", "c1", "t-1", Some(5), "user", 10);
    let steps = [
        ("The answer", "The answer"),
        ("The answer <oai-mem-cit", "The answer "),
        ("The answer <oai-mem-citation>secret notes", "The answer "),
        (
            "The answer <oai-mem-citation>secret notes</oai-mem-citation> and <spe",
            "The answer  and ",
        ),
        (
            "The answer <oai-mem-citation>secret notes</oai-mem-citation> and <speak>more</speak>",
            "The answer  and <speak>more</speak>",
        ),
        (
            "The answer <oai-mem-citation>secret notes</oai-mem-citation> and <speak>more</speak> x < 5",
            "The answer  and <speak>more</speak> x < 5",
        ),
    ];
    for (at, (written, expected)) in steps.iter().enumerate() {
        let at = i64::try_from(at).unwrap();
        hub.message_text(
            "said",
            written,
            (Some("final"), Some("r-said")),
            Some("t-1"),
            20 + at * 10,
        );
        let text = shown(&mut hub, 25 + at * 10);
        assert_eq!(text, *expected, "after {written:?}");
        assert!(
            !text.contains("secret") && !text.contains("oai-mem"),
            "{text}"
        );
    }
}

/// What an agent writes with a `<` that is not a tag (`i<n`, `a<b`) shows as it streams: only
/// the start of a tag the Host hides waits (issue 824).
#[test]
fn a_live_message_with_a_less_than_sign_keeps_streaming() {
    let mut hub = Hub::new("e1", "a1", "s1");
    let shown = |hub: &mut Hub, now: i64| {
        let (snapshot, _) = hub.snapshot(now).unwrap();
        snapshot["items"]
            .as_array()
            .unwrap()
            .iter()
            .find(|item| item["id"] == "code")
            .map(|item| item["text"].as_str().unwrap_or("").to_string())
            .unwrap_or_default()
    };
    hub.begin_turn("s1", "c1", "t-1", Some(5), "user", 10);
    let steps = [
        ("for (i = 0; i<n", "for (i = 0; i<n"),
        ("for (i = 0; i<n; i++) done", "for (i = 0; i<n; i++) done"),
        (
            "for (i = 0; i<n; i++) done. If a<b and more",
            "for (i = 0; i<n; i++) done. If a<b and more",
        ),
    ];
    for (at, (written, expected)) in steps.iter().enumerate() {
        let at = i64::try_from(at).unwrap();
        hub.message_text(
            "code",
            written,
            (Some("final"), Some("r-code")),
            Some("t-1"),
            20 + at * 10,
        );
        assert_eq!(
            shown(&mut hub, 25 + at * 10),
            *expected,
            "after {written:?}"
        );
    }
}
