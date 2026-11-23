//! Driver for one turn of Antigravity over the Agent Client Protocol (`agy acp`).
//!
//! The shim runs this as its tool. It reads the turn order on its standard input, starts
//! `agy acp` as its child, opens the conversation (`initialize`, then `session/resume` or
//! `session/new`), chooses the model and effort the order names, sends the prompt, and
//! prints on its standard output, one per line, every notification Antigravity sends during
//! the turn as it was sent, and its own lines (`{"clarp": …}`) for what it did. The backend's
//! interpreter reads both from the spool. When the prompt is answered, agy is ended and the
//! driver leaves. The protocol itself is `clarp-acp`'s.
//!
//! A Stop reaches the driver as SIGUSR1. It sends `session/cancel`, waits a bounded time for
//! Antigravity to answer the prompt, prints what it said, ends agy (its input closed, then
//! TERM, then KILL only if it stays), and ends itself with SIGTERM, so that the turn ends as
//! stopped. Every permission Antigravity asks for is answered at once by Clarp's policy
//! ([`clarp_acp::schema::chosen`]): nobody is waited for.

use std::collections::BTreeSet;
use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Sender};
use std::time::{Duration, Instant};

use base64::{Engine as _, engine::general_purpose::STANDARD};
use clarp_acp::agy::{self as shared, Failure, Kind, Order};
use clarp_acp::client::{self, Answer, Client, Ended, Handler, Inbound};
use clarp_acp::{code, method, schema};
use nix::sys::signal::{SigSet, Signal, kill, killpg};
use nix::unistd::{Pid, getpgrp, getpid};
use serde_json::{Value, json};

type Error = Box<dyn std::error::Error + Send + Sync>;
type Result<T> = std::result::Result<T, Error>;

/// How long Antigravity may take over a request before the prompt: it unpacks itself and
/// signs in when it starts.
const SETUP_WAIT: Duration = Duration::from_secs(5 * 60);
/// How long what agy left behind is given to leave after TERM, before it is killed.
const REAP_GRACE: Duration = Duration::from_millis(500);
/// How long agy is given to leave on its own once its input is closed, and again after TERM.
const LEAVE_WAIT: Duration = Duration::from_secs(5);
/// The certificate bundles of the distributions, for an agy whose own Python looks elsewhere.
const CA_BUNDLES: [&str; 3] = [
    "/etc/ssl/certs/ca-certificates.crt",
    "/etc/pki/tls/certs/ca-bundle.crt",
    "/etc/ssl/cert.pem",
];

/// Prints one line of the turn's record.
fn emit(value: &Value) -> io::Result<()> {
    let mut out = io::stdout().lock();
    serde_json::to_writer(&mut out, value)?;
    out.write_all(b"\n")?;
    out.flush()
}

fn said(what: &str, more: Value) -> Value {
    let mut line = json!({ "clarp": what });
    if let (Some(line), Value::Object(more)) = (line.as_object_mut(), more) {
        line.extend(more);
    }
    line
}

fn ms(since: Instant) -> u64 {
    u64::try_from(since.elapsed().as_millis()).unwrap_or(u64::MAX)
}

/// What the driver does with what Antigravity says besides its answers.
struct Record {
    /// What Antigravity says now is the replay of a conversation it loaded, not the turn's.
    quiet: bool,
    /// When agy was started, and when it first said something of the prompt.
    launched: Instant,
    prompting: bool,
    first_update_ms: Option<u64>,
    /// How long continuing the conversation took.
    resume_ms: Option<u64>,
    /// The tool calls Antigravity began and has not said the end of: while one is open, the
    /// prompt may be silent for as long as a tool runs.
    open_tools: BTreeSet<String>,
    /// The prompt was cancelled: what Antigravity asks after that is declined.
    stopping: bool,
}

impl Handler for Record {
    fn notification(&mut self, message: &Value) -> io::Result<()> {
        let update = message["method"] == method::SESSION_UPDATE;
        if self.quiet && update {
            return Ok(());
        }
        if update && self.prompting && self.first_update_ms.is_none() {
            self.first_update_ms = Some(ms(self.launched));
        }
        match schema::SessionUpdate::of_notification(message) {
            Some(
                schema::SessionUpdate::ToolCall(call) | schema::SessionUpdate::ToolCallUpdate(call),
            ) if !call.tool_call_id.is_empty() => {
                if call.ended() {
                    self.open_tools.remove(&call.tool_call_id);
                } else {
                    self.open_tools.insert(call.tool_call_id);
                }
            }
            _ => {}
        }
        // One line of the record each, short enough that the shim never splits it.
        emit(&schema::bounded(message))
    }

    fn request(
        &mut self,
        asked: &str,
        params: &Value,
    ) -> io::Result<std::result::Result<Value, (i64, String)>> {
        if asked != method::REQUEST_PERMISSION {
            // No files and no terminal are offered: agy uses its own tools.
            return Ok(Err((
                code::METHOD_NOT_FOUND,
                "Method not found".to_string(),
            )));
        }
        let options = schema::permission_options(params);
        // After a Stop, nothing more is allowed: the request is answered as cancelled.
        let chosen = if self.stopping {
            None
        } else {
            schema::chosen(&options)
        };
        emit(&said(
            "permission",
            json!({
                "title": params["toolCall"]["title"],
                "toolCallId": params["toolCall"]["toolCallId"],
                "options": options.iter().map(|option| option.name.clone()).collect::<Vec<_>>(),
                "kind": chosen.map(|option| option.kind.clone()),
                "answer": chosen.map(|option| option.name.clone()),
                "stopped": self.stopping,
            }),
        ))?;
        Ok(Ok(schema::permission_outcome(chosen)))
    }
}

/// The conversation as the driver holds it: the client, what it records, and a Stop.
struct Turn {
    client: Client<ChildStdin>,
    record: Record,
    /// A Stop came: the prompt is cancelled, and the wait for its answer is bounded.
    stopping: Option<Instant>,
}

/// How the turn went, in the driver's exit: nought for an answered prompt.
enum Outcome {
    Answered,
    Failed,
    Stopped,
}

impl Turn {
    /// A request answered within the setup bound. A Stop before the prompt ends the turn.
    fn setup(&mut self, asked: &str, params: Value) -> Result<std::result::Result<Answer, Ended>> {
        let id = self.client.send_request(asked, params)?;
        Ok(self.client.wait(id, SETUP_WAIT, None, &mut self.record)?)
    }

    /// The prompt, answered; a Stop cancels it and waits a bounded time for the answer.
    fn prompt(
        &mut self,
        session: &str,
        blocks: Vec<Value>,
        silence: Duration,
    ) -> Result<std::result::Result<Answer, Ended>> {
        let id = self.client.send_request(
            method::SESSION_PROMPT,
            json!({"sessionId": session, "prompt": blocks}),
        )?;
        self.record.prompting = true;
        let tool_silence = Duration::from_secs(shared::TOOL_SILENCE_MOST_SECS);
        loop {
            // A tool that runs says nothing until it ends: a build or a test suite. The short
            // bound holds only while no tool call is open.
            let short = self.record.open_tools.is_empty();
            let bound = if short { silence } else { tool_silence };
            match self
                .client
                .wait(id, bound, self.stopping, &mut self.record)?
            {
                Err(Ended::Silent)
                    if short && self.stopping.is_none() && !self.record.open_tools.is_empty() => {}
                Err(Ended::Interrupted) => {
                    self.record.stopping = true;
                    if self.stopping.is_none() {
                        self.stopping =
                            Some(Instant::now() + Duration::from_millis(shared::CANCEL_WAIT_MS));
                        let _ = self
                            .client
                            .notify(method::SESSION_CANCEL, json!({"sessionId": session}));
                        emit(&said("cancel", json!({"session": session})))?;
                    }
                }
                other => {
                    self.record.prompting = false;
                    return Ok(other);
                }
            }
        }
    }
}

/// agy, ended when the driver is done with it.
struct Agent {
    child: Child,
}

impl Agent {
    fn end(&mut self, client: &mut Client<ChildStdin>) {
        client.close();
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

/// SIGUSR1, the shim's way to ask for a Stop, becomes an interrupt of the client's wait.
fn stop_watch(inbound: Sender<Inbound>) -> Result<()> {
    // SIGTERM is held by every thread of the driver, so that the driver can end what agy left
    // in its process group without ending itself ([`reap`]); agy and its children start with
    // nothing held. A stopped turn lets it through to end itself by it.
    let mut held = SigSet::empty();
    held.add(Signal::SIGTERM);
    held.thread_block()?;
    let mut signals = SigSet::empty();
    signals.add(Signal::SIGUSR1);
    signals.thread_block()?;
    std::thread::spawn(move || {
        while signals.wait().is_ok() {
            if inbound.send(Inbound::Interrupt).is_err() {
                return;
            }
        }
    });
    Ok(())
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
    // What agy is given is `clarp-acp`'s to say; what the Host's environment already holds is
    // read here.
    let bundle = std::env::var_os("SSL_CERT_FILE")
        .is_none()
        .then(|| CA_BUNDLES.iter().find(|path| Path::new(path).is_file()))
        .flatten();
    let over_ssh = std::env::var_os("SSH_CONNECTION").is_some();
    let given = shared::child_env(shared::profile().as_deref(), bundle.copied(), over_ssh);
    command.args(&given.args);
    for variable in given.removed {
        command.env_remove(variable);
    }
    for (variable, value) in given.set {
        command.env(variable, value);
    }
    command
}

fn notice(text: String) -> io::Result<()> {
    emit(&said("notice", json!({"code": "agy-model", "text": text})))
}

/// Sets the model and the thinking level the order asks for, as Antigravity offers them
/// ([`shared::choose`]). A model that is not offered, or that Antigravity will not take, fails
/// the turn: it never runs on another model without a word. A thinking level that is not
/// offered is said, and the turn goes on.
fn configure(
    turn: &mut Turn,
    session: &str,
    order: &Order,
    opened: &Value,
) -> Result<std::result::Result<(), Outcome>> {
    let mut options = opened
        .get("configOptions")
        .filter(|options| options.is_array())
        .cloned()
        .unwrap_or(Value::Null);
    let models = &opened["models"];
    let choice = match shared::choose(&options, models, &order.model, &order.effort) {
        Ok(choice) => choice,
        Err(why) => {
            failed("config", &json!({"message": why}))?;
            return Ok(Err(Outcome::Failed));
        }
    };
    for note in &choice.notes {
        notice(note.clone())?;
    }
    let mut set_model = None;
    let asked = [
        (true, choice.model.clone()),
        (false, choice.thought.clone()),
    ];
    for (is_model, wanted) in asked {
        let Some((config, value)) = wanted else {
            continue;
        };
        let current = if config.is_empty() {
            models["currentModelId"].clone()
        } else {
            schema::option(&options, &config)
                .map_or(Value::Null, |option| option["currentValue"].clone())
        };
        if current == json!(value) {
            continue;
        }
        let (asked_for, params) = if config.is_empty() {
            (
                method::SESSION_SET_MODEL,
                json!({"sessionId": session, "modelId": value}),
            )
        } else {
            (
                method::SESSION_SET_CONFIG_OPTION,
                json!({"sessionId": session, "configId": config, "value": value}),
            )
        };
        match turn.setup(asked_for, params)? {
            Err(how) => return Ok(Err(early("config", how)?)),
            Ok(Answer::Error(error)) if is_model => {
                let why = format!(
                    "Antigravity did not take the model {value}: {}",
                    shared::error_text(&error)
                );
                failed("config", &json!({"message": why}))?;
                return Ok(Err(Outcome::Failed));
            }
            Ok(Answer::Error(error)) => notice(format!(
                "Antigravity did not take {value} as its thinking level: {}",
                shared::error_text(&error)
            ))?,
            Ok(Answer::Result(answer)) => {
                if config.is_empty() {
                    set_model = Some(value);
                } else if let Some(changed) =
                    answer.get("configOptions").filter(|all| all.is_array())
                {
                    options = changed.clone();
                } else if let Some(entry) = options
                    .as_array_mut()
                    .and_then(|all| all.iter_mut().find(|option| option["id"] == json!(config)))
                {
                    entry["currentValue"] = json!(value);
                }
            }
        }
    }
    let model = set_model
        .or_else(|| schema::current(&options, "model"))
        .or_else(|| models["currentModelId"].as_str().map(str::to_string));
    if let Some(model) = model {
        emit(&said("model", json!({"model": model})))?;
    }
    Ok(Ok(()))
}

/// The content of the prompt: the text, then each file that goes as more than its path.
fn blocks(order: &Order, text: &str, capabilities: &Value) -> io::Result<Vec<Value>> {
    let mut blocks = vec![schema::text_block(text)];
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
        let name = path
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("file")
            .to_string();
        let block = match kind {
            Kind::Link => {
                schema::resource_link(&format!("file://{}", path.display()), &name, mime, size)
            }
            Kind::Image => {
                carried += size;
                schema::image_block(mime, STANDARD.encode(std::fs::read(&path)?))
            }
            Kind::Audio => {
                carried += size;
                schema::audio_block(mime, STANDARD.encode(std::fs::read(&path)?))
            }
        };
        emit(&said(
            "attached",
            json!({"path": path, "as": block["type"]}),
        ))?;
        blocks.push(block);
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
fn failed(stage: &str, error: &Value) -> io::Result<()> {
    let text = shared::error_text(error);
    emit(&said(
        "failed",
        json!({"stage": stage, "error": error, "text": text}),
    ))
}

/// The turn ends before its answer: why, as the interpreter reads it.
fn early(stage: &str, how: Ended) -> io::Result<Outcome> {
    let text = match how {
        Ended::Closed => "Antigravity ended before it answered",
        Ended::Silent => "Antigravity stopped answering",
        Ended::Interrupted => return Ok(Outcome::Stopped),
    };
    emit(&said(
        "failed",
        json!({"stage": stage, "text": text, "silent": how == Ended::Silent}),
    ))?;
    Ok(Outcome::Failed)
}

/// Whether an error says the conversation is not there.
fn gone(error: &Value) -> bool {
    error["code"] == code::NOT_FOUND
        || shared::classify(&shared::error_text(error)) == Failure::Unknown
}

/// Opens the conversation: the one the order names, else a new one. Its id and what the
/// agent answered of it (its options); or how the turn ended instead.
fn open(
    turn: &mut Turn,
    order: &Order,
    cwd: &str,
    meta: Option<&Value>,
) -> Result<std::result::Result<(String, Value), Outcome>> {
    let mut params = json!({"cwd": cwd, "mcpServers": []});
    if let Some(meta) = meta {
        params["_meta"] = meta.clone();
    }
    let named = order.session.trim();
    if !named.is_empty() {
        let mut resume = params.clone();
        resume["sessionId"] = json!(named);
        let began = Instant::now();
        let mut answer = turn.setup(method::SESSION_RESUME, resume.clone())?;
        if matches!(&answer, Ok(Answer::Error(error)) if error["code"] == code::METHOD_NOT_FOUND) {
            // An agy without resume loads the conversation and replays it: the replay is not
            // this turn's and stays out of the record.
            turn.record.quiet = true;
            answer = turn.setup(method::SESSION_LOAD, resume)?;
            turn.record.quiet = false;
        }
        turn.record.resume_ms = Some(ms(began));
        match answer {
            Ok(Answer::Result(opened)) => {
                emit(&said("bound", json!({"session": named, "resumed": true})))?;
                return Ok(Ok((named.to_string(), opened)));
            }
            // A conversation agy no longer has is not begun anew here, empty: it is marked,
            // and the turn fails, so that the Host continues it with its own record of it.
            Ok(Answer::Error(error)) if gone(&error) => {
                mark_poisoned(named);
                let said = shared::error_text(&error);
                failed(
                    "session",
                    &json!({"code": code::NOT_FOUND, "message": format!("Session not found: {said}")}),
                )?;
                return Ok(Err(Outcome::Failed));
            }
            Ok(Answer::Error(error)) => {
                failed(
                    if error["code"] == code::AUTH_REQUIRED {
                        "auth"
                    } else {
                        "session"
                    },
                    &error,
                )?;
                return Ok(Err(Outcome::Failed));
            }
            Err(how) => return Ok(Err(early("session", how)?)),
        }
    }
    match turn.setup(method::SESSION_NEW, params)? {
        Ok(Answer::Result(opened)) => {
            match opened["sessionId"].as_str().filter(|id| !id.is_empty()) {
                Some(id) => {
                    emit(&said("bound", json!({"session": id, "resumed": false})))?;
                    Ok(Ok((id.to_string(), opened)))
                }
                None => {
                    failed(
                        "session",
                        &json!({"message": "Antigravity opened no conversation"}),
                    )?;
                    Ok(Err(Outcome::Failed))
                }
            }
        }
        Ok(Answer::Error(error)) => {
            failed(
                if error["code"] == code::AUTH_REQUIRED {
                    "auth"
                } else {
                    "session"
                },
                &error,
            )?;
            Ok(Err(Outcome::Failed))
        }
        Err(how) => Ok(Err(early("session", how)?)),
    }
}

fn run_turn(turn: &mut Turn, order: &Order, cwd: &str) -> Result<Outcome> {
    let init = json!({
        "protocolVersion": clarp_acp::PROTOCOL_VERSION,
        "clientCapabilities": {"fs": {"readTextFile": false, "writeTextFile": false}, "terminal": false},
        "clientInfo": {"name": "clarp", "title": "Clarp", "version": clarp_acp::CLIENT_VERSION},
    });
    let initialized = match turn.setup(method::INITIALIZE, init)? {
        Ok(Answer::Result(result)) => result,
        Ok(Answer::Error(error)) => {
            failed("initialize", &error)?;
            return Ok(Outcome::Failed);
        }
        Err(how) => return Ok(early("initialize", how)?),
    };
    let capabilities = &initialized["agentCapabilities"];
    let appends = capabilities["_meta"]["antigravity"]["systemPrompt"]["append"] == true;
    emit(&said(
        "initialized",
        json!({"agent": initialized["agentInfo"], "prompt": capabilities["promptCapabilities"], "appends": appends}),
    ))?;
    let meta = (appends && !order.append.trim().is_empty())
        .then(|| json!({"antigravity": {"systemPrompt": {"append": order.append}}}));
    let (session, opened) = match open(turn, order, cwd, meta.as_ref())? {
        Ok(open) => open,
        Err(outcome) => return Ok(outcome),
    };
    if let Err(outcome) = configure(turn, &session, order, &opened)? {
        return Ok(outcome);
    }
    let text = if meta.is_some() || order.headed.is_empty() {
        &order.prompt
    } else {
        &order.headed
    };
    let prompt = blocks(order, text, &capabilities["promptCapabilities"])?;
    let silence = match order.silence_ms {
        0 => Duration::from_secs(shared::SILENCE_MOST_SECS),
        ms => Duration::from_millis(ms),
    };
    let answer = turn.prompt(&session, prompt, silence)?;
    emit(&said(
        "timing",
        json!({
            "launch_to_first_update_ms": turn.record.first_update_ms,
            "resume_ms": turn.record.resume_ms,
        }),
    ))?;
    let stopped = turn.stopping.is_some();
    Ok(match answer {
        Ok(Answer::Result(result)) => {
            emit(&said("prompted", json!({"result": result})))?;
            if stopped {
                Outcome::Stopped
            } else {
                Outcome::Answered
            }
        }
        Ok(Answer::Error(error)) => {
            if shared::classify(&shared::error_text(&error)) == Failure::Signature {
                mark_poisoned(&session);
            }
            failed("prompt", &error)?;
            if stopped {
                Outcome::Stopped
            } else {
                Outcome::Failed
            }
        }
        // A Stop whose cancel was not answered in time: agy is ended below.
        // After a Stop, however agy ends, the turn was stopped.
        Err(_) if stopped => Outcome::Stopped,
        Err(how) => early("prompt", how)?,
    })
}

/// Ends what agy left behind in the driver's process group: a server or a command it started,
/// which would outlive the turn, and, holding the shim's standard error, keep the turn from
/// ending. Only where the driver leads a group of its own, as the shim starts it: never the
/// group of whoever started the driver otherwise. TERM first (the driver holds it), then KILL
/// to what is still there.
fn reap() {
    let group = getpgrp();
    if group != getpid() || others_of(group).is_empty() {
        return;
    }
    if killpg(group, Signal::SIGTERM).is_err() {
        return;
    }
    std::thread::sleep(REAP_GRACE);
    for pid in others_of(group) {
        let _ = kill(pid, Signal::SIGKILL);
    }
}

/// The processes of a group but the driver, as `/proc` lists them.
fn others_of(group: Pid) -> Vec<Pid> {
    let me = getpid();
    let Ok(entries) = std::fs::read_dir("/proc") else {
        return Vec::new();
    };
    entries
        .flatten()
        .filter_map(|entry| entry.file_name().to_str()?.parse::<i32>().ok())
        .map(Pid::from_raw)
        .filter(|pid| *pid != me)
        .filter(|pid| {
            // The fields after the name in parentheses: state, parent, group.
            std::fs::read_to_string(format!("/proc/{pid}/stat"))
                .ok()
                .and_then(|stat| {
                    let after = stat.rsplit_once(')')?.1;
                    after.split_whitespace().nth(2)?.parse::<i32>().ok()
                })
                .is_some_and(|of| of == group.as_raw())
        })
        .collect()
}

pub fn run(program: &str) -> Result<u8> {
    let (inbound, received) = mpsc::channel();
    // Before any thread starts: the threads take the mask, and only the watch takes SIGUSR1.
    stop_watch(inbound.clone())?;
    let order = Order::from_json(&serde_json::from_reader(io::stdin().lock())?);
    let cwd = if order.cwd.trim().is_empty() {
        std::env::current_dir()?
    } else {
        PathBuf::from(&order.cwd)
    };
    let launched = Instant::now();
    let mut child = command(program, &cwd).spawn()?;
    let output = child.stdout.take().ok_or("agy's output is missing")?;
    let input = child.stdin.take().ok_or("agy's input is missing")?;
    client::read_into(output, inbound);
    let mut turn = Turn {
        client: Client::new(input, received),
        record: Record {
            quiet: false,
            launched,
            prompting: false,
            first_update_ms: None,
            resume_ms: None,
            open_tools: BTreeSet::new(),
            stopping: false,
        },
        stopping: None,
    };
    let mut agent = Agent { child };
    let outcome = run_turn(&mut turn, &order, &cwd.display().to_string());
    agent.end(&mut turn.client);
    reap();
    match outcome? {
        Outcome::Answered => Ok(0),
        Outcome::Failed => Ok(1),
        Outcome::Stopped => {
            // A stopped turn ends by signal, as the shim's own stop would end a tool.
            let mut held = SigSet::empty();
            held.add(Signal::SIGTERM);
            let _ = held.thread_unblock();
            let _ = kill(Pid::this(), Signal::SIGTERM);
            loop {
                std::thread::park();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_driver_line_carries_its_own_fields() {
        assert_eq!(
            said("bound", json!({"session": "s"})),
            json!({"clarp": "bound", "session": "s"})
        );
    }
}
