//! The desk of a machine's account seats as an app is told it, held to what the Python Host
//! answers (`tests/fixtures/desk-vectors.json`, written by `server-rs/tools/desk-vectors.py`):
//! stories of what the owner's tool answered to a live read, of reads that failed in every
//! way one can, of a Host that is started anew, and of an app that asks at one moment and at
//! another; and two tables, of what kind of window a label is and of the command that takes
//! a session up again.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use clarp_kernel::desk::{self, Agent, Answer, Answers, Asked, Desk, Failed, Outcome};
use clarp_kernel::seats::{Acted, Seats};
use serde_json::Value;

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/desk-vectors.json")).unwrap()
}

/// Where two bodies do not say the same. A whole number and the same number written with a
/// point are one number, and the order of an object's fields says nothing.
fn unlike(ours: &Value, theirs: &Value, at: &str, found: &mut Vec<String>) {
    match (ours, theirs) {
        (Value::Number(a), Value::Number(b)) if a.as_f64() == b.as_f64() => {}
        (Value::Object(a), Value::Object(b)) => {
            let mut names: Vec<&String> = a.keys().chain(b.keys()).collect();
            names.sort();
            names.dedup();
            for name in names {
                match (a.get(name), b.get(name)) {
                    (Some(a), Some(b)) => unlike(a, b, &format!("{at}/{name}"), found),
                    (here, _) => {
                        let side = if here.is_some() {
                            "only here"
                        } else {
                            "only there"
                        };
                        found.push(format!("{at}/{name}: {side}"));
                    }
                }
            }
        }
        (Value::Array(a), Value::Array(b)) if a.len() == b.len() => {
            for (index, (a, b)) in a.iter().zip(b).enumerate() {
                unlike(a, b, &format!("{at}[{index}]"), found);
            }
        }
        (a, b) if a == b => {}
        (a, b) => found.push(format!("{at}: {a} here, {b} there")),
    }
}

/// What one asking of the tool came to, as a step of a story says it: what it said, what it
/// printed and how it ended, or that it took too long or was not there to be run.
fn answered(said: &Value) -> Answer {
    match said["fails"].as_str() {
        Some("timeout") => Err(Failed::TookTooLong),
        Some("missing") => Err(Failed::DidNotRun),
        Some(other) => panic!("a failure this test does not know: {other}"),
        None => match said.get("says") {
            Some(says) => desk::answer(Some(0), &says.to_string()),
            None => {
                let exit = said["exit"].as_i64().map(|exit| exit as i32);
                desk::answer(exit, said["printed"].as_str().unwrap())
            }
        },
    }
}

/// A Host as a story sees it: what it keeps, and what it knows only since it started.
struct Host {
    seats: Seats,
    desk: Desk,
    last_attempt_ms: i64,
    reads: usize,
}

impl Host {
    /// A live read is asked for: it runs when one is allowed, as the Host lets it.
    fn read(&mut self, step: &Value, now: i64) -> Outcome {
        self.reads += 1;
        if now < self.desk.next_read_at(self.last_attempt_ms) {
            return Outcome::Held;
        }
        self.last_attempt_ms = now;
        let answers = Answers {
            desk: answered(&step["tui"]),
            list: answered(&step["list"]),
            work: answered(&step["work"]),
            // What the list says of a Host's agents is taken as it is, as the Python Host does.
            agents: None,
        };
        if self.desk.read(&mut self.seats, answers, now) {
            Outcome::Read
        } else {
            Outcome::Failed
        }
    }
}

fn of_the_seats(seats: &mut Seats, step: &Value) {
    let provider = step["provider"].as_str().unwrap();
    match step["op"].as_str().unwrap() {
        "seats-read" => seats.read(
            provider,
            &step["payload"],
            step["received_at"].as_i64().unwrap(),
        ),
        "seats-action" => {
            let did = Acted {
                decision: &step["decision"],
                mode: step["mode"].as_str().unwrap_or(""),
                switched: step["switched"] == true,
                now: step["now"].as_i64().unwrap(),
                floor_pct: &step["floor_pct"],
                cadence_s: &step["cadence_s"],
                next_due_at: &step["next_due_at"],
            };
            seats.acted(provider, &did);
        }
        "seats-failure" => {
            let how = step["error"].as_str().unwrap();
            seats.failed(provider, how, step["now"].as_i64().unwrap());
        }
        other => panic!("a step this test does not know: {other}"),
    }
}

#[test]
fn the_desk_is_read_kept_and_served_as_the_python_host_does() {
    let vectors = vectors();
    let stories = vectors["stories"].as_array().unwrap();
    let (mut steps, mut reads, mut askings, mut accounts) = (0, 0, 0, 0);
    let mut outcomes = std::collections::BTreeMap::new();
    for story in stories {
        let name = story["story"].as_str().unwrap();
        let enabled = story["janitor_enabled"].as_bool().unwrap();
        let mut host = Host {
            seats: Seats::with_salt("a salt of the test's own"),
            desk: Desk::default(),
            last_attempt_ms: 0,
            reads: 0,
        };
        for (index, step) in story["steps"].as_array().unwrap().iter().enumerate() {
            steps += 1;
            let at = format!("{name}, step {index}");
            let now = step["at"].as_i64().unwrap_or(0);
            match step["op"].as_str().unwrap() {
                "read" => {
                    let outcome = host.read(step, now);
                    assert_eq!(outcome.word(), step["outcome"], "{at}");
                    *outcomes.entry(outcome.word()).or_insert(0) += 1;
                }
                "asked" => {
                    askings += 1;
                    let outcome = (step["refresh"] == true).then(|| host.read(step, now));
                    let asked = Asked {
                        served_at: now,
                        installed: step["installed"] == true,
                        reading: false,
                        outcome,
                        last_attempt_ms: host.last_attempt_ms,
                    };
                    let status = host.seats.served(enabled, now);
                    let body = host.desk.served(&status, &asked);
                    let mut found = Vec::new();
                    unlike(&body, &step["body"], "", &mut found);
                    assert!(found.is_empty(), "{at}: {found:#?}");
                    // A live read hands the tool's list to the seats' status.
                    unlike(&status, &step["status"], "status", &mut found);
                    assert!(found.is_empty(), "{at}: {found:#?}");
                    accounts += body["accounts"].as_array().unwrap().len();
                    // What is kept is what is served again by a Host that kept it and started.
                    let kept: Desk =
                        serde_json::from_str(&serde_json::to_string(&host.desk).unwrap()).unwrap();
                    assert_eq!(kept, host.desk, "{at}");
                }
                // What a Host knows only since it started is gone; what it keeps stays.
                "restart" => host.last_attempt_ms = 0,
                _ => of_the_seats(&mut host.seats, step),
            }
        }
        reads += host.reads;
    }
    // Counted by the script that wrote the vectors, and off what was served.
    assert_eq!(
        (stories.len(), steps, reads, askings, accounts),
        (18, 119, 58, 63, 200)
    );
    // Every way a read can come out is among them.
    assert!(
        outcomes.values().all(|count| *count > 0) && outcomes.len() == 3,
        "{outcomes:?}"
    );
}

#[test]
fn a_label_of_the_tool_is_the_kind_of_window_the_python_host_takes_it_for() {
    let vectors = vectors();
    let labels = vectors["windows"].as_array().unwrap();
    let mut own = 0;
    for row in labels {
        let (tool, label) = (
            row["tool"].as_str().unwrap(),
            row["label"].as_str().unwrap(),
        );
        let (kind, short) = desk::window_kind(tool, label);
        assert_eq!(
            serde_json::json!([kind, short]),
            row["is"],
            "{tool}: {label:?}"
        );
        own += usize::from(!kind.starts_with("window:"));
    }
    assert_eq!(labels.len(), 204);
    // Windows of an account and of a model are among them, and windows by their own name.
    assert!(own > 30 && own < labels.len() - 30, "{own}");
}

#[test]
fn a_session_is_taken_up_again_with_its_own_tools_command_or_with_none() {
    let vectors = vectors();
    let names = vectors["commands"].as_array().unwrap();
    let mut known = 0;
    for row in names {
        let tool = row["tool"].as_str().unwrap();
        let command = desk::resume_command(tool, "a-session");
        assert_eq!(serde_json::json!(command), row["command"], "{tool:?}");
        known += usize::from(command.is_some());
    }
    assert_eq!((names.len(), known), (16, 5));
}

#[test]
fn what_the_tool_printed_is_read_or_said_to_have_failed() {
    assert_eq!(desk::answer(Some(0), ""), Ok(serde_json::json!({})));
    assert_eq!(
        desk::answer(Some(0), " {\"a\": 1}\n"),
        Ok(serde_json::json!({ "a": 1 }))
    );
    for (exit, printed, how) in [
        (Some(1), "{}", Failed::DidNotRun),
        (None, "{}", Failed::DidNotRun),
        (Some(0), "not json", Failed::NotReadable),
        (Some(0), "[1]", Failed::NotReadable),
        (Some(0), "null", Failed::NotReadable),
        (Some(0), "{\"a\": [}", Failed::NotReadable),
    ] {
        assert_eq!(
            desk::answer(exit, printed),
            Err(how),
            "{exit:?} {printed:?}"
        );
    }
}

/// What a tool's desk says of four accounts, two of them in use, and what its list says of a
/// Host's agents and of stopped work: all of that another Host's.
fn told_of_another_host() -> (Value, Value) {
    let of_desk = serde_json::json!({ "sessions": 2, "accounts": [
        { "provider": "claude", "alias": "ai8", "active": false },
        { "provider": "claude", "alias": "ai13", "active": true },
        { "provider": "Codex", "alias": " main ", "active": true },
        { "provider": "codex", "alias": "spare", "active": true },
    ]});
    let list = serde_json::json!({ "accounts": [], "extensions": { "clarp": {
        "agents": {
            "total": 37, "live": 5, "claude_backed": 30, "by_backend": { "claude": 30, "codex": 7 },
            "live_by_account": { "ai13": 5 }, "agents": [{ "session": "theirs", "would_use": "ai8" }],
        },
        "stopped": [
            { "kind": "clarp", "id": "koko", "backend": "claude", "cause": "usage_limit" },
            { "kind": "native", "id": "a-session-of-nobody", "backend": "claude" },
            { "kind": "native", "id": " the-session-of-alpha ", "backend": "claude" },
            { "kind": "other", "id": "of-no-kind" },
        ],
    }}});
    (of_desk, list)
}

#[test]
fn of_agents_a_host_says_its_own_word_and_not_what_the_tool_has_from_another() {
    use serde_json::json;
    let (of_desk, list) = told_of_another_host();
    let agent = |session, tool, working, tool_session| Agent {
        session,
        tool,
        working,
        tool_session,
    };
    let agents = [
        agent("alpha", "claude", true, Some("the-session-of-alpha")),
        agent("beta", "claude", false, None),
        agent("gamma", "codex", true, Some("a-thread")),
        agent("delta", "scripted", true, None),
    ];
    let own = desk::of_this_host(&of_desk, Some(&list), &agents);
    let expected = json!({
        "agents": {
            "total": 4, "live": 3, "claude_backed": 2,
            "by_backend": { "claude": 2, "codex": 1, "scripted": 1 },
            "live_by_account": { "ai13": 1, "main": 1 },
            "agents": [
                { "session": "alpha", "would_use": "ai13" },
                { "session": "beta", "would_use": "ai13" },
                { "session": "gamma", "would_use": "main" },
                { "session": "delta", "would_use": null },
            ],
        },
        // A Host's own stopped work is that Host's; a session of one of this Host's agents is
        // no session to take up in a terminal; what is of no kind is not said again.
        "stopped": [{ "kind": "native", "id": "a-session-of-nobody", "backend": "claude" }],
    });
    assert_eq!(own, expected);

    // The desk is made with it, whether or not the list answered.
    for listed in [true, false] {
        let mut seats = Seats::with_salt("a salt of the test's own");
        let mut kept = Desk::default();
        let answers = Answers {
            desk: Ok(of_desk.clone()),
            list: if listed {
                Ok(list.clone())
            } else {
                Err(Failed::DidNotRun)
            },
            work: Ok(json!({})),
            agents: Some(desk::of_this_host(
                &of_desk,
                listed.then_some(&list),
                &agents,
            )),
        };
        assert!(kept.read(&mut seats, answers, 1_800_000_000_000));
        let asked = Asked {
            served_at: 1_800_000_001_000,
            installed: true,
            reading: false,
            outcome: Some(Outcome::Read),
            last_attempt_ms: 1_800_000_000_000,
        };
        let body = kept.served(&seats.served(false, asked.served_at), &asked);
        let counted = json!({
            "total": 4, "live": 3, "claude_backed": 2,
            "by_backend": { "claude": 2, "codex": 1, "scripted": 1 },
            "live_by_account": { "ai13": 1, "main": 1 },
            "would_use_by_account": { "ai13": 2, "main": 1 },
        });
        assert_eq!(body["clarp_agents"], counted, "{listed}");
        let of = |alias: &str| {
            let accounts = body["accounts"].as_array().unwrap();
            let account = accounts.iter().find(|one| one["alias"] == alias).unwrap();
            (
                account["live_sessions"].clone(),
                account["would_use_sessions"].clone(),
            )
        };
        assert_eq!(of("ai13"), (json!(1), json!(2)));
        assert_eq!(of("ai8"), (json!(0), json!(0)));
        assert_eq!(of("main"), (json!(1), json!(1)));
        assert_eq!(of("spare"), (json!(0), json!(0)));
        let stopped: Vec<&str> = body["stopped_work"]
            .as_array()
            .unwrap()
            .iter()
            .map(|item| item["id"].as_str().unwrap())
            .collect();
        if listed {
            assert_eq!(stopped, ["a-session-of-nobody"]);
            let command = &body["stopped_work"][0]["resume_command"];
            assert_eq!(command, "claude --resume a-session-of-nobody");
        } else {
            assert!(stopped.is_empty(), "{stopped:?}");
        }
        // A read without the list is no whole new desk, with this Host's word or without.
        assert_eq!(body["refreshed"], listed);
    }

    // A Host without agents says so; and where the tool's desk names no account in use, no
    // agent is said to use one.
    let none = desk::of_this_host(&json!({}), None, &[]);
    assert_eq!(
        (&none["agents"]["total"], &none["stopped"]),
        (&json!(0), &json!([]))
    );
    let nobody = desk::of_this_host(&json!({}), None, &agents[..1]);
    let listed = json!([{ "session": "alpha", "would_use": null }]);
    assert_eq!(nobody["agents"]["agents"], listed);
    assert_eq!(nobody["agents"]["live_by_account"], json!({}));
    assert_eq!(nobody["agents"]["live"], 1);
}
