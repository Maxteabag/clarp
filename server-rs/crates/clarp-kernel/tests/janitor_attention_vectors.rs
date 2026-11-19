//! When a janitor's failures reach its owner, held to what the Python Host's own functions left
//! step by step (`tests/fixtures/janitor-attention-vectors.json`, written by
//! `server-rs/tools/janitor-attention-vectors.py`): after each run, each change of the
//! configuration, each thing the owner did, the items there are and what `GET /attention` shows
//! of them.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::artifacts;
use clarp_kernel::janitors::book::{Agent, Book, Config, World};
use clarp_kernel::janitors::runs::Run;
use clarp_kernel::janitors::{attention, schedule};
use serde_json::{Map, Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/janitor-attention-vectors.json")).unwrap()
}

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
        Vec::new()
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

fn text(value: &Value) -> String {
    value.as_str().unwrap_or_default().to_string()
}

/// A run as the story wrote it, of the configuration the janitor has now.
fn run(told: &Value, agent_id: &str, session: &str, generation: u64) -> Run {
    Run {
        run_id: text(&told["run_id"]),
        agent_id: agent_id.to_string(),
        session: session.to_string(),
        attachment_id: format!("{session}-attachment"),
        generation,
        trace_id: text(&told["run_id"]),
        status: text(&told["status"]),
        outcome: text(&told["outcome"]),
        configuration: told["configuration"]
            .as_object()
            .cloned()
            .unwrap_or_default(),
        created_at: told["created_at"].as_i64().unwrap(),
        started_at: told["created_at"].as_i64().map(|at| at + 100),
        finished_at: told["finished_at"].as_i64(),
        error: text(&told["error"]),
        demand_result: Some(told["demand_result"].clone()).filter(|result| !result.is_null()),
        ..Run::default()
    }
}

/// The items of one janitor as the reference lists them: by when each was made.
fn items_of(book: &Book, agent_id: &str) -> Vec<Value> {
    let mut own: Vec<&artifacts::Artifact> = book
        .alerts
        .values()
        .filter(|alert| alert.agent_id == agent_id)
        .collect();
    own.sort_by(|one, other| {
        (one.created_at, &one.artifact_id).cmp(&(other.created_at, &other.artifact_id))
    });
    own.into_iter()
        .map(|alert| {
            json!({
                "artifact_id": alert.artifact_id, "reference_id": alert.reference_id,
                "type": alert.kind, "status": alert.status, "title": alert.title,
                "summary": alert.summary, "payload": Value::Object(alert.payload.clone()),
                "archived": alert.archived_at.is_some(), "deleted": alert.deleted_at.is_some(),
            })
        })
        .collect()
}

/// What `GET /attention` shows of one janitor, without the moments.
fn shown(book: &Book, host: &Host, agent_id: &str, include_archived: bool) -> Vec<Value> {
    let listed = book.pending_attention(host, include_archived);
    let mine = listed
        .into_iter()
        .filter(|item| item["agent_id"] == agent_id);
    mine.map(|item| {
        let mut item: Map<String, Value> = item.as_object().cloned().unwrap();
        let archived = !item["archived_at"].is_null();
        for moment in ["created_at", "updated_at", "archived_at", "agent_id"] {
            item.remove(moment);
        }
        item.insert("archived".to_string(), json!(archived));
        Value::Object(item)
    })
    .collect()
}

#[test]
fn a_janitors_failures_reach_its_owner_as_the_python_host_raises_keeps_and_closes_them() {
    let vectors = vectors();
    assert_eq!(vectors["kind"], attention::KIND);
    assert_eq!(vectors["prefix"], attention::PREFIX);
    let mut now = 1_900_000_000_000_i64;
    for story in vectors["stories"].as_array().unwrap() {
        let what = text(&story["what"]);
        let (agent_id, session) = (text(&story["agent_id"]), text(&story["session"]));
        let template = text(&story["template_id"]);
        // The job's name is the reference's.
        let job = clarp_kernel::janitors::template(&template).unwrap();
        assert_eq!(job["name"], story["job_name"], "{what}");
        let agent = Agent {
            agent_id: agent_id.clone(),
            session: session.clone(),
            name: text(&story["persona"]),
            is_janitor: true,
            ..Agent::default()
        };
        let mut host = Host {
            agents: BTreeMap::from([(agent_id.clone(), agent)]),
        };
        let mut book = Book::default();
        let config = Config {
            template_id: template.clone(),
            enabled: true,
            revision: 1,
            generation: 1,
            ..Config::default()
        };
        book.configs.insert(agent_id.clone(), config);
        for step in story["steps"].as_array().unwrap() {
            now += 1_000;
            let said = format!("{what}: {}", text(&step["step"]));
            let generation = book.configs[&agent_id].generation;
            match step["action"].as_str().unwrap() {
                "run" | "runs" => {
                    for told in step["runs"].as_array().unwrap() {
                        let run = run(told, &agent_id, &session, generation);
                        book.runs.insert(run.run_id.clone(), run);
                    }
                }
                action @ ("archive" | "discard") => {
                    let artifact_id = text(&step["artifact_id"]);
                    let current = book.alerts[&artifact_id].clone();
                    let done = if action == "archive" {
                        artifacts::archived(&current, true, current.updated_at, now)
                    } else {
                        artifacts::discarded(&current, current.updated_at, now)
                    };
                    let (changed, moved) = done.unwrap();
                    assert!(moved, "{said}");
                    book.alerts.insert(artifact_id, changed);
                }
                "generation" => book.configs.get_mut(&agent_id).unwrap().generation += 1,
                "enabled" => {
                    let on = step["enabled"].as_bool().unwrap();
                    book.configs.get_mut(&agent_id).unwrap().enabled = on;
                }
                "janitor" => {
                    let is = step["is_janitor"].as_bool().unwrap();
                    host.agents.get_mut(&agent_id).unwrap().is_janitor = is;
                }
                "archived" => host.agents.get_mut(&agent_id).unwrap().archived = true,
                other => panic!("{said}: no such step {other}"),
            }
            let changed = book.reconcile_attention(&host, now);
            assert_eq!(json!(changed), step["changed"], "{said}");
            assert_eq!(json!(items_of(&book, &agent_id)), step["alerts"], "{said}");
            assert_eq!(
                json!(shown(&book, &host, &agent_id, false)),
                step["pending"],
                "{said}"
            );
            assert_eq!(
                json!(shown(&book, &host, &agent_id, true)),
                step["pending_with_archived"],
                "{said}"
            );
            // Asked again with nothing new, nothing is written again.
            assert_eq!(book.reconcile_attention(&host, now + 1), 0, "{said}");
        }
    }
}

#[test]
fn an_item_is_named_by_its_episode_alone() {
    let reference = "janitor-failure:agent-1:1:initial";
    let id = attention::alert_id(reference);
    assert!(id.starts_with(attention::ALERT) && id.len() == attention::ALERT.len() + 40);
    assert_eq!(id, attention::alert_id(reference));
    assert_ne!(id, attention::alert_id("janitor-failure:agent-1:2:initial"));
    assert!(attention::is_alert(&id));
}
