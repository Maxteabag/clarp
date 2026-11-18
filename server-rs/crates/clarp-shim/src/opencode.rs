//! Driver for a single OpenCode server. Only the driver knows the server's password.
use std::collections::BTreeSet;
use std::io::{self, BufReader, Read, Write};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, mpsc};
use std::time::Duration;

use clarp_tool_args::opencode;
use base64::{Engine as _, engine::general_purpose::STANDARD};
use nix::sys::signal::{SigSet, Signal, kill};
use nix::unistd::Pid;
use serde_json::{Value, json};

type Error = Box<dyn std::error::Error + Send + Sync>;
type Result<T> = std::result::Result<T, Error>;

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

// An announcement is two whitespace-delimited tokens: "on" follows "listening",
// then the loopback URL. A chunk boundary never ends a token.
fn port(stdout: impl Read + Send + 'static) -> Option<u16> {
    let (send, receive) = mpsc::sync_channel(1);
    std::thread::spawn(move || {
        let mut input = BufReader::new(stdout);
        let mut token = Vec::new();
        let mut stage = 0;
        let mut chunk = [0u8; 4096];
        while let Ok(n) = input.read(&mut chunk) {
            for b in chunk[..n].iter().copied().chain((n == 0).then_some(b' ')) {
                if !b.is_ascii_whitespace() {
                    if token.len() <= 8192 {
                        token.push(b);
                    }
                    continue;
                }
                if token.is_empty() {
                    continue;
                }
                let word = std::str::from_utf8(&token).unwrap_or_default();
                if stage == 2
                    && token.len() <= 8192
                    && let Some(port) = word
                        .strip_prefix("http://127.0.0.1:")
                        .and_then(|s| s.trim_end_matches('/').parse::<u16>().ok())
                        .filter(|n| *n != 0)
                {
                    let _ = send.send(port);
                    let _ = io::copy(&mut input, &mut io::sink());
                    return;
                }
                stage = match (stage, word) {
                    (_, "listening") => 1,
                    (1, "on") => 2,
                    _ => 0,
                };
                token.clear();
            }
            if n == 0 {
                break;
            }
        }
    });
    receive.recv_timeout(Duration::from_secs(30)).ok()
}

#[derive(Clone)]
struct Client {
    base: String,
    auth: String,
    directory: String,
}

fn component(value: &str) -> String {
    let mut out = String::new();
    for b in value.bytes() {
        if b.is_ascii_alphanumeric() || b"-_.~".contains(&b) {
            out.push(b as char);
        } else {
            use std::fmt::Write;
            let _ = write!(out, "%{b:02X}");
        }
    }
    out
}

impl Client {
    fn url(&self, path: &str) -> String {
        format!(
            "{}{path}?directory={}",
            self.base,
            component(&self.directory)
        )
    }

    fn request(&self, method: minreq::Method, path: &str) -> minreq::Request {
        minreq::Request::new(method, self.url(path))
            .with_follow_redirects(false)
            .with_max_headers_size(64 * 1024)
            .with_max_status_line_length(4096)
            .with_header("Authorization", &self.auth)
    }

    fn response(req: minreq::Request) -> Result<minreq::ResponseLazy> {
        let reply = req.send_lazy()?;
        if !(200..300).contains(&reply.status_code) {
            return Err(format!("OpenCode server returned HTTP {}", reply.status_code).into());
        }
        Ok(reply)
    }

    fn json(req: minreq::Request) -> Result<Value> {
        const LIMIT: u64 = 10 * 1024 * 1024;
        let mut bytes = Vec::new();
        Read::take(Self::response(req)?, LIMIT + 1).read_to_end(&mut bytes)?;
        if bytes.len() as u64 > LIMIT {
            return Err("OpenCode response exceeds ten MiB".into());
        }
        if bytes.iter().all(u8::is_ascii_whitespace) {
            Ok(Value::Null)
        } else {
            Ok(serde_json::from_slice(&bytes)?)
        }
    }

    fn post(&self, path: &str, body: Option<&Value>, seconds: u64) -> Result<Value> {
        let mut req = self
            .request(minreq::Method::Post, path)
            .with_timeout(seconds);
        if let Some(body) = body {
            req = req
                .with_header("Content-Type", "application/json")
                .with_body(serde_json::to_vec(body)?);
        }
        Self::json(req)
    }

    fn subscribe(&self) -> Result<mpsc::Receiver<Value>> {
        let req = self
            .request(minreq::Method::Get, "/event")
            .with_header("Accept", "text/event-stream");
        let (opened, ready) = mpsc::channel();
        let (send, receive) = mpsc::sync_channel(8);
        std::thread::spawn(move || match Self::response(req) {
            Ok(stream) => {
                if opened.send(Ok(())).is_ok() {
                    events(stream, send);
                }
            }
            Err(err) => {
                let _ = opened.send(Err(err));
            }
        });
        ready.recv_timeout(Duration::from_secs(30))??;
        Ok(receive)
    }

    fn open_session(&self, old: &str) -> Result<String> {
        if old.is_empty() {
            let rules: Vec<_> = ["question", "plan_enter", "plan_exit"]
                .iter()
                .map(|rule| json!({"permission":rule,"pattern":"*","action":"deny"}))
                .collect();
            let result = self.post("/session", Some(&json!({"permission":rules})), 30)?;
            return result["id"]
                .as_str()
                .filter(|id| clarp_tool_args::native_session_name(id))
                .map(str::to_owned)
                .ok_or_else(|| "OpenCode did not create a session".into());
        }
        if !clarp_tool_args::native_session_name(old) {
            return Err(format!("OpenCode session {old} not found").into());
        }
        let path = format!("/session/{}", component(old));
        let result = Self::json(self.request(minreq::Method::Get, &path).with_timeout(30))
            .map_err(|_| format!("OpenCode session {old} not found"))?;
        if result["id"].as_str() != Some(old) {
            return Err(format!("OpenCode session {old} not found").into());
        }
        Ok(old.to_string())
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

// ResponseLazy decodes HTTP chunking incrementally. Its Read implementation waits
// to fill the caller's buffer, so take decoded bytes directly: the next event
// must be delivered even when the peer is now waiting for a permission reply.
fn events(stream: minreq::ResponseLazy, send: mpsc::SyncSender<Value>) {
    const LIMIT: usize = 4 * 1024 * 1024;
    let mut line = Vec::new();
    let mut data = Vec::new();
    for byte in stream {
        let Ok((byte, _)) = byte else { break };
        if byte != b'\n' {
            if line.len() >= LIMIT {
                break;
            }
            line.push(byte);
            continue;
        }
        let payload = line.strip_suffix(b"\r").unwrap_or(&line);
        if payload.is_empty() {
            if let Ok(event) = serde_json::from_slice(&data)
                && send.send(event).is_err()
            {
                break;
            }
            data.clear();
        } else if let Some(text) = payload.strip_prefix(b"data:") {
            let text = text.trim_ascii_start();
            if data.len() + text.len() + 1 > LIMIT {
                break;
            }
            data.extend_from_slice(text);
            data.push(b'\n');
        }
        line.clear();
    }
}

fn serve(client: Client, request: &Value, stop: &Mutex<Stop>) -> Result<()> {
    let sid = client.open_session(request["session"].as_str().unwrap_or_default())?;
    emit(&json!({"type":"session", "sessionID":sid}))?;
    let session = format!("/session/{}", component(&sid));
    let stream = client.subscribe()?;
    let mut body = json!({"parts":[{"type":"text","text":request["prompt"]}]});
    if let Some(model) = opencode::model_pair(request["model"].as_str().unwrap_or_default()) {
        body["model"] = model;
    }
    if request["effort"].as_str().is_some_and(|s| !s.is_empty()) {
        body["variant"] = request["effort"].clone();
    }
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
    let mut sessions = BTreeSet::from([sid.clone()]);
    let mut busy = false;
    loop {
        let ev = stream
            .recv_timeout(Duration::from_secs(90))
            .map_err(|_| "OpenCode server stopped before the turn finished")?;
        let props = &ev["properties"];
        let kind = ev["type"].as_str().unwrap_or("");
        let mine = props["sessionID"].as_str() == Some(&sid);
        match kind {
            "session.created" => {
                if props["info"]["parentID"]
                    .as_str()
                    .is_some_and(|id| sessions.contains(id))
                    && let Some(id) = props["info"]["id"].as_str()
                {
                    sessions.insert(id.to_string());
                }
            }
            "permission.asked" | "question.asked" => {
                if props["sessionID"]
                    .as_str()
                    .is_some_and(|id| sessions.contains(id))
                    && let Some(id) = props["id"].as_str().filter(|id| !id.is_empty())
                {
                    let id = component(id);
                    if kind == "permission.asked" {
                        client.post(
                            &format!("/permission/{id}/reply"),
                            Some(&json!({"reply":"once"})),
                            30,
                        )?;
                    } else {
                        client.post(&format!("/question/{id}/reject"), None, 30)?;
                    }
                }
            }
            "message.part.updated" if props["part"]["sessionID"] == sid => {
                busy |= props["part"]["type"] == "step-start";
                emit(&ev)?;
            }
            "message.part.delta" | "session.error" if mine => emit(&ev)?,
            "session.status" if mine => match props["status"]["type"].as_str() {
                Some("busy" | "retry") => busy = true,
                Some("idle") if busy => break,
                _ => {}
            },
            "session.idle" if mine && busy => break,
            _ => {}
        }
    }
    Ok(())
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
        let client = Client {
            base: format!("http://127.0.0.1:{port}"),
            directory: std::env::current_dir()?.display().to_string(),
            auth: format!(
                "Basic {}",
                STANDARD.encode(format!("opencode:{password}").as_bytes())
            ),
        };
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

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn announcements_are_loopback_only_bounded_and_complete() {
        let ok = "listening on http://127.0.0.1:12345";
        for (data, want) in [
            (ok.to_owned(), Some(12345)),
            (format!("{} {ok}/\nnoise", "x".repeat(40000)), Some(12345)),
            ("listening on http://192.0.2.9:10".to_owned(), None),
            ("listening on http://127.0.0.1:65536".to_owned(), None),
            ("listening on http://127.0.0.1:0".to_owned(), None),
            ("listening on http://127.0.0.1:12@outside".to_owned(), None),
            (format!("{ok}{}\n{ok}", "/".repeat(8192)), Some(12345)),
            (format!("{ok}{}", "/".repeat(8192)), None),
            (format!("{} {ok}", "x".repeat(4096 - 12)), Some(12345)),
            (String::new(), None),
        ] {
            assert_eq!(port(io::Cursor::new(data.into_bytes())), want);
        }
    }
}
