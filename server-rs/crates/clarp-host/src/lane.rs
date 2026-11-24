//! The lane layer: an agent's tool kept alive between its chat turns, in one process under the
//! shim, while the owner's switch for that tool is on (issues 742 and 761). What every lane
//! shares lives here, whatever tool it keeps:
//!
//! - the owner's settings in `config.toml`: one switch per tool, the idle time and the cap
//!   shared by all ([`Settings`]);
//! - where a lane listens, and how it is asked, watched, retired and known to have left
//!   ([`Endpoint`]);
//! - the registry: one memory budget in bytes for this Host, across every tool's lanes, the
//!   longest idle leaving first ([`Settings::budget`]);
//! - whether a running lane is used as it is or retired and started again: another thread,
//!   other options (model, prompt, provider, ...), or what its driver says it kept;
//! - the turn-identity file its tools read the running turn from ([`TURN_FILE_ENV`]);
//! - an attempt that reached nothing of the tool offered once more to a new lane, after a
//!   changed login ([`clarp_shim::lane::LOGIN_CHANGED`]) or a lane that left by itself.
//!
//! A tool's driver ([`Driver`]) adds what is its own: how its process is started, what its
//! digest covers, and how a turn is sent. [`codex`] is the Codex app-server's, and the only one
//! built; Claude's stream-json, Muse's MSP and OpenCode's serve are planned (issue 761). The
//! lane process's side of the same seam is the shim's `lane_driver`. Why there are lanes at all,
//! and what holds with or without one, is `server-rs/docs/lanes.md`.

pub(crate) mod codex;
pub(crate) mod digest;
mod memory;

use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::process::Child;
use std::time::Duration;

use clarp_shim::lane::{Control, Holder, LOGIN_CHANGED, Status, owner_lock};
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

    /// The shim's word that runs this tool's lane process, and the start of its scope's name.
    pub(crate) fn shim_word(self) -> (&'static str, &'static str) {
        match self {
            Self::Codex => ("codex-goal-lane", "clarp-codex-goal"),
        }
    }

    /// The folder under the run folder that holds the locks of this tool's conversations.
    fn locks(self) -> &'static str {
        match self {
            Self::Codex => "codex-threads",
        }
    }

    /// What a lane of this tool holds in memory when it says nothing and cannot be read (just
    /// started, or gone between the look and the read), in bytes: what was measured of it.
    fn typical_bytes(self) -> u64 {
        match self {
            // An idle app-server with its shim, measured by `tools/turn-timings` (issue 761).
            Self::Codex => 270 * 1024 * 1024,
        }
    }
}

/// The idle time is shared by every tool's lanes. It keeps the name it had when Codex had the
/// only lane, so an installed config (and `install-host`, which carries it) behaves as before.
const IDLE_MINUTES: &str = "codex_lane_idle_minutes";
/// The cap: the memory all lanes of this Host may hold together, in MiB (the lead's decision of
/// 24 Nov 2026, issues 761 and 763).
const MEMORY_MB: &str = "lane_memory_mb";
/// 2,200 MB unless the owner says: what he approved for the Codex lane (eight lanes at about
/// 270 MB), so switching on lanes of other tools never quietly raises what he agreed to.
const MEMORY_MB_DEFAULT: u64 = 2_200;
/// The count of lanes the cap was before the budget replaced it. It is no longer read; a Host
/// that finds it says so once, at its start ([`replaced_settings`]).
const MOST: &str = "codex_lane_most";

/// The owner's settings of lanes, in this Host's `config.toml`, read anew at each use so they
/// change without a build or a restart. Top-level keys, before any table:
///
/// ```toml
/// codex_chat_lane = true        # Codex chat turns run on the agent's lane (default false)
/// codex_lane_idle_minutes = 15  # a lane with no turn and no goal leaves after this long
/// lane_memory_mb = 2200         # what all lanes, of every tool, may hold together (MiB);
///                               # the longest idle one leaves first
/// ```
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Settings {
    /// Each tool's switch, in the order of [`Tool::ALL`].
    chat: [bool; Tool::ALL.len()],
    pub idle_ms: u64,
    /// The cap: bytes all lanes of this Host may hold together. Each counts at what its driver
    /// reads of itself, else its process tree's proportional set size, else its tool's typical
    /// size; the lane about to start at its typical size.
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
            budget: u64::try_from(number(MEMORY_MB, 0))
                .ok()
                .filter(|mb| *mb > 0)
                .unwrap_or(MEMORY_MB_DEFAULT)
                .saturating_mul(1024 * 1024),
        }
    }

    /// What the owner's config says that this Host no longer reads, in words to log once at its
    /// start: `codex_lane_most`, which the memory budget replaced.
    fn replaced(file: Option<&toml::Table>) -> Option<String> {
        let most = file?.get(MOST)?;
        let most = most
            .as_integer()
            .map_or_else(|| format!("{most:?}"), |count| count.to_string());
        Some(format!(
            "{MOST} = {most} in config.toml is no longer read: lanes are kept within \
             {MEMORY_MB} (2200 MB unless set), the memory all lanes may hold together"
        ))
    }

    /// Whether the owner runs this tool's chat turns on lanes.
    pub(crate) fn chat(&self, tool: Tool) -> bool {
        Tool::ALL
            .iter()
            .position(|each| *each == tool)
            .is_some_and(|at| self.chat[at])
    }
}

/// What this Host's config says of lanes that it no longer reads, for the Host to log once when
/// it starts.
pub fn replaced_settings() -> Option<String> {
    let path = crate::config::host_config_dir().join("config.toml");
    Settings::replaced(crate::owner::OwnerFile::new(Some(path)).read().as_ref())
}

/// The folder of the locks of `tool`'s conversations, made where it is missing.
pub(crate) fn thread_locks(run: &Path, tool: Tool) -> io::Result<PathBuf> {
    let locks = run.join(tool.locks());
    std::fs::create_dir_all(&locks)?;
    Ok(locks)
}

/// One writer per conversation: a turn of its own process holds its conversation's lock as a
/// [`Holder::Turn`], shared and without waiting, for its whole life. A lane holds it as a
/// [`Holder::Lane`], exclusive, from before it touches the conversation until its tool has exited,
/// even when another agent has taken up the same conversation. So a per-turn process fails
/// rather than writes beside a lane, and a lane that warms beside a per-turn process takes the
/// conversation only once that process is gone.
pub(crate) fn guard_exec(
    order: &mut clarp_actor::ShimOrder,
    run: &Path,
    tool: Tool,
    thread: &str,
) -> io::Result<()> {
    let lock = clarp_shim::lane::thread_lock(&thread_locks(run, tool)?, thread);
    let program = std::mem::replace(&mut order.program, "flock".into());
    let args = std::mem::take(&mut order.args);
    order.args = Holder::Turn
        .flock_args()
        .iter()
        .map(|arg| (*arg).to_owned())
        .chain([
            "--no-fork".into(),
            lock.to_string_lossy().into_owned(),
            program,
        ])
        .collect();
    order.args.extend(args);
    Ok(())
}

/// What, other than its lane, may open an agent's conversation with its tool.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[allow(
    dead_code,
    reason = "the single-writer seam of every touch (issue 761); only Exec is wired yet"
)]
pub(crate) enum Touch {
    /// A turn run as a process of its own: the switch off, an attempt the lane does not take
    /// ([`Driver::takes`]), or a tool without a lane.
    Exec,
    /// A fork that runs the tool on the agent's conversation. A fork that copies it (as words,
    /// as this Host's helper forks do, or by the tool inside the lane, as Muse's `session/fork`)
    /// opens nothing and needs no way made: copying is preferred, so a fork costs the lane
    /// nothing.
    Fork,
    /// A status fork that runs the tool on the conversation, read-only. As for [`Touch::Fork`],
    /// one that copies needs no way made.
    StatusFork,
    /// The agent moves to another tool: its new session is written from the Host's record,
    /// so only a transit that opens the old session needs the way made.
    Transit,
    /// The agent's tool spoken to by hand, on its conversation (`GET /terminal/<session>`).
    Terminal,
}

/// Single writer, the layer's rule for every tool: before anything else opens an agent's
/// conversation (`touch`), every lane of that agent leaves, whatever its tool, and is waited for
/// until its tool has exited. A tool may refuse a second process on a conversation at once
/// rather than wait for it (Muse's session lock), so waiting is the rule, not a lock to queue on.
/// A lane that still runs work refuses, and the touch must not go ahead: an exec turn is then
/// failed by [`guard_exec`]'s lease rather than write beside it. The first refusal is returned,
/// after every lane was asked.
///
/// Where it is called today: before every exec turn of an agent (what Codex did before the
/// layer). A terminal, a fork or a transit that opens the session does not call it yet; see
/// `server-rs/docs/lanes.md`.
pub(crate) fn make_way(run: &Path, agent_id: &str, touch: Touch) -> io::Result<()> {
    let mut first = Ok(());
    for tool in Tool::ALL {
        let lane = Endpoint::of(tool, run, agent_id);
        // A lane warm with no session holds nothing to make way for: it takes the
        // conversation up only once this process, too, is gone.
        if !lane.released().unwrap_or(true) && lane.inspect().is_ok_and(|state| state.warming) {
            continue;
        }
        let result = lane.retire().map_err(|why| {
            io::Error::new(
                why.kind(),
                format!("{touch:?} waits for the {} lane: {why}", tool.name()),
            )
        });
        if first.is_ok() {
            first = result;
        }
    }
    first
}

/// Where an agent's lane keeps the identity of its running turn.
pub(crate) fn turn_file(run: &Path, agent_id: &str) -> PathBuf {
    run.join(format!("{agent_id}-turn.token"))
}

/// The login a lane runs under: it holds the pool's lock shared while it owns a turn, and
/// leaves when the login file names another account.
pub(crate) struct Login {
    pub lock: Option<PathBuf>,
    pub file: Option<PathBuf>,
    /// The tool's home and provider of an agent bound to one of its own (Codex today): its
    /// lane's tool runs in that home and on that provider, and leaves when it names another.
    /// `Err`: the agent is bound, and its binding cannot be used: no lane starts for it.
    pub own: Result<Option<clarp_backend::own_provider::OwnProvider>, String>,
}

/// What a tool's lane driver does in the Host that is its own (issue 761). Four are planned:
/// the Codex app-server over JSON-RPC ([`codex`], the only one built), Claude Code's
/// stream-json on stdin, Muse's MSP over stdio, and OpenCode's serve.
///
/// The rest of the seam is the same for every driver, so it is not here:
/// - steer and Stop go on the claim's own socket (`clarp_shim::Request::Steer`, `Stop`),
///   as for a turn of its own process; the lane process answers them in its tool's way
///   (the shim's `lane_driver::Driver`);
/// - the end of a turn is the claim's spool ending and the claim let go, followed by the
///   Host as any turn's;
/// - health is the lane's [`Status`] ([`Endpoint::inspect`]), its memory
///   [`Endpoint::memory`]: every lane is a process tree under the shim.
///
/// What a driver may or may not do is said here, not assumed: which attempts it takes
/// ([`Driver::takes`]), which changes it applies to its running tool and which start it again
/// ([`Driver::restart_for`] and its digest), and whether it keeps goals ([`Goals`]).
pub(crate) trait Driver {
    /// The tool it drives.
    const TOOL: Tool;
    /// What a turn asks of the lane: a prompt, or (Codex) a goal's activation or recovery.
    type Submission;
    /// The driver's snapshot of its lane, which reads as a [`Status`] as well.
    type Snapshot: DeserializeOwned;

    fn endpoint(&self) -> &Endpoint;

    /// Whether `order` may run on the lane. One it does not take runs as a process of its own,
    /// the per-turn fallback, once the agent's lanes have left ([`make_way`]). Every
    /// driver leaves an opening turn to its own gated launch. A driver whose tool can only take
    /// up a conversation that exists, not start one under the Host's id (Muse's serve), says
    /// no as well to the first turn of a conversation, a fork and a takeover.
    fn takes(order: &clarp_actor::ShimOrder) -> bool
    where
        Self: Sized,
    {
        order.opening.is_none()
    }

    /// What a turn does when the agent has no warm lane ready ([`Cold`]).
    const COLD: Cold;

    /// Starts the agent's lane without its session, beside a per-turn process that holds the
    /// session now ([`Cold::Beside`]). The lane takes the session only once that process is
    /// gone: it waits, in its own process, for the conversation's lock
    /// ([`clarp_shim::lane::thread_lock`]) that the per-turn process holds shared for its life,
    /// then takes it up (Muse's `session/resume`, OpenCode's `GET /session/:id`). A lane that
    /// fails to start costs nothing: the next turn runs per turn again.
    #[allow(
        dead_code,
        reason = "the seam for drivers that warm beside a per-turn process (issue 761)"
    )]
    ///
    /// The lane is then warm, with no session (`Status::warming`): spawned and initialized
    /// (Muse in 0.35-0.58 s), holding no conversation. `thread` is the conversation it takes up
    /// once the per-turn process is gone. A new conversation has none yet; the Host hands it
    /// over with [`Endpoint::take`] once the per-turn turn has named it.
    fn warm(
        &self,
        launcher: &crate::launcher::Launcher,
        profile: &clarp_actor::Profile,
        login: &Login,
        thread: Option<&str>,
    ) -> io::Result<Option<Child>>
    where
        Self: Sized,
    {
        let _ = (launcher, profile, login, thread);
        Err(io::Error::new(
            io::ErrorKind::Unsupported,
            format!("the {} lane does not start beside a turn", Self::TOOL.name()),
        ))
    }

    /// Whether the running lane, as `state` says, must start again for a turn that asks for
    /// `model` and `effort`. What the tool takes with a turn, or by a call to its running
    /// process (Muse's `session/setModel`), is applied in place and answers false; what it can
    /// only be started with belongs in the options digest instead.
    fn restart_for(state: &Status, model: &Option<String>, effort: &Option<String>) -> bool
    where
        Self: Sized;

    /// Attaches to the agent's running lane where it is the one wanted ([`Endpoint::place`]),
    /// or starts a new one and returns its child for the reaper. `thread` is `None` for a new
    /// conversation; `turn` is the model and effort a prompt asks for.
    fn start(
        &self,
        launcher: &crate::launcher::Launcher,
        profile: &clarp_actor::Profile,
        login: &Login,
        thread: Option<&str>,
        turn: Option<(&Option<String>, &Option<String>)>,
    ) -> io::Result<Option<Child>>;

    /// Sends an admitted attempt to the lane. `token` is the turn's identity for the Clarp
    /// tools it calls. A refusal for a changed login has the kind `PermissionDenied`: the lane
    /// has left, and nothing of the attempt reached the tool ([`once_more`]).
    fn submit(
        &self,
        order: clarp_actor::ShimOrder,
        submission: Self::Submission,
        token: Option<String>,
    ) -> io::Result<Self::Snapshot>;
}

/// A driver whose tool keeps goals of its own on its conversation and may start turns for them by
/// itself (Codex's native goals). Its goal requests (`Reserve`, `Goal`, `Recover`, `Probe`) and
/// its goal submissions are its own: no other driver has them, and the layer needs none of them.
pub(crate) trait Goals: Driver {
    /// After a Stop of a goal's turn: the goal is paused where it is still active, so its tool
    /// starts no turn of its own after the Stop.
    fn recover_stop(&self) -> io::Result<Self::Snapshot>;
}

/// What a turn does when the agent has no warm lane ready for it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Cold {
    /// The turn waits while its lane starts and takes up the session, then runs on it: Codex
    /// today, whose first turn on a new lane pays the app-server's start and the thread's load.
    Waits,
    /// No turn ever waits for a cold lane (the lead's rule for every driver): the turn runs as a
    /// process of its own, as it would with the switch off, and the lane starts beside it
    /// without the session ([`Driver::warm`]). The next turn runs on the lane once it is warm.
    /// The worst first turn is a per-turn one, never a cold start; the cold start shows only
    /// as memory taken early.
    #[allow(
        dead_code,
        reason = "the rule for the Muse, OpenCode and Claude drivers (issue 761)"
    )]
    Beside,
}

impl Tool {
    /// Whether this tool's driver takes `order` on its lane ([`Driver::takes`]).
    pub(crate) fn takes(self, order: &clarp_actor::ShimOrder) -> bool {
        match self {
            Self::Codex => <codex::Lane as Driver>::takes(order),
        }
    }

    /// What a turn of this tool does without a warm lane ([`Driver::COLD`]).
    pub(crate) fn cold(self) -> Cold {
        match self {
            Self::Codex => <codex::Lane as Driver>::COLD,
        }
    }

    /// Whether `order` runs on the agent's lane now: one the driver takes, and, for a driver
    /// that never makes a turn wait for a cold lane, only once that lane answers and is free.
    /// Otherwise the attempt runs as a process of its own.
    pub(crate) fn runs_on_lane(
        self,
        order: &clarp_actor::ShimOrder,
        run: &Path,
        agent_id: &str,
    ) -> bool {
        self.takes(order)
            && match self.cold() {
                Cold::Waits => true,
                Cold::Beside => Endpoint::of(self, run, agent_id).warm(),
            }
    }
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
    /// its own thread. Before a new lane starts, room is made for it within `budget` bytes.
    pub(crate) fn place(
        &self,
        run: &Path,
        thread: Option<&str>,
        digest: &str,
        kept: impl FnOnce(&Status) -> bool,
        budget: u64,
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
        make_room(run, self, budget);
        Ok(Place::Free)
    }

    pub(crate) fn inspect(&self) -> io::Result<Status> {
        self.ask(&Control::Inspect)
    }

    /// The lane runs, answers, holds its conversation, and is free for a claim: a turn sent to it
    /// starts at once. A lane still warm with no session is not yet ready.
    pub(crate) fn warm(&self) -> bool {
        self.inspect()
            .is_ok_and(|state| state.available && !state.warming)
    }

    /// Hands a lane that is warm with no session its conversation ([`Control::Take`]).
    #[allow(
        dead_code,
        reason = "the hand-over of drivers that warm beside a turn (issue 761); held by its tests"
    )]
    pub(crate) fn take(&self, thread: &str) -> io::Result<Status> {
        self.ask(&Control::Take {
            thread: thread.to_owned(),
        })
    }

    /// What the lane that said `state` holds in memory, in bytes: what its driver read itself
    /// where it says it, else its process and every one below it, read from `/proc`.
    pub(crate) fn memory(state: &Status) -> Option<u64> {
        state.memory_bytes.or_else(|| {
            (state.instance != 0)
                .then(|| memory::of(state.instance))
                .flatten()
        })
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
        self.ask::<Status>(&Control::Retire)?;
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

/// Keeps this Host's lanes, of every tool, within `budget` bytes together, the one about to start
/// (`own`) included at its tool's typical size. A lane's memory is what its driver says, or what
/// is read of its process tree, or its tool's typical size where neither is there. The idle lanes
/// that held no turn the longest are asked to leave first, until the rest fit. A lane that runs a
/// turn, or whose work may continue, refuses and counts. Read only when a lane is about to start.
fn make_room(run: &Path, own: &Endpoint, budget: u64) {
    let Ok(entries) = std::fs::read_dir(run) else {
        return;
    };
    let mut idle = Vec::new();
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
        let memory = Endpoint::memory(&state).unwrap_or_else(|| lane.tool.typical_bytes());
        held = held.saturating_add(memory);
        if state.claim.is_none() && state.available {
            idle.push((state.idle_ms, memory, lane));
        }
    }
    idle.sort_by_key(|(idle_ms, _, _)| std::cmp::Reverse(*idle_ms));
    for (_, memory, lane) in idle {
        if held <= budget {
            break;
        }
        if lane.ask::<Status>(&Control::Retire).is_ok() {
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
