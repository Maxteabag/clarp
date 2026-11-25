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
//! and its requests. [`codex`] is the Codex app-server's; [`claude`] is Claude Code's
//! stream-json on stdin, which starts from a turn's own order (no turn waits for a cold lane).
//! The lane process's side of the same lifecycle is the shim's `lane_*` modules. What holds
//! with a lane and without one is `server-rs/docs/lanes.md`.

pub(crate) mod claude;
pub(crate) mod codex;

use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use clarp_shim::lane::{LOGIN_CHANGED, Lifecycle, Status, owner_lock, owner_pid};
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
    Claude,
}

impl Tool {
    pub(crate) const ALL: [Self; 2] = [Self::Codex, Self::Claude];

    /// The tool of an agent's backend, where that tool has a lane.
    pub(crate) fn of(backend: &str) -> Option<Self> {
        match backend {
            "codex" => Some(Self::Codex),
            "claude" => Some(Self::Claude),
            _ => None,
        }
    }

    /// The owner's switch that runs this tool's chat turns on lanes.
    pub(crate) fn switch(self) -> &'static str {
        match self {
            Self::Codex => "codex_chat_lane",
            Self::Claude => "claude_chat_lane",
        }
    }

    /// What ends the name of this tool's lane sockets in the run folder, after the agent's id.
    fn suffix(self) -> &'static str {
        match self {
            Self::Codex => "-cxg.sock",
            Self::Claude => "-cll.sock",
        }
    }

    /// How the tool is named in what the Host answers its apps (`GET /lane-settings`).
    pub(crate) fn key(self) -> &'static str {
        match self {
            Self::Codex => "codex",
            Self::Claude => "claude",
        }
    }

    /// How the tool is named in the words of a refusal.
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Codex => "Codex",
            Self::Claude => "Claude",
        }
    }

    /// What a lane of this tool counts at in the memory budget before it has weighed itself,
    /// the one about to start among them, in bytes.
    fn typical_bytes(self) -> u64 {
        match self {
            // An idle app-server with its shim, measured by `tools/turn-timings` (issue 761).
            Self::Codex => 270 * MIB,
            // A kept Claude Code on Sonnet with no MCP server weighed 159 MiB after a turn
            // (24 Nov 2026); an agent's MCP servers and the shim add to it.
            Self::Claude => 300 * MIB,
        }
    }
}

pub(crate) const MIB: u64 = 1024 * 1024;

/// The idle time is shared by every tool's lanes. It keeps the name it had when Codex had the
/// only lane, so an installed config (and `install-host`, which carries it) behaves as before.
pub(crate) const IDLE_MINUTES: &str = "codex_lane_idle_minutes";
/// The cap: the memory all lanes of this Host may hold together, in MiB (the lead's decision of
/// 24 Nov 2026, issue 761).
pub(crate) const MEMORY_MB: &str = "lane_memory_mb";
/// 2,200 MB unless the owner says: what he approved for the Codex lane (eight lanes at about
/// 270 MB each), so switching on lanes of other tools never quietly raises what he agreed to.
const MEMORY_MB_DEFAULT: u64 = 2_200;
/// The count of lanes this Host keeps, of every tool, the one about to start included: a limit
/// beside the budget, with the meaning it had when it was the only one (the lead's decision of
/// 25 Nov 2026). Idle Codex lanes measure 83-142 MiB of PSS, so the budget alone would keep
/// 15-20 of them; the count keeps the eight the owner agreed to.
pub(crate) const MOST: &str = "codex_lane_most";

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
/// claude_chat_lane = true       # Claude chat turns run on a kept Claude Code (default false)
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
        Self::of(crate::owner::OwnerFile::new(config_path()).read().as_ref())
    }

    pub(crate) fn of(file: Option<&toml::Table>) -> Self {
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

/// The Host's own `config.toml`, where the owner's settings of lanes are: what [`Settings::read`]
/// reads and `POST /lane-settings` writes. `None` on a Host named no folder (`--config`): its
/// lanes take their defaults, and nothing is written (issue 780).
pub(crate) fn config_path() -> Option<PathBuf> {
    crate::config::host_config_file()
}

/// Before the agent's tool is opened by hand on its conversation (`GET /terminal/<session>`),
/// an idle lane of the agent's, of any tool, is retired and waited for until its tool has
/// exited: one writer per conversation. Blocking, for at most [`LEAVE_WAIT`]: a lane that has
/// not let go by then (its tool hangs on its way out) is ended by its own process id and said
/// once in the Host's log, rather than holding the request and the agent's lease (reviews of
/// 25 Nov 2026). False: a lane refused to leave because it runs work of its own, and the
/// terminal is refused as it is while any turn of the agent runs (issue #770).
pub(crate) fn make_way_if_idle(run: &Path, agent_id: &str) -> bool {
    make_way_within(run, agent_id, LEAVE_WAIT)
}

/// How long a turn or the terminal waits for a lane asked to leave to let go, before the lane
/// is ended by its process id and the turn or terminal goes on without it (issue #772): no
/// turn waits on a lane's exit without a bound.
pub(crate) const LEAVE_WAIT: Duration = Duration::from_secs(15);
/// How long a lane ended by its process id is given to have let go of its owner lock.
const ENDED_WAIT: Duration = Duration::from_secs(2);
/// How often a bounded wait looks at the owner lock again.
const RELEASE_LOOK: Duration = Duration::from_millis(100);

fn make_way_within(run: &Path, agent_id: &str, bound: Duration) -> bool {
    // Every tool's lane is asked, also after one refused.
    let mut made_way = true;
    for tool in Tool::ALL {
        made_way &= Endpoint::of(tool, run, agent_id)
            .retire_within(bound)
            .is_ok();
    }
    made_way
}

/// How long a turn the owner sent keeps an agent's lane his (the owner's rule of 25 Nov 2026).
const OWNERS_TURN: Duration = Duration::from_secs(30 * 60);
/// How long a chat the owner has open keeps its agent's lane his after its last mark: a
/// connection that watches the chat marks it every `VIEW_MARK_EVERY` while it is open.
const OWNERS_VIEW: Duration = Duration::from_secs(60);
pub(crate) const VIEW_MARK_EVERY: Duration = Duration::from_secs(15);

/// A turn of origin user started for this agent: its lanes are the owner's for half an hour.
/// Kept as a file's time in the run folder, where lanes already keep what each other may read,
/// so a lane's room is decided without asking the agent's thread. Blocking, and short.
pub(crate) fn mark_owners_turn(run: &Path, agent_id: &str) {
    touch(&run.join(format!("{agent_id}-owner-turn.mark")));
}

/// A chat of this agent is open on one of the owner's devices (a connection watches it live).
pub(crate) fn mark_owners_view(run: &Path, agent_id: &str) {
    touch(&run.join(format!("{agent_id}-owner-view.mark")));
}

fn touch(path: &Path) {
    let _ = OpenOptions::new()
        .create(true)
        .append(true)
        .open(path)
        .and_then(|file| file.set_modified(std::time::SystemTime::now()));
}

/// Whether this agent's lanes are the owner's: a turn of his within half an hour, or a chat of
/// it open on one of his devices. Every other lane is the agents' own (helpers, janitors,
/// one agent asking another).
fn owners(run: &Path, agent_id: &str) -> bool {
    let fresh = |name: &str, within: Duration| {
        std::fs::metadata(run.join(format!("{agent_id}-{name}.mark")))
            .and_then(|meta| meta.modified())
            .is_ok_and(|at| at.elapsed().is_ok_and(|age| age <= within))
    };
    fresh("owner-turn", OWNERS_TURN) || fresh("owner-view", OWNERS_VIEW)
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
    /// No place is free within both limits, and the lane is not one a turn needs: it does not
    /// start, and the turn runs in a process of its own (issue 786).
    Full,
}

/// A lane that would start past both limits and that no turn needs: none is started (issue 786).
#[derive(Debug)]
pub(crate) struct NoPlace;

impl std::fmt::Display for NoPlace {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str("no lane place is free: every lane runs work")
    }
}

impl std::error::Error for NoPlace {}

impl NoPlace {
    /// Whether `error` is this refusal, as a lane's start returns it.
    pub(crate) fn is(error: &io::Error) -> bool {
        error.get_ref().is_some_and(|inner| inner.is::<NoPlace>())
    }

    pub(crate) fn error() -> io::Error {
        io::Error::other(NoPlace)
    }
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
        turn_file(run, self.agent().unwrap_or("lane"))
    }

    /// The agent this lane is of, from its socket's name.
    fn agent(&self) -> Option<&str> {
        self.socket
            .file_name()
            .and_then(|name| name.to_str())
            .and_then(|name| name.strip_suffix(self.tool.suffix()))
    }

    /// Decides whether the lane that runs is used for `thread` with options of `digest`: one
    /// started otherwise (another thread, other options, or what `kept` says the tool keeps from
    /// an earlier turn) is retired while idle; one that still runs work is used as it is, for
    /// its own thread. Before a new lane starts, room is made for it within `room`. Where every
    /// place runs work, a lane that is `needed` (a native goal's) still starts and counts; any
    /// other is not started (`Place::Full`), so the limits hold (issue 786).
    pub(crate) fn place(
        &self,
        run: &Path,
        thread: Option<&str>,
        digest: &str,
        kept: impl FnOnce(&Status) -> bool,
        room: Room,
        needed: bool,
    ) -> io::Result<Place> {
        self.place_within(run, (thread, digest), kept, room, LEAVE_WAIT)
    }

    /// [`Self::place`], waiting at most `bound` for a lane that must make way to let go.
    fn place_within(
        &self,
        run: &Path,
        (thread, digest): (Option<&str>, &str),
        kept: impl FnOnce(&Status) -> bool,
        room: Room,
        bound: Duration,
    ) -> io::Result<Place> {
        if let Ok(state) = self.inspect() {
            let same_thread = state.thread == thread.unwrap_or_default();
            if same_thread && state.options == digest && !kept(&state) {
                return Ok(Place::Kept);
            }
            // A lane that does not let go in time has been ended by its process id; the
            // look at its owner lock below says whether it has gone.
            if self.retire_within(bound).is_err() {
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
        if make_room(run, self, room, needed) || needed {
            Ok(Place::Free)
        } else {
            Ok(Place::Full)
        }
    }

    pub(crate) fn inspect(&self) -> io::Result<Status> {
        self.ask(&Lifecycle::Inspect)
    }

    /// One request, and the state the lane answers with, read as `T`.
    pub(crate) fn ask<T: DeserializeOwned>(&self, request: &impl Serialize) -> io::Result<T> {
        let stream = self.connect(request, RPC_TIMEOUT)?;
        replied(&mut BufReader::new(stream), self.tool)
    }

    /// What the lane is, asked without blocking a thread: a connect that cannot be taken (the
    /// lane's queue is full, or nothing listens) fails or waits as a future, so the one who asks
    /// bounds the whole of it with a deadline.
    async fn inspect_async(&self) -> io::Result<Status> {
        use tokio::io::{AsyncBufReadExt, AsyncReadExt, AsyncWriteExt};
        let mut stream = tokio::net::UnixStream::connect(&self.socket).await?;
        let mut request = serde_json::to_vec(&Lifecycle::Inspect).map_err(io::Error::other)?;
        request.push(b'\n');
        stream.write_all(&request).await?;
        let mut answer = Vec::new();
        tokio::io::BufReader::new(stream)
            .take(MAX_STATE)
            .read_until(b'\n', &mut answer)
            .await?;
        replied(&mut answer.as_slice(), self.tool)
    }

    /// Losing a watcher says nothing about thread ownership. The agent must
    /// reconcile it with the lane lock/claim before choosing another launcher.
    pub(crate) fn watch<T: DeserializeOwned>(
        &self,
        request: &impl Serialize,
        mut observe: impl FnMut(T) -> bool,
    ) -> io::Result<()> {
        let stream = self.connect(request, RPC_TIMEOUT)?;
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

    /// Ask an idle lane to leave, and wait until it has, for at most [`LEAVE_WAIT`]: its owner
    /// lock is let go only after its tool has been reaped. A lane that still runs work refuses,
    /// and so does one that has not let go by the bound, after it was ended by its process id.
    pub(crate) fn retire(&self) -> io::Result<()> {
        if self.retire_within(LEAVE_WAIT)? {
            Ok(())
        } else {
            Err(io::Error::other(format!(
                "the {} lane did not let go",
                self.tool.name()
            )))
        }
    }

    /// Asks an idle lane to leave and waits at most `bound` for it to have gone: `true` once its
    /// owner lock is free. A lane that still runs work refuses, which is an error. A lane
    /// answers a retirement only once it has gone, so its answer is waited for within the
    /// bound; no answer, a closed connection or a socket that no longer listens is a lane on
    /// its way out, never a refusal. One that has not let go by the bound is ended by its own
    /// process id ([`Self::let_go_within`]), and `false` means even that did not free it.
    pub(crate) fn retire_within(&self, bound: Duration) -> io::Result<bool> {
        if self.released()? {
            return Ok(true);
        }
        let until = Instant::now() + bound;
        match self.connect(&Lifecycle::Retire, bound) {
            Ok(stream) => match replied::<Status>(&mut BufReader::new(stream), self.tool) {
                Ok(_) => {}
                Err(error) if leaving(&error) => {}
                Err(error) => return Err(error),
            },
            Err(error) if leaving(&error) => {}
            Err(error) => return Err(error),
        }
        self.let_go_within(until)
    }

    /// Waits until `until` for a lane on its way out to let go of its owner lock. One that has
    /// not by then is ended by its own process id, the one it wrote into the lock once it held
    /// it (never by a name or a pattern), said once in the Host's log, and given a moment to be
    /// gone. `true`: the lock is free.
    pub(crate) fn let_go_within(&self, until: Instant) -> io::Result<bool> {
        if self.released_by(until)? {
            return Ok(true);
        }
        let name = self
            .socket
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_default();
        // Read while the lock is held, so the id is that of the process holding it.
        let pid = owner_pid(&self.socket);
        if self.released()? {
            return Ok(true);
        }
        let Some(pid) = pid.and_then(|pid| ended(pid).then_some(pid)) else {
            eprintln!(
                "clarp-host: the {} lane {name} did not let go in time and names no process \
                 to end; it is left as it is",
                self.tool.name()
            );
            return Ok(false);
        };
        eprintln!(
            "clarp-host: the {} lane {name} did not let go in time; its process {pid} is ended",
            self.tool.name()
        );
        self.released_by(Instant::now() + ENDED_WAIT)
    }

    /// Looks at the owner lock until it is free or `until` has passed. A lock cannot be waited
    /// for with a deadline, so it is looked at again and again, for a bounded time.
    fn released_by(&self, until: Instant) -> io::Result<bool> {
        loop {
            if self.released()? {
                return Ok(true);
            }
            let left = until.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return Ok(false);
            }
            std::thread::sleep(left.min(RELEASE_LOOK));
        }
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

    /// One request sent, its answer to be read within `wait`.
    fn connect(&self, request: &impl Serialize, wait: Duration) -> io::Result<UnixStream> {
        let mut stream = UnixStream::connect(&self.socket)?;
        stream.set_read_timeout(Some(wait.max(Duration::from_millis(1))))?;
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
/// there, and is looked at again by `again` rather than waited for. The wait is bounded as a
/// retirement's is: one that does not let go in time is ended by its process id, and `again`
/// finds out whether it has gone.
pub(crate) fn once_more<T>(
    first: io::Result<T>,
    spool: &Path,
    lane: &Endpoint,
    again: impl FnOnce() -> io::Result<T>,
) -> io::Result<T> {
    match first {
        Err(error) if !spool.try_exists().unwrap_or(true) => {
            if error.kind() == io::ErrorKind::PermissionDenied {
                lane.let_go_within(Instant::now() + LEAVE_WAIT)?;
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
/// lane weighed itself, in its own process. Answers whether both limits hold with the new lane
/// counted: `false` when the lanes that are left all run work.
///
/// The owner's agents come first (his rule of 25 Nov 2026). The idle lanes of agents he is not
/// talking with leave first, longest idle first, then idle lanes of his, longest idle first. A
/// lane for an agent of his may send any idle lane away; a lane of the agents' own may send
/// away only idle lanes of the agents' own, so it never takes an owner's place.
///
/// What may leave is reckoned first. A lane that is not `needed` sends nothing away unless what
/// may leave is enough for both limits: a start that will not happen ends no lane. A `needed`
/// lane starts whatever the answer, so it sends away what it may, toward the limits.
fn make_room(run: &Path, own: &Endpoint, room: Room, needed: bool) -> bool {
    let for_owner = own.agent().is_some_and(|agent| owners(run, agent));
    let mut idle = Vec::new();
    let mut live = 1usize;
    let mut held = own.tool.typical_bytes();
    for (lane, state) in answering(run, &own.socket) {
        let memory = weight(&lane, &state);
        live += 1;
        held = held.saturating_add(memory);
        if state.claim.is_none() && state.available {
            let his = lane.agent().is_some_and(|agent| owners(run, agent));
            if for_owner || !his {
                idle.push((his, state.idle_ms, memory, lane));
            }
        }
    }
    idle.sort_by_key(|(his, idle_ms, _, _)| (*his, std::cmp::Reverse(*idle_ms)));
    let fits = |live: usize, held: u64| live <= room.most && held <= room.budget;
    // What the lanes would be once those that may leave had, in order, until both limits hold.
    let (mut would_live, mut would_hold) = (live, held);
    for (_, _, memory, _) in &idle {
        if fits(would_live, would_hold) {
            break;
        }
        would_live -= 1;
        would_hold = would_hold.saturating_sub(*memory);
    }
    if !fits(would_live, would_hold) && !needed {
        return false;
    }
    for (_, _, memory, lane) in idle {
        if fits(live, held) {
            break;
        }
        if lane.ask::<Status>(&Lifecycle::Retire).is_ok() {
            live -= 1;
            held = held.saturating_sub(memory);
        }
    }
    fits(live, held)
}

/// Every lane in the run folder but `skip` that answers what it is, with its answer. A socket
/// nothing answers on (a lane that has left) is passed over.
fn answering(run: &Path, skip: &Path) -> Vec<(Endpoint, Status)> {
    lanes_in(run)
        .into_iter()
        .filter(|lane| lane.socket != skip)
        // Asked, not locked: taking another lane's owner lock, even for a moment, could make
        // a lane of that agent that starts in that moment fail (review of 23 Nov 2026).
        .filter_map(|lane| {
            let state = lane.inspect().ok()?;
            Some((lane, state))
        })
        .collect()
}

/// The lanes whose sockets are in the run folder, by their names. Blocking.
pub(crate) fn lanes_in(run: &Path) -> Vec<Endpoint> {
    let Ok(entries) = std::fs::read_dir(run) else {
        return Vec::new();
    };
    entries
        .flatten()
        .filter_map(|entry| Endpoint::found(entry.path()))
        .collect()
}

/// What a lane counts at in the memory budget: what it weighed itself. A weight that is not
/// there, or is nothing, is no reading: the lane counts at its tool's typical size, never at 0,
/// so the budget bounds the lanes even where `/proc` cannot be read (review of 25 Nov 2026).
fn weight(lane: &Endpoint, state: &Status) -> u64 {
    state
        .memory_bytes
        .filter(|bytes| *bytes > 0)
        .unwrap_or_else(|| lane.tool.typical_bytes())
}

/// What the lanes of one tool hold now.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct Held {
    /// Lanes that run, busy or idle.
    pub lanes: usize,
    /// Of them, those that hold a turn or work that may continue.
    pub busy: usize,
    /// What they hold together, counted as the memory budget counts them.
    pub bytes: u64,
}

/// What this Host's lanes hold now, and how many of them did not answer in time.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) struct Holding {
    /// Per tool, in the order of [`Tool::ALL`].
    pub tools: [Held; Tool::ALL.len()],
    /// Lanes that were asked and had not answered by the deadline: not counted in `tools`.
    pub unanswered: usize,
}

/// How long `/lane-settings` waits for the lanes' answers, all of them together: they are asked at
/// once, so one lane that hangs, or many, never holds a settings screen longer than this.
pub(crate) const HELD_LOOK: Duration = Duration::from_secs(2);

/// What the lanes of `lanes` hold now: each is asked its state, all at once and within
/// `deadline`, and counts at what it weighed itself in its own process, as when room is made. No
/// lane is started for it, and the Host reads no `/proc`. A lane nothing answers for (it has
/// left) is not counted; one that has not answered by the deadline is counted as unanswered.
pub(crate) async fn held(lanes: Vec<Endpoint>, deadline: tokio::time::Instant) -> Holding {
    let mut asked = tokio::task::JoinSet::new();
    for lane in lanes {
        asked.spawn(async move {
            let state = tokio::time::timeout_at(deadline, lane.inspect_async()).await;
            (lane, state)
        });
    }
    let mut holding = Holding::default();
    while let Some(joined) = asked.join_next().await {
        let Ok((lane, state)) = joined else {
            continue;
        };
        let state = match state {
            Ok(Ok(state)) => state,
            Ok(Err(_)) => continue,
            Err(_) => {
                holding.unanswered += 1;
                continue;
            }
        };
        let Some(at) = Tool::ALL.iter().position(|tool| *tool == lane.tool) else {
            continue;
        };
        let tool = &mut holding.tools[at];
        tool.lanes += 1;
        if state.claim.is_some() || !state.available {
            tool.busy += 1;
        }
        tool.bytes = tool.bytes.saturating_add(weight(&lane, &state));
    }
    holding
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

/// An answer that never came, a connection closed without one, or a socket no longer there:
/// what a lane on its way out gives, which is no refusal.
fn leaving(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::WouldBlock
            | io::ErrorKind::TimedOut
            | io::ErrorKind::UnexpectedEof
            | io::ErrorKind::NotFound
            | io::ErrorKind::ConnectionRefused
            | io::ErrorKind::ConnectionReset
            | io::ErrorKind::BrokenPipe
    )
}

/// Ends the process `pid` of a lane that holds its owner lock and did not let go, with SIGKILL:
/// it was asked to leave and given its time. The id is safe to signal while the lock is held:
/// the lane wrote it there after it took the lock, the lock is let go when that process ends,
/// and no child of it inherits the lock. No id that would signal more than one process, or
/// this Host itself, is ever used. True: the signal was sent.
fn ended(pid: u32) -> bool {
    if pid <= 1 || pid == std::process::id() {
        return false;
    }
    let Ok(raw) = i32::try_from(pid) else {
        return false;
    };
    nix::sys::signal::kill(
        nix::unistd::Pid::from_raw(raw),
        nix::sys::signal::Signal::SIGKILL,
    )
    .is_ok()
}

#[cfg(test)]
#[path = "lane/tests.rs"]
mod tests;
