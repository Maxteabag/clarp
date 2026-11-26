//! One process keeps an agent's `opencode serve` between its chat turns (issue 763): the
//! OpenCode driver of the lane layer. The server listens on loopback behind a password this
//! process makes and never hands out; each claim the Host gives runs here, over HTTP, and its
//! record goes to the claim's spool in the lines today's per-turn driver prints. What every lane
//! keeps for itself (its options, locks, turn file, idle clock, login and sockets) is in the
//! `lane_*` modules.
//!
//! A turn takes the user's steered words while it runs (issue 784): they are posted to the busy
//! session as a message the lane names, which OpenCode reads at its next step and answers in the
//! same busy period. They are read when an answer that names that message starts. Words still
//! unread when the turn ends, by its idle or a Stop, are taken out of the session again: the
//! Host runs them as a turn of their own.

use std::collections::{BTreeMap, BTreeSet};
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::Stdio;
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, SyncSender};
use std::time::{Duration, Instant};

use base64::{Engine as _, engine::general_purpose::STANDARD};
use clarp_shim::Reply;
use clarp_shim::lane::{Claim, LOGIN_CHANGED, thread_lock};
use clarp_shim::lane_spool::ClaimSpool;
use clarp_shim::opencode_lane::{Request, Snapshot, spoken};
use clarp_tool_args::opencode::compaction;
use nix::sys::signal::{Signal, killpg};
use nix::unistd::Pid;
use serde_json::{Value, json};

use crate::lane_login::Login;
use crate::lane_own::{Idle, Own, hold_owner, lock_exclusive};
use crate::lane_socket::{Socket, TurnAsk, write};
use crate::opencode_follow::{Follow, body};
use crate::opencode_http::{Client, port};

/// How long OpenCode has to end a turn after its abort when the Stop named no time.
const STOP_GRACE: Duration = Duration::from_secs(5);
/// How long a server asked to end may take before its group is killed.
const END_GRACE: Duration = Duration::from_secs(5);
/// How long a turn held open at its idle only because OpenCode's answers took its unread steered
/// words in by message order waits for the run that order says is coming (issue 864). With no
/// busy event by then, the order was wrong (an id from a clock that stepped back), and the turn
/// ends as that idle would have.
const RECHECK: Duration = Duration::from_secs(5);
/// What a claim's turn ends with when the server went away under it: today's driver's words.
const SERVER_GONE: &str = "OpenCode server stopped before the turn finished";

pub(super) enum Input {
    /// One event of the server's stream.
    Event(Value),
    /// The server's event stream ended.
    EventsGone,
    /// The server's process exited.
    ServeGone,
    Control(Request, UnixStream),
    Follow((u64, u32), UnixStream),
    /// A Stop on the attempt's socket, with how long OpenCode has to end the turn itself.
    Stop((u64, u32), u64, UnixStream),
    /// Steered words for the attempt, under the id their reading is told by.
    Steer((u64, u32), String, String, UnixStream),
    /// Lines for a tool's input, which this lane never takes.
    Refuse(UnixStream),
}

impl Input {
    fn control(request: Request, stream: UnixStream) -> Self {
        Self::Control(request, stream)
    }

    fn turn(attempt: (u64, u32), ask: TurnAsk, stream: UnixStream) -> Self {
        match ask {
            TurnAsk::Follow => Self::Follow(attempt, stream),
            TurnAsk::Stop(grace_ms) => Self::Stop(attempt, grace_ms, stream),
            TurnAsk::Steer(id, text) => Self::Steer(attempt, id, text, stream),
            TurnAsk::Stdin(_) => Self::Refuse(stream),
        }
    }
}

/// OpenCode's login file names no account: any change of it is a changed login (the lead's
/// rule of 24 Nov 2026, a content hash of `auth.json`).
fn account(_: &Value) -> Option<&str> {
    None
}

pub(super) fn from_args(mut args: impl Iterator<Item = String>) -> io::Result<()> {
    let (Some(socket), Some(program), Some(options), None) =
        (args.next(), args.next(), args.next(), args.next())
    else {
        return Err(io::Error::other(
            "usage: opencode-lane SOCKET OPENCODE OPTIONS_FILE",
        ));
    };
    let options = serde_json::from_reader(fs::File::open(options)?).map_err(io::Error::other)?;
    run(Path::new(&socket), &program, options)
}

fn run(path: &Path, program: &str, mut options: Value) -> io::Result<()> {
    let _ = nix::unistd::setsid();
    let own = Own::take(&mut options);
    let lock = hold_owner(path)?;
    match fs::remove_file(path) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let login = Login::read(&mut options, account)?;
    let directory = options["cwd"].as_str().map(str::to_owned).map_or_else(
        || std::env::current_dir().map(|d| d.display().to_string()),
        Ok,
    )?;
    let (send, input) = mpsc::sync_channel(256);
    let serve = Serve::start(program, directory, send.clone())?;
    let _listener = Socket::control(path, send.clone(), Input::control)?;
    let idle = Idle::new(own.idle);
    let mut lane = Lane {
        serve,
        own,
        login,
        idle,
        thread: String::new(),
        _thread_lock: None,
        session_busy: false,
        turn: None,
        turn_socket: None,
        followers: Vec::new(),
        stop_deadline: None,
        recheck: None,
        stale_login: false,
        model: None,
        effort: None,
        memory: None,
        ids: MessageIds::default(),
        input,
        send,
        retire_reply: None,
    };
    lane.weigh();
    // Ready with no session: a turn that runs on its own serve meanwhile keeps the session
    // until its process is gone (the lead's rule: no turn waits for a cold lane).
    println!("ready");
    io::stdout().flush()?;
    let result = lane.serve();
    // An idle lane that took a retirement leaves for good from here, whatever follows: it says
    // so at once, so the Host that asked counts its place now rather than after the server's
    // end (up to END_GRACE, issue 908). It answers nothing else from here, and still lets go
    // of its locks only once the server has been reaped.
    if let Some(mut stream) = lane.retire_reply.take() {
        let _ = write(&mut stream, &json!({"ok": true, "state": lane.inspect()}));
    }
    lane.end(Some(1), None, Some(SERVER_GONE));
    // The server is ended and reaped before the session's lock and the owner lock are let go.
    lane.serve.end();
    drop(lane);
    drop(lock);
    result
}

/// How long a server that ended before it said where it listens is given before it is started
/// once more.
const START_AGAIN_AFTER: Duration = Duration::from_millis(500);
/// How long a server whose output closed before it said where it listens is given to have ended:
/// one that ended is started once more, one still there after saying nothing for the whole wait
/// is not.
const ENDED_LOOK: Duration = Duration::from_millis(250);
/// The most of what a server writes to its error output that is kept, from its end.
const SAID_KEPT: usize = 4096;

/// A server that ended, or did not say, before it said where it listens; with the last line it
/// wrote to its error output, if any, and whether it had ended by itself.
#[derive(Debug)]
struct Unheard {
    said: String,
    ended: bool,
}

impl std::fmt::Display for Unheard {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        if self.said.is_empty() {
            write!(f, "OpenCode did not say where it listens")
        } else {
            write!(f, "OpenCode did not say where it listens: {}", self.said)
        }
    }
}

impl std::error::Error for Unheard {}

/// A server's error output, read to its end on a thread of its own so the server never blocks on
/// it, the last of it kept.
struct Said(Receiver<Vec<u8>>);

impl Said {
    fn read(mut from: impl Read + Send + 'static) -> Self {
        let (send, kept) = mpsc::sync_channel(1);
        let _ = std::thread::Builder::new()
            .name("opencode-lane-said".into())
            .spawn(move || {
                let mut tail = Vec::new();
                let mut chunk = [0u8; 4096];
                while let Ok(n) = from.read(&mut chunk) {
                    if n == 0 {
                        break;
                    }
                    tail.extend_from_slice(&chunk[..n]);
                    if tail.len() > SAID_KEPT {
                        tail.drain(..tail.len() - SAID_KEPT);
                    }
                }
                let _ = send.send(tail);
            });
        Self(kept)
    }

    /// The last line it wrote with words in it, without terminal colours, once it has ended
    /// (waited for a second at most). Empty where there was none.
    fn last(self) -> String {
        let tail = self
            .0
            .recv_timeout(Duration::from_secs(1))
            .unwrap_or_default();
        last_line(&String::from_utf8_lossy(&tail))
    }
}

/// The last line of `text` with words in it, its terminal escapes left out and cut to 300
/// characters.
fn last_line(text: &str) -> String {
    let mut plain = String::with_capacity(text.len());
    let mut chars = text.chars();
    while let Some(c) = chars.next() {
        if c == '\u{1b}' {
            // An escape sequence: up to and with its final letter.
            for c in chars.by_ref() {
                if c.is_ascii_alphabetic() {
                    break;
                }
            }
        } else {
            plain.push(c);
        }
    }
    plain
        .lines()
        .map(str::trim)
        .rfind(|line| line.chars().any(char::is_alphanumeric))
        .map(|line| line.chars().take(300).collect())
        .unwrap_or_default()
}

/// The kept server: its process group, its client, and what it said it is.
struct Serve {
    pid: u32,
    client: Client,
    version: String,
    /// Says once the process has exited and been reaped.
    gone: Receiver<()>,
    ended: bool,
}

impl Serve {
    /// Starts the server in a group of its own, through the shim's `tool-child`, so a lane that
    /// is killed takes its server with it. Warms the agent's directory, opens the event stream,
    /// and refuses a server whose session API Clarp does not read.
    ///
    /// A server that ends before it says where it listens is started once more, a moment later:
    /// two servers opening OpenCode's database at the same moment (the lane's beside its agent's
    /// first turn, which starts a server of its own) can find it locked, and one of them ends
    /// at once (issue 908). What it said on its way out is in the error. One that said nothing
    /// for the whole wait, and was still there, is not started again.
    fn start(program: &str, directory: String, send: SyncSender<Input>) -> io::Result<Self> {
        let ended = |error: &io::Error| {
            error
                .get_ref()
                .and_then(|inner| inner.downcast_ref::<Unheard>())
                .is_some_and(|unheard| unheard.ended)
        };
        match Self::start_once(program, directory.clone(), send.clone()) {
            Err(error) if ended(&error) => {
                eprintln!("clarp-shim: OpenCode lane: {error}; started once more");
                std::thread::sleep(START_AGAIN_AFTER);
                Self::start_once(program, directory, send)
            }
            started => started,
        }
    }

    fn start_once(program: &str, directory: String, send: SyncSender<Input>) -> io::Result<Self> {
        let mut random = [0u8; 24];
        File::open("/dev/urandom")?.read_exact(&mut random)?;
        let password = STANDARD.encode(random);
        let serve = ["serve", "--hostname", "127.0.0.1", "--port", "0"];
        let mut child = crate::tool_child::command(program)
            .args(serve)
            .env("OPENCODE_SERVER_PASSWORD", &password)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0)
            .spawn()?;
        let stdout = child.stdout.take();
        let said = child.stderr.take().map(Said::read);
        let pid = child.id();
        let (reaped, gone) = mpsc::channel();
        let told = send.clone();
        // The lane hears of a server's end only once that server said where it listens: one that
        // ended before is this start's to answer for, and its end must not reach the lane that a
        // server started once more serves (issue 908).
        let heard = Arc::new(AtomicBool::new(false));
        let listened = heard.clone();
        std::thread::Builder::new()
            .name("opencode-lane-serve".into())
            .spawn(move || {
                let _ = child.wait();
                let _ = reaped.send(());
                if listened.load(Ordering::Acquire) {
                    let _ = told.send(Input::ServeGone);
                }
            })?;
        let mut serve = Self {
            pid,
            client: Client::new(0, "", String::new()),
            version: String::new(),
            gone,
            ended: false,
        };
        let Some(port) = stdout.and_then(port) else {
            let ended = serve.gone.recv_timeout(ENDED_LOOK).is_ok();
            serve.end();
            let said = said.map(Said::last).unwrap_or_default();
            return Err(io::Error::other(Unheard { said, ended }));
        };
        heard.store(true, Ordering::Release);
        serve.client = Client::new(port, &password, directory);
        let ready = (|| {
            let health = serve.client.get("/global/health", 30)?;
            serve.version = health["version"].as_str().unwrap_or_default().to_owned();
            if !spoken(&serve.version) {
                return Err(format!(
                    "OpenCode {} cannot read Clarp's sessions: the lane needs the 1.x line",
                    serve.version
                )
                .into());
            }
            // The directory's instance (configuration, plugins, MCP servers) is made by the
            // first request that names it: here, not in the first turn.
            serve.client.get("/session", 30)?;
            serve.client.subscribe()
        })();
        let events = match ready {
            Ok(events) => events,
            Err(error) => {
                serve.end();
                return Err(io::Error::other(error.to_string()));
            }
        };
        std::thread::Builder::new()
            .name("opencode-lane-events".into())
            .spawn(move || {
                for event in events {
                    if send.send(Input::Event(event)).is_err() {
                        return;
                    }
                }
                let _ = send.send(Input::EventsGone);
            })?;
        Ok(serve)
    }

    fn group(&self) -> Pid {
        Pid::from_raw(i32::try_from(self.pid).unwrap_or(i32::MAX))
    }

    /// Ends the server and all it started: asked, then killed, and waited for.
    fn end(&mut self) {
        if std::mem::replace(&mut self.ended, true) {
            return;
        }
        let _ = killpg(self.group(), Signal::SIGTERM);
        if matches!(
            self.gone.recv_timeout(END_GRACE),
            Err(RecvTimeoutError::Timeout)
        ) {
            let _ = killpg(self.group(), Signal::SIGKILL);
            let _ = self.gone.recv();
        }
        // What the server started and left in its group (an MCP server) goes with it.
        let _ = killpg(self.group(), Signal::SIGKILL);
    }

    /// The server did not end a turn after its abort: its group is ended where it stands.
    fn kill(&mut self) {
        self.ended = true;
        let _ = killpg(self.group(), Signal::SIGKILL);
        let _ = self.gone.recv_timeout(END_GRACE);
    }
}

impl Drop for Serve {
    fn drop(&mut self) {
        self.end();
    }
}

/// The turn a claim runs: its spool and what its events mean.
struct Turn {
    claim: Claim,
    spool: ClaimSpool,
    follow: Follow,
    /// A Stop asked for its end: the idle that follows ends it as stopped.
    stopping: bool,
    /// The steered words posted to the session, in the order they were posted: the message the
    /// lane named them, then the id the Host steered them under.
    steered: Vec<(String, String)>,
    /// Which messages OpenCode stored before which, in its own store order ([`StoreOrder`]).
    order: StoreOrder,
}

/// What the lane saw of the order OpenCode stored the turn's messages in, by the order of the
/// events it told them with, never by an id or a clock (issue 878). OpenCode tells each message
/// as it stores it (`session/session.ts` 629-633, `updateMessage` then its `message.updated`),
/// and the lane reads one stream of them, so the order of those events is its store order. A
/// steer's own post says nothing of when it was stored: `prompt_async` answers before the message
/// is stored (`server/routes/instance/httpapi/handlers/session.ts` 311-329, the prompt forked;
/// `session/prompt.ts` 635, `createUserMessage`, stores it later).
#[derive(Default, Clone)]
struct StoreOrder {
    /// The turn's own steers OpenCode has told as stored.
    stored: BTreeSet<String>,
    /// The last assistant message OpenCode told was its compaction's summary ([`compaction`]).
    after_summary: bool,
    /// OpenCode's own messages to go on after a compaction (`session/compaction.ts`: the user
    /// message it stores after its summary), each with the turn's steers it had told as stored
    /// before it.
    go_on: BTreeMap<String, BTreeSet<String>>,
}

impl StoreOrder {
    /// A `message.updated` of the turn's session.
    fn told(&mut self, info: &Value, steered: &[(String, String)]) {
        match (info["role"].as_str(), info["id"].as_str()) {
            (Some("assistant"), _) => self.after_summary = compaction(info),
            (Some("user"), Some(id)) if steered.iter().any(|(steer, _)| steer == id) => {
                self.stored.insert(id.to_owned());
            }
            (Some("user"), Some(id)) if self.after_summary => {
                self.go_on.insert(id.to_owned(), self.stored.clone());
                self.after_summary = false;
            }
            _ => {}
        }
    }
}

/// Whether an answer to the user message `parent` took the steered message `message` in.
/// OpenCode reads every user message stored since its last answer together and names the last of
/// them as the answer's parent (issue 862), so an answer takes in each message up to and including
/// its parent. The lane's message ids and OpenCode's are of one ascending form (`MessageIds`): a
/// message was stored at or before `parent` when its id sorts at or before it.
fn taken_in(message: &str, parent: &str) -> bool {
    parent.starts_with("msg_") && message <= parent
}

/// Whether the session's stored message `list` proves that OpenCode took `message` in: it holds
/// an answer whose parent is `message` or one of the turn's own steers `posted` after it, in the
/// order the lane posted them. OpenCode answers the last message it took in, and takes in every
/// message stored before it (issue 862), so such an answer is certain, whatever the ids or the
/// clock say. An answer to a message not of this turn proves nothing, but for one: OpenCode's
/// own message that tells it to go on after it compacted its context, for the steers the lane saw
/// it store before that ([`after_compaction`]).
fn proven(list: &Value, posted: &[(String, String)], order: &StoreOrder, message: &str) -> bool {
    let Some(own) = posted.iter().position(|(steered, _)| steered == message) else {
        return false;
    };
    let stored = list.as_array().map_or(&[][..], Vec::as_slice);
    let answered: Vec<&str> = stored
        .iter()
        .filter(|stored| stored["info"]["role"] == "assistant" && !compaction(&stored["info"]))
        .filter_map(|stored| stored["info"]["parentID"].as_str())
        .collect();
    posted[own..]
        .iter()
        .any(|(steered, _)| answered.contains(&steered.as_str()))
        || answered
            .iter()
            .any(|parent| after_compaction(stored, order, parent, message))
}

/// Whether the session's stored message `list` holds an answer whose parent is `message` itself:
/// OpenCode's own record that it read exactly those words. Every other proof ([`proven`],
/// [`taken_in`]) infers it from another message, which may have been stored while `message` never
/// was (a post answers before it is stored), so it is no confirmation after a Stop (issue 933).
fn answered_itself(list: &Value, message: &str) -> bool {
    list.as_array().into_iter().flatten().any(|stored| {
        stored["info"]["role"] == "assistant"
            && !compaction(&stored["info"])
            && stored["info"]["parentID"].as_str() == Some(message)
    })
}

/// Whether `parent` is OpenCode's own message that tells it to go on after it compacted its
/// context, and `message` a steer of the turn's own OpenCode stored before it: then the answer to
/// it took `message` in (issue 878). OpenCode's answer after a compaction names that message as
/// its parent, not the steers it read, and it answers from all that was stored since its
/// compaction (`session/message-v2.ts`, `filterCompacted`): a steer stored while it wrote its
/// summary too. "Stored before" is the lane's own event order ([`StoreOrder`]), never an id. The
/// stored list must agree that `parent` is that message: a user message whose last assistant
/// message before it is a compaction's summary. A steer the lane did not see stored before it
/// is not proven by it, and runs once as a turn of its own: a repeat is rare, a lost word worse.
fn after_compaction(stored: &[Value], order: &StoreOrder, parent: &str, message: &str) -> bool {
    if !order
        .go_on
        .get(parent)
        .is_some_and(|before| before.contains(message))
    {
        return false;
    }
    let Some(go_on) = stored
        .iter()
        .position(|kept| kept["info"]["id"].as_str() == Some(parent))
    else {
        return false;
    };
    stored[go_on]["info"]["role"] == "user"
        && stored[..go_on]
            .iter()
            .rev()
            .find(|kept| kept["info"]["role"] == "assistant")
            .is_some_and(|summary| compaction(&summary["info"]))
}

/// What the idle that ends a turn found of its unread steered words.
enum Settle {
    /// None is left unread or untaken back: the turn ends.
    Done,
    /// The session runs again: its events come, and the turn stays open for them.
    Runs,
    /// An answer took them in by message order alone, with no run seen yet.
    ByOrder,
}

/// Whether an answer of a session's message `list` took in any of `messages` ([`taken_in`]).
fn any_taken_in(list: &Value, messages: &[(String, String)]) -> bool {
    list.as_array().into_iter().flatten().any(|message| {
        message["info"]["role"] == "assistant"
            && !compaction(&message["info"])
            && message["info"]["parentID"].as_str().is_some_and(|parent| {
                messages
                    .iter()
                    .any(|(steered, _)| taken_in(steered, parent))
            })
    })
}

impl Turn {
    /// The steered messages whose reading is not written, with the ids the Host steered them
    /// under.
    fn unread(&self) -> Vec<(String, String)> {
        self.steered
            .iter()
            .filter(|(_, id)| !self.spool.was_read(id))
            .map(|(message, id)| (message.clone(), id.clone()))
            .collect()
    }
}

/// Names the messages the lane posts, in OpenCode's ascending form: `msg_`, twelve hex digits of
/// the time in milliseconds times 4096 plus a count within that millisecond, then fourteen
/// letters and digits at random. OpenCode orders a session's messages by their ids, so steered
/// words sort after what the turn said before them, as a message OpenCode named would.
#[derive(Default)]
struct MessageIds {
    ms: i64,
    count: u64,
}

impl MessageIds {
    const LETTERS: &'static [u8] =
        b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";

    fn next(&mut self, now_ms: i64) -> io::Result<String> {
        if now_ms != self.ms {
            self.ms = now_ms;
            self.count = 0;
        }
        self.count += 1;
        let ms = u64::try_from(now_ms).unwrap_or_default();
        let stamp = ms.wrapping_mul(0x1000).wrapping_add(self.count) & 0xFFFF_FFFF_FFFF;
        let mut random = [0u8; 14];
        File::open("/dev/urandom")?.read_exact(&mut random)?;
        let tail: String = random
            .iter()
            .map(|b| Self::LETTERS[usize::from(*b) % Self::LETTERS.len()] as char)
            .collect();
        Ok(format!("msg_{stamp:012x}{tail}"))
    }
}

struct Lane {
    /// Dropped first: the server is reaped before any lock is let go.
    serve: Serve,
    own: Own,
    login: Login,
    idle: Idle,
    /// The session the lane holds; empty until its first claim takes one.
    thread: String,
    /// Held, never read: no other process writes the session while the lane lives.
    _thread_lock: Option<File>,
    /// The session runs on the server now, by its own events.
    session_busy: bool,
    turn: Option<Turn>,
    turn_socket: Option<Socket>,
    followers: Vec<UnixStream>,
    stop_deadline: Option<(Instant, (u64, u32))>,
    /// A turn held open at its idle by message order alone is looked at again then; true once
    /// a read of the session's messages at a recheck failed.
    recheck: Option<(Instant, bool)>,
    stale_login: bool,
    model: Option<String>,
    effort: Option<String>,
    memory: Option<u64>,
    ids: MessageIds,
    input: Receiver<Input>,
    send: SyncSender<Input>,
    retire_reply: Option<UnixStream>,
}

impl Lane {
    fn inspect(&self) -> Snapshot {
        Snapshot {
            thread: self.thread.clone(),
            available: self.turn.is_none(),
            claim: self.turn.as_ref().map(|turn| turn.claim.clone()),
            options: self.own.digest.clone(),
            model: self.model.clone(),
            effort: self.effort.clone(),
            idle_ms: self.idle.ms(self.turn.is_some()),
            memory_bytes: self.memory,
            working: Some(self.turn.is_some()),
            version: self.serve.version.clone(),
            pid: self.serve.pid,
        }
    }

    /// Weighs the lane: itself, the server and its group (issue 761). When the lane is ready
    /// and when a claim has ended, after the turn's last spool line: never on a turn's path.
    fn weigh(&mut self) {
        self.memory = crate::lane_memory::held(std::process::id(), self.serve.pid);
    }

    fn hint(&mut self) {
        self.followers
            .retain_mut(|stream| match stream.write(b"\n") {
                Ok(_) => true,
                Err(error) => error.kind() == io::ErrorKind::WouldBlock,
            });
    }

    fn holds(&self, (turn, attempt): (u64, u32)) -> bool {
        self.turn
            .as_ref()
            .is_some_and(|held| (held.claim.turn, held.claim.attempt) == (turn, attempt))
    }

    fn deadline(&self) -> Option<Instant> {
        if let Some((at, _)) = self.stop_deadline {
            return Some(at);
        }
        self.recheck
            .map(|(at, _)| at)
            .into_iter()
            .chain(self.idle.deadline(self.turn.is_none()))
            .min()
    }

    /// Err: the server did not end a stopped turn in time, and the lane leaves. True: the
    /// lane's idle time is over.
    fn expired(&mut self) -> io::Result<bool> {
        if let Some((at, attempt)) = self.stop_deadline {
            if Instant::now() < at {
                return Ok(false);
            }
            self.stop_deadline = None;
            if self.holds(attempt) {
                self.serve.kill();
                self.end(None, Some(nix::libc::SIGKILL), None);
                return Err(io::Error::other(
                    "OpenCode did not end its turn after a Stop",
                ));
            }
            return Ok(false);
        }
        Ok(self.idle.over(self.turn.is_none()))
    }

    fn next_input(&mut self) -> io::Result<Option<Input>> {
        loop {
            if self
                .stop_deadline
                .is_some_and(|(at, _)| Instant::now() >= at)
                && self.expired()?
            {
                return Err(io::Error::new(io::ErrorKind::TimedOut, "idle"));
            }
            let Some(at) = self.deadline() else {
                return Ok(self.input.recv().ok());
            };
            match self
                .input
                .recv_timeout(at.saturating_duration_since(Instant::now()))
            {
                Ok(input) => return Ok(Some(input)),
                Err(RecvTimeoutError::Disconnected) => return Ok(None),
                Err(RecvTimeoutError::Timeout) => {
                    // A recheck fires only with nothing waiting: a busy event already in the
                    // channel, however late the loop is, is taken first and cancels it.
                    if let Some((at, failed)) = self.recheck
                        && Instant::now() >= at
                    {
                        self.recheck = None;
                        self.look_again(failed);
                        continue;
                    }
                    if self.expired()? {
                        return Err(io::Error::new(io::ErrorKind::TimedOut, "idle"));
                    }
                }
            }
        }
    }

    fn serve(&mut self) -> io::Result<()> {
        loop {
            let input = match self.next_input() {
                Ok(input) => input,
                Err(error) if error.kind() == io::ErrorKind::TimedOut => return Ok(()),
                Err(error) => return Err(error),
            };
            match input {
                Some(Input::Event(event)) => self.event(&event),
                Some(Input::Control(request, stream)) => {
                    if self.control(request, stream) {
                        return Ok(());
                    }
                }
                Some(Input::Follow(attempt, mut stream)) => {
                    if self.holds(attempt) && self.followers.len() < 8 {
                        let _ = write(&mut stream, &Reply::ok(None));
                        let _ = stream.set_nonblocking(true);
                        self.followers.push(stream);
                    } else {
                        let _ = write(&mut stream, &Reply::failed("no such turn on this lane"));
                    }
                }
                Some(Input::Stop(attempt, grace_ms, mut stream)) => {
                    let grace = if grace_ms == 0 {
                        STOP_GRACE
                    } else {
                        Duration::from_millis(grace_ms)
                    };
                    self.stop(attempt, grace);
                    let _ = write(&mut stream, &Reply::ok(None));
                }
                Some(Input::Steer(attempt, id, text, mut stream)) => {
                    let reply = match self.steer(attempt, &id, &text) {
                        Ok(()) => Reply::ok(None),
                        Err(error) => Reply::failed(error.to_string()),
                    };
                    let _ = write(&mut stream, &reply);
                }
                Some(Input::Refuse(mut stream)) => {
                    let _ = write(&mut stream, &Reply::failed("Unsupported lane turn request"));
                }
                Some(Input::ServeGone | Input::EventsGone) | None => {
                    return Err(io::Error::other(SERVER_GONE));
                }
            }
        }
    }

    /// Answers the Host. True: the lane leaves.
    fn control(&mut self, request: Request, mut stream: UnixStream) -> bool {
        let result = match request {
            Request::Inspect => {
                let _ = write(&mut stream, &json!({"ok": true, "state": self.inspect()}));
                return false;
            }
            Request::Retire if self.turn.is_none() => {
                self.retire_reply = Some(stream);
                return true;
            }
            Request::Retire => Err(io::Error::other("the OpenCode lane runs a turn")),
            Request::Prompt {
                claim,
                session,
                text,
                model,
                effort,
                token,
            } => self.prompt(claim, &session, &text, &model, &effort, token.as_deref()),
        };
        let reply = match result {
            Ok(()) => json!({"ok": true, "state": self.inspect()}),
            // Nothing of the attempt reached OpenCode: the Host may give it to the next lane.
            Err(error) if self.stale_login => {
                json!({"ok": false, "error": error.to_string(), "code": LOGIN_CHANGED})
            }
            Err(error) => json!({"ok": false, "error": error.to_string()}),
        };
        let _ = write(&mut stream, &reply);
        if self.turn.is_none() {
            self.login.release();
        }
        self.stale_login
    }

    fn use_login(&mut self) -> io::Result<()> {
        if !self.login.acquire()? {
            self.stale_login = true;
            return Err(io::Error::other(
                "the OpenCode login changed before this attempt",
            ));
        }
        Ok(())
    }

    /// Takes the agent's session for the lane, once: only while no other process writes it
    /// (its lock taken exclusive, which a turn on its own serve holds shared for its life),
    /// and kept from then on. An empty `session` is a new conversation: the lane makes it.
    fn take(&mut self, session: &str) -> io::Result<()> {
        if !self.thread.is_empty() {
            return if session.is_empty() || session == self.thread {
                Ok(())
            } else {
                Err(io::Error::other("the OpenCode lane holds another session"))
            };
        }
        let dir = self
            .own
            .thread_locks
            .clone()
            .ok_or_else(|| io::Error::other("the OpenCode lane was given no lock folder"))?;
        let opened = |old: &str| {
            self.serve
                .client
                .open_session(old)
                .map_err(|error| io::Error::other(error.to_string()))
        };
        let (sid, lock) = if session.is_empty() {
            let sid = opened("")?;
            let lock = lock_exclusive(&thread_lock(&dir, &sid))?;
            (sid, lock)
        } else {
            let lock = lock_exclusive(&thread_lock(&dir, session)).map_err(|_| {
                io::Error::new(
                    io::ErrorKind::WouldBlock,
                    "another process still writes this OpenCode session",
                )
            })?;
            (opened(session)?, lock)
        };
        self.thread = sid;
        self._thread_lock = Some(lock);
        Ok(())
    }

    fn prompt(
        &mut self,
        claim: Claim,
        session: &str,
        text: &str,
        model: &str,
        effort: &str,
        token: Option<&str>,
    ) -> io::Result<()> {
        if self.turn.as_ref().is_some_and(|turn| turn.claim == claim) {
            return Ok(());
        }
        if self.turn.is_some() {
            return Err(io::Error::other("the OpenCode lane runs another turn"));
        }
        self.use_login()?;
        self.take(session)?;
        let socket = Socket::turn(&claim, self.send.clone(), Input::turn, false)?;
        // The spool is made before anything reaches OpenCode: a Host that restarts follows
        // this attempt instead of starting it again.
        let now = super::now_ms();
        let mut spool = ClaimSpool::create(&claim.spool, now)?;
        spool.spawned(self.serve.pid, now)?;
        // The session line today's per-turn driver prints first, which OpenCode's reader binds.
        spool.out_json(&json!({"type": "session", "sessionID": self.thread}), now)?;
        spool.sync()?;
        self.own.keep_token(token, "OpenCode");
        self.idle.reset();
        self.turn_socket = Some(socket);
        self.turn = Some(Turn {
            claim,
            spool,
            follow: Follow::new(&self.thread),
            stopping: false,
            steered: Vec::new(),
            order: StoreOrder::default(),
        });
        // A turn the lane lost (its Stop's grace ran out on the Host's side) may still run.
        if self.session_busy {
            let _ = self.serve.client.abort(&self.thread, 10);
        }
        let posted = self.serve.client.post(
            &format!(
                "/session/{}/prompt_async",
                crate::opencode_http::component(&self.thread)
            ),
            Some(&body(text, model, effort)),
            30,
        );
        if !model.is_empty() {
            self.model = Some(model.to_owned());
        }
        if !effort.is_empty() {
            self.effort = Some(effort.to_owned());
        }
        if let Err(error) = posted {
            self.end(Some(1), None, Some(&error.to_string()));
            return Ok(());
        }
        // The prompt is with OpenCode: the turn takes steered words from now until it ends.
        let opened = self.turn.as_mut().map_or(Ok(()), |turn| {
            turn.spool.steer_open(now)?;
            turn.spool.sync()
        });
        if let Err(error) = opened {
            self.end(Some(1), None, Some(&error.to_string()));
        }
        Ok(())
    }

    /// More of the user's words for the running turn: posted to the busy session as a message
    /// the lane names, which OpenCode stores at once and reads at its next step. Refused while a
    /// Stop ends the turn or once it ended; then they wait for a turn of their own.
    fn steer(&mut self, attempt: (u64, u32), id: &str, text: &str) -> io::Result<()> {
        if !self.holds(attempt) {
            return Err(io::Error::other(
                "the OpenCode lane does not run this attempt",
            ));
        }
        if self.turn.as_ref().is_some_and(|turn| turn.stopping) || self.stop_deadline.is_some() {
            return Err(io::Error::other("the turn is being stopped"));
        }
        if id.is_empty() || text.trim().is_empty() {
            return Err(io::Error::other("steered words need an id and a text"));
        }
        let message = self.ids.next(super::now_ms())?;
        let mut posted = body(
            text,
            self.model.as_deref().unwrap_or(""),
            self.effort.as_deref().unwrap_or(""),
        );
        posted["messageID"] = json!(message);
        let result = self.serve.client.post(
            &format!(
                "/session/{}/prompt_async",
                crate::opencode_http::component(&self.thread)
            ),
            Some(&posted),
            30,
        );
        // Kept even when the answer did not come back: OpenCode may have stored the words, and
        // their reading, or their removal at the turn's end, is then still told.
        if let Some(turn) = self.turn.as_mut() {
            turn.steered.push((message, id.to_owned()));
        }
        result
            .map(|_| ())
            .map_err(|error| io::Error::other(error.to_string()))
    }

    /// Whether OpenCode runs the session now, by its own status list (idle sessions are not in
    /// it). Unknown when the list cannot be read.
    fn session_runs(&self) -> Option<bool> {
        let list = self.serve.client.get("/session/status", 10).ok()?;
        Some(matches!(
            list[&self.thread]["type"].as_str(),
            Some("busy" | "retry")
        ))
    }

    /// Whether OpenCode answered any of these messages already, by the session's last messages:
    /// an answer names the last message it took in (`parentID`), which is this one or one
    /// stored after it ([`taken_in`]). Unknown is no.
    fn answered(&self, messages: &[(String, String)]) -> bool {
        self.stored()
            .is_some_and(|list| any_taken_in(&list, messages))
    }

    /// The session's last messages, as OpenCode stored them and in the order it lists them.
    fn stored(&self) -> Option<Value> {
        let path = format!(
            "/session/{}/message?limit=50",
            crate::opencode_http::component(&self.thread)
        );
        self.serve
            .client
            .get(&path, 10)
            .ok()
            .filter(Value::is_array)
    }

    /// At the idle that ends the turn: the steered words OpenCode stored and has not read are
    /// taken out of the session, so the Host runs them as a turn of their own and OpenCode never
    /// meets them twice. Unless [`Settle::Done`], the turn stays open until a later idle.
    ///
    /// OpenCode 1.18 starts no run by itself for words stored while its run went on: such a
    /// prompt only waits for that run (`effect/runner.ts`, `ensureRunning`). But words that
    /// reach it after that run ended start a run of their own, which can come after the idle
    /// the lane is reading now: then the session is busy again, or has even answered them
    /// already with that answer still on its way to the lane, and the words are this turn's.
    /// A delete the session refuses as busy (409) says the same. A Stop's idle comes after an
    /// abort: words the stored messages prove answered are read, and the rest are taken back as
    /// they are. Words not taken back otherwise count as read, so that the Host never runs them
    /// a second time; after a Stop the Host does not count that and gives them back.
    ///
    /// `by_order`: an answer that took the words in by message order holds the turn open
    /// ([`Settle::ByOrder`]); a recheck that found no run since passes false.
    fn take_back_unread(&mut self, stopping: bool, by_order: bool) -> io::Result<Settle> {
        let unread = match self.turn.as_ref() {
            Some(turn) => turn.unread(),
            None => return Ok(Settle::Done),
        };
        // A Stop's idle: words OpenCode answered themselves, whose answer's start the lane never
        // heard, were read; deleting them would delete what OpenCode answered, and the Host would
        // run them again (issue 933: OpenCode 1.18.33 ran a steered message's tool twice). So
        // were words only assumed read, which the Host does not count after a Stop. The
        // session's stored messages decide ([`answered_itself`]); what they do not show, or a
        // list that cannot be read, is taken back as before, and an assumption stays one.
        let unread = if stopping {
            self.read_by_stored(unread)?
        } else {
            unread
        };
        if unread.is_empty() {
            return Ok(Settle::Done);
        }
        if !stopping && self.session_runs() == Some(true) {
            return Ok(Settle::Runs);
        }
        if !stopping && by_order && self.answered(&unread) {
            return Ok(Settle::ByOrder);
        }
        for (message, id) in unread {
            let path = format!(
                "/session/{}/message/{}",
                crate::opencode_http::component(&self.thread),
                crate::opencode_http::component(&message)
            );
            match self.serve.client.delete(&path, 10) {
                Ok(200..=299) => {}
                Ok(409) if !stopping => return Ok(Settle::Runs),
                // Not OpenCode's record that it read them: the Host counts this only for a turn
                // that was not stopped, and gives the words of a stopped one back.
                answer => {
                    eprintln!(
                        "clarp-shim: OpenCode lane: steered words were not taken back ({answer:?}), and count as read"
                    );
                    if let Some(turn) = self.turn.as_mut() {
                        turn.spool.steer_assumed(&id, super::now_ms())?;
                    }
                }
            }
        }
        Ok(Settle::Done)
    }

    /// Writes as read, confirmed, the turn's steered words the session's stored messages show
    /// OpenCode answered themselves ([`answered_itself`]), of the `unread` ones and of those only
    /// assumed read, in the order they were posted, and returns the `unread` rest. An answer to
    /// later words is no proof here: those earlier words may never have been stored. A list that
    /// cannot be read proves nothing.
    fn read_by_stored(
        &mut self,
        unread: Vec<(String, String)>,
    ) -> io::Result<Vec<(String, String)>> {
        let open: Vec<(String, String)> = match self.turn.as_ref() {
            Some(turn) => turn
                .steered
                .iter()
                .filter(|(_, id)| !turn.spool.was_confirmed(id))
                .cloned()
                .collect(),
            None => return Ok(unread),
        };
        if open.is_empty() {
            return Ok(unread);
        }
        let Some(list) = self.stored() else {
            return Ok(unread);
        };
        let Some(turn) = self.turn.as_mut() else {
            return Ok(unread);
        };
        let now = super::now_ms();
        for (message, id) in &open {
            if answered_itself(&list, message) {
                turn.spool.steer_read(id, now)?;
            }
        }
        Ok(unread
            .into_iter()
            .filter(|(_, id)| !turn.spool.was_confirmed(id))
            .collect())
    }

    /// The recheck of a turn held open by message order alone (issue 864): no run came, or its
    /// events were lost. The session's stored messages decide, with no clock and no id order:
    /// words OpenCode answered by one of this turn's own steers posted with or after them
    /// ([`proven`]) were read, and are written so; the rest are taken back and run as a turn of
    /// their own, and the turn ends as its idle would have. A list that cannot be read holds the
    /// turn for one more recheck (`failed` says this is it); after that it ends without it.
    fn look_again(&mut self, failed: bool) {
        let Some((stopping, unread, posted, order)) = self.turn.as_ref().map(|turn| {
            (
                turn.stopping,
                turn.unread(),
                turn.steered.clone(),
                turn.order.clone(),
            )
        }) else {
            return;
        };
        match self.stored() {
            Some(list) => {
                let now = super::now_ms();
                // Inferred from another message, a reading is only assumed (issue 933).
                let read = unread
                    .iter()
                    .filter(|(message, _)| proven(&list, &posted, &order, message))
                    .try_for_each(|(message, id)| match self.turn.as_mut() {
                        Some(turn) if answered_itself(&list, message) => {
                            turn.spool.steer_read(id, now)
                        }
                        Some(turn) => turn.spool.steer_assumed(id, now),
                        None => Ok(()),
                    });
                if let Err(error) = read {
                    return self.end(Some(1), None, Some(&error.to_string()));
                }
            }
            None if !failed && !unread.is_empty() => {
                self.recheck = Some((Instant::now() + RECHECK, true));
                return self.hint();
            }
            None => {}
        }
        match self.take_back_unread(stopping, false) {
            Ok(Settle::Runs) => self.hint(),
            Ok(_) if stopping => self.end(None, Some(nix::libc::SIGINT), None),
            Ok(_) => self.end(Some(0), None, None),
            Err(error) => self.end(Some(1), None, Some(&error.to_string())),
        }
    }

    fn event(&mut self, event: &Value) {
        let props = &event["properties"];
        if props["sessionID"].as_str() == Some(&self.thread) {
            match event["type"].as_str() {
                Some("session.status") => {
                    self.session_busy = props["status"]["type"] != "idle";
                    // The run that message order said was coming: the turn goes on with it.
                    if self.session_busy {
                        self.recheck = None;
                    }
                }
                Some("session.idle") => self.session_busy = false,
                _ => {}
            }
        }
        let Some(turn) = self.turn.as_mut() else {
            return;
        };
        if event["type"] == "message.updated"
            && props["info"]["sessionID"].as_str() == Some(&self.thread)
        {
            turn.order.told(&props["info"], &turn.steered);
        }
        // Steered words are read when an answer that took them in starts: its parent, the last
        // message it took in, is theirs or stored after theirs. Each is written in the order they
        // were posted, and durable, before anything of that answer.
        let info = &props["info"];
        if event["type"] == "message.updated"
            && info["sessionID"].as_str() == Some(&self.thread)
            && info["role"] == "assistant"
            // OpenCode's summary for itself as it compacts its context answers no one (issue
            // 874): the answer after it reads what it took in.
            && !compaction(info)
            && let Some(parent) = info["parentID"].as_str()
        {
            let now = super::now_ms();
            // Only the answer's own parent is OpenCode's record of a reading; the words before it
            // are taken in by id order, an inference (issue 933).
            let read = turn
                .steered
                .iter()
                .filter(|(message, _)| taken_in(message, parent))
                .try_for_each(|(message, id)| {
                    if message == parent {
                        turn.spool.steer_read(id, now)
                    } else {
                        turn.spool.steer_assumed(id, now)
                    }
                });
            if let Err(error) = read {
                self.end(Some(1), None, Some(&error.to_string()));
                return;
            }
        }
        let mut lines = Vec::new();
        let seen = turn.follow.see(&self.serve.client, event, &mut |line| {
            lines.push(line.to_string());
            Ok(())
        });
        let now = super::now_ms();
        let written = lines
            .into_iter()
            .try_for_each(|line| turn.spool.out(&line, now));
        let stopping = turn.stopping;
        if let Err(error) = written {
            self.end(Some(1), None, Some(&error.to_string()));
            return;
        }
        // A turn held open for its recheck: another idle of the same run (OpenCode tells each
        // idle twice, as a status and as `session.idle`) decides nothing; the recheck does. A
        // run that came since cleared the recheck with its busy event; a Stop's idle ends it.
        if matches!(seen, Ok(true)) && self.recheck.is_some() && !stopping {
            return self.hint();
        }
        if matches!(seen, Ok(true)) {
            match self.take_back_unread(stopping, true) {
                Ok(Settle::Done) => {}
                // The session runs the steered words: the turn goes on to its next idle.
                Ok(Settle::Runs) => return self.hint(),
                // Only message order says a run took them in: it must show within RECHECK.
                Ok(Settle::ByOrder) => {
                    self.recheck = Some((Instant::now() + RECHECK, false));
                    return self.hint();
                }
                Err(error) => return self.end(Some(1), None, Some(&error.to_string())),
            }
        }
        match seen {
            Ok(true) if stopping => self.end(None, Some(nix::libc::SIGINT), None),
            Ok(true) => self.end(Some(0), None, None),
            Ok(false) => self.hint(),
            Err(error) => self.end(Some(1), None, Some(&error.to_string())),
        }
    }

    /// A Stop: OpenCode is told to abort the turn, and the idle that follows ends it as
    /// stopped. The server is kept; one that does not end the turn within `grace` is ended.
    fn stop(&mut self, attempt: (u64, u32), grace: Duration) {
        if !self.holds(attempt) {
            return;
        }
        if let Some(turn) = self.turn.as_mut() {
            turn.stopping = true;
        }
        let aborted = self.serve.client.abort(&self.thread, 3);
        let grace = if aborted.is_ok() {
            grace
        } else {
            Duration::ZERO
        };
        self.stop_deadline = Some((Instant::now() + grace, attempt));
    }

    /// Ends the claim's turn in its spool, once: `error` goes to its error lines first.
    fn end(&mut self, code: Option<i32>, signal: Option<i32>, error: Option<&str>) {
        let Some(mut turn) = self.turn.take() else {
            return;
        };
        let now = super::now_ms();
        let mut ended = || -> io::Result<()> {
            if let Some(error) = error {
                turn.spool.err(error, now)?;
            }
            // How the turn ended is how the steered words read into it ended; a stopped turn
            // says nothing, and they ended with it.
            match (code, signal) {
                (_, Some(_)) => {}
                (Some(0), None) => turn.spool.turn_completed(now)?,
                _ => turn
                    .spool
                    .turn_failed(error.unwrap_or("the OpenCode turn failed"), now)?,
            }
            turn.spool.exit(code, signal, now)
        };
        if let Err(error) = ended() {
            eprintln!("clarp-shim: OpenCode lane: a turn's end was not kept: {error}");
        }
        self.stop_deadline = None;
        self.recheck = None;
        self.hint();
        self.followers.clear();
        self.turn_socket = None;
        self.login.release();
        self.idle.reset();
        self.weigh();
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::{MessageIds, StoreOrder, Unheard, any_taken_in, last_line, proven, taken_in};
    use serde_json::json;

    #[test]
    fn a_server_that_ended_unheard_says_the_last_line_it_wrote() {
        // What OpenCode 1.18 wrote when another server held its database (issue 908).
        let said = "\u{1b}[91m\u{1b}[1mError: \u{1b}[0mUnexpected error\n\ndatabase is locked\n";
        assert_eq!(last_line(said), "database is locked");
        assert_eq!(last_line(""), "");
        assert_eq!(last_line("\n  \n"), "");
        let unheard = |said: String| Unheard { said, ended: true };
        assert_eq!(
            unheard(last_line(said)).to_string(),
            "OpenCode did not say where it listens: database is locked"
        );
        assert_eq!(
            unheard(String::new()).to_string(),
            "OpenCode did not say where it listens"
        );
        assert_eq!(last_line(&"x".repeat(400)).len(), 300);
    }

    #[test]
    fn an_answer_takes_in_every_message_up_to_its_parent() {
        let mut ids = MessageIds::default();
        let (first, second) = (
            ids.next(1_795_619_200_256).unwrap(),
            ids.next(1_795_619_200_300).unwrap(),
        );
        // OpenCode read both together and named the last as the answer's parent (issue 862).
        assert!(taken_in(&first, &second));
        assert!(taken_in(&second, &second));
        // An answer to what came before them took neither in, and a message posted after an
        // answer's parent was not taken in by it.
        let prompt = "msg_2133e9c00001OpenCodeNamed1";
        assert!(!taken_in(&first, prompt));
        let later = ids.next(1_795_619_200_400).unwrap();
        assert!(!taken_in(&later, &second));
        assert!(!taken_in(&first, ""));
    }

    #[test]
    fn only_an_answer_to_one_of_the_turns_own_steers_from_it_on_proves_it_read() {
        let answer = |parent: &str| json!({"info": {"role": "assistant", "parentID": parent}});
        let asked = |id: &str| json!({"info": {"role": "user", "id": id}});
        let posted: Vec<(String, String)> = ["b", "c", "d"]
            .iter()
            .map(|m| ((*m).to_owned(), format!("u-{m}")))
            .collect();
        // Answered itself, or with a steer the lane posted after it (issue 862).
        assert!(proven(
            &json!([answer("b")]),
            &posted,
            &StoreOrder::default(),
            "b"
        ));
        assert!(proven(
            &json!([asked("b"), asked("c"), answer("c")]),
            &posted,
            &StoreOrder::default(),
            "b"
        ));
        // An answer to a steer posted before it, to a message not of this turn (whatever its id
        // or place says: a clock that stepped back), or none: no proof (issue 864).
        assert!(!proven(
            &json!([answer("b")]),
            &posted,
            &StoreOrder::default(),
            "c"
        ));
        let ahead = "msg_ffffffffffffA";
        assert!(!proven(
            &json!([asked(ahead), answer(ahead), asked("d")]),
            &posted,
            &StoreOrder::default(),
            "d"
        ));
        assert!(!proven(
            &json!([asked("d")]),
            &posted,
            &StoreOrder::default(),
            "d"
        ));
        assert!(!proven(
            &json!({"not": "a list"}),
            &posted,
            &StoreOrder::default(),
            "b"
        ));
        assert!(!proven(
            &json!([answer("x")]),
            &posted,
            &StoreOrder::default(),
            "x"
        ));
        // A clock that stepped back between two posts: the later steer's id sorts before the
        // earlier one's. Post order decides, and the earlier is still proven by it.
        let mut ids = MessageIds::default();
        let earlier = ids.next(1_795_619_200_400).unwrap();
        let later = ids.next(1_795_619_200_100).unwrap();
        assert!(later < earlier);
        let skewed = vec![
            (earlier.clone(), "u-1".to_owned()),
            (later.clone(), "u-2".to_owned()),
        ];
        assert!(proven(
            &json!([asked(&earlier), asked(&later), answer(&later)]),
            &skewed,
            &StoreOrder::default(),
            &earlier
        ));
        assert!(!proven(
            &json!([answer(&earlier)]),
            &skewed,
            &StoreOrder::default(),
            &later
        ));
        // An older answer whose parent is the turn's prompt, stored before the steer but with an
        // id that sorts after it (a clock step): message order says the steer was taken in, but
        // the answer's parent is no steer of this turn, so nothing proves it.
        let prompt = ids.next(1_795_619_200_900).unwrap();
        let steer = ids.next(1_795_619_200_500).unwrap();
        assert!(taken_in(&steer, &prompt), "order alone says taken in");
        let list = json!([asked(&prompt), answer(&prompt), asked(&steer)]);
        assert!(!proven(
            &list,
            &[(steer.clone(), "u-s".to_owned())],
            &StoreOrder::default(),
            &steer,
        ));
    }

    #[test]
    fn an_answer_after_opencodes_compaction_proves_the_steers_it_stored_before_its_go_on() {
        let asked = |id: &str| json!({"info": {"role": "user", "id": id}});
        let answer = |parent: &str| json!({"info": {"role": "assistant", "parentID": parent}});
        let summary = |parent: &str| {
            json!({"info": {"role": "assistant", "parentID": parent, "mode": "compaction",
                   "agent": "compaction", "summary": true}})
        };
        let posted: Vec<(String, String)> = ["b", "c", "e", "t"]
            .iter()
            .map(|m| ((*m).to_owned(), format!("u-{m}")))
            .collect();
        // What OpenCode told as stored, in its order (issue 878): b before its compaction, c
        // while it summarized, then its go-on message; t, posted earlier, stored only after it
        // (its post answers before it is stored), and e after it.
        let mut order = StoreOrder::default();
        for info in [
            json!({"role": "user", "id": "b"}),
            json!({"role": "user", "id": "compaction"}),
            summary("compaction")["info"].clone(),
            json!({"role": "user", "id": "c"}),
            json!({"role": "user", "id": "go-on"}),
            json!({"role": "user", "id": "t"}),
            json!({"role": "user", "id": "e"}),
        ] {
            order.told(&info, &posted);
        }
        let list = json!([
            asked("b"),
            asked("compaction"),
            summary("compaction"),
            asked("c"),
            asked("go-on"),
            asked("t"),
            answer("go-on"),
            asked("e"),
        ]);
        assert!(proven(&list, &posted, &order, "b"));
        assert!(
            proven(&list, &posted, &order, "c"),
            "stored while it summarized"
        );
        assert!(
            !proven(&list, &posted, &order, "t"),
            "posted before, stored after"
        );
        assert!(!proven(&list, &posted, &order, "e"));
        // Not seen stored at all (no mark): runs once.
        assert!(!proven(&list, &posted, &StoreOrder::default(), "b"));
        // The stored list must agree: no compaction's summary right before the go-on message, or
        // only the summary itself, proves nothing.
        let list = json!([asked("b"), asked("go-on"), answer("go-on")]);
        assert!(!proven(&list, &posted, &order, "b"));
        let list = json!([asked("b"), asked("compaction"), summary("compaction")]);
        assert!(!proven(&list, &posted, &order, "b"));
        // An ordinary message of OpenCode's after an ordinary answer is no go-on message.
        let mut plain = StoreOrder::default();
        for info in [
            json!({"role": "user", "id": "b"}),
            json!({"role": "assistant", "id": "x"}),
            json!({"role": "user", "id": "other"}),
        ] {
            plain.told(&info, &posted);
        }
        assert!(plain.go_on.is_empty());
    }

    #[test]
    fn at_the_idle_a_message_answered_with_a_later_one_counts_as_answered() {
        let mut ids = MessageIds::default();
        let (unread, later, after) = (
            ids.next(1_795_619_200_256).unwrap(),
            ids.next(1_795_619_200_300).unwrap(),
            ids.next(1_795_619_200_400).unwrap(),
        );
        let answer = |parent: &str| json!({"info": {"role": "assistant", "parentID": parent}});
        let asked = |message: &str| json!({"info": {"role": "user", "id": message}});
        // OpenCode answered the unread message together with a later one: the answer names the
        // later one (issue 862).
        let list = json!([asked(&unread), asked(&later), answer(&later)]);
        assert!(any_taken_in(&list, &[(unread.clone(), "u".into())]));
        // An answer before it, or a user message alone, is no answer to it.
        let list = json!([answer("msg_2133e9c00001OpenCodeNamed1"), asked(&after)]);
        assert!(!any_taken_in(&list, &[(unread.clone(), "u".into())]));
        let list = json!([answer(&unread)]);
        assert!(!any_taken_in(&list, &[(after, "a".into())]));
    }

    #[test]
    fn the_lanes_messages_are_named_in_opencodes_ascending_form() {
        let mut ids = MessageIds::default();
        let first = ids.next(1_795_619_200_256).unwrap();
        let same = ids.next(1_795_619_200_256).unwrap();
        let later = ids.next(1_795_619_200_257).unwrap();
        // A message OpenCode named in that millisecond, first of it (`msg_2133e9d00001...`).
        assert_eq!(&first[..16], "msg_2133e9d00001", "{first}");
        assert_eq!(&same[..16], "msg_2133e9d00002", "{same}");
        assert_eq!(&later[..16], "msg_2133e9d01001", "{later}");
        for id in [&first, &same, &later] {
            assert_eq!(id.len(), 30, "{id}");
            assert!(id[16..].bytes().all(|b| b.is_ascii_alphanumeric()), "{id}");
        }
        assert!(first[..16] < same[..16] && same[..16] < later[..16]);
    }
}
