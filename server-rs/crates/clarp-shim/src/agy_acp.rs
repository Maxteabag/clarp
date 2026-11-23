//! Driver for one turn of Antigravity over the Agent Client Protocol (`agy acp`, ACP v1:
//! JSON-RPC 2.0, one message per line on standard input and output).
//!
//! The shim runs this as its tool. It reads the turn order on its standard input, starts
//! `agy acp` as its child, opens the conversation (`initialize`, then `session/resume` or
//! `session/new`), chooses the model and effort the order names, sends the prompt, and
//! prints on its standard output, one per line, every message Antigravity sends during the
//! turn as it was sent, and its own lines (`{"clarp": …}`) for what it did. The backend's
//! interpreter reads both from the spool. When the prompt is answered, agy is ended and the
//! driver leaves.
//!
//! A Stop reaches the driver as SIGUSR1. It sends `session/cancel`, waits a bounded time for
//! Antigravity to say the prompt was cancelled, prints what it said, ends agy, and ends itself
//! with SIGTERM, so that the turn ends as stopped.

use std::io::{self, BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError, Sender};
use std::time::{Duration, Instant};

use base64::{Engine as _, engine::general_purpose::STANDARD};
use clarp_tool_args::agy_acp::{self as shared, Failure, Kind, Order};
use nix::sys::signal::{SigSet, Signal, kill};
use nix::unistd::Pid;
use serde_json::{Value, json};

type Error = Box<dyn std::error::Error + Send + Sync>;
type Result<T> = std::result::Result<T, Error>;

/// The ACP version this driver speaks.
const PROTOCOL_VERSION: u64 = 1;
/// How long Antigravity may take over a request before the prompt: it unpacks itself and
/// signs in when it starts.
const SETUP_WAIT: Duration = Duration::from_secs(5 * 60);
/// The longest line read from Antigravity. A longer one is no message of the protocol.
const LINE_MOST: usize = 64 * 1024 * 1024;
/// How long agy is given to leave on its own once its input is closed, and again after TERM.
const LEAVE_WAIT: Duration = Duration::from_secs(5);
/// JSON-RPC: the method is not one this client serves.
const METHOD_NOT_FOUND: i64 = -32601;
/// ACP: the agent needs a sign-in first.
const AUTH_REQUIRED: i64 = -32000;
/// ACP: what was asked for is not there (a conversation).
const NOT_FOUND: i64 = -32002;
/// The certificate bundles of the distributions, for an agy whose own Python looks elsewhere.
const CA_BUNDLES: [&str; 3] = [
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
];

enum Event {
    Message(Value),
    Closed,
    Stop,
}

/// How a wait for an answer ended without one.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Ended {
    /// agy's output closed: it is gone.
    Closed,
    /// The turn was stopped.
    Stopped,
    /// agy said nothing for longer than the wait.
    Silent,
}

enum Answer {
    Result(Value),
    Error(Value),
}

/// Prints one line of the turn's record.
fn emit(value: &Value) -> Result<()> {
    let mut out = io::stdout().lock();
    serde_json::to_writer(&mut out, value)?;
    out.write_all(b"\n")?;
    out.flush()?;
    Ok(())
}

fn said(what: &str, more: Value) -> Value {
    let mut line = json!({ "clarp": what });
    if let (Some(line), Value::Object(more)) = (line.as_object_mut(), more) {
        line.extend(more);
    }
    line
}

/// The other side of the protocol: what is written to agy and what it says.
struct Peer {
    input: Option<ChildStdin>,
    next: u64,
    events: Receiver<Event>,
    /// The conversation, once it is open: what a Stop cancels.
    session: Option<String>,
    /// A Stop came. Every wait after it is bounded by `deadline`.
    stopping: bool,
    deadline: Option<Instant>,
    /// What Antigravity says now is the replay of a conversation it loaded, not the turn's.
    quiet: bool,
}

impl Peer {
    fn send(&mut self, message: &Value) -> Result<()> {
        let input = self.input.as_mut().ok_or("agy's input is closed")?;
        let mut line = serde_json::to_vec(message)?;
        line.push(b'\n');
        input.write_all(&line)?;
        input.flush()?;
        Ok(())
    }

    fn notify(&mut self, method: &str, params: Value) -> Result<()> {
        self.send(&json!({"jsonrpc": "2.0", "method": method, "params": params}))
    }

    fn respond(&mut self, id: &Value, result: std::result::Result<Value, (i64, &str)>) {
        let message = match result {
            Ok(result) => json!({"jsonrpc": "2.0", "id": id, "result": result}),
            Err((code, message)) => {
                json!({"jsonrpc": "2.0", "id": id, "error": {"code": code, "message": message}})
            }
        };
        // agy that cannot be written to has gone, which its closed output says next.
        let _ = self.send(&message);
    }

    /// A Stop: the open prompt is cancelled, and every wait from now on is bounded.
    fn stop(&mut self) -> Result<()> {
        if self.stopping {
            return Ok(());
        }
        self.stopping = true;
        self.deadline = Some(Instant::now() + Duration::from_millis(shared::CANCEL_WAIT_MS));
        if let Some(session) = self.session.clone() {
            let _ = self.notify("session/cancel", json!({"sessionId": session}));
            emit(&said("cancel", json!({"session": session})))?;
        }
        Ok(())
    }

    /// Sends a request and waits for its answer. What else agy says meanwhile is printed or
    /// answered. `wait` bounds the silence between two messages, not the whole request.
    fn request(&mut self, method: &str, params: Value, wait: Duration) -> Result<std::result::Result<Answer, Ended>> {
        let id = self.next;
        self.next += 1;
        self.send(&json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}))?;
        loop {
            let bound = match self.deadline {
                Some(deadline) => deadline.saturating_duration_since(Instant::now()).min(wait),
                None => wait,
            };
            match self.events.recv_timeout(bound) {
                Ok(Event::Message(message)) => {
                    let answers = message.get("method").is_none() && message["id"] == json!(id);
                    if answers {
                        return Ok(Ok(match message.get("error") {
                            Some(error) if !error.is_null() => Answer::Error(error.clone()),
                            _ => Answer::Result(message["result"].clone()),
                        }));
                    }
                    self.other(message)?;
                }
                Ok(Event::Stop) => {
                    if self.session.is_none() || method != "session/prompt" {
                        self.stopping = true;
                        return Ok(Err(Ended::Stopped));
                    }
                    self.stop()?;
                }
                Ok(Event::Closed) | Err(RecvTimeoutError::Disconnected) => {
                    return Ok(Err(Ended::Closed));
                }
                Err(RecvTimeoutError::Timeout) if self.stopping => return Ok(Err(Ended::Stopped)),
                Err(RecvTimeoutError::Timeout) => return Ok(Err(Ended::Silent)),
            }
        }
    }

    /// A message that answers nothing this driver asked: a notification, which goes to the
    /// record, or a request of agy's, which is answered.
    fn other(&mut self, message: Value) -> Result<()> {
        let method = message["method"].as_str().unwrap_or_default().to_string();
        let Some(id) = message.get("id").cloned() else {
            if !(self.quiet && method == "session/update") && !method.is_empty() {
                emit(&message)?;
            }
            return Ok(());
        };
        if method.is_empty() {
            return Ok(()); // an answer to nothing asked
        }
        match method.as_str() {
            "session/request_permission" => {
                let params = &message["params"];
                let options = params["options"].as_array().cloned().unwrap_or_default();
                let chosen = ["allow_once", "allow_always"].iter().find_map(|kind| {
                    options
                        .iter()
                        .find(|option| option["kind"] == *kind)
                        .and_then(|option| option["optionId"].as_str())
                        .filter(|id| !id.trim().is_empty())
                        .map(|id| (*kind, id.to_string()))
                });
                let (answer, outcome) = match &chosen {
                    Some((kind, option)) => (
                        *kind,
                        json!({"outcome": {"outcome": "selected", "optionId": option}}),
                    ),
                    None => ("cancelled", json!({"outcome": {"outcome": "cancelled"}})),
                };
                self.respond(&id, Ok(outcome));
                let names: Vec<Value> = options.iter().map(|option| option["name"].clone()).collect();
                emit(&said(
                    "permission",
                    json!({
                        "title": params["toolCall"]["title"],
                        "toolCallId": params["toolCall"]["toolCallId"],
                        "options": names,
                        "answer": answer,
                    }),
                ))?;
            }
            // No files and no terminal are offered: agy uses its own tools.
            _ => self.respond(&id, Err((METHOD_NOT_FOUND, "Method not found"))),
        }
        Ok(())
    }
}

/// agy, ended when the driver is done with it: its input closed, then TERM, then KILL.
struct Agent {
    child: Child,
}

impl Agent {
    fn end(&mut self, peer: &mut Peer) {
        peer.input = None;
        if self.leaves_within(LEAVE_WAIT) {
            return;
        }
        let pid = Pid::from_raw(self.child.id() as i32);
        let _ = kill(pid, Signal::SIGTERM);
        if self.leaves_within(LEAVE_WAIT) {
            return;
        }
        let _ = self.child.kill();
        let _ = self.child.wait();
    }

    /// Whether agy is gone within `wait`. A thread waits for it, so that no clock is polled.
    fn leaves_within(&mut self, wait: Duration) -> bool {
        let pid = self.child.id();
        let (gone, told) = mpsc::channel();
        std::thread::spawn(move || {
            let _ = super::observe_exit(pid);
            let _ = gone.send(());
        });
        if told.recv_timeout(wait).is_ok() {
            let _ = self.child.wait();
            return true;
        }
        false
    }
}

fn stop_watch(events: Sender<Event>) -> Result<()> {
    let mut signals = SigSet::empty();
    signals.add(Signal::SIGUSR1);
    signals.thread_block()?;
    std::thread::spawn(move || {
        while signals.wait().is_ok() {
            if events.send(Event::Stop).is_err() {
                return;
            }
        }
    });
    Ok(())
}

fn read_lines(output: impl Read + Send + 'static, events: Sender<Event>) {
    std::thread::spawn(move || {
        let mut reader = BufReader::new(output);
        let mut line = Vec::new();
        loop {
            line.clear();
            let read = (&mut reader)
                .take(LINE_MOST as u64 + 1)
                .read_until(b'\n', &mut line);
            match read {
                Ok(0) | Err(_) => break,
                Ok(_) if line.len() > LINE_MOST => break,
                Ok(_) => {
                    // What is not JSON is no message of the protocol: agy's own words, which
                    // belong on its standard error.
                    if let Ok(message) = serde_json::from_slice::<Value>(&line)
                        && message.is_object()
                        && events.send(Event::Message(message)).is_err()
                    {
                        return;
                    }
                }
            }
        }
        let _ = events.send(Event::Closed);
    });
}

fn command(program: &str, cwd: &Path) -> Command {
    let mut command = Command::new(program);
    command
        .arg("acp")
        .arg("--dangerously-skip-permissions")
        .current_dir(cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    for variable in shared::STRIPPED {
        command.env_remove(variable);
    }
    command
        .env("AGY_CLI_DISABLE_AUTO_UPDATE", "1")
        .env("PYTHONUNBUFFERED", "1");
    if std::env::var_os("SSL_CERT_FILE").is_none()
        && let Some(bundle) = CA_BUNDLES.iter().find(|path| Path::new(path).is_file())
    {
        command.env("SSL_CERT_FILE", bundle);
    }
    // A profile of its own keeps its login in a file there. agy 1.5 keeps a Google sign-in
    // in the keyring unless it believes it runs over SSH, whatever else it is told.
    if let Some(profile) = shared::profile() {
        command.arg("--gemini-dir").arg(&profile);
        command.env("AGY_ACP_FORCE_FILE_STORAGE", "1");
        if std::env::var_os("SSH_CONNECTION").is_none() {
            command.env("SSH_CONNECTION", "127.0.0.1 0 127.0.0.1 0");
        }
    }
    command
}

/// The values a select option offers, groups flattened.
fn offered(option: &Value) -> Vec<(String, String)> {
    let mut values = Vec::new();
    for entry in option["options"].as_array().into_iter().flatten() {
        if let Some(group) = entry["options"].as_array() {
            for inner in group {
                if let Some(value) = inner["value"].as_str() {
                    values.push((value.to_string(), inner["name"].as_str().unwrap_or(value).to_string()));
                }
            }
        } else if let Some(value) = entry["value"].as_str() {
            values.push((value.to_string(), entry["name"].as_str().unwrap_or(value).to_string()));
        }
    }
    values
}

/// The option of a category (`model`, `thought_level`), by its category or its id.
fn option<'a>(options: &'a Value, category: &str) -> Option<&'a Value> {
    let options = options.as_array()?;
    options
        .iter()
        .find(|option| option["category"] == category)
        .or_else(|| options.iter().find(|option| option["id"] == category))
}

/// The value of an option that is what was asked for: the same value, or the same name,
/// whatever the case.
fn matching(option: &Value, wanted: &str) -> Option<String> {
    let wanted = wanted.trim();
    offered(option)
        .into_iter()
        .find(|(value, name)| value.eq_ignore_ascii_case(wanted) || name.eq_ignore_ascii_case(wanted))
        .map(|(value, _)| value)
}

/// The conversation's options after a change of them, where the answer says them.
fn options_of(answer: &Value) -> Option<Value> {
    answer.get("configOptions").filter(|options| options.is_array()).cloned()
}

/// Chooses the model and the effort, where the order names them and Antigravity offers
/// them. What is not offered is said, and the turn goes on with what Antigravity chose.
fn configure(peer: &mut Peer, session: &str, order: &Order, opened: &Value) -> Result<std::result::Result<(), Ended>> {
    let mut options = options_of(opened).unwrap_or(Value::Null);
    for (category, wanted) in [("model", order.model.as_str()), ("thought_level", order.effort.as_str())] {
        if wanted.trim().is_empty() {
            continue;
        }
        let Some(found) = option(&options, category).cloned() else {
            // An agy that has no options says its models apart (`models`).
            if category == "model" && opened["models"]["availableModels"].is_array() {
                let known = opened["models"]["availableModels"].as_array().into_iter().flatten().find_map(|model| {
                    let id = model["modelId"].as_str()?;
                    let name = model["name"].as_str().unwrap_or(id);
                    (id.eq_ignore_ascii_case(wanted.trim()) || name.eq_ignore_ascii_case(wanted.trim())).then(|| id.to_string())
                });
                match known {
                    Some(id) if opened["models"]["currentModelId"] != json!(id) => {
                        match peer.request("session/set_model", json!({"sessionId": session, "modelId": id}), SETUP_WAIT)? {
                            Err(ended) => return Ok(Err(ended)),
                            Ok(Answer::Error(error)) => emit(&said("notice", json!({"code": "agy-model", "text": format!("Antigravity did not take the model {wanted}: {}", shared::error_text(&error))})))?,
                            Ok(Answer::Result(_)) => emit(&said("model", json!({"model": id})))?,
                        }
                    }
                    Some(_) => {}
                    None => emit(&said("notice", json!({"code": "agy-model", "text": format!("Antigravity does not offer the model {wanted} here; it runs on its own choice.")})))?,
                }
            } else {
                emit(&said("notice", json!({"code": "agy-model", "text": format!("Antigravity offers no choice of {category}; {wanted} was not applied.")})))?;
            }
            continue;
        };
        let Some(value) = matching(&found, wanted) else {
            let names: Vec<String> = offered(&found).into_iter().map(|(value, _)| value).collect();
            emit(&said("notice", json!({"code": "agy-model", "text": format!("Antigravity does not offer {wanted} as its {category} (it offers {}); it runs on its own choice.", names.join(", "))})))?;
            continue;
        };
        if found["currentValue"] == json!(value) {
            continue;
        }
        let config = found["id"].as_str().unwrap_or(category).to_string();
        match peer.request(
            "session/set_config_option",
            json!({"sessionId": session, "configId": config, "value": value}),
            SETUP_WAIT,
        )? {
            Err(ended) => return Ok(Err(ended)),
            Ok(Answer::Error(error)) => emit(&said(
                "notice",
                json!({"code": "agy-model", "text": format!("Antigravity did not take {value} as its {category}: {}", shared::error_text(&error))}),
            ))?,
            Ok(Answer::Result(answer)) => {
                if let Some(changed) = options_of(&answer) {
                    options = changed;
                } else if let Some(entry) = options.as_array_mut().and_then(|all| all.iter_mut().find(|option| option["id"] == json!(config))) {
                    entry["currentValue"] = json!(value);
                }
            }
        }
    }
    if let Some(model) = option(&options, "model").and_then(|model| model["currentValue"].as_str()) {
        emit(&said("model", json!({"model": model})))?;
    } else if let Some(model) = opened["models"]["currentModelId"].as_str() {
        emit(&said("model", json!({"model": model})))?;
    }
    Ok(Ok(()))
}

/// The content of the prompt: the text, then each file that goes as more than its path.
fn blocks(order: &Order, text: &str, capabilities: &Value) -> Result<Vec<Value>> {
    let mut blocks = vec![json!({"type": "text", "text": text})];
    let mut carried = 0u64;
    for path in order.attachments.iter().take(shared::ATTACHED_COUNT_MOST) {
        let path = PathBuf::from(path);
        let refused = |why: &str| emit(&said("not_attached", json!({"path": path, "why": why})));
        let Some((kind, mime)) = shared::kind_of(&path) else {
            refused("kind")?;
            continue;
        };
        let size = match std::fs::metadata(&path) {
            Ok(meta) if meta.is_file() => meta.len(),
            _ => {
                refused("missing")?;
                continue;
            }
        };
        let (allowed, most) = match kind {
            Kind::Image => (capabilities["image"] == true, shared::IMAGE_MOST),
            Kind::Audio => (capabilities["audio"] == true, shared::AUDIO_MOST),
            // A link is what every agent of the protocol takes.
            Kind::Link => (true, u64::MAX),
        };
        if !allowed {
            refused("not taken")?;
            continue;
        }
        if size > most || (kind != Kind::Link && carried + size > shared::ATTACHED_MOST) {
            refused("too large")?;
            continue;
        }
        let name = path.file_name().and_then(|name| name.to_str()).unwrap_or("file").to_string();
        let block = match kind {
            Kind::Link => json!({
                "type": "resource_link",
                "uri": format!("file://{}", path.display()),
                "name": name,
                "mimeType": mime,
                "size": size,
            }),
            Kind::Image | Kind::Audio => {
                let bytes = std::fs::read(&path)?;
                carried += size;
                json!({
                    "type": if kind == Kind::Image { "image" } else { "audio" },
                    "mimeType": mime,
                    "data": STANDARD.encode(bytes),
                })
            }
        };
        blocks.push(block);
        emit(&said("attached", json!({"path": path, "as": blocks.last().map(|block| block["type"].clone())})))?;
    }
    Ok(blocks)
}

/// Writes the mark of a conversation Antigravity can no longer continue.
fn mark_poisoned(session: &str) {
    if let Some(dir) = shared::poisoned_dir()
        && let Some(mark) = shared::poisoned_mark(&dir, session)
    {
        let _ = std::fs::create_dir_all(&dir);
        let _ = std::fs::write(mark, b"");
    }
}

/// Prints a failure the way the interpreter reads it.
fn failed(stage: &str, error: &Value) -> Result<()> {
    let text = shared::error_text(error);
    emit(&said("failed", json!({"stage": stage, "error": error, "text": text})))
}

fn ended(stage: &str, how: Ended) -> Result<()> {
    let text = match how {
        Ended::Closed => "Antigravity ended before it answered",
        Ended::Silent => "Antigravity stopped answering",
        Ended::Stopped => return Ok(()),
    };
    emit(&said("failed", json!({"stage": stage, "text": text, "silent": how == Ended::Silent})))
}

/// What became of the turn, in the driver's exit code: nought for an answered prompt.
enum Turn {
    Answered,
    Failed,
    Stopped,
}

fn open_session(peer: &mut Peer, order: &Order, cwd: &str, meta: Option<&Value>) -> Result<std::result::Result<(String, Value), Turn>> {
    let mut params = json!({"cwd": cwd, "mcpServers": []});
    if let Some(meta) = meta {
        params["_meta"] = meta.clone();
    }
    let mut lost = false;
    if !order.session.trim().is_empty() {
        let mut resume = params.clone();
        resume["sessionId"] = json!(order.session.trim());
        match peer.request("session/resume", resume.clone(), SETUP_WAIT)? {
            Ok(Answer::Result(opened)) => {
                emit(&said("bound", json!({"session": order.session.trim(), "resumed": true})))?;
                return Ok(Ok((order.session.trim().to_string(), opened)));
            }
            Ok(Answer::Error(error)) if error["code"] == METHOD_NOT_FOUND => {
                // An agy without resume loads the conversation, and replays it: the replay is
                // not this turn's and stays out of the record.
                peer.quiet = true;
                let loaded = peer.request("session/load", resume, SETUP_WAIT)?;
                peer.quiet = false;
                match loaded {
                    Ok(Answer::Result(opened)) => {
                        emit(&said("bound", json!({"session": order.session.trim(), "resumed": true})))?;
                        return Ok(Ok((order.session.trim().to_string(), opened)));
                    }
                    Ok(Answer::Error(error)) if gone(&error) => lost = true,
                    Ok(Answer::Error(error)) => {
                        failed("session", &error)?;
                        return Ok(Err(Turn::Failed));
                    }
                    Err(how) => return Ok(Err(finish_early("session", how)?)),
                }
            }
            Ok(Answer::Error(error)) if gone(&error) => lost = true,
            Ok(Answer::Error(error)) => {
                failed("session", &error)?;
                return Ok(Err(Turn::Failed));
            }
            Err(how) => return Ok(Err(finish_early("session", how)?)),
        }
    }
    match peer.request("session/new", params, SETUP_WAIT)? {
        Ok(Answer::Result(opened)) => match opened["sessionId"].as_str().filter(|id| !id.is_empty()) {
            Some(id) => {
                emit(&said("bound", json!({"session": id, "resumed": false, "lost": lost.then_some(order.session.trim())})))?;
                Ok(Ok((id.to_string(), opened)))
            }
            None => {
                failed("session", &json!({"message": "Antigravity opened no conversation"}))?;
                Ok(Err(Turn::Failed))
            }
        },
        Ok(Answer::Error(error)) => {
            failed(if error["code"] == AUTH_REQUIRED { "auth" } else { "session" }, &error)?;
            Ok(Err(Turn::Failed))
        }
        Err(how) => Ok(Err(finish_early("session", how)?)),
    }
}

/// Whether an error says the conversation is not there.
fn gone(error: &Value) -> bool {
    error["code"] == NOT_FOUND || shared::classify(&shared::error_text(error)) == Failure::Unknown
}

fn finish_early(stage: &str, how: Ended) -> Result<Turn> {
    ended(stage, how)?;
    Ok(if how == Ended::Stopped { Turn::Stopped } else { Turn::Failed })
}

fn turn(peer: &mut Peer, order: &Order) -> Result<Turn> {
    let cwd = if order.cwd.trim().is_empty() {
        std::env::current_dir()?.display().to_string()
    } else {
        order.cwd.clone()
    };
    let initialized = match peer.request(
        "initialize",
        json!({
            "protocolVersion": PROTOCOL_VERSION,
            "clientCapabilities": {"fs": {"readTextFile": false, "writeTextFile": false}, "terminal": false},
            "clientInfo": {"name": "clarp", "title": "Clarp", "version": env!("CARGO_PKG_VERSION")},
        }),
        SETUP_WAIT,
    )? {
        Ok(Answer::Result(result)) => result,
        Ok(Answer::Error(error)) => {
            failed("initialize", &error)?;
            return Ok(Turn::Failed);
        }
        Err(how) => return finish_early("initialize", how),
    };
    let capabilities = &initialized["agentCapabilities"];
    let appends = capabilities["_meta"]["antigravity"]["systemPrompt"]["append"] == true;
    emit(&said(
        "initialized",
        json!({
            "agent": initialized["agentInfo"],
            "prompt": capabilities["promptCapabilities"],
            "appends": appends,
        }),
    ))?;
    let meta = (appends && !order.append.trim().is_empty())
        .then(|| json!({"antigravity": {"systemPrompt": {"append": order.append}}}));
    let (session, opened) = match open_session(peer, order, &cwd, meta.as_ref())? {
        Ok(open) => open,
        Err(turn) => return Ok(turn),
    };
    peer.session = Some(session.clone());
    if let Err(how) = configure(peer, &session, order, &opened)? {
        return finish_early("config", how);
    }
    let text = if meta.is_some() || order.headed.is_empty() {
        &order.prompt
    } else {
        &order.headed
    };
    let prompt = blocks(order, text, &capabilities["promptCapabilities"])?;
    let silence = Duration::from_secs(shared::SILENCE_MOST_SECS);
    match peer.request("session/prompt", json!({"sessionId": session, "prompt": prompt}), silence)? {
        Ok(Answer::Result(result)) => {
            emit(&said("prompted", json!({"result": result})))?;
            Ok(if peer.stopping { Turn::Stopped } else { Turn::Answered })
        }
        Ok(Answer::Error(error)) => {
            let text = shared::error_text(&error);
            if shared::classify(&text) == Failure::Signature {
                mark_poisoned(&session);
            }
            failed("prompt", &error)?;
            Ok(if peer.stopping { Turn::Stopped } else { Turn::Failed })
        }
        Err(how) => finish_early("prompt", how),
    }
}

pub fn run(program: &str) -> Result<u8> {
    let (events, received) = mpsc::channel();
    // Before any thread starts: the threads take the mask, and only the watch takes SIGUSR1.
    stop_watch(events.clone())?;
    let order = Order::from_json(&serde_json::from_reader(io::stdin().lock())?);
    let cwd = if order.cwd.trim().is_empty() {
        std::env::current_dir()?
    } else {
        PathBuf::from(&order.cwd)
    };
    let mut child = command(program, &cwd).spawn()?;
    let output = child.stdout.take().ok_or("agy's output is missing")?;
    read_lines(output, events);
    let mut peer = Peer {
        input: child.stdin.take(),
        next: 1,
        events: received,
        session: None,
        stopping: false,
        deadline: None,
        quiet: false,
    };
    let mut agent = Agent { child };
    let turn = turn(&mut peer, &order);
    agent.end(&mut peer);
    match turn? {
        Turn::Answered => Ok(0),
        Turn::Failed => Ok(1),
        Turn::Stopped => {
            // A stopped turn ends by signal, as the shim's own stop would end a tool.
            let _ = kill(Pid::this(), Signal::SIGTERM);
            loop {
                std::thread::park();
            }
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    #[test]
    fn options_are_found_by_category_or_id_and_matched_by_value_or_name() {
        let options = json!([
            {"id": "mode", "category": "mode", "options": [{"value": "default"}]},
            {"id": "model", "category": "model", "currentValue": "gemini-a",
             "options": [{"group": "g", "name": "G", "options": [
                 {"value": "gemini-a", "name": "Gemini A"},
                 {"value": "claude-b", "name": "Claude B"}]}]},
            {"id": "thought_level", "options": [{"value": "low"}, {"value": "high"}]}
        ]);
        let model = option(&options, "model").unwrap();
        assert_eq!(matching(model, "Claude B").as_deref(), Some("claude-b"));
        assert_eq!(matching(model, "GEMINI-A").as_deref(), Some("gemini-a"));
        assert_eq!(matching(model, "nope"), None);
        let thought = option(&options, "thought_level").unwrap();
        assert_eq!(matching(thought, "high").as_deref(), Some("high"));
    }

    #[test]
    fn a_driver_line_carries_its_own_fields() {
        assert_eq!(
            said("bound", json!({"session": "s"})),
            json!({"clarp": "bound", "session": "s"})
        );
    }
}
