//! Turns by the machine's pressure (`turn_pressure_admission`): while the kernel says the
//! machine stalls on its processors, its disks or its memory, the turns nobody is waiting on
//! (helpers, janitors, one agent asking another, a goal's wake, a trigger) wait before their
//! tool is started; the owner's own turns, and the Oracle's with his words, never do.
//!
//! - What is read: the kernel's pressure files (`/proc/pressure/cpu`, `io`, `memory`), the share
//!   of time tasks waited, over the last 10 and 60 seconds ([`Reading`]). A machine without
//!   them is never under pressure.
//! - When the machine is under pressure ([`Thresholds`]): io `some` avg60 at or above 30, cpu
//!   `some` avg60 at or above 60, or memory `full` avg10 at or above 10, each the owner's
//!   setting in the Host's `config.toml` ([`crate::lane::Settings`]). It is calm again only
//!   once every one of them is below 80% of its threshold, so a reading that wavers about a
//!   threshold does not hold and let go turn by turn.
//! - A turn that waits is let go when the machine is calm again, or [`BOUND`] after it began to
//!   wait, whichever comes first, so nothing waits for ever; one at a time, the pressure read
//!   again between two ([`Gate::tick`]).
//! - Nothing is read on a timer while no turn waits: the pressure is read when a turn is about
//!   to start, when the apps ask (`GET /lane-settings`), and every [`EVERY`] while a turn waits.

use std::collections::{BTreeMap, VecDeque};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, OnceLock, PoisonError};
use std::time::Duration;

use clarp_ports::clock::Clock;

/// The owner's settings, top-level keys of the Host's `config.toml`, in percent.
pub(crate) const CPU: &str = "pressure_cpu_percent";
pub(crate) const IO: &str = "pressure_io_percent";
pub(crate) const MEMORY: &str = "pressure_memory_percent";
/// What the owner may set each threshold to.
pub(crate) const LEAST: u32 = 5;
pub(crate) const MOST: u32 = 95;

/// How long a turn waits at most, whatever the pressure.
pub(crate) const BOUND: Duration = Duration::from_secs(10 * 60);
/// How often the pressure is read while a turn waits.
pub(crate) const EVERY: Duration = Duration::from_secs(5);
/// How long after a turn was let go the next one may be: the pressure is read again first.
pub(crate) const BETWEEN: Duration = Duration::from_secs(2);
/// How far back the turns that started without a lane are counted.
pub(crate) const COLD_WITHIN: Duration = Duration::from_secs(60 * 60);
/// The most cold starts kept: an hour of them on a busy Host, with room to spare.
const COLD_KEPT: usize = 10_000;

/// The turns that started without a lane in the last hour (`cold_starts_last_hour`): what keeps
/// a machine busy is the tools started anew for each turn, not the processes that live.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub(crate) struct ColdStarts {
    pub total: usize,
    /// By the agent's tool (its backend).
    pub tools: BTreeMap<String, usize>,
}

/// The share of time, in percent, tasks waited over the last 10 and 60 seconds.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub(crate) struct Stall {
    pub avg10: f64,
    pub avg60: f64,
}

/// One pressure file: `some` (at least one task waited) and, for io and memory, `full` (every
/// task that could run waited).
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub(crate) struct Resource {
    pub some: Stall,
    pub full: Option<Stall>,
}

impl Resource {
    /// Of the text of a pressure file:
    /// `some avg10=1.00 avg60=2.00 avg300=3.00 total=4`, and a `full` line like it.
    pub(crate) fn of(text: &str) -> Option<Self> {
        Some(Self {
            some: stall(text, "some")?,
            full: stall(text, "full"),
        })
    }
}

fn stall(text: &str, kind: &str) -> Option<Stall> {
    let rest = text
        .lines()
        .find_map(|line| line.strip_prefix(kind)?.strip_prefix(' '))?;
    let field = |name: &str| {
        rest.split_whitespace()
            .find_map(|word| word.strip_prefix(name)?.strip_prefix('='))
            .and_then(|value| value.parse::<f64>().ok())
            .filter(|value| value.is_finite())
    };
    Some(Stall {
        avg10: field("avg10")?,
        avg60: field("avg60")?,
    })
}

/// Where the CPU figure was read.
#[derive(Debug, Clone, Copy, Default, PartialEq, Eq)]
pub(crate) enum CpuSource {
    /// The Host's own cgroup (`cpu.pressure` of the cgroup `/proc/self/cgroup` names): the
    /// Host's own threads waiting for a processor. What runs in the agents' turns, in scopes of
    /// their own, is not in it: a render in a turn keeps the machine's figure high for hours,
    /// and holding new work for it would hold everything behind work nobody can stop.
    HostCgroup,
    /// The machine's (`/proc/pressure/cpu`), where the Host's cgroup cannot be read.
    #[default]
    System,
}

impl CpuSource {
    pub(crate) fn name(self) -> &'static str {
        match self {
            Self::HostCgroup => "host-cgroup",
            Self::System => "system",
        }
    }
}

/// The machine's pressure now. A file that is not there or cannot be read is `None`, and
/// counts as no pressure.
#[derive(Debug, Clone, Copy, Default, PartialEq)]
pub(crate) struct Reading {
    pub cpu: Option<Resource>,
    pub cpu_source: CpuSource,
    pub io: Option<Resource>,
    pub memory: Option<Resource>,
}

/// The kernel's own folder of pressure files.
const PROC_PRESSURE: &str = "/proc/pressure";

/// The `cpu.pressure` of the cgroup the text of `/proc/self/cgroup` names (its cgroup v2 line,
/// `0::<path>`), under `/sys/fs/cgroup`.
pub(crate) fn cgroup_cpu_of(text: &str) -> Option<PathBuf> {
    let path = text.lines().find_map(|line| line.strip_prefix("0::"))?;
    Some(
        Path::new("/sys/fs/cgroup")
            .join(path.trim().trim_start_matches('/'))
            .join("cpu.pressure"),
    )
}

/// Where the Host's own CPU pressure is read, beside the folder `dir`: on a machine
/// (`/proc/pressure`), its own cgroup's file; in a folder a test names, that folder's `host-cpu`.
fn host_cpu_of(dir: &Path) -> Option<PathBuf> {
    if dir == Path::new(PROC_PRESSURE) {
        cgroup_cpu_of(&std::fs::read_to_string("/proc/self/cgroup").ok()?)
    } else {
        Some(dir.join("host-cpu"))
    }
}

impl Reading {
    /// The files of the folder `dir` (`/proc/pressure` on a machine): io and memory as the
    /// machine has them, since every process stalls on those; the CPU as the Host's own cgroup
    /// has it, or the machine's where that cannot be read. File reads, no process started.
    pub(crate) fn read(dir: &Path) -> Self {
        let file = |path: &Path| {
            std::fs::read_to_string(path)
                .ok()
                .as_deref()
                .and_then(Resource::of)
        };
        let one = |name: &str| file(&dir.join(name));
        let (cpu, cpu_source) = match host_cpu_of(dir).as_deref().and_then(file) {
            Some(own) => (Some(own), CpuSource::HostCgroup),
            None => (one("cpu"), CpuSource::System),
        };
        Self {
            cpu,
            cpu_source,
            io: one("io"),
            memory: one("memory"),
        }
    }

    /// The three figures the thresholds are of: cpu `some` avg60, io `some` avg60, memory
    /// `full` avg10.
    fn figures(&self) -> [f64; 3] {
        [
            self.cpu.map_or(0.0, |cpu| cpu.some.avg60),
            self.io.map_or(0.0, |io| io.some.avg60),
            self.memory
                .and_then(|memory| memory.full)
                .map_or(0.0, |full| full.avg10),
        ]
    }
}

/// The owner's thresholds, in percent, in the order of [`Reading::figures`].
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub(crate) struct Thresholds {
    pub cpu: u32,
    pub io: u32,
    pub memory: u32,
}

impl Thresholds {
    pub(crate) const DEFAULT: Self = Self {
        cpu: 60,
        io: 30,
        memory: 10,
    };

    /// Of the top-level keys of the Host's `config.toml`; a value outside [`LEAST`]..=[`MOST`]
    /// is taken at the nearer end.
    pub(crate) fn of(file: Option<&toml::Table>) -> Self {
        let percent = |key: &str, default: u32| {
            file.and_then(|file| file.get(key))
                .and_then(toml::Value::as_integer)
                .map_or(default, |n| {
                    u32::try_from(n.clamp(i64::from(LEAST), i64::from(MOST))).unwrap_or(default)
                })
        };
        Self {
            cpu: percent(CPU, Self::DEFAULT.cpu),
            io: percent(IO, Self::DEFAULT.io),
            memory: percent(MEMORY, Self::DEFAULT.memory),
        }
    }

    fn each(self) -> [f64; 3] {
        [self.cpu, self.io, self.memory].map(f64::from)
    }

    /// Whether the machine is under pressure after `reading`, where it was `high` before: it
    /// goes high when one figure reaches its threshold, and calm again only when every figure
    /// is below 80% of its own.
    pub(crate) fn high_after(self, high: bool, reading: &Reading) -> bool {
        let mut pairs = reading.figures().into_iter().zip(self.each());
        if high {
            !pairs.all(|(figure, threshold)| figure < threshold * 0.8)
        } else {
            pairs.any(|(figure, threshold)| figure >= threshold)
        }
    }
}

/// A turn that waits: whose, since when on the Host's clock, and how it is let go.
struct Waiting {
    agent: String,
    turn: u64,
    since_ms: i64,
    go: Box<dyn FnOnce() + Send>,
}

#[derive(Default)]
struct State {
    high: bool,
    waiting: VecDeque<Waiting>,
    /// The thread that reads the pressure while a turn waits runs.
    ticking: bool,
}

/// What the gate saw at one look.
#[derive(Debug, Clone, Copy, PartialEq)]
pub(crate) struct Look {
    pub reading: Reading,
    pub high: bool,
    pub thresholds: Thresholds,
    /// Turns that wait now.
    pub waiting: usize,
}

/// The Host's one gate of turns by the machine's pressure, shared by every agent.
pub struct Gate {
    /// Where the pressure files are. `None`: nowhere, and the machine is never under pressure.
    dir: Option<PathBuf>,
    clock: Arc<dyn Clock + Send + Sync>,
    thresholds: fn() -> Thresholds,
    /// Whether a turn that waits starts the thread that lets it go ([`Gate::tick`]). A unit
    /// test ticks by hand.
    ticks_itself: bool,
    state: Mutex<State>,
    /// When each turn that started without a lane started, on the Host's clock, and its tool.
    cold: Mutex<VecDeque<(i64, String)>>,
}

impl Gate {
    /// The gate of a Host whose pressure files are in `dir`, the owner's thresholds read anew
    /// at each look.
    pub fn new(dir: Option<PathBuf>, clock: Arc<dyn Clock + Send + Sync>) -> Arc<Self> {
        Arc::new(Self {
            dir,
            clock,
            thresholds: || crate::lane::Settings::read().pressure,
            ticks_itself: true,
            state: Mutex::default(),
            cold: Mutex::default(),
        })
    }

    /// A gate that never holds anything: no pressure is read.
    fn off() -> Arc<Self> {
        Self::new(None, Arc::new(clarp_ports::clock::SystemClock))
    }

    fn state(&self) -> std::sync::MutexGuard<'_, State> {
        self.state.lock().unwrap_or_else(PoisonError::into_inner)
    }

    /// Reads the pressure and decides whether the machine is under it.
    fn looked(&self, state: &mut State) -> Look {
        let reading = self.dir.as_deref().map(Reading::read).unwrap_or_default();
        let thresholds = (self.thresholds)();
        state.high = thresholds.high_after(state.high, &reading);
        Look {
            reading,
            high: state.high,
            thresholds,
            waiting: state.waiting.len(),
        }
    }

    /// The pressure read now, as the apps are told it.
    pub(crate) fn look(&self) -> Look {
        self.looked(&mut self.state())
    }

    /// Whether the machine is under pressure now, read now.
    pub(crate) fn high(&self) -> bool {
        self.look().high
    }

    /// A turn of `agent` nobody waits on is about to start: whether it waits instead, because
    /// the machine is under pressure, or other turns wait already and go first. One that waits
    /// is let go by `go`, once.
    pub(crate) fn hold(
        self: &Arc<Self>,
        agent: &str,
        turn: u64,
        go: Box<dyn FnOnce() + Send>,
    ) -> bool {
        let mut state = self.state();
        let look = self.looked(&mut state);
        if !look.high && state.waiting.is_empty() {
            return false;
        }
        state.waiting.push_back(Waiting {
            agent: agent.to_owned(),
            turn,
            since_ms: self.clock.now_ms(),
            go,
        });
        if self.ticks_itself && !state.ticking {
            let gate = Arc::clone(self);
            let spawned = std::thread::Builder::new()
                .name("turn-pressure".into())
                .spawn(move || {
                    let mut wait = EVERY;
                    loop {
                        std::thread::sleep(wait);
                        match gate.tick() {
                            Some(next) => wait = next,
                            None => break,
                        }
                    }
                });
            if let Err(why) = spawned {
                // Nothing could let it go: it does not wait.
                eprintln!("clarp-host: a turn is not held for the machine's pressure: {why}");
                state.waiting.pop_back();
                return false;
            }
            state.ticking = true;
        }
        true
    }

    /// A turn of an agent of `tool` starts without a lane: its tool is started anew.
    pub(crate) fn started_cold(&self, tool: &str) {
        let now = self.clock.now_ms();
        let mut cold = self.cold.lock().unwrap_or_else(PoisonError::into_inner);
        Self::forget_old(&mut cold, now);
        if cold.len() >= COLD_KEPT {
            cold.pop_front();
        }
        cold.push_back((now, tool.to_owned()));
    }

    /// The turns that started without a lane in the last hour.
    pub(crate) fn cold_starts(&self) -> ColdStarts {
        let mut cold = self.cold.lock().unwrap_or_else(PoisonError::into_inner);
        Self::forget_old(&mut cold, self.clock.now_ms());
        let mut counted = ColdStarts {
            total: cold.len(),
            tools: BTreeMap::new(),
        };
        for (_, tool) in cold.iter() {
            *counted.tools.entry(tool.clone()).or_default() += 1;
        }
        counted
    }

    fn forget_old(cold: &mut VecDeque<(i64, String)>, now: i64) {
        let within = i64::try_from(COLD_WITHIN.as_millis()).unwrap_or(i64::MAX);
        while cold
            .front()
            .is_some_and(|(at, _)| now.saturating_sub(*at) >= within)
        {
            cold.pop_front();
        }
    }

    /// A turn that waits was stopped or went another way: it no longer waits.
    pub(crate) fn forget(&self, agent: &str, turn: u64) {
        self.state()
            .waiting
            .retain(|waiting| !(waiting.agent == agent && waiting.turn == turn));
    }

    /// How many turns wait now.
    #[cfg(test)]
    pub(crate) fn waiting(&self) -> usize {
        self.state().waiting.len()
    }

    /// One look while turns wait: the first is let go when the machine is calm, else the first
    /// that has waited [`BOUND`]; at most one each look. When to look next; `None` once nothing
    /// waits, and the thread that ticks ends.
    pub(crate) fn tick(&self) -> Option<Duration> {
        let go = {
            let mut state = self.state();
            if state.waiting.is_empty() {
                state.ticking = false;
                return None;
            }
            let look = self.looked(&mut state);
            let now = self.clock.now_ms();
            let bound = i64::try_from(BOUND.as_millis()).unwrap_or(i64::MAX);
            let first = if look.high {
                state
                    .waiting
                    .iter()
                    .position(|waiting| now.saturating_sub(waiting.since_ms) >= bound)
            } else {
                Some(0)
            };
            first.and_then(|at| state.waiting.remove(at))
        };
        match go {
            Some(waiting) => {
                (waiting.go)();
                Some(BETWEEN)
            }
            None => Some(EVERY),
        }
    }
}

/// The gate of this Host's process, for what is given no handle on it (the lanes).
static SHARED: OnceLock<Arc<Gate>> = OnceLock::new();

/// Makes `gate` the gate of this process. Once, at the Host's start.
pub fn install(gate: Arc<Gate>) {
    if SHARED.set(gate).is_err() {
        eprintln!("clarp-host: the pressure gate was set already");
    }
}

/// The gate of this process; where none was installed (a unit test), one that never holds.
pub(crate) fn shared() -> Arc<Gate> {
    static OFF: OnceLock<Arc<Gate>> = OnceLock::new();
    SHARED
        .get()
        .unwrap_or_else(|| OFF.get_or_init(Gate::off))
        .clone()
}

#[cfg(test)]
#[path = "pressure/tests.rs"]
mod tests;
