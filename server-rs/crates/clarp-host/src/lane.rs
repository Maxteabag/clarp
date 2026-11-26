//! The lane layer: an agent's tool kept alive between its chat turns, in one process under the
//! shim, while the owner's switch for that tool is on (issues 742 and 761). What every lane
//! shares lives here, whatever tool it keeps:
//!
//! - the owner's settings in `config.toml`: one switch per tool, the idle time, the count of
//!   lanes and the memory budget shared by all ([`Settings`]);
//! - where a lane listens, and how it is asked, watched, retired and known to have left
//!   ([`Endpoint`]);
//! - the registry: room is made across every tool's lanes, by default from the memory the
//!   machine has free ([`Memory`], issue 792), else within a fixed count and memory budget, the
//!   longest idle leaving first;
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
mod gate;
mod holder;
pub(crate) mod muse;
pub(crate) mod opencode;
mod scope;

use std::fs::OpenOptions;
use std::io::{self, BufRead, BufReader, Read, Write};
use std::os::unix::net::UnixStream;
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use clarp_shim::lane::{LOGIN_CHANGED, Lifecycle, Status, owner_group, owner_lock};
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
    Muse,
    /// OpenCode's `serve`, for agents of `opencode` and of `deepseek` (issue 763).
    OpenCode,
}

impl Tool {
    pub(crate) const ALL: [Self; 4] = [Self::Codex, Self::Claude, Self::Muse, Self::OpenCode];

    /// The tool of an agent's backend, where that tool has a lane.
    pub(crate) fn of(backend: &str) -> Option<Self> {
        match backend {
            "codex" => Some(Self::Codex),
            "claude" => Some(Self::Claude),
            "muse" => Some(Self::Muse),
            "opencode" | "deepseek" => Some(Self::OpenCode),
            _ => None,
        }
    }

    /// The owner's switch that runs this tool's chat turns on lanes.
    pub(crate) fn switch(self) -> &'static str {
        match self {
            Self::Codex => "codex_chat_lane",
            Self::Claude => "claude_chat_lane",
            Self::Muse => "muse_chat_lane",
            Self::OpenCode => "opencode_chat_lane",
        }
    }

    /// What ends the name of this tool's lane sockets in the run folder, after the agent's id.
    fn suffix(self) -> &'static str {
        match self {
            Self::Codex => "-cxg.sock",
            Self::Claude => "-cll.sock",
            Self::Muse => "-mus.sock",
            Self::OpenCode => clarp_shim::opencode_lane::SOCKET_SUFFIX,
        }
    }

    /// How the tool is named in what the Host answers its apps (`GET /lane-settings`).
    pub(crate) fn key(self) -> &'static str {
        match self {
            Self::Codex => "codex",
            Self::Claude => "claude",
            Self::Muse => "muse",
            Self::OpenCode => "opencode",
        }
    }

    /// How the tool is named in the words of a refusal.
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Codex => "Codex",
            Self::Claude => "Claude",
            Self::Muse => "Muse",
            Self::OpenCode => "OpenCode",
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
            // An idle `muse serve` with its session, measured on Muse 1.4.25 (issue 762):
            // 255 MiB of PSS idle, 294-300 MiB after a few turns.
            Self::Muse => 300 * MIB,
            // An `opencode serve` after a turn or two: 571-590 MiB of PSS (issue 763).
            Self::OpenCode => 590 * MIB,
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
/// How the limits are set: `"auto"` (the default) from the memory the machine has free, or
/// `"fixed"` by [`MOST`] and [`MEMORY_MB`] as before (the owner's decision of 25 Nov 2026,
/// issue 792).
pub(crate) const LIMITS: &str = "lane_limits";
/// In auto: the share of the machine's memory, in percent, kept free of new lanes.
pub(crate) const RESERVE_PERCENT: &str = "lane_reserve_percent";
const RESERVE_PERCENT_DEFAULT: u64 = 20;
/// Once the most lanes in auto, whatever was free. Auto has no count of lanes since the owner's
/// word of 25 Nov 2026 (many live agents are fine; what counts is the memory, and turns that
/// start cold): the key is still read and told, and bounds nothing.
pub(crate) const CEILING: &str = "lane_ceiling";
const CEILING_DEFAULT: usize = 24;

/// How the limits of lanes are set.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Mode {
    /// From the memory the machine has free when a lane starts: a lane starts while what is
    /// free after it stays above the reserve, however many lanes run.
    Auto,
    /// By the count and the memory budget the owner set.
    Fixed,
}

impl Mode {
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::Auto => "auto",
            Self::Fixed => "fixed",
        }
    }

    pub(crate) fn named(name: &str) -> Option<Self> {
        [Self::Auto, Self::Fixed]
            .into_iter()
            .find(|mode| mode.name() == name)
    }

    /// The mode where the owner's config sets none: auto, unless the Host was started with
    /// `--lane-limits-default` (a test's Host runs fixed).
    fn default() -> Self {
        DEFAULT_MODE.get().copied().unwrap_or(Self::Auto)
    }
}

/// Set once at the Host's start from `--lane-limits-default`.
static DEFAULT_MODE: std::sync::OnceLock<Mode> = std::sync::OnceLock::new();

/// The lanes' mode where the owner's config sets none, from the Host's start.
pub(crate) fn set_default_mode(mode: Mode) {
    if DEFAULT_MODE.set(mode).is_err() {
        eprintln!(
            "clarp-host: the lanes' default mode was set already; {} is not taken",
            mode.name()
        );
    }
}

/// What the machine's memory is now, from `/proc/meminfo`.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Memory {
    /// `MemAvailable`: what new processes can have without the machine swapping.
    pub available: u64,
    /// `MemTotal`.
    pub total: u64,
}

impl Memory {
    /// Read now: a file read, no process started. `None` where it cannot be read.
    pub(crate) fn read() -> Option<Self> {
        Self::of(&std::fs::read_to_string("/proc/meminfo").ok()?)
    }

    /// Of the text of `/proc/meminfo`, which counts in kB.
    pub(crate) fn of(text: &str) -> Option<Self> {
        let field = |name: &str| {
            text.lines()
                .find_map(|line| line.strip_prefix(name)?.strip_prefix(':'))
                .and_then(|rest| rest.split_whitespace().next()?.parse::<u64>().ok())
                .map(|kb| kb.saturating_mul(1024))
        };
        Some(Self {
            available: field("MemAvailable")?,
            total: field("MemTotal")?,
        })
    }
}

/// What must stay free of lanes, in auto.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Free {
    /// What the machine had free when the room was read.
    pub available: u64,
    /// What must stay free after a lane starts.
    pub reserve: u64,
}

/// The limits all lanes of a Host are kept within, whatever their tools.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Room {
    /// Live lanes, the one about to start included.
    pub most: usize,
    /// Bytes they may hold together.
    pub budget: u64,
    /// In auto: what the machine must keep free. A lane that leaves gives back what it weighed.
    pub free: Option<Free>,
}

impl Room {
    /// Whether `live` lanes holding `held` bytes fit, a new one of `new` bytes among them, once
    /// lanes that weighed `freed` bytes together have left.
    fn fits(&self, live: usize, held: u64, new: u64, freed: u64) -> bool {
        live <= self.most
            && held <= self.budget
            && self.free.is_none_or(|free| {
                free.available.saturating_add(freed) >= free.reserve.saturating_add(new)
            })
    }
}

/// The owner's settings of lanes, in this Host's `config.toml`, read anew at each use so they
/// change without a build or a restart. Top-level keys, before any table:
///
/// ```toml
/// codex_chat_lane = true        # Codex chat turns run on the agent's lane (default false)
/// claude_chat_lane = true       # Claude chat turns run on a kept Claude Code (default false)
/// muse_chat_lane = true         # Muse chat turns run on the agent's lane (default false)
/// opencode_chat_lane = true     # OpenCode and DeepSeek chat turns run on the agent's kept
///                               # `opencode serve` (default false)
/// codex_lane_idle_minutes = 15  # a lane with no turn and no goal leaves after this long
/// lane_limits = "auto"          # from the memory free (default), or "fixed" by the two below
/// lane_reserve_percent = 20     # auto: of the machine's memory, kept free of new lanes
/// lane_ceiling = 24             # read and told, and bounds nothing: auto has no count
/// codex_lane_most = 8           # fixed: live lanes this Host keeps, of every tool
/// lane_memory_mb = 2200         # fixed: and what they may hold together (PSS); a lane starts
///                               # only when both allow it, the longest idle one leaving first
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
    pub mode: Mode,
    /// In auto: percent of the machine's memory kept free of new lanes.
    pub reserve_percent: u64,
    /// Read and told; it bounds nothing ([`CEILING`]).
    pub ceiling: usize,
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
            mode: file
                .and_then(|file| file.get(LIMITS))
                .and_then(toml::Value::as_str)
                .and_then(Mode::named)
                .unwrap_or_else(Mode::default),
            reserve_percent: u64::try_from(number(
                RESERVE_PERCENT,
                i64::try_from(RESERVE_PERCENT_DEFAULT).unwrap_or(20),
            ))
            .unwrap_or(RESERVE_PERCENT_DEFAULT)
            .clamp(5, 50),
            ceiling: usize::try_from(number(
                CEILING,
                i64::try_from(CEILING_DEFAULT).unwrap_or(24),
            ))
            .unwrap_or(CEILING_DEFAULT),
        }
    }

    /// The limits a new lane is started within, the machine's memory read now.
    pub(crate) fn room(&self) -> Room {
        self.room_with(Memory::read())
    }

    /// The limits with the machine's memory as `memory` says. In auto, a machine whose memory
    /// cannot be read keeps the fixed limits.
    pub(crate) fn room_with(&self, memory: Option<Memory>) -> Room {
        match (self.mode, memory) {
            // No count: as many lanes as the memory keeps above the reserve.
            (Mode::Auto, Some(memory)) => Room {
                most: usize::MAX,
                budget: u64::MAX,
                free: Some(Free {
                    available: memory.available,
                    reserve: self.reserve_of(memory),
                }),
            },
            _ => Room {
                most: self.most,
                budget: self.budget,
                free: None,
            },
        }
    }

    /// What auto keeps free of new lanes on a machine with `memory`.
    pub(crate) fn reserve_of(&self, memory: Memory) -> u64 {
        memory.total / 100 * self.reserve_percent
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
/// exited: one writer per conversation. Blocking, for at most [`LEAVE_WAIT`]: an idle lane
/// that has not let go by then (its tool hangs on its way out) is ended with its tool and said
/// once in the Host's log, rather than holding the request and the agent's lease (reviews of
/// 25 Nov 2026). False: a lane refused to leave because it runs work of its own, or was too
/// slow to say what it does, and the terminal is refused as it is while any turn of the agent
/// runs (issue #770).
pub(crate) fn make_way_if_idle(run: &Path, agent_id: &str) -> bool {
    make_way_within(run, agent_id, LEAVE_WAIT)
}

/// How long a turn or the terminal waits for a lane asked to leave to let go, before the lane
/// is ended by its process id and the turn or terminal goes on without it (issue #772): no
/// turn waits on a lane's exit without a bound.
pub(crate) const LEAVE_WAIT: Duration = Duration::from_secs(15);
/// How long a turn waits for a lane on its way out (refused for its login, retired or idle, its
/// socket gone and its owner lock still held) to let go before it starts the lane in its place:
/// a lane's tool is given 2 s to end before its group is ended (the shim's `EXIT_LIMIT`), and a
/// cold start costs about as much as this. A turn never waits the 15 s of [`LEAVE_WAIT`] for one:
/// one that has not let go by then is left as it is, the turn runs in a process of its own at
/// once, and the lane starts for the next turn (issue 860). Retirements, and any wait no turn
/// stands behind, keep [`LEAVE_WAIT`].
pub(crate) const TURN_LEAVE_WAIT: Duration = Duration::from_secs(3);
/// How long a lane asked to leave for another's room is given to refuse: a refusal comes at
/// once, and an ask taken by a Claude lane is answered only once its tool has gone.
const REFUSAL_LOOK: Duration = Duration::from_millis(500);
/// How often a lane asked to leave for another's room is looked at for its socket going.
const LEAVING_LOOK: Duration = Duration::from_millis(20);
/// How long a lane ended by its process id is given to have let go of its owner lock.
const ENDED_WAIT: Duration = Duration::from_secs(2);
/// How often a bounded wait looks at the owner lock again.
const RELEASE_LOOK: Duration = Duration::from_millis(100);

fn make_way_within(run: &Path, agent_id: &str, bound: Duration) -> bool {
    // Every tool's lane is asked, also after one refused. Each is retired as a turn's would be
    // (issue 861): from when it is seen leaving, TURN_LEAVE_WAIT before what is left of an idle
    // lane is ended; before that, at most `bound`.
    let mut made_way = true;
    for tool in Tool::ALL {
        made_way &= Endpoint::of(tool, run, agent_id)
            .retire_for_a_turn_within(bound, TURN_LEAVE_WAIT.min(bound))
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

/// A turn of its own process holds its conversation's lock (under `locks`, named by `thread`)
/// shared for its whole life: `flock --shared --nonblock --no-fork LOCK` before its program. A
/// lane holds the same lock exclusive for as long as its tool lives, so the conversation never
/// has two writers; a turn that finds it held fails at once. `shim` is the launcher's own shim,
/// named here for a program given as `clarp-shim`, which the launcher would otherwise name.
pub(crate) fn guard_exec(
    order: &mut clarp_actor::ShimOrder,
    locks: &Path,
    thread: &str,
    shim: &Path,
) -> io::Result<()> {
    std::fs::create_dir_all(locks)?;
    let lock = clarp_shim::lane::thread_lock(locks, thread);
    if order.program == "clarp-shim" {
        order.program = shim.to_string_lossy().into_owned();
    }
    let program = std::mem::replace(&mut order.program, "flock".into());
    let args = std::mem::take(&mut order.args);
    order.args = vec![
        "--shared".into(),
        "--nonblock".into(),
        "--no-fork".into(),
        lock.to_string_lossy().into_owned(),
        program,
    ];
    order.args.extend(args);
    Ok(())
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

/// What a lane's start may do.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) enum Want {
    /// A native goal's lane: it starts even past the limits, and counts.
    Needed,
    /// A chat turn's lane: it starts within the limits, else its turn runs in a process of its
    /// own (`Place::Full`).
    Optional,
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

/// A lane's refusal of a claim because the tool's login changed since it started
/// ([`LOGIN_CHANGED`]): the lane has left, and nothing of the attempt reached the tool. Told by
/// its type alone, never by an error's kind: a permission error on the lane's socket is no
/// changed login (issue 854).
#[derive(Debug)]
pub(crate) struct LoginChanged(String);

impl std::fmt::Display for LoginChanged {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(&self.0)
    }
}

impl std::error::Error for LoginChanged {}

impl LoginChanged {
    /// Whether `error` is this refusal, as a lane's answer returns it.
    pub(crate) fn is(error: &io::Error) -> bool {
        error
            .get_ref()
            .is_some_and(|inner| inner.is::<LoginChanged>())
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
        self.place_within(run, (thread, digest), kept, room, needed, LEAVE_WAIT)
    }

    /// [`Self::place`], waiting at most `bound` for a lane that must make way to let go.
    fn place_within(
        &self,
        run: &Path,
        (thread, digest): (Option<&str>, &str),
        kept: impl FnOnce(&Status) -> bool,
        room: Room,
        needed: bool,
        bound: Duration,
    ) -> io::Result<Place> {
        let mut look = self.inspect();
        // Nobody listens on its socket, and its owner lock is held: a lane on its way out
        // (retired, idle, or refused for its login: it stops answering before it ends its tool,
        // and lets go of the lock up to seconds later), or one that is starting (every lane takes
        // its lock before it binds its socket; OpenCode's starts its server in between, about
        // 1.5 s). It is waited for, at most TURN_LEAVE_WAIT since a turn may stand behind this
        // start, until its lock is let go, or its socket answers, and then looked at as any
        // lane that answers. It is never ended from here (issue 860).
        if matches!(&look, Err(error) if gone(error)) && !self.released()? {
            let seen = Instant::now();
            look = self.answers_or_lets_go(seen + TURN_LEAVE_WAIT.min(bound), look);
            // Neither let go nor answered: the turn goes on without it, and a lane that hangs
            // while leaving is ended in the background, no turn waiting on it (issue 861).
            if matches!(&look, Err(error) if gone(error)) && !self.released()? {
                self.end_if_hung_later(seen);
            }
        }
        if let Ok(state) = look {
            let same_thread = state.thread == thread.unwrap_or_default();
            if same_thread && state.options == digest && !kept(&state) {
                return Ok(Place::Kept);
            }
            // A lane that does not let go in time has been ended with its tool, where it may
            // be; the look at its owner lock below says whether it has gone. One that holds this
            // turn's conversation is retired as a turn's must be, never run beside (one writer);
            // one of another thread is retired while the turn waits at most TURN_LEAVE_WAIT,
            // and carries on without it (issue 861).
            let retired = if same_thread {
                self.retire_for_a_turn_within(bound, TURN_LEAVE_WAIT.min(bound))
            } else {
                self.retire_beside(bound, TURN_LEAVE_WAIT.min(bound))
            };
            if retired.is_err() {
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
            // Said, so a turn that ran cold for want of a place is known in the Host's log.
            eprintln!(
                "clarp-host: no place for the {} lane of {}: no idle lane that may leave for it made room; its turn runs in a process of its own",
                self.tool.name(),
                self.agent().unwrap_or("an agent"),
            );
            Ok(Place::Full)
        }
    }

    /// Waits until `until` for a lane nobody answers for while its owner lock is held: until the
    /// lock is let go (it was leaving), or its socket answers (it was starting), whichever comes
    /// first. Never ends it. What the lane answered, or `missing` (the look that found nobody)
    /// where it did not.
    fn answers_or_lets_go(
        &self,
        until: Instant,
        missing: io::Result<Status>,
    ) -> io::Result<Status> {
        loop {
            if self.released()? {
                return missing;
            }
            let left = until.saturating_duration_since(Instant::now());
            if left.is_zero() {
                return missing;
            }
            if self.socket.exists()
                && let Ok(state) = self
                    .connect(&Lifecycle::Inspect, left)
                    .and_then(|stream| replied::<Status>(&mut BufReader::new(stream), self.tool))
            {
                return Ok(state);
            }
            std::thread::sleep(left.min(RELEASE_LOOK));
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

    /// Asks an idle lane to leave and waits at most `bound` for it to have gone: `true` once its
    /// owner lock is free. It is asked what it does first, and never ended while it works: a
    /// lane busy with a turn may be slow to answer (Codex loads a long conversation for up to
    /// 25 s), and a slow answer is no sign of a lane on its way out (review of 25 Nov 2026).
    /// - A lane that answers it works, or does not say, is asked to leave and may refuse, which
    ///   is an error; one that is slow to answer is an error too. Either is waited for, never
    ///   ended.
    /// - A lane that said it was idle answers a retirement only once it has gone, so its answer
    ///   is waited for within the bound; one that has not let go by then is ended with its tool
    ///   ([`Self::let_go_within`]).
    /// - A socket nobody listens on any more is a lane on its way out: it stops answering before
    ///   it ends its tool. It too is ended once the bound has passed.
    ///
    /// `false`: the lane has not let go, and was not ended, or even that did not free it.
    pub(crate) fn retire_within(&self, bound: Duration) -> io::Result<bool> {
        if self.released()? {
            return Ok(true);
        }
        let until = Instant::now() + bound;
        let asked = self
            .connect(&Lifecycle::Inspect, bound)
            .and_then(|stream| replied::<Status>(&mut BufReader::new(stream), self.tool));
        let idle = match asked {
            Ok(state) => state.working == Some(false) && state.claim.is_none(),
            Err(error) if gone(&error) => return self.let_go_within(until, true),
            Err(error) => return Err(error),
        };
        let left = until.saturating_duration_since(Instant::now());
        let retired = self
            .connect(&Lifecycle::Retire, left)
            .and_then(|stream| replied::<Status>(&mut BufReader::new(stream), self.tool));
        match retired {
            Ok(_) => {}
            Err(error) if gone(&error) => {}
            Err(error) if idle && unanswered(&error) => {}
            Err(error) => return Err(error),
        }
        self.let_go_within(until, idle)
    }

    /// Waits until `until` for a lane on its way out to let go of its owner lock. One that has
    /// not by then is ended with its tool, where `may_end` says it may be (it said it was idle,
    /// or it stopped answering on its way out): by its own systemd scope ([`scope`]), which
    /// holds the lane and every process it started; where it has none, by its tool's process
    /// group and its own id. The lane's id is the lock's holder as the kernel names it
    /// ([`holder`]), never the lock file's words alone, and never a name or a pattern. Said once
    /// in the Host's log, and given a moment to be gone. `true`: the lock is free.
    pub(crate) fn let_go_within(&self, until: Instant, may_end: bool) -> io::Result<bool> {
        if self.released_by(until)? {
            return Ok(true);
        }
        let name = self
            .socket
            .file_name()
            .map(|name| name.to_string_lossy().into_owned())
            .unwrap_or_default();
        if !may_end {
            eprintln!(
                "clarp-host: the {} lane {name} did not let go in time; it said it works, or \
                 did not say, so it is left as it is",
                self.tool.name()
            );
            return Ok(false);
        }
        // The lock's holder now, from the kernel: the lock file may still name the lane before
        // this one, which wrote it before this one took the lock and wrote its own.
        let lock = owner_lock(&self.socket);
        let Some(pid) = holder::of(&lock).filter(|pid| may_signal(*pid)) else {
            eprintln!(
                "clarp-host: the {} lane {name} did not let go in time and no process of it \
                 holds its lock; it is left as it is",
                self.tool.name()
            );
            return self.released();
        };
        // A handle on that process, opened before the holder is looked at again: from here it
        // names this process whatever its id comes to name, and the second look shows that it
        // held the lock when the handle was opened.
        let Ok(process) = handle_on(pid) else {
            return self.released();
        };
        if holder::of(&lock) != Some(pid) {
            return self.released();
        }
        // Its own scope ends the lane and its tool together, whatever their process groups: a
        // tool that hangs may not read the end of its input, and with only the lane's process
        // gone it would write the conversation beside the lane or turn that comes next. Where
        // it has no scope of its own (started outside systemd), its tool's group goes first,
        // while the lane is still that group leader's parent, then the lane. Its tool also ends
        // with the lane by itself: the lane starts it with a death signal (`tool-child`).
        let found = scope::of_process(pid);
        // The scope was read by the id: had the process ended meanwhile, the id might have
        // named a process of another lane's scope. Its handle says whether it still lives.
        if process.as_ref().is_some_and(ended) {
            return self.released();
        }
        let how = match found {
            Some(unit) if scope::end(&unit) => {
                format!("its scope {unit} is ended, its tool with it")
            }
            unended => {
                let group = owner_group(&self.socket)
                    .filter(|group| may_signal(*group) && holder::leads_for(*group, pid));
                if let Some(group) = group {
                    signal(group, true);
                }
                match &process {
                    Some(handle) => {
                        let _ = rustix::process::pidfd_send_signal(
                            handle,
                            rustix::process::Signal::KILL,
                        );
                    }
                    None => signal(pid, false),
                }
                let tool = group.map_or_else(
                    || "its tool leads no group of its own any more".to_owned(),
                    |group| format!("its tool's process group {group}"),
                );
                let why = unended.map_or_else(
                    || "it runs in no scope of its own".to_owned(),
                    |unit| format!("its scope {unit} could not be ended"),
                );
                format!("{why}, so its process {pid} is ended, and {tool}")
            }
        };
        eprintln!(
            "clarp-host: the {} lane {name} did not let go in time; {how}",
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

    /// Waits at most `bound` for a lane on its way out to let go of its owner lock, its own proof
    /// that it has gone, and never ends it: what a turn waits for (with [`TURN_LEAVE_WAIT`])
    /// before it starts the lane in its place. `true`: the lock is free.
    pub(crate) fn gone_within(&self, bound: Duration) -> io::Result<bool> {
        let seen = Instant::now();
        let gone = self.released_by(seen + bound)?;
        if !gone {
            self.end_if_hung_later(seen);
        }
        Ok(gone)
    }

    /// The end of a lane that hangs while leaving, which a turn gave up on without ending it
    /// (issue 861): once [`LEAVE_WAIT`] has passed since it was seen leaving, it is ended, on a
    /// thread of its own that no turn waits on, only if its socket still does not answer and its
    /// owner lock is still held. A lane that answers by then (one that was starting) is never
    /// ended. One such watch per lane at a time.
    pub(crate) fn end_if_hung_later(&self, seen: Instant) {
        static WATCHED: std::sync::LazyLock<std::sync::Mutex<std::collections::HashSet<PathBuf>>> =
            std::sync::LazyLock::new(Default::default);
        let watched = |socket: &Path, add: bool| {
            let mut set = WATCHED
                .lock()
                .unwrap_or_else(std::sync::PoisonError::into_inner);
            if add {
                set.insert(socket.to_owned())
            } else {
                set.remove(socket)
            }
        };
        if !watched(&self.socket, true) {
            return;
        }
        let lane = self.clone();
        let spawned = std::thread::Builder::new()
            .name("lane-hung-end".into())
            .spawn(move || {
                lane.end_if_hung_at(seen + LEAVE_WAIT);
                watched(&lane.socket, false);
            });
        if spawned.is_err() {
            watched(&self.socket, false);
        }
    }

    /// What [`Self::end_if_hung_later`] does at `at`: `true` when it ended the lane.
    fn end_if_hung_at(&self, at: Instant) -> bool {
        if self.released_by(at).unwrap_or(true) {
            return false;
        }
        let silent = matches!(self.inspect(), Err(error) if gone(&error));
        if !silent || self.released().unwrap_or(true) {
            return false;
        }
        eprintln!(
            "clarp-host: the {} lane hung while leaving for {} s; it is ended",
            self.tool.name(),
            LEAVE_WAIT.as_secs()
        );
        self.let_go_within(Instant::now(), true).unwrap_or(false)
    }

    /// Asks an idle lane to leave so another may start in its place, and waits for no exit: the
    /// new lane holds no lock of the old one's (another agent's), so it need not wait for the
    /// old tool to end. A lane that refuses (a turn claimed it since it was looked at) says so
    /// at once. One that takes the ask removes its socket first, as every lane leaving does, and
    /// answers at once (Codex, Muse, OpenCode) or once its tool has gone (Claude Code, up to its
    /// own 2 s limit); nothing is waited for beyond [`REFUSAL_LOOK`] (issue 908). Whether it
    /// left for this ask: it said it goes, or its socket went after it was asked. One that
    /// refused, said nothing with its socket still there, or had gone before the ask (another
    /// start's place, or its own idle time) did not.
    fn asked_to_leave(&self) -> bool {
        let Ok(stream) = self.connect(&Lifecycle::Retire, LEAVING_LOOK) else {
            return false;
        };
        let until = Instant::now() + REFUSAL_LOOK;
        let mut input = BufReader::new(stream);
        let mut said = String::new();
        loop {
            match input.read_line(&mut said) {
                Ok(0) => return !self.socket.exists(),
                Ok(_) => return replied::<Status>(&mut said.as_bytes(), self.tool).is_ok(),
                Err(error) if unanswered(&error) => {
                    // On its way out: its socket is gone, and its answer may wait for its exit.
                    if !self.socket.exists() {
                        return true;
                    }
                    if Instant::now() >= until {
                        return false;
                    }
                }
                Err(_) => return !self.socket.exists(),
            }
        }
    }

    /// Retires a lane of another thread, within `bound`, on a thread of its own: the caller waits
    /// at most `wait` for its answer, and goes on without it after that, while the retirement
    /// carries on. An error when it refused, or had not let go by then.
    fn retire_beside(&self, bound: Duration, wait: Duration) -> io::Result<bool> {
        let (sender, answer) = std::sync::mpsc::channel();
        let lane = self.clone();
        std::thread::Builder::new()
            .name("lane-retire-beside".into())
            .spawn(move || {
                let _ = sender.send(lane.retire_within(bound));
            })?;
        match answer.recv_timeout(wait) {
            Ok(retired) => retired,
            Err(_) => Err(io::Error::new(
                io::ErrorKind::TimedOut,
                format!("the {} lane is still leaving", self.tool.name()),
            )),
        }
    }

    /// The retirement a turn stands behind: the agent's lane must let go of the conversation
    /// before the turn may write it (one writer), so a lane that refuses because it runs work
    /// is an error and the turn queues, as for [`Self::retire_within`]. The turn's time is
    /// counted from when the lane is seen to begin leaving, its socket gone, which every lane
    /// removes first: [`TURN_LEAVE_WAIT`] from then (the 2 s its own clock gives its tool, and
    /// a second), after which an idle lane is ended with what is left of it, so no end lands
    /// inside its own clean exit or flush. Before it begins leaving (still in its loop), the
    /// wait is capped by [`LEAVE_WAIT`] from the ask, as a retirement's (issue 861).
    pub(crate) fn retire_for_a_turn(&self) -> io::Result<bool> {
        self.retire_for_a_turn_within(LEAVE_WAIT, TURN_LEAVE_WAIT)
    }

    /// [`Self::retire_for_a_turn`] with the cap before leaving and the wait after it given.
    fn retire_for_a_turn_within(&self, cap: Duration, after_leaving: Duration) -> io::Result<bool> {
        if self.released()? {
            return Ok(true);
        }
        let capped = Instant::now() + cap;
        let mut asked = self
            .connect(&Lifecycle::Inspect, cap)
            .and_then(|stream| replied::<Status>(&mut BufReader::new(stream), self.tool));
        // Nobody on its socket with its lock held: on its way out, seen now, or starting.
        if matches!(&asked, Err(error) if gone(error)) {
            asked = self.answers_or_lets_go(Instant::now() + after_leaving, asked);
            if matches!(&asked, Err(error) if gone(error)) {
                return if self.released()? {
                    Ok(true)
                } else {
                    self.let_go_within(Instant::now(), true)
                };
            }
        }
        let state = asked?;
        let idle = state.working == Some(false) && state.claim.is_none();
        // The answer to the ask to leave comes once the lane has gone (Claude's) or at once:
        // read beside the wait, which watches the lane itself.
        let (sender, answer) = std::sync::mpsc::channel();
        let lane = self.clone();
        let left = capped.saturating_duration_since(Instant::now());
        let _ = std::thread::Builder::new()
            .name("lane-retire".into())
            .spawn(move || {
                let retired = lane
                    .connect(&Lifecycle::Retire, left)
                    .and_then(|stream| replied::<Status>(&mut BufReader::new(stream), lane.tool));
                let _ = sender.send(retired);
            });
        let mut seen_leaving = None;
        // Whether the lane took the ask (answered it, or went away from it), as
        // retire_within reads it.
        let mut took = false;
        loop {
            if self.released()? {
                return Ok(true);
            }
            match answer.try_recv() {
                Ok(Ok(_)) => took = true,
                Ok(Err(error)) if gone(&error) => took = true,
                Ok(Err(error)) if idle && unanswered(&error) => {}
                Ok(Err(error)) => return Err(error),
                Err(_) => {}
            }
            let now = Instant::now();
            if seen_leaving.is_none() && !self.socket.exists() {
                seen_leaving = Some(now);
            }
            let deadline = seen_leaving.map_or(capped, |seen| seen + after_leaving);
            if now >= deadline {
                // A lane that works, or did not say, is never ended: one that took the ask is
                // left as it is; one that did not answer it did not leave.
                if !idle {
                    if took {
                        return self.let_go_within(now, false);
                    }
                    return Err(io::Error::new(
                        io::ErrorKind::TimedOut,
                        format!("the {} lane did not leave", self.tool.name()),
                    ));
                }
                return self.let_go_within(now, true);
            }
            std::thread::sleep(deadline.saturating_duration_since(now).min(RELEASE_LOOK));
        }
    }

    /// A retirement response only acknowledges the request. The process
    /// releases the owner lock after reaping its tool.
    ///
    /// The look takes the lock shared, for a moment: the lane holds it exclusive, so a lane that
    /// is there still refuses it, while two of the Host's own looks never refuse each other. A
    /// look that took it exclusive made a lane that had left seem there to another thread looking
    /// at the same moment (on a seat switch, while other agents' lanes made room), and a turn's
    /// start of the next lane failed for it (issue 857).
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
        match file.try_lock_shared() {
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

/// Starts a lane in place of one that has just left (refused for its login, or idle and of
/// another login): a start that fails, which may be one that still found the old lane's owner
/// there a moment after it was seen gone, is tried once more once that owner has let go. All of
/// it within `bound`, counted from before the first try, so a start's own wait and this one
/// never add up: on a turn's path [`TURN_LEAVE_WAIT`]. Never more than twice (issues 857, 860).
pub(crate) fn start_once_more<T>(
    lane: &Endpoint,
    bound: Duration,
    mut start: impl FnMut() -> io::Result<T>,
) -> io::Result<T> {
    let until = Instant::now() + bound;
    match start() {
        Err(first) => {
            if lane.released_by(until).unwrap_or(false) {
                eprintln!(
                    "clarp-host: a {} lane start is tried once more: {first}",
                    lane.tool.name()
                );
                start()
            } else {
                Err(first)
            }
        }
        started => started,
    }
}

/// An attempt is offered to a lane once more when the first offer `first` failed and nothing of
/// it reached the tool (its `spool` is not there): the login changed and the lane left, or an
/// idle lane left between the look and the offer (its own idle time, another agent's room). A
/// lane that refused for its login is on its way out and is waited for; any other may still be
/// there, and is looked at again by `again` rather than waited for.
/// - A chat turn (`turn`) waits at most [`TURN_LEAVE_WAIT`] and ends nothing: a lane that has
///   not let go by then is left to leave on its own (and ended in the background should it
///   hang), and the refusal comes back, so the turn runs in a process of its own (issue 861).
/// - Anything else (a goal's attempt, which no process of its own can run) waits as a
///   retirement does: one that does not let go in time is ended by its process id, and `again`
///   finds out whether it has gone.
pub(crate) fn once_more<T>(
    first: io::Result<T>,
    spool: &Path,
    lane: &Endpoint,
    turn: bool,
    again: impl FnOnce() -> io::Result<T>,
) -> io::Result<T> {
    match first {
        Err(error) if !spool.try_exists().unwrap_or(true) => {
            if LoginChanged::is(&error) {
                if turn {
                    if !lane.gone_within(TURN_LEAVE_WAIT)? {
                        return Err(error);
                    }
                } else {
                    lane.let_go_within(Instant::now() + LEAVE_WAIT, true)?;
                }
            }
            again()
        }
        other => other,
    }
}

/// Keeps this Host's lanes, of every tool, the one about to start (`own`) included, within the
/// limits of `room`: at most `room.most` of them, holding at most `room.budget` bytes together,
/// and in auto with the machine's free memory, less the new lane's typical size, at or above the
/// reserve, each lane that leaves giving back what it weighed (issue 792).
/// A lane counts at what it weighed itself (`Status::memory_bytes`), or at its tool's typical
/// size where it has not said one or said nothing (its `/proc` could not be read); the one about
/// to start at its typical size. The idle lanes that held no turn the longest are asked to leave
/// first, until both limits hold. A lane that runs a turn, or whose work may continue, or that a
/// turn has claimed, is never chosen, and counts. Read only when a lane is about to start. The
/// Host reads no `/proc` here: each lane weighed itself, in its own process.
///
/// Where the limits cannot be reached (the machine's memory was taken by other programs, or the
/// owner lowered the limits below what is held), a lane still starts in the place of idle ones:
/// the fewest idle lanes, in the same order, that pay for it, each counted at least at what a new
/// lane of its tool counts at, so one idle Claude lane makes room for one Claude lane (issue
/// 908). A turn refused its lane runs its tool in a process of its own, as big as the lane and
/// started cold, so refusing while idle lanes sit there only adds to what is held.
///
/// The owner's agents come first (his rule of 25 Nov 2026). The idle lanes of agents he is not
/// talking with leave first, longest idle first, then idle lanes of his, longest idle first. A
/// lane for an agent of his may send any idle lane away; a lane of the agents' own may send
/// away only idle lanes of the agents' own, so it never takes a lane the owner's next turn is
/// about to use. A start of his is counted at the [`gate`] before it looks at the lanes: a start
/// of the agents' own that chooses while his is still looking leaves room for his, so where one
/// place can be made it is his. Nobody waits for another's start.
///
/// What may leave is reckoned first. A lane that is not `needed` sends nothing away unless what
/// may leave makes room for it, within the limits or in the place of idle lanes: a start that
/// will not happen ends no lane. A `needed` lane starts whatever the answer, so it sends away
/// what it may, toward the limits. Only a lane that left counts as a place: one that refused,
/// said nothing, or had gone before it was asked does not, and a plan within the limits is
/// checked against the limits again once they were asked, never taken as paid. Answers whether
/// the new lane has its place: `false` when no place could be made.
fn make_room(run: &Path, own: &Endpoint, room: Room, needed: bool) -> bool {
    let for_owner = own.agent().is_some_and(|agent| owners(run, agent));
    let gate = gate::of(run);
    let new = own.tool.typical_bytes();
    // His start is counted at the gate before it looks at the lanes: a start of the agents' own
    // that chooses meanwhile leaves room for it. Nobody waits for it.
    let first = for_owner.then(|| gate.owner_first(new));
    let (mut live, mut held, growing, idle) = idle_in_order(run, &own.socket, for_owner);
    // What lanes started moments ago will still take is not free, whatever the machine says now.
    let room = Room {
        free: room.free.map(|free| Free {
            available: free.available.saturating_sub(growing),
            ..free
        }),
        ..room
    };
    live += 1;
    held = held.saturating_add(new);
    let start = held;
    // The limits with the new lane counted, and the owner's starts still choosing beside it.
    let fits = |live: usize, held: u64, ahead: gate::Ahead| {
        room.fits(
            live + ahead.count,
            held.saturating_add(ahead.bytes),
            new.saturating_add(ahead.bytes),
            start - held,
        )
    };
    let planned = gate.choose(first, |seen| {
        let ahead = seen.ahead;
        // A lane another start chose, or that has gone since it was looked at, is no place.
        let idle: Vec<Leaving> = idle
            .into_iter()
            .filter(|(_, _, _, lane)| seen.free(&lane.socket))
            .collect();
        let wanted = new.saturating_add(ahead.bytes);
        let within = |live: usize, held: u64| fits(live, held, ahead);
        let Some(plan) = to_leave(&idle, live, held, wanted, &within, needed) else {
            return (None, Vec::new());
        };
        let chosen: Vec<Leaving> = idle.into_iter().take(plan.leaving()).collect();
        let sockets = chosen.iter().map(|lane| lane.3.socket.clone()).collect();
        (Some((plan, ahead, chosen)), sockets)
    });
    let Some((plan, ahead, chosen)) = planned else {
        return false;
    };
    // Only a lane that left counts as a place: one that said it goes, or whose socket went
    // after it was asked. One that refused (a turn claimed it), said nothing, or had gone
    // before the ask, counts as it was.
    let (mut left, mut paid) = (0usize, 0u64);
    let mut asked = Vec::with_capacity(chosen.len());
    for (_, _, memory, lane) in &chosen {
        let went = lane.asked_to_leave();
        if went {
            live -= 1;
            held = held.saturating_sub(*memory);
            left += 1;
            paid = paid.saturating_add(pays(lane, *memory));
        }
        asked.push((lane.socket.as_path(), went));
    }
    gate.asked(asked);
    match plan {
        // Within the limits only: those that left are checked as they are, and a plan that
        // needed more of them to leave never turns into one that pays.
        Plan::Fits(_) | Plan::Toward(_) => fits(live, held, ahead),
        Plan::Paid(_) => {
            let placed = left > 0 && paid >= new.saturating_add(ahead.bytes);
            if placed {
                eprintln!(
                    "clarp-host: over the lanes' limits, {left} idle lane(s) left to make room for the {} lane of {}",
                    own.tool.name(),
                    own.agent().unwrap_or("an agent"),
                );
            }
            placed
        }
    }
}

/// How idle lanes make room for a new one ([`to_leave`]): how many of them, in order, leave.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Plan {
    /// The fewest after which every limit holds.
    Fits(usize),
    /// No count of them brings the lanes within the limits: the fewest that pay for the new lane
    /// ([`pays`]), whose place it takes.
    Paid(usize),
    /// A `needed` lane where no count fits: every one, toward the limits.
    Toward(usize),
}

impl Plan {
    fn leaving(self) -> usize {
        match self {
            Self::Fits(count) | Self::Paid(count) | Self::Toward(count) => count,
        }
    }
}

/// How many of `idle`, in order, leave for a new lane of `new` bytes, with `live` lanes holding
/// `held` bytes, the new one counted. Every count is tried for the limits (`fits`) first, and
/// the fewest that fits is taken: fewer leave for nothing. Only where none fits, not even all of
/// them, does the new lane take the place of the fewest that pay for it, and a `needed` lane
/// (which starts anyway) sends them all away instead. `None`: the lane does not start.
fn to_leave(
    idle: &[Leaving],
    live: usize,
    held: u64,
    new: u64,
    fits: &impl Fn(usize, u64) -> bool,
    needed: bool,
) -> Option<Plan> {
    let (mut would_live, mut would_hold) = (live, held);
    let (mut paid, mut pays_at) = (0u64, None);
    for (count, (_, _, memory, lane)) in idle.iter().enumerate() {
        if fits(would_live, would_hold) {
            return Some(Plan::Fits(count));
        }
        would_live -= 1;
        would_hold = would_hold.saturating_sub(*memory);
        paid = paid.saturating_add(pays(lane, *memory));
        if pays_at.is_none() && paid >= new {
            pays_at = Some(count + 1);
        }
    }
    let all = idle.len();
    if fits(would_live, would_hold) {
        Some(Plan::Fits(all))
    } else if needed {
        Some(Plan::Toward(all))
    } else {
        pays_at.map(Plan::Paid)
    }
}

/// What an idle lane that leaves pays toward a new one: what it weighed, and never less than a
/// new lane of its own tool counts at, so a lane makes room for one like it.
fn pays(lane: &Endpoint, weighed: u64) -> u64 {
    weighed.max(lane.tool.typical_bytes())
}

/// An idle lane that may be asked to leave: whether it is the owner's, how long it has been
/// idle, what it weighed, and where it listens.
type Leaving = (bool, u64, u64, Endpoint);

/// How long after its start a lane is still taken to grow: Codex loads the conversation on its
/// first turn, which a weighing before it does not show.
const YOUNG: Duration = Duration::from_secs(60);

/// What a lane that has not weighed itself yet, or is younger than [`YOUNG`], may still grow to:
/// its tool's typical size beyond what it said, never below 0. Counted against what the machine
/// has free, so a burst of starts, each reading the same free memory, cannot overshoot the reserve
/// (review of 25 Nov 2026).
fn still_to_grow(lane: &Endpoint, state: &Status) -> u64 {
    let said = state.memory_bytes.unwrap_or(0);
    let young = std::fs::metadata(&lane.socket)
        .and_then(|meta| meta.modified())
        .ok()
        .and_then(|made| made.elapsed().ok())
        .is_none_or(|age| age < YOUNG);
    if said == 0 || young {
        lane.tool.typical_bytes().saturating_sub(said)
    } else {
        0
    }
}

/// The lanes of the run folder but `skip`: how many answer and what they weigh together, and of
/// them the idle ones that may leave, in the order they leave (the owner's rule of 25 Nov 2026):
/// the agents' own first, then his, each longest idle first. His are among them only `for_owner`.
fn idle_in_order(run: &Path, skip: &Path, for_owner: bool) -> (usize, u64, u64, Vec<Leaving>) {
    let mut idle = Vec::new();
    let (mut live, mut held, mut growing) = (0usize, 0u64, 0u64);
    for (lane, state) in answering(run, skip) {
        let memory = weight(&lane, &state);
        live += 1;
        held = held.saturating_add(memory);
        growing = growing.saturating_add(still_to_grow(&lane, &state));
        if state.claim.is_none() && state.available {
            let his = lane.agent().is_some_and(|agent| owners(run, agent));
            if for_owner || !his {
                idle.push((his, state.idle_ms, memory, lane));
            }
        }
    }
    idle.sort_by_key(|(his, idle_ms, _, _)| (*his, std::cmp::Reverse(*idle_ms)));
    (live, held, growing, idle)
}

/// In auto, with less free than the reserve: idle lanes leave, in the order room is made (the
/// agents' own, then the owner's, each longest idle first), until what they weighed brings the
/// machine back above it. A lane that runs work never leaves. How many left. Blocking.
pub(crate) fn shed(run: &Path, settings: &Settings, memory: Option<Memory>) -> usize {
    let Some(memory) = memory.filter(|_| settings.mode == Mode::Auto) else {
        return 0;
    };
    let reserve = settings.reserve_of(memory);
    if memory.available >= reserve {
        return 0;
    }
    let (_, _, _, idle) = idle_in_order(run, Path::new(""), true);
    let (mut freed, mut left) = (0u64, 0usize);
    for (_, _, weighed, lane) in idle {
        if memory.available.saturating_add(freed) >= reserve {
            break;
        }
        if lane.ask::<Status>(&Lifecycle::Retire).is_ok() {
            freed = freed.saturating_add(weighed);
            left += 1;
        }
    }
    left
}

/// The kernel's memory pressure as a trigger (`/proc/pressure/memory`): told when tasks waited
/// for memory 10% of two seconds, the shortest window a process that is not root may ask for.
const PRESSURE: &[u8] = b"some 200000 2000000\0";

/// Sheds idle lanes when the kernel says memory is short: a lane that starts makes its own room,
/// but between starts a machine can run short with nothing starting. Its own thread waits on the
/// kernel's trigger, never a timer. Where the kernel has no pressure file or refuses the trigger,
/// it says so once and ends: lanes still keep within the reserve at each start. Blocking.
pub(crate) fn watch_pressure(run: &Path) {
    use nix::poll::{PollFd, PollFlags, PollTimeout, poll};
    use std::os::fd::AsFd;
    let opened = OpenOptions::new()
        .read(true)
        .write(true)
        .open("/proc/pressure/memory")
        .and_then(|mut file| file.write_all(PRESSURE).map(|()| file));
    let trigger = match opened {
        Ok(trigger) => trigger,
        Err(why) => {
            eprintln!(
                "clarp-host: lanes are not told of memory pressure ({why}); they keep within the reserve as they start"
            );
            return;
        }
    };
    loop {
        let mut told = [PollFd::new(trigger.as_fd(), PollFlags::POLLPRI)];
        match poll(&mut told, PollTimeout::NONE) {
            Ok(_) => {}
            Err(nix::errno::Errno::EINTR) => continue,
            Err(why) => {
                eprintln!("clarp-host: the memory pressure trigger failed: {why}");
                return;
            }
        }
        let said = told[0].revents().unwrap_or(PollFlags::empty());
        if said.intersects(PollFlags::POLLERR | PollFlags::POLLNVAL) {
            eprintln!("clarp-host: the memory pressure trigger ended ({said:?})");
            return;
        }
        let left = shed(run, &Settings::read(), Memory::read());
        if left > 0 {
            eprintln!("clarp-host: memory was short: {left} idle lane(s) asked to leave");
        }
    }
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

/// A refusal for a changed login is a [`LoginChanged`]: the lane has left, and nothing of the
/// attempt reached the tool.
fn replied<T: DeserializeOwned>(input: &mut impl BufRead, tool: Tool) -> io::Result<T> {
    let reply: Reply<T> = serde_json::from_slice(&line(input, tool)?).map_err(io::Error::other)?;
    let login_changed = reply.code.as_deref() == Some(LOGIN_CHANGED);
    reply.state.ok_or_else(|| {
        let words = reply
            .error
            .unwrap_or_else(|| format!("{} lane supplied no state", tool.name()));
        if login_changed {
            io::Error::other(LoginChanged(words))
        } else {
            io::Error::other(words)
        }
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

/// A socket nobody listens on any more, or a connection closed without an answer: what a lane
/// on its way out gives, which is no refusal. A lane stops answering before it ends its tool.
fn gone(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::UnexpectedEof
            | io::ErrorKind::NotFound
            | io::ErrorKind::ConnectionRefused
            | io::ErrorKind::ConnectionReset
            | io::ErrorKind::BrokenPipe
    )
}

/// An answer that did not come in time: from a lane that said it was idle, one on its way out
/// (its answer to a retirement comes only once it has gone); from any other, perhaps a busy one.
fn unanswered(error: &io::Error) -> bool {
    matches!(
        error.kind(),
        io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut
    )
}

/// Whether an id may be signalled: never one that would reach more than one process or group by
/// itself (0, 1), nor this Host's own process or group.
fn may_signal(id: u32) -> bool {
    id > 1
        && id != std::process::id()
        && i32::try_from(id).is_ok_and(|raw| raw != nix::unistd::getpgrp().as_raw())
}

/// A handle on the process `pid` (`pidfd`), through which it is signalled whatever its id comes
/// to name. `Err`: the process is gone. `Ok(None)`: this kernel has no handles on processes.
fn handle_on(pid: u32) -> io::Result<Option<std::os::fd::OwnedFd>> {
    let Ok(raw) = i32::try_from(pid) else {
        return Ok(None);
    };
    let Some(id) = rustix::process::Pid::from_raw(raw) else {
        return Ok(None);
    };
    match rustix::process::pidfd_open(id, rustix::process::PidfdFlags::empty()) {
        Ok(handle) => Ok(Some(handle)),
        Err(error) if error == rustix::io::Errno::SRCH => Err(io::Error::from(error)),
        Err(_) => Ok(None),
    }
}

/// Whether the process a handle (`pidfd`) is on has ended: its handle is readable then.
fn ended(process: &std::os::fd::OwnedFd) -> bool {
    use nix::poll::{PollFd, PollFlags, PollTimeout, poll};
    use std::os::fd::AsFd;
    let mut looked = [PollFd::new(process.as_fd(), PollFlags::POLLIN)];
    let ready = poll(&mut looked, PollTimeout::ZERO);
    ready.is_ok_and(|ready| ready > 0)
}

/// Ends with SIGKILL a lane's tool's process `group` (whose leader was checked to be the lane's
/// child, and keeps its id until the lane reaps it), or, on a kernel without handles on
/// processes, the lane's process `id`, which the kernel named as its lock's holder right before.
/// The lane's process is otherwise signalled through its handle (`pidfd`).
fn signal(id: u32, group: bool) {
    let Ok(raw) = i32::try_from(id) else {
        return;
    };
    let target = nix::unistd::Pid::from_raw(raw);
    let kill = nix::sys::signal::Signal::SIGKILL;
    let _ = if group {
        nix::sys::signal::killpg(target, kill)
    } else {
        nix::sys::signal::kill(target, kill)
    };
}

#[cfg(test)]
#[path = "lane/tests.rs"]
mod tests;
