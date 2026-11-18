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

/// The moment the tests of this Host's own word are asked at.
const NOW: i64 = 1_800_000_200_000;

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
        ..Agent::default()
    };
    let agents = [
        agent("alpha", "claude", true, Some("the-session-of-alpha")),
        agent("beta", "claude", false, None),
        agent("gamma", "codex", true, Some("a-thread")),
        agent("delta", "scripted", true, None),
    ];
    let own = desk::of_this_host(&of_desk, Some(&list), &agents, NOW);
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
                NOW,
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
    let none = desk::of_this_host(&json!({}), None, &[], NOW);
    assert_eq!(
        (&none["agents"]["total"], &none["stopped"]),
        (&json!(0), &json!([]))
    );
    let nobody = desk::of_this_host(&json!({}), None, &agents[..1], NOW);
    let listed = json!([{ "session": "alpha", "would_use": null }]);
    assert_eq!(nobody["agents"]["agents"], listed);
    assert_eq!(nobody["agents"]["live_by_account"], json!({}));
    assert_eq!(nobody["agents"]["live"], 1);
}

#[test]
fn a_hosts_own_agents_that_stopped_are_listed_with_why_and_whether_they_could_go_on() {
    use clarp_kernel::desk::Cause;
    use serde_json::json;
    let at = 1_800_000_000_i64;
    let window =
        |label: &str, used: f64| json!({ "label": label, "used": used, "reset": at + 10_000 });
    let of_desk = json!({ "accounts": [
        {
            "provider": "claude", "alias": "work", "display_alias": "Work", "active": true,
            "checked_at": at, "windows": [window("All models · 5-hour", 0.5)],
        },
        {
            "provider": "codex", "alias": "main", "active": true, "checked_at": at,
            "windows": [window("codex primary weekly", 1.0)],
        },
        { "provider": "muse", "alias": "meta", "active": true },
    ]});
    let list = json!({ "accounts": [], "extensions": { "clarp": { "stopped": [
        { "kind": "clarp", "id": "another-hosts", "backend": "claude" },
        { "kind": "native", "id": "nobodys", "backend": "codex" },
    ]}}});
    let agent = |session, tool, stopped, waiting| Agent {
        session,
        name: "A Name",
        tool,
        model: "a-model",
        cwd: "/work",
        stopped,
        waiting,
        ..Agent::default()
    };
    let limit = Some((Cause::UsageLimit, 1_800_000_000_000));
    let agents = [
        agent(
            "limited",
            "claude",
            Some((Cause::UsageLimit, 1_800_000_123_456)),
            0,
        ),
        agent(
            "waits",
            "codex",
            Some((Cause::WaitsForAnAccount, 1_800_000_000_001)),
            2,
        ),
        agent("held", "claude", Some((Cause::QueueHeld, 0)), 3),
        agent("of-an-unknown-account", "muse", limit, 0),
        agent("of-no-account", "agy", limit, 0),
        agent("well", "claude", None, 1),
    ];
    let own = desk::of_this_host(&of_desk, Some(&list), &agents, NOW);
    let of = |row: &Value| {
        let id = row["id"].as_str().unwrap().to_string();
        (id, row["cause"].clone(), row["readiness"].clone())
    };
    let said: Vec<_> = own["stopped"].as_array().unwrap().iter().map(of).collect();
    let stands = |id: &str, cause: &str, ready: bool, hard: Value, warning: Value| {
        let readiness = json!({ "ready": ready, "hard": hard, "warning": warning });
        (id.to_string(), json!(cause), readiness)
    };
    let expected = [
        stands(
            "limited",
            "usage_limit",
            true,
            Value::Null,
            json!("Work could serve it"),
        ),
        stands(
            "waits",
            "waiting_for_account",
            false,
            json!("main has no quota left"),
            Value::Null,
        ),
        stands(
            "held",
            "queue_paused",
            false,
            json!("queue is paused; a prompt would only queue"),
            Value::Null,
        ),
        stands(
            "of-an-unknown-account",
            "usage_limit",
            true,
            Value::Null,
            json!("what is left of meta is not known"),
        ),
        stands(
            "of-no-account",
            "usage_limit",
            false,
            json!("no account of agy is in use"),
            Value::Null,
        ),
        // And after this Host's own, what the tool says stopped that is no Host's.
        ("nobodys".to_string(), Value::Null, Value::Null),
    ];
    assert_eq!(said, expected);

    // An account in use whose login no longer holds, and one that could not be read.
    for (error, hard) in [
        ("Access token expired; sign-in needed", "work is signed out"),
        ("HTTP 500", "work could not be read"),
    ] {
        let failing = json!({ "accounts": [
            { "provider": "claude", "alias": "work", "active": true, "error": error },
        ]});
        let own = desk::of_this_host(&failing, None, &agents[..1], NOW);
        let expected = json!({ "ready": false, "hard": hard, "warning": null });
        assert_eq!(own["stopped"][0]["readiness"], expected, "{error}");
    }

    // What the list says of the account in use decides with the desk: extra usage covers a
    // window that is at its limit.
    let full = json!({ "accounts": [{
        "provider": "claude", "alias": "work", "active": true, "checked_at": at,
        "windows": [window("All models · 5-hour", 1.0)],
    }]});
    let covers =
        json!({ "accounts": [{ "alias": "work", "usage": { "extra_usage_enabled": true } }] });
    let covered = desk::of_this_host(&full, Some(&covers), &agents[..1], NOW);
    assert_eq!(covered["stopped"][0]["readiness"]["ready"], true);
    let bare = desk::of_this_host(&full, None, &agents[..1], NOW);
    assert_eq!(
        bare["stopped"][0]["readiness"]["hard"],
        "work has no quota left"
    );
    // And the tool's own block of an account of Codex is in its list's part for Codex.
    let of_codex = json!({ "accounts": [{
        "provider": "codex", "alias": "main", "active": true, "checked_at": at,
        "windows": [window("codex primary weekly", 0.2)],
    }]});
    let blocks =
        json!({ "codex": { "accounts": [{ "alias": "main", "usage": { "blocked": true } }] } });
    let blocked = desk::of_this_host(&of_codex, Some(&blocks), &agents[1..2], NOW);
    let hard = &blocked["stopped"][0]["readiness"]["hard"];
    assert_eq!(hard, "main has no quota left");
    let free = desk::of_this_host(&of_codex, None, &agents[1..2], NOW);
    assert_eq!(free["stopped"][0]["readiness"]["ready"], true);

    // A stop for a usage limit is listed for a week, to the millisecond; a queue that is
    // held is listed for as long as it is.
    let week = desk::STOP_SHOWN_FOR_MS;
    let older = [
        agent(
            "a-week-ago",
            "claude",
            Some((Cause::UsageLimit, NOW - week)),
            0,
        ),
        agent(
            "longer-ago",
            "claude",
            Some((Cause::UsageLimit, NOW - week - 1)),
            0,
        ),
        agent(
            "held-long-ago",
            "claude",
            Some((Cause::QueueHeld, NOW - 3 * week)),
            1,
        ),
        agent(
            "waits-long",
            "claude",
            Some((Cause::WaitsForAnAccount, NOW - 3 * week)),
            0,
        ),
    ];
    let listed = desk::of_this_host(&of_desk, None, &older, NOW);
    let ids: Vec<&str> = listed["stopped"]
        .as_array()
        .unwrap()
        .iter()
        .map(|row| row["id"].as_str().unwrap())
        .collect();
    assert_eq!(ids, ["a-week-ago", "held-long-ago", "waits-long"]);
    // And a week is seven days: a stop of six days ago is listed, one of eight is not.
    let day = 24 * 60 * 60 * 1000;
    let by_days = [
        agent(
            "six-days-ago",
            "claude",
            Some((Cause::UsageLimit, NOW - 6 * day)),
            0,
        ),
        agent(
            "eight-days-ago",
            "claude",
            Some((Cause::UsageLimit, NOW - 8 * day)),
            0,
        ),
    ];
    let listed = desk::of_this_host(&of_desk, None, &by_days, NOW);
    assert_eq!(listed["stopped"].as_array().unwrap().len(), 1);
    assert_eq!(listed["stopped"][0]["id"], "six-days-ago");

    // The desk shows such a row whole, and the stop it names is the one an asking to
    // continue finds.
    let mut seats = Seats::with_salt("a salt of the test's own");
    let mut kept = Desk::default();
    let answers = Answers {
        desk: Ok(of_desk.clone()),
        list: Ok(list.clone()),
        work: Ok(json!({})),
        agents: Some(own),
    };
    assert!(kept.read(&mut seats, answers, 1_800_000_200_000));
    let shown = kept.stopped_work().unwrap();
    let whole = json!({
        "kind": "clarp", "cause": "usage_limit", "pending": 0, "id": "limited", "name": "A Name",
        "backend": "claude", "model": "a-model", "account": "work", "cwd": "/work",
        "last_message": "", "stopped_at": 1_800_000_123_456_i64,
        "readiness": { "ready": true, "hard": null, "warning": "Work could serve it" },
        "resume_command": null,
    });
    assert_eq!(shown[0], whole);
    assert_eq!(
        (&shown[1]["pending"], &shown[1]["account"]),
        (&json!(2), &json!("main"))
    );
    // A queue that is held since nobody knows when says no moment.
    assert_eq!(
        (&shown[2]["pending"], &shown[2]["stopped_at"]),
        (&json!(3), &Value::Null)
    );
    assert_eq!(shown[5]["resume_command"], "codex resume nobodys");
    assert_eq!(kept.observed_at(), Some(1_800_000_200_000));
    let asked_for = clarp_kernel::resume::Agent {
        session: "limited",
        agent_id: "an-id",
        name: "A Name",
        archived: false,
        janitor: false,
        held: false,
        works: false,
    };
    let stop = clarp_kernel::resume::stop_of(&asked_for, shown);
    assert_eq!(stop.map(|(_, at)| at), Some(1_800_000_123_456));
    assert_eq!(Desk::default().stopped_work(), None);
}
