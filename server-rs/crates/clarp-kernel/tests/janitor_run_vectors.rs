//! The runs of a janitor's scheduled task, held to the Python Host's own runner, look by look
//! (`tests/fixtures/janitor-run-vectors.json`, written by
//! `server-rs/tools/janitor-run-vectors.py`, which drives `JanitorRunner` with a clock, an
//! ending of the turn and an answer of the queue that each step sets).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::{BTreeMap, BTreeSet};

use clarp_kernel::janitors::book::{Agent, Asked, Book, Effect, World};
use clarp_kernel::janitors::runs::{self, Delivery, Ended};
use clarp_kernel::janitors::{Refused, schedule};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/janitor-run-vectors.json")).unwrap()
}

/// The made-up Host the stories happen on. Its schedules are read by the kernel's own rule,
/// on the machine's zones.
struct Host {
    agents: BTreeMap<String, Agent>,
    drawn: u32,
}

impl World for Host {
    fn agent(&self, key: &str) -> Option<Agent> {
        let by_session = || self.agents.values().find(|agent| agent.session == key);
        self.agents.get(key).or_else(by_session).cloned()
    }

    fn zone(&self, name: &str) -> bool {
        schedule::zone(name).is_some()
    }

    fn draw(&mut self) -> String {
        self.drawn += 1;
        format!("attachment-raw-{}", self.drawn)
    }

    fn backend(&self, name: &str) -> bool {
        ["claude", "codex"].contains(&name)
    }

    fn model(&self, _backend: &str, _model: &str) -> bool {
        true
    }

    fn efforts(&self, _backend: &str) -> Vec<String> {
        vec!["low".to_string(), "medium".to_string(), "high".to_string()]
    }

    fn base_interval(&self, expression: &str, zone: &str, now_ms: i64) -> i64 {
        schedule::base_interval(expression, zone, now_ms).unwrap()
    }

    fn next_run(&self, expression: &str, zone: &str, from_ms: i64) -> Option<i64> {
        schedule::next_run(expression, zone, from_ms).unwrap()
    }

    fn directory(&self, path: &str) -> String {
        path.to_string()
    }

    fn drawn(&mut self) -> String {
        self.drawn += 1;
        format!("{:012x}", self.drawn)
    }

    fn face(&self, _file: &str) -> String {
        String::new()
    }
}

impl Host {
    fn of(vectors: &Value) -> Self {
        let word = |value: &Value| value.as_str().unwrap().to_string();
        let agents = vectors["agents"].as_array().unwrap().iter().map(|agent| {
            let made = Agent {
                agent_id: word(&agent["agent_id"]),
                session: word(&agent["session"]),
                name: word(&agent["persona"]),
                backend: word(&agent["backend"]),
                ..Agent::default()
            };
            (made.agent_id.clone(), made)
        });
        Self {
            agents: agents.collect(),
            drawn: 0,
        }
    }

    fn becomes(&mut self, effects: Vec<Effect>) {
        for effect in effects {
            match effect {
                Effect::Convert { agent_id } => {
                    self.agents.get_mut(&agent_id).unwrap().is_janitor = true
                }
                Effect::PutAway { agent_id } => {
                    self.agents.get_mut(&agent_id).unwrap().archived = true
                }
                Effect::Release { agent_id } => {
                    self.agents.get_mut(&agent_id).unwrap().is_janitor = false
                }
                Effect::Tune { .. } => {}
            }
        }
    }
}

fn told(refused: &Refused) -> Value {
    json!([refused.status, refused.code, refused.message])
}

/// A janitor as the book shows it, without its picture (told elsewhere).
fn shown(book: &Book, host: &Host, key: &str, now: i64) -> Result<Value, Refused> {
    let mut janitor = book.shown(key, host, now)?;
    janitor.as_object_mut().unwrap().remove("avatar_url");
    Ok(janitor)
}

/// One look of the runner at every trigger it looks at: what was handed to the queue, and how
/// many of those the queue took.
fn looked(book: &mut Book, host: &Host, given: &Value, now: i64) -> (Vec<Value>, i64) {
    let ended = given.get("ended").map(|ended| {
        // Why a turn failed is in what it left behind, as a text of JSON.
        let detail = ended["detail"].as_str().unwrap_or_default();
        let detail: Value = serde_json::from_str(detail).unwrap_or(Value::Null);
        let why = [&detail["error"], &detail["reason"]];
        let why = why
            .iter()
            .find_map(|said| said.as_str().filter(|said| !said.is_empty()));
        Ended {
            failed: ended["kind"] == "error",
            why: why.unwrap_or_default().to_string(),
        }
    });
    let how = match given["delivery"].as_str().unwrap_or("accepted") {
        "accepted" => Delivery::Taken,
        "refused" => Delivery::NotTaken,
        _ => Delivery::Broke("RuntimeError".to_string()),
    };
    let (mut handed, mut taken) = (Vec::new(), 0);
    for id in book.scheduled(host) {
        if let Some(hand) = book.look(&id, ended.as_ref(), host, now) {
            handed.push(json!([hand.run_id, hand.session, hand.prompt]));
            taken += i64::from(book.handed(&id, &how, host, now));
        }
    }
    (handed, taken)
}

/// One asking of the book that is no look of the runner.
fn asked(book: &mut Book, host: &mut Host, step: &Value, now: i64) -> Result<Value, Refused> {
    let key = step["of"].as_str().unwrap();
    let mut given: Asked = step["given"].as_object().unwrap().clone();
    let revision = given.remove("expected_revision").unwrap_or(Value::Null);
    let said = |key: &str| step["given"][key].as_str().unwrap_or_default().to_string();
    match step["ask"].as_str().unwrap() {
        "get" => shown(book, host, key, now),
        "create" => book.create(key, &given, host, now).and_then(|effects| {
            host.becomes(effects);
            shown(book, host, key, now)
        }),
        "configure" => book
            .configure(key, &revision, &given, host, now)
            .and_then(|effects| {
                host.becomes(effects);
                shown(book, host, key, now)
            }),
        "enabled" => {
            let on = given["enabled"] == true;
            let turned = book.set_enabled(key, &revision, on, host, now);
            turned.and_then(|()| shown(book, host, key, now))
        }
        "remove" => book.remove(key, &revision, host, now).map(|effects| {
            host.becomes(effects);
            json!(true)
        }),
        "release" => {
            let let_go = book.release(key, &revision, (None, None), host, now);
            let_go.and_then(|effects| {
                host.becomes(effects);
                shown(book, host, key, now)
            })
        }
        "report" => book.report(&said("run"), &said("activity"), &said("summary"), host),
        "started" => book.started(&said("run"), host, now),
        "context" => book.context(&said("run"), host),
        "valid" => Ok(json!(book.may_start(
            &said("session"),
            &said("run"),
            &said("trace"),
            host
        ))),
        "runs" => {
            let limit = step["given"]["limit"].as_i64().unwrap();
            book.runs_of(key, limit, host).map(Value::Array)
        }
        other => panic!("nothing is known as {other}"),
    }
}

/// What is kept after a step, of the janitors of one story: what the runner knows of each of
/// their triggers, their runs, and each as it is read.
fn kept(book: &Book, host: &Host, sessions: &BTreeSet<String>, now: i64) -> Value {
    let own = |agent_id: &str| {
        let agent = host.agents.get(agent_id);
        agent.is_some_and(|agent| sessions.contains(&agent.session))
    };
    let mut progress = serde_json::Map::new();
    for (id, state) in &book.progress {
        if book
            .triggers
            .get(id)
            .is_some_and(|kept| own(&kept.agent_id))
        {
            progress.insert(id.clone(), serde_json::to_value(state).unwrap());
        }
    }
    let (mut runs, mut janitors) = (serde_json::Map::new(), serde_json::Map::new());
    for session in sessions {
        let own = book.runs_of(session, 100, host).unwrap();
        let rows = own.iter().map(|run| {
            json!([
                run["run_id"],
                run["status"],
                run["outcome"],
                run["error"],
                run["activity"],
                run["activity_summary"],
                run["created_at"],
                run["started_at"],
                run["finished_at"]
            ])
        });
        runs.insert(session.clone(), rows.collect());
        janitors.insert(session.clone(), shown(book, host, session, now).unwrap());
    }
    json!({ "progress": progress, "runs": runs, "janitors": janitors })
}

/// What the Python Host kept after a step, of the janitors of the step's story.
fn theirs(after: &Value, book: &Book, host: &Host, sessions: &BTreeSet<String>) -> Value {
    let mut after = after.clone();
    let progress = after["progress"].as_object_mut().unwrap();
    progress.retain(|id, _| {
        let agent = book
            .triggers
            .get(id)
            .and_then(|kept| host.agents.get(&kept.agent_id));
        agent.is_some_and(|agent| sessions.contains(&agent.session))
    });
    after
}

#[test]
fn a_scheduled_task_is_run_look_by_look_as_the_python_host_runs_it() {
    let vectors = vectors();
    let mut host = Host::of(&vectors);
    let mut book = Book::default();
    let stories = vectors["stories"].as_array().unwrap();
    let (mut steps, mut looks, mut handed_over) = (0, 0, 0);
    for story in stories {
        // A task that keeps its pace runs at every time its schedule names (R31, mended by the
        // Python Host's PR 425).
        if story["paced"] == true {
            assert_eq!(story["ran"], story["named"], "{}", story["name"]);
        }
        for step in story["steps"].as_array().unwrap() {
            let what = format!("{}: {}", story["name"], step["what"]);
            let now = step["now"].as_i64().unwrap();
            let sessions: BTreeSet<String> = step["after"]["janitors"]
                .as_object()
                .unwrap()
                .keys()
                .cloned()
                .collect();
            if step["ask"] == "tick" {
                let (handed, taken) = looked(&mut book, &host, &step["given"], now);
                looks += 1;
                handed_over += handed.len();
                assert_eq!(json!(handed), step["handed"], "{what}");
                assert_eq!(taken, step["count"], "{what}");
            } else {
                let done = asked(&mut book, &mut host, step, now);
                match (done, step.get("taken"), step.get("refused")) {
                    (Ok(ours), Some(theirs), _) => assert_eq!(&ours, theirs, "{what}"),
                    (Err(ours), _, Some(theirs)) => assert_eq!(&told(&ours), theirs, "{what}"),
                    (ours, theirs, refused) => {
                        panic!("{what}: here {ours:?}, there {theirs:?} {refused:?}")
                    }
                }
            }
            let ours = kept(&book, &host, &sessions, now);
            let theirs = theirs(&step["after"], &book, &host, &sessions);
            for part in ["progress", "runs", "janitors"] {
                assert_eq!(ours[part], theirs[part], "{what}: {part}");
            }
            steps += 1;
        }
    }
    assert_eq!((stories.len(), steps, looks), (6, 155, 102));
    assert_eq!(handed_over, 42);
    // What is kept can be written down and read back as it was.
    let written = serde_json::to_string(&book).unwrap();
    let read: Book = serde_json::from_str(&written).unwrap();
    assert_eq!(read, book);
}

#[test]
fn a_run_is_read_whole_as_the_python_host_reads_it() {
    let vectors = vectors();
    let mut host = Host::of(&vectors);
    let mut book = Book::default();
    for story in vectors["stories"].as_array().unwrap() {
        for step in story["steps"].as_array().unwrap() {
            let now = step["now"].as_i64().unwrap();
            if step["ask"] == "tick" {
                looked(&mut book, &host, &step["given"], now);
            } else {
                let _ = asked(&mut book, &mut host, step, now);
            }
        }
    }
    let mut read = 0;
    for run in vectors["runs"].as_array().unwrap() {
        let ours = &book.runs[run["run_id"].as_str().unwrap()];
        assert_eq!(&ours.shown(), run, "{}", run["run_id"]);
        read += 1;
    }
    assert_eq!((vectors["runs"].as_array().unwrap().len(), read), (40, 40));
}

#[test]
fn what_is_secret_is_taken_out_of_why_a_run_failed_as_the_python_host_takes_it_out() {
    let vectors = vectors();
    let rows = vectors["redactions"].as_array().unwrap();
    for row in rows {
        let text = row[0].as_str().unwrap();
        assert_eq!(runs::redact(text), row[1], "{text}");
    }
    assert_eq!(rows.len(), 28);
    assert_eq!(
        vectors["offered_again_after_ms"],
        runs::OFFERED_AGAIN_AFTER_MS
    );
    assert_eq!(vectors["given_up_after_ms"], runs::GIVEN_UP_AFTER_MS);
}

#[test]
fn a_run_of_a_time_has_one_id_and_is_given_the_owners_words() {
    // The same trigger, generation and time: the same run. Another of any: another.
    let id = runs::run_id("nightly", 3, 1_806_057_000_000);
    assert_eq!(id, runs::run_id("nightly", 3, 1_806_057_000_000));
    assert_eq!(id.len(), "janitor-".len() + 32);
    assert_ne!(id, runs::run_id("nightly", 4, 1_806_057_000_000));
    assert_ne!(id, runs::run_id("nightly", 3, 1_806_057_000_001));
    assert_ne!(id, runs::run_id("daily", 3, 1_806_057_000_000));
}
