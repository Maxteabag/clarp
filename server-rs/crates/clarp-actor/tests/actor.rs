//! The actor against a world that plays the shim: turns run, restarts lose nothing, and no turn
//! starts twice.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use clarp_actor::conversation::Which;
use clarp_actor::{Action, Actor, ImportedRow, Outputs, Profile, ShimOrder};
use clarp_backend::Backend;
use clarp_backend::scripted::Scripted;
use clarp_codec::standard_set;
use clarp_kernel::conversation::{Entry, Message, ToolStatus};
use clarp_kernel::turn::{Admission, Origin, Outcome, TurnId};
use clarp_ports::log::LogEngine;
use clarp_spool::{Entry as Line, Record, ShimEvent, SpoolWriter};
use clarp_store_mem::MemEngine;

static WORLDS: AtomicU64 = AtomicU64::new(0);

/// Everything around one agent's actor: its disk, its shims, and the clock.
struct World {
    root: PathBuf,
    engine: MemEngine,
    profile: Profile,
    backend: Arc<dyn Backend>,
    now_ms: i64,
    /// The shims that have a turn, by turn: each is the writer of that turn's spool.
    shims: BTreeMap<u64, SpoolWriter>,
    /// Every turn a shim actually took, in order. A turn in here twice ran twice.
    took: Vec<u64>,
    /// Launches that found the turn taken.
    turned_away: u32,
    followed: Vec<u64>,
    stopped: Vec<u64>,
}

impl World {
    fn new(name: &str) -> Self {
        let n = WORLDS.fetch_add(1, Ordering::SeqCst);
        let root =
            std::env::temp_dir().join(format!("clarp-actor-{name}-{}-{n}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let profile = Profile {
            agent_id: "a1".to_string(),
            session: "maximus".to_string(),
            conversation_id: "conv-1".to_string(),
            dir: root.join("agents/a1"),
            run_dir: root.join("run"),
            cwd: root.join("work"),
            model: None,
            effort: None,
            identity: None,
            tool_env: vec![("CLARP_HOST_ADDR".to_string(), "127.0.0.1:1".to_string())],
            stop_grace_ms: 3_000,
            keep_spools: false,
        };
        Self {
            root,
            engine: MemEngine::new(),
            profile,
            backend: Arc::new(Scripted::echo()),
            now_ms: 1_000,
            shims: BTreeMap::new(),
            took: Vec::new(),
            turned_away: 0,
            followed: Vec::new(),
            stopped: Vec::new(),
        }
    }

    fn tick(&mut self) -> i64 {
        self.now_ms += 10;
        self.now_ms
    }

    /// Starts the server's side: opens the actor from what is on disk, and does what it asks.
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

    /// Does what the actor asked, and what follows from that, until nothing is asked.
    fn carry_out(&mut self, actor: &mut Actor, first: Outputs) {
        let mut asked = vec![first];
        while let Some(out) = asked.pop() {
            for action in out.actions {
                match action {
                    Action::Launch(order) => {
                        self.launch(&order);
                        let now = self.tick();
                        asked.push(actor.spool_changed(now).unwrap());
                    }
                    Action::Follow { turn, .. } => self.followed.push(turn.0),
                    Action::Stop { turn, .. } => self.stopped.push(turn.0),
                }
            }
        }
    }

    /// A shim is started for a turn. It takes the turn only if no shim has it.
    fn launch(&mut self, order: &ShimOrder) {
        assert!(
            order.stdin_file.is_file(),
            "the message is on disk before a shim is asked to read it"
        );
        match SpoolWriter::create(&order.spool) {
            Ok(writer) => {
                self.shims.insert(order.turn.0, writer);
                self.took.push(order.turn.0);
                self.write(order.turn.0, Line::Shim(ShimEvent::Spawned { pid: 4_242 }));
            }
            Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                self.turned_away += 1;
            }
            Err(error) => panic!("the spool cannot be made: {error}"),
        }
    }

    /// The shim of a turn appends to its spool. The server may or may not be there.
    fn write(&mut self, turn: u64, entry: Line) {
        let t = self.tick();
        let shim = self.shims.get_mut(&turn).expect("the turn has a shim");
        shim.append(&Record { t, entry }).unwrap();
    }

    fn says(&mut self, turn: u64, line: &str) {
        self.write(turn, Line::Out(line.to_string()));
    }

    fn exits(&mut self, turn: u64, code: Option<i32>, signal: Option<i32>) {
        self.write(turn, Line::Shim(ShimEvent::Exited { code, signal }));
        self.shims.remove(&turn);
    }

    /// Tells the actor its spool has grown, and does what it asks.
    fn hint(&mut self, actor: &mut Actor) -> Outputs {
        let now = self.tick();
        let out = actor.spool_changed(now).unwrap();
        let seen = out.clone();
        self.carry_out(actor, out);
        seen
    }

    fn send(&mut self, actor: &mut Actor, id: &str, text: &str, admission: Admission) -> Outputs {
        let now = self.tick();
        let (_, out) = actor.send(&message(id, text), admission, now).unwrap();
        let seen = out.clone();
        self.carry_out(actor, out);
        seen
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
        trace_id: None,
        sender: None,
        client: None,
    }
}

/// A conversation as a client would show it: each thing's id with what it says.
fn shown(actor: &Actor) -> Vec<(String, String)> {
    actor
        .tail(1_000)
        .unwrap()
        .entries
        .into_iter()
        .map(|version| {
            let says = match &version.entry {
                Entry::Message(message) => format!("user: {}", message.text),
                Entry::Reply(reply) if version.live => format!("writing: {}", reply.text),
                Entry::Reply(reply) => format!("agent: {}", reply.text),
                Entry::Tool(call) => format!("{} {:?} {:?}", call.name, call.status, call.output),
                Entry::Notice(notice) => format!("notice: {}", notice.code),
                Entry::TurnEnd(end) => format!("end: {}", end.status),
                Entry::Imported(imported) => format!(
                    "from before, {}: {}",
                    imported.row["role"].as_str().unwrap_or("?"),
                    imported.row["text"].as_str().unwrap_or("")
                ),
            };
            (version.entry.id().to_string(), says)
        })
        .collect()
}

fn pair(id: &str, says: &str) -> (String, String) {
    (id.to_string(), says.to_string())
}

#[test]
fn a_message_runs_as_a_turn_and_the_reply_grows_and_settles_in_the_conversation() {
    let mut world = World::new("turn");
    let mut actor = world.open();
    assert_eq!(actor.status().live, None);

    let now = world.tick();
    let (sent, out) = actor
        .send(&message("u-1", "go"), Admission::Queue, now)
        .unwrap();
    assert_eq!(
        (sent.turn, sent.queued, sent.duplicate),
        (Some(TurnId(1)), false, false)
    );
    let order = match &out.actions[..] {
        [Action::Launch(order)] => order.clone(),
        other => panic!("a message to a free agent launches its turn, not {other:?}"),
    };
    assert_eq!(std::fs::read(&order.stdin_file).unwrap(), b"go\n");
    assert_eq!(order.spool, world.profile.spool_path(TurnId(1)));
    assert_eq!(order.program, "sh");
    assert!(actor.status().live.as_ref().unwrap().starting);
    world.carry_out(&mut actor, out);
    assert_eq!(world.took, vec![1]);
    assert!(
        !actor.status().live.as_ref().unwrap().starting,
        "the shim said the tool is up"
    );
    let opened = actor.tail(10).unwrap();
    assert_eq!(shown(&actor), vec![pair("u-1", "user: go")]);

    // The reply as it is written: shown, with a revision of its own, and not stored.
    world.says(1, r#"{"session": "s1"}"#);
    world.says(1, "Let me");
    assert!(world.hint(&mut actor).changed.conversation);
    let first = actor.after(opened.latest_revision, 10).unwrap();
    assert_eq!(first.entries.len(), 1);
    assert!(first.entries[0].live);
    assert!(first.latest_revision > opened.latest_revision);
    world.says(1, "look.");
    world.hint(&mut actor);
    let second = actor.after(first.latest_revision, 10).unwrap();
    assert_eq!(
        second.entries.len(),
        1,
        "the reply changed, so it is newer again"
    );
    assert!(second.entries[0].revision > first.entries[0].revision);
    assert_eq!(
        shown(&actor),
        vec![
            pair("u-1", "user: go"),
            pair("r-1-1", "writing: Let me\nlook.")
        ]
    );
    assert_eq!(actor.status().backend_session.as_deref(), Some("s1"));

    // The tool ends: the reply is stored under the id it was shown with, above every revision
    // it had while it was written.
    world.exits(1, Some(0), None);
    let ended = world.hint(&mut actor);
    assert!(ended.changed.status && ended.changed.conversation);
    let last = actor.after(second.latest_revision, 10).unwrap();
    assert_eq!(last.entries.len(), 1);
    assert!(!last.entries[0].live && last.entries[0].revision > second.entries[0].revision);
    assert_eq!(
        shown(&actor),
        vec![
            pair("u-1", "user: go"),
            pair("r-1-1", "agent: Let me\nlook.")
        ]
    );
    let status = actor.status();
    assert_eq!(status.live, None);
    assert_eq!(status.last.unwrap().outcome, Outcome::Completed);
    assert!(
        !world.profile.spool_path(TurnId(1)).exists()
            && !world.profile.stdin_path(TurnId(1)).exists(),
        "a turn that is in the log leaves no files behind"
    );
    assert!(
        actor
            .after(last.latest_revision, 10)
            .unwrap()
            .entries
            .is_empty(),
        "and a client that has it all is given nothing"
    );
}

#[test]
fn a_message_sent_twice_is_one_turn() {
    let mut world = World::new("twice");
    let mut actor = world.open();
    world.send(&mut actor, "u-1", "go", Admission::Queue);
    let now = world.tick();
    let (again, out) = actor
        .send(&message("u-1", "go"), Admission::Queue, now)
        .unwrap();
    assert!(again.duplicate && out.actions.is_empty() && !out.changed.conversation);
    world.exits(1, Some(0), None);
    world.hint(&mut actor);
    assert_eq!(world.took, vec![1]);
    assert_eq!(actor.status().waiting, vec![]);
    assert_eq!(shown(&actor).len(), 1);
}

#[test]
fn what_is_said_is_in_the_conversation_at_once_and_what_is_left_for_later_when_its_turn_starts() {
    let mut world = World::new("later");
    let mut actor = world.open();
    let ids = |actor: &Actor| {
        actor
            .tail(100)
            .unwrap()
            .entries
            .iter()
            .map(|version| version.entry.id().to_string())
            .collect::<Vec<_>>()
    };
    world.send(&mut actor, "u-1", "go", Admission::Queue);
    // Said while the agent is busy: there at once. Left for later: in the queue.
    let said = world.send(&mut actor, "u-2", "and this", Admission::Queue);
    assert!(said.changed.conversation && said.changed.queue);
    let head = actor.head_revision();
    let left = world.send(&mut actor, "u-3", "when you are done", Admission::Later);
    assert!(!left.changed.conversation && left.changed.queue);
    assert_eq!(ids(&actor), vec!["u-1", "u-2"]);
    assert_eq!(
        actor.head_revision(),
        head,
        "what is not in the conversation does not change it"
    );
    let waiting: Vec<String> = actor
        .queue()
        .unwrap()
        .into_iter()
        .map(|waiting| waiting.message_id)
        .collect();
    assert_eq!(waiting, vec!["u-2", "u-3"], "both wait their turn");

    world.says(1, "One.");
    world.exits(1, Some(0), None);
    world.hint(&mut actor);
    assert_eq!(world.took, vec![1, 2]);
    assert_eq!(ids(&actor), vec!["u-1", "u-2", "r-1-1"]);
    world.says(2, "Two.");
    world.exits(2, Some(0), None);
    let before = actor.head_revision();
    world.hint(&mut actor);
    assert_eq!(world.took, vec![1, 2, 3]);
    assert_eq!(
        ids(&actor),
        vec!["u-1", "u-2", "r-1-1", "r-2-1", "u-3"],
        "its turn started: now it is in the conversation, after the answer before it"
    );
    // A client that asks what is newer is given the message and the answer that closed the
    // turn before it, and holds the newest revision afterwards.
    let page = actor.after(before, 10).unwrap();
    let newer: Vec<&str> = page.entries.iter().map(|v| v.entry.id()).collect();
    assert_eq!(newer, vec!["u-3", "r-2-1"]);
    assert_eq!(page.latest_revision, actor.head_revision());
    assert_eq!(
        page.entries[1].revision,
        actor.head_revision(),
        "the head is the conversation's newest change, not the log's newest item"
    );

    // A restart changes nothing about the conversation, and does not move its head.
    let (head, all) = (actor.head_revision(), ids(&actor));
    drop(actor);
    let mut actor = world.open();
    assert_eq!((actor.head_revision(), ids(&actor)), (head, all));
    world.says(3, "Three.");
    world.exits(3, Some(0), None);
    world.hint(&mut actor);
    assert_eq!(world.took, vec![1, 2, 3], "and no turn ran twice");
    assert_eq!(actor.status().waiting, vec![]);
}

#[test]
fn a_stop_is_passed_to_the_shim_and_a_direct_message_takes_the_place_of_the_live_turn() {
    let mut world = World::new("stop");
    let mut actor = world.open();
    world.send(&mut actor, "u-1", "go", Admission::Queue);
    world.send(&mut actor, "u-2", "later", Admission::Queue);
    assert_eq!(actor.status().waiting, vec![TurnId(2)]);

    let now = world.tick();
    let (told, out) = actor.stop("peter", now).unwrap();
    assert_eq!(told, 1);
    assert!(matches!(
        &out.actions[..],
        [Action::Stop { turn: TurnId(1), grace_ms: 3_000, socket }] if *socket == world.profile.socket_path(TurnId(1))
    ));
    world.carry_out(&mut actor, out);
    let status = actor.status();
    assert!(status.paused && status.live.as_ref().unwrap().stopping);
    // The tool goes, by the signal the shim sent it.
    world.exits(1, None, Some(15));
    world.hint(&mut actor);
    let status = actor.status();
    assert_eq!(
        status.last.unwrap().outcome,
        Outcome::Stopped {
            by: "peter".to_string()
        }
    );
    assert_eq!(
        (status.live, status.waiting),
        (None, vec![TurnId(2)]),
        "what waited is held"
    );

    // The user says something new: it runs although the mailbox is held.
    world.send(&mut actor, "u-3", "this instead", Admission::Direct);
    assert_eq!(world.took, vec![1, 3]);
    // And another, while that one runs: the live turn is told to make way.
    let replaced = world.send(&mut actor, "u-4", "no, this", Admission::Direct);
    assert!(matches!(
        &replaced.actions[..],
        [Action::Stop {
            turn: TurnId(3),
            ..
        }]
    ));
    world.exits(3, None, Some(15));
    world.hint(&mut actor);
    assert_eq!(world.took, vec![1, 3, 4]);
    world.says(4, "Here.");
    world.exits(4, Some(0), None);
    world.hint(&mut actor);
    assert_eq!(
        actor.status().waiting,
        vec![TurnId(2)],
        "still held, until a resume"
    );
    let now = world.tick();
    let (was_held, out) = actor.resume("peter", now).unwrap();
    assert!(was_held);
    world.carry_out(&mut actor, out);
    assert_eq!(world.took, vec![1, 3, 4, 2]);
    let now = world.tick();
    assert!(
        !actor.resume("peter", now).unwrap().0,
        "a mailbox that is not held cannot be resumed"
    );
    assert_eq!(world.stopped, vec![1, 3]);
}

#[test]
fn a_turn_whose_shim_is_gone_without_a_word_is_interrupted_and_the_next_one_runs() {
    let mut world = World::new("gone");
    let mut actor = world.open();
    world.send(&mut actor, "u-1", "go", Admission::Queue);
    world.send(&mut actor, "u-2", "next", Admission::Queue);
    world.says(1, "Half an ans");
    // The shim dies with its tool: no last record. All the server learns is that nobody answers.
    world.shims.remove(&1);
    let now = world.tick();
    let out = actor.shim_gone(TurnId(1), now).unwrap();
    assert!(out.changed.status);
    world.carry_out(&mut actor, out);
    assert!(matches!(
        actor.status().last.unwrap().outcome,
        Outcome::Interrupted { .. }
    ));
    assert_eq!(world.took, vec![1, 2], "the next message runs");
    assert!(
        shown(&actor).contains(&pair("r-1-1", "agent: Half an ans")),
        "what the agent had written before it was cut off is kept"
    );
    assert_eq!(actor.status().live.unwrap().turn, TurnId(2));
    // News about a turn that is over changes nothing.
    let now = world.tick();
    assert_eq!(actor.shim_gone(TurnId(1), now).unwrap(), Outputs::default());
}

#[test]
fn a_shim_that_cannot_be_started_fails_the_turn_with_the_reason() {
    let mut world = World::new("nostart");
    let mut actor = world.open();
    let now = world.tick();
    let (_, out) = actor
        .send(&message("u-1", "go"), Admission::Queue, now)
        .unwrap();
    assert!(matches!(&out.actions[..], [Action::Launch(_)]));
    // The world could not start the shim at all.
    let now = world.tick();
    let out = actor
        .launch_failed(TurnId(1), "clarp-shim: no such file", now)
        .unwrap();
    assert!(out.actions.is_empty());
    assert_eq!(
        actor.status().last.unwrap().outcome,
        Outcome::not_started("clarp-shim: no such file".to_string())
    );
    // A shim that started and could not start the tool says so in the spool.
    let now = world.tick();
    let (_, out) = actor
        .send(&message("u-2", "again"), Admission::Queue, now)
        .unwrap();
    let Action::Launch(order) = &out.actions[0] else {
        panic!("the next message launches");
    };
    let mut spool = SpoolWriter::create(&order.spool).unwrap();
    spool
        .append(&Record {
            t: 5,
            entry: Line::Shim(ShimEvent::SpawnFailed {
                error: "claude: not found".to_string(),
            }),
        })
        .unwrap();
    world.hint(&mut actor);
    assert_eq!(
        actor.status().last.unwrap().outcome,
        Outcome::not_started("claude: not found".to_string())
    );
    assert_eq!(actor.status().live, None);
}

#[test]
fn a_conversation_is_read_in_pages() {
    let mut world = World::new("pages");
    let mut actor = world.open();
    for n in 1..=6u64 {
        world.send(
            &mut actor,
            &format!("u-{n}"),
            &format!("message {n}"),
            Admission::Queue,
        );
        world.says(n, &format!("answer {n}"));
        world.exits(n, Some(0), None);
        world.hint(&mut actor);
    }
    let ids = |page: &clarp_actor::conversation::Page| {
        page.entries
            .iter()
            .map(|version| version.entry.id().to_string())
            .collect::<Vec<_>>()
    };
    let newest = actor.tail(4).unwrap();
    assert_eq!(ids(&newest), vec!["u-5", "r-5-1", "u-6", "r-6-1"]);
    assert!(newest.has_more);
    let older = actor.before("u-5", 4).unwrap().unwrap();
    assert_eq!(ids(&older), vec!["u-3", "r-3-1", "u-4", "r-4-1"]);
    assert!(older.has_more);
    let oldest = actor.before("u-3", 10).unwrap().unwrap();
    assert_eq!(ids(&oldest), vec!["u-1", "r-1-1", "u-2", "r-2-1"]);
    assert!(!oldest.has_more);
    assert!(actor.before("nobody", 4).unwrap().is_none());

    // Catching up from the beginning, a few at a time, gives everything once, in order.
    let mut cursor = 0;
    let mut caught = Vec::new();
    loop {
        let page = actor.after(cursor, 5).unwrap();
        // A row that changed is given again: the reply a turn ended on, when the turn ends.
        caught.extend(ids(&page));
        assert!(page.latest_revision >= cursor);
        cursor = page.latest_revision;
        if !page.has_more {
            break;
        }
    }
    let everything = ids(&actor.tail(100).unwrap());
    let mut once = Vec::new();
    for id in &caught {
        if !once.contains(id) {
            once.push(id.clone());
        }
    }
    assert_eq!(once, everything, "everything, in the order it first came");
    assert_eq!(cursor, actor.head_revision());
    // And the rows that closed a turn say how it ended.
    let closed: Vec<String> = actor
        .tail(100)
        .unwrap()
        .entries
        .iter()
        .filter(|row| {
            row.turn
                .as_ref()
                .is_some_and(|end| end.status == "completed")
        })
        .map(|row| row.entry.id().to_string())
        .collect();
    assert_eq!(
        closed,
        vec!["r-1-1", "r-2-1", "r-3-1", "r-4-1", "r-5-1", "r-6-1"]
    );
}

// ---- restarts -------------------------------------------------------------------------------

#[derive(Clone, Copy, Debug)]
enum Event {
    Send(&'static str, &'static str),
    Says(u64, &'static str),
    Exits(u64),
}

/// Two messages, the second sent while the first turn runs; a tool call; replies in pieces.
#[test]
fn a_chat_read_without_automation_is_counted_in_what_is_left() {
    let mut world = World::new("folded");
    let mut actor = world.open();
    // Six turns: every second one is the Host's own routine, and its rows carry that.
    for n in 1..=6u64 {
        let origin = if n % 2 == 0 {
            Origin::Trigger {
                name: "heartbeat".to_string(),
            }
        } else {
            Origin::User
        };
        let sent = Message {
            origin,
            ..message(&format!("u-{n}"), &format!("message {n}"))
        };
        let now = world.tick();
        let (_, out) = actor.send(&sent, Admission::Queue, now).unwrap();
        world.carry_out(&mut actor, out);
        world.says(n, &format!("answer {n}"));
        world.exits(n, Some(0), None);
        world.hint(&mut actor);
    }
    let ids = |page: &clarp_actor::conversation::Page| {
        page.entries
            .iter()
            .map(|version| version.entry.id().to_string())
            .collect::<Vec<_>>()
    };
    let without = Which::WithoutAutomated;
    assert_eq!(
        ids(&actor.tail(100).unwrap()).len(),
        12,
        "everything is there"
    );
    let newest = actor.tail_of(4, without).unwrap();
    assert_eq!(
        ids(&newest),
        vec!["u-3", "r-3-1", "u-5", "r-5-1"],
        "a page is full of what is left, not thinned out"
    );
    assert!(newest.has_more);
    let older = actor.before_of("u-3", 4, without).unwrap().unwrap();
    assert_eq!(ids(&older), vec!["u-1", "r-1-1"]);
    assert!(!older.has_more);
    // Before a row that is itself left out: what is left before it.
    let around = actor.before_of("u-4", 1, without).unwrap().unwrap();
    assert_eq!(ids(&around), vec!["r-3-1"]);
    assert!(around.has_more);

    // Catching up gives what is left once, in order, and ends at the head: what is left out
    // is passed over, not read again.
    let mut cursor = 0;
    let mut caught = Vec::new();
    loop {
        let page = actor.after_of(cursor, 2, without).unwrap();
        caught.extend(ids(&page));
        assert!(page.latest_revision >= cursor);
        cursor = page.latest_revision;
        if !page.has_more {
            break;
        }
    }
    let mut once = Vec::new();
    for id in &caught {
        if !once.contains(id) {
            once.push(id.clone());
        }
    }
    assert_eq!(
        once,
        vec!["u-1", "r-1-1", "u-3", "r-3-1", "u-5", "r-5-1"],
        "{caught:?}"
    );
    assert_eq!(cursor, actor.head_revision());
    // The rows of a routine turn say whose turn it was.
    let all = actor.tail(100).unwrap();
    let reply = all
        .entries
        .iter()
        .find(|row| row.entry.id() == "r-2-1")
        .unwrap();
    assert_eq!(reply.entry.origin_name(), "heartbeat");
}

fn a_working_day() -> Vec<Event> {
    vec![
        Event::Send("u-1", "go"),
        Event::Says(1, r#"{"session": "s1"}"#),
        Event::Says(1, "Let me look."),
        Event::Send("u-2", "and again"),
        Event::Says(
            1,
            r#"{"tool": "Bash", "id": "t1", "input": {"command": "ls"}}"#,
        ),
        Event::Says(1, r#"{"done": "t1", "output": "a.txt"}"#),
        Event::Says(1, "One file."),
        Event::Says(1, "Done."),
        Event::Exits(1),
        Event::Says(2, r#"{"session": "s2"}"#),
        Event::Says(2, "Second answer."),
        Event::Exits(2),
    ]
}

/// Where the server dies during a day, and how.
#[derive(Clone, Copy, Debug)]
struct Crash {
    /// The server dies while it handles this event...
    at: usize,
    /// ...after it wrote its log and before it did what it had decided to do.
    before_acting: bool,
    /// How many of the following events happen while it is down.
    down_for: usize,
}

struct Day {
    conversation: Vec<(String, String)>,
    took: Vec<u64>,
    last_outcome: Option<Outcome>,
    backend_session: Option<String>,
    idle: bool,
    turned_away: u32,
}

fn live_through(events: &[Event], crash: Option<Crash>) -> Day {
    let mut world = World::new("day");
    let mut actor = Some(world.open());
    let mut down_left = 0usize;
    for (at, event) in events.iter().enumerate() {
        if actor.is_none() {
            // Only a tool that is running can do anything while the server is down. A message
            // cannot be sent to a server that is not there, and a tool that was never started
            // prints nothing: for either, the server has to come back first.
            let possible_while_down = match event {
                Event::Send(..) => false,
                Event::Says(turn, _) | Event::Exits(turn) => world.shims.contains_key(turn),
            };
            if down_left == 0 || !possible_while_down {
                actor = Some(world.open());
            } else {
                down_left -= 1;
            }
        }
        let crash_here = crash.filter(|crash| crash.at == at);
        let asked = match (event, actor.as_mut()) {
            (Event::Send(id, text), Some(actor)) => {
                let now = world.tick();
                let (_, out) = actor
                    .send(&message(id, text), Admission::Queue, now)
                    .unwrap();
                Some(out)
            }
            (Event::Send(..), None) => unreachable!("the server was brought back for a message"),
            (Event::Says(turn, line), actor) => {
                world.says(*turn, line);
                let now = world.tick();
                actor.map(|actor| actor.spool_changed(now).unwrap())
            }
            (Event::Exits(turn), actor) => {
                world.exits(*turn, Some(0), None);
                let now = world.tick();
                actor.map(|actor| actor.spool_changed(now).unwrap())
            }
        };
        match (asked, crash_here) {
            (Some(_), Some(crash)) if crash.before_acting => {
                // The log has it; nothing that was decided was done.
                actor = None;
                down_left = crash.down_for;
            }
            (Some(out), crash) => {
                if let Some(actor) = actor.as_mut() {
                    world.carry_out(actor, out);
                }
                if let Some(crash) = crash {
                    actor = None;
                    down_left = crash.down_for;
                }
            }
            (None, _) => {}
        }
    }
    let actor = match actor {
        Some(actor) => actor,
        None => world.open(),
    };
    let status = actor.status();
    Day {
        conversation: shown(&actor),
        took: world.took.clone(),
        last_outcome: status.last.map(|turn| turn.outcome),
        backend_session: status.backend_session,
        idle: status.live.is_none() && status.waiting.is_empty(),
        turned_away: world.turned_away,
    }
}

#[test]
fn a_day_without_a_restart_is_what_it_should_be() {
    let day = live_through(&a_working_day(), None);
    assert_eq!(
        day.conversation,
        vec![
            pair("u-1", "user: go"),
            pair("u-2", "user: and again"),
            pair("r-1-1", "agent: Let me look."),
            pair("t1", "Bash Ok Some(\"a.txt\")"),
            pair("r-1-2", "agent: One file.\nDone."),
            pair("r-2-1", "agent: Second answer."),
        ]
    );
    assert_eq!(day.took, vec![1, 2]);
    assert_eq!(day.last_outcome, Some(Outcome::Completed));
    assert_eq!(day.backend_session.as_deref(), Some("s2"));
    assert!(day.idle);
    // The tool call is in the conversation once, as it ended.
    assert!(
        day.conversation
            .iter()
            .filter(|(id, _)| id == "t1")
            .all(|(_, says)| says.contains(&format!("{:?}", ToolStatus::Ok)))
    );
}

#[test]
fn a_restart_at_any_point_of_the_day_loses_nothing_and_repeats_nothing() {
    let events = a_working_day();
    let calm = live_through(&events, None);
    let mut tried = 0;
    for at in 0..events.len() {
        for before_acting in [false, true] {
            for down_for in 0..=3 {
                let crash = Crash {
                    at,
                    before_acting,
                    down_for,
                };
                let day = live_through(&events, Some(crash));
                assert_eq!(
                    day.conversation, calm.conversation,
                    "{crash:?}: the conversation is the one of a day without a restart"
                );
                assert_eq!(
                    day.took, calm.took,
                    "{crash:?}: every turn ran, and ran once"
                );
                assert_eq!(day.last_outcome, calm.last_outcome, "{crash:?}");
                assert_eq!(day.backend_session, calm.backend_session, "{crash:?}");
                assert!(day.idle, "{crash:?}: nothing is left running or waiting");
                assert_eq!(
                    day.turned_away, 0,
                    "{crash:?}: no second shim was started for a turn that had one"
                );
                tried += 1;
            }
        }
    }
    assert_eq!(tried, events.len() * 2 * 4);
}

#[test]
fn a_restart_rejoins_a_running_turn_and_starts_one_that_no_shim_took() {
    // A turn that was decided, with the server gone before any shim took it: started on return.
    let mut world = World::new("rejoin");
    let mut actor = world.open();
    let now = world.tick();
    let (_, lost) = actor
        .send(&message("u-1", "go"), Admission::Queue, now)
        .unwrap();
    assert!(matches!(&lost.actions[..], [Action::Launch(_)]));
    drop(actor);
    let mut actor = world.open();
    assert_eq!(world.took, vec![1], "the turn is started after the restart");
    assert!(world.followed.is_empty());

    // A turn that a shim has: followed on return, not started again, and nothing is repeated.
    world.says(1, "Working on");
    world.hint(&mut actor);
    drop(actor);
    world.says(1, "it.");
    let actor = world.open();
    assert_eq!(world.took, vec![1]);
    assert_eq!(world.followed, vec![1]);
    assert_eq!(
        shown(&actor),
        vec![
            pair("u-1", "user: go"),
            pair("r-1-1", "writing: Working on\nit.")
        ],
        "what was written while the server was away is there, once"
    );
    // The revisions go on above what the server before this one had given out.
    assert!(
        actor.tail(10).unwrap().entries[1].revision > actor.tail(10).unwrap().entries[0].revision
    );

    // A stop that was decided and maybe never heard is told again.
    let mut actor = actor;
    let now = world.tick();
    let (_, unheard) = actor.stop("peter", now).unwrap();
    assert!(matches!(&unheard.actions[..], [Action::Stop { .. }]));
    drop(actor);
    let actor = world.open();
    assert_eq!(
        world.stopped,
        vec![1],
        "the stop reaches the shim after the restart"
    );
    assert!(actor.status().live.unwrap().stopping);
}

#[test]
fn an_agent_that_arrives_from_another_host_brings_its_conversation_and_goes_on_in_it() {
    let mut world = World::new("import");
    let mut actor = world.open();
    let came = |id: &str, role: &str, text: &str, ts_ms: i64| ImportedRow {
        id: id.to_string(),
        ts_ms,
        row: serde_json::json!({"id": id, "role": role, "text": text, "revision": 812}),
    };
    let now = world.tick();
    let (taken, out) = actor
        .import(
            "http://elitebook:7682",
            Some("sess-from-before".to_string()),
            vec![
                came("u-old", "user", "what we spoke of", 100),
                came("live-old", "assistant", "the answer back then", 200),
            ],
            now,
        )
        .unwrap();
    assert_eq!(taken, Ok(2));
    assert!(out.changed.conversation && out.actions.is_empty());
    assert_eq!(
        shown(&actor),
        vec![
            pair("u-old", "from before, user: what we spoke of"),
            pair("live-old", "from before, assistant: the answer back then"),
        ]
    );
    assert_eq!(
        actor.status().backend_session.as_deref(),
        Some("sess-from-before")
    );

    // The next message goes on in the tool's own session, and its rows follow the old ones.
    let now = world.tick();
    let (_, out) = actor
        .send(&message("u-new", "and now?"), Admission::Queue, now)
        .unwrap();
    let Action::Launch(order) = &out.actions[0] else {
        panic!("the message launches a turn");
    };
    assert_eq!(order.turn, TurnId(1));
    world.carry_out(&mut actor, out);
    world.says(1, "Here we are again.");
    world.exits(1, Some(0), None);
    world.hint(&mut actor);
    let rows = shown(&actor);
    assert_eq!(rows.len(), 4);
    assert_eq!(rows[2], pair("u-new", "user: and now?"));
    assert_eq!(rows[3], pair("r-new-1", "agent: Here we are again."));
    let page = actor.tail(10).unwrap();
    assert!(
        page.entries
            .windows(2)
            .all(|pair| pair[0].revision < pair[1].revision),
        "old rows and new ones are in one order"
    );

    // It arrives once. A second arrival, or one after it has worked here, is refused.
    let now = world.tick();
    let (again, _) = actor.import("elsewhere", None, Vec::new(), now).unwrap();
    assert!(again.is_err());
    // And all of it is there after a restart.
    drop(actor);
    let actor = world.open();
    assert_eq!(shown(&actor), rows);
    assert_eq!(
        actor.status().backend_session.as_deref(),
        Some("sess-from-before")
    );
}
