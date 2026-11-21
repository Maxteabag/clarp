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
use clarp_shim::codex_lane::{Claim, GoalAction, Request, Snapshot};
use serde_json::{Value, json};

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

fn run(path: &Path, program: &str, options: Value) -> io::Result<()> {
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
    match fs::remove_file(path) {
        Ok(()) => {}
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error),
    }
    let (send, input) = mpsc::sync_channel(64);
    let _listener = Socket::control(path, send.clone())?;
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
        input,
        send,
        options,
        thread,
        goal: None,
        loaded: false,
        fenced: false,
        uncertain_goal: false,
        slot: Slot::default(),
        turn_socket: None,
        watchers: Vec::new(),
        followers: Vec::new(),
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
    result
}

struct Lane {
    server: AppServer,
    input: Receiver<Input>,
    send: SyncSender<Input>,
    options: Value,
    thread: String,
    goal: Option<Value>,
    loaded: bool,
    fenced: bool,
    uncertain_goal: bool,
    slot: Slot,
    turn_socket: Option<Socket>,
    watchers: Vec<UnixStream>,
    followers: Vec<UnixStream>,
    deferred: VecDeque<Input>,
    early: Vec<(String, Value)>,
}

impl Lane {
    fn inspect(&self) -> Snapshot {
        Snapshot {
            thread: self.thread.clone(),
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
                let _ = write(&mut stream, &json!({"ok": true, "state": self.inspect()}));
                return Ok(true);
            }
            Request::Retire => Err(io::Error::other("the Codex lane still owns live work")),
        };
        let reply = match result {
            Ok(()) => json!({"ok": true, "state": self.inspect()}),
            Err(error) => json!({"ok": false, "error": error.to_string()}),
        };
        let _ = write(&mut stream, &reply);
        self.show();
        Ok(false)
    }

    fn reserve(&mut self, claim: Claim) -> io::Result<()> {
        if self.slot.claim() == Some(&claim) {
            return Ok(());
        }
        if self.slot.claim().is_some() {
            return Err(io::Error::other("the Codex lane is already reserved"));
        }
        let socket = Socket::turn(&claim, self.send.clone())?;
        if !self.slot.reserve_goal(claim) {
            return Err(io::Error::other("the lane could not reserve the goal turn"));
        }
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
        if self.slot.claim().is_some() || !self.loaded || self.fenced {
            return Err(io::Error::other("the Codex lane cannot start a prompt now"));
        }
        let socket = Socket::turn(&claim, self.send.clone())?;
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
        let id = self.server.request("turn/start", params)?;
        if !self.slot.reserve_prompt(claim, id) {
            return Err(io::Error::other(
                "the lane could not reserve the prompted turn",
            ));
        }
        self.turn_socket = Some(socket);
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
            self.clear_slot();
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
        }
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
        self.load()?;
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
            self.goal = Some(goal.clone());
        } else if goal.is_null() {
            self.goal = None;
        }
        if self.slot.is_goal_reserved()
            && (goal.is_null() || matches!(goal["status"].as_str(), Some("paused" | "complete")))
        {
            self.clear_slot();
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
            self.clear_slot();
        }
        paused
    }

    fn clear_slot(&mut self) {
        self.slot = Slot::Free;
        self.hint();
        self.followers.clear();
        self.turn_socket = None;
        self.show();
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
            self.clear_slot();
        } else {
            self.hint();
        }
        Ok(())
    }
}
