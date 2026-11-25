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

use std::collections::BTreeMap;
use std::fs::{self, File};
use std::io::{self, Read, Write};
use std::os::unix::net::UnixStream;
use std::os::unix::process::CommandExt;
use std::path::Path;
use std::process::Stdio;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, SyncSender};
use std::time::{Duration, Instant};

use base64::{Engine as _, engine::general_purpose::STANDARD};
use clarp_shim::Reply;
use clarp_shim::lane::{Claim, LOGIN_CHANGED, thread_lock};
use clarp_shim::lane_spool::ClaimSpool;
use clarp_shim::opencode_lane::{Request, Snapshot, spoken};
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
    lane.end(Some(1), None, Some(SERVER_GONE));
    let retired = lane
        .retire_reply
        .take()
        .map(|stream| (stream, lane.inspect()));
    // The server is ended and reaped before the session's lock and the owner lock are let go.
    lane.serve.end();
    drop(lane);
    drop(lock);
    if let Some((mut stream, state)) = retired {
        let _ = write(&mut stream, &json!({"ok": true, "state": state}));
    }
    result
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
    fn start(program: &str, directory: String, send: SyncSender<Input>) -> io::Result<Self> {
        let mut random = [0u8; 24];
        File::open("/dev/urandom")?.read_exact(&mut random)?;
        let password = STANDARD.encode(random);
        let serve = ["serve", "--hostname", "127.0.0.1", "--port", "0"];
        let mut child = crate::tool_child::command(program)
            .args(serve)
            .env("OPENCODE_SERVER_PASSWORD", &password)
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .process_group(0)
            .spawn()?;
        let stdout = child.stdout.take();
        let pid = child.id();
        let (reaped, gone) = mpsc::channel();
        let told = send.clone();
        std::thread::Builder::new()
            .name("opencode-lane-serve".into())
            .spawn(move || {
                let _ = child.wait();
                let _ = reaped.send(());
                let _ = told.send(Input::ServeGone);
            })?;
        let mut serve = Self {
            pid,
            client: Client::new(0, "", String::new()),
            version: String::new(),
            gone,
            ended: false,
        };
        let Some(port) = stdout.and_then(port) else {
            serve.end();
            return Err(io::Error::other("OpenCode did not say where it listens"));
        };
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
    /// The steered words posted to the session: the message the lane named them, then the id
    /// the Host steered them under.
    steered: BTreeMap<String, String>,
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
        self.idle.deadline(self.turn.is_none())
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
            steered: BTreeMap::new(),
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
            turn.steered.insert(message, id.to_owned());
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
    /// an answer names the message it answers (`parentID`). Unknown is no.
    fn answered(&self, messages: &[(String, String)]) -> bool {
        let path = format!(
            "/session/{}/message?limit=50",
            crate::opencode_http::component(&self.thread)
        );
        let Ok(list) = self.serve.client.get(&path, 10) else {
            return false;
        };
        list.as_array().into_iter().flatten().any(|message| {
            message["info"]["role"] == "assistant"
                && messages
                    .iter()
                    .any(|(steered, _)| message["info"]["parentID"] == steered.as_str())
        })
    }

    /// At the idle that ends the turn: the steered words OpenCode stored and has not read are
    /// taken out of the session, so the Host runs them as a turn of their own and OpenCode never
    /// meets them twice. False: the session runs again, and the turn stays open until that ends.
    ///
    /// OpenCode 1.18 starts no run by itself for words stored while its run went on: such a
    /// prompt only waits for that run (`effect/runner.ts`, `ensureRunning`). But words that
    /// reach it after that run ended start a run of their own, which can come after the idle
    /// the lane is reading now: then the session is busy again, or has even answered them
    /// already with that answer still on its way to the lane, and the words are this turn's.
    /// A delete the session refuses as busy (409) says the same. A Stop's idle comes after an
    /// abort, and takes the words back as they are. Words not taken back otherwise count as
    /// read, so that the Host never runs them a second time.
    fn take_back_unread(&mut self, stopping: bool) -> io::Result<bool> {
        let unread = match self.turn.as_ref() {
            Some(turn) => turn.unread(),
            None => return Ok(true),
        };
        if unread.is_empty() {
            return Ok(true);
        }
        if !stopping && (self.session_runs() == Some(true) || self.answered(&unread)) {
            return Ok(false);
        }
        for (message, id) in unread {
            let path = format!(
                "/session/{}/message/{}",
                crate::opencode_http::component(&self.thread),
                crate::opencode_http::component(&message)
            );
            match self.serve.client.delete(&path, 10) {
                Ok(200..=299) => {}
                Ok(409) if !stopping => return Ok(false),
                answer => {
                    eprintln!(
                        "clarp-shim: OpenCode lane: steered words were not taken back ({answer:?}), and count as read"
                    );
                    if let Some(turn) = self.turn.as_mut() {
                        turn.spool.steer_read(&id, super::now_ms())?;
                    }
                }
            }
        }
        Ok(true)
    }

    fn event(&mut self, event: &Value) {
        let props = &event["properties"];
        if props["sessionID"].as_str() == Some(&self.thread) {
            match event["type"].as_str() {
                Some("session.status") => {
                    self.session_busy = props["status"]["type"] != "idle";
                }
                Some("session.idle") => self.session_busy = false,
                _ => {}
            }
        }
        let Some(turn) = self.turn.as_mut() else {
            return;
        };
        // Steered words are read when an answer that names their message starts: written, and
        // durable, before anything of that answer.
        let info = &props["info"];
        if event["type"] == "message.updated"
            && info["sessionID"].as_str() == Some(&self.thread)
            && info["role"] == "assistant"
            && let Some(id) = info["parentID"]
                .as_str()
                .and_then(|parent| turn.steered.get(parent))
                .cloned()
            && let Err(error) = turn.spool.steer_read(&id, super::now_ms())
        {
            self.end(Some(1), None, Some(&error.to_string()));
            return;
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
        if matches!(seen, Ok(true)) {
            match self.take_back_unread(stopping) {
                Ok(true) => {}
                // The session runs the steered words: the turn goes on to its next idle.
                Ok(false) => return self.hint(),
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
    use super::MessageIds;

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
