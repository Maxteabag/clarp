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
//! The order is the first line of its standard input. Each line after it is a message the Host
//! sends while the turn runs: it goes to Antigravity as a prompt of its own on the open
//! conversation, and the driver prints when it was read and which messages each answer covers
//! ([`prompts`]; the lines are `clarp_acp::agy::steer`'s).
//!
//! A message that comes while Antigravity writes its reply (no tool running) cuts that reply:
//! at the next line end outside a code block the prompt is cancelled, and the message goes as
//! the next prompt with the reply so far given back before it, since agy forgets the text of a
//! cancelled prompt. The cut is no Stop: the turn goes on. `CLARP_AGY_ACP_CUT=off` (or `0`,
//! `false`, `no`) in the driver's environment, which it inherits from the Host, turns the cut
//! off: the message then goes at once, as during a tool (`clarp_acp::agy::steer::CUT_ENV`).
//!
//! A Stop reaches the driver as SIGUSR1. It sends `session/cancel`, waits a bounded time for
//! Antigravity to answer the prompt, prints what it said, ends agy (its input closed, then
//! TERM, then KILL only if it stays), and ends itself with SIGTERM, so that the turn ends as
//! stopped. Every permission Antigravity asks for is answered at once by Clarp's policy
//! ([`clarp_acp::schema::chosen`]): nobody is waited for.

mod prompts;

use std::collections::BTreeSet;
use std::io::{self, BufRead, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{self, Sender};
use std::time::{Duration, Instant};

use base64::{Engine as _, engine::general_purpose::STANDARD};
use clarp_acp::agy::{self as shared, Failure, Kind, Order, steer};
use clarp_acp::client::{self, Answer, Client, Ended, Handler, Inbound};
use clarp_acp::{code, method, schema};
use nix::sys::signal::{SigSet, Signal, kill};
use nix::unistd::{Pid, getpid};
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
    /// A janitor's question is asked in a sandbox and has no tool permissions.
    read_only: bool,
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
    /// Every tool call Antigravity has begun in the turn: one that goes on or ends after a
    /// message was sent is work begun before it, no sign that the message was taken.
    seen_tools: BTreeSet<String>,
    /// The prompt was cancelled: what Antigravity asks after that is declined.
    stopping: bool,
    /// A message whose read is printed before Antigravity's next update: it is at work on the
    /// prompt that carries it ([`prompts`]).
    read_next: Option<String>,
    /// The message whose read was printed, for the prompts to take up.
    took: Option<String>,
    /// What Antigravity has said in answer since the last answer was settled.
    answer_text: String,
    /// A settled line was printed: the shim has closed the Host's input on it.
    settled: bool,
    /// Antigravity is writing a message: its last word was a piece of one, and no thought or
    /// tool call has ended it since. The Host shows such pieces as one reply.
    writing: bool,
    /// Where the message being written stands: its paragraph breaks and code blocks can begin
    /// in one piece and end in the next.
    written: Written,
    /// A message is held to cut the reply being written at its next line end ([`prompts`]).
    cut: Cut,
    /// The prompts have something of the cut to act on: the wait ends.
    wake: bool,
    /// How much of what agy wrote after the line end a cut was made at was left out.
    dropped: usize,
    /// Where in `answer_text` the prompt agy works on began: what a cut gives back.
    since: usize,
    /// The message went at the bound while its cancelled prompt was not answered yet: what is
    /// left out from then on is kept here. It is the cancelled prompt's if that prompt answers,
    /// and the message's if the message's answer passes over it ([`prompts`]).
    keep_late: bool,
    late: Vec<Value>,
}

/// Where a cut of the reply being written stands.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
enum Cut {
    None,
    /// A message is held: the reply is cut at its next line end outside a code block.
    Line,
    /// The line end came: the prompt is to be cancelled. What agy writes after it is left out.
    Due,
    /// agy stopped writing before a line end came (a thought, a tool): the message goes as a
    /// prompt of its own, as when nothing is written.
    Missed,
    /// The prompt is cancelled and its answer awaited. What agy writes meanwhile is left out.
    Cutting,
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
        // Antigravity is at work on the prompt whose message waits to be read: the read comes
        // before what it does with it. Only work counts: agy also says, on its own, which
        // commands it offers, what the conversation is called and how full it is, before it has
        // touched the message (seen in a stopped turn's spool, 23 Nov 2026).
        //
        // A piece that goes on with the message agy was writing when it took the new prompt is
        // the rest of that message, not work on the new one: the read waits for the message's
        // first paragraph break, so that the reply before the message stays whole (seen live on
        // 23 Nov 2026: "5 plus" | the message | " 4 is 9"). agy 1.5 may answer the message
        // inside the text it goes on writing, with no other sign until the answer; the break
        // is where that answer can begin. A blank line inside a code block is no such break: it
        // would cut the block in two. Where none comes, the read waits for what comes
        // after the message, or for the newer prompt's answer.
        let kind = message["params"]["update"]["sessionUpdate"].as_str();
        let chunk = update && kind == Some("agent_message_chunk");
        let goes_on = self.writing && chunk;
        let mut words = message["params"]["update"]["content"]["text"]
            .as_str()
            .filter(|_| chunk);
        // A cut ends the reply at a line end: what agy writes after it is left out, as agy
        // itself forgets it when the prompt is cancelled. Should agy stop writing before a line
        // end comes, the held message goes as a prompt of its own.
        let mut cut_at = None;
        match self.cut {
            Cut::Due | Cut::Cutting
                if update
                    && matches!(kind, Some("agent_message_chunk" | "agent_thought_chunk")) =>
            {
                let left = &message["params"]["update"]["content"]["text"];
                self.dropped += left.as_str().map_or(0, str::len);
                if self.keep_late {
                    self.late.push(message.clone());
                }
                return Ok(());
            }
            Cut::Line if goes_on => {
                if let Some(whole) = words
                    && let Some(end) = self.written.clone().line_end(whole)
                {
                    cut_at = Some(end);
                    words = Some(&whole[..end]);
                    self.dropped += whole.len() - end;
                    self.cut = Cut::Due;
                    self.wake = true;
                }
            }
            Cut::Line if update && works(message, &self.seen_tools) => {
                self.cut = Cut::Missed;
                self.wake = true;
            }
            _ => {}
        }
        let shortened;
        let message = match (cut_at, words) {
            (Some(_), Some(head)) => {
                shortened = with_words(message, head);
                &shortened
            }
            _ => message,
        };
        if chunk && !goes_on {
            self.written = Written::default();
        }
        let found = words.and_then(|words| self.written.take(words));
        // Where in this update the read goes: before it (0), or after the paragraph break.
        let read_at = if update && self.read_next.is_some() && works(message, &self.seen_tools) {
            if goes_on { found } else { Some(0) }
        } else {
            None
        };
        // What ends a message being written, as the Host reads it: a thought or a tool call.
        match kind {
            Some("agent_message_chunk") if update => self.writing = true,
            Some("agent_thought_chunk" | "tool_call" | "tool_call_update") if update => {
                self.writing = false;
            }
            _ => {}
        }
        if let Some(id) = message["params"]["update"]["toolCallId"]
            .as_str()
            .filter(|id| !id.is_empty())
        {
            self.seen_tools.insert(id.to_string());
        }
        match schema::SessionUpdate::of_notification(message) {
            Some(schema::SessionUpdate::AgentMessageChunk { content }) => {
                self.answer_text
                    .push_str(words.unwrap_or_else(|| content.text()));
            }
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
        // One line of the record each, short enough that the shim never splits it. A piece
        // with a paragraph break where the read goes is printed in two, the read between.
        let Some(at) = read_at else {
            return emit(&schema::bounded(message));
        };
        let words = words.unwrap_or_default();
        if at > 0 {
            emit(&schema::bounded(&with_words(message, &words[..at])))?;
        }
        if let Some(uuid) = self.read_next.take() {
            read_out(&uuid)?;
            self.took = Some(uuid);
        }
        if at == 0 {
            emit(&schema::bounded(message))
        } else if at < words.len() {
            emit(&schema::bounded(&with_words(message, &words[at..])))
        } else {
            Ok(())
        }
    }

    fn wakes(&mut self) -> bool {
        std::mem::take(&mut self.wake)
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
        let chosen = if self.stopping || self.read_only {
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
    /// Lines of the Host's that came before the turn's own prompt was sent.
    waiting: Vec<Value>,
    /// The Host has closed the driver's input: no more messages come.
    owner_done: bool,
}

/// How the turn went, in the driver's exit: nought for an answered prompt.
enum Outcome {
    Answered,
    /// In the words the failure was printed with: the settled line of a turn that failed
    /// before its answer says them again.
    Failed(String),
    Stopped,
}

impl Turn {
    /// A request answered within the setup bound. A Stop before the prompt ends the turn; a
    /// message the Host sends meanwhile waits for the prompt.
    fn setup(&mut self, asked: &str, params: Value) -> Result<std::result::Result<Answer, Ended>> {
        let id = self.client.send_request(asked, params)?;
        loop {
            match self.client.wait(id, SETUP_WAIT, None, &mut self.record)? {
                Err(Ended::Owner(Some(line))) => self.waiting.push(line),
                Err(Ended::Owner(None)) => self.owner_done = true,
                other => return Ok(other),
            }
        }
    }

    /// A Stop: the prompt that runs is cancelled, and the wait for its answer bounded. After
    /// it, what Antigravity asks is declined and no message is read.
    fn stop(&mut self, session: &str) -> Result<()> {
        self.record.stopping = true;
        self.record.read_next = None;
        self.record.cut = Cut::None;
        if self.stopping.is_none() {
            self.stopping = Some(Instant::now() + Duration::from_millis(shared::CANCEL_WAIT_MS));
            let _ = self
                .client
                .notify(method::SESSION_CANCEL, json!({"sessionId": session}));
            emit(&said("cancel", json!({"session": session})))?;
        }
        Ok(())
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

fn command(program: &str, cwd: &Path, read_only: bool) -> Command {
    let mut command = Command::new(program);
    command
        .arg("acp")
        .current_dir(cwd)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit());
    if read_only {
        command.args(["--sandbox", "--include-default-customizations=false"]);
    } else {
        command.arg("--dangerously-skip-permissions");
    }
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
            return Ok(Err(failed("config", &json!({"message": why}))?));
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
                return Ok(Err(failed("config", &json!({"message": why}))?));
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

/// The conversation the turn is bound to, with the name of the turn's own message where the
/// order gives one (an older Host's order gives none).
fn bound(session: &str, resumed: bool, uuid: &str) -> Value {
    let mut line = said("bound", json!({"session": session, "resumed": resumed}));
    if !uuid.trim().is_empty() {
        line["uuid"] = json!(uuid.trim());
    }
    line
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

/// Prints a failure the way the interpreter reads it. A failure of a prompt names the message
/// whose prompt it was (`uuid`); one before the prompt names none: it is the turn's own.
fn failed(stage: &str, error: &Value) -> io::Result<Outcome> {
    failed_for(stage, error, "")
}

fn failed_for(stage: &str, error: &Value, uuid: &str) -> io::Result<Outcome> {
    let text = shared::error_text(error);
    let mut line = said(
        "failed",
        json!({"stage": stage, "error": error, "text": text}),
    );
    if !uuid.is_empty() {
        line["uuid"] = json!(uuid);
    }
    emit(&line)?;
    Ok(Outcome::Failed(text))
}

/// Whether an update is Antigravity at work on its prompt: what it writes, thinks or plans, a
/// tool call it begins, or its own echo of the message. Not what it says by itself of the
/// conversation (its commands, its title, how full it is), and not a tool call begun before:
/// a command started earlier that ends now (a background build) says nothing of the message.
fn works(message: &Value, seen_tools: &BTreeSet<String>) -> bool {
    let update = &message["params"]["update"];
    match update["sessionUpdate"].as_str() {
        Some("agent_message_chunk" | "agent_thought_chunk" | "plan" | "user_message_chunk") => true,
        Some("tool_call" | "tool_call_update") => update["toolCallId"]
            .as_str()
            .is_some_and(|id| !id.is_empty() && !seen_tools.contains(id)),
        _ => false,
    }
}

/// What the driver knows of the message being written, for where a paragraph break in it ends.
#[derive(Clone, Default)]
struct Written {
    /// How many line breaks the message ends with, at most two.
    breaks: usize,
    /// Inside a code block, by the fence that opened it: three backticks or three tildes first
    /// on a line, after spaces, until a line of the same kind closes it. A blank line in it is
    /// no paragraph break, and no line end in it is a place to cut.
    fence: Option<char>,
    /// The fence characters first on the line being written, after spaces, and their kind.
    ticks: u8,
    line_char: Option<char>,
    /// The line being written is known to be a fence line or not.
    line_known: bool,
}

impl Written {
    /// Takes in a piece of the message: where in it the first paragraph break outside a code
    /// block ends, just after its last line break so that the rest begins with words. None
    /// when the piece holds no such break.
    fn take(&mut self, words: &str) -> Option<usize> {
        let mut found = None;
        for (at, c) in words.char_indices() {
            if found.is_none()
                && !matches!(c, '\n' | '\r')
                && self.breaks >= 2
                && self.fence.is_none()
            {
                found = Some(at);
            }
            self.step(c);
        }
        if found.is_none() && self.breaks >= 2 && self.fence.is_none() {
            found = Some(words.len());
        }
        found
    }

    /// Takes in a piece of the message up to its first line end outside a code block: where
    /// that line end ends. None when the piece holds no such line end.
    fn line_end(&mut self, words: &str) -> Option<usize> {
        for (at, c) in words.char_indices() {
            let ends = c == '\n' && self.fence.is_none();
            self.step(c);
            if ends {
                return Some(at + 1);
            }
        }
        None
    }

    /// The message stands at the start of a line outside a code block: a cut can come now.
    fn at_line_start(&self) -> bool {
        self.breaks > 0 && self.fence.is_none()
    }

    fn step(&mut self, c: char) {
        match c {
            '\n' => {
                self.breaks = (self.breaks + 1).min(2);
                self.ticks = 0;
                self.line_char = None;
                self.line_known = false;
            }
            '\r' => {}
            _ => {
                self.breaks = 0;
                self.first_on_line(c);
            }
        }
    }

    /// A character of the line being written: the third backtick (or tilde) first on it opens
    /// a code block, or closes one that a fence of its kind opened.
    fn first_on_line(&mut self, c: char) {
        if self.line_known || (c == ' ' && self.ticks == 0) {
            return;
        }
        if !matches!(c, '`' | '~') || self.line_char.is_some_and(|kind| kind != c) {
            self.line_known = true;
            return;
        }
        self.line_char = Some(c);
        self.ticks += 1;
        if self.ticks == 3 {
            self.fence = match self.fence {
                None => Some(c),
                Some(open) if open == c => None,
                other => other,
            };
            self.line_known = true;
        }
    }
}

/// A piece of a message with other words: a part of it, printed on its own.
fn with_words(message: &Value, words: &str) -> Value {
    let mut part = message.clone();
    part["params"]["update"]["content"]["text"] = json!(words);
    part
}

/// The read of a message: the one place it is printed. When it is printed is the prompts'
/// rule ([`prompts`]): once Antigravity has taken the message, before anything it says of it.
fn read_out(uuid: &str) -> io::Result<()> {
    emit(&steer::read_line(uuid))
}

/// The turn ends before its answer: why, as the interpreter reads it.
fn early(stage: &str, how: Ended) -> io::Result<Outcome> {
    early_for(stage, how, "")
}

fn early_for(stage: &str, how: Ended, uuid: &str) -> io::Result<Outcome> {
    let text = match how {
        // The Host's lines never end a wait of the setup's, which keeps them for the prompt.
        Ended::Closed | Ended::Owner(_) | Ended::Woken => "Antigravity ended before it answered",
        Ended::Silent => "Antigravity stopped answering",
        Ended::Interrupted => return Ok(Outcome::Stopped),
    };
    let mut line = said(
        "failed",
        json!({"stage": stage, "text": text, "silent": how == Ended::Silent}),
    );
    if !uuid.is_empty() {
        line["uuid"] = json!(uuid);
    }
    emit(&line)?;
    Ok(Outcome::Failed(text.to_string()))
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
                emit(&bound(named, true, &order.uuid))?;
                return Ok(Ok((named.to_string(), opened)));
            }
            // A conversation agy no longer has is not begun anew here, empty: it is marked,
            // and the turn fails, so that the Host continues it with its own record of it.
            Ok(Answer::Error(error)) if gone(&error) => {
                mark_poisoned(named);
                let said = shared::error_text(&error);
                return Ok(Err(failed(
                    "session",
                    &json!({"code": code::NOT_FOUND, "message": format!("Session not found: {said}")}),
                )?));
            }
            Ok(Answer::Error(error)) => {
                return Ok(Err(failed(
                    if error["code"] == code::AUTH_REQUIRED {
                        "auth"
                    } else {
                        "session"
                    },
                    &error,
                )?));
            }
            Err(how) => return Ok(Err(early("session", how)?)),
        }
    }
    match turn.setup(method::SESSION_NEW, params)? {
        Ok(Answer::Result(opened)) => {
            match opened["sessionId"].as_str().filter(|id| !id.is_empty()) {
                Some(id) => {
                    emit(&bound(id, false, &order.uuid))?;
                    Ok(Ok((id.to_string(), opened)))
                }
                None => Ok(Err(failed(
                    "session",
                    &json!({"message": "Antigravity opened no conversation"}),
                )?)),
            }
        }
        Ok(Answer::Error(error)) => Ok(Err(failed(
            if error["code"] == code::AUTH_REQUIRED {
                "auth"
            } else {
                "session"
            },
            &error,
        )?)),
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
            return Ok(failed("initialize", &error)?);
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
    let outcome = prompts::converse(turn, &session, order.uuid.trim(), prompt, silence)?;
    emit(&said(
        "timing",
        json!({
            "launch_to_first_update_ms": turn.record.first_update_ms,
            "resume_ms": turn.record.resume_ms,
        }),
    ))?;
    Ok(outcome)
}

/// Ends what agy left behind that would keep the turn from ending: a process that still holds
/// the driver's standard output or error (the shim's pipes), which the shim waits for to close.
/// Only those: a process an agent started on purpose to outlive the turn (`nohup … > log &`, a
/// dev server, a watcher) writes elsewhere and is left running, as with the `agy` card. TERM
/// first, then KILL to whatever still holds them after a grace.
fn reap() {
    let ours = own_pipes();
    if ours.is_empty() || holders(&ours).is_empty() {
        return;
    }
    for pid in holders(&ours) {
        let _ = kill(pid, Signal::SIGTERM);
    }
    std::thread::sleep(REAP_GRACE);
    for pid in holders(&ours) {
        let _ = kill(pid, Signal::SIGKILL);
    }
}

/// What the driver's standard output and error are, as `/proc` names them (`pipe:[…]`): only
/// pipes, which are the shim's, never a terminal or a file somebody else may share.
fn own_pipes() -> Vec<std::path::PathBuf> {
    [1, 2]
        .into_iter()
        .filter_map(|fd| std::fs::read_link(format!("/proc/self/fd/{fd}")).ok())
        .filter(|link| link.to_str().is_some_and(|link| link.starts_with("pipe:")))
        .collect()
}

/// The processes but the driver whose standard output or error is one of `ours`.
fn holders(ours: &[std::path::PathBuf]) -> Vec<Pid> {
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
            [1, 2].into_iter().any(|fd| {
                std::fs::read_link(format!("/proc/{pid}/fd/{fd}"))
                    .is_ok_and(|link| ours.contains(&link))
            })
        })
        .collect()
}

pub fn run(program: &str, read_only: bool) -> Result<u8> {
    let (inbound, received) = mpsc::channel();
    // Before any thread starts: the threads take the mask, and only the watch takes SIGUSR1.
    stop_watch(inbound.clone())?;
    // The order is the first line. Each line after it is a message of the Host's for the turn,
    // read on a thread of its own for as long as the Host writes: the shim closes the input on
    // the first settled line, or right after the order where the turn is not steered. Nothing
    // ever waits for its end.
    let mut from_host = io::BufReader::new(io::stdin());
    let mut first = String::new();
    from_host.read_line(&mut first)?;
    let order = Order::from_json(&serde_json::from_str(&first)?);
    let owner = inbound.clone();
    let cwd = if order.cwd.trim().is_empty() {
        std::env::current_dir()?
    } else {
        PathBuf::from(&order.cwd)
    };
    let launched = Instant::now();
    let mut child = command(program, &cwd, read_only).spawn()?;
    let output = child.stdout.take().ok_or("agy's output is missing")?;
    let input = child.stdin.take().ok_or("agy's input is missing")?;
    client::read_into(output, inbound);
    std::thread::spawn(move || {
        for line in from_host.lines() {
            let Ok(line) = line else { break };
            if let Ok(said) = serde_json::from_str::<Value>(&line)
                && said.is_object()
                && owner.send(Inbound::Owner(Some(said))).is_err()
            {
                return;
            }
        }
        let _ = owner.send(Inbound::Owner(None));
    });
    let mut turn = Turn {
        client: Client::new(input, received),
        record: Record {
            read_only,
            quiet: false,
            launched,
            prompting: false,
            first_update_ms: None,
            resume_ms: None,
            open_tools: BTreeSet::new(),
            seen_tools: BTreeSet::new(),
            stopping: false,
            read_next: None,
            took: None,
            answer_text: String::new(),
            settled: false,
            writing: false,
            written: Written::default(),
            cut: Cut::None,
            wake: false,
            dropped: 0,
            since: 0,
            keep_late: false,
            late: Vec::new(),
        },
        stopping: None,
        waiting: Vec::new(),
        owner_done: false,
    };
    let mut agent = Agent { child };
    let outcome = run_turn(&mut turn, &order, &cwd.display().to_string());
    // A turn that failed before any answer was settled says so in a settled line naming its
    // own message: the Host reads its end from it, and the shim closes the input on it.
    if !matches!(outcome, Ok(Outcome::Answered | Outcome::Stopped)) && !turn.record.settled {
        let own: Vec<String> = Some(order.uuid.trim().to_string())
            .filter(|uuid| !uuid.is_empty())
            .into_iter()
            .collect();
        // In the failure's own words: an unoffered model, a signed-out agy, a lost conversation.
        let why = match &outcome {
            Err(error) => error.to_string(),
            Ok(Outcome::Failed(text)) if !text.trim().is_empty() => text.clone(),
            Ok(_) => "Antigravity's turn failed before it answered".to_string(),
        };
        let _ = emit(&steer::settled_line(true, &why, &own));
    }
    agent.end(&mut turn.client);
    reap();
    match outcome? {
        Outcome::Answered => Ok(0),
        Outcome::Failed(_) => Ok(1),
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
    fn only_work_on_the_prompt_takes_a_message() {
        let update = |kind: &str, more: Value| {
            let mut update = json!({"sessionUpdate": kind});
            if let (Some(update), Value::Object(more)) = (update.as_object_mut(), more) {
                update.extend(more);
            }
            json!({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "s", "update": update}})
        };
        let seen: BTreeSet<String> = ["t-old".to_string()].into();
        for working in [
            update("agent_message_chunk", json!({})),
            update("agent_thought_chunk", json!({})),
            update("plan", json!({})),
            update("user_message_chunk", json!({})),
            update("tool_call", json!({"toolCallId": "t-new"})),
            update("tool_call_update", json!({"toolCallId": "t-new"})),
        ] {
            assert!(works(&working, &seen), "{working}");
        }
        for by_itself in [
            update("usage_update", json!({"used": 0})),
            update("session_info_update", json!({})),
            update("available_commands_update", json!({})),
            update("config_option_update", json!({})),
            update(
                "tool_call_update",
                json!({"toolCallId": "t-old", "status": "completed"}),
            ),
            update("tool_call", json!({})),
        ] {
            assert!(!works(&by_itself, &seen), "{by_itself}");
        }
    }

    /// Where the first paragraph break ends in the last piece, for a message written in these.
    fn first_break(pieces: &[&str]) -> Option<usize> {
        let mut written = Written::default();
        let mut last = None;
        for piece in pieces {
            last = written.take(piece);
        }
        last
    }

    #[test]
    fn the_read_goes_after_a_paragraph_break_never_inside_a_sentence() {
        assert_eq!(first_break(&[" 4 is 9.\n\n7 times"]), Some(10));
        assert_eq!(first_break(&["a\n\n\nb"]), Some(4));
        assert_eq!(first_break(&["a\r\n\r\nb"]), Some(5));
        assert_eq!(first_break(&["done.\n\n"]), Some(7));
        // Across two pieces: the first half ended the one before.
        assert_eq!(first_break(&["9.\n", "\n7 times"]), Some(1));
        assert_eq!(first_break(&["9.\n\n", "7 times"]), Some(0));
        for none in ["5 plus", " 4 is 9", "one\ntwo", "\nthree\n", ""] {
            assert_eq!(first_break(&[none]), None, "{none:?}");
        }
        assert_eq!(first_break(&["a\n", "x\n"]), None);
    }

    #[test]
    fn a_blank_line_in_a_code_block_is_no_paragraph_break() {
        // The blank line in the block is passed over; the one after it closes is the break.
        assert_eq!(
            first_break(&["Here:\n```rust\nlet a = 1;\n\nlet b = 2;\n```\n\nDone"]),
            Some(42)
        );
        // The block opened in a piece before, with spaces before the fence; split fences.
        assert_eq!(first_break(&["  ``", "`\nx\n", "\ny"]), None);
        assert_eq!(first_break(&["```\nx\n", "\ny\n  ```\n", "\nz"]), Some(1));
        // A block that never closes holds no break.
        assert_eq!(first_break(&["```\nx\n\ny\n\nz"]), None);
        // Backticks inside a line are no fence; a break before a block is one.
        assert_eq!(first_break(&["say ```x``` then\n\nmore"]), Some(18));
        assert_eq!(first_break(&["text\n\n```\ncode"]), Some(6));
        // A tilde fence is a fence; a fence of the other kind inside it is code.
        assert_eq!(first_break(&["~~~\nx\n\n```\n\ny\n~~~\n\nz"]), Some(19));
    }

    #[test]
    fn a_cut_finds_a_line_end_outside_any_code_block() {
        let mut written = Written::default();
        assert_eq!(written.line_end("1. Pho\n2. Ra"), Some(7));
        let mut written = Written::default();
        assert_eq!(written.line_end("Run:"), None);
        assert!(!written.at_line_start());
        assert_eq!(written.line_end("\n"), Some(1));
        assert!(written.at_line_start());
        // Inside a block, by either fence, no line is a place to cut; the closing line is.
        let mut written = Written::default();
        assert_eq!(written.line_end("```sh\necho a\n~~~\necho b\n``"), None);
        assert!(!written.at_line_start());
        assert_eq!(written.line_end("`\nDone."), Some(2));
        let mut written = Written::default();
        assert_eq!(written.take("~~~\nx\n"), None);
        assert!(!written.at_line_start());
    }

    #[test]
    fn a_driver_line_carries_its_own_fields() {
        assert_eq!(
            said("bound", json!({"session": "s"})),
            json!({"clarp": "bound", "session": "s"})
        );
    }
}
