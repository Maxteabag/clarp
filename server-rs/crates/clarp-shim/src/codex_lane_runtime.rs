//! One process owns an agent's Codex thread between its turns: its chat turns, and a native
//! goal's. The Host's reservation, Codex's request replies, and the spool writer meet here.

use std::collections::{BTreeMap, VecDeque};
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, SyncSender};
use std::time::{Duration, Instant};

use clarp_shim::Reply;
use clarp_shim::codex_app::{AppServer, Frame};
use clarp_shim::codex_lane::{
    Claim, GoalAction, LOGIN_CHANGED, Request, Reservation, Snapshot, thread_lock,
};
use serde_json::{Value, json};

use crate::codex_lane_login::Login;
use crate::codex_lane_socket::{Socket, write};
use crate::codex_lane_turns::Slot;

const RPC_LIMIT: Duration = Duration::from_secs(12);
/// Loading a thread reads its whole rollout: a long one takes longer than a request. Below
/// the Host's own 30 s for a first request (`codex_goal_lane::RPC_TIMEOUT`).
const LOAD_LIMIT: Duration = Duration::from_secs(25);
/// Replies nobody waited for yet, kept for a request still being waited on further out.
const STRAY_MOST: usize = 64;
const BUFFER_MOST: usize = 512;
/// How long Codex has to end a turn after `turn/interrupt` when the Stop named no time.
const STOP_GRACE: Duration = Duration::from_secs(5);

pub(super) enum Input {
    Provider(io::Result<Frame>),
    ProviderGone,
    Control(Request, UnixStream),
    Follow((u64, u32), UnixStream),
    /// A Stop on the attempt's socket, with how long Codex has to end the turn itself.
    Stop((u64, u32), u64, UnixStream),
    /// Steered words for the attempt, under the id their reading is told by.
    Steer((u64, u32), String, String, UnixStream),
}

/// What the Host starts a lane with that is the lane's own, taken out of Codex's options.
struct Own {
    /// Where the locks of native threads are: the thread a lane starts is locked there.
    thread_locks: Option<PathBuf>,
    /// A thread's lock, named by the Host for a lane that resumes it.
    thread_lock: Option<PathBuf>,
    /// How long the lane lives with no claim and no goal that may continue.
    idle: Option<Duration>,
    /// Where the identity of the lane's running turn is kept for the Clarp tools it calls.
    turn_file: Option<PathBuf>,
    /// The Host's digest of the options, told back in every snapshot.
    digest: String,
}

impl Own {
    fn take(options: &mut Value) -> Self {
        let Some(fields) = options.as_object_mut() else {
            return Self {
                thread_locks: None,
                thread_lock: None,
                idle: None,
                turn_file: None,
                digest: String::new(),
            };
        };
        let path = |value: Option<Value>| value?.as_str().map(PathBuf::from);
        Self {
            thread_locks: path(fields.remove("_clarpThreadLockDir")),
            thread_lock: path(fields.remove("_clarpThreadLock")),
            idle: fields
                .remove("_clarpIdleMs")
                .and_then(|ms| ms.as_u64())
                .filter(|ms| *ms > 0)
                .map(Duration::from_millis),
            turn_file: path(fields.remove("_clarpTurnFile")),
            digest: fields
                .remove("_clarpOptionsDigest")
                .and_then(|digest| digest.as_str().map(str::to_owned))
                .unwrap_or_default(),
        }
    }
}

fn lock_exclusive(path: &Path) -> io::Result<File> {
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(path)?;
    file.try_lock().map_err(io::Error::other)?;
    Ok(file)
}

pub(super) fn from_args(mut args: impl Iterator<Item = String>) -> io::Result<()> {
    let (Some(socket), Some(program), Some(options), None) =
        (args.next(), args.next(), args.next(), args.next())
    else {
        return Err(io::Error::other(
            "usage: codex-goal-lane SOCKET CODEX OPTIONS_FILE",
        ));
    };
    let options = serde_json::from_reader(fs::File::open(options)?).map_err(io::Error::other)?;
    run(Path::new(&socket), &program, options)
}

fn run(path: &Path, program: &str, mut options: Value) -> io::Result<()> {
    let _ = nix::unistd::setsid();
    // Empty for a new conversation: the lane starts the thread with its first prompt.
    let thread = options["threadId"].as_str().unwrap_or_default().to_owned();
    let own = Own::take(&mut options);
    if let Some(parent) = path.parent() {
        fs::create_dir_all(parent)?;
    }
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .mode(0o600)
        .open(path.with_extension("lock"))?;
    lock.try_lock().map_err(io::Error::other)?;
    let named_lock = match (&own.thread_lock, &own.thread_locks) {
        (Some(path), _) => Some(path.clone()),
        (None, Some(dir)) if !thread.is_empty() => Some(thread_lock(dir, &thread)),
        _ => None,
    };
    let held_thread = named_lock.as_deref().map(lock_exclusive).transpose()?;
    match fs::remove_file(path) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let (send, input) = mpsc::sync_channel(64);
    let _listener = Socket::control(path, send.clone())?;
    let login = Login::read(&mut options)?;
    let (server, frames) = AppServer::spawn(Command::new(program).args(["app-server", "--stdio"]))?;
    let relay = send.clone();
    std::thread::Builder::new()
        .name("codex-lane-frames".into())
        .spawn(move || {
            for frame in frames {
                if relay.send(Input::Provider(frame)).is_err() {
                    return;
                }
            }
            let _ = relay.send(Input::ProviderGone);
        })?;

    let model = options["model"].as_str().map(str::to_owned);
    let mut lane = Lane {
        server,
        _thread_lock: held_thread,
        own,
        model,
        effort: None,
        idle_since: Instant::now(),
        stop_deadline: None,
        stray: BTreeMap::new(),
        revision: 0,
        input,
        send,
        options,
        thread,
        goal: None,
        loaded: false,
        known_goal: false,
        fenced: false,
        deferred_goal: false,
        rejected_turn: None,
        fence_pause: None,
        uncertain_goal: false,
        stale_login: false,
        login,
        slot: Slot::default(),
        turn_socket: None,
        watchers: Vec::new(),
        followers: Vec::new(),
        retire_reply: None,
        deferred: VecDeque::new(),
        early: Vec::new(),
    };
    let init = lane.server.initialize()?;
    lane.response(init, None)?;
    lane.server.initialized()?;
    // The supervisor is ready, but no thread is loaded until the actor says so.
    println!("ready");
    io::stdout().flush()?;
    let result = lane.serve();
    let _ = lane.slot.lost(super::now_ms());
    let retired = lane
        .retire_reply
        .take()
        .map(|stream| (stream, lane.inspect()));
    // The locks witness the app-server's real lifetime, including shutdown: the lane's
    // fields drop in order, the app-server before the thread's lock.
    drop(lane);
    drop(lock);
    if let Some((mut stream, state)) = retired {
        let _ = write(&mut stream, &json!({"ok": true, "state": state}));
    }
    result
}

struct Lane {
    /// Dropped first: Codex is reaped before any lock is let go.
    server: AppServer,
    /// Held, never read: let go only after `server` has been reaped.
    _thread_lock: Option<File>,
    own: Own,
    /// The model and effort Codex was last given, which it keeps for the turns after.
    model: Option<String>,
    effort: Option<String>,
    /// Since when the lane has held no claim.
    idle_since: Instant,
    /// The attempt a Stop interrupted, and when Codex is ended if the turn still runs.
    stop_deadline: Option<(Instant, (u64, u32))>,
    /// Replies that came while another request was waited on, by request id. A request made
    /// while handling a notification (`check_reads`) must not swallow the reply that an
    /// outer request waits for (review of 23 Nov 2026).
    stray: BTreeMap<u64, Result<Value, String>>,
    revision: u64,
    input: Receiver<Input>,
    send: SyncSender<Input>,
    options: Value,
    thread: String,
    goal: Option<Value>,
    loaded: bool,
    known_goal: bool,
    fenced: bool,
    deferred_goal: bool,
    rejected_turn: Option<String>,
    fence_pause: Option<u64>,
    uncertain_goal: bool,
    stale_login: bool,
    login: Login,
    slot: Slot,
    turn_socket: Option<Socket>,
    watchers: Vec<UnixStream>,
    followers: Vec<UnixStream>,
    retire_reply: Option<UnixStream>,
    deferred: VecDeque<Input>,
    early: Vec<(String, Value)>,
}

impl Lane {
    fn inspect(&mut self) -> Snapshot {
        self.revision = self.revision.saturating_add(1);
        Snapshot {
            thread: self.thread.clone(),
            instance: std::process::id(),
            revision: self.revision,
            known_goal: self.known_goal,
            deferred_goal: self.deferred_goal,
            available: self.slot.claim().is_none() && self.rejected_turn.is_none(),
            goal: self.goal.clone(),
            claim: self.slot.claim().cloned(),
            native_turn: self.slot.native_turn().map(str::to_owned),
            options: self.own.digest.clone(),
            model: self.model.clone(),
            effort: self.effort.clone(),
            idle_ms: if self.slot.claim().is_none() {
                u64::try_from(self.idle_since.elapsed().as_millis()).unwrap_or(u64::MAX)
            } else {
                0
            },
        }
    }

    fn show(&mut self) {
        let snapshot = self.inspect();
        self.watchers
            .retain_mut(|stream| write(stream, &snapshot).is_ok());
    }

    fn hint(&mut self) {
        self.followers
            .retain_mut(|stream| match stream.write(b"\n") {
                Ok(_) => true,
                Err(error) => error.kind() == io::ErrorKind::WouldBlock,
            });
    }

    /// When the lane next has something to do by itself: end a turn that did not answer a
    /// Stop, or leave after its idle time.
    fn deadline(&self) -> Option<Instant> {
        if let Some((at, _)) = self.stop_deadline {
            return Some(at);
        }
        self.own
            .idle
            .filter(|_| self.retirable())
            .map(|idle| self.idle_since + idle)
    }

    fn retirable(&self) -> bool {
        self.slot.claim().is_none() && self.rejected_turn.is_none() && !self.may_continue()
    }

    /// True: the lane's idle time is over, and it leaves as if it were retired.
    fn expired(&mut self) -> io::Result<bool> {
        if let Some((at, attempt)) = self.stop_deadline {
            if Instant::now() < at {
                return Ok(false);
            }
            self.stop_deadline = None;
            if self.holds(attempt) && self.slot.native_turn().is_some() {
                // D26: the tool's own way first, bounded, then its group.
                self.server.kill_group();
                self.slot.killed(super::now_ms())?;
                self.hint();
                self.followers.clear();
                self.turn_socket = None;
                return Err(io::Error::other("Codex did not end its turn after a Stop"));
            }
            return Ok(false);
        }
        Ok(self.retirable()
            && self
                .own
                .idle
                .is_some_and(|idle| self.idle_since.elapsed() >= idle))
    }

    fn next_input(&mut self) -> io::Result<Option<Input>> {
        loop {
            // A Codex that keeps talking after a Stop must not put off its end: the deadline
            // is looked at before anything that waits is taken (review of 23 Nov 2026).
            if self
                .stop_deadline
                .is_some_and(|(at, _)| Instant::now() >= at)
                && self.expired()?
            {
                return Err(io::Error::new(io::ErrorKind::TimedOut, "idle"));
            }
            if let Some(input) = self.deferred.pop_front() {
                return Ok(Some(input));
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
                // A lane that was idle for its time leaves as a retired one does.
                Err(error) if error.kind() == io::ErrorKind::TimedOut => return Ok(()),
                Err(error) => return Err(error),
            };
            match input {
                Some(Input::Provider(frame)) => {
                    self.frame(frame?, None)?;
                }
                Some(Input::Control(request, stream)) => {
                    if self.control(request, stream)? {
                        return Ok(());
                    }
                }
                Some(Input::Follow(attempt, mut stream)) => {
                    if self.holds(attempt) && self.followers.len() < 8 {
                        let _ = write(&mut stream, &Reply::ok(None));
                        let _ = stream.set_nonblocking(true);
                        self.followers.push(stream);
                    }
                }
                Some(Input::Stop(attempt, grace_ms, mut stream)) => {
                    let reply = match self.stop(attempt, Duration::from_millis(grace_ms)) {
                        Ok(()) => Reply::ok(None),
                        Err(error) => Reply::failed(error.to_string()),
                    };
                    let _ = write(&mut stream, &reply);
                }
                Some(Input::Steer(attempt, id, text, mut stream)) => {
                    let reply = match self.steer(attempt, &id, &text) {
                        Ok(()) => Reply::ok(None),
                        Err(error) => Reply::failed(error.to_string()),
                    };
                    let _ = write(&mut stream, &reply);
                }
                Some(Input::ProviderGone) | None => {
                    return Err(io::Error::other("Codex app-server disconnected"));
                }
            }
        }
    }

    fn holds(&self, (turn, attempt): (u64, u32)) -> bool {
        self.slot
            .claim()
            .is_some_and(|claim| (claim.turn, claim.attempt) == (turn, attempt))
    }

    fn control(&mut self, request: Request, mut stream: UnixStream) -> io::Result<bool> {
        let result = match request {
            Request::Inspect | Request::Watch => {
                let _ = write(&mut stream, &json!({"ok": true, "state": self.inspect()}));
                if matches!(request, Request::Watch) && self.watchers.len() < 4 {
                    let _ = stream.set_nonblocking(true);
                    self.watchers.push(stream);
                }
                return Ok(false);
            }
            Request::Probe => self.probe(),
            Request::Reserve { claim, token } => self.reserve(claim, token.as_deref()),
            Request::Prompt {
                claim,
                text,
                model,
                effort,
                token,
            } => self.prompt(claim, text, model, effort, token.as_deref()),
            Request::Goal { action, objective } => self.goal(action, objective),
            Request::Recover => self.recover(),
            Request::Stop { turn, attempt } => self.stop((turn, attempt), STOP_GRACE),
            Request::Retire
                if self.slot.claim().is_none()
                    && self.rejected_turn.is_none()
                    && !self.may_continue() =>
            {
                self.retire_reply = Some(stream);
                return Ok(true);
            }
            Request::Retire => Err(io::Error::other("the Codex lane still owns live work")),
        };
        let reply = match result {
            Ok(()) => json!({"ok": true, "state": self.inspect()}),
            // A changed login is said with a code: nothing reached Codex, and the Host may
            // give the same attempt to the lane that starts after this one.
            Err(error) if self.stale_login => {
                json!({"ok": false, "error": error.to_string(), "code": LOGIN_CHANGED})
            }
            Err(error) => json!({"ok": false, "error": error.to_string()}),
        };
        let _ = write(&mut stream, &reply);
        if self.slot.claim().is_none() {
            self.login.release();
        }
        self.show();
        Ok(self.stale_login)
    }

    fn use_login(&mut self) -> io::Result<()> {
        if !self.login.acquire()? {
            self.stale_login = true;
            return Err(io::Error::other(
                "the Codex login changed before this attempt",
            ));
        }
        Ok(())
    }

    fn reserve(&mut self, claim: Claim, token: Option<&str>) -> io::Result<()> {
        if self.slot.claim() == Some(&claim) {
            return Ok(());
        }
        if self.slot.claim().is_some() || self.rejected_turn.is_some() {
            return Err(io::Error::other("the Codex lane is already reserved"));
        }
        let socket = Socket::turn(&claim, self.send.clone())?;
        self.use_login()?;
        self.keep_token(token);
        self.slot.reserve(Reservation::Goal(claim))?;
        self.turn_socket = Some(socket);
        self.idle_since = Instant::now();
        Ok(())
    }

    /// The identity of the turn the lane now runs, for the Clarp tools it calls: written whole
    /// by rename, readable by the owner only, so a tool never reads half of one. A file that
    /// cannot be written leaves the turn to run: its tools then present the one before.
    fn keep_token(&self, token: Option<&str>) {
        let (Some(path), Some(token)) = (self.own.turn_file.as_deref(), token) else {
            return;
        };
        let written = (|| {
            let partial = path.with_extension(format!("{}.part", std::process::id()));
            // A file left by a lane of the same pid before a crash is not written through.
            match fs::remove_file(&partial) {
                Ok(()) => {}
                Err(error) if error.kind() == io::ErrorKind::NotFound => {}
                Err(error) => return Err(error),
            }
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&partial)?;
            file.write_all(token.as_bytes())?;
            file.sync_all()?;
            fs::rename(&partial, path)
        })();
        if let Err(error) = written {
            eprintln!("clarp-shim: Codex lane: the turn's identity was not written: {error}");
        }
    }

    fn prompt(
        &mut self,
        claim: Claim,
        text: String,
        model: Option<String>,
        effort: Option<String>,
        token: Option<&str>,
    ) -> io::Result<()> {
        if self.slot.claim() == Some(&claim) {
            return Ok(());
        }
        if self.slot.claim().is_some() || self.rejected_turn.is_some() {
            return Err(io::Error::other("the Codex lane cannot start a prompt now"));
        }
        let socket = Socket::turn(&claim, self.send.clone())?;
        self.use_login()?;
        self.load()?;
        self.keep_token(token);
        self.idle_since = Instant::now();
        let mut params = json!({
            "threadId": self.thread,
            "input": [{"type": "text", "text": text}],
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
        });
        if let Some(model) = &model {
            params["model"] = json!(model);
        }
        if let Some(effort) = &effort {
            params["effort"] = json!(effort);
        }
        if let Some(cwd) = self.options.get("cwd") {
            params["cwd"] = cwd.clone();
        }
        let request = self.server.next_request();
        self.slot
            .reserve(Reservation::PromptPending { claim, request })?;
        self.turn_socket = Some(socket);
        let id = self.server.request("turn/start", params)?;
        let result = self.response(id, Some(id));
        let accepted = result
            .as_ref()
            .ok()
            .and_then(|result| result["turn"]["id"].as_str());
        if accepted.is_some() {
            // Codex keeps what a turn was given for the turns after it.
            self.model = model.or(self.model.take());
            self.effort = effort.or(self.effort.take());
        }
        if let Some(native) = accepted {
            self.slot.bind(
                Some(id),
                &self.thread,
                native,
                self.server.pid(),
                super::now_ms(),
            )?;
        }
        for (method, params) in std::mem::take(&mut self.early) {
            self.notice(&method, &params, None)?;
        }
        if accepted.is_none() {
            self.clear_slot()?;
            return match result {
                Err(error) => Err(error),
                Ok(_) => Err(io::Error::other("Codex supplied no turn ID")),
            };
        }
        Ok(())
    }

    fn goal(&mut self, action: GoalAction, objective: String) -> io::Result<()> {
        let activating = matches!(action, GoalAction::Start | GoalAction::Resume);
        if activating && !self.slot.is_goal_reserved() {
            return Err(io::Error::other(
                "a goal activation needs an admitted actor slot",
            ));
        }
        self.deferred_goal = false;
        self.fence_pause = None;
        self.fenced = !activating;
        self.use_login()?;
        self.load()?;
        let (method, params) = action.request(&self.thread, objective);
        self.uncertain_goal = true;
        let result = self.rpc(method, params)?;
        self.uncertain_goal = false;
        if action == GoalAction::Clear {
            self.apply_goal(&Value::Null);
        } else if let Some(goal) = result.get("goal") {
            self.apply_goal(goal);
        } else {
            self.uncertain_goal = true;
            return Err(io::Error::other("Codex supplied no goal result"));
        }
        Ok(())
    }

    fn recover(&mut self) -> io::Result<()> {
        if !self.slot.is_goal_reserved() {
            return Err(io::Error::other(
                "goal recovery needs an admitted actor slot",
            ));
        }
        let already_loaded = self.loaded;
        self.load()?;
        self.probe()?;
        if already_loaded
            && self.slot.is_goal_reserved()
            && (self.deferred_goal
                || self
                    .goal
                    .as_ref()
                    .is_some_and(|goal| goal["status"] == "active"))
        {
            self.goal(GoalAction::Resume, String::new())?;
        }
        Ok(())
    }

    fn probe(&mut self) -> io::Result<()> {
        self.use_login()?;
        let result = self.rpc("thread/goal/get", json!({"threadId": self.thread}))?;
        let goal = result
            .get("goal")
            .ok_or_else(|| io::Error::other("Codex supplied no goal result"))?;
        self.apply_goal(goal);
        self.uncertain_goal = false;
        Ok(())
    }

    fn load(&mut self) -> io::Result<()> {
        if self.loaded {
            return Ok(());
        }
        if self.thread.is_empty() {
            return self.start_thread();
        }
        let options = self.options.clone();
        let id = self.server.request("thread/resume", options)?;
        let result = self.response_within(id, None, LOAD_LIMIT)?;
        if result["thread"]["id"].as_str() != Some(&self.thread) {
            return Err(io::Error::other("Codex resumed a different thread"));
        }
        self.loaded = true;
        Ok(())
    }

    /// A new conversation's thread, started with the options a resumed one is given. Nobody
    /// else knows its name yet; the lane takes its lock before it names it to anyone.
    fn start_thread(&mut self) -> io::Result<()> {
        let mut params = self.options.clone();
        if let Some(fields) = params.as_object_mut() {
            fields.remove("threadId");
            fields.remove("excludeTurns");
        }
        let id = self.server.request("thread/start", params)?;
        let result = self.response_within(id, None, LOAD_LIMIT)?;
        let thread = result["thread"]["id"]
            .as_str()
            .filter(|thread| !thread.is_empty())
            .ok_or_else(|| io::Error::other("Codex started no thread"))?
            .to_owned();
        if let Some(dir) = &self.own.thread_locks {
            self._thread_lock = Some(lock_exclusive(&thread_lock(dir, &thread))?);
        }
        self.thread = thread;
        self.loaded = true;
        Ok(())
    }

    fn may_continue(&self) -> bool {
        self.uncertain_goal
            || self
                .goal
                .as_ref()
                .is_some_and(|goal| goal["status"] != "complete")
    }

    fn apply_goal(&mut self, goal: &Value) {
        if goal.is_object() {
            let older = self.goal.as_ref().is_some_and(|current| {
                let current = goal_time(&current["updatedAt"]);
                let update = goal_time(&goal["updatedAt"]);
                current
                    .zip(update)
                    .is_some_and(|(before, after)| after < before)
            });
            if older {
                return;
            }
            self.goal = Some(goal.clone());
        } else if goal.is_null() {
            self.goal = None;
        } else {
            return;
        }
        self.known_goal = true;
        if goal.is_null() || goal["status"] == "complete" {
            self.deferred_goal = false;
        }
        if self.slot.is_goal_reserved()
            && !self.deferred_goal
            && (goal.is_null() || matches!(goal["status"].as_str(), Some("paused" | "complete")))
        {
            let _ = self.clear_slot();
        } else {
            self.show();
        }
    }

    fn stop(&mut self, attempt: (u64, u32), grace: Duration) -> io::Result<()> {
        if !self.holds(attempt) {
            return Ok(());
        }
        self.fenced = true;
        self.deferred_goal = false;
        self.fence_pause = None;
        let native = self.slot.native_turn().map(str::to_owned);
        // Pause before interrupting. Even on a refusal, attempt the interrupt
        // and keep the fence against the next unsolicited notification.
        let paused = if self.uncertain_goal
            || self
                .goal
                .as_ref()
                .is_some_and(|goal| goal["status"] == "active")
        {
            let result = self.rpc(
                "thread/goal/set",
                json!({"threadId": self.thread, "status": "paused"}),
            );
            if let Ok(result) = &result {
                self.apply_goal(&result["goal"]);
            }
            result.map(|_| ())
        } else {
            Ok(())
        };
        if let Some(native) = native {
            let interrupted = self.rpc(
                "turn/interrupt",
                json!({"threadId": self.thread, "turnId": native}),
            );
            // Codex has the Stop's time to end the turn: at once when it did not answer.
            if self.holds(attempt) {
                let grace = if interrupted.is_ok() {
                    grace
                } else {
                    Duration::ZERO
                };
                self.stop_deadline = Some((Instant::now() + grace, attempt));
            }
            interrupted?;
        } else {
            self.clear_slot()?;
        }
        paused
    }

    /// More of the user's words for the running turn. Codex refuses them when that turn is
    /// no longer the active one (`expectedTurnId`), and then they wait for a turn of their
    /// own. Accepted, they are read once their receipt is in the spool.
    fn steer(&mut self, attempt: (u64, u32), id: &str, text: &str) -> io::Result<()> {
        if !self.holds(attempt) {
            return Err(io::Error::other(
                "the Codex lane does not hold this attempt",
            ));
        }
        if self.stop_deadline.is_some() {
            return Err(io::Error::other("the turn is being stopped"));
        }
        let Some(native) = self.slot.native_turn().map(str::to_owned) else {
            return Err(io::Error::other("the Codex turn has not started"));
        };
        if id.is_empty() || text.trim().is_empty() {
            return Err(io::Error::other("steered words need an id and a text"));
        }
        self.rpc(
            "turn/steer",
            json!({
                "threadId": self.thread,
                "expectedTurnId": native,
                "clientUserMessageId": id,
                "input": [{"type": "text", "text": text}],
            }),
        )?;
        self.slot.steered(id);
        Ok(())
    }

    /// Before a turn's end is written: whether Codex took in steered words it did not say it
    /// read. Codex 0.167.1 says each at once, and drops what an interrupt caught before it
    /// was read (23 Nov 2026); asking keeps a message from being lost or run twice should a
    /// later Codex differ. Returns what Codex said meanwhile, to be handled after the end.
    fn check_reads(&mut self) -> Vec<(String, Value)> {
        let unread = self.slot.unread();
        let Some(native) = self.slot.native_turn().map(str::to_owned) else {
            return Vec::new();
        };
        if unread.is_empty() {
            return Vec::new();
        }
        let Ok(id) = self
            .server
            .request(
                "thread/turns/list",
                json!({"threadId": self.thread, "limit": 3, "sortDirection": "desc", "itemsView": "full"}),
            )
        else {
            return Vec::new();
        };
        let outer = std::mem::take(&mut self.early);
        let result = self.response(id, Some(id));
        let later = std::mem::replace(&mut self.early, outer);
        let Ok(result) = result else {
            return later;
        };
        let turns = result["data"].as_array().cloned().unwrap_or_default();
        let read: Vec<String> = turns
            .iter()
            .filter(|turn| turn["id"] == native.as_str())
            .flat_map(|turn| turn["items"].as_array().cloned().unwrap_or_default())
            .filter(|item| item["type"] == "userMessage")
            .filter_map(|item| item["clientId"].as_str().map(str::to_owned))
            .filter(|id| unread.contains(id))
            .collect();
        let now = super::now_ms();
        for id in read {
            if let Err(error) = self.slot.receipt(&id, now) {
                eprintln!("clarp-shim: Codex lane: a reading was not kept: {error}");
            }
        }
        later
    }

    fn clear_slot(&mut self) -> io::Result<()> {
        self.idle_since = Instant::now();
        self.stop_deadline = None;
        self.slot.cancel(super::now_ms())?;
        self.hint();
        self.followers.clear();
        self.turn_socket = None;
        self.login.release();
        self.show();
        Ok(())
    }

    fn rpc(&mut self, method: &str, params: Value) -> io::Result<Value> {
        let id = self.server.request(method, params)?;
        self.response(id, None)
    }

    fn response(&mut self, id: u64, starting: Option<u64>) -> io::Result<Value> {
        self.response_within(id, starting, RPC_LIMIT)
    }

    fn response_within(
        &mut self,
        id: u64,
        starting: Option<u64>,
        limit: Duration,
    ) -> io::Result<Value> {
        let deadline = Instant::now() + limit;
        loop {
            if let Some(result) = self.stray.remove(&id) {
                return result.map_err(io::Error::other);
            }
            let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                return Err(io::Error::new(
                    io::ErrorKind::TimedOut,
                    "Codex request timed out",
                ));
            };
            match self.input.recv_timeout(remaining) {
                Ok(Input::Provider(Ok(Frame::Reply { id: found, result }))) if found == id => {
                    return result.map_err(io::Error::other);
                }
                Ok(Input::Provider(frame)) => {
                    self.frame(frame?, starting)?;
                }
                Ok(Input::ProviderGone) => {
                    return Err(io::Error::other("Codex app-server disconnected"));
                }
                Ok(event) if self.deferred.len() < BUFFER_MOST => self.deferred.push_back(event),
                Ok(_) => return Err(io::Error::other("Codex lane request backlog exceeded")),
                Err(error) => return Err(io::Error::other(error)),
            }
        }
    }

    fn frame(&mut self, frame: Frame, starting: Option<u64>) -> io::Result<()> {
        match frame {
            Frame::Notification { method, params } => self.notice(&method, &params, starting),
            Frame::Request { id } => self.server.refuse_server_request(id),
            Frame::Reply { id, result } => {
                if self.fence_pause == Some(id) {
                    self.fence_pause = None;
                    if let Ok(reply) = result
                        && let Some(goal) = reply.get("goal")
                    {
                        self.apply_goal(goal);
                    }
                } else {
                    // Kept for a request still waited on further out. The oldest go first:
                    // what nobody waits for (an interrupt sent and not awaited) cannot pile up.
                    self.stray.insert(id, result);
                    while self.stray.len() > STRAY_MOST {
                        self.stray.pop_first();
                    }
                }
                Ok(())
            }
        }
    }

    fn notice(&mut self, method: &str, params: &Value, starting: Option<u64>) -> io::Result<()> {
        let thread = params["threadId"]
            .as_str()
            .or(params["turn"]["threadId"].as_str());
        if thread != Some(self.thread.as_str()) {
            return Ok(());
        }
        match method {
            "thread/goal/updated" => {
                if let Some(goal) = params.get("goal") {
                    self.apply_goal(goal);
                }
                return Ok(());
            }
            "thread/goal/cleared" => {
                self.apply_goal(&Value::Null);
                return Ok(());
            }
            _ => {}
        }
        if starting.is_some() {
            if self.early.len() >= BUFFER_MOST {
                return Err(io::Error::other("Codex turn notification backlog exceeded"));
            }
            self.early.push((method.to_owned(), params.clone()));
            return Ok(());
        }
        let now = super::now_ms();
        if method == "turn/started"
            && let Some(native) = params["turn"]["id"].as_str()
        {
            if self.slot.native_turn() == Some(native) {
                return Ok(());
            }
            let admitted = !self.fenced
                && self
                    .slot
                    .bind(None, &self.thread, native, self.server.pid(), now)?;
            if !admitted {
                self.reject_start(native)?;
            } else {
                self.show();
            }
        }
        if method == "turn/completed"
            && self.rejected_turn.as_deref() == params["turn"]["id"].as_str()
            && self.rejected_turn.take().is_some()
        {
            self.show();
            return Ok(());
        }
        let later = if method == "turn/completed"
            && self.slot.native_turn().is_some()
            && self.slot.native_turn() == params["turn"]["id"].as_str()
        {
            self.check_reads()
        } else {
            Vec::new()
        };
        if self.slot.notification(method, params, now)? {
            self.clear_slot()?;
        } else {
            self.hint();
        }
        for (method, params) in later {
            self.notice(&method, &params, None)?;
        }
        Ok(())
    }

    fn reject_start(&mut self, native: &str) -> io::Result<()> {
        if self.rejected_turn.as_deref() == Some(native) {
            return Ok(());
        }
        let active = self
            .goal
            .as_ref()
            .is_some_and(|goal| goal["status"] == "active");
        self.deferred_goal |= active && !self.fenced;
        self.fenced = true;
        self.rejected_turn = Some(native.to_owned());
        if (!self.loaded || active) && self.fence_pause.is_none() {
            self.fence_pause = Some(self.server.request(
                "thread/goal/set",
                json!({"threadId": self.thread, "status": "paused"}),
            )?);
        }
        self.server.request(
            "turn/interrupt",
            json!({"threadId": self.thread, "turnId": native}),
        )?;
        self.show();
        Ok(())
    }
}

fn goal_time(value: &Value) -> Option<i64> {
    let n = value.as_i64().or_else(|| value.as_str()?.parse().ok())?;
    match n {
        1..100_000_000_000 => Some(n * 1_000),
        100_000_000_000.. => Some(n),
        _ => None,
    }
}
