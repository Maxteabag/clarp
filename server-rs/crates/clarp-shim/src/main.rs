//! `clarp-shim`: owns one tool process for as long as it lives.
//!
//!     clarp-shim --spool FILE --socket PATH [--cwd DIR] -- PROGRAM [ARGS...]
//!
//! It starts PROGRAM in its own process group, appends every line it prints to the spool, listens
//! on the socket for the server's few requests, and when the program is gone writes how it went
//! and exits with the same code. It knows nothing about any tool: the lines are opaque to it.
//! It never exits before the program does, and it does not care whether a server is there.

use std::io::{BufRead, BufReader, Read, Write};
use std::os::unix::net::{UnixListener, UnixStream};
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::PathBuf;
use std::process::{ChildStdin, Command, ExitCode, Stdio};
use std::sync::Arc;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc::{self, Sender};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

use clarp_shim::{Reply, Request, SOCKET_ENV};
use clarp_spool::{Entry, MAX_OUTPUT_CHUNK, Record, ShimEvent, SpoolWriter};
use nix::sys::signal::{Signal, killpg};
use nix::unistd::Pid;

struct Args {
    spool: PathBuf,
    socket: PathBuf,
    cwd: Option<PathBuf>,
    program: String,
    program_args: Vec<String>,
}

fn parse(mut argv: impl Iterator<Item = String>) -> Result<Args, String> {
    let (mut spool, mut socket, mut cwd) = (None, None, None);
    loop {
        match argv.next().as_deref() {
            Some("--spool") => spool = argv.next().map(PathBuf::from),
            Some("--socket") => socket = argv.next().map(PathBuf::from),
            Some("--cwd") => cwd = argv.next().map(PathBuf::from),
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
    Final(Entry, Sender<()>),
}

/// What the socket's threads send to the one thread that owns the tool's standard input.
enum ToStdin {
    Data(String),
    Close,
}

fn spool_thread(mut writer: SpoolWriter, entries: mpsc::Receiver<ToSpool>) {
    for message in entries {
        match message {
            ToSpool::Entry(entry) => {
                let durable = matches!(entry, Entry::Shim(_));
                // A spool that cannot be written is a turn nobody will ever read; there is no one
                // to tell but stderr, and the tool goes on regardless.
                if let Err(error) = writer.append(&Record { t: now_ms(), entry }) {
                    eprintln!("clarp-shim: cannot write the spool: {error}");
                } else if durable {
                    let _ = writer.sync();
                }
            }
            ToSpool::Final(entry, done) => {
                if let Err(error) = writer
                    .append(&Record { t: now_ms(), entry })
                    .and_then(|()| writer.sync())
                {
                    eprintln!("clarp-shim: cannot write the spool: {error}");
                }
                let _ = done.send(());
            }
        }
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
        if to_spool
            .send(ToSpool::Entry(wrap(
                String::from_utf8_lossy(&line).into_owned(),
            )))
            .is_err()
        {
            break;
        }
    }
}

fn stdin_thread(mut stdin: ChildStdin, requests: mpsc::Receiver<ToStdin>) {
    for request in requests {
        match request {
            ToStdin::Data(data) => {
                if stdin
                    .write_all(data.as_bytes())
                    .and_then(|()| stdin.flush())
                    .is_err()
                {
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
            Request::Ping => Reply::ok(Some(self.pid)),
            Request::Stdin { data } => match self.to_stdin.send(ToStdin::Data(data)) {
                Ok(()) => Reply::ok(Some(self.pid)),
                Err(_) => Reply::failed("the tool's input is closed"),
            },
            Request::CloseStdin => {
                let _ = self.to_stdin.send(ToStdin::Close);
                Reply::ok(Some(self.pid))
            }
            Request::Hook { name, data } => match self
                .to_spool
                .send(ToSpool::Entry(Entry::Hook { name, data }))
            {
                Ok(()) => Reply::ok(Some(self.pid)),
                Err(_) => Reply::failed("the spool is closed"),
            },
            Request::Stop { grace_ms } => {
                self.stop(grace_ms);
                Reply::ok(Some(self.pid))
            }
        }
    }

    fn signal(&self, signal: Signal) {
        if self.exited.load(Ordering::SeqCst) {
            return;
        }
        let _ = self
            .to_spool
            .send(ToSpool::Entry(Entry::Shim(ShimEvent::Stopping {
                signal: signal.as_str().to_string(),
            })));
        // The tool is the leader of its own process group, so this reaches what it started too.
        let _ = killpg(
            Pid::from_raw(i32::try_from(self.pid).unwrap_or(i32::MAX)),
            signal,
        );
    }

    fn stop(&self, grace_ms: u64) {
        self.signal(Signal::SIGTERM);
        let (pid, exited, to_spool) = (self.pid, self.exited.clone(), self.to_spool.clone());
        std::thread::spawn(move || {
            std::thread::sleep(Duration::from_millis(grace_ms));
            if !exited.load(Ordering::SeqCst) {
                let _ = to_spool.send(ToSpool::Entry(Entry::Shim(ShimEvent::Stopping {
                    signal: Signal::SIGKILL.as_str().to_string(),
                })));
                let _ = killpg(
                    Pid::from_raw(i32::try_from(pid).unwrap_or(i32::MAX)),
                    Signal::SIGKILL,
                );
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
        let reply = match serde_json::from_str::<Request>(&line) {
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
    }
}

fn run(args: Args) -> Result<u8, String> {
    let writer =
        SpoolWriter::open(&args.spool).map_err(|e| format!("{}: {e}", args.spool.display()))?;
    let (to_spool, spool_entries) = mpsc::channel();
    let spool_writer = std::thread::spawn(move || spool_thread(writer, spool_entries));
    let finish = |entry: Entry| {
        let (done, wait) = mpsc::channel();
        if to_spool.send(ToSpool::Final(entry, done)).is_ok() {
            let _ = wait.recv();
        }
    };

    let _ = std::fs::remove_file(&args.socket);
    if let Some(dir) = args.socket.parent() {
        std::fs::create_dir_all(dir).map_err(|e| format!("{}: {e}", dir.display()))?;
    }
    let listener =
        UnixListener::bind(&args.socket).map_err(|e| format!("{}: {e}", args.socket.display()))?;

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
        Err(error) => {
            finish(Entry::Shim(ShimEvent::SpawnFailed {
                error: format!("{}: {error}", args.program),
            }));
            let _ = std::fs::remove_file(&args.socket);
            return Ok(127);
        }
    };
    let pid = child.id();
    let _ = to_spool.send(ToSpool::Entry(Entry::Shim(ShimEvent::Spawned { pid })));

    let exited = Arc::new(AtomicBool::new(false));
    let (to_stdin, stdin_requests) = mpsc::channel();
    if let Some(stdin) = child.stdin.take() {
        std::thread::spawn(move || stdin_thread(stdin, stdin_requests));
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
    finish(Entry::Shim(ShimEvent::Exited {
        code: status.code(),
        signal: status.signal(),
    }));
    let _ = std::fs::remove_file(&args.socket);
    drop(to_spool);
    let _ = spool_writer;
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
                "usage: clarp-shim --spool FILE --socket PATH [--cwd DIR] -- PROGRAM [ARGS...]"
            );
            ExitCode::from(2)
        }
    }
}
