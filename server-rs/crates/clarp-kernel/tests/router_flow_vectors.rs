//! A message's way through the router of spoken messages, held to what the Python Host's
//! router did with the same messages (`tests/fixtures/router-flow-vectors.json`, written by
//! `server-rs/tools/router-flow-vectors.py`, which drives `OrchestratorService` with a model
//! that answers what each story says, and a Host around it that writes down what it is
//! asked to do), as that router is since its PR 436 of 18 Nov 2026: a command is never
//! held, a held message belongs to the session it was made in, and a name given anywhere is
//! the target, resolved as a voice name; and since its PR 443 of 19 Nov 2026: what nothing was
//! delivered for answers the session spoken to and names the session its target resolved to, an
//! app is told nothing aloud, and a message sent again is answered what it was answered first.
//! Where only the words of an error are another's, `expected` says so.

#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;

use clarp_kernel::janitors::book::{Agent, Asked, Book as Janitors, Config, World};
use clarp_kernel::janitors::{Attachment, demand, schedule};
use clarp_kernel::python;
use clarp_kernel::router::flow::{
    Around, Begun, Book, Done, Effect, Give, Message, Next, Outcome, Routing, Say, Start,
};
use clarp_kernel::router::packet::{self, Came, Member, Said, State};
use clarp_kernel::router::{self, JOB};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};

fn vectors() -> Value {
    serde_json::from_str(include_str!("fixtures/router-flow-vectors.json")).unwrap()
}

fn word(value: &Value) -> String {
    value.as_str().unwrap_or_default().to_string()
}

const HELD_TWICE: &str = "a question is asked a second time about a message that is held";
const HELD_HERE: &str = "a held message belongs to the session it was made in";

/// The Host the janitors' book asks: its agents, and nothing else that a story needs.
struct Host {
    agents: BTreeMap<String, Agent>,
}

impl Host {
    /// An agent of a story joins the Host.
    fn join(&mut self, agent: &Value) {
        let made = Agent {
            agent_id: word(&agent["agent_id"]),
            session: word(&agent["session"]),
            name: word(&agent["persona"]),
            backend: word(&agent["backend"]),
            archived: agent["archived"] == true,
            ..Agent::default()
        };
        self.agents.insert(made.agent_id.clone(), made);
    }
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

/// What happened to one message: what the model was shown, what the Host was asked to do,
/// and what the sender is answered.
#[derive(Default)]
struct Seen {
    shown: Vec<Value>,
    given: Vec<Give>,
    said: Vec<Value>,
    told: Vec<Value>,
    made: Vec<Start>,
    stopped: Vec<String>,
    /// Whose last clip the Host was asked to play again.
    replayed: Vec<String>,
    answered: Option<Outcome>,
}

/// The made-up Host the stories happen on, as the Python Host's stories began.
struct Stage {
    vectors: Value,
    /// The agents of the Host: those it began with, and those a story made as it began.
    agents: Vec<Value>,
    host: Host,
    janitors: Janitors,
    book: Book,
    /// The agent that is open, by its id.
    focus: String,
    /// What each agent was told and said before the first story, by its session.
    before: BTreeMap<String, Vec<Said>>,
    /// The router's janitor: its id and its session.
    router: (String, String),
}

fn said_row(say: &Say) -> Value {
    json!({ "text": say.text, "voice_id": say.voice_id, "session": say.session })
}

/// How a message of a story came in, with what the Python Host keeps of that in its words.
fn came_of(message: &Value) -> Came {
    let id = word(&message["id"]);
    let kept = json!({
        "admission_id": format!("adm-{id}"), "admission_version": 1,
        "authenticated_at_admission": true, "cooperative_principal": "administrator",
        "principal_id": "", "origin": "user", "sender_agent_id": "", "channel": "http",
        "observed_at": message["now"], "client_admission_id": id,
        "trace_id": format!("trace-{id}"), "original_text": message["text"], "client": "",
    });
    Came {
        client_id: id,
        origin: "user".to_string(),
        sender_agent_id: String::new(),
        kept: python::compact_ascii(&kept),
    }
}

impl Stage {
    fn new() -> Self {
        let vectors = vectors();
        let mut host = Host {
            agents: BTreeMap::new(),
        };
        let agents = vectors["agents"].as_array().unwrap().clone();
        for agent in &agents {
            host.join(agent);
        }
        let own = &vectors["router"];
        let router = (word(&own["agent_id"]), word(&own["session"]));
        let janitor = Agent {
            agent_id: router.0.clone(),
            session: router.1.clone(),
            name: word(&own["name"]),
            backend: word(&own["backend"]),
            model: word(&own["model"]),
            effort: word(&own["effort"]),
            is_janitor: true,
            ..Agent::default()
        };
        host.agents.insert(router.0.clone(), janitor);
        let started = 1_800_000_000_000 - 5_000_000;
        let config = Config {
            template_id: JOB.to_string(),
            enabled: own["enabled"] == true,
            revision: 1,
            generation: 1,
            execution: own["execution"].as_object().unwrap().clone(),
            created_at: started,
            updated_at: started,
            ..Config::default()
        };
        let attachments = own["attachments"].as_array().unwrap().iter();
        let attachments = attachments.map(|one| Attachment {
            attachment_id: word(&one["attachment_id"]),
            trigger_id: word(&one["trigger_id"]),
            trigger_version: one["trigger_version"].as_i64().unwrap(),
            enabled: one["enabled"] == true,
            config: one["config"].as_object().unwrap().clone(),
        });
        let mut janitors = Janitors::default();
        janitors.install(JOB, &router.0, config, attachments.collect());
        let mut before: BTreeMap<String, Vec<Said>> = BTreeMap::new();
        for said in vectors["said_before"].as_array().unwrap() {
            before
                .entry(word(&said["session"]))
                .or_default()
                .push(Said {
                    role: word(&said["role"]),
                    text: word(&said["text"]),
                    trace_id: word(&said["trace_id"]),
                    source: "seed".to_string(),
                    created_at: said["at"].as_i64().unwrap(),
                });
        }
        Self {
            focus: word(&vectors["focus"]),
            vectors,
            agents,
            host,
            janitors,
            book: Book::default(),
            before,
            router,
        }
    }

    /// A story begins: the agents it makes as it begins are made.
    fn begin(&mut self, story: &Value) {
        let made = story["agents"].as_array().cloned().unwrap_or_default();
        for agent in made {
            self.host.join(&agent);
            self.agents.push(agent);
        }
    }

    /// The agents as the router sees them: each with what it was told and said before the
    /// first story and what the router gave it since; and the router's own janitor, which
    /// is looked at and not spoken to.
    fn members(&self) -> Vec<Member> {
        let agents = self.agents.iter();
        let mut all: Vec<Member> = agents
            .map(|agent| {
                let (id, session) = (word(&agent["agent_id"]), word(&agent["session"]));
                let mut said = self.before.get(&session).cloned().unwrap_or_default();
                said.extend(self.book.given_to(&id).iter().cloned());
                Member {
                    persona: word(&agent["persona"]),
                    voice_id: word(&agent["voice_id"]),
                    cwd: word(&agent["cwd"]),
                    backend: word(&agent["backend"]),
                    archived: agent["archived"] == true,
                    voice_target: true,
                    created_at: agent["created_at"].as_i64().unwrap(),
                    last_activity: agent["last_activity"].as_i64().unwrap(),
                    state: agent["state"].as_object().map(|state| State {
                        kind: word(&state["kind"]),
                        detail: word(&state["detail"]),
                        ts: state["ts"].as_i64().unwrap(),
                    }),
                    said,
                    agent_id: id,
                    session,
                }
            })
            .collect();
        all.push(Member {
            agent_id: self.router.0.clone(),
            session: self.router.1.clone(),
            persona: word(&self.vectors["router"]["name"]),
            backend: word(&self.vectors["router"]["backend"]),
            voice_target: false,
            ..Member::default()
        });
        all
    }

    /// Sets the router as a story sets it before a message: its options, and on or off.
    fn set_router(&mut self, wanted: &Value, now: i64) {
        if wanted.is_string() {
            return; // As it was.
        }
        let (id, session) = self.router.clone();
        let options = wanted.get("options").filter(|options| options.is_object());
        if let Some(options) = options {
            let revision = json!(self.janitors.configs[&id].revision);
            let mut asked = Asked::new();
            asked.insert("options".to_string(), options.clone());
            let set = self
                .janitors
                .configure(&session, &revision, &asked, &mut self.host, now);
            set.unwrap();
        }
        let on = wanted["enabled"] == true;
        let kept = &self.janitors.configs[&id];
        if kept.enabled != on {
            let revision = json!(kept.revision);
            let set = self
                .janitors
                .set_enabled(&session, &revision, on, &self.host, now);
            set.unwrap();
        }
    }

    /// What the agent of a story does with a message it is given.
    fn dispatched(&self, give: &Give, message: &Value) -> Done {
        if let Some(status) = message["dispatch_fails_with"].as_u64() {
            return Done::NotGiven {
                status: u16::try_from(status).unwrap(),
                error: format!("the agent answered {status}"),
            };
        }
        let agent = self.host.agent(&give.session).unwrap();
        Done::Given {
            session: agent.session,
            backend: agent.backend,
        }
    }

    /// Does what the router asks of the Host as the Python Host's stories did it: by
    /// writing it down.
    fn carry_out(&mut self, effects: &[Effect], message: &Value, seen: &mut Seen) -> Done {
        let mut done = Done::Fine;
        for effect in effects {
            match effect {
                Effect::Give(give) => {
                    seen.given.push(give.clone());
                    done = self.dispatched(give, message);
                }
                Effect::Say(say) => seen.said.push(said_row(say)),
                // Nothing was said aloud by anybody in these stories: there is nothing to
                // play again.
                Effect::Repeat { session, or } => {
                    seen.replayed.push(session.clone());
                    seen.said.extend(or.as_ref().map(said_row));
                }
                Effect::Focus { agent_id, session } => {
                    self.focus.clone_from(agent_id);
                    let told = json!({
                        "type": "agent-focus", "session": session, "agent_id": agent_id,
                    });
                    seen.told.push(told);
                }
                Effect::Stop { agent_id } => seen.stopped.push(agent_id.clone()),
                Effect::Start(start) => {
                    seen.made.push(start.clone());
                    let name = start.name.as_deref();
                    done = match (name, &start.again) {
                        (Some("Broken"), _) => Done::Broke("no tool to run it with".to_string()),
                        (Some("Refused"), _) => Done::Refused {
                            code: "session in use".to_string(),
                        },
                        (_, Some(again)) => Done::Started {
                            session: again.clone(),
                        },
                        (name, None) => Done::Started {
                            session: format!("new-{}", name.unwrap_or("agent").to_lowercase()),
                        },
                    };
                }
            }
        }
        done
    }

    /// The receipt of a message's run made another before the message is sent again, as the
    /// Python Host's story makes it (`"<drop>"`: the field is taken away).
    fn edit_receipt(&mut self, id: &str, edits: &Value) {
        let digest = Sha256::digest(format!("u-{id}").as_bytes());
        let digest: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
        let request = json!(format!("routing-{digest}"));
        let run_id = demand::request_run_id(JOB, &request).unwrap();
        let run = self.janitors.runs.get_mut(&run_id).unwrap();
        let receipt = run.demand_result.as_mut().unwrap().as_object_mut().unwrap();
        for (key, value) in edits.as_object().unwrap() {
            if value == "<drop>" {
                receipt.remove(key);
            } else {
                receipt.insert(key.clone(), value.clone());
            }
        }
    }

    /// One message's way through the router.
    fn send(&mut self, message: &Value) -> Seen {
        let now = message["now"].as_i64().unwrap();
        self.set_router(&message["janitor"], now);
        let id = word(&message["id"]);
        if message["receipt"].is_object() {
            self.edit_receipt(&id, &message["receipt"]);
        }
        let trace = message["trace"]
            .as_str()
            .map(str::to_owned)
            .unwrap_or_else(|| format!("trace-{id}"));
        let came = came_of(message);
        let only: Option<Vec<String>> = message["only"]
            .as_array()
            .map(|only| only.iter().map(word).collect());
        let members = self.members();
        let focus = self.focus.clone();
        let said = Message {
            text: message["text"].as_str().unwrap(),
            requested_session: message["session"].as_str().unwrap(),
            trace_id: &trace,
            hands_free: message["hands_free"] == true,
            fallback_request: message["fallback"] == true,
            only_sessions: only.as_deref(),
            came: Some(&came),
        };
        let decide = message["decide"].as_array().unwrap();
        let fails = message["fails"].as_array().unwrap();
        // What the model of the story answers to each asking: the last of its answers for
        // every asking after.
        let answer = |asking: usize| {
            let at = asking.min(decide.len() - 1);
            match &decide[at] {
                words if fails[at] == true => Err(word(words)),
                Value::String(text) => router::extract_json(text),
                answer => Ok(answer.as_object().unwrap().clone()),
            }
        };
        let shown = |routing: &Routing| {
            json!({
                "packet": routing.packet(), "provider": routing.settings.provider,
                "model": routing.settings.model, "effort": routing.settings.effort,
            })
        };
        macro_rules! around {
            () => {
                Around {
                    agents: &members,
                    focus: Some(focus.as_str()),
                    janitors: &mut self.janitors,
                    world: &self.host,
                    now_ms: now,
                }
            };
        }
        let mut seen = Seen::default();
        let begun = self.book.begin(&said, &mut around!());
        seen.answered = match begun {
            Begun::NotMine => None,
            Begun::Answered(outcome) => Some(outcome),
            Begun::Again(again) => {
                seen.given.push(again.give.clone());
                let done = self.dispatched(&again.give, message);
                Some(self.book.given_again(&again, done, &around!()))
            }
            Begun::Ask(mut routing) => {
                seen.shown.push(shown(&routing));
                if message["pause_at_model"] == true {
                    // Its owner pauses the router while the model is asked.
                    let revision = json!(self.janitors.configs[&self.router.0].revision);
                    let session = self.router.1.clone();
                    let paused = self
                        .janitors
                        .set_enabled(&session, &revision, false, &self.host, now);
                    paused.unwrap();
                }
                let mut next = self.book.answered(&mut routing, answer(0), &mut around!());
                if next == Next::AskAgain {
                    seen.shown.push(shown(&routing));
                    next = self.book.answered(&mut routing, answer(1), &mut around!());
                    assert_eq!(next, Next::Decided, "{id}: a model is asked twice at most");
                }
                match next {
                    Next::Answered(outcome) => Some(*outcome),
                    Next::AskAgain | Next::Decided => {
                        let plan = self.book.apply(&routing, &format!("held-{id}"), &around!());
                        let done = self.carry_out(&plan.effects.clone(), message, &mut seen);
                        // A quarter of a second each time the model is asked.
                        let latency = 250 * i64::try_from(seen.shown.len()).unwrap();
                        Some(
                            self.book
                                .finish(&routing, plan, done, latency, &mut around!()),
                        )
                    }
                }
            }
        };
        let told = seen
            .answered
            .as_ref()
            .and_then(|outcome| outcome.told.clone());
        seen.told.extend(told);
        let still = seen
            .answered
            .as_ref()
            .and_then(|outcome| outcome.say.as_ref());
        seen.said.extend(still.map(said_row));
        seen
    }

    /// The run of a message, as the Python Host's story reads it.
    fn run_of(&self, id: &str) -> Value {
        let digest = Sha256::digest(format!("u-{id}").as_bytes());
        let digest: String = digest.iter().map(|byte| format!("{byte:02x}")).collect();
        let request = json!(format!("routing-{digest}"));
        match self.janitors.request_run(JOB, &request).unwrap() {
            None => Value::Null,
            Some(run) => json!({
                "status": run["status"], "outcome": run["outcome"], "error": run["error"],
                "demand_result": run["demand_result"],
            }),
        }
    }

    /// The messages that are held and those that were given since, as the Python Host's
    /// story reads them: the oldest first.
    fn held(&self) -> Value {
        let mut held: Vec<_> = self.book.held.iter().collect();
        held.sort_by(|one, other| {
            let (one, other) = (
                (one.created_at, &one.pending_id),
                (other.created_at, &other.pending_id),
            );
            one.cmp(&other)
        });
        let rows = held.into_iter().map(|held| {
            json!({
                "pending_id": held.pending_id, "trace_id": held.trace_id,
                "utterance": held.utterance, "requested_session": held.requested_session,
                "candidate_session": held.candidate_session,
                "speak_as_session": held.speak_as_session, "reason": held.reason,
                "created_at": held.created_at, "expires_at": held.expires_at,
                "status": held.status,
                "prompt_admission_json": held.came.as_ref().map(|came| came.kept.clone()),
            })
        });
        rows.collect()
    }

    /// What the router gave agents under a trace, as the Python Host's story reads it.
    fn given_under(&self, trace: &str) -> Value {
        let mut rows = Vec::new();
        for agent in &self.agents {
            for said in self.book.given_to(agent["agent_id"].as_str().unwrap()) {
                if said.trace_id == trace {
                    rows.push(json!({
                        "session": agent["session"], "role": said.role, "text": said.text,
                        "trace_id": said.trace_id, "source": said.source,
                        "created_at": said.created_at,
                    }));
                }
            }
        }
        json!(rows)
    }

    /// What a start of an agent asks of the Host, in the Python Host's words: an agent
    /// that is started again by everything the Host knows of it, a new one by its name.
    fn made_row(&self, start: &Start) -> Value {
        match &start.again {
            Some(session) => {
                let agent = self
                    .agents
                    .iter()
                    .find(|agent| agent["session"] == session.as_str());
                let agent = agent.unwrap();
                json!({
                    "name": agent["persona"], "session": session, "replace_sid": session,
                    "cwd": agent["cwd"], "backend": agent["backend"], "synthesize_audio": true,
                })
            }
            None => match &start.name {
                Some(name) => json!({ "name": name, "synthesize_audio": true }),
                None => json!({ "synthesize_audio": true }),
            },
        }
    }
}

fn given_row(give: &Give) -> Value {
    let came = give.came.as_ref();
    let kept = came.map(|came| serde_json::from_str::<Value>(&came.kept).unwrap());
    json!({
        "text": give.text, "requested_session": give.requested_session,
        "trace_id": give.trace_id,
        "client_msg_id": came.map_or("", |came| came.client_id.as_str()),
        "origin": came.map_or("user", |came| came.origin.as_str()),
        "sender_agent_id": came.map_or("", |came| came.sender_agent_id.as_str()),
        "synthesize_audio": true, "unheard_audio_sessions": [],
        "forced_session": give.session, "routed_by_orchestrator": true,
        "prompt_admission": kept,
    })
}

/// What a message's sender is answered, in the Python Host's words. Whether its message is
/// given again when it is sent again is told by the receipt of its run, and taken as the
/// Python Host says it here.
fn answered_row(outcome: Option<&Outcome>, theirs: &Value) -> Value {
    let Some(outcome) = outcome else {
        return Value::Null;
    };
    json!({
        "handled": true, "ok": outcome.ok, "session": outcome.session,
        "dispatch": outcome.dispatch, "trace_id": outcome.trace_id, "action": outcome.action,
        "decision_id": outcome.decision_id, "decision": outcome.decision,
        "error": outcome.error, "status": outcome.status,
        "retry_dispatch": theirs["retry_dispatch"], "target": outcome.target,
    })
}

/// What the Python Host did with a message: an answer of the model that holds no JSON is told
/// in that Host's own sentence since its PR 449 (R90), as here, on every interpreter.
fn expected(message: &Value) -> Value {
    let want = message.clone();
    if message["id"] == "w-1" {
        for place in ["/answered/error", "/decisions/0/error"] {
            assert_eq!(want.pointer(place).unwrap(), clarp_kernel::router::NOT_JSON);
        }
    }
    want
}

/// Holds what happened to a message here against what happened to it there.
fn compare(stage: &Stage, seen: &Seen, want: &Value, what: &str) {
    let id = want["id"].as_str().unwrap();
    let trace = want["trace"]
        .as_str()
        .map(str::to_owned)
        .unwrap_or_else(|| format!("trace-{id}"));
    let ours = |part: &str| match part {
        "shown" => json!(seen.shown),
        "given" => seen.given.iter().map(given_row).collect(),
        "said" => json!(seen.said),
        "told" => json!(seen.told),
        "made" => seen
            .made
            .iter()
            .map(|start| stage.made_row(start))
            .collect(),
        "stopped" => json!(seen.stopped),
        "replayed" => json!(seen.replayed),
        "answered" => answered_row(seen.answered.as_ref(), &want["answered"]),
        "decisions" => {
            let kept = stage.book.decisions.iter();
            let own = kept.filter(|kept| kept.trace_id == trace);
            own.map(clarp_kernel::router::flow::Kept::row).collect()
        }
        "held" => stage.held(),
        "run" => stage.run_of(id),
        "focus" => json!(stage.focus),
        "routing_messages" => stage.given_under(&trace),
        other => panic!("no part {other}"),
    };
    let parts = [
        "shown",
        "given",
        "said",
        "told",
        "made",
        "stopped",
        "replayed",
        "answered",
        "decisions",
        "held",
        "run",
        "focus",
        "routing_messages",
    ];
    for part in parts {
        let ours = ours(part);
        assert_eq!(ours, want[part], "{what}: {part}");
        // In the same order too: a model reads a packet as text, an app a row. (What a
        // run came to is kept by the janitors' book, in an order of its own.)
        if part != "run" {
            assert_eq!(
                ours.to_string(),
                want[part].to_string(),
                "{what}: {part}, as text"
            );
        }
    }
}

#[test]
fn a_messages_way_is_the_python_hosts_message_by_message() {
    let mut stage = Stage::new();
    let stories = stage.vectors["stories"].as_array().unwrap().clone();
    let (mut messages, mut asked, mut given, mut said) = (0, 0, 0, 0);
    for story in &stories {
        stage.begin(story);
        for message in story["messages"].as_array().unwrap() {
            let what = format!("{}: {} ({})", story["name"], message["what"], message["id"]);
            let seen = stage.send(message);
            compare(&stage, &seen, &expected(message), &what);
            messages += 1;
            asked += seen.shown.len();
            given += seen.given.len();
            said += seen.said.len();
        }
    }
    // Every message of the stories, and what the stories are there to show: a model asked
    // once and twice, messages given, and words aloud.
    assert_eq!((messages, asked, given, said), (144, 189, 14, 60));
}

/// Plays the stories up to the one named, and answers its messages by their ids.
fn up_to(stage: &mut Stage, name: &str) -> BTreeMap<String, Value> {
    let stories = stage.vectors["stories"].as_array().unwrap().clone();
    for story in &stories {
        stage.begin(story);
        if story["name"] == name {
            let messages = story["messages"].as_array().unwrap().iter();
            return messages
                .map(|one| (word(&one["id"]), one.clone()))
                .collect();
        }
        for message in story["messages"].as_array().unwrap() {
            stage.send(message);
        }
    }
    panic!("no story {name}");
}

/// The row of a held message, as the stage keeps it now.
fn held_row(stage: &Stage, id: &str) -> Value {
    let held = stage.held();
    let row = held
        .as_array()
        .unwrap()
        .iter()
        .find(|row| row["pending_id"] == id);
    row.cloned().unwrap_or(Value::Null)
}

#[test]
fn a_held_message_keeps_its_own_words_when_its_user_is_asked_a_second_time() {
    let mut stage = Stage::new();
    let messages = up_to(&mut stage, HELD_TWICE);
    stage.send(&messages["r-1"]);
    let first = held_row(&stage, "held-r-1");
    // Asked again of the same held message: its words, id, trace, time and way in stay;
    // whom it may be for, why it is asked and until when are the second question's.
    stage.send(&messages["r-2"]);
    let again = held_row(&stage, "held-r-1");
    for same in [
        "utterance",
        "trace_id",
        "created_at",
        "prompt_admission_json",
    ] {
        assert_eq!(again[same], first[same], "{same}");
    }
    assert_eq!(again["utterance"], "send the report");
    assert_eq!(again["candidate_session"], "");
    assert_eq!(again["reason"], "unclear answer");
    assert_ne!(again["expires_at"], first["expires_at"]);
    // The answer: the held message is given, in its own words and by its own way in.
    let seen = stage.send(&messages["r-3"]);
    let gave = given_row(&seen.given[0]);
    assert_eq!(gave["text"], "send the report");
    assert_eq!(gave["forced_session"], "zoe");
    assert_eq!(gave["client_msg_id"], "r-1");
    assert_eq!(held_row(&stage, "held-r-1")["status"], "sent");
}

#[test]
fn a_held_message_belongs_to_the_session_it_was_made_in() {
    let mut stage = Stage::new();
    let messages = up_to(&mut stage, HELD_HERE);
    let gone = "That message is no longer waiting. Please say it again.";
    let pending = |seen: &Seen| seen.shown[0]["packet"]["pending"].clone();
    stage.send(&messages["h-1"]);
    let first = held_row(&stage, "held-h-1");
    assert_eq!(first["requested_session"], "anna");
    // Not shown, claimed or renewed while speaking to another agent.
    let seen = stage.send(&messages["h-2"]);
    assert_eq!(pending(&seen), json!([]));
    assert!(seen.given.is_empty());
    assert_eq!(
        seen.said,
        [json!({ "text": gone, "voice_id": "voice-mike", "session": "mike" })]
    );
    assert_eq!(held_row(&stage, "held-h-1"), first);
    stage.send(&messages["h-3"]);
    assert_eq!(held_row(&stage, "held-h-1"), first);
    let other = held_row(&stage, "held-h-3");
    assert_eq!(other["utterance"], "what?");
    assert_eq!(other["requested_session"], "mike");
    // In its own session: shown without the other's, renewed, and given.
    let seen = stage.send(&messages["h-4"]);
    let shown = pending(&seen);
    let shown: Vec<&Value> = shown
        .as_array()
        .unwrap()
        .iter()
        .map(|held| &held["pending_id"])
        .collect();
    assert_eq!(shown, [&json!("held-h-1")]);
    let renewed = held_row(&stage, "held-h-1");
    assert_eq!(renewed["utterance"], "send the deck");
    assert_eq!(renewed["candidate_session"], "mike");
    assert_eq!(renewed["created_at"], first["created_at"]);
    let seen = stage.send(&messages["h-5"]);
    assert_eq!(given_row(&seen.given[0])["text"], "send the deck");
    assert_eq!(given_row(&seen.given[0])["client_msg_id"], "h-1");
    // Given, it is no longer renewed: the question holds the new message.
    stage.send(&messages["h-6"]);
    assert_eq!(held_row(&stage, "held-h-6")["utterance"], "and again?");
    // Where an app asked, nothing is said, as of any sentence; the other's hold is not released
    // from here.
    let seen = stage.send(&messages["h-7"]);
    assert!(seen.said.is_empty(), "{:?}", seen.said);
    assert_eq!(held_row(&stage, "held-h-3")["status"], "pending");
}

#[test]
fn words_are_held_against_names_as_the_python_host_holds_them() {
    let vectors = vectors();
    let cases = vectors["sound_alike"].as_array().unwrap();
    for case in cases {
        let said = case["utterance"].as_str().unwrap();
        let found = packet::sound_alike(said, case["agents"].as_array().unwrap());
        assert_eq!(json!(found), case["found"], "{said}");
        assert_eq!(
            json!(found).to_string(),
            case["found"].to_string(),
            "{said}, as text"
        );
    }
    assert_eq!(cases.len(), 9);
}

#[test]
fn a_text_is_shown_on_one_line_and_cut_where_it_is_long() {
    assert_eq!(
        packet::compact("  Draft the notes   for\n2.4. ", 240),
        "Draft the notes for 2.4."
    );
    assert_eq!(
        packet::compact(&"x".repeat(300), 240),
        format!("{}...", "x".repeat(237))
    );
    assert_eq!(packet::compact("abc def ghi", 9), "abc de...");
    assert_eq!(packet::compact("abc    def", 7), "abc def");
    assert_eq!(packet::compact("", 5), "");
}

#[test]
fn the_decisions_are_shown_the_newest_first_and_no_more_than_are_asked_for() {
    let mut stage = Stage::new();
    let stories = stage.vectors["stories"].as_array().unwrap().clone();
    for story in &stories {
        stage.begin(story);
        for message in story["messages"].as_array().unwrap() {
            stage.send(message);
        }
    }
    let all = stage.book.recent(200, None);
    let numbers: Vec<i64> = all
        .iter()
        .map(|kept| kept["decision_id"].as_i64().unwrap())
        .collect();
    let mut newest_first = numbers.clone();
    newest_first.sort_unstable_by(|one, other| other.cmp(one));
    assert_eq!(numbers, newest_first);
    assert_eq!(numbers.len(), stage.book.decisions.len());
    assert_eq!(stage.book.recent(3, None).len(), 3);
    assert_eq!(stage.book.recent(0, None).len(), 1);
    let ignored = stage.book.recent(200, Some("ignored"));
    assert!(!ignored.is_empty() && ignored.iter().all(|kept| kept["final_action"] == "ignored"));
    // What a row keeps as the text of its JSON is also there as what it says.
    let one = &all[0];
    let read: Value = serde_json::from_str(one["raw_response_json"].as_str().unwrap()).unwrap();
    assert_eq!(one["raw_response"], read);
    assert!(one["mentioned_sessions"].is_array());
}
