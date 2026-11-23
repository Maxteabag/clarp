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

use std::io::{self, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Sender};
use std::time::{Duration, Instant};

use base64::{Engine as _, engine::general_purpose::STANDARD};
use clarp_acp::agy::{self as shared, Failure, Kind, Order};
use clarp_acp::client::{self, Answer, Client, Ended, Handler, Inbound};
use clarp_acp::{code, method, schema};
use nix::sys::signal::{SigSet, Signal, kill};
use nix::unistd::Pid;
use serde_json::{Value, json};

type Error = Box<dyn std::error::Error + Send + Sync>;
type Result<T> = std::result::Result<T, Error>;

/// How long Antigravity may take over a request before the prompt: it unpacks itself and
/// signs in when it starts.
const SETUP_WAIT: Duration = Duration::from_secs(5 * 60);
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
        emit(message)
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
        let chosen = schema::chosen(&options);
        emit(&said(
            "permission",
            json!({
                "title": params["toolCall"]["title"],
                "toolCallId": params["toolCall"]["toolCallId"],
                "options": options.iter().map(|option| option.name.clone()).collect::<Vec<_>>(),
                "kind": chosen.map(|option| option.kind.clone()),
                "answer": chosen.map(|option| option.name.clone()),
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
    ) -> Result<std::result::Result<Answer, Ended>> {
        let id = self.client.send_request(
            method::SESSION_PROMPT,
            json!({"sessionId": session, "prompt": blocks}),
        )?;
        self.record.prompting = true;
        let silence = Duration::from_secs(shared::SILENCE_MOST_SECS);
        loop {
            match self
                .client
                .wait(id, silence, self.stopping, &mut self.record)?
            {
                Err(Ended::Interrupted) => {
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

fn notice(text: String) -> io::Result<()> {
    emit(&said("notice", json!({"code": "agy-model", "text": text})))
}

/// Chooses the model and the effort, where the order names them and Antigravity offers
/// them. What is not offered is said, and the turn goes on with what Antigravity chose.
fn configure(
    turn: &mut Turn,
    session: &str,
    order: &Order,
    opened: &Value,
) -> Result<std::result::Result<(), Ended>> {
    let mut options = opened
        .get("configOptions")
        .filter(|options| options.is_array())
        .cloned()
        .unwrap_or(Value::Null);
    let models = &opened["models"];
    let mut set_model = None;
    for (category, wanted) in [
        ("model", order.model.trim()),
        ("thought_level", order.effort.trim()),
    ] {
        if wanted.is_empty() {
            continue;
        }
        if let Some(found) = schema::option(&options, category).cloned() {
            let Some(value) = schema::matching(&found, wanted) else {
                let names: Vec<String> = schema::offered(&found)
                    .into_iter()
                    .map(|(value, _)| value)
                    .collect();
                notice(format!(
                    "Antigravity does not offer {wanted} as its {category} (it offers {}); it runs on its own choice.",
                    names.join(", ")
                ))?;
                continue;
            };
            if found["currentValue"] == json!(value) {
                continue;
            }
            let config = found["id"].as_str().unwrap_or(category).to_string();
            let params = json!({"sessionId": session, "configId": config, "value": value});
            match turn.setup(method::SESSION_SET_CONFIG_OPTION, params)? {
                Err(ended) => return Ok(Err(ended)),
                Ok(Answer::Error(error)) => notice(format!(
                    "Antigravity did not take {value} as its {category}: {}",
                    shared::error_text(&error)
                ))?,
                Ok(Answer::Result(answer)) => {
                    match answer.get("configOptions").filter(|all| all.is_array()) {
                        Some(changed) => options = changed.clone(),
                        None => {
                            if let Some(entry) = options.as_array_mut().and_then(|all| {
                                all.iter_mut().find(|option| option["id"] == json!(config))
                            }) {
                                entry["currentValue"] = json!(value);
                            }
                        }
                    }
                }
            }
            continue;
        }
        // An agy without options says its models apart (`models`), and takes one by its id.
        let listed = models["availableModels"]
            .as_array()
            .filter(|_| category == "model");
        let Some(listed) = listed else {
            notice(format!(
                "Antigravity offers no choice of {category}; {wanted} was not applied."
            ))?;
            continue;
        };
        let known = listed.iter().find_map(|model| {
            let id = model["modelId"].as_str()?;
            let name = model["name"].as_str().unwrap_or(id);
            (id.eq_ignore_ascii_case(wanted) || name.eq_ignore_ascii_case(wanted))
                .then(|| id.to_string())
        });
        match known {
            None => notice(format!(
                "Antigravity does not offer the model {wanted} here; it runs on its own choice."
            ))?,
            Some(id) if models["currentModelId"] == json!(id) => {}
            Some(id) => match turn.setup(
                method::SESSION_SET_MODEL,
                json!({"sessionId": session, "modelId": id}),
            )? {
                Err(ended) => return Ok(Err(ended)),
                Ok(Answer::Error(error)) => notice(format!(
                    "Antigravity did not take the model {wanted}: {}",
                    shared::error_text(&error)
                ))?,
                Ok(Answer::Result(_)) => set_model = Some(id),
            },
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
    let mut lost = None;
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
            Ok(Answer::Error(error)) if gone(&error) => lost = Some(named),
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
                    emit(&said(
                        "bound",
                        json!({"session": id, "resumed": false, "lost": lost}),
                    ))?;
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
        "clientInfo": {"name": "clarp", "title": "Clarp", "version": env!("CARGO_PKG_VERSION")},
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
    if let Err(how) = configure(turn, &session, order, &opened)? {
        return Ok(early("config", how)?);
    }
    let text = if meta.is_some() || order.headed.is_empty() {
        &order.prompt
    } else {
        &order.headed
    };
    let prompt = blocks(order, text, &capabilities["promptCapabilities"])?;
    let answer = turn.prompt(&session, prompt)?;
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
        Err(Ended::Silent) if stopped => Outcome::Stopped,
        Err(how) => early("prompt", how)?,
    })
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
        },
        stopping: None,
    };
    let mut agent = Agent { child };
    let outcome = run_turn(&mut turn, &order, &cwd.display().to_string());
    agent.end(&mut turn.client);
    match outcome? {
        Outcome::Answered => Ok(0),
        Outcome::Failed => Ok(1),
        Outcome::Stopped => {
            // A stopped turn ends by signal, as the shim's own stop would end a tool.
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
