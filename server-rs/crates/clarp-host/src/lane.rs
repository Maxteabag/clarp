//! The lane layer: an agent's tool kept alive between its chat turns, in one process under the
//! shim, while the owner's switch for that tool is on (issues 742 and 761). What every lane
//! shares lives here, whatever tool it keeps:
//!
//! - the owner's settings in `config.toml`: one switch per tool, the idle time, the count of
//!   lanes and the memory budget shared by all ([`Settings`]);
//! - where a lane listens, and how it is asked, watched, retired and known to have left
//!   ([`Endpoint`]);
//! - the registry: room is made across every tool's lanes within two limits, a count and a
//!   memory budget in bytes, the longest idle leaving first;
//! - whether a running lane is used as it is or retired and started again: another thread,
//!   other options (model, prompt, provider, ...), or what its driver says it kept;
//! - the turn-identity file its tools read the running turn from ([`TURN_FILE_ENV`]);
//! - an attempt that reached nothing of the tool offered once more to a new lane, after a
//!   changed login ([`clarp_shim::lane::LOGIN_CHANGED`]) or a lane that left by itself.
//!
//! A tool's driver adds what is its own: how its process is started, what its digest covers,
//! and its requests. [`codex`] is the Codex app-server's, and the only one so far. The lane
//! process's side of the same lifecycle is the shim's `lane_*` modules. What holds with a lane
//! and without one is `server-rs/docs/lanes.md`.

pub(crate) mod codex;

use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::Duration;

use clarp_shim::lane::{LOGIN_CHANGED, Lifecycle, Status, owner_lock};
use serde::Serialize;
use serde::de::DeserializeOwned;

const MAX_STATE: u64 = 8 * 1024 * 1024;
// A first mutation may wait for thread/resume and its own result.
const RPC_TIMEOUT: Duration = Duration::from_secs(30);
/// What a lane's tools find the identity of its running turn in, in the lane's environment
/// only. A tool reads it where `CLARP_PROVIDER_TURN` is not set.
pub(crate) const TURN_FILE_ENV: &str = "CLARP_PROVIDER_TURN_FILE";

/// The tools whose chat turns can run on a lane.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Tool {
    Codex,
}

impl Tool {
    pub(crate) const ALL: [Self; 1] = [Self::Codex];

    /// The tool of an agent's backend, where that tool has a lane.
    pub(crate) fn of(backend: &str) -> Option<Self> {
        match backend {
            "codex" => Some(Self::Codex),
            _ => None,
        }
    }

    /// The owner's switch that runs this tool's chat turns on lanes.
    fn switch(self) -> &'static str {
        match self {
            Self::Codex => "codex_chat_lane",
        }
    }

    /// What ends the name of this tool's lane sockets in the run folder, after the agent's id.
    fn suffix(self) -> &'static str {
        match self {
            Self::Codex => "-cxg.sock",
        }
    }

    /// How the tool is named in the words of a refusal.
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Codex => "Codex",
        }
    }

    /// What a lane of this tool counts at in the memory budget before it has weighed itself,
    /// the one about to start among them, in bytes.
    fn typical_bytes(self) -> u64 {
        match self {
            // An idle app-server with its shim, measured by `tools/turn-timings` (issue 761).
            Self::Codex => 270 * MIB,
        }
    }
}

const MIB: u64 = 1024 * 1024;

/// The idle time is shared by every tool's lanes. It keeps the name it had when Codex had the
/// only lane, so an installed config (and `install-host`, which carries it) behaves as before.
const IDLE_MINUTES: &str = "codex_lane_idle_minutes";
/// The cap: the memory all lanes of this Host may hold together, in MiB (the lead's decision of
/// 24 Nov 2026, issue 761).
const MEMORY_MB: &str = "lane_memory_mb";
/// 2,200 MB unless the owner says: what he approved for the Codex lane (eight lanes at about
/// 270 MB each), so switching on lanes of other tools never quietly raises what he agreed to.
const MEMORY_MB_DEFAULT: u64 = 2_200;
/// The count of lanes this Host keeps, of every tool, the one about to start included: a limit
/// beside the budget, with the meaning it had when it was the only one (the lead's decision of
/// 25 Nov 2026). Idle Codex lanes measure 83-142 MiB of PSS, so the budget alone would keep
/// 15-20 of them; the count keeps the eight the owner agreed to.
const MOST: &str = "codex_lane_most";

/// The two limits all lanes of a Host are kept within, whatever their tools.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Room {
    /// Live lanes, the one about to start included.
    pub most: usize,
    /// Bytes they may hold together.
    pub budget: u64,
}

/// The owner's settings of lanes, in this Host's `config.toml`, read anew at each use so they
/// change without a build or a restart. Top-level keys, before any table:
///
/// ```toml
/// codex_chat_lane = true        # Codex chat turns run on the agent's lane (default false)
/// codex_lane_idle_minutes = 15  # a lane with no turn and no goal leaves after this long
/// codex_lane_most = 8           # live lanes this Host keeps, of every tool
/// lane_memory_mb = 2200         # and what they may hold together (PSS); a lane starts only
///                               # when both allow it, the longest idle one leaving first
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Settings {
    /// Each tool's switch, in the order of [`Tool::ALL`].
    chat: [bool; Tool::ALL.len()],
    pub idle_ms: u64,
    /// Live lanes this Host keeps, the one about to start included.
    pub most: usize,
    /// Bytes all lanes of this Host may hold together.
    pub budget: u64,
}

impl Settings {
    pub(crate) fn read() -> Self {
        let path = crate::config::host_config_dir().join("config.toml");
        Self::of(crate::owner::OwnerFile::new(Some(path)).read().as_ref())
    }

    fn of(file: Option<&toml::Table>) -> Self {
        let number = |key: &str, default: i64| {
            file.and_then(|file| file.get(key))
                .and_then(toml::Value::as_integer)
                .filter(|n| *n > 0)
                .unwrap_or(default)
        };
        Self {
            chat: Tool::ALL.map(|tool| {
                file.and_then(|file| file.get(tool.switch()))
                    .and_then(toml::Value::as_bool)
                    .unwrap_or(false)
            }),
            idle_ms: u64::try_from(number(IDLE_MINUTES, 15))
                .unwrap_or(15)
                .saturating_mul(60_000),
            most: usize::try_from(number(MOST, 8)).unwrap_or(8),
            budget: u64::try_from(number(MEMORY_MB, 0))
                .ok()
                .filter(|mb| *mb > 0)
                .unwrap_or(MEMORY_MB_DEFAULT)
                .saturating_mul(MIB),
        }
    }

    /// The limits a new lane is started within.
    pub(crate) fn room(&self) -> Room {
        Room {
            most: self.most,
            budget: self.budget,
        }
    }

    /// Whether the owner runs this tool's chat turns on lanes.
    pub(crate) fn chat(&self, tool: Tool) -> bool {
        Tool::ALL
            .iter()
            .position(|each| *each == tool)
            .is_some_and(|at| self.chat[at])
    }
}

/// Before the agent's tool is opened by hand on its conversation (`GET /terminal/<session>`),
/// an idle lane of the agent's, of any tool, is retired and waited for until its tool has
/// exited: one writer per conversation. A lane that still runs a turn refuses, and the terminal
/// opens as it did before lanes (the lead's decision of 25 Nov 2026: that two-writer case is
/// older than lanes, and is issue #770). Blocking: it waits for the lane's tool to exit.
pub(crate) fn make_way_if_idle(run: &Path, agent_id: &str) {
    for tool in Tool::ALL {
        let _ = Endpoint::of(tool, run, agent_id).retire();
    }
}

/// Where an agent's lane keeps the identity of its running turn.
pub(crate) fn turn_file(run: &Path, agent_id: &str) -> PathBuf {
    run.join(format!("{agent_id}-turn.token"))
}

/// Whether a lane that runs is used as it is or must make way for a new one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Place {
    /// The lane that runs is the one wanted, or still runs work for the same thread: use it.
    Kept,
    /// No lane runs now, room has been made, and a new one may start.
    Free,
}

/// An agent's lane of one tool, as the Host reaches it: its control socket in the run folder.
#[derive(Clone, Debug, PartialEq, Eq)]
pub(crate) struct Endpoint {
    tool: Tool,
    socket: PathBuf,
}

impl Endpoint {
    pub(crate) fn of(tool: Tool, run: &Path, agent_id: &str) -> Self {
        Self {
            tool,
            socket: run.join(format!("{agent_id}{}", tool.suffix())),
        }
    }

    /// The lane a socket in the run folder is, by its name.
    fn found(socket: PathBuf) -> Option<Self> {
        let name = socket.file_name()?.to_str()?;
        let tool = Tool::ALL
            .into_iter()
            .find(|tool| name.ends_with(tool.suffix()))?;
        Some(Self { tool, socket })
    }

    pub(crate) fn socket(&self) -> &Path {
        &self.socket
    }

    /// The turn file of this lane's agent, named from its socket.
    pub(crate) fn turn_file(&self, run: &Path) -> PathBuf {
        let agent = self
            .socket
            .file_name()
            .and_then(|name| name.to_str())
            .and_then(|name| name.strip_suffix(self.tool.suffix()))
            .unwrap_or("lane");
        turn_file(run, agent)
    }

    /// Decides whether the lane that runs is used for `thread` with options of `digest`: one
    /// started otherwise (another thread, other options, or what `kept` says the tool keeps from
    /// an earlier turn) is retired while idle; one that still runs work is used as it is, for
    /// its own thread. Before a new lane starts, room is made for it within `room`.
    pub(crate) fn place(
        &self,
        run: &Path,
        thread: Option<&str>,
        digest: &str,
        kept: impl FnOnce(&Status) -> bool,
        room: Room,
    ) -> io::Result<Place> {
        if let Ok(state) = self.inspect() {
            let same_thread = state.thread == thread.unwrap_or_default();
            if same_thread && state.options == digest && !kept(&state) {
                return Ok(Place::Kept);
            }
            if self.retire().is_err() {
                return if same_thread {
                    Ok(Place::Kept)
                } else {
                    Err(io::Error::other(format!(
                        "the {} lane owns another thread",
                        self.tool.name()
                    )))
                };
            }
        }
        if !self.released()? {
            return Err(io::Error::other(format!(
                "the {} lane owner has not disconnected",
                self.tool.name()
            )));
        }
        std::fs::create_dir_all(run)?;
        make_room(run, self, room);
        Ok(Place::Free)
    }

    pub(crate) fn inspect(&self) -> io::Result<Status> {
        self.ask(&Lifecycle::Inspect)
    }

    /// One request, and the state the lane answers with, read as `T`.
    pub(crate) fn ask<T: DeserializeOwned>(&self, request: &impl Serialize) -> io::Result<T> {
        let stream = self.connect(request)?;
        replied(&mut BufReader::new(stream), self.tool)
    }

    /// Losing a watcher says nothing about thread ownership. The agent must
    /// reconcile it with the lane lock/claim before choosing another launcher.
    pub(crate) fn watch<T: DeserializeOwned>(
        &self,
        request: &impl Serialize,
        mut observe: impl FnMut(T) -> bool,
    ) -> io::Result<()> {
        let stream = self.connect(request)?;
        let mut input = BufReader::new(stream);
        if !observe(replied(&mut input, self.tool)?) {
            return Ok(());
        }
        input.get_ref().set_read_timeout(None)?;
        loop {
            let state =
                serde_json::from_slice(&line(&mut input, self.tool)?).map_err(io::Error::other)?;
            if !observe(state) {
                return Ok(());
            }
        }
    }

    /// Ask an idle lane to leave, and wait until it has: its owner lock is let go only after
    /// its tool has been reaped. A lane that still runs work refuses.
    pub(crate) fn retire(&self) -> io::Result<()> {
        if self.released()? {
            return Ok(());
        }
        self.ask::<Status>(&Lifecycle::Retire)?;
        self.wait_released()
    }

    /// Waits for the lane's owner lock, which its process holds until it is gone.
    pub(crate) fn wait_released(&self) -> io::Result<()> {
        let file = match OpenOptions::new()
            .read(true)
            .write(true)
            .open(owner_lock(&self.socket))
        {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(()),
            Err(error) => return Err(error),
        };
        file.lock()
    }

    /// A retirement response only acknowledges the request. The process
    /// releases the owner lock after reaping its tool.
    pub(crate) fn released(&self) -> io::Result<bool> {
        let file = match OpenOptions::new()
            .read(true)
            .write(true)
            .open(owner_lock(&self.socket))
        {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(true),
            Err(error) => return Err(error),
        };
        match file.try_lock() {
            Ok(()) => Ok(true),
            Err(std::fs::TryLockError::WouldBlock) => Ok(false),
            Err(std::fs::TryLockError::Error(error)) => Err(error),
        }
    }

    fn connect(&self, request: &impl Serialize) -> io::Result<UnixStream> {
        let mut stream = UnixStream::connect(&self.socket)?;
        stream.set_read_timeout(Some(RPC_TIMEOUT))?;
        stream.set_write_timeout(Some(RPC_TIMEOUT))?;
        serde_json::to_writer(&mut stream, request).map_err(io::Error::other)?;
        stream.write_all(b"\n")?;
        Ok(stream)
    }
}

/// An attempt is offered to a lane once more when the first offer `first` failed and nothing of
/// it reached the tool (its `spool` is not there): the login changed and the lane left, or an
/// idle lane left between the look and the offer (its own idle time, another agent's room). A
/// lane that refused for its login is on its way out and is waited for; any other may still be
/// there, and is looked at again by `again` rather than waited for.
pub(crate) fn once_more<T>(
    first: io::Result<T>,
    spool: &Path,
    lane: &Endpoint,
    again: impl FnOnce() -> io::Result<T>,
) -> io::Result<T> {
    match first {
        Err(error) if !spool.try_exists().unwrap_or(true) => {
            if error.kind() == io::ErrorKind::PermissionDenied {
                lane.wait_released()?;
            }
            again()
        }
        other => other,
    }
}

/// Keeps this Host's lanes, of every tool, the one about to start (`own`) included, within both
/// limits of `room`: at most `room.most` of them, holding at most `room.budget` bytes together.
/// A lane counts at what it weighed itself (`Status::memory_bytes`), or at its tool's typical
/// size where it has not said one or said nothing (its `/proc` could not be read); the one about
/// to start at its typical size. The idle lanes that held no turn the longest are asked to leave
/// first, until both limits hold. A lane that runs a turn, or whose work may continue, refuses
/// and counts. Read only when a lane is about to start. The Host reads no `/proc` here: each
/// lane weighed itself, in its own process.
fn make_room(run: &Path, own: &Endpoint, room: Room) {
    let Ok(entries) = std::fs::read_dir(run) else {
        return;
    };
    let mut idle = Vec::new();
    let mut live = 1usize;
    let mut held = own.tool.typical_bytes();
    for entry in entries.flatten() {
        let path = entry.path();
        if path == own.socket {
            continue;
        }
        let Some(lane) = Endpoint::found(path) else {
            continue;
        };
        // Asked, not locked: taking another lane's owner lock, even for a moment, could make
        // a lane of that agent that starts in that moment fail (review of 23 Nov 2026).
        let Ok(state) = lane.inspect() else {
            continue;
        };
        // A weight that is not there, or is nothing, is no reading: the lane counts at its
        // estimate, never at 0, so the budget bounds the lanes even where `/proc` cannot be
        // read (review of 25 Nov 2026).
        let memory = state
            .memory_bytes
            .filter(|bytes| *bytes > 0)
            .unwrap_or_else(|| lane.tool.typical_bytes());
        live += 1;
        held = held.saturating_add(memory);
        if state.claim.is_none() && state.available {
            idle.push((state.idle_ms, memory, lane));
        }
    }
    idle.sort_by_key(|(idle_ms, _, _)| std::cmp::Reverse(*idle_ms));
    for (_, memory, lane) in idle {
        if live <= room.most && held <= room.budget {
            break;
        }
        if lane.ask::<Status>(&Lifecycle::Retire).is_ok() {
            live -= 1;
            held = held.saturating_sub(memory);
        }
    }
}

#[derive(serde::Deserialize)]
struct Reply<T> {
    state: Option<T>,
    error: Option<String>,
    #[serde(default)]
    code: Option<String>,
}

/// A refusal for a changed login has the kind `PermissionDenied`: the lane has left, and
/// nothing of the attempt reached the tool.
fn replied<T: DeserializeOwned>(input: &mut impl BufRead, tool: Tool) -> io::Result<T> {
    let reply: Reply<T> = serde_json::from_slice(&line(input, tool)?).map_err(io::Error::other)?;
    let kind = if reply.code.as_deref() == Some(LOGIN_CHANGED) {
        io::ErrorKind::PermissionDenied
    } else {
        io::ErrorKind::Other
    };
    reply.state.ok_or_else(|| {
        io::Error::new(
            kind,
            reply
                .error
                .unwrap_or_else(|| format!("{} lane supplied no state", tool.name())),
        )
    })
}

fn line(input: &mut impl BufRead, tool: Tool) -> io::Result<Vec<u8>> {
    let mut line = Vec::new();
    input.take(MAX_STATE).read_until(b'\n', &mut line)?;
    if line.last() != Some(&b'\n') {
        return Err(io::Error::new(
            io::ErrorKind::UnexpectedEof,
            format!("{} lane connection closed", tool.name()),
        ));
    }
    Ok(line)
}

#[cfg(test)]
#[path = "lane/tests.rs"]
mod tests;
