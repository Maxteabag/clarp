//! `clarp-shim`: owns one tool process for as long as it lives.
//!
//!     clarp-shim --spool FILE --socket PATH [--cwd DIR] [--stdin-file FILE] [--prompt-as-file] -- PROGRAM [ARGS...]
//!
//! It makes the turn's spool, starts PROGRAM in its own process group, appends every line it
//! prints to the spool, listens on the socket for the server's few requests, and when the program
//! is gone writes how it went and exits with the same code. It knows nothing about any tool: the
//! lines are opaque to it. It never exits before the program does, and it does not care whether
//! a server is there.
//!
//! The order of the first steps is what makes a turn start at most once:
//!
//! 1. The spool is created, and creating it fails when it is already there. Of two shims started
//!    for the same turn, one gets the turn and the other leaves without starting anything.
//! 2. The program is started, and "spawned" is written to the spool and made durable.
//! 3. Only then is the program given the message on its standard input, and the shim prints
//!    `ready` on its own standard output for whoever started it.
//!
//! A spool that says "spawned" belongs to a turn that must never be started again. A spool that
//! does not belongs to a turn whose program was never given anything to do.

mod opencode;

use std::io::{BufRead, BufReader, ErrorKind, Read, Write};
use std::os::fd::{AsFd, BorrowedFd, OwnedFd};
use std::os::unix::net::{UnixListener, UnixStream};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{ChildStdin, Command, ExitCode, Stdio};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::mpsc::{self, Sender};
use std::sync::{Arc, Condvar, Mutex, MutexGuard, PoisonError, TryLockError};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use clarp_shim::{
    EXIT_NOT_STARTED, EXIT_TURN_TAKEN, HOLD_MOST_MS, Held, Reply, Request, SOCKET_ENV, take_lines,
};
use clarp_spool::{Entry, MAX_OUTPUT_CHUNK, Record, ShimEvent, SpoolWriter};
use nix::errno::Errno;
use nix::poll::{PollFd, PollFlags, PollTimeout, poll};
use nix::sys::signal::{SigSet, Signal, kill, killpg};
use nix::sys::wait::{Id, WaitPidFlag, WaitStatus, waitid};
use nix::unistd::{Pid, setsid};

/// How long a tool gets to end by itself when the shim is told to go.
const GRACE_ON_SIGNAL_MS: u64 = 5_000;
/// How long a hook's report waits for what the tool printed before it. A tool that never
/// stops printing is not waited for.
const HOOK_WAITS: Duration = Duration::from_millis(250);
/// How much of a tool's output is taken at a time.
const READ_AT_ONCE: usize = 64 * 1024;
/// How long a hold waits between two looks at whether the tool stands still and all it printed
/// is taken.
const HOLD_LOOKS_EVERY: Duration = Duration::from_millis(1);
/// The driver a served tool is run through: the shim's own program, with this word first.
const SERVED: &str = "opencode-turn";
const ENDED_WHILE_HELD: &str = "the tool was ended while it was held";

struct Args {
    before_stop: Option<(Signal, Duration)>,
    spool: PathBuf,
    socket: PathBuf,
    cwd: Option<PathBuf>,
    stdin_file: Option<PathBuf>,
    prompt_as_file: bool,
    named_session: Option<String>,
    program: String,
    program_args: Vec<String>,
}

fn parse(mut argv: impl Iterator<Item = String>) -> Result<Args, String> {
    let mut before_stop = None;
    let (mut spool, mut socket, mut cwd, mut stdin_file) = (None, None, None, None);
    let mut named_session = None;
    let mut prompt_as_file = false;
    loop {
        match argv.next().as_deref() {
            Some("--before-stop") => {
                let signal = argv
                    .next()
                    .ok_or("missing direct child signal")?
                    .parse()
                    .map_err(|_| "invalid direct child signal")?;
                let ms: u64 = argv
                    .next()
                    .ok_or("missing stop wait")?
                    .parse()
                    .map_err(|_| "invalid stop wait")?;
                before_stop = Some((signal, Duration::from_millis(ms)));
            }
            Some("--spool") => spool = argv.next().map(PathBuf::from),
            Some("--socket") => socket = argv.next().map(PathBuf::from),
            Some("--named-session") => {
                named_session = Some(argv.next().ok_or("missing session id")?)
            }
            Some("--cwd") => cwd = argv.next().map(PathBuf::from),
            Some("--prompt-as-file") => prompt_as_file = true,
            Some("--stdin-file") => stdin_file = argv.next().map(PathBuf::from),
            Some("--") => break,
            Some(other) => return Err(format!("unknown argument {other}")),
            None => return Err("missing -- PROGRAM".to_string()),
        }
    }
    let program = argv.next().ok_or("missing PROGRAM after --")?;
    Ok(Args {
        before_stop,
        spool: spool.ok_or("missing --spool FILE")?,
        socket: socket.ok_or("missing --socket PATH")?,
        cwd,
        stdin_file,
        prompt_as_file,
        named_session,
        program,
        program_args: argv.collect(),
    })
}

fn now_ms() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| i64::try_from(d.as_millis()).unwrap_or(i64::MAX))
        .unwrap_or(0)
}

/// What the threads send to the one thread that owns the spool.
enum ToSpool {
    Entry(Entry),
    /// Write, make durable, and tell the sender it is done.
    Now(Entry, Sender<()>),
    /// Durably end this spool, then drop its receiver so later signals and hooks cannot append.
    Last(Entry, Sender<()>),
    /// A connection that wants a newline whenever the spool has grown.
    Follow(UnixStream),
    /// Say how far the spool goes, once everything sent before this is written.
    End(Sender<std::io::Result<u64>>),
}

/// What is sent to the one thread that owns the tool's standard input.
enum ToStdin {
    Data(Vec<u8>),
    Close,
}

/// Appends one entry. A spool that cannot be written is a turn nobody will ever read; there is
/// no one to tell but stderr, and the tool goes on regardless.
fn write(writer: &mut SpoolWriter, entry: Entry, durable: bool) {
    let record = Record { t: now_ms(), entry };
    let written = writer
        .append(&record)
        .and_then(|()| if durable { writer.sync() } else { Ok(()) });
    if let Err(error) = written {
        eprintln!("clarp-shim: cannot write the spool: {error}");
    }
}

fn spool_thread(mut writer: SpoolWriter, entries: mpsc::Receiver<ToSpool>) {
    let mut followers: Vec<UnixStream> = Vec::new();
    for message in entries {
        let last = matches!(&message, ToSpool::Last(..));
        match message {
            ToSpool::Entry(entry) => {
                let durable = matches!(entry, Entry::Shim(_));
                write(&mut writer, entry, durable);
            }
            ToSpool::Now(entry, done) | ToSpool::Last(entry, done) => {
                write(&mut writer, entry, true);
                let _ = done.send(());
            }
            ToSpool::Follow(follower) => followers.push(follower),
            ToSpool::End(answer) => {
                let _ = answer.send(writer.end());
            }
        }
        // Whoever follows is told there is more to read. A follower that is not reading already
        // has a hint waiting and is not waited for; one that is gone is forgotten.
        followers.retain_mut(|follower| match follower.write(b"\n") {
            Ok(_) => true,
            Err(error) => error.kind() == ErrorKind::WouldBlock,
        });
        if last {
            return;
        }
    }
}

/// The order of what the tool prints with what its hooks report.
///
/// The two reach the shim on different ways: output through a pipe, a hook's report through
/// the socket. A hook is the tool's own child and the tool waits for it, so what the tool
/// printed before it called the hook is in the pipe when the hook reports. The spool is the
/// record of the turn, and a record in the wrong order stays wrong: so output is taken from
/// the pipe by whoever holds `turn`, and a hook's report is recorded by whoever holds it
/// while nothing waits in the pipe.
struct Printed {
    turn: Mutex<()>,
    /// The reading end of the tool's stream, to ask whether something waits in it.
    pipe: OwnedFd,
    /// How many bytes the pump holds that are not a whole line yet. Changed only by whoever
    /// holds `turn`.
    partial: AtomicUsize,
}

/// Whether something waits to be read. `wait` waits until something does, or until the other
/// end is closed.
fn waits(fd: BorrowedFd<'_>, wait: bool) -> bool {
    let timeout = if wait {
        PollTimeout::NONE
    } else {
        PollTimeout::ZERO
    };
    loop {
        let mut asked = [PollFd::new(fd, PollFlags::POLLIN)];
        match poll(&mut asked, timeout) {
            Err(Errno::EINTR) => {}
            Err(_) => return false,
            Ok(_) => {
                return asked[0]
                    .revents()
                    .is_some_and(|seen| seen.contains(PollFlags::POLLIN));
            }
        }
    }
}

impl Printed {
    fn new(pipe: OwnedFd) -> Self {
        Self {
            turn: Mutex::new(()),
            pipe,
            partial: AtomicUsize::new(0),
        }
    }

    /// Whether something waits in the pipe. `wait` waits until something does, or until the
    /// tool has closed its end.
    fn waits(&self, wait: bool) -> bool {
        waits(self.pipe.as_fd(), wait)
    }

    /// Records something after every whole line the tool has printed so far.
    fn after_what_was_printed<T>(&self, record: impl FnOnce() -> T) -> T {
        let until = Instant::now() + HOOK_WAITS;
        loop {
            let turn = match self.turn.try_lock() {
                Ok(turn) => Some(turn),
                Err(TryLockError::Poisoned(poisoned)) => Some(poisoned.into_inner()),
                Err(TryLockError::WouldBlock) => None,
            };
            let late = Instant::now() >= until;
            if late || (turn.is_some() && !self.waits(false)) {
                return record();
            }
            drop(turn);
            std::thread::sleep(Duration::from_micros(200));
        }
    }
}

/// Copies a stream of the tool into the spool, a record per line ([`take_lines`]). With
/// `printed`, the stream is taken a turn at a time, so that a hook's report finds its place in
/// it and a hold can count what was printed.
fn pump(
    mut stream: impl Read,
    to_spool: &Sender<ToSpool>,
    wrap: fn(String) -> Entry,
    printed: Option<&Printed>,
) {
    let mut held = Vec::new();
    let mut chunk = vec![0; READ_AT_ONCE];
    let record = |line: String| to_spool.send(ToSpool::Entry(wrap(line))).is_ok();
    loop {
        // Waiting for the tool is done without the turn: a hook is not kept waiting by a
        // tool that prints nothing.
        let turn = printed.map(|printed| {
            printed.waits(true);
            printed.turn.lock().unwrap_or_else(PoisonError::into_inner)
        });
        let read = match stream.read(&mut chunk) {
            Ok(read) => read,
            Err(error) if error.kind() == ErrorKind::Interrupted => continue,
            Err(_) => 0,
        };
        if read == 0 {
            // What the tool printed last, without the end of a line, recorded in the same turn.
            if !held.is_empty() {
                record(String::from_utf8_lossy(&held).into_owned());
            }
            if let Some(printed) = printed {
                printed.partial.store(0, Ordering::SeqCst);
            }
            return;
        }
        held.extend_from_slice(&chunk[..read]);
        if !take_lines(&mut held, MAX_OUTPUT_CHUNK, record) {
            return;
        }
        if let Some(printed) = printed {
            printed.partial.store(held.len(), Ordering::SeqCst);
        }
        drop(turn);
    }
}

fn stdin_thread(mut stdin: ChildStdin, requests: mpsc::Receiver<ToStdin>) {
    for request in requests {
        match request {
            ToStdin::Data(data) => {
                if stdin.write_all(&data).and_then(|()| stdin.flush()).is_err() {
                    break; // the tool closed its input; there is nothing left to feed
                }
            }
            ToStdin::Close => break,
        }
    }
    // Dropping the handle closes the pipe.
}

#[derive(Default)]
enum StopStage {
    #[default]
    Ready,
    Asked,
    Signalled,
    Finished,
}

struct BeforeStop {
    signal: Signal,
    wait: Duration,
}

/// Where a turn is with its one hold.
#[derive(Default)]
enum Hold {
    /// Never held.
    #[default]
    Free,
    /// Every process of the tool's group is sent SIGSTOP, and is let go at `until` at the latest.
    Held { until: Instant },
    /// Let go: by a release, or by the bound.
    Released,
    /// Killed while it was held, without running again.
    Ended,
}

/// One connection to the socket. A request read from it is read and carried out by whoever holds
/// `turn`, so that a hold can see that no hook's report is half taken.
struct Connection {
    /// A second name for the socket, to ask whether something waits in it.
    socket: OwnedFd,
    turn: Mutex<()>,
    /// Whether a whole request has been read from it and waits to be carried out.
    pending: AtomicBool,
}

impl Connection {
    /// Whether nothing of a request is in its way: nothing waits in the socket or was read.
    fn idle(&self) -> bool {
        !self.pending.load(Ordering::SeqCst) && !waits(self.socket.as_fd(), false)
    }
}

fn lock<T>(held: &Mutex<T>) -> MutexGuard<'_, T> {
    held.lock().unwrap_or_else(PoisonError::into_inner)
}

/// Whether every thread of every process of the group `group` stands still: stopped, or dead.
/// What `/proc` cannot say of a process is a process that is gone.
fn stands_still(group: i32) -> bool {
    let Ok(all) = std::fs::read_dir("/proc") else {
        return false;
    };
    for entry in all.flatten() {
        let Some(pid) = entry.file_name().to_str().and_then(|n| n.parse::<i32>().ok()) else {
            continue;
        };
        match state_and_group(Path::new(&format!("/proc/{pid}/stat"))) {
            Some((_, member)) if member == group => {}
            _ => continue,
        }
        let Ok(tasks) = std::fs::read_dir(format!("/proc/{pid}/task")) else {
            continue;
        };
        for task in tasks.flatten() {
            if let Some((state, _)) = state_and_group(&task.path().join("stat"))
                && !matches!(state, 'T' | 'Z')
            {
                return false;
            }
        }
    }
    true
}

/// The state and the process group in a `stat` of `/proc`. The name before them is in brackets
/// and may hold anything, brackets and spaces too: what follows the last bracket is read.
fn state_and_group(stat: &Path) -> Option<(char, i32)> {
    let stat = std::fs::read_to_string(stat).ok()?;
    let mut after = stat[stat.rfind(')')? + 1..].split_whitespace();
    let state = after.next()?.chars().next()?;
    let group = after.nth(1)?.parse().ok()?;
    Some((state, group))
}

struct Control {
    before_stop: Option<BeforeStop>,
    pid: u32,
    leader_exited: AtomicBool,
    stage: Mutex<StopStage>,
    changed: Condvar,
    to_spool: Sender<ToSpool>,
    to_stdin: Sender<ToStdin>,
    /// The tool's standard output, when its order with the hooks can be kept.
    printed: Option<Arc<Printed>>,
    /// The tool's standard error, taken the same way.
    errors: Option<Arc<Printed>>,
    /// Whether the tool is a server run through the shim's own driver: it is never held.
    served: bool,
    hold: Mutex<Hold>,
    hold_changed: Condvar,
    /// A second name for the socket's listener, to ask whether a connection waits to be taken.
    /// A connection is taken only by whoever holds `connections`.
    listener: Option<OwnedFd>,
    connections: Mutex<Vec<Arc<Connection>>>,
}

impl Control {
    fn handle(self: &Arc<Self>, request: Request, own: &Arc<Connection>) -> Reply {
        match request {
            Request::Hold { ms } => self.hold(ms, own),
            Request::Release => Reply {
                let_go: Some(self.let_go()),
                ..Reply::ok(Some(self.pid))
            },
            Request::Ping | Request::Follow => Reply::ok(Some(self.pid)),
            Request::Stdin { data } => match self.to_stdin.send(ToStdin::Data(data.into_bytes())) {
                Ok(()) => Reply::ok(Some(self.pid)),
                Err(_) => Reply::failed("the tool's input is closed"),
            },
            Request::CloseStdin => {
                let _ = self.to_stdin.send(ToStdin::Close);
                Reply::ok(Some(self.pid))
            }
            Request::Hook { name, data } => {
                let record = || {
                    self.to_spool
                        .send(ToSpool::Entry(Entry::Hook { name, data }))
                };
                let recorded = match &self.printed {
                    Some(printed) => printed.after_what_was_printed(record),
                    None => record(),
                };
                match recorded {
                    Ok(()) => Reply::ok(Some(self.pid)),
                    Err(_) => Reply::failed("the spool is closed"),
                }
            }
            // Carried out by whoever answers, after the answer is written (`serve`).
            Request::Stop { .. } => Reply::ok(Some(self.pid)),
        }
    }

    /// Signals the tool's whole process group: the tool leads it, so this reaches what it started.
    fn signal(pid: u32, to_spool: &Sender<ToSpool>, signal: Signal) {
        let stopping = ShimEvent::Stopping {
            signal: signal.as_str().to_string(),
        };
        let _ = to_spool.send(ToSpool::Entry(Entry::Shim(stopping)));
        let _ = killpg(
            Pid::from_raw(i32::try_from(pid).unwrap_or(i32::MAX)),
            signal,
        );
    }

    fn stop(&self, grace_ms: u64) {
        let mut stage = self.stage.lock().unwrap_or_else(PoisonError::into_inner);
        if !matches!(*stage, StopStage::Ready) {
            return;
        }
        *stage = StopStage::Asked;
        // A held tool is killed where it stands. A polite signal would have to let it run to take
        // it, and a tool that runs prints.
        {
            let mut hold = self.hold.lock().unwrap_or_else(PoisonError::into_inner);
            if matches!(*hold, Hold::Held { .. }) {
                *hold = Hold::Ended;
                self.hold_changed.notify_all();
                drop(hold);
                Self::signal(self.pid, &self.to_spool, Signal::SIGKILL);
                *stage = StopStage::Signalled;
                self.changed.notify_all();
                return;
            }
        }
        if let Some(early) = &self.before_stop
            && !self.leader_exited.load(Ordering::SeqCst)
        {
            let pid = Pid::from_raw(self.pid as i32);
            let _ = kill(pid, early.signal);
            (stage, _) = self
                .changed
                .wait_timeout_while(stage, early.wait, |_| {
                    !self.leader_exited.load(Ordering::SeqCst)
                })
                .unwrap_or_else(PoisonError::into_inner);
        }
        let term_at = Instant::now();
        Self::signal(self.pid, &self.to_spool, Signal::SIGTERM);
        drop(stage);
        // The deadline belongs to the whole group, even if its leader has exited.
        std::thread::sleep(Duration::from_millis(grace_ms).saturating_sub(term_at.elapsed()));
        Self::signal(self.pid, &self.to_spool, Signal::SIGKILL);
        let mut stage = self.stage.lock().unwrap_or_else(PoisonError::into_inner);
        *stage = StopStage::Signalled;
        self.changed.notify_all();
    }

    fn group(&self) -> Pid {
        Pid::from_raw(i32::try_from(self.pid).unwrap_or(i32::MAX))
    }

    /// Holds the tool: every process of its group stands still until it is let go, and the
    /// answer says how far the spool goes with all it printed before.
    fn hold(self: &Arc<Self>, ms: u64, own: &Arc<Connection>) -> Reply {
        if self.served {
            return Reply::failed("a served tool cannot be held");
        }
        let until = {
            let stage = self.stage.lock().unwrap_or_else(PoisonError::into_inner);
            if self.leader_exited.load(Ordering::SeqCst) {
                return Reply::failed("the tool is gone");
            }
            if !matches!(*stage, StopStage::Ready) {
                return Reply::failed("the tool is being stopped");
            }
            let mut hold = self.hold.lock().unwrap_or_else(PoisonError::into_inner);
            if !matches!(*hold, Hold::Free) {
                return Reply::failed("the turn has been held once already");
            }
            let until = Instant::now() + Duration::from_millis(ms.min(HOLD_MOST_MS));
            *hold = Hold::Held { until };
            until
        };
        // The bound holds whatever the Host does: a thread of its own lets the tool go.
        let control = self.clone();
        std::thread::spawn(move || control.let_go_at_the_bound());
        if let Err(error) = self.stand_still(until, || stands_still(self.pid as i32)) {
            return Reply::failed(error);
        }
        let counted = self.count(own, until);
        let hold = lock(&self.hold);
        let left = until.saturating_duration_since(Instant::now());
        match (counted, &*hold) {
            (_, Hold::Ended) => Reply::failed(ENDED_WHILE_HELD),
            (Some(mut held), Hold::Held { .. }) if !left.is_zero() => {
                held.left_ms = u64::try_from(left.as_millis()).unwrap_or(u64::MAX);
                Reply {
                    held: Some(held),
                    ..Reply::ok(Some(self.pid))
                }
            }
            _ => {
                drop(hold);
                self.let_go();
                Reply::failed("what the tool printed could not be counted within the bound")
            }
        }
    }

    /// Tells the group to stop until `still` says every thread of it stands still. A signal is
    /// not instant, and a process that runs may have forked while it went out: so the group is
    /// told again, under the hold's lock and only while it is held, never after the bound has let
    /// it go.
    fn stand_still(&self, until: Instant, still: impl Fn() -> bool) -> Result<(), &'static str> {
        loop {
            let hold = lock(&self.hold);
            match *hold {
                Hold::Ended => return Err(ENDED_WHILE_HELD),
                Hold::Held { .. } if Instant::now() < until => {
                    let _ = killpg(self.group(), Signal::SIGSTOP);
                }
                _ => {
                    drop(hold);
                    self.let_go();
                    return Err("the tool did not stand still within the bound");
                }
            }
            drop(hold);
            if still() {
                return Ok(());
            }
            std::thread::sleep(HOLD_LOOKS_EVERY);
        }
    }

    /// Takes everything the stopped group printed into the spool, and says how far the spool
    /// goes. Nothing is in a pipe, no pump has a chunk in hand, no hook's report is half taken
    /// and no connection waits to be taken, all at once, and then the one writer of the spool is
    /// asked for its length after everything sent to it before.
    fn count(&self, own: &Arc<Connection>, until: Instant) -> Option<Held> {
        let (Some(out), Some(err)) = (self.printed.as_deref(), self.errors.as_deref()) else {
            return None;
        };
        loop {
            if Instant::now() >= until {
                return None;
            }
            let connections = lock(&self.connections);
            let waiting = self
                .listener
                .as_ref()
                .is_none_or(|listener| waits(listener.as_fd(), false));
            let mut turns = Vec::new();
            let mut idle = !waiting;
            for connection in connections.iter().filter(|c| !Arc::ptr_eq(c, own)) {
                if !idle {
                    break;
                }
                turns.push(lock(&connection.turn));
                idle = connection.idle();
            }
            if idle {
                let _out = lock(&out.turn);
                let _err = lock(&err.turn);
                if !out.waits(false) && !err.waits(false) {
                    let (answer, end) = mpsc::channel();
                    self.to_spool.send(ToSpool::End(answer)).ok()?;
                    let spool = end.recv().ok()?.ok()?;
                    return Some(Held {
                        spool,
                        partial_out: out.partial.load(Ordering::SeqCst) as u64,
                        partial_err: err.partial.load(Ordering::SeqCst) as u64,
                        left_ms: 0,
                    });
                }
            }
            drop(turns);
            drop(connections);
            std::thread::sleep(Duration::from_micros(200));
        }
    }

    /// Lets a held tool go on. Says whether it was held.
    fn let_go(&self) -> bool {
        let mut hold = self.hold.lock().unwrap_or_else(PoisonError::into_inner);
        if !matches!(*hold, Hold::Held { .. }) {
            return false;
        }
        *hold = Hold::Released;
        let _ = killpg(self.group(), Signal::SIGCONT);
        self.hold_changed.notify_all();
        true
    }

    fn let_go_at_the_bound(&self) {
        let mut hold = self.hold.lock().unwrap_or_else(PoisonError::into_inner);
        while let Hold::Held { until } = *hold {
            let left = until.saturating_duration_since(Instant::now());
            if left.is_zero() {
                drop(hold);
                self.let_go();
                return;
            }
            (hold, _) = self
                .hold_changed
                .wait_timeout(hold, left)
                .unwrap_or_else(PoisonError::into_inner);
        }
    }
}

/// Answers one connection, a request a line. A request is read from the socket and carried out
/// under the connection's turn ([`Connection`]); the answer is written after it.
fn serve(connection: UnixStream, me: &Arc<Connection>, control: &Arc<Control>) {
    let Ok(mut read_half) = connection.try_clone() else {
        return;
    };
    let mut write_half = connection;
    let mut taken: Vec<u8> = Vec::new();
    let mut chunk = [0u8; 8 * 1024];
    let mut ended = false;
    while !ended {
        // Waiting for the next request is done without the turn.
        if !me.pending.load(Ordering::SeqCst) {
            waits(me.socket.as_fd(), true);
        }
        let turn = lock(&me.turn);
        if !me.pending.load(Ordering::SeqCst) {
            match read_half.read(&mut chunk) {
                Err(error) if error.kind() == ErrorKind::Interrupted => continue,
                Ok(0) if taken.is_empty() => break,
                Ok(0) => ended = true,
                Ok(read) => taken.extend_from_slice(&chunk[..read]),
                Err(_) => break,
            }
        }
        // A whole line, or what is left when the other end has finished.
        let end = match taken.iter().position(|byte| *byte == b'\n') {
            Some(at) => at,
            None if ended => taken.len(),
            None => continue,
        };
        let mut line: Vec<u8> = taken.drain(..end).collect();
        if !taken.is_empty() {
            taken.remove(0);
        }
        me.pending.store(taken.contains(&b'\n'), Ordering::SeqCst);
        if line.last() == Some(&b'\r') {
            line.pop();
        }
        let Ok(line) = String::from_utf8(line) else {
            break;
        };
        if line.trim().is_empty() {
            continue;
        }
        let request = serde_json::from_str::<Request>(&line);
        let follow = matches!(request, Ok(Request::Follow));
        // A stop is answered before it is carried out. A tool that is told to end may be gone
        // at once, and the shim with it: an answer written afterwards may never leave, and
        // whoever asked would read that nobody answered (seen once in a hundred runs of the
        // shim's own test, 17 Nov 2026).
        let stop = match &request {
            Ok(Request::Stop { grace_ms }) => Some(*grace_ms),
            _ => None,
        };
        let reply = match request {
            Ok(request) => control.handle(request, me),
            Err(error) => Reply::failed(format!("not a request: {error}")),
        };
        drop(turn);
        let text = serde_json::to_string(&reply).map(|text| text + "\n");
        let written = text
            .as_ref()
            .is_ok_and(|text| write_half.write_all(text.as_bytes()).is_ok());
        // Whether or not the answer could be given, what was asked is done.
        if let Some(grace_ms) = stop {
            control.stop(grace_ms);
        }
        if !written {
            break;
        }
        if follow {
            // From here this connection only carries hints, written by the thread that owns the
            // spool. They must never make that thread wait.
            if write_half.set_nonblocking(true).is_ok() {
                let _ = control.to_spool.send(ToSpool::Follow(write_half));
            }
            break;
        }
    }
    lock(&control.connections).retain(|other| !Arc::ptr_eq(other, me));
}

/// Takes the socket's connections, each under the list of connections: a connection is waiting
/// in the listener or is in the list, never between.
fn accept(listener: &UnixListener, control: &Arc<Control>) {
    loop {
        if !waits(listener.as_fd(), true) {
            std::thread::sleep(Duration::from_millis(1));
        }
        let mut connections = lock(&control.connections);
        let Ok((connection, _)) = listener.accept() else {
            continue;
        };
        let me = match connection.as_fd().try_clone_to_owned() {
            Ok(socket) if connection.set_nonblocking(false).is_ok() => Arc::new(Connection {
                socket,
                turn: Mutex::new(()),
                pending: AtomicBool::new(false),
            }),
            _ => continue,
        };
        connections.push(me.clone());
        drop(connections);
        let control = control.clone();
        std::thread::spawn(move || serve(connection, &me, &control));
    }
}

fn listen(socket: &Path) -> std::io::Result<UnixListener> {
    let _ = std::fs::remove_file(socket);
    if let Some(dir) = socket.parent() {
        std::fs::create_dir_all(dir)?;
    }
    UnixListener::bind(socket)
}

/// Observe an exited child, keeping its pid reserved until its owner reaps it.
fn observe_exit(pid: u32) -> Result<std::process::ExitStatus, String> {
    loop {
        let child = Id::Pid(Pid::from_raw(pid as i32));
        match waitid(child, WaitPidFlag::WEXITED | WaitPidFlag::WNOWAIT) {
            Ok(WaitStatus::Exited(_, code)) => {
                return Ok(std::process::ExitStatus::from_raw(code << 8));
            }
            Ok(WaitStatus::Signaled(_, signal, core)) => {
                return Ok(std::process::ExitStatus::from_raw(
                    signal as i32 | if core { 128 } else { 0 },
                ));
            }
            Ok(_) | Err(Errno::EINTR) => continue,
            Err(e) => return Err(format!("observing child {pid}: {e}")),
        }
    }
}

fn run(args: Args) -> Result<u8, String> {
    // A session of its own: what happens to the server's terminal or process group does not
    // reach a running turn.
    let _ = setsid();
    let mut writer = match SpoolWriter::create(&args.spool) {
        Ok(writer) => writer,
        Err(error) if error.kind() == ErrorKind::AlreadyExists => {
            eprintln!(
                "clarp-shim: {} is already there: another shim has this turn",
                args.spool.display()
            );
            return Ok(EXIT_TURN_TAKEN);
        }
        Err(error) => return Err(format!("{}: {error}", args.spool.display())),
    };
    // From here on the turn is this shim's, and whatever goes wrong is said in the spool.
    if let Some(id) = args.named_session {
        let entry = Entry::Shim(ShimEvent::Session { id });
        writer
            .append(&Record { t: now_ms(), entry })
            .and_then(|()| writer.sync())
            .map_err(|error| format!("cannot record the assigned session: {error}"))?;
    }
    let mut not_started = |error: String| -> Result<u8, String> {
        write(
            &mut writer,
            Entry::Shim(ShimEvent::SpawnFailed { error }),
            true,
        );
        let _ = std::fs::remove_file(&args.socket);
        Ok(EXIT_NOT_STARTED)
    };
    // The actor already wrote the turn's input into its private file. A tool
    // which needs a seekable prompt file opens it by path until the turn ends.
    let prompt_path = if args.prompt_as_file {
        let Some(path) = args.stdin_file.as_ref() else {
            return not_started("--prompt-as-file requires --stdin-file".into());
        };
        Some(path)
    } else {
        None
    };
    let message = match args
        .stdin_file
        .as_ref()
        .filter(|_| !args.prompt_as_file)
        .map(std::fs::read)
        .transpose()
    {
        Ok(message) => message,
        Err(error) => return not_started(format!("the message could not be read: {error}")),
    };
    let listener = match listen(&args.socket) {
        Ok(listener) => listener,
        Err(error) => return not_started(format!("{}: {error}", args.socket.display())),
    };
    let mut command = Command::new(&args.program);
    command
        .args(&args.program_args)
        .args(prompt_path)
        .stdin(if args.prompt_as_file {
            Stdio::null()
        } else {
            Stdio::piped()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .process_group(0)
        .env(SOCKET_ENV, &args.socket);
    if let Some(cwd) = &args.cwd {
        command.current_dir(cwd);
    }
    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => return not_started(format!("{}: {error}", args.program)),
    };
    let pid = child.id();

    // The tool was started with the signals as they were. From here this thread, and every
    // thread it starts, leaves them to the one thread that waits for them: a shim that is told
    // to go ends its tool first and says so in the spool.
    let mut signals = SigSet::empty();
    signals.add(Signal::SIGTERM);
    signals.add(Signal::SIGINT);
    signals.add(Signal::SIGHUP);
    let signals_blocked = signals.thread_block().is_ok();

    let (to_spool, spool_entries) = mpsc::channel();
    std::thread::spawn(move || spool_thread(writer, spool_entries));
    let record_now = |entry: Entry| {
        let (done, wait) = mpsc::channel();
        if to_spool.send(ToSpool::Now(entry, done)).is_ok() {
            let _ = wait.recv();
        }
    };
    record_now(Entry::Shim(ShimEvent::Spawned { pid }));
    // Whoever started this shim may wait for this line: the turn is taken, the tool is up and
    // the socket answers. A shim that ends without saying it never had the turn, or said why
    // in the spool.
    {
        let mut out = std::io::stdout();
        let _ = out.write_all(b"ready\n").and_then(|()| out.flush());
    }

    let (to_stdin, stdin_requests) = mpsc::channel();
    if let Some(stdin) = child.stdin.take() {
        std::thread::spawn(move || stdin_thread(stdin, stdin_requests));
    }
    if let Some(message) = message {
        let _ = to_stdin.send(ToStdin::Data(message));
        let _ = to_stdin.send(ToStdin::Close);
    }
    let mut pumps = Vec::new();
    let mut printed = None;
    if let Some(stdout) = child.stdout.take() {
        // A second name for the pipe's reading end, to ask whether something waits in it.
        // Without one the output is recorded as before, and a hook where it arrives.
        printed = stdout
            .as_fd()
            .try_clone_to_owned()
            .ok()
            .map(|pipe| Arc::new(Printed::new(pipe)));
        let (to_spool, printed) = (to_spool.clone(), printed.clone());
        pumps.push(std::thread::spawn(move || {
            pump(stdout, &to_spool, Entry::Out, printed.as_deref());
        }));
    }
    let mut errors = None;
    if let Some(stderr) = child.stderr.take() {
        errors = stderr
            .as_fd()
            .try_clone_to_owned()
            .ok()
            .map(|pipe| Arc::new(Printed::new(pipe)));
        let (to_spool, errors) = (to_spool.clone(), errors.clone());
        pumps.push(std::thread::spawn(move || {
            pump(stderr, &to_spool, Entry::Err, errors.as_deref());
        }));
    }
    let control = Arc::new(Control {
        before_stop: args
            .before_stop
            .map(|(signal, wait)| BeforeStop { signal, wait }),
        pid,
        leader_exited: AtomicBool::new(false),
        stage: Mutex::default(),
        changed: Condvar::new(),
        to_spool: to_spool.clone(),
        to_stdin,
        printed,
        errors,
        served: args.program_args.first().is_some_and(|word| word == SERVED),
        hold: Mutex::default(),
        hold_changed: Condvar::new(),
        listener: listener
            .set_nonblocking(true)
            .and_then(|()| listener.as_fd().try_clone_to_owned())
            .ok(),
        connections: Mutex::default(),
    });
    if signals_blocked {
        let control = control.clone();
        std::thread::spawn(move || {
            while signals.wait().is_ok() {
                control.stop(GRACE_ON_SIGNAL_MS);
            }
        });
    }
    let listener_control = control.clone();
    std::thread::spawn(move || accept(&listener, &listener_control));

    let observed = observe_exit(pid)?;
    {
        let _stage = control.stage.lock().unwrap_or_else(PoisonError::into_inner);
        control.leader_exited.store(true, Ordering::SeqCst);
        control.changed.notify_all();
    }
    // A descendant can keep a pipe open after the leader has gone. Stop still
    // owns the group until we have drained both pipes and committed Exited.
    for pump in pumps {
        let _ = pump.join();
    }
    let mut stage = control.stage.lock().unwrap_or_else(PoisonError::into_inner);
    let (done, wait) = mpsc::channel();
    let entry = Entry::Shim(ShimEvent::Exited {
        code: observed.code(),
        signal: observed.signal(),
    });
    if to_spool.send(ToSpool::Last(entry, done)).is_ok() {
        let _ = wait.recv();
    }
    let _ = std::fs::remove_file(&args.socket);
    if matches!(*stage, StopStage::Ready) {
        *stage = StopStage::Finished;
    }
    drop(
        control
            .changed
            .wait_while(stage, |s| matches!(s, StopStage::Asked))
            .unwrap_or_else(PoisonError::into_inner),
    );
    // A tool killed from outside while it was held may leave a process of its group that holds
    // neither pipe. It is let go now, while the group's number is still the tool's: once the
    // shim is gone nothing would ever let it go.
    control.let_go();
    child
        .wait()
        .map_err(|e| format!("reaping {}: {e}", args.program))?;
    Ok(observed
        .code()
        .and_then(|c| u8::try_from(c).ok())
        .unwrap_or(1))
}

/// A hook is advisory. Invalid or oversized input produces no response.
fn background_hook(agent: Option<String>) {
    const MAX_INPUT: u64 = 64 * 1024;
    let Some(agent) = agent else {
        return;
    };
    let mut bytes = Vec::new();
    if std::io::stdin()
        .lock()
        .take(MAX_INPUT + 1)
        .read_to_end(&mut bytes)
        .is_err()
        || bytes.len() as u64 > MAX_INPUT
    {
        return;
    }
    let Ok(payload) = serde_json::from_slice(&bytes) else {
        return;
    };
    let managed = std::env::var_os("CLARP_PROVIDER_TURN").is_some_and(|value| !value.is_empty());
    if let Some(reply) = clarp_provider_hook::background_hook(&agent, managed, &payload) {
        let _ = serde_json::to_writer(std::io::stdout().lock(), &reply);
    }
}

fn main() -> ExitCode {
    let mut argv = std::env::args().skip(1).peekable();
    if argv
        .peek()
        .is_some_and(|word| word == "claude-background-hook")
    {
        argv.next();
        background_hook(argv.next());
        return ExitCode::SUCCESS;
    }
    if argv.peek().is_some_and(|word| word == "opencode-turn") {
        argv.next();
        return match opencode::run(&argv.next().unwrap_or_else(|| "opencode".into())) {
            Ok(code) => ExitCode::from(code),
            Err(error) => {
                eprintln!("{error}");
                ExitCode::FAILURE
            }
        };
    }
    match parse(argv).and_then(run) {
        Ok(code) => ExitCode::from(code),
        Err(error) => {
            eprintln!("clarp-shim: {error}");
            eprintln!(
                "usage: clarp-shim --spool FILE --socket PATH [--cwd DIR] [--stdin-file FILE] [--prompt-as-file] -- PROGRAM [ARGS...]"
            );
            ExitCode::from(2)
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    /// A `Control` as `run` makes it, for a stand-in tool: a `sleep` in a process group of its
    /// own, so that what is signalled reaches nothing else. The pipes are the test's: nothing
    /// pumps them.
    struct Rig {
        control: Arc<Control>,
        out: std::io::PipeWriter,
        out_read: std::io::PipeReader,
        err: std::io::PipeWriter,
        err_read: std::io::PipeReader,
        listener: UnixListener,
        socket: PathBuf,
        spool: PathBuf,
        sleeper: std::process::Child,
        dir: PathBuf,
    }

    fn rig(name: &str) -> Rig {
        let dir = std::env::temp_dir().join(format!("clarp-rig-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        let spool = dir.join("spool.jsonl");
        let writer = SpoolWriter::create(&spool).unwrap();
        let (to_spool, entries) = mpsc::channel();
        std::thread::spawn(move || spool_thread(writer, entries));
        let (out_read, out) = std::io::pipe().unwrap();
        let (err_read, err) = std::io::pipe().unwrap();
        let socket = dir.join("s");
        let listener = UnixListener::bind(&socket).unwrap();
        let sleeper = Command::new("sleep")
            .arg("30")
            .process_group(0)
            .spawn()
            .unwrap();
        let (to_stdin, _) = mpsc::channel();
        let control = Arc::new(Control {
            before_stop: None,
            pid: sleeper.id(),
            leader_exited: AtomicBool::new(false),
            stage: Mutex::default(),
            changed: Condvar::new(),
            to_spool,
            to_stdin,
            printed: Some(Arc::new(Printed::new(
                out_read.as_fd().try_clone_to_owned().unwrap(),
            ))),
            errors: Some(Arc::new(Printed::new(err_read.as_fd().try_clone_to_owned().unwrap()))),
            served: false,
            hold: Mutex::default(),
            hold_changed: Condvar::new(),
            listener: Some(listener.as_fd().try_clone_to_owned().unwrap()),
            connections: Mutex::default(),
        });
        Rig {
            control,
            out,
            out_read,
            err,
            err_read,
            listener,
            socket,
            spool,
            sleeper,
            dir,
        }
    }

    impl Rig {
        fn sleeper(&self) -> i32 {
            i32::try_from(self.sleeper.id()).unwrap()
        }

        fn state(&self) -> Option<char> {
            state_and_group(Path::new(&format!("/proc/{}/stat", self.sleeper()))).map(|s| s.0)
        }

        fn mark(&self, hold: Hold) {
            *lock(&self.control.hold) = hold;
        }

        fn end(mut self) {
            let _ = self.sleeper.kill();
            let _ = self.sleeper.wait();
            std::fs::remove_dir_all(&self.dir).unwrap();
        }
    }

    fn connection(stream: &UnixStream) -> Arc<Connection> {
        Arc::new(Connection {
            socket: stream.as_fd().try_clone_to_owned().unwrap(),
            turn: Mutex::new(()),
            pending: AtomicBool::new(false),
        })
    }

    /// What `count` says when it is given fifty milliseconds. It must say it within two seconds.
    fn count(rig: &Rig, own: &Arc<Connection>) -> Option<Held> {
        let (control, own) = (rig.control.clone(), own.clone());
        let (said, answer) = mpsc::channel();
        std::thread::spawn(move || {
            let until = Instant::now() + Duration::from_millis(50);
            let _ = said.send(control.count(&own, until));
        });
        answer
            .recv_timeout(Duration::from_secs(2))
            .expect("count ends at its bound")
    }

    #[test]
    fn a_count_waits_for_everything_in_its_way_and_then_says_how_far_the_spool_goes() {
        let mut rig = rig("count");
        let (own, _own_peer) = UnixStream::pair().unwrap();
        let own = connection(&own);
        let record = Entry::Out("printed".into());
        rig.control.to_spool.send(ToSpool::Entry(record)).unwrap();
        let length = |rig: &Rig| std::fs::metadata(&rig.spool).unwrap().len();
        let held = count(&rig, &own).expect("nothing is in the way");
        assert_eq!(held.spool, length(&rig), "the record sent before is counted");
        assert!(held.spool > 0);
        assert_eq!((held.partial_out, held.partial_err, held.left_ms), (0, 0, 0));

        // What waits in either pipe is in the way, until it is taken.
        rig.out.write_all(b"x").unwrap();
        assert_eq!(count(&rig, &own), None, "a byte in the pipe of standard output");
        rig.out_read.read_exact(&mut [0]).unwrap();
        rig.err.write_all(b"x").unwrap();
        assert_eq!(count(&rig, &own), None, "a byte in the pipe of standard error");
        rig.err_read.read_exact(&mut [0]).unwrap();
        assert!(count(&rig, &own).is_some());

        // A connection that waits to be taken is in the way; taken, it is a connection.
        let client = UnixStream::connect(&rig.socket).unwrap();
        assert_eq!(count(&rig, &own), None, "a connection waits in the listener");
        let (taken, _) = rig.listener.accept().unwrap();
        let other = connection(&taken);
        lock(&rig.control.connections).push(other.clone());
        assert!(count(&rig, &own).is_some(), "a connection with nothing in it");
        // What waits in a connection is in the way; so is a request read and not carried out.
        (&client).write_all(b"{\"op\":\"ping\"}\n").unwrap();
        assert_eq!(count(&rig, &own), None, "a request in a connection");
        let mut request = [0; 14];
        (&taken).read_exact(&mut request).unwrap();
        assert!(count(&rig, &own).is_some());
        other.pending.store(true, Ordering::SeqCst);
        assert_eq!(count(&rig, &own), None, "a request read and not carried out");
        other.pending.store(false, Ordering::SeqCst);
        // But not the connection that asks for the count.
        let own_other = other.clone();
        (&client).write_all(b"{\"op\":\"ping\"}\n").unwrap();
        assert!(count(&rig, &own_other).is_some(), "its own request is not in its way");
        assert_eq!(count(&rig, &own), None);
        (&taken).read_exact(&mut request).unwrap();

        // A connection's turn is waited for: a request is being carried out under it.
        let turn = lock(&other.turn);
        let (control, own_too) = (rig.control.clone(), own.clone());
        let counting = std::thread::spawn(move || {
            control.count(&own_too, Instant::now() + Duration::from_secs(5))
        });
        std::thread::sleep(Duration::from_millis(50));
        assert!(!counting.is_finished(), "the count waits for the turn");
        rig.control
            .to_spool
            .send(ToSpool::Entry(Entry::Out("carried out".into())))
            .unwrap();
        drop(turn);
        let held = counting.join().unwrap().unwrap();
        assert_eq!(held.spool, length(&rig), "and counts what was carried out under it");

        // What the pumps hold of a line that is not whole is said.
        rig.control
            .printed
            .as_ref()
            .unwrap()
            .partial
            .store(3, Ordering::SeqCst);
        rig.control
            .errors
            .as_ref()
            .unwrap()
            .partial
            .store(2, Ordering::SeqCst);
        let held = count(&rig, &own).unwrap();
        assert_eq!((held.partial_out, held.partial_err), (3, 2));
        drop(client);
        rig.end();
    }

    #[test]
    fn a_count_is_nothing_once_its_bound_is_past() {
        let rig = rig("count-late");
        let (own, _peer) = UnixStream::pair().unwrap();
        let own = connection(&own);
        assert_eq!(rig.control.count(&own, Instant::now()), None);
        assert!(rig.control.count(&own, Instant::now() + Duration::from_secs(1)).is_some());
        rig.end();
    }

    #[test]
    fn a_group_that_does_not_stand_still_is_let_go_at_the_bound() {
        let rig = rig("bound");
        let until = Instant::now() + Duration::from_millis(80);
        rig.mark(Hold::Held { until });
        let began = Instant::now();
        let (control, said) = (rig.control.clone(), mpsc::channel());
        let answer = said.0;
        std::thread::spawn(move || {
            let _ = answer.send(control.stand_still(until, || false));
        });
        let result = said.1.recv_timeout(Duration::from_secs(2)).unwrap();
        assert_eq!(result, Err("the tool did not stand still within the bound"));
        assert!(began.elapsed() >= Duration::from_millis(75));
        assert!(matches!(*lock(&rig.control.hold), Hold::Released));
        std::thread::sleep(Duration::from_millis(50));
        assert_ne!(rig.state(), Some('T'), "it was let go");

        // One that stands still is held at once and stays so.
        let rig2 = self::rig("still");
        rig2.mark(Hold::Held {
            until: Instant::now() + Duration::from_secs(2),
        });
        let looked = AtomicUsize::new(0);
        let until = Instant::now() + Duration::from_secs(2);
        let still = || looked.fetch_add(1, Ordering::SeqCst) >= 2;
        assert_eq!(rig2.control.stand_still(until, still), Ok(()));
        assert_eq!(looked.load(Ordering::SeqCst), 3, "looked at until it stood still");
        std::thread::sleep(Duration::from_millis(50));
        assert_eq!(rig2.state(), Some('T'));
        assert!(rig2.control.let_go());
        // Ended while it was looked at: not let go, and said.
        rig2.mark(Hold::Ended);
        assert_eq!(rig2.control.stand_still(until, || false), Err(ENDED_WHILE_HELD));
        assert!(matches!(*lock(&rig2.control.hold), Hold::Ended));
        rig.end();
        rig2.end();
    }

    #[test]
    fn a_held_tool_is_ended_at_once_and_a_hold_under_way_says_so() {
        let mut rig = rig("ended");
        let (own, _peer) = UnixStream::pair().unwrap();
        let own = connection(&own);
        // Something waits in the pipe: the hold cannot answer before it is taken.
        rig.out.write_all(b"in the way").unwrap();
        let (control, own_too) = (rig.control.clone(), own.clone());
        let holding = std::thread::spawn(move || control.hold(2_000, &own_too));
        let sleeper = rig.sleeper();
        let stopped = || {
            state_and_group(Path::new(&format!("/proc/{sleeper}/stat"))).map(|s| s.0)
                == Some('T')
        };
        let deadline = Instant::now() + Duration::from_secs(2);
        while !stopped() {
            assert!(Instant::now() < deadline, "the hold stops the tool");
            std::thread::sleep(Duration::from_millis(5));
        }
        let began = Instant::now();
        rig.control.stop(5_000);
        assert!(began.elapsed() < Duration::from_secs(1), "no grace");
        assert!(matches!(*lock(&rig.control.hold), Hold::Ended));
        assert!(matches!(*lock(&rig.control.stage), StopStage::Signalled));
        let status = rig.sleeper.wait().unwrap();
        assert_eq!(status.signal(), Some(9), "killed where it stood");
        rig.out_read.read_exact(&mut [0; 10]).unwrap();
        let reply = holding.join().unwrap();
        assert_eq!(reply.error.as_deref(), Some(ENDED_WHILE_HELD));
        assert_eq!(reply.held, None);
        assert!(!rig.control.let_go(), "an ended tool is not let go");
        let said: Vec<Entry> = clarp_spool::SpoolReader::new(&rig.spool)
            .read_from(0)
            .unwrap()
            .records
            .into_iter()
            .map(|r| r.entry)
            .collect();
        assert_eq!(
            said,
            [Entry::Shim(ShimEvent::Stopping {
                signal: "SIGKILL".into()
            })]
        );
        rig.end();
    }

    #[test]
    fn a_stat_is_read_after_the_last_bracket_of_the_name() {
        let dir = std::env::temp_dir().join(format!("clarp-stat-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let stat = dir.join("stat");
        for (line, want) in [
            ("12 (sh) S 1 777 777 0", Some(('S', 777))),
            ("12 (a) T 1 2 b) T 9 4242 4242 0", Some(('T', 4242))),
            ("12 (x y) Z 1 -1", Some(('Z', -1))),
            ("12 (sh) S 1", None),
            ("12 sh S 1 777", None),
            ("", None),
        ] {
            std::fs::write(&stat, line).unwrap();
            assert_eq!(state_and_group(&stat), want, "{line}");
        }
        assert_eq!(state_and_group(&dir.join("absent")), None);
        std::fs::remove_dir_all(dir).unwrap();
    }

    #[test]
    fn a_group_with_one_process_awake_does_not_stand_still() {
        // This test's own process group: this thread runs.
        let group = nix::unistd::getpgrp().as_raw();
        assert!(!stands_still(group));
        // A group with no process left stands still.
        assert!(stands_still(i32::MAX));
    }

    #[test]
    fn the_last_spool_record_discards_a_kill_already_queued_behind_it() {
        let file = std::env::temp_dir().join(format!("clarp-closed-spool-{}", std::process::id()));
        let writer = SpoolWriter::create(&file).unwrap();
        let (send, receive) = mpsc::channel();
        let (done, ack) = mpsc::channel();
        let term = Entry::Shim(ShimEvent::Stopping {
            signal: "SIGTERM".into(),
        });
        let end = Entry::Shim(ShimEvent::Exited {
            code: None,
            signal: Some(15),
        });
        send.send(ToSpool::Entry(term.clone())).unwrap();
        send.send(ToSpool::Last(end.clone(), done)).unwrap();
        send.send(ToSpool::Entry(Entry::Shim(ShimEvent::Stopping {
            signal: "SIGKILL".into(),
        })))
        .unwrap();
        drop(send);
        spool_thread(writer, receive);
        ack.recv().unwrap();
        let entries = clarp_spool::SpoolReader::new(&file)
            .read_from(0)
            .unwrap()
            .records
            .into_iter()
            .map(|r| r.entry)
            .collect::<Vec<_>>();
        assert_eq!(entries, [term, end]);
        std::fs::remove_file(file).unwrap();
    }

    fn printed() -> (Arc<Printed>, std::io::PipeReader, std::io::PipeWriter) {
        let (reader, writer) = std::io::pipe().unwrap();
        let pipe = reader.as_fd().try_clone_to_owned().unwrap();
        let printed = Arc::new(Printed::new(pipe));
        (printed, reader, writer)
    }

    #[test]
    fn a_hooks_report_waits_for_what_was_printed_before_it() {
        let (printed, mut reader, mut writer) = printed();
        assert!(!printed.waits(false));
        // Nothing in the pipe: recorded at once.
        assert_eq!(printed.after_what_was_printed(|| 1), 1);

        writer.write_all(b"printed before the hook\n").unwrap();
        assert!(printed.waits(false));
        let recorded = Arc::new(AtomicBool::new(false));
        let hook = {
            let (printed, recorded) = (printed.clone(), recorded.clone());
            std::thread::spawn(move || {
                printed.after_what_was_printed(|| recorded.store(true, Ordering::SeqCst));
            })
        };
        std::thread::sleep(Duration::from_millis(40));
        assert!(
            !recorded.load(Ordering::SeqCst),
            "the line is still in the pipe"
        );
        // The pump takes the line, holding the turn while it does.
        {
            let _turn = printed.turn.lock().unwrap();
            let mut line = [0; 64];
            assert_eq!(reader.read(&mut line).unwrap(), 24);
            std::thread::sleep(Duration::from_millis(20));
            assert!(
                !recorded.load(Ordering::SeqCst),
                "the pump has the line in hand"
            );
        }
        hook.join().unwrap();
        assert!(recorded.load(Ordering::SeqCst));
    }

    #[test]
    fn a_tool_that_never_stops_printing_is_not_waited_for() {
        let (printed, _reader, mut writer) = printed();
        writer.write_all(b"never taken\n").unwrap();
        let began = Instant::now();
        assert_eq!(printed.after_what_was_printed(|| 7), 7);
        assert!(began.elapsed() >= HOOK_WAITS);
        assert!(began.elapsed() < HOOK_WAITS * 8);
    }

    #[test]
    fn output_is_recorded_a_line_at_a_time_and_what_is_left_at_the_end() {
        let (to_spool, entries) = mpsc::channel();
        let printed = b"one\ntwo\r\nthree".to_vec();
        pump(printed.as_slice(), &to_spool, Entry::Out, None);
        drop(to_spool);
        let lines: Vec<Entry> = entries
            .into_iter()
            .filter_map(|message| match message {
                ToSpool::Entry(entry) => Some(entry),
                _ => None,
            })
            .collect();
        assert_eq!(
            lines,
            [
                Entry::Out("one".to_string()),
                Entry::Out("two".to_string()),
                Entry::Out("three".to_string())
            ]
        );
    }
}
