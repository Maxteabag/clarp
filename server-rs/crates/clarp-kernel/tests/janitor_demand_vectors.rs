//! Runs of a janitor that answer one request, held to what the Python Host's own functions
//! answered step by step (`tests/fixtures/janitor-demand-vectors.json`, written by
//! `server-rs/tools/janitor-demand-vectors.py`).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::janitors::book::{Agent, Asked, Book, Config, World};
use clarp_kernel::janitors::{Attachment, Refused, demand, schedule};
use serde_json::{Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/janitor-demand-vectors.json")).unwrap()
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

/// The Host on its first day: its agents, and the two janitors of its own that answer
/// requests, as the Python Host installed them.
fn first_day(vectors: &Value) -> (Host, Book, BTreeMap<String, String>) {
    let word = |value: &Value| value.as_str().unwrap().to_string();
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
    let (mut book, mut sessions) = (Book::default(), BTreeMap::new());
    for (role, janitor) in vectors["first_day"].as_object().unwrap() {
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
        sessions.insert(role.clone(), own.session.clone());
        host.agents.insert(id.clone(), own);
        let config = Config {
            template_id: role.clone(),
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
        book.install(role, &id, config, attachments.collect());
    }
    (host, book, sessions)
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

#[test]
fn the_janitors_that_answer_requests_are_installed_as_the_python_host_installs_them() {
    let vectors = vectors();
    let (host, book, sessions) = first_day(&vectors);
    for (role, theirs) in vectors["first_day"].as_object().unwrap() {
        assert_eq!(
            &shown(&book, &host, &sessions[role], T0).unwrap(),
            theirs,
            "{role}"
        );
    }
    assert_eq!(vectors["given_up_after_ms"], demand::GIVEN_UP_AFTER_MS);
}

#[test]
fn a_request_is_one_run_claimed_once_and_answered_once_as_on_the_python_host() {
    let vectors = vectors();
    let (mut host, mut book, sessions) = first_day(&vectors);
    let stories = vectors["stories"].as_array().unwrap();
    let (mut steps, mut refused) = (0, 0);
    for story in stories {
        for step in story["steps"].as_array().unwrap() {
            let what = format!("{}: {}", story["name"], step["what"]);
            let now = step["now"].as_i64().unwrap();
            // `{worker}` in what is given stands for that agent's id.
            let mut given = step["given"].to_string();
            for agent in vectors["agents"].as_array().unwrap() {
                let session = agent["session"].as_str().unwrap();
                given = given.replace(&format!("{{{session}}}"), &format!("id-{session}"));
            }
            let given: Value = serde_json::from_str(&given).unwrap();
            let said = |key: &str| given[key].as_str();
            let role = said("role").unwrap_or_default();
            let session = sessions.get(role).cloned().unwrap_or_default();
            let run = said("run").unwrap_or_default();
            let seen = |book: &Book, host: &Host| {
                shown(book, host, &session, now).unwrap()["revision"].clone()
            };
            let done: Result<Value, Refused> = match step["ask"].as_str().unwrap() {
                "resolve" => book
                    .resolve(role, said("target"), &host)
                    .map(|found| match found {
                        Some(id) => shown(&book, &host, &id, now).unwrap(),
                        None => Value::Null,
                    }),
                "begin" => {
                    let begun = book.begin_run(
                        role,
                        &given["request"],
                        given.get("context"),
                        said("target"),
                        &host,
                        now,
                    );
                    begun.map(|run| run.unwrap_or(Value::Null))
                }
                "request-run" => {
                    let found = book.request_run(role, &given["request"]);
                    found.map(|run| run.unwrap_or(Value::Null))
                }
                "current" => Ok(json!(book.is_current(run, &host, now))),
                "claim" => Ok(json!(book.claim_run(run, &host, now))),
                "complete" => {
                    let reported =
                        said("activity").map(|activity| (activity, said("summary").unwrap_or("")));
                    let kept = book.complete_run(
                        run,
                        said("outcome").unwrap_or_default(),
                        given.get("result"),
                        said("error").unwrap_or(""),
                        reported,
                        &host,
                        now,
                    );
                    kept.map(|kept| json!(kept))
                }
                "recover" => Ok(json!(book.recover_expired(now))),
                "get" => shown(&book, &host, &session, now),
                "enabled" => {
                    let on = given["enabled"] == true;
                    let revision = seen(&book, &host);
                    let turned = book.set_enabled(&session, &revision, on, &host, now);
                    turned.and_then(|()| shown(&book, &host, &session, now))
                }
                "configure" => {
                    let mut asked = Asked::new();
                    asked.insert("options".to_string(), given["options"].clone());
                    let revision = seen(&book, &host);
                    let set = book.configure(&session, &revision, &asked, &mut host, now);
                    set.and_then(|_| shown(&book, &host, &session, now))
                }
                "runs" => book.runs_of(&session, 100, &host).map(Value::Array),
                other => panic!("{what}: nothing is known as {other}"),
            };
            match (done, step.get("taken"), step.get("refused")) {
                (Ok(ours), Some(theirs), _) => assert_eq!(&ours, theirs, "{what}"),
                (Err(ours), _, Some(theirs)) => {
                    assert_eq!(&told(&ours), theirs, "{what}");
                    refused += 1;
                }
                (ours, theirs, why) => panic!("{what}: here {ours:?}, there {theirs:?} {why:?}"),
            }
            steps += 1;
        }
    }
    assert_eq!((stories.len(), steps, refused), (3, 73, 13));
    // What is kept can be written down and read back as it was.
    let written = serde_json::to_string(&book).unwrap();
    let read: Book = serde_json::from_str(&written).unwrap();
    assert_eq!(read, book);
}
