//! The books of what is said aloud, held to what the Python Host's own functions left step
//! by step (`tests/fixtures/audio-bookkeeper-vectors.json`, written by
//! `server-rs/tools/audio-bookkeeper-vectors.py`): what its database sees of a clip, and
//! what a pass of its bookkeeper writes down of it.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::janitors::audio::{self, Seen};
use clarp_kernel::janitors::book::{Agent, Asked, Book, Config, World};
use clarp_kernel::janitors::{Attachment, schedule};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/audio-bookkeeper-vectors.json")).unwrap()
}

const T0: i64 = 1_800_000_000_000;

/// The made-up Host the stories happen on.
struct Host {
    agents: BTreeMap<String, Agent>,
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
        "attachment-raw-1".to_string()
    }

    fn backend(&self, name: &str) -> bool {
        ["claude", "codex"].contains(&name)
    }

    fn model(&self, _backend: &str, _model: &str) -> bool {
        true
    }

    fn efforts(&self, _backend: &str) -> Vec<String> {
        let efforts = ["minimal", "low", "medium", "high", "xhigh"];
        efforts.iter().map(|word| (*word).to_string()).collect()
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
        "000000000001".to_string()
    }

    fn face(&self, _file: &str) -> String {
        String::new()
    }
}

/// The Host on its first day: its agents, and its bookkeeper as the Python Host installed it.
fn first_day(vectors: &Value) -> (Host, Book, String) {
    let word = |value: &Value| value.as_str().unwrap_or_default().to_string();
    let mut host = Host {
        agents: BTreeMap::new(),
    };
    for agent in vectors["agents"].as_array().unwrap() {
        let made = Agent {
            agent_id: word(&agent["agent_id"]),
            session: word(&agent["session"]),
            name: word(&agent["persona"]),
            backend: word(&agent["backend"]),
            ..Agent::default()
        };
        host.agents.insert(made.agent_id.clone(), made);
    }
    let janitor = &vectors["first_day"];
    let id = word(&janitor["agent_id"]);
    let own = Agent {
        agent_id: id.clone(),
        session: word(&janitor["session"]),
        name: word(&janitor["name"]),
        backend: word(&janitor["backend"]),
        model: word(&janitor["model"]),
        effort: word(&janitor["effort"]),
        is_janitor: true,
        ..Agent::default()
    };
    let session = own.session.clone();
    host.agents.insert(id.clone(), own);
    let config = Config {
        template_id: audio::ROLE.to_string(),
        enabled: janitor["enabled"] == true,
        revision: 1,
        generation: 1,
        execution: janitor["execution"].as_object().unwrap().clone(),
        created_at: T0,
        updated_at: T0,
        ..Config::default()
    };
    let attachments = janitor["attachments"].as_array().unwrap().iter();
    let attachments = attachments.map(|one| Attachment {
        attachment_id: word(&one["attachment_id"]),
        trigger_id: word(&one["trigger_id"]),
        trigger_version: one["trigger_version"].as_i64().unwrap(),
        enabled: one["enabled"] == true,
        config: one["config"].as_object().unwrap().clone(),
    });
    let mut book = Book::default();
    book.install(audio::ROLE, &id, config, attachments.collect());
    (host, book, session)
}

/// What a step was given, with every agent by its id here.
fn filled(given: &Value) -> Value {
    let mut text = given.to_string();
    for name in ["worker", "other", "third"] {
        text = text.replace(&format!("{{{name}}}"), &format!("id-{name}"));
    }
    serde_json::from_str(&text).unwrap()
}

#[test]
fn what_is_seen_of_clips_is_written_down_as_the_python_host_writes_it() {
    let vectors = vectors();
    let (mut host, mut book, session) = first_day(&vectors);
    let mut seen = Seen::default();
    // A clip by the name its step gives it: its number, its agent, its turn.
    let mut clips: BTreeMap<String, (u64, String, Option<u64>)> = BTreeMap::new();
    let (mut steps, mut passes, mut written_down) = (0, 0, 0);
    for story in vectors["stories"].as_array().unwrap() {
        let name = story["name"].as_str().unwrap();
        for (number, step) in story["steps"].as_array().unwrap().iter().enumerate() {
            let context = format!("{name}, step {number} ({})", step["what"]);
            let now = step["now"].as_i64().unwrap();
            let given = filled(&step["given"]);
            let word = |key: &str| given[key].as_str().unwrap_or_default().to_string();
            let revision = |book: &Book, host: &Host| {
                let shown = book.shown(&session, host, now).unwrap();
                shown["revision"].clone()
            };
            match step["ask"].as_str().unwrap() {
                "clip" => {
                    let clip_id = clips.len() as u64 + 1;
                    let (agent, turn) = (word("agent"), given["turn"].as_u64());
                    // A clip is made by its producer and for its listener at once: seen in
                    // that order. One that came whole says so.
                    let says = given["producer"].as_str().unwrap_or("complete");
                    seen.observe(clip_id, &agent, turn, &audio::producer(says), now);
                    seen.observe(clip_id, &agent, turn, &audio::client("synthesized"), now);
                    clips.insert(word("name"), (clip_id, agent, turn));
                }
                asked @ ("producer" | "status") => {
                    let (clip_id, agent, turn) = clips[&word("name")].clone();
                    let stage = match asked {
                        "producer" => audio::producer(&word("to")),
                        _ => audio::client(&word("to")),
                    };
                    seen.observe(clip_id, &agent, turn, &stage, now);
                }
                "drain" => {
                    let limit = given["limit"].as_u64();
                    let limit = limit.map_or(audio::AT_A_TIME, |limit| limit as usize);
                    let written = book.keep_audio(&mut seen, &host, now, limit);
                    assert_eq!(json!(written.len()), step["answer"], "{context}");
                    let runs = book.runs_of(&session, 100, &host).unwrap();
                    assert_eq!(
                        json!(runs),
                        step["runs"],
                        "{context}: the bookkeeper's runs"
                    );
                    passes += 1;
                    written_down += written.len();
                }
                "enabled" => {
                    let on = given["on"] == true;
                    let seen_revision = revision(&book, &host);
                    book.set_enabled(&session, &seen_revision, on, &host, now)
                        .unwrap();
                }
                "configure" => {
                    let mut asked = Asked::new();
                    asked.insert("scope".to_string(), given["scope"].clone());
                    let seen_revision = revision(&book, &host);
                    book.configure(&session, &seen_revision, &asked, &mut host, now)
                        .unwrap();
                }
                other => panic!("{context}: {other}"),
            }
            // After every step: everything that was seen, and how the bookkeeper stands.
            let named = |clip_id: u64| {
                let mut known = clips.iter();
                known.find(|(_, (id, _, _))| *id == clip_id).unwrap().0
            };
            let ours: Vec<Value> = seen
                .all()
                .iter()
                .map(|fact| {
                    json!({
                        "event_id": fact.event_id, "clip": named(fact.clip_id),
                        "clip_id": fact.clip_id, "agent_id": fact.agent_id,
                        "turn_id": fact.turn_id, "runtime_id": null, "stage": fact.stage,
                        "run_id": fact.run_id, "completed_at": fact.completed_at,
                    })
                })
                .collect();
            assert_eq!(json!(ours), step["seen"], "{context}: what was seen");
            let shown = book.shown(&session, &host, now).unwrap();
            let keys = [
                "enabled",
                "revision",
                "generation",
                "scope",
                "last_run_at",
                "last_error",
            ];
            let stands: serde_json::Map<String, Value> = keys
                .iter()
                .map(|key| ((*key).to_string(), shown[*key].clone()))
                .collect();
            assert_eq!(
                Value::Object(stands),
                step["keeper"],
                "{context}: the bookkeeper"
            );
            steps += 1;
        }
    }
    // Counted by the script that wrote the vectors.
    assert_eq!((steps, passes, written_down), (64, 15, 66));
    assert_eq!(seen.all().len(), 66);
    assert_eq!(seen.last(), 66);
}

#[test]
fn what_was_seen_long_ago_is_forgotten_and_its_number_is_never_given_again() {
    let mut seen = Seen::default();
    seen.observe(1, "a", Some(3), &audio::producer("streaming"), 1_000);
    seen.observe(1, "a", Some(3), &audio::client("synthesized"), 1_000);
    seen.observe(2, "a", None, &audio::producer("complete"), 5_000);
    // Seen a second time: no new fact.
    assert!(
        seen.observe(1, "a", Some(3), &audio::client("synthesized"), 9_000)
            .is_none()
    );
    let gone = seen.forget_before(2_000);
    assert_eq!(gone.len(), 2);
    assert_eq!(seen.all().len(), 1);
    let next = seen.observe(1, "a", Some(3), &audio::client("play-ok"), 9_000);
    assert_eq!(next.unwrap().event_id, 4);
    // Read again where it is kept: the numbers go on from the last that was given.
    let kept: Vec<audio::Fact> = seen.all().into_iter().cloned().collect();
    let mut again = Seen::new(kept, 4);
    assert_eq!(again.last(), 4);
    let next = again.observe(7, "b", None, &audio::producer("failed"), 9_500);
    assert_eq!(next.unwrap().event_id, 5);
    // A number that was kept smaller than what is there is put right.
    let kept: Vec<audio::Fact> = again.all().into_iter().cloned().collect();
    assert_eq!(Seen::new(kept, 0).last(), 5);
}
