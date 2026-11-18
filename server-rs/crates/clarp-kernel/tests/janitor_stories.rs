//! The book of janitors, held to what the Python Host answered step by step as janitors were
//! made, changed, turned on and off and removed (`tests/fixtures/janitor-vectors.json`, written
//! by `server-rs/tools/janitor-vectors.py`, which runs the Python Host's own modules).

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::janitors::book::{Agent, Asked, Book, Config, Effect, World};
use clarp_kernel::janitors::{self, Attachment, Refused};
use serde_json::{Map, Value, json};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/janitor-vectors.json")).unwrap()
}

const NOW: i64 = 1_800_000_000_000;
const ZONES: [&str; 2] = ["UTC", "Europe/Oslo"];

/// The made-up Host the stories happen on: its agents, and what the book asks of it.
struct Host {
    agents: BTreeMap<String, Agent>,
    drawn: u32,
    /// Where each picture is fetched, and how far apart a schedule's runs are: as the
    /// Python Host said them in the same answers.
    faces: BTreeMap<String, String>,
    bases: BTreeMap<(String, String), i64>,
}

impl World for Host {
    fn agent(&self, key: &str) -> Option<Agent> {
        let by_session = || self.agents.values().find(|agent| agent.session == key);
        self.agents.get(key).or_else(by_session).cloned()
    }

    fn zone(&self, name: &str) -> bool {
        ZONES.contains(&name)
    }

    fn draw(&mut self) -> String {
        self.drawn += 1;
        format!("attachment-raw-{}", self.drawn)
    }

    fn backend(&self, name: &str) -> bool {
        ["claude", "codex", "agy"].contains(&name)
    }

    fn model(&self, _backend: &str, _model: &str) -> bool {
        true
    }

    fn efforts(&self, _backend: &str) -> Vec<String> {
        ["low", "medium", "high"]
            .iter()
            .map(|word| (*word).to_string())
            .collect()
    }

    fn base_interval(&self, expression: &str, zone: &str, _now_ms: i64) -> i64 {
        let key = (expression.to_string(), zone.to_string());
        *self
            .bases
            .get(&key)
            .unwrap_or_else(|| panic!("no base is known for {key:?}"))
    }

    fn next_run(&self, _expression: &str, _zone: &str, _from_ms: i64) -> Option<i64> {
        // Nothing runs in these stories.
        None
    }

    fn directory(&self, path: &str) -> String {
        path.to_string()
    }

    fn drawn(&mut self) -> String {
        self.drawn += 1;
        format!("{:012x}", self.drawn)
    }

    fn face(&self, file: &str) -> String {
        self.faces.get(file).cloned().unwrap_or_default()
    }
}

impl Host {
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
                Effect::Tune {
                    agent_id,
                    backend,
                    model,
                    effort,
                } => {
                    let agent = self.agents.get_mut(&agent_id).unwrap();
                    agent.backend = backend.unwrap_or(agent.backend.clone());
                    agent.model = model.unwrap_or(agent.model.clone());
                    agent.effort = effort.unwrap_or(agent.effort.clone());
                }
            }
        }
    }
}

/// Every janitor an answer of the Python Host shows: for what it says of pictures and of
/// schedules, which the book asks its Host.
fn janitors_in(value: &Value, found: &mut Vec<Value>) {
    match value {
        Value::Object(fields)
            if fields.contains_key("template_id") && fields.contains_key("cadence") =>
        {
            found.push(value.clone());
        }
        Value::Object(fields) => fields.values().for_each(|inner| janitors_in(inner, found)),
        Value::Array(items) => items.iter().for_each(|inner| janitors_in(inner, found)),
        _ => {}
    }
}

/// The made-up Host as it stands on its first day, and the book with the Host's own janitors
/// in it as the Python Host installed them.
fn first_day(vectors: &Value) -> (Host, Book, BTreeMap<String, String>) {
    let word = |value: &Value| value.as_str().unwrap().to_string();
    let mut host = Host {
        agents: BTreeMap::new(),
        drawn: 0,
        faces: BTreeMap::new(),
        bases: BTreeMap::new(),
    };
    for agent in vectors["agents"].as_array().unwrap() {
        let made = Agent {
            agent_id: word(&agent["agent_id"]),
            session: word(&agent["session"]),
            name: word(&agent["persona"]),
            backend: word(&agent["backend"]),
            archived: agent["archived"] == true,
            ..Agent::default()
        };
        host.agents.insert(made.agent_id.clone(), made);
    }
    let mut shown = Vec::new();
    janitors_in(vectors, &mut shown);
    for janitor in &shown {
        let file = janitors::portrait(janitor["template_id"].as_str().unwrap());
        if janitor["avatar_url"] != "" {
            host.faces
                .insert(file.to_string(), word(&janitor["avatar_url"]));
        }
        for lane in janitor["cadence"].as_array().unwrap() {
            let name = lane["lane"].as_str().unwrap();
            let Some(attachment_id) = name.strip_prefix("schedule:") else {
                continue;
            };
            let attachments = janitor["attachments"].as_array().unwrap();
            let own = attachments
                .iter()
                .find(|one| one["attachment_id"] == attachment_id)
                .unwrap();
            let key = (
                word(&own["config"]["cron"]),
                word(&own["config"]["timezone"]),
            );
            host.bases
                .insert(key, lane["base_interval_seconds"].as_i64().unwrap());
        }
    }
    let (mut book, mut roles) = (Book::default(), BTreeMap::new());
    for janitor in vectors["first_day"].as_array().unwrap() {
        let (id, role) = (word(&janitor["agent_id"]), word(&janitor["builtin_role"]));
        host.agents.insert(
            id.clone(),
            Agent {
                agent_id: id.clone(),
                session: word(&janitor["session"]),
                name: word(&janitor["name"]),
                backend: word(&janitor["backend"]),
                model: word(&janitor["model"]),
                effort: word(&janitor["effort"]),
                is_janitor: true,
                ..Agent::default()
            },
        );
        let config = Config {
            template_id: word(&janitor["template_id"]),
            enabled: janitor["enabled"] == true,
            revision: 1,
            generation: 1,
            execution: janitor["execution"].as_object().unwrap().clone(),
            created_at: NOW,
            updated_at: NOW,
            ..Config::default()
        };
        let attachments = janitor["attachments"]
            .as_array()
            .unwrap()
            .iter()
            .map(|one| Attachment {
                attachment_id: word(&one["attachment_id"]),
                trigger_id: word(&one["trigger_id"]),
                trigger_version: one["trigger_version"].as_i64().unwrap(),
                enabled: one["enabled"] == true,
                config: one["config"].as_object().unwrap().clone(),
            });
        book.install(&role, &id, config, attachments.collect());
        roles.insert(role, id);
    }
    (host, book, roles)
}

fn told(refused: &Refused) -> Value {
    json!([refused.status, refused.code, refused.message])
}

#[test]
fn the_hosts_own_janitors_are_shown_as_the_python_host_shows_them() {
    let vectors = vectors();
    let (host, book, _) = first_day(&vectors);
    let rows = vectors["first_day"].as_array().unwrap();
    for theirs in rows {
        let mut ours = book
            .shown(theirs["agent_id"].as_str().unwrap(), &host, NOW)
            .unwrap();
        // A janitor the Python Host runs by itself and that is on has a next run there; no
        // janitor runs here yet.
        let mut theirs = theirs.clone();
        for lanes in [&mut ours, &mut theirs] {
            for lane in lanes["cadence"].as_array_mut().unwrap() {
                lane["next_run_at"] = Value::Null;
            }
        }
        assert_eq!(ours, theirs, "{}", theirs["session"]);
    }
    assert_eq!(rows.len(), 7);
    let listed = book.listed(&host);
    assert_eq!(listed.len(), 7);
}

#[test]
fn a_janitors_life_is_answered_step_by_step_as_the_python_host_answers_it() {
    let vectors = vectors();
    let (mut host, mut book, roles) = first_day(&vectors);
    let stories = vectors["stories"].as_array().unwrap();
    // An attachment that names no id is given one the Host draws: told, there and here, as
    // the number of its first appearing in an answer.
    let first_number = {
        let whole = vectors["stories"].to_string();
        let numbers = whole.match_indices("<drawn:").map(|(at, _)| {
            let rest = &whole[at + "<drawn:".len()..];
            rest[..rest.find('>').unwrap()].parse::<u32>().unwrap()
        });
        numbers.min().unwrap()
    };
    let mut names: Vec<String> = Vec::new();
    let mut named = |value: &Value| -> Value {
        let mut text = value.to_string();
        let mut from = 0;
        while let Some(at) = text[from..].find("attachment-raw-") {
            let start = from + at;
            let digits = text[start + "attachment-raw-".len()..]
                .chars()
                .take_while(char::is_ascii_digit)
                .count();
            let raw = text[start..start + "attachment-raw-".len() + digits].to_string();
            if !names.contains(&raw) {
                names.push(raw.clone());
            }
            let place = names.iter().position(|known| *known == raw).unwrap();
            let name = format!("<drawn:{}>", first_number + u32::try_from(place).unwrap());
            text.replace_range(start..start + raw.len(), &name);
            from = start + name.len();
        }
        serde_json::from_str(&text).unwrap()
    };
    let mut steps = 0;
    for story in stories {
        for step in story["steps"].as_array().unwrap() {
            let what = format!("{}: {}", story["name"], step["what"]);
            let of = step["of"].as_str().unwrap();
            let key = match of.strip_prefix('{').and_then(|role| role.strip_suffix('}')) {
                Some(role) => roles[role].clone(),
                None => of.to_string(),
            };
            // `{coder}` in what is given stands for that agent's id.
            let mut given = step["given"].to_string();
            for agent in vectors["agents"].as_array().unwrap() {
                let session = agent["session"].as_str().unwrap();
                given = given.replace(&format!("{{{session}}}"), &format!("id-{session}"));
            }
            let mut given: Asked = serde_json::from_str(&given).unwrap();
            let revision = given.remove("expected_revision").unwrap_or(Value::Null);
            let done: Result<Value, Refused> = match step["ask"].as_str().unwrap() {
                "get" => book.shown(&key, &host, NOW),
                "create" => book
                    .create(&key, &given, &mut host, NOW)
                    .and_then(|effects| {
                        host.becomes(effects);
                        book.shown(&key, &host, NOW)
                    }),
                "configure" => book
                    .configure(&key, &revision, &given, &mut host, NOW)
                    .and_then(|effects| {
                        host.becomes(effects);
                        book.shown(&key, &host, NOW)
                    }),
                "enabled" => {
                    let on = given["enabled"] == true;
                    let turned = book.set_enabled(&key, &revision, on, &host, NOW);
                    turned.and_then(|()| book.shown(&key, &host, NOW))
                }
                "remove" => book.remove(&key, &revision, &host, NOW).map(|effects| {
                    host.becomes(effects);
                    json!(true)
                }),
                "reset" => book
                    .reset_defaults(&key, &revision, &mut host, NOW)
                    .and_then(|effects| {
                        host.becomes(effects);
                        book.shown(&key, &host, NOW)
                    }),
                "release" => {
                    let successor = (
                        given.get("successor_session"),
                        given.get("successor_revision"),
                    );
                    let let_go = book.release(&key, &revision, successor, &host, NOW);
                    let_go.and_then(|effects| {
                        host.becomes(effects);
                        book.shown(&key, &host, NOW)
                    })
                }
                "adopt" => {
                    let taken = book.adopt_options(&key, &revision, &given["options"], &host, NOW);
                    taken.and_then(|()| book.shown(&key, &host, NOW))
                }
                "list" => {
                    let mut rows: Vec<Value> = Vec::new();
                    for id in book.listed(&host) {
                        let one = book.shown(&id, &host, NOW).unwrap();
                        rows.push(json!([
                            one["session"],
                            one["enabled"],
                            one["revision"],
                            one["generation"]
                        ]));
                    }
                    rows.sort_by_key(|row| row[0].as_str().unwrap().to_string());
                    Ok(json!(rows))
                }
                other => panic!("{what}: nothing is known as {other}"),
            };
            match (done, step.get("taken"), step.get("refused")) {
                (Ok(ours), Some(theirs), _) => assert_eq!(&named(&ours), theirs, "{what}"),
                (Err(ours), _, Some(theirs)) => assert_eq!(&told(&ours), theirs, "{what}"),
                (ours, theirs, refused) => {
                    panic!("{what}: here {ours:?}, there {theirs:?} {refused:?}")
                }
            }
            steps += 1;
        }
    }
    assert_eq!((stories.len(), steps), (3, 79));
    // What is kept can be written down and read back as it was.
    let written = serde_json::to_string(&book).unwrap();
    let read: Book = serde_json::from_str(&written).unwrap();
    assert_eq!(read, book);
    let _: Map<String, Value> = serde_json::from_str(&written).unwrap();
}
