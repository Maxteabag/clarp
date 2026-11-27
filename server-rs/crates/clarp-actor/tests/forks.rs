//! The agent's conversation fork (issue 718) against a world that plays the shims: a message of
//! the user's while the agent works for another agent is answered at once by a fork of its
//! session, the next goes to the same fork, the agent's next turn opens with a note of it, and
//! a fork that cannot answer gives its message back with nothing lost and nothing run twice.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use clarp_actor::fork::{FORK_ATTEMPT, FORK_ENV};
use clarp_actor::{Action, Actor, Marks, Outputs, Profile, ShimOrder};
use clarp_backend::claude::Claude;
use clarp_codec::standard_set;
use clarp_kernel::conversation::{Entry, Message, Sender};
use clarp_kernel::turn::{Admission, Origin, TurnId};
use clarp_ports::log::LogEngine;
use clarp_spool::{Entry as Line, Record, ShimEvent, SpoolWriter};
use clarp_store_mem::MemEngine;
use serde_json::json;

static WORLDS: AtomicU64 = AtomicU64::new(0);

struct World {
    root: PathBuf,
    engine: Box<dyn LogEngine>,
    profile: Profile,
    now_ms: i64,
    /// The shims that run, by the spool they write.
    shims: BTreeMap<PathBuf, SpoolWriter>,
    /// Every spool a shim was started for, in order: one in here twice ran twice.
    took: Vec<PathBuf>,
    /// The orders of the fork's turns, in order.
    forks: Vec<ShimOrder>,
    followed: Vec<(u64, u32)>,
    stopped: Vec<PathBuf>,
    /// The next launch of the agent's own turn is held: no shim takes it.
    hold_next: bool,
    /// The launches that were held.
    held: Vec<u64>,
    /// The arguments of each launch of the agent's own turns, in order.
    launched: Vec<Vec<String>>,
}

impl World {
    fn new(name: &str) -> Self {
        let n = WORLDS.fetch_add(1, Ordering::SeqCst);
        let root =
            std::env::temp_dir().join(format!("clarp-forks-{name}-{}-{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let profile = Profile {
            agent_id: "a1".to_string(),
            session: "hazel".to_string(),
            conversation_id: "11111111-1111-4111-8111-111111111111".to_string(),
            dir: root.join("agents/a1"),
            run_dir: root.join("run"),
            cwd: root.join("work"),
            model: None,
            effort: None,
            told: None,
            tool_env: vec![("CLARP_HOST_ADDR".to_string(), "127.0.0.1:1".to_string())],
            stop_grace_ms: 3_000,
            keep_spools: true,
            accounts: clarp_kernel::recovery::Accounts::None,
            epoch: "epoch-test".to_string(),
            hides_coordination: true,
        };
        Self {
            root,
            engine: Box::new(MemEngine::new()),
            profile,
            now_ms: 1_000,
            shims: BTreeMap::new(),
            took: Vec::new(),
            forks: Vec::new(),
            followed: Vec::new(),
            stopped: Vec::new(),
            hold_next: false,
            held: Vec::new(),
            launched: Vec::new(),
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
            Actor::open(self.profile.clone(), Arc::new(Claude::new()), log, now).unwrap();
        self.carry_out(&mut actor, out);
        actor
    }

    fn carry_out(&mut self, actor: &mut Actor, first: Outputs) {
        let mut asked = vec![first];
        while let Some(out) = asked.pop() {
            for action in out.actions {
                match action {
                    Action::Launch(order) if std::mem::take(&mut self.hold_next) => {
                        self.held.push(order.turn.0);
                    }
                    Action::Launch(order) => {
                        self.launched.push(order.args.clone());
                        self.launch(&order);
                        let now = self.tick();
                        asked.push(actor.spool_changed(now).unwrap());
                    }
                    Action::Fork { order, .. } => {
                        self.launch(&order);
                        self.forks.push(*order);
                        let now = self.tick();
                        asked.push(actor.spool_changed(now).unwrap());
                    }
                    Action::ForkFollow { turn, attempt, .. } => {
                        self.followed.push((turn.0, attempt));
                    }
                    Action::Follow { turn, attempt, .. }
                    | Action::FollowExit { turn, attempt, .. } => {
                        self.followed.push((turn.0, attempt))
                    }
                    Action::Stop { turn, socket, .. } => {
                        let fork = socket
                            .to_string_lossy()
                            .contains(&format!(".{FORK_ATTEMPT}"));
                        let attempt = if fork { FORK_ATTEMPT } else { 1 };
                        self.stopped.push(self.profile.spool_path_of(turn, attempt));
                    }
                    Action::Steer { .. } => panic!("no follow-up is steered here"),
                    Action::CheckNetwork { .. } => panic!("no connection fails here"),
                }
            }
        }
    }

    fn launch(&mut self, order: &ShimOrder) {
        assert!(order.stdin_file.is_file(), "the message is on disk first");
        let writer = SpoolWriter::create(&order.spool).expect("one shim per spool");
        self.shims.insert(order.spool.clone(), writer);
        self.took.push(order.spool.clone());
        self.write(
            &order.spool.clone(),
            Line::Shim(ShimEvent::Spawned { pid: 4_242 }),
        );
    }

    fn main(&self, turn: u64) -> PathBuf {
        self.profile.spool_path_of(TurnId(turn), 1)
    }

    fn fork(&self, turn: u64) -> PathBuf {
        self.profile.spool_path_of(TurnId(turn), FORK_ATTEMPT)
    }

    fn write(&mut self, spool: &PathBuf, entry: Line) {
        let t = self.tick();
        let shim = self.shims.get_mut(spool).expect("a shim runs there");
        shim.append(&Record { t, entry }).unwrap();
    }

    fn says(&mut self, spool: &PathBuf, line: serde_json::Value) {
        self.write(spool, Line::Out(line.to_string()));
    }

    /// One whole message of Claude Code's, as it streams it.
    fn answers(&mut self, spool: &PathBuf, id: &str, said: &str) {
        let piece = |event: serde_json::Value| json!({"type": "stream_event", "event": event});
        let name = format!("msg_{id}");
        self.says(
            spool,
            piece(json!({"type": "message_start", "message": {"id": name}})),
        );
        let block = json!({"type": "text", "text": said});
        self.says(
            spool,
            piece(json!({"type": "content_block_start", "index": 0, "content_block": block})),
        );
        self.says(spool, piece(json!({"type": "message_stop"})));
    }

    fn ends(&mut self, spool: &PathBuf, said: &str) {
        self.says(
            spool,
            json!({"type": "result", "subtype": "success", "is_error": false, "result": said}),
        );
        self.exits(spool, Some(0));
    }

    fn exits(&mut self, spool: &PathBuf, code: Option<i32>) {
        self.write(spool, Line::Shim(ShimEvent::Exited { code, signal: None }));
        self.shims.remove(spool);
    }

    fn hint(&mut self, actor: &mut Actor) {
        let now = self.tick();
        let out = actor.spool_changed(now).unwrap();
        self.carry_out(actor, out);
    }

    /// Another agent writes: the agent's own turn works for that agent.
    fn agent_writes(&mut self, actor: &mut Actor, id: &str) {
        let message = Message {
            trace_id: Some(format!("tr-{id}")),
            origin: Origin::Agent {
                agent_id: "a-boss".to_string(),
            },
            sender: Some(Sender {
                agent_id: "a-boss".to_string(),
                name: "Boss".to_string(),
                session: "boss".to_string(),
            }),
            ..message(id, "work for me")
        };
        let marks = Marks {
            coordination: Some("a-boss".to_string()),
            trace_named: true,
            user_word: false,
            wait_for_turn: false,
        };
        let now = self.tick();
        let (_, out) = actor
            .send_marked(&message, Admission::Queue, None, &marks, now)
            .unwrap()
            .unwrap();
        self.carry_out(actor, out);
    }

    /// The user writes, and the Host offers the message to the fork, as it does for an agent
    /// its owner named in Conversation view. Answers whether the fork took it.
    fn user(&mut self, actor: &mut Actor, id: &str, text: &str) -> bool {
        let now = self.tick();
        let (sent, out) = actor
            .send(&message(id, text), Admission::Queue, now)
            .unwrap();
        self.carry_out(actor, out);
        if !sent.queued {
            return false;
        }
        let now = self.tick();
        let (taken, out) = actor.fork_take(sent.turn.unwrap(), now).unwrap();
        self.carry_out(actor, out);
        taken
    }

    fn stdin(&self, turn: u64, attempt: u32) -> String {
        std::fs::read_to_string(self.profile.stdin_path_of(TurnId(turn), attempt)).unwrap()
    }
}

impl Drop for World {
    fn drop(&mut self) {
        let _ = std::fs::remove_dir_all(&self.root);
    }
}

fn message(id: &str, text: &str) -> Message {
    Message {
        id: id.to_string(),
        text: text.to_string(),
        origin: Origin::User,
        ..Message::default()
    }
}

/// What the user sees: each row's id, what it says, and the session of a reply.
fn chat(actor: &Actor) -> Vec<(String, String, String)> {
    let page = actor
        .tail_of(100, clarp_actor::conversation::Which::WithoutCoordination)
        .unwrap();
    page.entries
        .iter()
        .filter_map(|row| match &row.entry {
            Entry::Message(message) => Some((
                message.id.clone(),
                format!("user: {}", message.text),
                String::new(),
            )),
            Entry::Reply(reply) => Some((
                reply.id.clone(),
                format!("agent: {}", reply.text),
                reply.backend_session.clone().unwrap_or_default(),
            )),
            _ => None,
        })
        .collect()
}

fn arg_after<'a>(order: &'a ShimOrder, flag: &str) -> Option<&'a str> {
    let at = order.args.iter().position(|arg| arg == flag)?;
    order.args.get(at + 1).map(String::as_str)
}

/// The agent works for another agent, its own session named.
fn busy_for_an_agent(world: &mut World, actor: &mut Actor) {
    world.agent_writes(actor, "u-a1");
    let main = world.main(1);
    world.says(
        &main,
        json!({"type": "system", "subtype": "init", "session_id": world.profile.conversation_id}),
    );
    world.hint(actor);
    assert!(actor.status().live.is_some());
}

#[test]
fn the_fork_answers_at_once_takes_the_next_message_and_opens_the_next_turn_with_a_note() {
    let mut world = World::new("answers");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    let conversation = world.profile.conversation_id.clone();

    // The user writes: a fork of the agent's own session answers.
    assert!(world.user(&mut actor, "u-1", "Are you there?"));
    let first = world.forks[0].clone();
    assert_eq!(first.spool, world.fork(2));
    assert_eq!(arg_after(&first, "--resume"), Some(conversation.as_str()));
    assert!(first.args.iter().any(|arg| arg == "--fork-session"));
    let session = arg_after(&first, "--session-id").unwrap().to_string();
    assert_ne!(session, conversation);
    assert!(
        first
            .env
            .iter()
            .any(|(key, value)| key == FORK_ENV && value == "1")
    );
    assert!(world.stdin(2, FORK_ATTEMPT).contains("Are you there?"));
    assert!(world.stdin(2, FORK_ATTEMPT).contains("short-lived fork"));
    let fork = world.fork(2);
    world.says(
        &fork,
        json!({"type": "system", "subtype": "init", "session_id": session}),
    );
    world.answers(&fork, "f1", "Yes, working for Boss; here now.");
    world.ends(&fork, "Yes, working for Boss; here now.");
    world.hint(&mut actor);
    assert_eq!(
        chat(&actor),
        [
            ("u-1".into(), "user: Are you there?".into(), String::new()),
            (
                "r-f1".into(),
                "agent: Yes, working for Boss; here now.".into(),
                session.clone()
            ),
        ]
    );
    assert!(
        actor.status().live.is_some(),
        "the agent's own turn still works"
    );

    // The next message goes to the same fork, which takes its own session up.
    assert!(world.user(&mut actor, "u-2", "Send Max the summary."));
    let second = world.forks[1].clone();
    assert_eq!(arg_after(&second, "--resume"), Some(session.as_str()));
    assert!(!second.args.iter().any(|arg| arg == "--fork-session"));
    let fork = world.fork(3);
    world.answers(&fork, "f2", "Sent.");
    world.ends(&fork, "Sent.");
    world.hint(&mut actor);

    // The agent's own turn ends: nothing of the user's runs again in it.
    let main = world.main(1);
    world.answers(&main, "m1", "Done, Boss.");
    world.ends(&main, "Done, Boss.");
    world.hint(&mut actor);
    assert!(!actor.status().live.is_some());
    assert!(!world.took.contains(&world.main(2)) && !world.took.contains(&world.main(3)));
    assert_eq!(
        world.took.len(),
        3,
        "one main turn and two of the fork's: {:?}",
        world.took
    );

    // Its next turn opens with what the fork said and did.
    world.agent_writes(&mut actor, "u-a2");
    let opened = world.stdin(4, 1);
    assert!(opened.contains("While you were busy"), "{opened}");
    assert!(opened.contains("Are you there?") && opened.contains("Send Max the summary."));
    assert!(opened.contains("here now.") && opened.contains("Sent."));

    // The fork is over: the next message of the user's forks anew.
    assert!(world.user(&mut actor, "u-3", "And now?"));
    let third = world.forks[2].clone();
    assert!(third.args.iter().any(|arg| arg == "--fork-session"));
    assert_ne!(arg_after(&third, "--session-id"), Some(session.as_str()));
    assert_eq!(
        actor.profile().conversation_id,
        conversation,
        "a fork never renames the conversation"
    );
}

#[test]
fn a_fork_that_cannot_answer_gives_the_message_back_whole() {
    let mut world = World::new("gives-back");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    assert!(world.user(&mut actor, "u-1", "Quick question"));
    // Its tool ends without a word: nothing of it reached the user.
    let fork = world.fork(2);
    world.exits(&fork, Some(1));
    world.hint(&mut actor);
    assert!(chat(&actor).is_empty(), "{:?}", chat(&actor));
    // The fork takes nothing more while it is broken.
    assert!(!world.user(&mut actor, "u-2", "Still there?"));

    // The agent's own turn ends: the messages run as they would have, each once.
    let main = world.main(1);
    world.answers(&main, "m1", "Done.");
    world.ends(&main, "Done.");
    world.hint(&mut actor);
    assert!(world.took.contains(&world.main(2)));
    assert!(world.stdin(2, 1).contains("Quick question"));
    let runs = world
        .took
        .iter()
        .filter(|spool| **spool == world.main(2))
        .count();
    assert_eq!(runs, 1);
}

#[test]
fn a_fork_not_yet_answering_when_the_agent_is_free_is_stopped_and_its_message_runs() {
    let mut world = World::new("overtaken");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    assert!(world.user(&mut actor, "u-1", "Hello"));
    let main = world.main(1);
    world.answers(&main, "m1", "Done.");
    world.ends(&main, "Done.");
    world.hint(&mut actor);
    assert!(
        world.stopped.contains(&world.fork(2)),
        "{:?}",
        world.stopped
    );
    assert!(world.took.contains(&world.main(2)));
    // What the stopped fork still prints is not the user's to see.
    let fork = world.fork(2);
    world.answers(&fork, "late", "Too late.");
    world.ends(&fork, "Too late.");
    world.hint(&mut actor);
    assert!(
        chat(&actor).iter().all(|(id, _, _)| id != "r-late"),
        "{:?}",
        chat(&actor)
    );
}

#[test]
fn only_a_turn_for_other_agents_is_forked() {
    let mut world = World::new("users-turn");
    let mut actor = world.open();
    world.user(&mut actor, "u-1", "Do the thing");
    assert!(actor.status().live.is_some());
    assert!(!world.user(&mut actor, "u-2", "And another"));
    assert!(world.forks.is_empty());
}

#[test]
fn a_fork_that_ran_on_while_the_host_was_away_is_followed_again() {
    let mut world = World::new("restart");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    assert!(world.user(&mut actor, "u-1", "Status?"));
    let fork = world.fork(2);
    world.answers(&fork, "f1", "Half");
    world.hint(&mut actor);
    drop(actor);

    let mut actor = world.open();
    assert!(world.followed.contains(&(2, FORK_ATTEMPT)));
    world.answers(&fork, "f2", "Whole.");
    world.ends(&fork, "Whole.");
    world.hint(&mut actor);
    let shown: Vec<String> = chat(&actor).into_iter().map(|(id, _, _)| id).collect();
    assert_eq!(shown, ["u-1", "r-f1", "r-f2"]);
}

#[test]
fn while_the_fork_answers_live_shows_its_turn_with_the_agents_beside_and_stop_ends_only_it() {
    let mut world = World::new("live-and-stop");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    assert!(world.user(&mut actor, "u-1", "Where are we?"));
    let session = arg_after(&world.forks[0], "--session-id")
        .unwrap()
        .to_string();
    let fork = world.fork(2);
    world.answers(&fork, "half", "Halfway.");
    world.hint(&mut actor);

    // What the user talks to is the fork's turn; the agent's own runs beside it.
    let now = world.tick();
    let shown = actor.shown_whole(now);
    assert_eq!(shown["turn"]["audience"], "user", "{shown:#}");
    assert_eq!(shown["turn"]["status"], "running");
    assert_eq!(shown["main_turn"]["audience"], "agents", "{shown:#}");
    assert_eq!(shown["main_turn"]["status"], "running");
    assert!(shown["main_turn"]["activity"]["state"].is_string());
    assert!(shown["main_turn"].get("headline").is_none());

    // Every row of the fork's turn says it came from the fork, the user's own included.
    let page = actor
        .tail_of(100, clarp_actor::conversation::Which::WithoutCoordination)
        .unwrap();
    for row in &page.entries {
        assert_eq!(row.session.as_deref(), Some(session.as_str()), "{row:?}");
    }

    // A Stop stops the fork, and only the fork.
    let (stopped, out) = actor.fork_halt();
    assert!(stopped);
    world.carry_out(&mut actor, out);
    assert!(world.stopped.contains(&world.fork(2)));
    world.write(
        &fork,
        Line::Shim(ShimEvent::Exited {
            code: None,
            signal: Some(15),
        }),
    );
    world.shims.remove(&fork);
    world.hint(&mut actor);
    assert!(
        actor.status().live.is_some(),
        "the agent's own turn runs on"
    );
    assert!(!world.stopped.contains(&world.main(1)));
    let now = world.tick();
    let shown = actor.shown_whole(now);
    assert!(shown.get("main_turn").is_none(), "{shown:#}");
    assert!(!actor.fork_halt().0, "nothing of the fork runs any more");
}

/// The ops of live events, each with the event's number.
fn ops(events: &[serde_json::Value]) -> Vec<(i64, serde_json::Value)> {
    events
        .iter()
        .flat_map(|event| {
            let lseq = event["lseq"].as_i64().unwrap_or(0);
            event["ops"]
                .as_array()
                .cloned()
                .unwrap_or_default()
                .into_iter()
                .map(move |op| (lseq, op))
        })
        .collect()
}

#[test]
fn the_live_events_hand_over_to_the_fork_and_back_and_tell_the_agents_turn_beside_it() {
    let mut world = World::new("live-events");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    let now = world.tick();
    let before = actor.take_shown(now);
    let main_lseq = before
        .iter()
        .filter_map(|event| event["lseq"].as_i64())
        .max()
        .unwrap_or(0);

    assert!(world.user(&mut actor, "u-1", "Where are we?"));
    let fork = world.fork(2);
    world.answers(&fork, "half", "Halfway.");
    world.hint(&mut actor);
    let now = world.tick();
    let during = ops(&actor.take_shown(now));
    // Nothing says the agent's own turn ended: it runs on, beside the fork's.
    assert!(
        during
            .iter()
            .all(|(_, op)| !(op["op"] == "turn" && op["turn"]["status"] == "interrupted")),
        "{during:#?}"
    );
    let (told_at, told) = during
        .iter()
        .find(|(_, op)| op["op"] == "main_turn")
        .cloned()
        .expect("the agent's own turn is told beside the fork's");
    assert_eq!(told["main_turn"]["audience"], "agents", "{told:#}");
    assert_eq!(told["main_turn"]["status"], "running");
    assert!(
        told_at > main_lseq + 1,
        "numbered past the agent's own hub, so a client asks for the snapshot"
    );

    // The fork is stopped by the user: its turn ends as interrupted, and the agent's own turn
    // is no longer beside it.
    let (stopped, out) = actor.fork_halt();
    assert!(stopped);
    world.carry_out(&mut actor, out);
    world.write(
        &fork,
        Line::Shim(ShimEvent::Exited {
            code: None,
            signal: Some(15),
        }),
    );
    world.shims.remove(&fork);
    world.hint(&mut actor);
    let now = world.tick();
    let after = ops(&actor.take_shown(now));
    let ended = after
        .iter()
        .position(|(_, op)| op["op"] == "turn" && op["turn"]["status"] == "interrupted")
        .expect("the fork's turn ends, as interrupted");
    let gone = after
        .iter()
        .position(|(_, op)| op["op"] == "main_turn" && op["main_turn"].is_null())
        .expect("and the agent's own turn is told to be no longer beside it");
    assert!(ended < gone, "{after:#?}");
    let now = world.tick();
    let whole = actor.shown_whole(now);
    assert_eq!(whole["turn"]["audience"], "agents", "{whole:#}");
    assert_eq!(whole["turn"]["status"], "running");
    assert!(whole.get("main_turn").is_none());
}

/// One message of Claude Code's that calls a tool.
fn calls_a_tool(world: &mut World, spool: &PathBuf, id: &str, call: &str) {
    let block = json!({"type": "tool_use", "id": call, "name": "Bash", "input": {"command": "ls"}});
    let whole =
        json!({"type": "assistant", "message": {"id": format!("msg_{id}"), "content": [block]}});
    world.says(spool, whole);
}

/// The fork answers one message and is done; the agent's own turn then ends.
fn fork_answered_and_main_ended(world: &mut World, actor: &mut Actor) {
    busy_for_an_agent(world, actor);
    assert!(world.user(actor, "u-1", "Are you there?"));
    let fork = world.fork(2);
    world.answers(&fork, "f1", "Yes, here.");
    world.ends(&fork, "Yes, here.");
    world.hint(actor);
    let main = world.main(1);
    world.answers(&main, "m1", "Done.");
    world.ends(&main, "Done.");
    world.hint(actor);
    assert!(actor.status().live.is_none());
}

#[test]
fn the_note_goes_to_the_first_turn_whose_tool_runs() {
    let mut world = World::new("note-kept");
    let mut actor = world.open();
    fork_answered_and_main_ended(&mut world, &mut actor);

    // The next turn is given the note, and is stopped before its tool ever starts.
    world.hold_next = true;
    world.agent_writes(&mut actor, "u-a2");
    assert_eq!(world.held, [3]);
    assert!(world.stdin(3, 1).contains("While you were busy"));
    let log = world
        .engine
        .open(&world.profile.dir, standard_set("zstd").unwrap())
        .unwrap();
    let lane = log
        .find(clarp_kernel::journal::kind::FORK_LANE)
        .unwrap()
        .unwrap();
    assert!(
        lane.ts_ms > 0,
        "the fork's record has the time of the turn's start"
    );
    let now = world.tick();
    let (_, out) = actor.stop("user", now).unwrap();
    world.carry_out(&mut actor, out);
    let now = world.tick();
    let out = actor.shim_gone(TurnId(3), 1, now).unwrap();
    world.carry_out(&mut actor, out);
    assert!(actor.status().live.is_none());

    // The turn after it is given the note again; once its tool ran, no later turn is.
    let now = world.tick();
    let (_, out) = actor.resume("user", now).unwrap();
    world.carry_out(&mut actor, out);
    world.agent_writes(&mut actor, "u-a3");
    assert!(world.stdin(4, 1).contains("While you were busy"));
    assert!(world.stdin(4, 1).contains("Yes, here."));
    let main = world.main(4);
    world.answers(&main, "m4", "Noted.");
    world.ends(&main, "Noted.");
    world.hint(&mut actor);
    world.agent_writes(&mut actor, "u-a4");
    assert!(!world.stdin(5, 1).contains("While you were busy"));
}

#[test]
fn a_users_message_while_the_fork_answers_waits_for_it_and_opens_with_its_note() {
    let mut world = World::new("never-two");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    assert!(world.user(&mut actor, "u-1", "Are you there?"));
    let fork = world.fork(2);
    world.answers(&fork, "f1", "Looking.");
    world.hint(&mut actor);
    // The agent's own turn ends while the fork still answers.
    let main = world.main(1);
    world.answers(&main, "m1", "Done.");
    world.ends(&main, "Done.");
    world.hint(&mut actor);

    // The user writes again: the agent's own session does not answer beside the fork.
    let now = world.tick();
    let (sent, out) = actor
        .send(&message("u-2", "And the plan?"), Admission::Queue, now)
        .unwrap();
    world.carry_out(&mut actor, out);
    assert_eq!(sent.turn, Some(TurnId(3)));
    assert!(!world.took.contains(&world.main(3)), "{:?}", world.took);

    // The fork's turn ends: the agent's own turn begins, told what the fork said.
    world.answers(&fork, "f2", "Here now.");
    world.ends(&fork, "Here now.");
    world.hint(&mut actor);
    assert!(world.took.contains(&world.main(3)));
    let opened = world.stdin(3, 1);
    assert!(opened.contains("While you were busy"), "{opened}");
    assert!(opened.contains("Here now.") && opened.contains("And the plan?"));
}

#[test]
fn a_stop_of_a_fork_that_had_not_answered_settles_its_message_as_stopped() {
    let mut world = World::new("stopped-unread");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    assert!(world.user(&mut actor, "u-1", "Never mind"));
    let (stopped, out) = actor.fork_halt();
    assert!(stopped);
    world.carry_out(&mut actor, out);
    let fork = world.fork(2);
    world.write(
        &fork,
        Line::Shim(ShimEvent::Exited {
            code: None,
            signal: Some(15),
        }),
    );
    world.shims.remove(&fork);
    world.hint(&mut actor);
    let page = actor.tail(10).unwrap();
    let user = page
        .entries
        .iter()
        .find(|row| row.entry.id() == "u-1")
        .expect("the stopped message is in the conversation");
    assert_eq!(
        user.turn.as_ref().map(|end| end.status.as_str()),
        Some("stopped")
    );

    // The agent's own turn ends: the stopped message does not run.
    let main = world.main(1);
    world.answers(&main, "m1", "Done.");
    world.ends(&main, "Done.");
    world.hint(&mut actor);
    assert!(!world.took.contains(&world.main(2)), "{:?}", world.took);
    assert!(actor.status().live.is_none());
}

#[test]
fn a_fork_followed_again_after_a_restart_still_closes_its_open_call() {
    let mut world = World::new("restore-calls");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    assert!(world.user(&mut actor, "u-1", "List the files"));
    let fork = world.fork(2);
    calls_a_tool(&mut world, &fork, "f1", "toolu_1");
    world.hint(&mut actor);
    let began = world.now_ms;
    drop(actor);

    let mut actor = world.open();
    let reopened = world.now_ms;
    // The fork's tool dies without a word: its open call is cut off, at its first time.
    world.exits(&fork, Some(1));
    world.hint(&mut actor);
    let page = actor.tail(20).unwrap();
    let end = page
        .entries
        .iter()
        .find_map(|row| row.turn.clone().filter(|end| end.turn == 2))
        .expect("the fork's turn has its end");
    assert!(
        end.started_at_ms.is_some_and(|at| at < began && at > 0),
        "{end:?}"
    );
    let call = page
        .entries
        .iter()
        .flat_map(|row| row.tools.iter())
        .find(|call| call.id == "toolu_1")
        .expect("the call is on its row");
    assert_eq!(
        call.status,
        clarp_kernel::conversation::ToolStatus::Interrupted
    );
    assert!(reopened > began);
}

#[test]
fn a_turn_held_for_the_fork_stays_held_across_a_restart_and_opens_with_the_note() {
    let mut world = World::new("held-restart");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    assert!(world.user(&mut actor, "u-1", "Are you there?"));
    let fork = world.fork(2);
    world.answers(&fork, "f1", "Looking.");
    world.hint(&mut actor);
    let main = world.main(1);
    world.answers(&main, "m1", "Done.");
    world.ends(&main, "Done.");
    world.hint(&mut actor);
    let now = world.tick();
    let (_, out) = actor
        .send(&message("u-2", "And the plan?"), Admission::Queue, now)
        .unwrap();
    world.carry_out(&mut actor, out);
    assert!(!world.took.contains(&world.main(3)));

    // The Host starts again while the fork still answers: the held turn is held again.
    drop(actor);
    let mut actor = world.open();
    assert!(world.followed.contains(&(2, FORK_ATTEMPT)));
    assert!(!world.took.contains(&world.main(3)), "{:?}", world.took);

    world.answers(&fork, "f2", "Here now.");
    world.ends(&fork, "Here now.");
    world.hint(&mut actor);
    assert!(world.took.contains(&world.main(3)));
    let opened = world.stdin(3, 1);
    assert!(opened.contains("While you were busy"), "{opened}");
    assert!(opened.contains("Here now.") && opened.contains("And the plan?"));
}

#[test]
fn only_the_forks_tool_is_refused_file_edits() {
    let mut world = World::new("edit-guard");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    assert!(world.user(&mut actor, "u-1", "Fix the typo"));
    let settings = arg_after(&world.forks[0], "--settings").expect("the fork's settings");
    let guard: serde_json::Value = serde_json::from_str(settings).unwrap();
    let entry = &guard["hooks"]["PreToolUse"][0];
    assert_eq!(entry["matcher"], clarp_actor::fork::EDIT_TOOLS);
    for tool in ["Edit", "Write", "MultiEdit", "NotebookEdit"] {
        assert!(
            clarp_actor::fork::EDIT_TOOLS
                .split('|')
                .any(|named| named == tool)
        );
    }
    assert!(
        entry["hooks"][0]["command"]
            .as_str()
            .is_some_and(|command| command.ends_with("exit 2"))
    );
    let stdin = world.stdin(2, FORK_ATTEMPT);
    assert!(stdin.contains("refuses you file edits"), "{stdin}");

    // The agent's own turn is started as ever.
    let main = world.main(1);
    world.answers(&main, "m1", "Done.");
    world.ends(&main, "Done.");
    world.hint(&mut actor);
    world.agent_writes(&mut actor, "u-a2");
    let own = world
        .launched
        .last()
        .expect("the agent's own turn was launched")
        .clone();
    assert!(
        !own.iter()
            .any(|arg| arg.contains(clarp_actor::fork::EDIT_TOOLS)),
        "{own:?}"
    );
}

/// The `main_turn` operations among live events: what an app that follows the events is told
/// of the agent's own turn beside its fork's (clarp-ios#139).
fn main_turns(events: &[serde_json::Value]) -> Vec<serde_json::Value> {
    ops(events)
        .into_iter()
        .filter(|(_, op)| op["op"] == clarp_actor::fork::MAIN_TURN_OP)
        .map(|(_, op)| op["main_turn"].clone())
        .collect()
}

#[test]
fn main_turn_is_told_once_per_change_and_the_users_stop_leaves_the_agents_mailbox_open() {
    let mut world = World::new("live-quiet");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    let now = world.tick();
    let _ = actor.take_shown(now);
    assert!(world.user(&mut actor, "u-1", "Where are we?"));
    let fork = world.fork(2);
    world.answers(&fork, "half", "Halfway.");
    world.hint(&mut actor);
    let now = world.tick();
    let told = main_turns(&actor.take_shown(now));
    let main = told
        .last()
        .expect("the agent's own turn is told beside the fork's");
    let now = world.tick();
    assert_eq!(
        actor.shown_whole(now)["main_turn"],
        *main,
        "snapshot and events agree"
    );
    assert!(main["started_at_ms"].is_i64() && main["tool_count"].is_u64());
    assert!(main.get("headline").is_none() && main["activity"].get("headline").is_none());

    // Nothing changed: nothing more is told.
    world.hint(&mut actor);
    let now = world.tick();
    assert!(main_turns(&actor.take_shown(now)).is_empty());

    // The user stops the fork: the agent's mailbox is not held, and its own turn runs on.
    let (stopped, out) = actor.fork_halt();
    assert!(stopped);
    world.carry_out(&mut actor, out);
    world.write(
        &fork,
        Line::Shim(ShimEvent::Exited {
            code: None,
            signal: Some(15),
        }),
    );
    world.shims.remove(&fork);
    world.hint(&mut actor);
    assert!(!actor.status().paused, "the agent's mailbox is not held");
    assert!(actor.status().live.is_some());
    assert!(!world.stopped.contains(&world.main(1)));

    // A Stop of the agent's own (`target: main`) stops the agent's own turn.
    let now = world.tick();
    let (_, out) = actor.stop("user", now).unwrap();
    world.carry_out(&mut actor, out);
    assert!(
        world.stopped.contains(&world.main(1)),
        "{:?}",
        world.stopped
    );
}

#[test]
fn a_fork_that_cannot_answer_shows_the_agents_own_turn_again() {
    let mut world = World::new("live-fallback");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    let now = world.tick();
    let main_id = actor.shown_whole(now)["turn"]["turn_id"].clone();
    let _ = actor.take_shown(now);
    assert!(world.user(&mut actor, "u-1", "Are you there?"));
    world.hint(&mut actor);
    let now = world.tick();
    assert_eq!(actor.shown_whole(now)["turn"]["audience"], "user");
    let _ = actor.take_shown(now);

    let fork = world.fork(2);
    world.exits(&fork, Some(1));
    world.hint(&mut actor);
    let now = world.tick();
    assert_eq!(
        main_turns(&actor.take_shown(now)),
        [serde_json::Value::Null]
    );
    let now = world.tick();
    let shown = actor.shown_whole(now);
    assert!(shown.get("main_turn").is_none(), "{shown:#}");
    assert_eq!(shown["turn"]["turn_id"], main_id);
    assert_eq!(
        actor.queue().unwrap().len(),
        1,
        "the message waits as it would have"
    );
}

#[test]
fn a_note_owed_when_the_host_restarts_still_opens_the_next_turn() {
    let mut world = World::new("note-restart");
    let mut actor = world.open();
    fork_answered_and_main_ended(&mut world, &mut actor);
    drop(actor);

    let mut actor = world.open();
    world.agent_writes(&mut actor, "u-a2");
    let opened = world.stdin(3, 1);
    assert!(opened.contains("While you were busy"), "{opened}");
    assert!(opened.contains("Yes, here."), "{opened}");
}

#[test]
fn a_fork_that_answered_while_the_host_was_away_keeps_its_answer_and_the_message_runs_once() {
    let mut world = World::new("answered-away");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    assert!(world.user(&mut actor, "u-1", "Are you there?"));
    let session = arg_after(&world.forks[0], "--session-id")
        .unwrap()
        .to_string();
    drop(actor);

    // While the Host is away the agent's own turn ends, and the fork answers.
    let main = world.main(1);
    world.answers(&main, "m1", "Done.");
    world.ends(&main, "Done.");
    let fork = world.fork(2);
    world.answers(&fork, "away", "Answered while you were away.");
    world.ends(&fork, "Answered while you were away.");

    // The Host comes back: the fork is taken up before the agent's own turn, so what it
    // answered is kept, and the message does not run again.
    let actor = world.open();
    assert!(!world.took.contains(&world.main(2)), "{:?}", world.took);
    let chat = chat(&actor);
    let asked = chat.iter().filter(|(id, _, _)| id == "u-1").count();
    assert_eq!(asked, 1, "{chat:?}");
    let answered: Vec<_> = chat
        .iter()
        .filter(|(_, said, _)| said == "agent: Answered while you were away.")
        .collect();
    assert_eq!(answered.len(), 1, "{chat:?}");
    assert_eq!(answered[0].2, session, "the fork's own words");
    assert!(actor.status().live.is_none());
    assert!(actor.queue().unwrap().is_empty());
}

#[test]
fn a_silent_fork_gives_its_message_back_once_when_the_main_turn_ended_while_the_host_was_away() {
    let mut world = World::new("silent-away");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    assert!(world.user(&mut actor, "u-1", "Are you there?"));
    drop(actor);

    // While the Host is away the agent's own turn ends; the fork has said nothing yet.
    let main = world.main(1);
    world.answers(&main, "m1", "Done.");
    world.ends(&main, "Done.");

    // The Host comes back: the message runs once, as the agent's own turn, and the fork,
    // which never answered, is stopped.
    let actor = world.open();
    let runs = world
        .took
        .iter()
        .filter(|spool| **spool == world.main(2))
        .count();
    assert_eq!(runs, 1, "{:?}", world.took);
    assert!(
        world.stopped.contains(&world.fork(2)),
        "{:?}",
        world.stopped
    );
    let asked = chat(&actor).iter().filter(|(id, _, _)| id == "u-1").count();
    assert_eq!(asked, 1, "the message is in the chat once");
}

#[test]
fn a_message_the_fork_had_answered_does_not_run_again_after_a_restart() {
    let mut world = World::new("read-restart");
    let mut actor = world.open();
    busy_for_an_agent(&mut world, &mut actor);
    assert!(world.user(&mut actor, "u-1", "Are you there?"));
    let fork = world.fork(2);
    world.answers(&fork, "f1", "Yes, here.");
    world.hint(&mut actor);
    drop(actor);

    // While the Host is away the agent's own turn ends, and the fork's ends as well.
    let main = world.main(1);
    world.answers(&main, "m1", "Done.");
    world.ends(&main, "Done.");
    world.ends(&fork, "Yes, here.");

    let mut actor = world.open();
    world.hint(&mut actor);
    assert!(!world.took.contains(&world.main(2)), "{:?}", world.took);
    let answered = chat(&actor)
        .iter()
        .filter(|(_, said, _)| said == "agent: Yes, here.")
        .count();
    assert_eq!(answered, 1, "{:?}", chat(&actor));
    assert!(actor.status().live.is_none());
}
