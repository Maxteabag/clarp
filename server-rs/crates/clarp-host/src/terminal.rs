//! A terminal: a program of this machine on a pseudo-terminal of its own, its keys and its
//! screen carried over a socket.
//!
//! ```text
//!   what the client says in bytes               -> the program's keys
//!   what the program prints                     -> the client, in bytes
//!   a text {"resize":{"cols":C,"rows":R}}       -> the size of the terminal
//! ```
//!
//! The Python Host's `terminal_ws.py`. The program leads a session of its own with the
//! terminal as its own, which a shell and the tools of agents need to be spoken to. When
//! the client is gone, or the program is, the program's group is asked to end, killed if
//! it does not within a second, and the socket is closed as one that ended well.
//!
//! A Host has one terminal of its own (`GET /server-terminal`): a login shell in the home
//! of whoever the Host runs as, for its administrator and for a device of full scope.
//! Nothing a Host starts is given the Host's own secrets, and neither is that shell.
//!
//! And each agent has one (`GET /terminal/<session>`): the agent's own tool, spoken to by
//! hand, taken up on the conversation the agent has with it and started as the agent's
//! turns are (where the agent works, told the agent's name and where its tools find their
//! Host). Two processes on one conversation would spoil it, so while an agent's terminal
//! is open no turn of that agent is started: its launch waits for the terminal to be
//! closed (`agent.rs`). Other agents go on as they do.

use std::path::{Path, PathBuf};
use std::time::Duration;

use axum::Router;
use axum::extract::{Path as Named, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::get;
use clarp_kernel::python;
use nix::sys::signal::{Signal, kill, killpg};
use nix::unistd::Pid;
use serde_json::Value;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

use crate::agent::AgentHandle;
use crate::edge::{ApiError, App};
use crate::line::{self, ENDED_WELL, Heard, Line, Socket, Told};

/// How much of what a program printed goes to the client in one message.
const PRINTED_MOST: usize = 64 * 1024;
/// The size a terminal has until its client says another: its columns and its rows.
const SIZE: (u16, u16) = (80, 24);
/// How long a program is given to end when it is asked to, before it is killed.
const ENDS_WITHIN: Duration = Duration::from_secs(1);
/// What a request that asks for no socket is told, by the routes of terminals.
const NO_SOCKET: &str = "upgrade required (websocket)";
/// The shell of a machine that names none.
const USUAL_SHELL: &str = "/bin/sh";

/// What a janitor's terminal is refused with: the words of every control of a chat that
/// names one.
const LOOKED_AT_ONLY: &str = "Janitors are inspection-only. Use their Pause or Configure controls.";

/// What runs in a terminal, and where.
struct Program {
    /// What the terminal is called in the Host's journal.
    called: String,
    argv: Vec<String>,
    cwd: PathBuf,
    /// What it is told on top of the Host's environment.
    env: Vec<(String, String)>,
    /// The agent whose tool it is, when it is one's: no turn of it is started meanwhile.
    agent: Option<AgentHandle>,
}

/// An agent's terminal that is open: its agent is told so, and told again when this goes,
/// however it goes.
struct Open(AgentHandle);

impl Open {
    fn new(agent: AgentHandle) -> Self {
        agent.terminal(true);
        Self(agent)
    }
}

impl Drop for Open {
    fn drop(&mut self) {
        self.0.terminal(false);
    }
}

/// A number as Python's `int()` reads a value a client sent: a whole number, the whole
/// part of one that is not, a text of digits. `None`: Python reads no number in it.
fn whole(value: &Value) -> Option<i128> {
    match value {
        Value::Bool(yes) => Some(i128::from(*yes)),
        Value::Number(number) => match (number.as_i64(), number.as_u64()) {
            (Some(whole), _) => Some(i128::from(whole)),
            (None, Some(whole)) => Some(i128::from(whole)),
            // A number with a part after its point: what is before the point.
            (None, None) => number.as_f64().map(|number| number.trunc() as i128),
        },
        Value::String(text) => python::int(text),
        _ => None,
    }
}

/// The size a client asks for in a text it sent, `{"resize": {"cols": C, "rows": R}}`: its
/// columns and its rows. A side it does not name, or names as nothing, is the side a
/// terminal starts with, and a side below one is one. `None`: the text asks for no size,
/// or for one that cannot be read or that no terminal has.
fn resized(text: &str) -> Option<(u16, u16)> {
    let said: Value = serde_json::from_str(text).ok()?;
    let asked = said.get("resize")?.as_object()?;
    let side = |name: &str, usual: u16| -> Option<u16> {
        let wanted = match asked.get(name) {
            Some(named) if python::truthy(named) => whole(named)?,
            _ => i128::from(usual),
        };
        u16::try_from(wanted.max(1)).ok()
    };
    Some((side("cols", SIZE.0)?, side("rows", SIZE.1)?))
}

/// Whether a program can be run, as Python's `shutil.which` finds one: a path that is a
/// file somebody may run, or a name of such a file on the path.
fn runnable(program: &str) -> bool {
    use std::os::unix::fs::PermissionsExt;
    let runs = |file: &Path| {
        let found = std::fs::metadata(file);
        found.is_ok_and(|found| found.is_file() && found.permissions().mode() & 0o111 != 0)
    };
    if program.contains('/') {
        return runs(Path::new(program));
    }
    let path = std::env::var_os("PATH").unwrap_or_default();
    std::env::split_paths(&path).any(|dir| runs(&dir.join(program)))
}

/// Starts the program on a terminal of its own. `None`: it could not be started.
fn started(program: &Program) -> Option<(pty_process::Pty, tokio::process::Child)> {
    let (terminal, programs_end) = pty_process::open().ok()?;
    terminal
        .resize(pty_process::Size::new(SIZE.1, SIZE.0))
        .ok()?;
    let (name, said) = program.argv.split_first()?;
    let mut command = pty_process::Command::new(name)
        .args(said)
        .current_dir(&program.cwd)
        .env("TERM", "xterm-256color")
        .kill_on_drop(true);
    // A machine that names no language is one where a terminal speaks UTF-8.
    if std::env::var_os("LANG").is_none() {
        command = command.env("LANG", "en_US.UTF-8");
    }
    for secret in crate::OWN_SECRETS {
        command = command.env_remove(secret);
    }
    for (name, value) in &program.env {
        command = command.env(name, value);
    }
    let child = command.spawn(programs_end).ok()?;
    Some((terminal, child))
}

/// Ends a program and what it started in its group: asked to, and killed when it has not
/// ended in time. A shell that is spoken to does not end for the asking.
async fn ended(child: &mut tokio::process::Child) {
    // No number: it has ended, and was seen to.
    let Some(pid) = child.id().and_then(|pid| i32::try_from(pid).ok()) else {
        return;
    };
    // The program leads its session, so its group has its own number.
    let group = Pid::from_raw(pid);
    if killpg(group, Signal::SIGTERM).is_err() {
        let _ = kill(group, Signal::SIGTERM);
    }
    if tokio::time::timeout(ENDS_WITHIN, child.wait())
        .await
        .is_err()
    {
        let _ = killpg(group, Signal::SIGKILL);
        let _ = child.wait().await;
    }
}

/// Serves one terminal on a socket, from the program's start to its end.
async fn serve(mut line: Line, program: Program) {
    let called = &program.called;
    let Some((terminal, mut child)) = started(&program) else {
        eprintln!("clarp-host: the terminal {called} could not be started");
        line.tell(Told::Close(ENDED_WELL, String::new())).await;
        return;
    };
    // From here to the end of this, no turn of the agent whose tool it is is started.
    let _open = program.agent.clone().map(Open::new);
    let pid = child.id().and_then(|pid| i32::try_from(pid).ok());
    eprintln!(
        "clarp-host: the terminal {called} started ({}) terminalStart",
        program.argv.first().map_or("", String::as_str)
    );
    let (mut screen, mut keys) = terminal.into_split();

    // What the program prints goes to the client as it comes, until the program is gone:
    // the terminal ends, or says, as Linux does, that nobody is at its other end.
    let teller = line.teller();
    let prints = async {
        let mut printed = vec![0u8; PRINTED_MOST];
        loop {
            let count = match screen.read(&mut printed).await {
                Ok(0) | Err(_) => break,
                Ok(count) => count,
            };
            let told = Told::Bytes(printed[..count].to_vec());
            if teller.send(told).await.is_err() {
                break;
            }
        }
    };
    // What the client types goes to the program, and a size it asks for is set, in the
    // order it did both.
    let hears = async {
        while let Some(heard) = line.heard().await {
            match heard {
                Heard::Bytes(typed) => {
                    // A program that takes its keys slowly is waited for, but not when
                    // its client is gone meanwhile; what a client typed before it went is
                    // typed where the program takes it. What the client types while the
                    // program is slow waits on its line, as far as a line holds it.
                    let taken = tokio::select! {
                        biased;
                        written = keys.write_all(&typed) => written.is_ok(),
                        () = line.gone() => false,
                    };
                    if !taken {
                        break;
                    }
                }
                // A text that asks for no size is passed over.
                Heard::Text(text) => {
                    let Some((columns, rows)) = resized(&text) else {
                        continue;
                    };
                    let _ = keys.resize(pty_process::Size::new(rows, columns));
                    // The program is told that its terminal changed, whatever the
                    // terminal tells the group in front.
                    if let Some(pid) = pid {
                        let _ = kill(Pid::from_raw(pid), Signal::SIGWINCH);
                    }
                }
            }
        }
    };
    // The terminal is served until the program is gone or takes no keys any more, or
    // its client is gone.
    tokio::select! {
        () = prints => {}
        () = hears => {}
    }
    // The program is ended while its terminal is still there, as the Python Host ends it:
    // a terminal that went first would end it at once, and not for the asking.
    ended(&mut child).await;
    drop((screen, keys));
    line.tell(Told::Close(ENDED_WELL, String::new())).await;
    eprintln!("clarp-host: the terminal {called} closed terminalClose");
}

/// `GET /server-terminal`: a login shell in the home of whoever the Host runs as.
async fn of_the_host(State(app): State<App>, socket: Socket) -> Response {
    // A Host without a token is open to whoever is on its machine, and nobody there has
    // said who they are: no shell is opened for that.
    if app.config.auth_token.is_empty() {
        let why = "Server terminal requires authenticated full-device access";
        return ApiError::new(StatusCode::UNAUTHORIZED, why).into_response();
    }
    if !socket.asked() {
        return line::refused(StatusCode::UPGRADE_REQUIRED, NO_SOCKET);
    }
    let named = std::env::var("SHELL").ok();
    let shell = named
        .filter(|shell| !shell.is_empty())
        .unwrap_or_else(|| USUAL_SHELL.to_string());
    if !runnable(&shell) {
        let why = "server shell unavailable";
        return line::refused(StatusCode::INTERNAL_SERVER_ERROR, why);
    }
    let program = Program {
        called: "server-home".to_string(),
        argv: vec![shell, "-l".to_string()],
        cwd: crate::config::home(),
        env: Vec::new(),
        agent: None,
    };
    socket.open(move |line| serve(line, program))
}

/// What an agent's tool is started as in a terminal: its program on this machine, and
/// what the Python Host starts that tool with. `Err`: the tool has no terminal (501), in
/// that Host's words, or is not on this machine (500).
fn command_of(tool: &str, tool_session: &str) -> Result<Vec<String>, (StatusCode, String)> {
    let started_with = clarp_kernel::socket::terminal_of(tool, tool_session)
        .map_err(|none| (StatusCode::NOT_IMPLEMENTED, none))?;
    // A tool this Host does not run has no terminal here, whatever it has elsewhere.
    let Some(program) = clarp_backend::program(tool) else {
        let none = format!("no interactive terminal for the {tool} backend");
        return Err((StatusCode::NOT_IMPLEMENTED, none));
    };
    if !runnable(&program) {
        let none = format!("{program} not on PATH");
        return Err((StatusCode::INTERNAL_SERVER_ERROR, none));
    }
    Ok(std::iter::once(program).chain(started_with).collect())
}

/// `GET /terminal/<session>`: the agent's own tool in a terminal, taken up on the
/// conversation the agent has with it.
async fn of_an_agent(
    State(app): State<App>,
    Named(named): Named<String>,
    socket: Socket,
) -> Response {
    if !socket.asked() {
        return line::refused(StatusCode::UPGRADE_REQUIRED, NO_SOCKET);
    }
    let session = named.trim_matches('/');
    let Some(member) = app.anyone(session) else {
        return line::refused(StatusCode::NOT_FOUND, "no such agent");
    };
    if member.record.is_janitor {
        return line::refused(StatusCode::FORBIDDEN, LOOKED_AT_ONLY);
    }
    let tool = member.record.backend.as_str();
    let Ok(facts) = member.agent.terminal_facts().await else {
        let why = "the agent cannot be asked";
        return line::refused(StatusCode::SERVICE_UNAVAILABLE, why);
    };
    let argv = match command_of(tool, facts.tool_session.as_deref().unwrap_or("")) {
        Ok(argv) => argv,
        Err((status, none)) => return line::refused(status, &none),
    };
    // Where the agent works, when that is still there: else the home, as for the Host's
    // own terminal.
    let cwd = if facts.cwd.is_dir() {
        facts.cwd
    } else {
        crate::config::home()
    };
    let program = Program {
        called: session.to_string(),
        argv,
        cwd,
        env: facts.env,
        agent: Some(member.agent.clone()),
    };
    socket.open(move |line| serve(line, program))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/server-terminal", get(of_the_host))
        .route("/terminal/{*named}", get(of_an_agent))
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use super::*;

    #[test]
    fn a_tool_this_host_does_not_run_has_no_terminal_here() {
        assert!(clarp_backend::program("muse").is_some());
        assert_eq!(
            clarp_kernel::socket::terminal_of("muse", "abc").unwrap(),
            ["--yolo", "resume", "abc"]
        );
        let none = (
            StatusCode::NOT_IMPLEMENTED,
            "no interactive terminal for the scripted backend".to_string(),
        );
        assert_eq!(command_of("scripted", "a-session"), Err(none));
    }

    #[test]
    fn a_size_is_read_as_the_python_host_reads_one_and_what_is_none_is_passed_over() {
        // Columns and rows, each by its name.
        assert_eq!(
            resized(r#"{"resize":{"cols":132,"rows":43}}"#),
            Some((132, 43))
        );
        assert_eq!(
            resized(r#"{"resize":{"rows":43,"cols":132},"x":1}"#),
            Some((132, 43))
        );
        // A side that is not named, or named as nothing, is the side a terminal starts with.
        assert_eq!(resized(r#"{"resize":{}}"#), Some((80, 24)));
        assert_eq!(resized(r#"{"resize":{"cols":100}}"#), Some((100, 24)));
        assert_eq!(resized(r#"{"resize":{"rows":50}}"#), Some((80, 50)));
        for nothing in ["0", "null", "\"\"", "false", "[]", "{}", "0.0"] {
            let said = format!(r#"{{"resize":{{"cols":{nothing},"rows":{nothing}}}}}"#);
            assert_eq!(resized(&said), Some((80, 24)), "{said}");
        }
        // A number as Python's int() reads it: the whole part, a text of digits, a true.
        assert_eq!(
            resized(r#"{"resize":{"cols":100.9,"rows":30.2}}"#),
            Some((100, 30))
        );
        assert_eq!(
            resized(r#"{"resize":{"cols":" 90 ","rows":"1_0"}}"#),
            Some((90, 10))
        );
        assert_eq!(
            resized(r#"{"resize":{"cols":true,"rows":true}}"#),
            Some((1, 1))
        );
        // A side below one is one; the largest a terminal has is taken.
        assert_eq!(
            resized(r#"{"resize":{"cols":-5,"rows":-0.5}}"#),
            Some((1, 1))
        );
        assert_eq!(
            resized(r#"{"resize":{"cols":65535,"rows":65535}}"#),
            Some((65535, 65535))
        );
        // What no terminal has, and what is no number, asks for nothing.
        assert_eq!(resized(r#"{"resize":{"cols":65536,"rows":24}}"#), None);
        assert_eq!(resized(r#"{"resize":{"cols":80,"rows":65536}}"#), None);
        assert_eq!(resized(r#"{"resize":{"cols":1e30,"rows":24}}"#), None);
        assert_eq!(resized(r#"{"resize":{"cols":"wide","rows":24}}"#), None);
        assert_eq!(resized(r#"{"resize":{"cols":80,"rows":"12.5"}}"#), None);
        assert_eq!(resized(r#"{"resize":{"cols":[80],"rows":24}}"#), None);
        assert_eq!(resized(r#"{"resize":{"cols":80,"rows":{"n":24}}}"#), None);
        // And what is no asking for a size at all.
        for other in [
            "",
            "{}",
            "not json",
            "[1,2]",
            "7",
            r#""resize""#,
            r#"{"resize":null}"#,
            r#"{"resize":[80,24]}"#,
            r#"{"resize":"80x24"}"#,
            r#"{"size":{"cols":80,"rows":24}}"#,
        ] {
            assert_eq!(resized(other), None, "{other}");
        }
    }
}
