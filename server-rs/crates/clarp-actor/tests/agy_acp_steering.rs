//! A message the user sends to a busy agy-acp agent reaches its running turn, as one sent to
//! a busy Claude agent does: the actor against a world that plays the shim and the agy-acp
//! driver, which prints what Antigravity said and the steering evidence
//! (`clarp_kernel::steering`): an echo when it took a message, a result naming what each
//! answer covers. The world plays an OpenCode lane's turn too, which says on its spool which
//! steered words it read (`steer.read`).
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use clarp_actor::conversation::Versioned;
use clarp_actor::{Action, Actor, JoinedRead, Outputs, Profile, ShimOrder, Steering};
use clarp_backend::Backend;
use clarp_backend::agy_acp::AgyAcp;
use clarp_codec::standard_set;
use clarp_kernel::control::Control;
use clarp_kernel::conversation::{Entry, Message};
use clarp_kernel::takeover;
use clarp_kernel::turn::{Admission, Origin, TurnId};
use clarp_ports::log::LogEngine;
use clarp_spool::{Entry as Line, Record, ShimEvent, SpoolWriter};
use clarp_store_mem::MemEngine;
use serde_json::{Value, json};

static WORLDS: AtomicU64 = AtomicU64::new(0);

/// One agy-acp agent's disk, its shims and its clock.
struct World {
    root: PathBuf,
    engine: Box<dyn LogEngine>,
    profile: Profile,
    backend: Arc<dyn Backend>,
    now_ms: i64,
    shims: BTreeMap<u64, SpoolWriter>,
    /// Every turn a shim took, in order.
    took: Vec<u64>,
    /// The orders the shims were given, by turn.
    orders: BTreeMap<u64, ShimOrder>,
    /// What the Host wrote to a running turn's input.
    steered: Vec<Value>,
    stopped: Vec<u64>,
    /// The messages the actor said a running turn read, as the Host announces them.
    joined: Vec<JoinedRead>,
}

impl World {
    fn new(name: &str) -> Self {
        let n = WORLDS.fetch_add(1, Ordering::SeqCst);
        let root = std::env::temp_dir().join(format!(
            "clarp-agy-acp-steer-{name}-{}-{n}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&root);
        let profile = Profile {
            agent_id: "a1".to_string(),
            session: "nova".to_string(),
            conversation_id: "conv-1".to_string(),
            dir: root.join("agents/a1"),
            run_dir: root.join("run"),
            cwd: root.join("work"),
            model: None,
            effort: None,
            told: None,
            tool_env: Vec::new(),
            stop_grace_ms: 3_000,
            keep_spools: false,
            accounts: clarp_kernel::recovery::Accounts::None,
            epoch: "epoch-test".to_string(),
            hides_coordination: true,
        };
        Self {
            root,
            engine: Box::new(MemEngine::new()),
            profile,
            backend: Arc::new(AgyAcp::with_program("agy")),
            now_ms: 1_000,
            shims: BTreeMap::new(),
            took: Vec::new(),
            orders: BTreeMap::new(),
            steered: Vec::new(),
            stopped: Vec::new(),
            joined: Vec::new(),
        }
    }

    fn tick(&mut self) -> i64 {
        self.now_ms += 10;
        self.now_ms
    }

    fn open(&mut self) -> Actor {
        let log = self
            .engine
            .open(&self.profile.dir, standard_set("zstd").unwrap())
            .unwrap();
        let now = self.tick();
        let (mut actor, out) =
            Actor::open(self.profile.clone(), self.backend.clone(), log, now).unwrap();
        self.carry_out(&mut actor, out);
        actor
    }

    fn carry_out(&mut self, actor: &mut Actor, first: Outputs) {
        let mut asked = vec![first];
        while let Some(out) = asked.pop() {
            self.joined.extend(out.joined_reads);
            for action in out.actions {
                match action {
                    Action::Launch(order) => {
                        self.launch(&order);
                        let now = self.tick();
                        asked.push(actor.spool_changed(now).unwrap());
                    }
                    Action::Steer { input, .. } => match input {
                        Steering::Stdin(line) => {
                            assert!(line.ends_with('\n'), "one line");
                            self.steered.push(serde_json::from_str(&line).unwrap());
                        }
                        Steering::Words { id, text } => {
                            self.steered.push(json!({"words": id, "text": text}));
                        }
                    },
                    Action::Stop { turn, .. } => self.stopped.push(turn.0),
                    Action::Follow { .. } | Action::FollowExit { .. } => {}
                    Action::Fork(_) | Action::ForkFollow { .. } => panic!("agy-acp has no fork"),
                    Action::CheckNetwork { .. } => panic!("no connection fails here"),
                }
            }
        }
    }

    /// A shim takes the turn and starts the driver; a turn whose input stays open says who
    /// owns it, as the shim does.
    fn launch(&mut self, order: &ShimOrder) {
        let mut writer = SpoolWriter::create(&order.spool).unwrap();
        let t = self.tick();
        writer
            .append(&Record {
                t,
                entry: Line::Shim(ShimEvent::Spawned { pid: 4_242 }),
            })
            .unwrap();
        if order.streams_input {
            writer
                .append(&Record {
                    t,
                    entry: Line::Shim(ShimEvent::InputOpen {
                        shim_pid: 4_241,
                        shim_start: "boot:1".into(),
                        tool_pid: 4_242,
                        tool_start: "boot:2".into(),
                        group: 4_242,
                        projects: self.root.join("projects"),
                    }),
                })
                .unwrap();
        }
        self.shims.insert(order.turn.0, writer);
        self.took.push(order.turn.0);
        self.orders.insert(order.turn.0, order.clone());
    }

    /// The name the turn's first message has in its order: what its answer's result names.
    fn primary(&self, turn: u64) -> String {
        let order: Value =
            serde_json::from_slice(&std::fs::read(&self.orders[&turn].stdin_file).unwrap())
                .unwrap();
        order["uuid"].as_str().unwrap().to_string()
    }

    fn says(&mut self, turn: u64, line: &Value) {
        let t = self.tick();
        let shim = self.shims.get_mut(&turn).expect("the turn has a shim");
        shim.append(&Record {
            t,
            entry: Line::Out(line.to_string()),
        })
        .unwrap();
    }

    fn exits(&mut self, turn: u64, code: Option<i32>, signal: Option<i32>) {
        let t = self.tick();
        let mut shim = self.shims.remove(&turn).expect("the turn has a shim");
        shim.append(&Record {
            t,
            entry: Line::Shim(ShimEvent::Exited { code, signal }),
        })
        .unwrap();
    }

    fn hint(&mut self, actor: &mut Actor) {
        let now = self.tick();
        let out = actor.spool_changed(now).unwrap();
        self.carry_out(actor, out);
    }

    /// The user sends a message while a turn runs, and the Host offers it to that turn as it
    /// does for a turn of a tool that steers.
    fn send_while_running(&mut self, actor: &mut Actor, id: &str, text: &str) -> TurnId {
        let now = self.tick();
        let trace = format!("trace-{id}");
        let message = Message {
            id: format!("u-{id}"),
            text: text.to_string(),
            origin: Origin::User,
            trace_id: Some(trace.clone()),
            ..Message::default()
        };
        let (sent, out) = actor.send(&message, Admission::Queue, now).unwrap();
        self.carry_out(actor, out);
        assert!(sent.queued, "it waits behind the running turn");
        let turn = sent.turn.unwrap();
        self.hint(actor);
        let uuid = takeover::prompt_uuid(&trace).unwrap();
        let now = self.tick();
        let steer = actor.steer_active(turn, uuid, text, now).unwrap();
        let out = Outputs {
            actions: steer.into_iter().collect(),
            ..Outputs::default()
        };
        self.carry_out(actor, out);
        turn
    }
}

impl Drop for World {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

fn update(value: Value) -> Value {
    json!({"jsonrpc": "2.0", "method": "session/update",
           "params": {"sessionId": "conv-a", "update": value}})
}

fn chunk(text: &str) -> Value {
    update(json!({"sessionUpdate": "agent_message_chunk",
                  "content": {"type": "text", "text": text}}))
}

fn bound() -> Value {
    json!({"clarp": "bound", "session": "conv-a", "resumed": false})
}

fn prompted() -> Value {
    json!({"clarp": "prompted", "result": {"stopReason": "end_turn"}})
}

fn echo(uuid: &str) -> Value {
    json!({"type": "user", "isReplay": true, "uuid": uuid})
}

fn result(uuids: &[&str]) -> Value {
    json!({"type": "result", "subtype": "success", "is_error": false, "result": "",
           "user_message_uuids": uuids})
}

fn rows(actor: &Actor) -> Vec<Versioned> {
    actor.tail(1_000).unwrap().entries
}

/// The user's messages in the conversation, in order.
fn messages(actor: &Actor) -> Vec<String> {
    rows(actor)
        .iter()
        .filter_map(|row| match &row.entry {
            Entry::Message(message) => Some(message.id.clone()),
            _ => None,
        })
        .collect()
}

/// What the agent said, stored or being written, with the turn it is of.
fn replies(actor: &Actor) -> Vec<(String, u64, bool)> {
    rows(actor)
        .iter()
        .filter_map(|row| match &row.entry {
            Entry::Reply(reply) => Some((reply.text.clone(), reply.turn, row.live)),
            _ => None,
        })
        .collect()
}

/// How each turn ended, by its end's id: an end is told on the row that closed its turn.
fn ends(actor: &Actor) -> Vec<(String, String)> {
    rows(actor)
        .iter()
        .filter_map(|row| row.turn.as_ref())
        .map(|end| (end.id.clone(), end.status.clone()))
        .collect()
}

/// Starts turn 1 and lets its driver open the conversation and begin to answer.
fn running(world: &mut World) -> Actor {
    let mut actor = world.open();
    let now = world.tick();
    let first = Message {
        id: "u-1".into(),
        text: "go".into(),
        origin: Origin::User,
        trace_id: Some("trace-1".into()),
        ..Message::default()
    };
    let (_, out) = actor.send(&first, Admission::Queue, now).unwrap();
    world.carry_out(&mut actor, out);
    assert_eq!(world.took, [1]);
    let order = &world.orders[&1];
    assert!(order.streams_input, "an agy-acp turn keeps its input open");
    assert!(
        !order.args.iter().any(|arg| arg.starts_with("--replay")),
        "Claude's flag is Claude's"
    );
    assert_eq!(
        world.primary(1),
        takeover::attempt_prompt_uuid("a1", 1, Some("trace-1"), 1)
    );
    world.says(1, &bound());
    world.hint(&mut actor);
    // Someone watches the turn: what it shows is kept from here on.
    let now = world.tick();
    actor.shown_whole(now);
    actor
}

/// The replies that work followed: said along the way, which the conversation does not show.
fn commentary(actor: &Actor) -> Vec<String> {
    rows(actor)
        .iter()
        .filter(|row| row.commentary)
        .map(|row| row.entry.id().to_string())
        .collect()
}

/// The id of the stored reply with this text.
fn reply_id(actor: &Actor, text: &str) -> String {
    rows(actor)
        .iter()
        .find_map(|row| match &row.entry {
            Entry::Reply(reply) if reply.text == text => Some(reply.id.clone()),
            _ => None,
        })
        .unwrap()
}

/// How those who watch the running turn are shown the message with this text.
fn live_phase(world: &mut World, actor: &mut Actor, text: &str) -> Option<String> {
    let now = world.tick();
    actor.shown_whole(now)["items"]
        .as_array()?
        .iter()
        .find(|item| item["kind"] == "message" && item["text"] == text)
        .and_then(|item| item["phase"].as_str().map(str::to_string))
}

/// What was said before a message was read stays an answer, live and stored, while a tool
/// runs for the message.
fn still_an_answer(world: &mut World, actor: &mut Actor, text: &str) {
    let id = reply_id(actor, text);
    assert!(
        !commentary(actor).contains(&id),
        "{text}: kept as an answer"
    );
    assert_eq!(
        live_phase(world, actor, text).as_deref(),
        Some("final"),
        "{text}: shown as an answer"
    );
}

#[test]
fn a_message_read_before_the_first_answer_joins_the_running_turn() {
    let mut world = World::new("joined");
    let mut actor = running(&mut world);
    let primary = world.primary(1);
    world.says(1, &chunk("Working on it."));
    world.hint(&mut actor);

    let turn = world.send_while_running(&mut actor, "2", "and check the tests");
    let uuid = takeover::prompt_uuid("trace-2").unwrap();
    assert_eq!(
        world.steered,
        [json!({"type": "user", "uuid": uuid,
                "message": {"role": "user", "content": [{"type": "text", "text": "and check the tests"}]}})]
    );
    assert_eq!(messages(&actor), ["u-1"], "a write to the pipe is no read");
    assert!(world.joined.is_empty());

    world.says(1, &echo(&uuid));
    world.hint(&mut actor);
    assert_eq!(messages(&actor), ["u-1", "u-2"], "it enters at its echo");
    assert_eq!(world.joined.len(), 1, "the queue is told it started");
    assert_eq!(world.joined[0].turn, turn);
    assert_eq!(
        world.joined[0].client_id, "2",
        "the id the client sent it with"
    );
    assert!(actor.status().waiting.is_empty());

    // The work for the new message makes nothing said before it narration.
    world.says(1, &tool("pending"));
    world.hint(&mut actor);
    still_an_answer(&mut world, &mut actor, "Working on it.");
    world.says(1, &tool("completed"));
    world.says(1, &chunk("Tests pass too."));
    world.says(1, &prompted());
    world.says(1, &result(&[&primary, &uuid]));
    world.exits(1, Some(0), None);
    world.hint(&mut actor);

    assert_eq!(
        world.took,
        [1],
        "it ran in the turn it joined, and not again"
    );
    assert_eq!(
        replies(&actor),
        [
            ("Working on it.".to_string(), 1, false),
            ("Tests pass too.".to_string(), 1, false)
        ],
        "what was said before the message was read is whole at its echo"
    );
    let id = reply_id(&actor, "Working on it.");
    assert!(
        !commentary(&actor).contains(&id),
        "kept as an answer after the turn"
    );
    assert_eq!(ends(&actor), [("t-1".to_string(), "completed".to_string())]);
    let status = actor.status();
    assert!(status.live.is_none() && status.waiting.is_empty());
}

/// Issue 839: the user's messages that waited while nothing offered them to the running turn
/// go into it before a newer one that is offered, in the order they were said; the newer one
/// alone is refused while they wait.
#[test]
fn messages_that_waited_go_in_before_a_newer_one_in_the_order_they_were_said() {
    let mut world = World::new("order");
    let mut actor = running(&mut world);
    world.says(1, &chunk("Working on it."));
    world.hint(&mut actor);
    let said = |id: u64| Message {
        id: format!("u-{id}"),
        text: format!("message {id}"),
        origin: Origin::User,
        trace_id: Some(format!("trace-{id}")),
        ..Message::default()
    };
    let uuid = |id: u64| takeover::prompt_uuid(&format!("trace-{id}")).unwrap();
    let mut newest = TurnId(0);
    for id in 2..=6 {
        let now = world.tick();
        let (sent, out) = actor.send(&said(id), Admission::Queue, now).unwrap();
        world.carry_out(&mut actor, out);
        assert!(sent.queued, "it waits behind the running turn");
        newest = sent.turn.unwrap();
    }
    let now = world.tick();
    assert!(
        actor
            .steer_active(newest, uuid(6), "message 6", now)
            .unwrap()
            .is_none(),
        "the newest may not go in before the older ones"
    );
    let now = world.tick();
    let mut actions = actor.steer_older(newest, now).unwrap();
    actions.extend(
        actor
            .steer_active(newest, uuid(6), "message 6", now)
            .unwrap(),
    );
    world.carry_out(
        &mut actor,
        Outputs {
            actions,
            ..Outputs::default()
        },
    );
    let offered: Vec<Value> = world
        .steered
        .iter()
        .map(|line| line["uuid"].clone())
        .collect();
    assert_eq!(
        offered,
        (2..=6).map(|id| json!(uuid(id))).collect::<Vec<_>>()
    );
    for id in 2..=6 {
        world.says(1, &echo(&uuid(id)));
    }
    world.hint(&mut actor);
    assert_eq!(
        messages(&actor),
        ["u-1", "u-2", "u-3", "u-4", "u-5", "u-6"],
        "stored in the order they were said"
    );
    assert!(actor.status().waiting.is_empty());
}

#[test]
fn a_message_read_after_the_first_answer_is_a_turn_of_its_own_in_the_same_process() {
    let mut world = World::new("separate");
    let mut actor = running(&mut world);
    let primary = world.primary(1);
    world.says(1, &chunk("Working on it."));
    world.hint(&mut actor);
    let turn = world.send_while_running(&mut actor, "2", "then the docs");
    let uuid = takeover::prompt_uuid("trace-2").unwrap();

    // The first prompt is answered before the driver takes the message.
    world.says(1, &chunk(" Done."));
    world.says(1, &prompted());
    world.says(1, &result(&[&primary]));
    world.hint(&mut actor);
    assert_eq!(
        replies(&actor),
        [("Working on it. Done.".to_string(), 1, false)],
        "an answer given before the message was read is the first turn's, stored"
    );
    assert!(actor.status().live.is_some(), "the process still runs");

    world.says(1, &echo(&uuid));
    world.says(1, &tool("pending"));
    world.hint(&mut actor);
    assert_eq!(messages(&actor), ["u-1", "u-2"]);
    assert_eq!(world.joined.len(), 1);
    assert_eq!(world.joined[0].turn, turn);
    // The work for the new message makes nothing of the first turn's answer narration.
    still_an_answer(&mut world, &mut actor, "Working on it. Done.");
    world.says(1, &tool("completed"));
    world.says(1, &chunk("Docs"));
    world.hint(&mut actor);

    // A Host that restarts here reads the turn's spool again and goes on where it was.
    drop(actor);
    let mut actor = world.open();
    world.says(1, &chunk(" updated."));
    world.says(1, &prompted());
    world.says(1, &result(&[&uuid]));
    world.exits(1, Some(0), None);
    world.hint(&mut actor);

    assert_eq!(world.took, [1]);
    assert_eq!(
        replies(&actor),
        [
            ("Working on it. Done.".to_string(), 1, false),
            ("Docs updated.".to_string(), turn.0, false),
        ],
        "the second answer is the second turn's"
    );
    let ended = ends(&actor);
    assert!(ended.contains(&(format!("t-{}", turn.0), "completed".to_string())));
    assert!(ended.contains(&("t-1".to_string(), "completed".to_string())));
    let status = actor.status();
    assert!(status.live.is_none() && status.waiting.is_empty());
}

#[test]
fn a_message_the_driver_does_not_take_runs_as_its_own_turn_afterwards() {
    let mut world = World::new("refused");
    let mut actor = running(&mut world);
    let primary = world.primary(1);
    let turn = world.send_while_running(&mut actor, "2", "one more thing");
    assert_eq!(world.steered.len(), 1);

    world.says(1, &chunk("All done."));
    world.says(1, &prompted());
    world.says(1, &result(&[&primary]));
    world.exits(1, Some(0), None);
    world.hint(&mut actor);

    assert_eq!(
        world.took,
        [1, turn.0],
        "the unread message got its own turn"
    );
    assert!(world.joined.is_empty(), "it was never read into the first");
    assert_eq!(
        world.primary(turn.0),
        takeover::attempt_prompt_uuid("a1", turn.0, Some("trace-2"), 1),
        "it runs under its own name"
    );
    assert_eq!(ends(&actor), [("t-1".to_string(), "completed".to_string())]);
}

#[test]
fn a_message_with_a_file_waits_for_its_own_turn() {
    let mut world = World::new("file");
    let mut actor = running(&mut world);
    world.send_while_running(&mut actor, "2", "see /data/uploads/s/shot.png");
    assert!(
        world.steered.is_empty(),
        "a picture goes to agy only with a prompt"
    );
}

#[test]
fn a_stop_ends_a_read_message_with_the_turn_and_runs_an_unread_one_after_it() {
    let mut world = World::new("stop");
    let mut actor = running(&mut world);
    let primary = world.primary(1);
    let read = world.send_while_running(&mut actor, "2", "read this");
    let unread = world.send_while_running(&mut actor, "3", "and this");
    assert_eq!(world.steered.len(), 2);
    let read_uuid = takeover::prompt_uuid("trace-2").unwrap();

    world.says(1, &chunk("First."));
    world.says(1, &prompted());
    world.says(1, &result(&[&primary]));
    world.says(1, &echo(&read_uuid));
    world.says(1, &chunk("Half"));
    world.hint(&mut actor);
    assert_eq!(messages(&actor), ["u-1", "u-2"]);

    let now = world.tick();
    let (told, out) = actor
        .stop_and_start_waiting(Control::by(clarp_kernel::control::USER), now)
        .unwrap();
    assert_eq!(told, 1);
    world.carry_out(&mut actor, out);
    assert_eq!(world.stopped, [1]);
    world.says(1, &json!({"clarp": "cancel", "session": "conv-a"}));
    world.says(
        1,
        &json!({"clarp": "prompted", "result": {"stopReason": "cancelled"}}),
    );
    world.exits(1, None, Some(15));
    world.hint(&mut actor);

    let ended = ends(&actor);
    assert!(
        ended.contains(&(format!("t-{}", read.0), "interrupted".to_string())),
        "{ended:?}"
    );
    assert!(ended.iter().any(|(id, _)| id == "t-1"), "{ended:?}");
    assert_eq!(
        replies(&actor),
        [
            ("First.".to_string(), 1, false),
            ("Half".to_string(), read.0, false)
        ]
    );
    assert_eq!(
        world.took,
        [1, unread.0],
        "the unread message runs after the Stop"
    );
    assert_eq!(world.joined.len(), 1, "only the read message was read");
}

/// An answer of the driver's, naming the prompt it answers.
fn answer(uuid: &str) -> Value {
    json!({"clarp": "prompted", "uuid": uuid, "result": {"stopReason": "end_turn"}})
}

fn tool(status: &str) -> Value {
    let kind = if status == "pending" {
        "tool_call"
    } else {
        "tool_call_update"
    };
    update(
        json!({"sessionUpdate": kind, "toolCallId": "t-1", "title": "run_command",
                  "kind": "execute", "status": status, "rawInput": {"CommandLine": "make"}}),
    )
}

#[test]
fn a_joined_message_is_settled_by_its_own_result_when_each_answer_has_one() {
    // What the driver prints: one result per answer, the turn's own first.
    let mut world = World::new("joined-two-results");
    let mut actor = running(&mut world);
    let primary = world.primary(1);
    world.says(1, &chunk("Building."));
    world.says(1, &tool("pending"));
    world.hint(&mut actor);
    let turn = world.send_while_running(&mut actor, "2", "and run the tests");
    let uuid = takeover::prompt_uuid("trace-2").unwrap();

    world.says(1, &echo(&uuid));
    world.hint(&mut actor);
    assert_eq!(messages(&actor), ["u-1", "u-2"]);
    assert_eq!(world.joined.len(), 1);
    assert_eq!(world.joined[0].turn, turn);

    world.says(1, &tool("completed"));
    world.says(1, &answer(&primary));
    world.says(1, &result(&[&primary]));
    world.hint(&mut actor);
    assert!(
        actor.status().live.is_some(),
        "the turn runs until its process ends"
    );
    world.says(1, &chunk("Tests pass."));
    world.says(1, &answer(&uuid));
    world.says(1, &result(&[&uuid]));
    world.exits(1, Some(0), None);
    world.hint(&mut actor);

    assert_eq!(
        world.took,
        [1],
        "the joined message ran in the turn, and not again"
    );
    let said: Vec<_> = replies(&actor)
        .into_iter()
        .map(|(text, turn, _)| (text, turn))
        .collect();
    assert_eq!(
        said,
        [("Building.".to_string(), 1), ("Tests pass.".to_string(), 1)],
        "a joined message is answered in the turn it joined"
    );
    assert_eq!(ends(&actor), [("t-1".to_string(), "completed".to_string())]);
    let status = actor.status();
    assert!(status.live.is_none() && status.waiting.is_empty());
}

#[test]
fn a_message_whose_driver_died_before_it_was_taken_runs_as_its_own_turn_after_a_restart() {
    let mut world = World::new("dead-driver");
    let mut actor = running(&mut world);
    world.says(1, &chunk("Half an ans"));
    world.hint(&mut actor);
    let turn = world.send_while_running(&mut actor, "2", "and this");
    assert_eq!(world.steered.len(), 1);

    // The driver is killed before agy took the message, while the Host is down.
    drop(actor);
    world.exits(1, None, Some(9));
    let mut actor = world.open();
    world.hint(&mut actor);

    assert!(world.joined.is_empty(), "it was never read");
    assert_eq!(world.took, [1, turn.0], "it runs as a turn of its own");
    assert!(
        ends(&actor)
            .iter()
            .any(|(id, status)| id == "t-1" && status == "interrupted"),
        "{:?}",
        ends(&actor)
    );
    assert!(
        replies(&actor)
            .iter()
            .any(|(text, turn, _)| text == "Half an ans" && *turn == 1),
        "what was said before is kept"
    );
}

/// Issue 784. A lane may say it read words under an id the Host never gave the turn (OpenCode
/// stored words whose post timed out). The actor reads nothing into the turn for it: the message
/// the Host did give it stays unread, and runs as a turn of its own after.
#[test]
fn a_lanes_reading_of_an_id_the_host_never_gave_is_ignored() {
    let mut world = World::new("unknown-read");
    world.backend = clarp_backend::by_id("opencode").unwrap();
    let mut actor = world.open();
    let now = world.tick();
    let first = Message {
        id: "u-1".into(),
        text: "go".into(),
        origin: Origin::User,
        trace_id: Some("trace-1".into()),
        ..Message::default()
    };
    let (_, out) = actor.send(&first, Admission::Queue, now).unwrap();
    world.carry_out(&mut actor, out);
    assert_eq!(world.took, [1]);
    world.says(1, &json!({"type": "session", "sessionID": "ses_a"}));
    world.says(1, &json!({"type": "steer.open"}));
    world.hint(&mut actor);

    let turn = world.send_while_running(&mut actor, "2", "more");
    let given = takeover::prompt_uuid("trace-2").unwrap();
    assert_eq!(world.steered, [json!({"words": given, "text": "more"})]);
    world.says(1, &json!({"type": "steer.read", "id": "never-given"}));
    world.says(1, &json!({"type": "steer.read", "id": ""}));
    world.hint(&mut actor);
    assert!(world.joined.is_empty(), "{:?}", world.joined.len());
    assert_eq!(messages(&actor), ["u-1"]);

    world.says(1, &json!({"type": "turn.completed"}));
    world.exits(1, Some(0), None);
    world.hint(&mut actor);
    assert_eq!(
        world.took,
        [1, turn.0],
        "the message the turn never read runs on its own"
    );
    assert_eq!(messages(&actor), ["u-1", "u-2"]);
    assert!(world.joined.is_empty());
}

/// A step of an OpenCode answer that says `text`, as the lane writes OpenCode's events.
fn opencode_step(id: &str, text: &str) -> Vec<Value> {
    let part = |part: Value| json!({"type": "message.part.updated", "properties": {"sessionID": "ses_a", "part": part}});
    vec![
        part(json!({"id": format!("{id}s"), "messageID": id, "type": "step-start"})),
        part(json!({"id": format!("{id}t"), "messageID": id, "type": "text", "text": text})),
        part(
            json!({"id": format!("{id}f"), "messageID": id, "type": "step-finish", "reason": "stop"}),
        ),
    ]
}

/// Issue 933. After a Stop, a lane finds by its tool's own record that the tool read steered
/// words (`confirmed`: OpenCode's stored answer to them, Codex's list of the turn): they were read
/// into the stopped turn and never run again, also when the Host restarts before the stopped
/// tool is gone: a reading the actor met before the restart, and one the lane writes only while
/// the Host is down, which the reopened actor meets first in the spool. Nothing else the stopped tool printed is taken. A reading the lane only assumed
/// (words it could not take back), one of words the turn was never given, and a second one say
/// nothing: the words still unread run after the Stop, once each, in the order they were sent.
#[test]
fn after_a_stop_only_a_confirmed_lane_reading_keeps_words_from_running_again() {
    let mut world = World::new("confirmed-after-stop");
    world.backend = clarp_backend::by_id("opencode").unwrap();
    let mut actor = world.open();
    let now = world.tick();
    let first = Message {
        id: "u-1".into(),
        text: "go".into(),
        origin: Origin::User,
        trace_id: Some("trace-1".into()),
        ..Message::default()
    };
    let (_, out) = actor.send(&first, Admission::Queue, now).unwrap();
    world.carry_out(&mut actor, out);
    world.says(1, &json!({"type": "session", "sessionID": "ses_a"}));
    world.says(1, &json!({"type": "steer.open"}));
    world.hint(&mut actor);
    let read = world.send_while_running(&mut actor, "2", "read");
    let assumed = world.send_while_running(&mut actor, "3", "assumed");
    let unread = world.send_while_running(&mut actor, "4", "unread");
    let later = world.send_while_running(&mut actor, "5", "read later");
    assert_eq!(world.steered.len(), 4);
    let id = |trace: &str| takeover::prompt_uuid(trace).unwrap();

    let now = world.tick();
    let (told, out) = actor
        .stop_and_start_waiting(Control::by(clarp_kernel::control::USER), now)
        .unwrap();
    assert_eq!(told, 1);
    world.carry_out(&mut actor, out);
    assert_eq!(world.stopped, [1]);

    // What the stopped tool still says is not taken; the lane's reading of the first words is.
    for line in opencode_step("m9", "after the stop") {
        world.says(1, &line);
    }
    world.says(
        1,
        &json!({"type": "steer.read", "id": id("trace-2"), "confirmed": true}),
    );
    world.hint(&mut actor);
    assert_eq!(world.joined.len(), 1);
    assert_eq!(world.joined[0].turn, read);

    // The Host restarts before the lane is done: the turn is taken up again from its log.
    drop(actor);
    world.says(1, &json!({"type": "steer.read", "id": id("trace-3")}));
    world.says(
        1,
        &json!({"type": "steer.read", "id": id("trace-5"), "confirmed": true}),
    );
    world.says(
        1,
        &json!({"type": "steer.read", "id": id("trace-2"), "confirmed": true}),
    );
    world.says(
        1,
        &json!({"type": "steer.read", "id": "never-given", "confirmed": true}),
    );
    world.exits(1, None, Some(2));
    let mut actor = world.open();
    world.hint(&mut actor);

    // Read into a turn: the confirmed words once, the assumed ones never. (Words that waited
    // behind may go to the next turn together with its own, a reading of their own.)
    let joined: Vec<u64> = world.joined.iter().map(|read| read.turn.0).collect();
    assert_eq!(
        joined.iter().filter(|turn| **turn == read.0).count(),
        1,
        "{joined:?}"
    );
    assert_eq!(
        joined.iter().filter(|turn| **turn == later.0).count(),
        1,
        "{joined:?}"
    );
    assert!(!joined.contains(&assumed.0), "{joined:?}");
    assert!(
        ends(&actor)
            .iter()
            .any(|(end, status)| end == "t-1" && status == "interrupted"),
        "{:?}",
        ends(&actor)
    );
    assert_eq!(world.took, [1, assumed.0], "the read words never run again");
    world.says(assumed.0, &json!({"type": "session", "sessionID": "ses_a"}));
    world.says(assumed.0, &json!({"type": "turn.completed"}));
    world.exits(assumed.0, Some(0), None);
    world.hint(&mut actor);
    assert!(
        !world.took.contains(&read.0) && !world.took.contains(&later.0),
        "{:?}",
        world.took
    );
    assert!(
        !replies(&actor)
            .iter()
            .any(|(text, _, _)| text == "after the stop"),
        "{:?}",
        replies(&actor)
    );
    assert_eq!(messages(&actor), ["u-1", "u-2", "u-5", "u-3", "u-4"]);
    // The words still unread are delivered once: in the next turn's prompt, or as their own turn.
    let joined: Vec<u64> = world.joined.iter().map(|read| read.turn.0).collect();
    assert_ne!(
        world.took.contains(&unread.0),
        joined.contains(&unread.0),
        "{:?} {joined:?}",
        world.took
    );
}
