//! Driver for a single OpenCode server. Only the driver knows the server's password.
use std::io::{self, Read, Write};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, mpsc};
use std::time::Duration;

use base64::{Engine as _, engine::general_purpose::STANDARD};
use clarp_tool_args::opencode;
use nix::sys::signal::{SigSet, Signal, kill};
use nix::unistd::Pid;
use serde_json::{Value, json};

use crate::opencode_follow as follow;
use crate::opencode_http::{Client, Result, component, port};

struct Server(Child, Arc<Mutex<Stop>>);

fn leave_stop_to_shim(stop: &Mutex<Stop>) {
    if stop
        .lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
        .requested
    {
        // The abort thread ends this process by signal. In particular an idle
        // event caused by that abort must not let main return success.
        loop {
            std::thread::park();
        }
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        // TERM lets the server close the MCP and language servers it started.
        // The observer leaves the pid waitable until the watchdog has finished.
        leave_stop_to_shim(&self.1);
        let pid = self.0.id();
        let server = Pid::from_raw(pid as i32);
        let _ = kill(server, Signal::SIGTERM);
        std::thread::scope(|scope| {
            let (ended, wait) = mpsc::channel::<()>();
            scope.spawn(move || {
                if matches!(
                    wait.recv_timeout(Duration::from_secs(5)),
                    Err(mpsc::RecvTimeoutError::Timeout)
                ) {
                    let _ = kill(server, Signal::SIGKILL);
                }
            });
            let _ = super::observe_exit(pid);
            let _ = ended.send(());
        });
        let _ = self.0.wait();
        leave_stop_to_shim(&self.1);
    }
}

#[derive(Default)]
struct Stop {
    requested: bool,
    session: Option<(Client, String)>,
}

fn stop_handle() -> Result<Arc<Mutex<Stop>>> {
    let mut signals = SigSet::empty();
    signals.add(Signal::SIGUSR1);
    signals.thread_block()?;
    let stop = Arc::new(Mutex::new(Stop::default()));
    let watched = stop.clone();
    std::thread::spawn(move || {
        if signals.wait().is_ok() {
            let session = {
                let mut state = watched
                    .lock()
                    .unwrap_or_else(std::sync::PoisonError::into_inner);
                state.requested = true;
                state.session.clone()
            };
            if let Some((client, path)) = session {
                let _ = client.post(&format!("{path}/abort"), None, 3);
            }
            // Ending the driver releases the outer shim's wait immediately.
            // The shim terminates the server and any other members of this group.
            let _ = kill(Pid::this(), Signal::SIGTERM);
            // If the kernel could not deliver TERM, the outer shim still owns escalation.
            loop {
                std::thread::park();
            }
        }
    });
    Ok(stop)
}

fn emit(value: &Value) -> Result<()> {
    let mut out = io::stdout().lock();
    serde_json::to_writer(&mut out, value)?;
    out.write_all(b"\n")?;
    out.flush()?;
    Ok(())
}

fn serve(client: Client, request: &Value, stop: &Mutex<Stop>) -> Result<()> {
    let sid = client.open_session(request["session"].as_str().unwrap_or_default())?;
    emit(&json!({"type":"session", "sessionID":sid}))?;
    let session = format!("/session/{}", component(&sid));
    let stream = client.subscribe()?;
    let body = follow::body(
        request["prompt"].as_str().unwrap_or_default(),
        request["model"].as_str().unwrap_or_default(),
        request["effort"].as_str().unwrap_or_default(),
    );
    {
        let mut state = stop
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner);
        if state.requested {
            return Err("turn stopped".into());
        }
        state.session = Some((client.clone(), session.clone()));
    }
    client.post(&format!("{session}/prompt_async"), Some(&body), 30)?;
    let mut turn = follow::Follow::new(&sid);
    loop {
        let ev = stream
            .recv_timeout(Duration::from_secs(90))
            .map_err(|_| "OpenCode server stopped before the turn finished")?;
        if turn.see(&client, &ev, &mut emit)? {
            return Ok(());
        }
    }
}

pub fn run(binary: &str) -> Result<u8> {
    let stop = stop_handle()?;
    let req: Value = serde_json::from_reader(io::stdin().lock())?;
    let mut random = [0u8; 24];
    std::fs::File::open("/dev/urandom")?.read_exact(&mut random)?;
    let password = STANDARD.encode(random);
    let child = Command::new(binary)
        .args(["serve", "--hostname", "127.0.0.1", "--port", "0"])
        .env("OPENCODE_SERVER_PASSWORD", &password)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .spawn()?;
    let mut server = Server(child, stop.clone());
    let stdout = server.0.stdout.take().ok_or("missing server stdout")?;
    if let Some(port) = port(stdout) {
        let client = Client::new(
            port,
            &password,
            std::env::current_dir()?.display().to_string(),
        );
        serve(client, &req, &stop)?;
        Ok(0)
    } else {
        drop(server);
        if stop
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
            .requested
        {
            return Err("turn stopped".into());
        }
        let mut args = opencode::command(
            req["session"].as_str().unwrap_or(""),
            req["model"].as_str().unwrap_or(""),
            req["effort"].as_str().unwrap_or(""),
            true,
        );
        args.push(req["prompt"].as_str().ok_or("missing prompt")?.to_owned());
        use std::os::unix::process::CommandExt;
        Err(Command::new(binary)
            .args(args)
            .stdin(Stdio::null())
            .exec()
            .into())
    }
}
