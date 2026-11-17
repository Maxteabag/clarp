//! A janitor that is made as a new agent, held to what the Python Host answered
//! (`tests/fixtures/janitor-creation-vectors.json`, written by
//! `server-rs/tools/janitor-creation-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::janitors::book::{Agent, Asked, Book, Effect, World};
use clarp_kernel::janitors::{self, Refused, schedule};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/janitor-creation-vectors.json")).unwrap()
}

const NOW: i64 = 1_800_000_000_000;
const WORK: &str = "/srv/work";
const HOME: &str = "/srv/home";

/// The made-up Host: its agents, one directory that is there, and what it has drawn.
struct Host {
    agents: BTreeMap<String, Agent>,
    drawn: Vec<String>,
    face: String,
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
        format!("attachment-raw-{}", self.drawn.len())
    }

    fn backend(&self, name: &str) -> bool {
        ["claude", "codex", "agy", "grok", "opencode"].contains(&name)
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
        let there = path.trim() == WORK;
        if there { WORK } else { HOME }.to_string()
    }

    fn drawn(&mut self) -> String {
        let drawn = format!("{:012x}", self.drawn.len() + 1);
        self.drawn.push(drawn.clone());
        drawn
    }

    fn face(&self, _file: &str) -> String {
        self.face.clone()
    }
}

impl Host {
    fn new(face: &str) -> Self {
        Self {
            agents: BTreeMap::new(),
            drawn: Vec::new(),
            face: face.to_string(),
        }
    }

    /// An answer with what this Host drew for a session told as that.
    fn named(&self, value: &Value) -> Value {
        let mut text = value.to_string();
        for drawn in &self.drawn {
            text = text.replace(&format!("-{drawn}"), "-<drawn>");
        }
        serde_json::from_str(&text).unwrap()
    }
}

/// What a request says, with the directory that is there under its name.
fn filled(payload: &Value) -> Value {
    serde_json::from_str(&payload.to_string().replace("{work}", WORK)).unwrap()
}

fn told(refused: &Refused) -> Value {
    json!([refused.status, refused.code, refused.message])
}

#[test]
fn a_request_for_a_new_janitor_is_checked_whole_before_anything_is_made() {
    let vectors = vectors();
    let (mut host, mut book) = (Host::new(""), Book::default());
    let rows = vectors["begins"].as_array().unwrap();
    let mut refused = 0;
    for row in rows {
        let begun =
            book.begin_creation(&row["request_id"], &filled(&row["payload"]), &mut host, NOW);
        match (begun, row.get("taken"), row.get("refused")) {
            (Ok(ours), Some(theirs), _) => {
                assert_eq!(&host.named(&ours.shown()), theirs, "{}", row["request_id"]);
            }
            (Err(ours), _, Some(theirs)) => {
                assert_eq!(&told(&ours), theirs, "{}", row["request_id"]);
                refused += 1;
            }
            (ours, theirs, why) => panic!(
                "{}: here {ours:?}, there {theirs:?} {why:?}",
                row["request_id"]
            ),
        }
    }
    assert_eq!((rows.len(), refused), (48, 25));
    // What was refused left nothing behind, and each one taken has a session of its own.
    let sessions: std::collections::BTreeSet<&String> =
        book.creations.values().map(|kept| &kept.session).collect();
    assert_eq!((book.creations.len(), sessions.len()), (23, 23));
    // The model a tool runs a janitor on when none is named is the Python Host's.
    for (backend, model) in vectors["default_models"].as_object().unwrap() {
        assert_eq!(janitors::default_model(backend), model, "{backend}");
    }
}

#[test]
fn a_new_janitor_is_made_once_however_often_its_request_is_sent() {
    let vectors = vectors();
    let steps = vectors["story"].as_array().unwrap();
    let job = steps.iter().find(|step| step["do"] == "job").unwrap();
    let mut host = Host::new(job["taken"]["avatar_url"].as_str().unwrap());
    let mut book = Book::default();
    let mut session = String::new();
    for step in steps {
        let what = step["what"].as_str().unwrap();
        let given = &step["given"];
        let id = given["request_id"].as_str().unwrap();
        let done: Result<Value, Refused> = match step["do"].as_str().unwrap() {
            "begin" => {
                let begun =
                    book.begin_creation(&json!(id), &filled(&given["payload"]), &mut host, NOW);
                begun.map(|begun| {
                    session.clone_from(&begun.session);
                    begun.shown()
                })
            }
            "made" => {
                // The Host makes the agent the request reserved, as a janitor, and says so.
                let kept = &book.creations[id];
                let agent = Agent {
                    agent_id: "id-made".to_string(),
                    session: kept.session.clone(),
                    name: kept.identity.persona.clone(),
                    backend: kept.identity.backend.clone(),
                    model: kept.identity.model.clone(),
                    effort: kept.identity.effort.clone(),
                    is_janitor: true,
                    ..Agent::default()
                };
                let row = json!({
                    "agent_id": agent.agent_id, "session": agent.session, "persona": agent.name,
                    "backend": agent.backend, "model": agent.model, "effort": agent.effort,
                    "cwd": kept.identity.cwd, "is_janitor": true,
                });
                host.agents.insert(agent.agent_id.clone(), agent);
                book.creation_made(id, "id-made");
                Ok(row)
            }
            "job" => {
                let fields: Asked = filled(&given["fields"]).as_object().unwrap().clone();
                let made = book.create(&session, &fields, &mut host, NOW);
                made.and_then(|effects| {
                    // It is a janitor already: nothing more becomes of its agent.
                    assert!(
                        effects
                            .iter()
                            .all(|effect| matches!(effect, Effect::Convert { .. }))
                    );
                    book.shown(&session, &host, NOW)
                })
            }
            "complete" => book.complete_creation(id, &session, &host, NOW),
            "complete-elsewhere" => book.complete_creation(id, "another-session", &host, NOW),
            other => panic!("{what}: nothing is known as {other}"),
        };
        match (done, step.get("taken"), step.get("refused")) {
            (Ok(ours), Some(theirs), _) => assert_eq!(&host.named(&ours), theirs, "{what}"),
            (Err(ours), _, Some(theirs)) => assert_eq!(&told(&ours), theirs, "{what}"),
            (ours, theirs, why) => panic!("{what}: here {ours:?}, there {theirs:?} {why:?}"),
        }
    }
    assert_eq!(steps.len(), 13);
    // What is kept can be written down and read back as it was.
    let written = serde_json::to_string(&book).unwrap();
    let read: Book = serde_json::from_str(&written).unwrap();
    assert_eq!(read, book);
}
