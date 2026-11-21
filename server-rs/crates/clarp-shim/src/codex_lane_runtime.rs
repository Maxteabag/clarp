//! One process owns a goal-enabled thread between its turns. The Host's
//! reservation, Codex's request replies, and the spool writer meet here.

use std::collections::VecDeque;
use std::fs::{self, OpenOptions};
use std::io::{self, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::process::Command;
use std::sync::mpsc::{self, Receiver, SyncSender};
use std::time::{Duration, Instant};

use clarp_shim::Reply;
use clarp_shim::codex_app::{AppServer, Frame};
use clarp_shim::codex_lane::{Claim, GoalAction, Request, Reservation, Snapshot};
use serde_json::{Value, json};

use crate::codex_lane_login::Login;
use crate::codex_lane_socket::{Socket, write};
use crate::codex_lane_turns::Slot;

const RPC_LIMIT: Duration = Duration::from_secs(12);
const BUFFER_MOST: usize = 512;

pub(super) enum Input {
    Provider(io::Result<Frame>),
    ProviderGone,
    Control(Request, UnixStream),
    Follow((u64, u32), UnixStream),
    Stop((u64, u32), UnixStream),
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
    let thread = options["threadId"]
        .as_str()
        .filter(|thread| !thread.is_empty())
        .ok_or_else(|| io::Error::other("a Codex lane needs a native thread"))?
        .to_owned();
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
    let thread_lock = options
        .as_object_mut()
        .and_then(|fields| fields.remove("_clarpThreadLock"));
    let thread_lock = match thread_lock.as_ref().and_then(Value::as_str) {
        Some(path) => {
            let file = OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .mode(0o600)
                .open(path)?;
            file.try_lock().map_err(io::Error::other)?;
            Some(file)
        }
        None => None,
    };
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

    let mut lane = Lane {
        server,
        revision: 0,
        input,
        send,
        options,
        thread,
        goal: None,
        loaded: false,
        known_goal: false,
        fenced: false,
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
    // The lock witnesses the app-server's real lifetime, including shutdown.
    drop(lane);
    drop(thread_lock);
    drop(lock);
    if let Some((mut stream, state)) = retired {
        let _ = write(&mut stream, &json!({"ok": true, "state": state}));
    }
    result
}

struct Lane {
    server: AppServer,
    revision: u64,
    input: Receiver<Input>,
    send: SyncSender<Input>,
    options: Value,
    thread: String,
    goal: Option<Value>,
    loaded: bool,
    known_goal: bool,
    fenced: bool,
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
            goal: self.goal.clone(),
            claim: self.slot.claim().cloned(),
            native_turn: self.slot.native_turn().map(str::to_owned),
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

    fn serve(&mut self) -> io::Result<()> {
        loop {
            let input = self.deferred.pop_front().or_else(|| self.input.recv().ok());
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
                Some(Input::Stop(attempt, mut stream)) => {
                    let reply = match self.stop(attempt) {
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
            Request::Reserve { claim } => self.reserve(claim),
            Request::Prompt {
                claim,
                text,
                model,
                effort,
            } => self.prompt(claim, text, model, effort),
            Request::Goal { action, objective } => self.goal(action, objective),
            Request::Recover => self.recover(),
            Request::Stop { turn, attempt } => self.stop((turn, attempt)),
            Request::Retire if self.slot.claim().is_none() && !self.may_continue() => {
                self.retire_reply = Some(stream);
                return Ok(true);
            }
            Request::Retire => Err(io::Error::other("the Codex lane still owns live work")),
        };
        let reply = match result {
            Ok(()) => json!({"ok": true, "state": self.inspect()}),
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

    fn reserve(&mut self, claim: Claim) -> io::Result<()> {
        if self.slot.claim() == Some(&claim) {
            return Ok(());
        }
        if self.slot.claim().is_some() {
            return Err(io::Error::other("the Codex lane is already reserved"));
        }
        let socket = Socket::turn(&claim, self.send.clone())?;
        self.use_login()?;
        self.slot.reserve(Reservation::Goal(claim))?;
        self.turn_socket = Some(socket);
        Ok(())
    }

    fn prompt(
        &mut self,
        claim: Claim,
        text: String,
        model: Option<String>,
        effort: Option<String>,
    ) -> io::Result<()> {
        if self.slot.claim() == Some(&claim) {
            return Ok(());
        }
        if self.slot.claim().is_some() {
            return Err(io::Error::other("the Codex lane cannot start a prompt now"));
        }
        let socket = Socket::turn(&claim, self.send.clone())?;
        self.use_login()?;
        self.load()?;
        let mut params = json!({
            "threadId": self.thread,
            "input": [{"type": "text", "text": text}],
            "approvalPolicy": "never",
            "sandboxPolicy": {"type": "dangerFullAccess"},
        });
        if let Some(model) = model {
            params["model"] = json!(model);
        }
        if let Some(effort) = effort {
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
        if activating {
            self.fenced = false;
        } else if action == GoalAction::Pause {
            self.fenced = true;
        }
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
            && self
                .goal
                .as_ref()
                .is_some_and(|goal| goal["status"] == "active")
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
        if !self.loaded {
            let options = self.options.clone();
            let result = self.rpc("thread/resume", options)?;
            if result["thread"]["id"].as_str() != Some(&self.thread) {
                return Err(io::Error::other("Codex resumed a different thread"));
            }
            self.loaded = true;
        }
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
        if self.slot.is_goal_reserved()
            && (goal.is_null() || matches!(goal["status"].as_str(), Some("paused" | "complete")))
        {
            let _ = self.clear_slot();
        } else {
            self.show();
        }
    }

    fn stop(&mut self, attempt: (u64, u32)) -> io::Result<()> {
        if !self.holds(attempt) {
            return Ok(());
        }
        self.fenced = true;
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
            self.rpc(
                "turn/interrupt",
                json!({"threadId": self.thread, "turnId": native}),
            )?;
        } else {
            self.clear_slot()?;
        }
        paused
    }

    fn clear_slot(&mut self) -> io::Result<()> {
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
        let deadline = Instant::now() + RPC_LIMIT;
        loop {
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
            Frame::Reply { .. } => Ok(()), // acknowledgement of a fence
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
                self.fenced = true;
                if !self.loaded
                    || self
                        .goal
                        .as_ref()
                        .is_some_and(|goal| goal["status"] == "active")
                {
                    self.server.request(
                        "thread/goal/set",
                        json!({"threadId": self.thread, "status": "paused"}),
                    )?;
                }
                self.server.request(
                    "turn/interrupt",
                    json!({"threadId": self.thread, "turnId": native}),
                )?;
            } else {
                self.show();
            }
        }
        if self.slot.notification(method, params, now)? {
            self.clear_slot()?;
        } else {
            self.hint();
        }
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
