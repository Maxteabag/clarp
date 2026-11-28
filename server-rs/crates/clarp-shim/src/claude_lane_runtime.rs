//! One process keeps an agent's Claude Code between its chat turns: the stream-json driver of
//! the lane layer (issue 761). What every lane keeps for itself (its options, locks, turn file,
//! idle clock, login and sockets) is in the `lane_*` modules.
//!
//! Claude Code is started once, `claude -p --input-format stream-json --resume <session>`, and
//! kept. Each attempt the Host gives the lane gets a spool of its own, written as a turn's own
//! shim writes one (`spawned`, `input-open`, every line Claude Code prints, `exited`), so the
//! Host reads a lane's turn as it reads any other:
//!
//! - the turn's message is written to Claude Code's input once its spool is made;
//! - more of the user's words come on the attempt's socket as a line for the input, while the
//!   input is open, until the turn's first `result`, as with `--stdin-until result`;
//! - the attempt ends at the `result` that settles every message written into it (its
//!   `user_message_uuids`): a message read after a `result` is a turn of its own in the same
//!   process, and its own `result` settles it;
//! - a Stop is Claude Code's interrupt on its input; it answers with the turn's `result` and
//!   keeps running. One that does not answer within the Stop's time is ended with its group.
//!
//! The lane leaves when it is retired or idle, when the login changed, when Claude Code exits,
//! after a turn that ended in an error (a usage limit among them: the pool takes it from there
//! as today) and after a compaction (the next turn starts a new process on the shortened
//! conversation).

use std::collections::BTreeSet;
use std::fs;
use std::io::{self, Read, Write};
use std::os::unix::net::UnixStream;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, SyncSender};
use std::time::{Duration, Instant};

use clarp_shim::Reply;
use clarp_shim::claude_lane::{
    CONTROL_RESPONSE, Claim, LOGIN_CHANGED, Request, Snapshot, interrupt, key,
};
use clarp_spool::{Entry, MAX_OUTPUT_CHUNK, Record, ShimEvent, SpoolWriter};
use serde_json::{Value, json};

use crate::lane_login::Login;
use crate::lane_own::{Idle, Own, hold_owner, lock_exclusive, name_tool};
use crate::lane_socket::{Socket, TurnAsk, write};

/// How long a Stop that named no time gives Claude Code to answer its interrupt.
const STOP_GRACE: Duration = Duration::from_secs(5);
/// How long a lane that leaves gives Claude Code to end by itself once its input is closed.
const EXIT_LIMIT: Duration = Duration::from_secs(2);
/// How long a new lane waits for the conversation's own process (the turn that ran beside it)
/// to let the conversation go.
const THREAD_WAIT: Duration = Duration::from_secs(10);
const READ_AT_ONCE: usize = 64 * 1024;

pub(super) enum Input {
    /// A whole line Claude Code printed.
    Line(String),
    /// Claude Code closed its output: it has gone.
    Gone,
    Control(Request, UnixStream),
    Follow((u64, u32), UnixStream),
    /// A Stop on the attempt's socket, with how long Claude Code has to end the turn.
    Stop((u64, u32), u64, UnixStream),
    /// A line for Claude Code's input, for the attempt.
    Stdin((u64, u32), String, UnixStream),
    /// A request the Claude lane does not take.
    Refused(UnixStream),
}

impl Input {
    fn control(request: Request, stream: UnixStream) -> Self {
        Self::Control(request, stream)
    }

    fn turn(attempt: (u64, u32), ask: TurnAsk, stream: UnixStream) -> Self {
        match ask {
            TurnAsk::Follow => Self::Follow(attempt, stream),
            TurnAsk::Stop(grace_ms) => Self::Stop(attempt, grace_ms, stream),
            TurnAsk::Stdin(data) => Self::Stdin(attempt, data, stream),
            TurnAsk::Steer(..) => Self::Refused(stream),
        }
    }
}

/// The account Claude Code's own file says is logged in: a refreshed token of the same account
/// keeps the lane, a seat switch to another retires it. A key login names its key there
/// instead; a file that names neither is one account, whatever else in it changes, since Claude
/// Code rewrites that file at every start.
fn account(said: &Value) -> Option<&str> {
    said["oauthAccount"]["accountUuid"]
        .as_str()
        .or(said["primaryApiKey"].as_str())
        .or(Some(""))
}

/// The login Claude Code's credentials file holds: its refresh token, which a new login, a
/// pasted token or a switch that writes only this file changes, and an access token's refresh
/// does not. A file with none (a key login) is one login. Hashed by the login check, never
/// shown.
fn credentials(said: &Value) -> Option<&str> {
    said["claudeAiOauth"]["refreshToken"].as_str().or(Some(""))
}

pub(super) fn from_args(mut args: impl Iterator<Item = String>) -> io::Result<()> {
    let (Some(socket), Some(options), None) = (args.next(), args.next(), args.next()) else {
        return Err(io::Error::other("usage: claude-lane SOCKET OPTIONS_FILE"));
    };
    let options = serde_json::from_reader(fs::File::open(options)?).map_err(io::Error::other)?;
    run(Path::new(&socket), options)
}

fn run(path: &Path, mut options: Value) -> io::Result<()> {
    let _ = nix::unistd::setsid();
    let own = Own::take(&mut options);
    let owned: Vec<PathBuf> = options[key::OWNED_FILES]
        .as_array()
        .map(|files| {
            files
                .iter()
                .filter_map(|file| file.as_str().map(PathBuf::from))
                .collect()
        })
        .unwrap_or_default();
    let lock = hold_owner(path)?;
    let result = (|| {
        // One writer per conversation: the lane holds it from before Claude Code starts until
        // Claude Code has been reaped.
        let held_thread = own
            .thread_lock
            .as_deref()
            .map(wait_for_thread)
            .transpose()?;
        match fs::remove_file(path) {
            Ok(()) => {}
            Err(error) if error.kind() == io::ErrorKind::NotFound => {}
            Err(error) => return Err(error),
        }
        let (send, input) = mpsc::sync_channel(256);
        let _listener = Socket::control(path, send.clone(), Input::control)?;
        let login = Login::read(&mut options, |value| account(value).map(str::to_owned))?;
        // The credentials are a second login file, read the same way with no lock of its own.
        let credentials = Login::read(
            &mut json!({ clarp_shim::lane::key::LOGIN_FILE: options[key::CREDENTIALS_FILE].take() }),
            |value| credentials(value).map(str::to_owned),
        )?;
        let session = options[key::SESSION]
            .as_str()
            .unwrap_or_default()
            .to_owned();
        let cwd = options[key::CWD].as_str().map(PathBuf::from);
        let program = options[key::TOOL_PROGRAM]
            .as_str()
            .ok_or_else(|| io::Error::other("the Claude lane names no program"))?
            .to_owned();
        let args: Vec<String> = options[key::ARGS]
            .as_array()
            .map(|args| {
                args.iter()
                    .filter_map(|arg| arg.as_str().map(str::to_owned))
                    .collect()
            })
            .unwrap_or_default();
        // Started through the shim's `tool-child`, so Claude Code ends with the lane's process.
        let mut command = crate::tool_child::command(&program);
        command
            .args(&args)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .process_group(0);
        if let Some(cwd) = &cwd {
            command.current_dir(cwd);
        }
        let mut child = command.spawn()?;
        // Claude Code leads a group of its own: named in the owner lock, so a lane that hangs on
        // its way out is ended with it.
        if let Err(error) = name_tool(&lock, child.id()) {
            eprintln!("clarp-shim: the lane could not name its tool in its owner lock: {error}");
        }
        let stdin = child.stdin.take();
        if let Some(stdout) = child.stdout.take() {
            let relay = send.clone();
            std::thread::Builder::new()
                .name("claude-lane-out".into())
                .spawn(move || lines(stdout, &relay))?;
        }
        if let Some(mut stderr) = child.stderr.take() {
            // What Claude Code says on its error stream is the lane's log, never a turn's.
            std::thread::Builder::new()
                .name("claude-lane-err".into())
                .spawn(move || {
                    let _ = io::copy(&mut stderr, &mut io::stderr());
                })?;
        }
        let idle = Idle::new(own.idle);
        let mut lane = Lane {
            child,
            stdin,
            _thread_lock: held_thread,
            own,
            idle,
            login,
            credentials,
            stale_login: false,
            thread: session,
            cwd,
            send,
            input,
            turn: None,
            claims: 0,
            memory: None,
            revision: 0,
            watchers: Vec::new(),
            leave_after: false,
            stops: 0,
            retire_reply: None,
        };
        println!("ready");
        io::stdout().flush()?;
        let served = lane.serve();
        // No longer answered from here on: the socket goes first, so a Host that asks finds a
        // lane on its way out (nobody listens), never a busy one that is slow to answer.
        let _ = fs::remove_file(path);
        let retired = lane.leave();
        let state = lane.inspect();
        // The locks witness Claude Code's real lifetime: it has been reaped before either is
        // let go.
        drop(lane);
        Ok((served, retired.map(|stream| (stream, state))))
    })();
    for file in &owned {
        let _ = fs::remove_file(file);
    }
    drop(lock);
    let (served, retired) = result?;
    if let Some((mut stream, state)) = retired {
        let _ = write(&mut stream, &json!({"ok": true, "state": state}));
    }
    served
}

/// Takes the conversation's lock. The turn that ran beside a new lane may still be letting it
/// go: it is waited for, not long.
fn wait_for_thread(lock: &Path) -> io::Result<fs::File> {
    let until = Instant::now() + THREAD_WAIT;
    loop {
        match lock_exclusive(lock) {
            Ok(file) => return Ok(file),
            Err(_) if Instant::now() < until => std::thread::sleep(Duration::from_millis(50)),
            Err(error) => return Err(error),
        }
    }
}

/// Every whole line Claude Code prints, to the lane's loop.
fn lines(mut stdout: impl Read, relay: &SyncSender<Input>) {
    let mut held = Vec::new();
    let mut chunk = vec![0; READ_AT_ONCE];
    loop {
        let read = match stdout.read(&mut chunk) {
            Ok(0) | Err(_) => break,
            Ok(read) => read,
        };
        held.extend_from_slice(&chunk[..read]);
        let went = clarp_shim::take_lines(&mut held, MAX_OUTPUT_CHUNK, |line| {
            relay.send(Input::Line(line)).is_ok()
        });
        if !went {
            return;
        }
    }
    if !held.is_empty() {
        let _ = relay.send(Input::Line(String::from_utf8_lossy(&held).into_owned()));
    }
    let _ = relay.send(Input::Gone);
}

/// The attempt the lane runs.
struct Turn {
    claim: Claim,
    writer: SpoolWriter,
    /// Its socket, for as long as the lane runs it.
    _socket: Socket,
    followers: Vec<UnixStream>,
    /// The messages written into it that no `result` has settled yet.
    unsettled: BTreeSet<String>,
    /// More words may be written: until its first `result`.
    open: bool,
    /// A Stop interrupted it, and when Claude Code is ended if it has not answered.
    stop_by: Option<Instant>,
}

struct Lane {
    child: Child,
    stdin: Option<ChildStdin>,
    /// Held, never read: let go only after Claude Code has been reaped.
    _thread_lock: Option<fs::File>,
    own: Own,
    idle: Idle,
    login: Login,
    credentials: Login,
    stale_login: bool,
    thread: String,
    cwd: Option<PathBuf>,
    send: SyncSender<Input>,
    input: Receiver<Input>,
    turn: Option<Turn>,
    claims: u64,
    /// What the lane held in memory when last weighed, in bytes ([`Lane::weigh`]).
    memory: Option<u64>,
    revision: u64,
    watchers: Vec<UnixStream>,
    /// Something happened that the next turn must not inherit (an error, a compaction): the
    /// lane leaves once its attempt has ended.
    leave_after: bool,
    stops: u64,
    retire_reply: Option<UnixStream>,
}

impl Lane {
    fn inspect(&mut self) -> Snapshot {
        self.revision = self.revision.saturating_add(1);
        Snapshot {
            thread: self.thread.clone(),
            instance: std::process::id(),
            revision: self.revision,
            available: self.turn.is_none() && !self.leave_after && !self.stale_login,
            claim: self.turn.as_ref().map(|turn| turn.claim.clone()),
            options: self.own.digest.clone(),
            model: None,
            effort: None,
            idle_ms: self.idle.ms(self.turn.is_some()),
            claims: self.claims,
            memory_bytes: self.memory,
            working: Some(self.turn.is_some()),
        }
    }

    fn show(&mut self) {
        if self.watchers.is_empty() {
            return;
        }
        let snapshot = self.inspect();
        self.watchers
            .retain_mut(|stream| write(stream, &snapshot).is_ok());
    }

    fn hint(turn: &mut Turn) {
        turn.followers
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

    /// When the lane next has something to do by itself: end a turn that did not answer a Stop,
    /// or leave after its idle time.
    fn deadline(&self) -> Option<Instant> {
        match &self.turn {
            Some(turn) => turn.stop_by,
            None => self.idle.deadline(true),
        }
    }

    fn serve(&mut self) -> io::Result<()> {
        loop {
            let input = match self.deadline() {
                None => self.input.recv().ok(),
                Some(at) => match self
                    .input
                    .recv_timeout(at.saturating_duration_since(Instant::now()))
                {
                    Ok(input) => Some(input),
                    Err(RecvTimeoutError::Disconnected) => None,
                    Err(RecvTimeoutError::Timeout) => {
                        if self.turn.is_some() {
                            return self.unanswered_stop();
                        }
                        if self.idle.over(true) {
                            return Ok(());
                        }
                        continue;
                    }
                },
            };
            match input {
                Some(Input::Line(line)) => {
                    if self.line(&line)? {
                        return Ok(());
                    }
                }
                Some(Input::Control(request, stream)) => {
                    if self.control(request, stream) {
                        return Ok(());
                    }
                }
                Some(Input::Follow(attempt, mut stream)) => {
                    let pid = self.child.id();
                    let held = self.holds(attempt);
                    if let Some(turn) = self.turn.as_mut().filter(|_| held)
                        && turn.followers.len() < 8
                    {
                        let _ = write(&mut stream, &Reply::ok(Some(pid)));
                        let _ = stream.set_nonblocking(true);
                        turn.followers.push(stream);
                    }
                }
                Some(Input::Stop(attempt, grace_ms, mut stream)) => {
                    let grace = Some(Duration::from_millis(grace_ms))
                        .filter(|grace| !grace.is_zero())
                        .unwrap_or(STOP_GRACE);
                    let reply = match self.stop(attempt, grace) {
                        Ok(()) => Reply::ok(Some(self.child.id())),
                        Err(error) => Reply::failed(error.to_string()),
                    };
                    let _ = write(&mut stream, &reply);
                }
                Some(Input::Stdin(attempt, data, mut stream)) => {
                    let reply = match self.steer(attempt, &data) {
                        Ok(()) => Reply::ok(Some(self.child.id())),
                        Err(error) => Reply::failed(error.to_string()),
                    };
                    let _ = write(&mut stream, &reply);
                }
                Some(Input::Refused(mut stream)) => {
                    let _ = write(&mut stream, &Reply::failed("Unsupported lane turn request"));
                }
                Some(Input::Gone) | None => return self.gone(),
            }
        }
    }

    /// True: the lane leaves.
    fn control(&mut self, request: Request, mut stream: UnixStream) -> bool {
        let result = match request {
            Request::Inspect | Request::Watch => {
                let _ = write(&mut stream, &json!({"ok": true, "state": self.inspect()}));
                if request == Request::Watch && self.watchers.len() < 4 {
                    let _ = stream.set_nonblocking(true);
                    self.watchers.push(stream);
                }
                return false;
            }
            Request::Prompt { claim, text, token } => self.prompt(claim, &text, token.as_deref()),
            Request::Retire if self.turn.is_none() => {
                self.retire_reply = Some(stream);
                return true;
            }
            Request::Retire => Err(io::Error::other("the Claude lane still runs a turn")),
        };
        let reply = match result {
            Ok(()) => json!({"ok": true, "state": self.inspect()}),
            // A changed login is said with a code: nothing reached Claude Code, and the Host
            // gives the same attempt to the lane that starts after this one.
            Err(error) if self.stale_login => {
                json!({"ok": false, "error": error.to_string(), "code": LOGIN_CHANGED})
            }
            Err(error) => json!({"ok": false, "error": error.to_string()}),
        };
        let _ = write(&mut stream, &reply);
        self.show();
        self.stale_login || (self.leave_after && self.turn.is_none())
    }

    fn prompt(&mut self, claim: Claim, text: &str, token: Option<&str>) -> io::Result<()> {
        if self.turn.as_ref().is_some_and(|turn| turn.claim == claim) {
            return Ok(());
        }
        if self.turn.is_some() || self.leave_after {
            return Err(io::Error::other("the Claude lane cannot take a turn now"));
        }
        let uuid =
            uuid_of(text).ok_or_else(|| io::Error::other("the turn's message names no uuid"))?;
        if !self.login.acquire()? || !self.credentials.acquire()? {
            self.login.release();
            self.stale_login = true;
            return Err(io::Error::other(
                "the Claude login changed before this attempt",
            ));
        }
        let opened = self.open(&claim);
        let (socket, writer) = match opened {
            Ok(opened) => opened,
            Err(error) => {
                self.login.release();
                return Err(error);
            }
        };
        self.own.keep_token(token, "Claude");
        self.claims = self.claims.saturating_add(1);
        self.idle.reset();
        self.turn = Some(Turn {
            claim,
            writer,
            _socket: socket,
            followers: Vec::new(),
            unsettled: BTreeSet::from([uuid]),
            open: true,
            stop_by: None,
        });
        if let Err(error) = self.give(text) {
            // Claude Code cannot be written to: it has gone, and the lane with it.
            self.end_turn(Some(1), None)?;
            self.leave_after = true;
            return Err(error);
        }
        Ok(())
    }

    /// The attempt's socket and spool. The spool is made before anything reaches Claude Code:
    /// an attempt whose spool is there must never be given to it again. It opens as a turn's
    /// own shim opens one, with the process that runs it and who owns its input.
    fn open(&self, claim: &Claim) -> io::Result<(Socket, SpoolWriter)> {
        let socket = Socket::turn(claim, self.send.clone(), Input::turn, true)?;
        let mut writer = SpoolWriter::create(&claim.spool)?;
        let now = super::now_ms();
        let pid = self.child.id();
        let mut opening = vec![
            Entry::Shim(ShimEvent::Session {
                id: self.thread.clone(),
            }),
            Entry::Shim(ShimEvent::Spawned { pid }),
        ];
        opening.extend(super::streaming_owner(pid, self.cwd.as_deref()).map(Entry::Shim));
        for entry in opening {
            writer.append(&Record { t: now, entry })?;
        }
        writer.sync()?;
        Ok((socket, writer))
    }

    /// Writes one line to Claude Code's input.
    fn give(&mut self, line: &str) -> io::Result<()> {
        let stdin = self
            .stdin
            .as_mut()
            .ok_or_else(|| io::Error::other("Claude Code's input is closed"))?;
        stdin.write_all(line.trim_end().as_bytes())?;
        stdin.write_all(b"\n")?;
        stdin.flush()
    }

    /// More of the user's words for the running turn, while its input is open.
    fn steer(&mut self, attempt: (u64, u32), data: &str) -> io::Result<()> {
        if !self.holds(attempt) {
            return Err(io::Error::other("the tool's input is closed"));
        }
        let Some(turn) = self.turn.as_mut() else {
            return Err(io::Error::other("the tool's input is closed"));
        };
        if !turn.open || turn.stop_by.is_some() {
            return Err(io::Error::other("the tool's input is closed"));
        }
        let uuid = uuid_of(data).ok_or_else(|| io::Error::other("the message names no uuid"))?;
        turn.unsettled.insert(uuid);
        self.give(data)
    }

    fn stop(&mut self, attempt: (u64, u32), grace: Duration) -> io::Result<()> {
        if !self.holds(attempt) {
            return Ok(());
        }
        if self
            .turn
            .as_ref()
            .is_some_and(|turn| turn.stop_by.is_some())
        {
            return Ok(());
        }
        self.stops = self.stops.saturating_add(1);
        let request = format!("clarp-stop-{}", self.stops);
        if let Some(turn) = self.turn.as_mut() {
            turn.stop_by = Some(Instant::now() + grace);
            turn.open = false;
            let stopping = ShimEvent::Stopping {
                signal: "interrupt".into(),
            };
            turn.writer.append(&Record {
                t: super::now_ms(),
                entry: Entry::Shim(stopping),
            })?;
            Self::hint(turn);
        }
        if let Err(error) = self.give(&interrupt(&request)) {
            // Nothing can reach Claude Code: it is ended at once.
            if let Some(turn) = self.turn.as_mut() {
                turn.stop_by = Some(Instant::now());
            }
            return Err(error);
        }
        Ok(())
    }

    /// True: the lane leaves.
    fn line(&mut self, line: &str) -> io::Result<bool> {
        let event: Value = serde_json::from_str(line).unwrap_or(Value::Null);
        let kind = event["type"].as_str().unwrap_or_default();
        if kind == CONTROL_RESPONSE {
            return Ok(false);
        }
        // A compaction shortened the conversation the process holds: the next turn starts a new
        // one on it.
        if kind == "system" && event["subtype"] == "compact_boundary" {
            self.leave_after = true;
        }
        let top_result = kind == "result"
            && event["parent_tool_use_id"]
                .as_str()
                .is_none_or(str::is_empty);
        let Some(turn) = self.turn.as_mut() else {
            // Claude Code answered with no turn of the Host's running: what it holds is no
            // longer what the Host knows of the conversation, and a new process reads it anew.
            if top_result {
                eprintln!("clarp-shim: Claude lane: a result came with no turn; leaving");
                return Ok(true);
            }
            return Ok(false);
        };
        turn.writer.append(&Record {
            t: super::now_ms(),
            entry: Entry::Out(line.to_owned()),
        })?;
        Self::hint(turn);
        if !top_result {
            return Ok(false);
        }
        turn.open = false;
        for uuid in event["user_message_uuids"].as_array().into_iter().flatten() {
            if let Some(uuid) = uuid.as_str() {
                turn.unsettled.remove(uuid);
            }
        }
        let stopped = turn.stop_by.is_some();
        if !(turn.unsettled.is_empty() || stopped) {
            return Ok(false);
        }
        // A turn that ended in an error (a usage limit, a failed request) leaves the process
        // behind it: the next turn starts a new one. An interrupt's own result is not one.
        if event["is_error"] == true && !stopped {
            self.leave_after = true;
        }
        if stopped {
            self.end_turn(None, Some(nix::libc::SIGINT))?;
        } else {
            self.end_turn(Some(0), None)?;
        }
        Ok(self.leave_after)
    }

    /// The attempt is over: its spool says so as a turn's own shim says its tool exited, and the
    /// lane is free.
    fn end_turn(&mut self, code: Option<i32>, signal: Option<i32>) -> io::Result<()> {
        let Some(mut turn) = self.turn.take() else {
            return Ok(());
        };
        turn.writer.append(&Record {
            t: super::now_ms(),
            entry: Entry::Shim(ShimEvent::Exited { code, signal }),
        })?;
        let synced = turn.writer.sync();
        Self::hint(&mut turn);
        drop(turn);
        self.login.release();
        self.idle.reset();
        self.weigh();
        self.show();
        synced
    }

    /// Weighs the lane: itself and Claude Code's group, its MCP servers among them (issue 761).
    /// Done when an attempt has ended, after its last spool line, so never on a turn's path; a
    /// Claude Code that has just started is not weighed yet, and the Host counts Claude's
    /// typical size for it until then.
    fn weigh(&mut self) {
        self.memory = crate::lane_memory::held(std::process::id(), self.child.id());
    }

    /// Claude Code did not answer a Stop in its time: it is ended with its group, and the lane
    /// leaves.
    fn unanswered_stop(&mut self) -> io::Result<()> {
        self.kill();
        self.end_turn(None, Some(nix::libc::SIGKILL))?;
        Err(io::Error::other(
            "Claude Code did not end its turn after a Stop",
        ))
    }

    /// Claude Code has gone by itself: the running attempt ends with how it went.
    fn gone(&mut self) -> io::Result<()> {
        let status = self.child.wait().ok();
        let code = status.and_then(|status| status.code());
        let signal = status.and_then(|status| status.signal());
        self.end_turn(code.or(Some(1)).filter(|_| signal.is_none()), signal)?;
        Err(io::Error::other("Claude Code exited"))
    }

    fn kill(&mut self) {
        if let Ok(pid) = i32::try_from(self.child.id()) {
            let _ = nix::sys::signal::killpg(
                nix::unistd::Pid::from_raw(pid),
                nix::sys::signal::Signal::SIGKILL,
            );
        }
    }

    /// Ends Claude Code before the lane goes: its input is closed, it is given a moment to end
    /// by itself, and then its group is ended. What answers a retirement is given back.
    fn leave(&mut self) -> Option<UnixStream> {
        drop(self.stdin.take());
        let until = Instant::now() + EXIT_LIMIT;
        loop {
            match self.child.try_wait() {
                Ok(Some(_)) => break,
                Ok(None) if Instant::now() < until => {
                    std::thread::sleep(Duration::from_millis(20));
                }
                _ => {
                    self.kill();
                    let _ = self.child.wait();
                    break;
                }
            }
        }
        // What the group left behind goes with it: a process of the lane must not outlive it.
        self.kill();
        let _ = self.end_turn(None, Some(nix::libc::SIGKILL));
        self.retire_reply.take()
    }
}

/// The id of a stream-json user line, which the `result` that answers it names.
fn uuid_of(line: &str) -> Option<String> {
    let said: Value = serde_json::from_str(line).ok()?;
    said["uuid"]
        .as_str()
        .filter(|uuid| !uuid.is_empty())
        .map(str::to_owned)
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::{account, credentials, uuid_of};
    use serde_json::json;

    #[test]
    fn the_uuid_of_a_user_line_is_read() {
        let line = json!({"type": "user", "uuid": "u-1", "message": {"role": "user"}}).to_string();
        assert_eq!(uuid_of(&line).as_deref(), Some("u-1"));
        assert_eq!(uuid_of(r#"{"type":"user"}"#), None);
        assert_eq!(uuid_of(r#"{"uuid": ""}"#), None);
    }

    #[test]
    fn a_claude_login_is_its_account_its_key_or_its_refresh_token() {
        let said = json!({"oauthAccount": {"accountUuid": "a"}, "numStartups": 4});
        assert_eq!(account(&said), Some("a"));
        assert_eq!(account(&json!({"numStartups": 5})), Some(""));
        // A key login is told by its key.
        assert_eq!(account(&json!({"primaryApiKey": "k1"})), Some("k1"));
        let said = json!({"claudeAiOauth": {"refreshToken": "r1", "accessToken": "a1"}});
        assert_eq!(credentials(&said), Some("r1"));
        assert_eq!(credentials(&json!({})), Some(""));
    }
}
