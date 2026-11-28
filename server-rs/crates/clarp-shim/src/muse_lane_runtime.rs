//! One process holds an agent's Muse session between its chat turns (issue 762): one
//! `muse serve`, spoken to in MSP. This is the Muse driver of the lane layer; what every lane
//! keeps for itself (its options, locks, turn file, idle clock, login and sockets) is in the
//! `lane_*` modules, and how a turn is written to its spool in `clarp_shim::muse_turn`.
//!
//! The lane starts *without* its session: an exec turn may hold it, and Muse refuses a second
//! process on a session (its own `.session.lock`). It takes the session when the Host says the
//! exec turn is over ([`Request::Take`]), or at its first prompt.

use std::collections::{BTreeMap, BTreeSet, VecDeque};
use std::fs;
use std::io::{self, Write};
use std::os::unix::net::UnixStream;
use std::path::Path;
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, SyncSender};
use std::time::{Duration, Instant};

use clarp_shim::Reply;
use clarp_shim::codex_app::{AppServer, Frame};
use clarp_shim::lane::LOGIN_CHANGED;
use clarp_shim::lane_spool::ClaimSpool;
use clarp_shim::muse_lane::{Claim, Request, Snapshot, command_id, key};
use clarp_shim::muse_turn::TurnSpool;
use serde_json::{Value, json};

use crate::lane_login::Login;
use crate::lane_own::{Idle, Own, hold_owner};
use crate::lane_socket::{Socket, TurnAsk, write};

const RPC_LIMIT: Duration = Duration::from_secs(12);
/// Loading a session reads its log: a long one takes longer than a request. Below the Host's
/// own 30 s for a request (`lane::RPC_TIMEOUT`).
const LOAD_LIMIT: Duration = Duration::from_secs(25);
/// How long a lane that leaves gives `muse serve` to leave in order once its input ends.
const EXIT_CLEAN_LIMIT: Duration = Duration::from_secs(2);
const STRAY_MOST: usize = 64;
const BUFFER_MOST: usize = 512;

pub(super) enum Input {
    Provider(io::Result<Frame>),
    ProviderGone,
    Control(Request, UnixStream),
    Follow((u64, u32), UnixStream),
    Stop((u64, u32), u64, UnixStream),
    Steer((u64, u32), String, String, UnixStream),
    /// A request the lane's turn socket takes from no one (a line for an input Muse has not).
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
            TurnAsk::Steer(id, text) => Self::Steer(attempt, id, text, stream),
            // The socket is made refusing a line for the input; one that came all the same is
            // refused here, never taken for another request.
            TurnAsk::Stdin(_) => Self::Refused(stream),
        }
    }
}

/// The account a Muse login file names: a refreshed token of the same account keeps the lane.
fn account(auth: &Value) -> Option<&str> {
    auth["providers"]["meta"]["user_email"].as_str()
}

/// The single turn a lane runs.
enum Slot {
    Free,
    /// The spool is made and `turn/start` is sent; Muse has not named the turn yet.
    Pending {
        claim: Claim,
        spool: ClaimSpool,
    },
    Running {
        claim: Claim,
        spool: TurnSpool,
    },
}

impl Slot {
    fn claim(&self) -> Option<&Claim> {
        match self {
            Self::Free => None,
            Self::Pending { claim, .. } | Self::Running { claim, .. } => Some(claim),
        }
    }
}

pub(super) fn from_args(mut args: impl Iterator<Item = String>) -> io::Result<()> {
    let (Some(socket), Some(program), Some(options), None) =
        (args.next(), args.next(), args.next(), args.next())
    else {
        return Err(io::Error::other(
            "usage: muse-lane SOCKET MUSE OPTIONS_FILE",
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
    let (send, input) = mpsc::sync_channel(64);
    let _listener = Socket::control(path, send.clone(), Input::control)?;
    let login = Login::read(&mut options, |value| account(value).map(str::to_owned))?;
    let session = options[key::SESSION]
        .as_str()
        .unwrap_or_default()
        .to_owned();
    if session.is_empty() {
        return Err(io::Error::other("a Muse lane needs its session"));
    }
    let model = options[key::MODEL].as_str().map(str::to_owned);
    // Started through the shim, so `muse serve` ends with this lane however the lane ends (#772).
    let mut command = crate::tool_child::command(program);
    command.args(["serve", "--disable-sandbox", "--trust-workspace"]);
    for setting in options[key::CONFIG].as_array().into_iter().flatten() {
        if let Some(setting) = setting.as_str() {
            command.args(["-c", setting]);
        }
    }
    if let Some(model) = &model {
        command.args(["--model", model]);
    }
    if let Some(cwd) = options[key::CWD].as_str() {
        command.current_dir(cwd);
    }
    let (server, frames) = AppServer::spawn_jsonrpc(&mut command)?;
    let relay = send.clone();
    std::thread::Builder::new()
        .name("muse-lane-frames".into())
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
        idle: Idle::new(own.idle),
        own,
        login,
        session,
        loaded: false,
        given_model: model.clone(),
        running_model: model,
        effort: None,
        allowance: None,
        slot: Slot::Free,
        early: Vec::new(),
        cancel_on_start: BTreeSet::new(),
        stop_deadline: None,
        stray: BTreeMap::new(),
        deferred: VecDeque::new(),
        input,
        send,
        turn_socket: None,
        followers: Vec::new(),
        retire_reply: None,
        stale_login: false,
        memory: None,
    };
    let init = lane.server.request(
        "initialize",
        json!({
            "clientInfo": {"name": "clarp", "title": "Clarp", "version": "1"},
            "capabilities": {"userInputDialogs": false},
        }),
    )?;
    lane.response(init, LOAD_LIMIT)?;
    lane.server.notify("initialized", None)?;
    lane.weigh();
    // Ready without the session: the Host says when it may be taken.
    println!("ready");
    io::stdout().flush()?;
    let result = lane.serve();
    if let Slot::Running { spool, .. } = &mut lane.slot {
        let _ = spool.lost("Muse serve disconnected", crate::now_ms());
    }
    let retired = lane
        .retire_reply
        .take()
        .map(|stream| (stream, lane.inspect()));
    // Muse leaves in order when its input ends, its session log written to its end; one that
    // does not within the bound is ended where it stands, and its lock on the session with it.
    lane.server.close(EXIT_CLEAN_LIMIT);
    drop(lane);
    drop(lock);
    if let Some((mut stream, state)) = retired {
        let _ = write(&mut stream, &json!({"ok": true, "state": state}));
    }
    result
}

struct Lane {
    /// Dropped first: Muse is reaped before the owner lock is let go.
    server: AppServer,
    own: Own,
    login: Login,
    idle: Idle,
    session: String,
    /// Muse holds the session for this lane.
    loaded: bool,
    /// The model the lane gave Muse, which it keeps for the turns after.
    given_model: Option<String>,
    /// The model Muse runs the session on, as it said.
    running_model: Option<String>,
    effort: Option<String>,
    /// The allowance Muse said last, for the next turn's usage.
    allowance: Option<Value>,
    slot: Slot,
    /// What Muse said of the session while `turn/start` was answered.
    early: Vec<(String, Value)>,
    /// Steered words that could not be taken back before Muse began them as a turn of their
    /// own: that turn is interrupted and retracted as soon as it begins. They were handed back.
    cancel_on_start: BTreeSet<String>,
    stop_deadline: Option<(Instant, (u64, u32))>,
    stray: BTreeMap<u64, Result<Value, String>>,
    deferred: VecDeque<Input>,
    input: Receiver<Input>,
    send: SyncSender<Input>,
    turn_socket: Option<Socket>,
    followers: Vec<UnixStream>,
    retire_reply: Option<UnixStream>,
    stale_login: bool,
    memory: Option<u64>,
}

#[path = "muse_lane_turns.rs"]
mod turns;
