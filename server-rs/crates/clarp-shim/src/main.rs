//! `clarp-shim`: owns one tool process for as long as it lives.
//!
//!     clarp-shim --spool FILE --socket PATH [--cwd DIR] [--stdin-file FILE] -- PROGRAM [ARGS...]
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

use std::io::{BufRead, BufReader, ErrorKind, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{ChildStdin, Command, ExitCode, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Sender};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use clarp_shim::{EXIT_NOT_STARTED, EXIT_TURN_TAKEN, Reply, Request, SOCKET_ENV};
use clarp_spool::{Entry, MAX_OUTPUT_CHUNK, Record, ShimEvent, SpoolWriter};
use nix::sys::signal::{SigSet, Signal, killpg};
use nix::unistd::{Pid, setsid};

/// How long a tool gets to end by itself when the shim is told to go.
const GRACE_ON_SIGNAL_MS: u64 = 5_000;

struct Args {
    spool: PathBuf,
    socket: PathBuf,
    cwd: Option<PathBuf>,
    stdin_file: Option<PathBuf>,
    program: String,
    program_args: Vec<String>,
}

fn parse(mut argv: impl Iterator<Item = String>) -> Result<Args, String> {
    let (mut spool, mut socket, mut cwd, mut stdin_file) = (None, None, None, None);
    loop {
        match argv.next().as_deref() {
            Some("--spool") => spool = argv.next().map(PathBuf::from),
            Some("--socket") => socket = argv.next().map(PathBuf::from),
            Some("--cwd") => cwd = argv.next().map(PathBuf::from),
            Some("--stdin-file") => stdin_file = argv.next().map(PathBuf::from),
            Some("--") => break,
            Some(other) => return Err(format!("unknown argument {other}")),
            None => return Err("missing -- PROGRAM".to_string()),
        }
    }
    let program = argv.next().ok_or("missing PROGRAM after --")?;
    Ok(Args {
        spool: spool.ok_or("missing --spool FILE")?,
        socket: socket.ok_or("missing --socket PATH")?,
        cwd,
        stdin_file,
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
    /// A connection that wants a newline whenever the spool has grown.
    Follow(UnixStream),
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
        match message {
            ToSpool::Entry(entry) => {
                let durable = matches!(entry, Entry::Shim(_));
                write(&mut writer, entry, durable);
            }
            ToSpool::Now(entry, done) => {
                write(&mut writer, entry, true);
                let _ = done.send(());
            }
            ToSpool::Follow(follower) => followers.push(follower),
        }
        // Whoever follows is told there is more to read. A follower that is not reading already
        // has a hint waiting and is not waited for; one that is gone is forgotten.
        followers.retain_mut(|follower| match follower.write(b"\n") {
            Ok(_) => true,
            Err(error) => error.kind() == ErrorKind::WouldBlock,
        });
    }
}

/// Copies a stream of the tool into the spool, a record per line. A line longer than a record may
/// be is split, so one runaway line cannot hold the shim's memory or the reader's.
fn pump(stream: impl Read, to_spool: Sender<ToSpool>, wrap: fn(String) -> Entry) {
    let mut reader = BufReader::new(stream);
    let mut line = Vec::new();
    loop {
        line.clear();
        let read = match reader
            .by_ref()
            .take(MAX_OUTPUT_CHUNK as u64)
            .read_until(b'\n', &mut line)
        {
            Ok(read) => read,
            Err(_) => break,
        };
        if read == 0 {
            break;
        }
        if line.last() == Some(&b'\n') {
            line.pop();
            if line.last() == Some(&b'\r') {
                line.pop();
            }
        }
        let text = String::from_utf8_lossy(&line).into_owned();
        if to_spool.send(ToSpool::Entry(wrap(text))).is_err() {
            break;
        }
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

struct Control {
    pid: u32,
    exited: Arc<AtomicBool>,
    to_spool: Sender<ToSpool>,
    to_stdin: Sender<ToStdin>,
}

impl Control {
    fn handle(&self, request: Request) -> Reply {
        match request {
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
                match self
                    .to_spool
                    .send(ToSpool::Entry(Entry::Hook { name, data }))
                {
                    Ok(()) => Reply::ok(Some(self.pid)),
                    Err(_) => Reply::failed("the spool is closed"),
                }
            }
            Request::Stop { grace_ms } => {
                self.stop(grace_ms);
                Reply::ok(Some(self.pid))
            }
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
        if self.exited.load(Ordering::SeqCst) {
            return;
        }
        Self::signal(self.pid, &self.to_spool, Signal::SIGTERM);
        let (pid, exited, to_spool) = (self.pid, self.exited.clone(), self.to_spool.clone());
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(grace_ms));
            if !exited.load(Ordering::SeqCst) {
                Self::signal(pid, &to_spool, Signal::SIGKILL);
            }
        });
    }
}

fn serve(connection: UnixStream, control: &Control) {
    let Ok(read_half) = connection.try_clone() else {
        return;
    };
    let mut write_half = connection;
    for line in BufReader::new(read_half).lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() {
            continue;
        }
        let request = serde_json::from_str::<Request>(&line);
        let follow = matches!(request, Ok(Request::Follow));
        let reply = match request {
            Ok(request) => control.handle(request),
            Err(error) => Reply::failed(format!("not a request: {error}")),
        };
        let Ok(mut text) = serde_json::to_string(&reply) else {
            break;
        };
        text.push('\n');
        if write_half.write_all(text.as_bytes()).is_err() {
            break;
        }
        if follow {
            // From here this connection only carries hints, written by the thread that owns the
            // spool. They must never make that thread wait.
            if write_half.set_nonblocking(true).is_ok() {
                let _ = control.to_spool.send(ToSpool::Follow(write_half));
            }
            return;
        }
    }
}

fn listen(socket: &Path) -> std::io::Result<UnixListener> {
    let _ = std::fs::remove_file(socket);
    if let Some(dir) = socket.parent() {
        std::fs::create_dir_all(dir)?;
    }
    UnixListener::bind(socket)
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
    let mut not_started = |error: String| -> Result<u8, String> {
        write(
            &mut writer,
            Entry::Shim(ShimEvent::SpawnFailed { error }),
            true,
        );
        let _ = std::fs::remove_file(&args.socket);
        Ok(EXIT_NOT_STARTED)
    };
    let message = match args.stdin_file.as_ref().map(std::fs::read).transpose() {
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
        .stdin(Stdio::piped())
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

    let exited = Arc::new(AtomicBool::new(false));
    let (to_stdin, stdin_requests) = mpsc::channel();
    if let Some(stdin) = child.stdin.take() {
        std::thread::spawn(move || stdin_thread(stdin, stdin_requests));
    }
    if let Some(message) = message {
        let _ = to_stdin.send(ToStdin::Data(message));
        let _ = to_stdin.send(ToStdin::Close);
    }
    let mut pumps = Vec::new();
    if let Some(stdout) = child.stdout.take() {
        let to_spool = to_spool.clone();
        pumps.push(std::thread::spawn(move || {
            pump(stdout, to_spool, Entry::Out)
        }));
    }
    if let Some(stderr) = child.stderr.take() {
        let to_spool = to_spool.clone();
        pumps.push(std::thread::spawn(move || {
            pump(stderr, to_spool, Entry::Err)
        }));
    }
    let control = Arc::new(Control {
        pid,
        exited: exited.clone(),
        to_spool: to_spool.clone(),
        to_stdin,
    });
    if signals_blocked {
        let control = control.clone();
        std::thread::spawn(move || {
            while signals.wait().is_ok() {
                control.stop(GRACE_ON_SIGNAL_MS);
            }
        });
    }
    std::thread::spawn(move || {
        for connection in listener.incoming().flatten() {
            let control = control.clone();
            std::thread::spawn(move || serve(connection, &control));
        }
    });

    let status = child
        .wait()
        .map_err(|e| format!("waiting for {}: {e}", args.program))?;
    exited.store(true, Ordering::SeqCst);
    // Everything the tool printed before it went is in the spool before the record that says it went.
    for pump in pumps {
        let _ = pump.join();
    }
    record_now(Entry::Shim(ShimEvent::Exited {
        code: status.code(),
        signal: status.signal(),
    }));
    let _ = std::fs::remove_file(&args.socket);
    Ok(status
        .code()
        .and_then(|code| u8::try_from(code).ok())
        .unwrap_or(1))
}

fn main() -> ExitCode {
    match parse(std::env::args().skip(1)).and_then(run) {
        Ok(code) => ExitCode::from(code),
        Err(error) => {
            eprintln!("clarp-shim: {error}");
            eprintln!(
                "usage: clarp-shim --spool FILE --socket PATH [--cwd DIR] [--stdin-file FILE] -- PROGRAM [ARGS...]"
            );
            ExitCode::from(2)
        }
    }
}
