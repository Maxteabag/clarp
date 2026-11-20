//! The actor against a world that plays the shim: turns run, restarts lose nothing, and no turn
//! starts twice.
#![allow(clippy::unwrap_used, clippy::expect_used)]

use std::collections::BTreeMap;
use std::path::PathBuf;
use std::sync::Arc;
use std::sync::atomic::{AtomicU64, Ordering};

use clarp_actor::conversation::Which;
use clarp_actor::{Action, Actor, ImportedRow, Outputs, Profile, ShimOrder, TurnFate};
use clarp_backend::Backend;
use clarp_backend::claude::Claude;
use clarp_backend::scripted::Scripted;
use clarp_codec::standard_set;
use clarp_kernel::conversation::{Entry, Message, ToolStatus};
use clarp_kernel::journal::kind;
use clarp_kernel::spoken::{CUT_OFF, KEPT_FOR_MS, LineState, Said};
use clarp_kernel::turn::{Admission, Origin, Outcome, TurnId};
use clarp_ports::log::LogEngine;
use clarp_spool::{Entry as Line, Record, ShimEvent, SpoolWriter};
use clarp_store_mem::MemEngine;
use serde_json::json;

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
            told: None,
            tool_env: vec![("CLARP_HOST_ADDR".to_string(), "127.0.0.1:1".to_string())],
            stop_grace_ms: 3_000,
            keep_spools: false,
            accounts: clarp_kernel::recovery::Accounts::None,
            epoch: "epoch-test".to_string(),
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
        ..Message::default()
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
                Entry::Commentary(part) => format!("along the way: {}", part.reply),
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
        Outcome::stopped(clarp_kernel::control::Control::by("peter"))
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
    let out = actor.shim_gone(TurnId(1), 1, now).unwrap();
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
    assert_eq!(
        actor.shim_gone(TurnId(1), 1, now).unwrap(),
        Outputs::default()
    );
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
        .launch_failed(TurnId(1), 1, "clarp-shim: no such file", now)
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

/// Whoever keeps a time and no revision (the Goal keeper: where a goal's next update reads
/// from is a time) is told the revision the conversation had then, and reads on from it.
#[test]
fn what_was_written_after_a_time_is_read_from_the_revision_the_conversation_had_then() {
    let mut world = World::new("revision-at");
    let mut actor = world.open();
    let mut after_turn = Vec::new();
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
        after_turn.push(world.tick());
    }
    let since = |actor: &Actor, ts_ms: i64| {
        let revision = actor.revision_at(ts_ms).unwrap();
        let page = actor.after(revision, 100).unwrap();
        let ids = page.entries.iter();
        ids.map(|version| version.entry.id().to_string())
            .collect::<Vec<_>>()
    };
    // After the third turn: the three that followed, and nothing of the three before.
    assert_eq!(
        since(&actor, after_turn[2]),
        vec!["u-4", "r-4-1", "u-5", "r-5-1", "u-6", "r-6-1"]
    );
    assert_eq!(since(&actor, after_turn[4]), vec!["u-6", "r-6-1"]);
    // Before anything was written: all of it. After everything: nothing.
    assert_eq!(actor.revision_at(0).unwrap(), 0);
    assert_eq!(since(&actor, 0).len(), 12);
    assert_eq!(since(&actor, after_turn[5]), Vec::<String>::new());
    assert_eq!(actor.revision_at(i64::MAX).unwrap(), actor.head_revision());
    // A time in the middle of a turn: the turn's message was written by then, its answer
    // was not, and the answer is what is read.
    let asked_at = {
        let page = actor.tail(100).unwrap();
        let fifth = page.entries.iter().find(|row| row.entry.id() == "u-5");
        fifth.unwrap().ts_ms
    };
    assert_eq!(since(&actor, asked_at), vec!["r-5-1", "u-6", "r-6-1"]);
}

// ---- restarts -------------------------------------------------------------------------------

#[derive(Clone, Copy, Debug)]
enum Event {
    Send(&'static str, &'static str),
    Says(u64, &'static str),
    Exits(u64),
}

/// A row as a chat shows it: its id, what it says, the uses of its tools as they stand,
/// whether tool work came after it, and how its turn ended when it is the row that closed it.
type Shown = (String, String, String, bool, String);

fn chat(page: &clarp_actor::conversation::Page) -> Vec<Shown> {
    page.entries
        .iter()
        .map(|row| {
            let said = match &row.entry {
                Entry::Message(message) => format!("user: {}", message.text),
                Entry::Reply(reply) if row.live => format!("writing: {}", reply.text),
                Entry::Reply(reply) => format!("agent: {}", reply.text),
                Entry::Notice(notice) => format!("notice: {}", notice.code),
                other => format!("{other:?}"),
            };
            let tools: Vec<String> = row
                .tools
                .iter()
                .map(|call| format!("{} {:?}", call.id, call.status))
                .collect();
            let end = row.turn.as_ref().map(|end| end.status.clone());
            (
                row.entry.id().to_string(),
                said,
                tools.join(", "),
                row.commentary,
                end.unwrap_or_default(),
            )
        })
        .collect()
}

fn row(id: &str, said: &str, tools: &str, commentary: bool, end: &str) -> Shown {
    (
        id.to_string(),
        said.to_string(),
        tools.to_string(),
        commentary,
        end.to_string(),
    )
}

/// One message of Claude Code as it streams it: the message starts, what it says and each
/// tool it calls arrive, the message stops.
fn claude_message(world: &mut World, turn: u64, id: &str, said: &str, calls: &[&str]) {
    let piece = |event: serde_json::Value| json!({"type": "stream_event", "event": event});
    let name = format!("msg_{id}");
    let start = piece(json!({"type": "message_start", "message": {"id": name}}));
    world.says(turn, &start.to_string());
    if !said.is_empty() {
        let block = json!({"type": "text", "text": said});
        let text =
            piece(json!({"type": "content_block_start", "index": 0, "content_block": block}));
        world.says(turn, &text.to_string());
    }
    for call in calls {
        let block =
            json!({"type": "tool_use", "id": call, "name": "Bash", "input": {"command": "ls"}});
        let whole = json!({"type": "assistant", "message": {"id": name, "content": [block]}});
        world.says(turn, &whole.to_string());
    }
    world.says(turn, &piece(json!({"type": "message_stop"})).to_string());
}

/// What a tool gave back, as Claude Code says it.
fn claude_returns(world: &mut World, turn: u64, call: &str, output: &str, failed: bool) {
    let result =
        json!({"type": "tool_result", "tool_use_id": call, "content": output, "is_error": failed});
    let whole = json!({"type": "user", "message": {"role": "user", "content": [result]}});
    world.says(turn, &whole.to_string());
}

#[test]
fn a_message_of_the_agent_is_one_row_with_the_tools_it_calls() {
    let mut world = World::new("message-rows");
    world.backend = Arc::new(Claude::new());
    let mut actor = world.open();
    world.send(&mut actor, "u-1", "go", Admission::Queue);
    let opened = actor.tail(10).unwrap();

    // It says something and calls a tool: one row, with the tool's use on it.
    claude_message(&mut world, 1, "a", "Looking.", &["t1"]);
    world.hint(&mut actor);
    assert_eq!(
        chat(&actor.tail(10).unwrap()),
        [
            row("u-1", "user: go", "", false, ""),
            row("r-a", "agent: Looking.", "t1 Running", false, ""),
        ]
    );
    let first = actor.after(opened.latest_revision, 10).unwrap();
    assert_eq!(
        chat(&first),
        [row("r-a", "agent: Looking.", "t1 Running", false, "")],
        "the row is new once, though it and its tool's use are two things in the log"
    );

    // The tool returns: the row changed, and whoever asks what is newer is given it again.
    claude_returns(&mut world, 1, "t1", "a.txt", false);
    world.hint(&mut actor);
    let second = actor.after(first.latest_revision, 10).unwrap();
    assert_eq!(
        chat(&second),
        [row("r-a", "agent: Looking.", "t1 Ok", false, "")]
    );
    assert!(second.entries[0].revision > first.entries[0].revision);
    assert_eq!(second.entries[0].ts_ms, first.entries[0].ts_ms);

    // A message that says nothing and calls two tools is a row all the same. Tool work came
    // after what was said before: that row was said along the way, and changed with it.
    claude_message(&mut world, 1, "b", "", &["t2", "t3"]);
    world.hint(&mut actor);
    let third = actor.after(second.latest_revision, 10).unwrap();
    assert_eq!(
        chat(&third),
        [
            row("r-a", "agent: Looking.", "t1 Ok", true, ""),
            row("r-b", "agent: ", "t2 Running, t3 Running", false, ""),
        ]
    );
    assert!(third.entries[0].revision > second.entries[0].revision);
    assert!(
        actor
            .after(third.latest_revision, 10)
            .unwrap()
            .entries
            .is_empty()
    );
    claude_returns(&mut world, 1, "t2", "one", false);
    claude_returns(&mut world, 1, "t3", "broke", true);

    // Then it answers, and the turn ends on what it answered.
    claude_message(&mut world, 1, "c", "Done.", &[]);
    world.says(
        1,
        r#"{"type":"result","subtype":"success","is_error":false,"result":"Done."}"#,
    );
    world.exits(1, Some(0), None);
    world.hint(&mut actor);
    let whole = [
        row("u-1", "user: go", "", false, ""),
        row("r-a", "agent: Looking.", "t1 Ok", true, ""),
        row("r-b", "agent: ", "t2 Ok, t3 Error", false, ""),
        row("r-c", "agent: Done.", "", false, "completed"),
    ];
    assert_eq!(chat(&actor.tail(10).unwrap()), whole);
    assert_eq!(actor.status().last.unwrap().outcome, Outcome::Completed);

    // A page is counted in rows, whatever the tools of a row did meanwhile, and a row on an
    // older page is read as it is now.
    let newest = actor.tail(2).unwrap();
    assert_eq!(chat(&newest), whole[2..]);
    assert!(newest.has_more);
    let older = actor.before("r-b", 2).unwrap().unwrap();
    assert_eq!(chat(&older), whole[..2]);
    assert!(!older.has_more);
    let one = actor.before("r-c", 1).unwrap().unwrap();
    assert_eq!(chat(&one), whole[2..3]);
    assert!(one.has_more);

    // A row by its id is the row as it is now. A tool's use is no row.
    let by_id = actor.row("r-a").unwrap().unwrap();
    assert_eq!(by_id.tools[0].output.as_deref(), Some("a.txt"));
    assert!(by_id.commentary);
    assert!(actor.row("t1").unwrap().is_none());
    assert!(actor.row("c-r-a").unwrap().is_none());

    // Catching up from the beginning, two things at a time: every row, in the order it
    // first came, and the last word on each is how it is now.
    let mut cursor = 0;
    let mut caught: Vec<Shown> = Vec::new();
    loop {
        let page = actor.after(cursor, 2).unwrap();
        assert!(page.latest_revision >= cursor);
        cursor = page.latest_revision;
        caught.extend(chat(&page));
        if !page.has_more {
            break;
        }
    }
    assert_eq!(cursor, actor.head_revision());
    let mut ids: Vec<&str> = Vec::new();
    for (id, ..) in &caught {
        if !ids.contains(&id.as_str()) {
            ids.push(id);
        }
    }
    assert_eq!(ids, ["u-1", "r-a", "r-b", "r-c"]);
    for shown in &whole {
        let last = caught.iter().rfind(|row| row.0 == shown.0);
        assert_eq!(last, Some(shown));
    }

    // In the log a row is written once: where it stands. The uses of its tools, that tool
    // work came after it and how its turn ended are parts that name it.
    let log = world
        .engine
        .open(&world.profile.dir, standard_set("zstd").unwrap())
        .unwrap();
    let keys = |kinds| -> Vec<String> {
        let items = log.read_after(0, kinds, 100).unwrap();
        items.into_iter().filter_map(|item| item.key).collect()
    };
    assert_eq!(keys(kind::ROWS), ["u-1", "r-a", "r-b", "r-c"]);
    assert_eq!(
        keys(kind::CONVERSATION),
        [
            "u-1", "r-a", "t1", "t1", "c-r-a", "r-b", "t2", "t3", "t2", "t3", "r-c", "t-1"
        ]
    );
}

#[test]
fn what_a_turn_said_before_a_restart_is_followed_by_the_tool_work_after_it() {
    let mut world = World::new("message-rows-restart");
    world.backend = Arc::new(Claude::new());
    let mut actor = world.open();
    world.send(&mut actor, "u-1", "go", Admission::Queue);
    claude_message(&mut world, 1, "a", "Looking.", &[]);
    world.hint(&mut actor);

    // The server goes and comes back while the turn runs. What the turn has said is in the
    // log, and so is that no tool work has followed it.
    drop(actor);
    let mut actor = world.open();
    assert_eq!(world.followed, vec![1]);
    claude_message(&mut world, 1, "b", "", &["t1"]);
    world.hint(&mut actor);
    assert_eq!(
        chat(&actor.tail(10).unwrap()),
        [
            row("u-1", "user: go", "", false, ""),
            row("r-a", "agent: Looking.", "", true, ""),
            row("r-b", "agent: ", "t1 Running", false, ""),
        ]
    );

    // And again, with a tool running: its use is still a part of its row afterwards.
    drop(actor);
    let mut actor = world.open();
    let before = actor.tail(10).unwrap().latest_revision;
    // The tool's process is killed. The use that never came back reads interrupted on its
    // row, and the word of the Host about the end is the row that closes the turn.
    world.exits(1, None, Some(9));
    world.hint(&mut actor);
    assert_eq!(
        chat(&actor.tail(10).unwrap()),
        [
            row("u-1", "user: go", "", false, ""),
            row("r-a", "agent: Looking.", "", true, ""),
            row("r-b", "agent: ", "t1 Interrupted", false, ""),
            row(
                "n-1-turn-interrupted",
                "notice: turn-interrupted",
                "",
                false,
                "interrupted"
            ),
        ]
    );
    let changed: Vec<String> = chat(&actor.after(before, 10).unwrap())
        .into_iter()
        .map(|row| row.0)
        .collect();
    assert_eq!(changed, ["r-b", "n-1-turn-interrupted"]);
}

#[test]
fn only_an_interrupted_turn_changes_what_its_tools_left_unfinished() {
    let mut world = World::new("message-rows-unfinished");
    world.backend = Arc::new(Claude::new());
    let mut actor = world.open();
    world.send(&mut actor, "u-1", "go", Admission::Queue);
    claude_message(&mut world, 1, "a", "", &["t1"]);
    // The turn ends as done, and the tool never said what it gave back.
    world.says(
        1,
        r#"{"type":"result","subtype":"success","is_error":false,"result":""}"#,
    );
    world.exits(1, Some(0), None);
    world.hint(&mut actor);
    assert_eq!(
        chat(&actor.tail(10).unwrap()),
        [
            row("u-1", "user: go", "", false, ""),
            row("r-a", "agent: ", "t1 Running", false, "completed"),
        ]
    );
}

/// The agent's lines as they stand: each line's id, what is said, and where it stands.
fn lines(actor: &Actor) -> Vec<(String, String, LineState)> {
    let lines = actor.speech().lines().iter();
    lines
        .map(|line| (line.id.clone(), line.text.clone(), line.state))
        .collect()
}

fn line(id: &str, text: &str, state: LineState) -> (String, String, LineState) {
    (id.to_string(), text.to_string(), state)
}

#[test]
fn what_a_reply_marks_for_the_ear_waits_to_be_spoken_and_goes_one_way() {
    let mut world = World::new("spoken");
    world.backend = Arc::new(Claude::new());
    let mut actor = world.open();
    // A message whose answers are said aloud.
    let now = world.tick();
    let aloud = Message {
        speak: true,
        ..message("u-1", "go")
    };
    let (_, out) = actor.send(&aloud, Admission::Queue, now).unwrap();
    world.carry_out(&mut actor, out);
    assert!(actor.speech().lines().is_empty());

    // The agent says something for the eye and something for the ear, and calls a tool.
    // What it marked waits to be spoken from the moment the reply is in the conversation.
    claude_message(
        &mut world,
        1,
        "a",
        "Looking. <speak>I will look.</speak>",
        &["t1"],
    );
    let seen = world.hint(&mut actor);
    assert!(seen.changed.speech && seen.changed.conversation);
    assert_eq!(
        lines(&actor),
        [line("s-a-1", "I will look.", LineState::Queued)]
    );
    let queued = &actor.speech().lines()[0];
    assert_eq!(
        (queued.reply.as_str(), queued.turn, queued.first),
        ("r-a", 1, true)
    );

    // An engine takes it: one line of an agent at a time.
    let now = world.tick();
    let taken = actor.claim_line(now).unwrap().unwrap();
    assert_eq!(
        (taken.id.as_str(), taken.state),
        ("s-a-1", LineState::Speaking)
    );
    assert!(actor.claim_line(now).unwrap().is_none());
    let kore = ("gemini", "Kore");
    let begun = actor
        .line_begun("s-a-1", (41, "1000__maximus.mp3"), kore, Some(9), now)
        .unwrap()
        .unwrap();
    assert_eq!((begun.clip_id, begun.event_id), (Some(41), Some(9)));
    assert!(
        actor
            .line_begun("s-nothing", (42, "x.mp3"), kore, None, now)
            .unwrap()
            .is_none()
    );
    assert!(
        actor
            .line_begun("s-a-1", (42, "x.mp3"), kore, None, now)
            .unwrap()
            .is_none(),
        "a line has one clip"
    );

    // Meanwhile the turn goes on and says more for the ear: it waits behind the first.
    claude_returns(&mut world, 1, "t1", "a.txt", false);
    claude_message(&mut world, 1, "c", "<speak>One file.</speak> Done.", &[]);
    world.says(
        1,
        r#"{"type":"result","subtype":"success","is_error":false,"result":"Done."}"#,
    );
    world.exits(1, Some(0), None);
    world.hint(&mut actor);
    assert_eq!(
        lines(&actor),
        [
            line("s-a-1", "I will look.", LineState::Speaking),
            line("s-c-1", "One file.", LineState::Queued),
        ]
    );
    assert!(actor.claim_line(world.tick()).unwrap().is_none());

    // The clip is whole, and the app says it played it.
    let now = world.tick();
    let said = |bytes| Said {
        bytes,
        provider: "cartesia",
        voice: "c-voice",
    };
    assert!(
        actor
            .line_ended("s-a-1", Some(40), Ok(said(1)), now)
            .unwrap()
            .is_none(),
        "ended by the one who made its clip, and by nobody else"
    );
    let spoken = actor
        .line_ended("s-a-1", Some(41), Ok(said(2_048)), now)
        .unwrap()
        .unwrap();
    assert_eq!(
        (spoken.state, spoken.bytes, spoken.provider.as_deref()),
        (LineState::Spoken, Some(2_048), Some("cartesia")),
        "the engine that spoke is the one that is written"
    );
    assert!(
        actor
            .line_ended("s-a-1", Some(41), Ok(said(1)), now)
            .unwrap()
            .is_none(),
        "over once"
    );
    assert!(actor.line_played(41, "play-ok", None, now).unwrap());
    assert!(
        !actor.line_played(99, "play-ok", None, now).unwrap(),
        "nobody's clip"
    );
    assert_eq!(
        actor.speech().by_clip(41).unwrap().played.as_deref(),
        Some("play-ok")
    );

    // The next line is taken, and the Host goes away while it is being spoken, for long.
    let taken = actor.claim_line(world.tick()).unwrap().unwrap();
    assert_eq!(taken.id, "s-c-1");
    drop(actor);
    world.now_ms += 3 * 60 * 1_000;
    let actor = world.open();
    // What was spoken is still spoken; what was cut off long ago is not begun again.
    assert_eq!(
        lines(&actor),
        [
            line("s-a-1", "I will look.", LineState::Spoken),
            line("s-c-1", "One file.", LineState::Failed),
        ]
    );
    assert_eq!(actor.speech().lines()[1].error.as_deref(), Some(CUT_OFF));
    // What is in the conversation is as it was: speech is beside it, not in it.
    assert_eq!(
        chat(&actor.tail(10).unwrap())
            .iter()
            .map(|row| row.0.as_str())
            .collect::<Vec<_>>(),
        ["u-1", "r-a", "r-c"]
    );
}

#[test]
fn a_line_cut_off_a_moment_ago_waits_again_and_a_stop_takes_back_what_waits() {
    let mut world = World::new("spoken-again");
    world.backend = Arc::new(Claude::new());
    let mut actor = world.open();
    let now = world.tick();
    let aloud = Message {
        speak: true,
        ..message("u-1", "go")
    };
    let (_, out) = actor.send(&aloud, Admission::Queue, now).unwrap();
    world.carry_out(&mut actor, out);
    let said = "<speak>First part.</speak> And <speak>second part.</speak>";
    claude_message(&mut world, 1, "a", said, &[]);
    world.hint(&mut actor);
    assert_eq!(
        lines(&actor),
        [
            line("s-a-1", "First part.", LineState::Queued),
            line("s-a-2", "second part.", LineState::Queued),
        ]
    );
    // An engine takes the first, and the Host is started again a moment later.
    actor.claim_line(world.tick()).unwrap().unwrap();
    drop(actor);
    let mut actor = world.open();
    assert_eq!(
        lines(&actor),
        [
            line("s-a-1", "First part.", LineState::Queued),
            line("s-a-2", "second part.", LineState::Queued),
        ],
        "begun a moment ago: it waits again"
    );
    // Whoever had it before the Host went cannot end it now: it is not theirs any more.
    let taken = actor.claim_line(world.tick()).unwrap().unwrap();
    let now = world.tick();
    actor
        .line_begun(&taken.id, (7, "7__a.mp3"), ("gemini", "Kore"), None, now)
        .unwrap()
        .unwrap();
    assert!(
        actor
            .line_ended(&taken.id, None, Err("from before"), now)
            .unwrap()
            .is_none()
    );
    // And the Host is opened once more, a moment later: it waits again, without that clip.
    drop(actor);
    let mut actor = world.open();
    assert_eq!(actor.speech().lines()[0].clip_id, None);
    // The user stops the agent: nothing more of what it had to say is spoken.
    let (_, out) = actor.stop("user", world.tick()).unwrap();
    assert!(out.changed.speech);
    world.carry_out(&mut actor, out);
    assert_eq!(
        lines(&actor),
        [
            line("s-a-1", "First part.", LineState::Dropped),
            line("s-a-2", "second part.", LineState::Dropped),
        ]
    );
    assert!(actor.claim_line(world.tick()).unwrap().is_none());
    // A Host that comes back a week later has forgotten them; its log has not.
    drop(actor);
    world.now_ms += KEPT_FOR_MS + 1;
    let actor = world.open();
    assert!(actor.speech().lines().is_empty());
    drop(actor);

    // What still waits when a Host comes back after an hour is not spoken any more.
    let mut late = World::new("spoken-late");
    late.backend = Arc::new(Claude::new());
    let mut actor = late.open();
    let now = late.tick();
    let (_, out) = actor.send(&aloud, Admission::Queue, now).unwrap();
    late.carry_out(&mut actor, out);
    claude_message(&mut late, 1, "a", "<speak>Too late.</speak>", &[]);
    late.hint(&mut actor);
    drop(actor);
    late.now_ms += 60 * 60 * 1_000;
    let mut actor = late.open();
    assert_eq!(
        lines(&actor),
        [line("s-a-1", "Too late.", LineState::Dropped)]
    );
    assert!(actor.claim_line(late.tick()).unwrap().is_none());

    // A message whose answers are not said aloud: what its replies mark is read, not heard.
    let mut quiet = World::new("spoken-quiet");
    quiet.backend = Arc::new(Claude::new());
    let mut actor = quiet.open();
    quiet.send(&mut actor, "u-1", "go", Admission::Queue);
    claude_message(&mut quiet, 1, "a", "<speak>Not aloud.</speak>", &[]);
    let seen = quiet.hint(&mut actor);
    assert!(!seen.changed.speech && actor.speech().lines().is_empty());
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

/// The pool of a tool's accounts, as a test holds it: it writes down what it is told, and says
/// that a turn that failed on its account waits.
#[derive(Default)]
struct Pool {
    told: std::sync::Mutex<Vec<String>>,
}

impl Pool {
    fn told(&self) -> Vec<String> {
        self.told.lock().unwrap().clone()
    }
}

impl clarp_actor::Pooled for Pool {
    fn failed(&self, turn: TurnId, plan: bool, _now_ms: i64) -> bool {
        let said = format!("turn {} failed on its account, plan: {plan}", turn.0);
        self.told.lock().unwrap().push(said);
        true
    }

    fn stopped(&self, _now_ms: i64) {
        self.told.lock().unwrap().push("stopped".to_string());
    }
}

#[test]
fn a_turn_the_pool_holds_for_its_plan_waits_and_ends_with_what_the_provider_said() {
    let mut world = World::new("pool-plan");
    world.profile.accounts = clarp_kernel::recovery::Accounts::Select;
    let mut actor = world.open();
    let pool = Arc::new(Pool::default());
    actor.pooled(pool.clone());
    world.send(&mut actor, "u-1", "go", Admission::Queue);
    // The provider refuses: the account's plan does not offer the model.
    let refused = "This model is not available on your current plan";
    world.write(1, Line::Err(refused.to_string()));
    world.exits(1, Some(1), None);
    world.hint(&mut actor);
    // The pool was asked, and told that it is the plan. It says the turn waits: it does,
    // and nothing in the conversation says that it failed.
    assert_eq!(
        pool.told(),
        vec!["turn 1 failed on its account, plan: true"]
    );
    let waits = actor.waits_for_an_account();
    assert!(
        waits.is_some_and(|(turn, _)| turn == TurnId(1)),
        "{waits:?}"
    );
    assert_eq!(actor.status().last, None);
    assert_eq!(shown(&actor), vec![pair("u-1", "user: go")]);

    // No account's plan offers the model, the pool finds: the turn ends, with what the
    // provider said of its own.
    let now = world.tick();
    let out = actor.end_waiting(now).unwrap();
    world.carry_out(&mut actor, out);
    let status = actor.status();
    let last = status.last.expect("the turn is over");
    assert!(
        matches!(&last.outcome, Outcome::Failed { reason, kind } if reason == refused && kind == "account_plan"),
        "{:?}",
        last.outcome
    );
    assert_eq!((status.live, actor.waits_for_an_account()), (None, None));
    let rows = shown(&actor);
    assert!(
        rows.iter().any(|(_, says)| says.starts_with("notice:")),
        "a row says why: {rows:#?}"
    );
    assert_eq!(world.took, vec![1], "it was not run a second time");
    // Asked once more, with nothing waiting: nothing happens.
    let now = world.tick();
    let again = actor.end_waiting(now).unwrap();
    assert!(again.actions.is_empty() && !again.changed.status);
    // And the next message runs.
    world.send(&mut actor, "u-2", "and this", Admission::Queue);
    assert_eq!(world.took, vec![1, 2]);
}

#[test]
fn a_turn_that_is_stopped_for_its_account_waits_and_is_taken_up() {
    let mut world = World::new("pool-stop");
    world.profile.accounts = clarp_kernel::recovery::Accounts::Select;
    let mut actor = world.open();
    let pool = Arc::new(Pool::default());
    actor.pooled(pool.clone());
    // Nothing runs: the pool is told so at once.
    let now = world.tick();
    let out = actor.stop_for_an_account(now).unwrap();
    assert!(out.actions.is_empty());
    assert_eq!(pool.told(), vec!["stopped"]);

    // A turn runs. The account its tool runs on is out for another agent's turn: it is
    // stopped, and the pool is told only when its process is gone.
    world.send(&mut actor, "u-1", "go", Admission::Queue);
    world.says(1, r#"{"session": "s1"}"#);
    world.hint(&mut actor);
    let now = world.tick();
    let out = actor.stop_for_an_account(now).unwrap();
    assert!(matches!(
        &out.actions[..],
        [Action::Stop {
            turn: TurnId(1),
            ..
        }]
    ));
    world.carry_out(&mut actor, out);
    assert_eq!((world.stopped.clone(), pool.told().len()), (vec![1], 1));
    world.exits(1, None, Some(15));
    world.hint(&mut actor);
    assert_eq!(pool.told(), vec!["stopped", "stopped"]);
    // The turn did not end: it waits for an account, and its conversation says nothing of a
    // failure.
    let waits = actor.waits_for_an_account();
    assert!(
        waits.is_some_and(|(turn, _)| turn == TurnId(1)),
        "{waits:?}"
    );
    assert_eq!(actor.status().last, None);
    assert_eq!(shown(&actor), vec![pair("u-1", "user: go")]);

    // The pool takes it up: its next attempt is launched.
    let now = world.tick();
    let out = actor.login_changed(now).unwrap();
    assert!(
        matches!(&out.actions[..], [Action::Launch(order)] if order.turn == TurnId(1) && order.attempt == 2),
        "{:?}",
        out.actions
    );
    world.carry_out(&mut actor, out);
    world.says(1, r#"{"reply": "Done."}"#);
    world.exits(1, Some(0), None);
    world.hint(&mut actor);
    assert_eq!(
        actor.status().last.map(|last| last.outcome),
        Some(Outcome::Completed)
    );
    // A turn that completed all the same before the stop reached it is over, not waiting.
    world.send(&mut actor, "u-2", "again", Admission::Queue);
    let now = world.tick();
    let out = actor.stop_for_an_account(now).unwrap();
    world.carry_out(&mut actor, out);
    world.says(2, r#"{"reply": "Done before the stop."}"#);
    world.exits(2, Some(0), None);
    world.hint(&mut actor);
    assert_eq!(actor.waits_for_an_account(), None);
    assert_eq!(
        actor.status().last.map(|last| last.outcome),
        Some(Outcome::Completed)
    );
}

#[test]
fn what_became_of_the_turn_of_a_message_is_told_by_the_messages_id() {
    let mut world = World::new("fate");
    let mut actor = world.open();
    let fate = |actor: &Actor, id: &str| actor.fate_of(id).unwrap();
    // A message nobody sent has no turn.
    assert_eq!(fate(&actor, "u-0"), TurnFate::Unknown);

    // One that runs, and two that were left for later and wait behind it.
    world.send(&mut actor, "u-1", "go", Admission::Queue);
    world.send(&mut actor, "u-2", "then this", Admission::Later);
    world.send(&mut actor, "u-3", "and this", Admission::Later);
    assert_eq!(fate(&actor, "u-1"), TurnFate::Runs);
    assert_eq!(fate(&actor, "u-2"), TurnFate::Waits);
    assert_eq!(fate(&actor, "u-3"), TurnFate::Waits);
    // One that is taken back before its turn began is gone: it will never run.
    let now = world.tick();
    let (was_waiting, out) = actor.withdraw(TurnId(3), "peter", now).unwrap();
    assert!(was_waiting);
    world.carry_out(&mut actor, out);
    assert_eq!(fate(&actor, "u-3"), TurnFate::Gone);

    // The turn answers and ends: how it ended, the row that closed it, what that row
    // says, and when.
    world.says(1, "the answer");
    world.exits(1, Some(0), None);
    world.hint(&mut actor);
    let end = actor.end_of("u-1").unwrap().unwrap();
    assert_eq!(
        fate(&actor, "u-1"),
        TurnFate::Ended {
            status: "completed".to_string(),
            closes: end.closes.clone(),
            text: "the answer".to_string(),
            ended_at_ms: end.ended_at_ms,
        }
    );
    assert_ne!(
        end.closes, "u-1",
        "the answer closed the turn, not its message"
    );
    // What waited runs now.
    assert_eq!(fate(&actor, "u-2"), TurnFate::Runs);

    // A turn that fails is told with the words that stand under its message, and one that
    // ends with nothing said left nothing.
    world.says(2, r#"{"fail": "the model said no"}"#);
    world.exits(2, Some(1), None);
    world.hint(&mut actor);
    let TurnFate::Ended { status, text, .. } = fate(&actor, "u-2") else {
        panic!("the turn that failed is over");
    };
    assert_eq!(status, "failed");
    assert!(text.contains("the model said no"), "{text}");
    world.send(&mut actor, "u-4", "say nothing", Admission::Queue);
    world.says(4, r#"{"activity": "thinking"}"#);
    world.exits(4, Some(0), None);
    world.hint(&mut actor);
    let TurnFate::Ended {
        status,
        closes,
        text,
        ..
    } = fate(&actor, "u-4")
    else {
        panic!("the turn that said nothing is over");
    };
    assert_eq!(
        (status.as_str(), closes.as_str(), text.as_str()),
        ("completed", "u-4", "")
    );

    // A turn that is stopped ends with no words of its own.
    world.send(&mut actor, "u-5", "go on", Admission::Queue);
    let now = world.tick();
    let (told, out) = actor.stop("peter", now).unwrap();
    assert_eq!(told, 1);
    world.carry_out(&mut actor, out);
    assert_eq!(
        fate(&actor, "u-5"),
        TurnFate::Runs,
        "until the tool is gone"
    );
    world.exits(5, None, Some(15));
    world.hint(&mut actor);
    let TurnFate::Ended { status, text, .. } = fate(&actor, "u-5") else {
        panic!("the turn that was stopped is over");
    };
    assert_eq!((status.as_str(), text.as_str()), ("interrupted", ""));
}

/// Sends a message somebody else than the user wrote.
fn send_from(world: &mut World, actor: &mut Actor, origin: Origin, id: &str, text: &str) {
    let now = world.tick();
    let sent = Message {
        origin,
        ..message(id, text)
    };
    let (_, out) = actor.send(&sent, Admission::Queue, now).unwrap();
    world.carry_out(actor, out);
}

#[test]
fn what_an_agent_last_finished_for_its_user_is_the_end_of_the_turn_their_newest_message_began() {
    let mut world = World::new("finished");
    let mut actor = world.open();
    let theirs = |origin: &str| matches!(origin, "user" | "oracle");
    // What it last finished for them: the message, what was answered, and when.
    let finished_since = |actor: &Actor, since: i64| {
        let found = actor.last_finished_for(theirs, since).unwrap();
        found.map(|(asked, answer)| {
            let Entry::Reply(reply) = &answer.entry else {
                panic!("what closed the turn is no answer: {answer:?}");
            };
            let ended = answer.turn.as_ref().unwrap().ended_at_ms;
            (asked.entry.id().to_string(), reply.text.clone(), ended)
        })
    };
    let finished = |actor: &Actor| finished_since(actor, 0);
    let ended = |actor: &Actor, id: &str| actor.end_of(id).unwrap().unwrap().ended_at_ms;
    let answers = |world: &mut World, actor: &mut Actor, turn: u64, said: &str| {
        world.says(turn, said);
        world.exits(turn, Some(0), None);
        world.hint(actor);
    };
    let the = |id: &str, said: &str, ended: i64| Some((id.to_string(), said.to_string(), ended));
    let an_agent = || Origin::Agent {
        agent_id: "a2".to_string(),
    };

    // An agent nobody wrote to finished nothing, and neither has one that is answering.
    assert_eq!(finished(&actor), None);
    world.send(&mut actor, "u-1", "go", Admission::Queue);
    assert_eq!(finished(&actor), None);
    // Its answer is what it finished: until it is too long ago for whoever asks.
    answers(&mut world, &mut actor, 1, "the first answer");
    let first = ended(&actor, "u-1");
    assert_eq!(finished(&actor), the("u-1", "the first answer", first));
    assert_eq!(
        finished_since(&actor, first),
        the("u-1", "the first answer", first)
    );
    assert_eq!(finished_since(&actor, first + 1), None);

    // What another agent writes to it changes nothing: while that is answered, while more
    // of it waits, and after.
    send_from(&mut world, &mut actor, an_agent(), "u-2", "from an agent");
    assert_eq!(finished(&actor), the("u-1", "the first answer", first));
    send_from(&mut world, &mut actor, an_agent(), "u-2b", "and more");
    assert_eq!(actor.queue().unwrap().len(), 1);
    assert_eq!(finished(&actor), the("u-1", "the first answer", first));
    // What the user leaves for later is what he wrote since, though it waits its turn; and
    // no longer once he took it back.
    world.send(&mut actor, "u-3", "and then?", Admission::Later);
    assert_eq!(finished(&actor), None);
    let now = world.tick();
    let (was_waiting, out) = actor.withdraw(TurnId(4), "peter", now).unwrap();
    assert!(was_waiting);
    world.carry_out(&mut actor, out);
    assert_eq!(finished(&actor), the("u-1", "the first answer", first));
    answers(&mut world, &mut actor, 2, "for the agent");
    answers(&mut world, &mut actor, 3, "and for the agent again");
    assert_eq!(finished(&actor), the("u-1", "the first answer", first));

    // What the Oracle hands it for the user is the user's own: nothing while it is
    // answered, and then its answer.
    let oracle = Origin::Trigger {
        name: "oracle".to_string(),
    };
    send_from(&mut world, &mut actor, oracle, "u-4", "look into it");
    assert_eq!(finished(&actor), None);
    answers(&mut world, &mut actor, 5, "found");
    let fourth = ended(&actor, "u-4");
    assert_eq!(finished(&actor), the("u-4", "found", fourth));

    // A turn of the user's that failed finished nothing, whatever it said before; and what
    // was finished before it is what he went on from.
    world.send(&mut actor, "u-5", "go on", Admission::Queue);
    world.says(6, r#"{"reply": "half an answer"}"#);
    world.says(6, r#"{"fail": "it broke"}"#);
    world.exits(6, Some(1), None);
    world.hint(&mut actor);
    assert_eq!(actor.end_of("u-5").unwrap().unwrap().status, "failed");
    assert_eq!(finished(&actor), None);
    // Nor did one that was stopped, though what it had said by then is what closed it.
    world.send(&mut actor, "u-5b", "go on then", Admission::Queue);
    world.says(7, r#"{"reply": "as far as I got"}"#);
    world.hint(&mut actor);
    let now = world.tick();
    let (_, out) = actor.stop("peter", now).unwrap();
    world.carry_out(&mut actor, out);
    world.exits(7, None, Some(15));
    world.hint(&mut actor);
    let stopped = actor.end_of("u-5b").unwrap().unwrap();
    assert_eq!(stopped.status, "interrupted");
    assert_ne!(stopped.closes, "u-5b");
    assert_eq!(finished(&actor), None);
    let now = world.tick();
    let (_, out) = actor.resume("peter", now).unwrap();
    world.carry_out(&mut actor, out);
    // Nor did one that ran to its end and said nothing.
    world.send(&mut actor, "u-6", "say nothing", Admission::Queue);
    answers(&mut world, &mut actor, 8, r#"{"activity": "thinking"}"#);
    assert_eq!(actor.end_of("u-6").unwrap().unwrap().status, "completed");
    assert_eq!(finished(&actor), None);

    // It is read from the log: the same after the Host was started again.
    world.send(&mut actor, "u-7", "and now?", Admission::Queue);
    answers(&mut world, &mut actor, 9, "the last answer");
    let seventh = ended(&actor, "u-7");
    assert_eq!(finished(&actor), the("u-7", "the last answer", seventh));
    drop(actor);
    let mut actor = world.open();
    assert_eq!(finished(&actor), the("u-7", "the last answer", seventh));

    // An agent that is started again has finished nothing in the conversation that begins:
    // not when another agent writes to it, and then what it answers the user there.
    let now = world.tick();
    let (started, out) = actor.start_again(now).unwrap();
    assert!(started);
    world.carry_out(&mut actor, out);
    assert_eq!(finished(&actor), None);
    send_from(
        &mut world,
        &mut actor,
        an_agent(),
        "u-8",
        "from an agent again",
    );
    answers(&mut world, &mut actor, 10, "for the agent again");
    assert_eq!(finished(&actor), None);
    world.send(&mut actor, "u-9", "anew", Admission::Queue);
    answers(
        &mut world,
        &mut actor,
        11,
        "the first answer of the new one",
    );
    let ninth = ended(&actor, "u-9");
    assert_eq!(
        finished(&actor),
        the("u-9", "the first answer of the new one", ninth)
    );
}

#[test]
fn provider_visits_commit_one_cursor_with_job_records_and_refuse_replayed_reads() {
    use clarp_kernel::jobs::JobStatus;
    use clarp_kernel::provider_tasks::{self as rules, Attempt, Cursor, FinalRead, Visit};
    let mut world = World::new("native-task");
    use clarp_ports::log::{AgentLog, Item, Kinds, LogError, NewItem, Seq};
    struct FailFirstNativeWrite(Box<dyn AgentLog>, bool);
    impl AgentLog for FailFirstNativeWrite {
        fn append(&mut self, items: &[NewItem]) -> Result<Seq, LogError> {
            if self.1 && items.iter().any(|i| i.kind == kind::PROVIDER_JOBS) {
                self.1 = false;
                return Err(LogError::Storage("fixture refused this append".into()));
            }
            self.0.append(items)
        }
        fn last_seq(&self) -> Result<Seq, LogError> {
            self.0.last_seq()
        }
        fn read_after(&self, n: Seq, k: Kinds, l: usize) -> Result<Vec<Item>, LogError> {
            self.0.read_after(n, k, l)
        }
        fn read_before(&self, n: Seq, k: Kinds, l: usize) -> Result<Vec<Item>, LogError> {
            self.0.read_before(n, k, l)
        }
        fn find(&self, key: &str) -> Result<Option<Item>, LogError> {
            self.0.find(key)
        }
    }
    let log = world
        .engine
        .open(&world.profile.dir, standard_set("zstd").unwrap())
        .unwrap();
    let (mut actor, _) = Actor::open(
        world.profile.clone(),
        world.backend.clone(),
        Box::new(FailFirstNativeWrite(log, true)),
        world.now_ms,
    )
    .unwrap();
    let id = rules::job_id(&world.profile.agent_id, "fixture", "tool");
    let owner = Attempt {
        turn: 1,
        attempt: 2,
    };
    let mut visit=Visit{native:"fixture".into(),bound_at:90,owner:Some(owner),expected:0,cursor:Cursor{device:1,inode:2,offset:256,..Default::default()},
        final_read:None,actions:vec![serde_json::from_value(json!({"tool":"tool","at":100,"state":"launching","title":"Read","project":"fixture"})).unwrap(),
        serde_json::from_value(json!({"tool":"tool","at":101,"state":"running","task":"child"})).unwrap()],eof:true};
    let revision = std::cell::Cell::new(500);
    let next = || {
        revision.set(revision.get() + 1);
        revision.get()
    };
    // When the one append fails, neither the advancing cursor nor any
    // public job or timeline entry escapes. The same batch can be retried.
    assert!(
        actor
            .apply_provider_visit(visit.clone(), 101, next)
            .is_err()
    );
    assert!(actor.jobs().get(&id).is_none());
    assert!(actor.provider_session("fixture").is_none());
    assert!(actor.job_timeline(&id, 10).unwrap().is_empty());
    actor
        .apply_provider_visit(visit.clone(), 101, next)
        .unwrap();
    assert_eq!(actor.jobs().get(&id).unwrap().status, JobStatus::Running);
    assert_eq!(actor.job_timeline(&id, 10).unwrap().len(), 2);
    // A stale read cannot advance the file position or retire this attempt.
    visit.final_read = Some(FinalRead {
        attempt: Some(owner),
        successor: None,
        exited_at: 200,
        device: 1,
        inode: 2,
        end: 900,
    });
    visit.cursor.offset = 500;
    visit.actions.clear();
    visit.eof = false;
    actor
        .apply_provider_visit(visit.clone(), 200, next)
        .unwrap();
    assert_eq!(
        actor.provider_session("fixture").unwrap().cursor.offset,
        256
    );
    visit.expected = 1;
    actor
        .apply_provider_visit(visit.clone(), 200, next)
        .unwrap();
    drop(actor);
    let mut actor = world.open();
    let saved = actor.provider_session("fixture").unwrap();
    assert_eq!(saved.final_read, visit.final_read);
    assert_eq!(saved.links[&id].attempt, owner);
    assert_eq!(actor.jobs().get(&id).unwrap().heartbeat_at_ms, None);
    visit.expected = saved.revision;
    visit.cursor.offset = 900;
    visit.eof = true;
    actor
        .apply_provider_visit(visit.clone(), 202, next)
        .unwrap();
    assert_eq!(actor.jobs().get(&id).unwrap().outcome_state(), "unknown");
    assert_eq!(
        actor.jobs().get(&id).unwrap().metadata["stop_basis"],
        "turn_exit"
    );
    assert_eq!(
        actor.jobs().get(&id).unwrap().metadata["turn_exited_at"],
        200
    );
    let mut actor = world.open();
    let saved = actor.provider_session("fixture").unwrap();
    assert_eq!(saved.final_read, None);
    visit.expected = saved.revision;
    visit.owner = None;
    visit.final_read = None;
    visit.cursor.offset += 100;
    visit.actions.push(serde_json::from_value(json!({"tool":"tool","at":180,"state":"completed","task":"child","require_receipt":true})).unwrap());
    actor.apply_provider_visit(visit, 205, next).unwrap();
    assert_eq!(actor.jobs().get(&id).unwrap().status, JobStatus::Succeeded);
    assert_eq!(actor.job_timeline(&id, 10).unwrap().len(), 4);
    let log = world
        .engine
        .open(&world.profile.dir, standard_set("zstd").unwrap())
        .unwrap();
    let entries = log
        .read_after(0, clarp_ports::log::Kinds::only(kind::PROVIDER_JOBS), 10)
        .unwrap();
    assert_eq!(entries.len(), 4);
    for item in entries {
        let kept: serde_json::Value = serde_json::from_slice(&item.body).unwrap();
        assert!(kept.get("actions").is_none());
        assert!(kept["session"]["cursor"].is_object() && kept["records"].is_array());
    }
    // The final byte boundary is durable while more than 64 tasks await
    // reconciliation. The successor cannot inherit any of those tasks.
    let archive = "crowded";
    let successor = Attempt {
        turn: 2,
        attempt: 1,
    };
    actor
        .provider_boundary(archive, Some(owner), (0, 0, 0), 300)
        .unwrap();
    let drain = |actor: &mut Actor, at| {
        let mut v = actor
            .provider_session(archive)
            .unwrap()
            .visit(archive.into());
        v.eof = true;
        actor.apply_provider_visit(v, at, next).unwrap();
    };
    drain(&mut actor, 300);
    for group in [0..64, 64..65] {
        let mut v = actor
            .provider_session(archive)
            .unwrap()
            .visit(archive.into());
        for n in group {
            v.actions.push(serde_json::from_value(json!({"tool":format!("tool-{n}"),"state":"launching","at":301,"title":"Fixture"})).unwrap());
        }
        v.cursor.offset += 1;
        v.cursor.device = 1;
        v.cursor.inode = 3;
        v.eof = true;
        actor.apply_provider_visit(v, 301, next).unwrap();
    }
    actor
        .provider_boundary(archive, Some(successor), (1, 3, 2), 400)
        .unwrap();
    drain(&mut actor, 400);
    assert_eq!(
        actor
            .jobs()
            .active()
            .filter(|j| j.metadata["native_session_id"] == archive)
            .count(),
        1
    );
    drop(actor);
    let mut actor = world.open();
    assert_eq!(actor.provider_session(archive).unwrap().owner, Some(owner));
    // Even a long outage cannot let the independent retention sweep
    // forget the one launch link whose final write is still outstanding.
    assert!(
        !actor
            .expire_provider_links(archive, 401 + 7 * 86_400_000)
            .unwrap()
    );
    drain(&mut actor, 401);
    assert_eq!(
        actor.provider_session(archive).unwrap().owner,
        Some(successor)
    );
    assert!(
        actor
            .provider_session(archive)
            .unwrap()
            .final_read
            .is_none()
    );
    assert_eq!(
        actor
            .jobs()
            .active()
            .filter(|j| j.metadata["native_session_id"] == archive)
            .count(),
        0
    );
    // The original exit time survives recovery. Its links are still
    // actionable at the retention boundary and removed one millisecond later.
    let week = 7 * 86_400_000;
    assert!(!actor.expire_provider_links(archive, 400 + week).unwrap());
    assert_eq!(actor.provider_session(archive).unwrap().links.len(), 65);
    assert!(actor.expire_provider_links(archive, 401 + week).unwrap());
    assert!(actor.provider_session(archive).unwrap().links.is_empty());
    // Equal native identities are displayed under the earliest registration,
    // including when an older ordinary job acquires its link later.
    let provenance =
        json!({"provider":"claude","native_session_id":"fixture","tool_use_id":"tool"});
    let register = |actor: &mut Actor, name: &str, metadata: serde_json::Value| {
        let command = clarp_kernel::jobs::JobCommand::Register {
            id: name.into(),
            kind: "fixture".into(),
            title: "Explicit".into(),
            queued: false,
            detail: String::new(),
            worker: clarp_kernel::jobs::Worker::none(),
            restart_cancelled: false,
            started_trace_id: String::new(),
            heartbeat_timeout_ms: None,
            metadata,
        };
        let (receipt, _) = actor.job(command, 500, next).unwrap();
        receipt.unwrap();
    };
    let replacement = |actor: &Actor| {
        actor
            .jobs()
            .provider_replacement(actor.jobs().get(&id).unwrap())
            .map(str::to_owned)
    };
    register(&mut actor, "z-first", json!({}));
    register(&mut actor, "a-second", provenance.clone());
    assert_eq!(replacement(&actor).as_deref(), Some("a-second"));
    register(&mut actor, "z-first", provenance.clone());
    assert_eq!(replacement(&actor).as_deref(), Some("z-first"));
    let mut actor = world.open();
    assert_eq!(replacement(&actor).as_deref(), Some("z-first"));
    let last = actor.job_timeline(&id, 1).unwrap();
    assert_eq!(
        last[0].note,
        "Display projection changed; native task lifecycle unchanged"
    );
    assert_eq!(
        last[0].job.revision,
        actor.jobs().get(&id).unwrap().revision
    );
    register(&mut actor, "z-first", json!({}));
    assert_eq!(replacement(&actor).as_deref(), Some("a-second"));
    register(&mut actor, "a-second", json!({}));
    assert_eq!(replacement(&actor), None);
}

#[test]
fn incoming_goal_events_keep_disjoint_ids_and_reject_a_collision_atomically() {
    use clarp_actor::Kept;
    use clarp_kernel::goals::{GoalBook, Owner, ledger::Event};
    let mut w = World::new("ledger-import");
    use clarp_ports::log::{AgentLog, Item, Kinds, LogError, NewItem, Seq};
    use std::sync::atomic::AtomicBool;
    struct Cut {
        inner: Box<dyn AgentLog>,
        fail: Arc<AtomicBool>,
    }
    impl AgentLog for Cut {
        fn append(&mut self, items: &[NewItem]) -> Result<Seq, LogError> {
            if self.fail.swap(false, Ordering::SeqCst) {
                return Err(LogError::Storage("cut before append".into()));
            }
            self.inner.append(items)
        }
        fn last_seq(&self) -> Result<Seq, LogError> {
            self.inner.last_seq()
        }
        fn find(&self, key: &str) -> Result<Option<Item>, LogError> {
            self.inner.find(key)
        }
        fn read_before(&self, b: Seq, k: Kinds, n: usize) -> Result<Vec<Item>, LogError> {
            self.inner.read_before(b, k, n)
        }
        fn read_after(&self, a: Seq, k: Kinds, n: usize) -> Result<Vec<Item>, LogError> {
            self.inner.read_after(a, k, n)
        }
    }
    let fail = Arc::new(AtomicBool::new(false));
    let log = Cut {
        inner: w
            .engine
            .open(&w.profile.dir, standard_set("zstd").unwrap())
            .unwrap(),
        fail: fail.clone(),
    };
    let (mut actor, _) = Actor::open(
        w.profile.clone(),
        w.backend.clone(),
        Box::new(log),
        w.now_ms,
    )
    .unwrap();
    let make = |name: &str| {
        let row = json!({"plan_id":name,"agent_id":"a1","status":"active","goal":{"history":[]}});
        GoalBook::new()
            .taken_over(
                Owner {
                    agent_id: "a1",
                    session: "fixture",
                },
                &row,
                &[],
            )
            .unwrap()
    };
    let event = |id: i64, name: &str| {
        serde_json::from_value::<Event>(json!({
        "event_id":id,"plan_id":name,"at":100,"kind":"checkpoint","subject":"goal","actor_kind":"owner","actor_agent_id":"a1","delegation_id":"","basis":"","source_refs":["goal_event:3"],"reason":"","prior":null,"new":{},"plan_revision":0
    })).unwrap()
    };
    let first = make("a1:first");
    let old = vec![event(3, "a1:first"), event(40, "a1:first")];
    fail.store(true, Ordering::SeqCst);
    let refused = actor.keep(
        &Kept {
            goal: Some(&first),
            imported_events: Some(&old),
            applied_cursor: Some(("apply", 100)),
            ..Default::default()
        },
        199,
    );
    assert!(refused.is_err());
    assert_eq!(actor.next_goal_event_id().unwrap(), 1);
    assert!(actor.goals().plan("a1:first").is_none());
    let disk = w
        .engine
        .open(&w.profile.dir, standard_set("zstd").unwrap())
        .unwrap();
    for key in [
        "ledger/id/3",
        "ledger/id/40",
        "ledger/tail/a1:first",
        "ledger/applied/apply",
        "g-a1:first",
    ] {
        assert!(disk.find(key).unwrap().is_none());
    }
    actor
        .keep(
            &Kept {
                goal: Some(&first),
                imported_events: Some(&old),
                applied_cursor: Some(("apply", 100)),
                ..Default::default()
            },
            200,
        )
        .unwrap();
    assert_eq!(
        serde_json::from_slice::<i64>(&disk.find("ledger/applied/apply").unwrap().unwrap().body)
            .unwrap(),
        40
    );
    assert_eq!(actor.next_goal_event_id().unwrap(), 41);
    // A failed explicit subgoal edit cannot consume an event ID, change its
    // summary, or appear in either the log or the book.
    let (proposed, subgoal_event) = actor
        .goals()
        .owner_subgoal(
            "a1:first",
            "add",
            &json!({"subgoal_id":"child","title":"Child"}),
            None,
            201,
        )
        .unwrap();
    let before = actor.goals().get("a1:first", 201).unwrap();
    let seq = disk.last_seq().unwrap();
    fail.store(true, Ordering::SeqCst);
    assert!(
        actor
            .keep(
                &Kept {
                    goal: Some(&proposed),
                    subgoal_event: Some(&subgoal_event),
                    ..Default::default()
                },
                201
            )
            .is_err()
    );
    assert_eq!(disk.last_seq().unwrap(), seq);
    assert_eq!(actor.goals().get("a1:first", 201).unwrap(), before);
    assert_eq!(actor.next_goal_event_id().unwrap(), 41);
    let second = make("a1:second");
    let mut enters = vec![event(7, "a1:second"), event(40, "a1:second")];
    let open_log = || {
        w.engine
            .open(&w.profile.dir, standard_set("zstd").unwrap())
            .unwrap()
    };
    let last = open_log().last_seq().unwrap();
    let err = actor
        .keep(
            &Kept {
                goal: Some(&second),
                imported_events: Some(&enters),
                entry_source: Some("source"),
                ..Default::default()
            },
            300,
        )
        .unwrap_err();
    assert!(err.to_string().contains("already belongs"));
    assert_eq!(open_log().last_seq().unwrap(), last);
    assert!(actor.goals().plan("a1:second").is_none());
    assert!(open_log().find("ledger/id/7").unwrap().is_none());
    enters[1].event_id = 19;
    actor
        .keep(
            &Kept {
                goal: Some(&second),
                imported_events: Some(&enters),
                entry_source: Some("source"),
                ..Default::default()
            },
            400,
        )
        .unwrap();
    assert_eq!(actor.next_goal_event_id().unwrap(), 42);
    let summary = actor.goals().get("a1:second", 400).unwrap();
    assert_eq!(summary["ledger"]["event_count"], 3);
    assert_eq!(summary["ledger"]["last_event_at"], 400);
    let empty = make("a1:empty");
    actor
        .keep(
            &Kept {
                goal: Some(&empty),
                imported_events: Some(&[]),
                ..Default::default()
            },
            450,
        )
        .unwrap();
    let row = |key: &str| {
        serde_json::from_slice::<serde_json::Value>(&open_log().find(key).unwrap().unwrap().body)
            .unwrap()
    };
    assert_eq!(row("ledger/id/41"), json!(["a1:second", 3]));
    assert_eq!(row("ledger/event/a1:second/2"), json!(enters[1]));
    let latest = row("ledger/event/a1:second/3");
    assert_eq!(latest["event_id"], 41);
    assert_eq!(latest["kind"], "plan_imported");
    assert_eq!(
        latest["new"],
        json!({"from":"source","plan_id":"a1:second","first_id":7,"last_id":19})
    );
    let (after, more) = actor.goal_events("a1:second", 40, 20).unwrap();
    assert_eq!(after.len(), 1);
    assert!(!more);
    assert_eq!(json!(after[0]), latest);
    drop(actor);
    let actor = w.open();
    assert!(actor.goal_events("a1:empty", 0, 20).unwrap().0.is_empty());
    assert_eq!(actor.next_goal_event_id().unwrap(), 42);
}

#[test]
fn a_legacy_goal_gets_only_its_surviving_history_and_one_status_baseline() {
    use clarp_kernel::goals::{GoalBook, Owner};
    use clarp_ports::log::NewItem;
    let mut w = World::new("legacy-ledger");
    let book = GoalBook::new();
    let plan = json!({"agent_id":"a1","plan_id":"a1:legacy","status":"paused","revision":7,"updated_at":90,
        "ledger":{"event_count":301,"last_event_at":82},"goal":{"history_offset":300,"history":[
            {"kind":"step","at":82,"revision":7,"detail":{"item_id":"s","reason":"ready"}}]}});
    let change = book
        .taken_over(
            Owner {
                agent_id: "a1",
                session: "fixture",
            },
            &plan,
            &[],
        )
        .unwrap();
    let mut log = w
        .engine
        .open(&w.profile.dir, standard_set("none").unwrap())
        .unwrap();
    let stored = serde_json::to_vec(&book.record(&change, 90)).unwrap();
    log.append(&[NewItem::new(kind::GOAL, 90, stored).keyed("g-a1:legacy")])
        .unwrap();
    for _ in 0..2 {
        let actor = w.open();
        let (events, more) = actor.goal_events("a1:legacy", 0, 500).unwrap();
        assert!(!more);
        assert_eq!(events.len(), 2);
        assert_eq!(events[0].actor_kind, "legacy");
        assert_eq!(events[0].subject, "step:s");
        assert_eq!(
            events[0].new,
            json!({"detail":{"item_id":"s","reason":"ready"}})
        );
        assert_eq!(events[1].kind, "ledger_started");
        assert_eq!(events[1].new, json!({"status":"paused"}));
        assert_eq!(actor.next_goal_event_id().unwrap(), 3);
        let plan = actor.goals().get("a1:legacy", 100).unwrap();
        assert_eq!(plan["ledger"]["event_count"], 2);
        assert_eq!(plan["ledger"]["last_event_at"], 90);
        assert_eq!(plan["goal"]["history_offset"], 300);
    }
}
