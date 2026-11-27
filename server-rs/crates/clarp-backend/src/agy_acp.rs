//! Antigravity over the Agent Client Protocol: `agy acp`, driven for one turn by the shim's
//! driver (`clarp-shim agy-acp-turn`, in `clarp-shim/src/agy_acp.rs`).
//!
//! The same program, login and conversations as the `agy` card: an ACP conversation's id is
//! the name of its conversation in Antigravity's own store. What this card adds is the way it
//! is spoken to: the message goes on standard input with no limit of a command line, pictures
//! and sounds go as themselves, the Host's head goes into Antigravity's instructions where it
//! takes an addition to them, and a Stop cancels the prompt in the protocol's own way.
//!
//! The driver prints, one per line, every message Antigravity sends during the turn, as it
//! was sent, and its own lines (`{"clarp": …}`) for what it did: the conversation it opened
//! (`bound`), the model (`model`), a word for the user (`notice`), a permission it answered
//! (`permission`), the answer to the prompt (`prompted`), what failed (`failed`), a Stop it
//! passed on (`cancel`). The interpreter here reads those lines and nothing else:
//!
//! ```text
//! {"clarp":"initialized","agent":{"name":"agy","version":"1.5.15"},"prompt":{"image":true},"appends":true}
//! {"clarp":"bound","session":"…","resumed":true,"uuid":"…"}
//! {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"…"}}}}
//! {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"tool_call","toolCallId":"…","title":"…","kind":"execute","status":"pending","rawInput":{"CommandLine":"ls"}}}}
//! {"jsonrpc":"2.0","method":"session/update","params":{"sessionId":"…","update":{"sessionUpdate":"tool_call_update","toolCallId":"…","status":"completed","rawOutput":{"combinedOutput":"…"}}}}
//! {"clarp":"prompted","uuid":"…","result":{"stopReason":"end_turn","usage":{"inputTokens":10,"outputTokens":5}}}
//! {"type":"result","subtype":"success","is_error":false,"result":"…","user_message_uuids":["…"]}
//! ```
//!
//! A message the user sends while the turn runs reaches it through the driver's input
//! (`Backend::steers`). The driver says when agy took one (`{"type":"user","isReplay":true,
//! "uuid":…}`), before anything agy says of it, and follows each answer with a result that
//! names the messages it answers: the steering evidence the actor reads
//! (`clarp_kernel::steering`), as it reads Claude Code's. Each answer line names the message
//! it answers (`uuid`; the turn's own is named on `bound`): the turn's outcome is its own
//! message's answer, and its output goes on until every message agy took is answered.

use std::collections::BTreeMap;
use std::path::Path;

use clarp_acp::agy::{self as shared, Order};
use clarp_acp::schema::{self, PromptResponse, SessionUpdate, StopReason};
use clarp_kernel::conversation::{Notice, Reply, ToolCall, ToolStatus};
use clarp_kernel::live::Glimpse;
use clarp_kernel::takeover::{self, Input};
use clarp_kernel::told;
use clarp_kernel::turn::{Activity, Origin, RunOutcome};
use clarp_spool::{Entry, Record, ShimEvent};
use serde_json::{Value, json};

use crate::agy::{self, Agy};
use crate::{
    Apart, Backend, ErrorTail, Interpreter, Launch, LiveReply, OwnHome, Question, Reading,
    SESSION_ENV, Signal, TurnSpec, shorten,
};

use clarp_child::MANAGED_AGY_ENV as MANAGED;

/// The card's name.
pub const ID: &str = "agy-acp";
/// What the ids of this card's live items begin with. The turn follows it, since agy's own
/// numbering of what it says starts again each turn ([`AcpInterpreter::shown`]).
const SHOWN: &str = "aa";
/// How long the driver has, after a Stop, to cancel the prompt and end agy before the shim
/// ends the whole group: the cancel wait and agy's two chances to leave, with room.
const STOP_GRACE_MS: u64 = 20_000;

pub struct AgyAcp {
    /// The `agy` card: what this one shares with it (its program, its questions asked once).
    agy: Agy,
    program: String,
}

impl AgyAcp {
    pub fn new() -> Self {
        Self::with_program("agy")
    }

    pub fn with_program(program: impl Into<String>) -> Self {
        let program = program.into();
        Self {
            agy: Agy::with_program(program.clone()),
            program,
        }
    }
}

impl Default for AgyAcp {
    fn default() -> Self {
        Self::new()
    }
}

/// The files a message names that may go as more than their path: absolute paths of files
/// a user attached (they are kept under an `uploads` directory), of a kind that goes as a
/// picture, a sound or a link. Each once, in the order named, and no more than the driver
/// takes.
pub fn attached(prompt: &str) -> Vec<String> {
    let mut found: Vec<String> = Vec::new();
    let parts = prompt.split(|c: char| {
        c.is_whitespace()
            || matches!(
                c,
                '"' | '\'' | '`' | '<' | '>' | '(' | ')' | '[' | ']' | '{' | '}' | ','
            )
    });
    for part in parts {
        let path = part.trim_end_matches(['.', ';', ':', '!', '?']);
        let named = path.starts_with('/')
            && path.contains("/uploads/")
            && !path.contains("/../")
            && shared::kind_of(Path::new(path)).is_some();
        if named && !found.iter().any(|seen| seen == path) {
            found.push(path.to_string());
            if found.len() == shared::ATTACHED_COUNT_MOST {
                break;
            }
        }
    }
    found
}

/// The order of one turn: the message as it is, and with the Host's head before it, for an
/// Antigravity that takes no addition to its instructions.
pub fn order(spec: &TurnSpec) -> Order {
    let given = |value: &Option<String>| {
        value
            .as_deref()
            .map(str::trim)
            .filter(|value| !value.is_empty())
            .unwrap_or_default()
            .to_string()
    };
    let prompt = match &spec.takeover {
        Some(Input::Agy { first }) => format!("{first}\n\n{}", spec.prompt),
        _ => spec.prompt.clone(),
    };
    let (headed, append) = match &spec.told {
        Some(told) => {
            let head = told.at_head(&spec.session);
            (told::headed(&head, None, &prompt), head)
        }
        None => (prompt.clone(), String::new()),
    };
    Order {
        conversation: false,
        session: given(&spec.backend_session),
        cwd: spec.cwd.display().to_string(),
        prompt,
        headed,
        append,
        model: given(&spec.model),
        effort: given(&spec.effort).to_lowercase(),
        // Only the new message's files: a record carried before it names old ones.
        attachments: attached(&told::without_restored(&spec.prompt)),
        silence_ms: 0,
        uuid: takeover::attempt_prompt_uuid(
            &spec.agent_id,
            spec.turn,
            spec.trace_id.as_deref(),
            spec.attempt,
        ),
    }
}

impl AgyAcp {
    fn launch_order(&self, session: &str, order: &Order) -> Launch {
        Launch {
            prompt_as_file: false,
            before_stop: Some(("SIGUSR1", STOP_GRACE_MS)),
            program: "clarp-shim".to_string(),
            args: vec![shared::DRIVER.to_string(), self.program.clone()],
            env: vec![
                (SESSION_ENV.to_string(), session.to_owned()),
                (MANAGED.to_string(), "1".to_string()),
            ],
            // A steered turn keeps input open for the lines that follow this order.
            stdin: format!("{}\n", order.to_json()).into_bytes(),
        }
    }
}

impl Backend for AgyAcp {
    fn id(&self) -> &'static str {
        ID
    }

    /// The `agy` card's binary, login and conversations.
    fn runs_through(&self) -> Option<&'static str> {
        Some("agy")
    }

    fn efforts(&self) -> &'static [&'static str] {
        self.agy.efforts()
    }

    /// The driver gives agy a message the Host writes while the turn runs, says when agy took
    /// it (before anything agy says of it), and names it in the result that answers it. The
    /// turn's output alone tells what was read: a Host that restarts reads it in the spool, and
    /// a message the driver never said it took (it refused it, or died before) runs as a turn
    /// of its own (`Backend::records_reads` is false).
    fn steers(&self) -> crate::Steers {
        crate::Steers::Stdin
    }

    /// Only words: the driver gives a steered message as text, and the files a message names
    /// go to agy as themselves only with a prompt of its own. A command to agy (`/compact`)
    /// is no words to add to the work: it waits for a turn of its own too.
    fn steers_message(&self, text: &str) -> bool {
        let text = text.trim();
        !text.is_empty()
            && !text.starts_with('/')
            && attached(&told::without_restored(text)).is_empty()
    }

    fn launch(&self, spec: &TurnSpec) -> Launch {
        self.launch_order(&spec.session, &order(spec))
    }

    fn conversation_launch(&self, spec: &TurnSpec) -> Option<Launch> {
        let mut order = order(spec);
        order.conversation = true;
        order.session.clear();
        order.attachments.clear();
        Some(self.launch_order(&spec.session, &order))
    }

    fn interpreter(&self, turn: u64, name: &str) -> Box<dyn Interpreter> {
        Box::new(AcpInterpreter::new(turn, name))
    }

    /// A conversation is kept while Antigravity has it and it has not been marked as one
    /// that cannot be continued (a thinking block without a valid signature in it).
    fn keeps(&self, session: &str) -> bool {
        let poisoned = shared::poisoned_dir()
            .and_then(|dir| shared::poisoned_mark(&dir, session))
            .is_some_and(|mark| mark.exists());
        !poisoned && crate::past::agy_kept(&shared::here(), session)
    }

    fn once(&self, question: &Question) -> Option<Launch> {
        self.agy.once(question)
    }

    fn answered(&self, printed: &str) -> Result<String, String> {
        self.agy.answered(printed)
    }

    fn reads(&self, reading: &Reading) -> Option<Apart> {
        self.agy.reads(reading)
    }

    fn own_home(&self) -> Option<OwnHome> {
        self.agy.own_home()
    }
}

fn text_of(value: &Value) -> &str {
    value.as_str().unwrap_or("")
}

/// Which message of the turn an answer answers.
enum Answers {
    /// The turn's own.
    Own,
    /// One agy took while the turn ran, by its name.
    Steered(String),
    /// One agy never took.
    Neither,
}

/// How a turn ended, as far as the lines tell it.
#[derive(Debug, Clone, PartialEq)]
enum Told {
    /// Antigravity answered the prompt, with this reason for stopping.
    Answered(StopReason),
    /// Something failed, in these words, at this stage.
    Failed { stage: String, text: String },
}

pub struct AcpInterpreter {
    turn: u64,
    name: String,
    bound: bool,
    /// The message being written, by its number in the turn.
    message: Option<(u64, String)>,
    /// The thought being written, by its number in the turn.
    thought: Option<(u64, String)>,
    pieces: u64,
    tools: BTreeMap<String, ToolCall>,
    activity: Option<Activity>,
    notices: u64,
    model: Option<String>,
    /// How the answer to the turn's own message went: what the turn's outcome is. A message
    /// the user sent while it ran is settled by the result that names it, which is the
    /// actor's to read (`clarp_kernel::steering`).
    told: Option<Told>,
    /// The name of the turn's own message, where the driver says it (`bound`), and whether
    /// it is answered.
    primary: Option<String>,
    answered: bool,
    /// The messages the driver says agy took while the turn ran, by their names, in order,
    /// and those of them that are answered. The turn's output goes on until each is.
    steered: Vec<String>,
    settled: std::collections::BTreeSet<String>,
    /// How the newest answer line went, whichever message it named: a result that settles
    /// the turn's own message with it (an answer that carried the work on for both) says
    /// the turn went so.
    last_answer: Option<Told>,
    /// What the answers so far took in and gave out, together.
    tokens: Option<(u64, u64)>,
    cancelled: bool,
    stderr: ErrorTail,
    ended: bool,
}

impl AcpInterpreter {
    /// The id of a live item of this turn: unique to the turn, so that a message, a thought
    /// or a tool of one turn never takes the place of another turn's (seen on 25 Nov 2026: a
    /// turn's answer took the previous turn's row, both `aa:m2`). The same on a replay of the
    /// spool, since the turn is.
    fn shown(&self, item: &str) -> String {
        format!("{SHOWN}:{}:{item}", self.turn)
    }

    pub fn new(turn: u64, name: &str) -> Self {
        Self {
            turn,
            name: name.to_string(),
            bound: false,
            message: None,
            thought: None,
            pieces: 0,
            tools: BTreeMap::new(),
            activity: None,
            notices: 0,
            model: None,
            told: None,
            primary: None,
            answered: false,
            steered: Vec::new(),
            settled: std::collections::BTreeSet::new(),
            last_answer: None,
            tokens: None,
            cancelled: false,
            stderr: ErrorTail::default(),
            ended: false,
        }
    }

    fn moved(&mut self, activity: Activity, out: &mut Vec<Signal>) {
        if self.activity.as_ref() != Some(&activity) {
            self.activity = Some(activity.clone());
            out.push(Signal::Moved(activity));
        }
    }

    fn notice(&mut self, code: &str, text: String, out: &mut Vec<Signal>) {
        self.notices += 1;
        out.push(Signal::Notice(Notice {
            id: format!("n-{}-{}", self.name, self.notices),
            turn: Some(self.turn),
            code: code.to_string(),
            text,
            trace_id: None,
            kind: None,
        }));
    }

    /// The message being written is whole: it is a message of the conversation.
    fn close_message(&mut self, out: &mut Vec<Signal>) {
        if let Some((number, text)) = self.message.take()
            && !text.trim().is_empty()
        {
            out.push(Signal::Glimpse(Glimpse::Done {
                id: self.shown(&format!("m{number}")),
            }));
            out.push(Signal::Reply(Reply {
                origin: Origin::User,
                id: format!("r-{}-{number}", self.name),
                turn: self.turn,
                text,
                trace_id: None,
                ..Reply::default()
            }));
        }
    }

    fn close_thought(&mut self, out: &mut Vec<Signal>) {
        if let Some((number, _)) = self.thought.take() {
            out.push(Signal::Glimpse(Glimpse::Done {
                id: self.shown(&format!("t{number}")),
            }));
        }
    }

    /// Whether every prompt of the turn is answered, or it failed before one could be: what
    /// comes after is not of the turn.
    fn over(&self) -> bool {
        (self.answered && self.steered.iter().all(|name| self.settled.contains(name)))
            || matches!(&self.told, Some(Told::Failed { stage, .. }) if stage != "prompt")
    }

    /// The message an answer answers, by the name the driver gives it.
    fn answers(&self, name: &str) -> Answers {
        if let Some(steered) = self.steered.iter().find(|steered| *steered == name) {
            return Answers::Steered(steered.clone());
        }
        match &self.primary {
            Some(primary) if primary != name => Answers::Neither,
            _ => Answers::Own,
        }
    }

    /// The message an answer line answers: the one it names, else, for a line that names
    /// none, the turn's own message while that waits, else the oldest message that waits.
    fn answered_by(&self, line: &Value) -> Answers {
        let name = text_of(&line["uuid"]);
        if !name.is_empty() {
            return self.answers(name);
        }
        if !self.answered {
            return Answers::Own;
        }
        self.steered
            .iter()
            .find(|name| !self.settled.contains(*name))
            .map_or(Answers::Own, |name| Answers::Steered(name.clone()))
    }

    /// One answer is in: the turn's own message's, which says how the turn went, or that of a
    /// message agy took while the turn ran. An answer to a message agy never took (one it
    /// refused) is none of the turn's.
    fn settle(&mut self, which: Answers, told: Told, out: &mut Vec<Signal>) {
        match which {
            Answers::Steered(name) => {
                self.settled.insert(name);
            }
            Answers::Own if !self.answered => {
                self.answered = true;
                // A failure the driver told before the prompt (its stage says which: the
                // model, the conversation, the sign-in) stays what the turn ended on, in its
                // own words; the result that settles it says no more, and nothing was asked of
                // the model.
                if matches!(&self.told, Some(Told::Failed { stage, .. }) if stage != "prompt") {
                    return;
                }
                out.push(Signal::Concluded);
                self.told = Some(told);
            }
            Answers::Own | Answers::Neither => {}
        }
    }

    fn on_line(&mut self, line: &str, t: i64, out: &mut Vec<Signal>) {
        let Ok(message) = serde_json::from_str::<Value>(line) else {
            return;
        };
        // What the driver says of a message the Host gave the running turn is the actor's to
        // read (`clarp_kernel::steering`): that it took it, and which answer settles it. Here
        // a message it took is one more prompt whose answer belongs to the turn's output.
        if message["type"] == "user" {
            let name = text_of(&message["uuid"]);
            if message["isReplay"] == true
                && !name.is_empty()
                && !self.steered.iter().any(|seen| seen == name)
            {
                self.steered.push(name.to_string());
                // What agy said before it took the message answered what came before: it is
                // whole here, and what agy says next is for the new message.
                self.close_thought(out);
                self.close_message(out);
            }
            return;
        }
        if message["type"] == "result" {
            // A result settles each message it names: one answer may cover more than one
            // prompt.
            let failed = message["is_error"] == true;
            let names = message["user_message_uuids"]
                .as_array()
                .into_iter()
                .flatten();
            for name in names
                .filter_map(Value::as_str)
                .filter(|name| !name.is_empty())
            {
                let which = self.answers(name);
                let told = match &self.last_answer {
                    Some(answer) => answer.clone(),
                    None if failed => Told::Failed {
                        stage: "prompt".to_string(),
                        text: text_of(&message["result"]).to_string(),
                    },
                    None => Told::Answered(StopReason::EndTurn),
                };
                self.settle(which, told, out);
            }
            return;
        }
        if self.over() {
            return; // nothing after the last answer is of the turn
        }
        if let Some(what) = message["clarp"].as_str() {
            self.on_driver(what, &message, out);
            return;
        }
        if let Some(update) = SessionUpdate::of_notification(&message) {
            self.on_update(update, t, out);
        }
    }

    fn on_driver(&mut self, what: &str, line: &Value, out: &mut Vec<Signal>) {
        match what {
            "initialized" => self.moved(Activity::Thinking, out),
            "bound" => {
                let primary = text_of(&line["uuid"]);
                if !primary.is_empty() {
                    self.primary = Some(primary.to_string());
                }
                let session = text_of(&line["session"]).trim();
                if !self.bound && !session.is_empty() {
                    self.bound = true;
                    out.push(Signal::Bound {
                        backend_session: session.to_string(),
                    });
                }
            }
            "model" => self.model(text_of(&line["model"]), out),
            "notice" => {
                let code = match text_of(&line["code"]) {
                    "" => "agy-acp",
                    code => code,
                };
                let text = text_of(&line["text"]).to_string();
                if !text.is_empty() {
                    self.notice(code, text, out);
                }
            }
            // A question with fixed choices is answered by policy, as a permission is: the
            // user is told what was chosen. An allowed permission says nothing.
            // One asked after a Stop is declined without a word: the turn is ending.
            "permission"
                if line["stopped"] != true
                    && !matches!(text_of(&line["kind"]), "allow_once" | "allow_always") =>
            {
                let title = shorten(text_of(&line["title"]), 300);
                let text = match line["answer"].as_str() {
                    Some(answer) => {
                        format!("Antigravity asked \"{title}\"; Clarp answered \"{answer}\".")
                    }
                    None => format!(
                        "Antigravity asked \"{title}\" and offered no answer; it was declined."
                    ),
                };
                self.notice("agy-question", text, out);
            }
            "cancel" => self.cancelled = true,
            // A reply cut for a message ends at the cut: nothing more of it is shown, so it is
            // a row from here, not when the message is read (seen 24 Nov 2026: the live reply
            // turned into its row just before the answer to the message, seconds later). The
            // cut is no Stop.
            "cut" if line["stage"] == "cancel" => {
                self.close_thought(out);
                self.close_message(out);
            }
            // An answer to a message agy never took (one it refused) leaves the turn as it was.
            "prompted" | "failed"
                if (what == "prompted" || line["stage"] == "prompt")
                    && matches!(self.answered_by(line), Answers::Neither) => {}
            "prompted" => {
                self.close_thought(out);
                self.close_message(out);
                let answer = PromptResponse::read(&line["result"]);
                if let Some((tokens_in, tokens_out)) = answer.usage.and_then(|usage| usage.tokens())
                {
                    let (had_in, had_out) = self.tokens.unwrap_or_default();
                    let (tokens_in, tokens_out) = (
                        had_in.saturating_add(tokens_in),
                        had_out.saturating_add(tokens_out),
                    );
                    self.tokens = Some((tokens_in, tokens_out));
                    out.push(Signal::Usage {
                        tokens_in,
                        tokens_out,
                    });
                }
                let which = self.answered_by(line);
                let told = Told::Answered(answer.stop_reason);
                self.last_answer = Some(told.clone());
                self.settle(which, told, out);
            }
            "failed" => {
                self.close_thought(out);
                self.close_message(out);
                let stage = text_of(&line["stage"]).to_string();
                let text = text_of(&line["text"]).to_string();
                if stage == "prompt" {
                    let which = self.answered_by(line);
                    let told = Told::Failed { stage, text };
                    self.last_answer = Some(told.clone());
                    self.settle(which, told, out);
                } else {
                    self.told = Some(Told::Failed { stage, text });
                }
            }
            _ => {}
        }
    }

    fn model(&mut self, model: &str, out: &mut Vec<Signal>) {
        let model = model.trim();
        if !model.is_empty() && self.model.as_deref() != Some(model) {
            self.model = Some(model.to_string());
            out.push(Signal::Model(model.to_string()));
        }
    }

    /// A piece of what the agent says or thinks, added to the message or thought being written.
    fn piece(&mut self, thought: bool, piece: &str, out: &mut Vec<Signal>) {
        if thought {
            self.close_message(out);
        } else {
            self.close_thought(out);
        }
        if piece.is_empty() {
            return;
        }
        let open = if thought {
            &self.thought
        } else {
            &self.message
        };
        let number = match open {
            Some((number, _)) => *number,
            None => {
                self.pieces += 1;
                self.pieces
            }
        };
        let slot = if thought {
            &mut self.thought
        } else {
            &mut self.message
        };
        let (_, text) = slot.get_or_insert_with(|| (number, String::new()));
        text.push_str(piece);
        let text = text.clone();
        if thought {
            self.moved(Activity::Thinking, out);
            if !text.trim().is_empty() {
                out.push(Signal::Glimpse(Glimpse::Reasoning {
                    id: self.shown(&format!("t{number}")),
                    text,
                }));
            }
            return;
        }
        let reply = format!("r-{}-{number}", self.name);
        self.moved(Activity::Responding, out);
        if !text.trim().is_empty() {
            out.push(Signal::Glimpse(Glimpse::Message {
                id: self.shown(&format!("m{number}")),
                text: text.clone(),
                row: Some(reply.clone()),
            }));
            out.push(Signal::Live(LiveReply { id: reply, text }));
        }
    }

    fn on_update(&mut self, update: SessionUpdate, t: i64, out: &mut Vec<Signal>) {
        match update {
            SessionUpdate::AgentMessageChunk { content } => self.piece(false, content.text(), out),
            SessionUpdate::AgentThoughtChunk { content } => self.piece(true, content.text(), out),
            SessionUpdate::ToolCall(call) | SessionUpdate::ToolCallUpdate(call) => {
                self.close_thought(out);
                self.close_message(out);
                self.on_tool(&call, t, out);
            }
            SessionUpdate::Plan { entries } => {
                let steps: Vec<Value> = entries
                    .iter()
                    .map(|entry| {
                        json!({
                            "text": entry.content,
                            "status": entry.status.as_deref().filter(|status| !status.is_empty()).unwrap_or("pending"),
                        })
                    })
                    .collect();
                out.push(Signal::Glimpse(Glimpse::Plan {
                    prefix: SHOWN.to_string(),
                    steps: Value::Array(steps),
                }));
            }
            SessionUpdate::UsageUpdate {
                used: Some(used), ..
            } => {
                out.push(Signal::Context { tokens: used });
            }
            SessionUpdate::ConfigOptionUpdate { config_options } => {
                if let Some(model) = schema::current(&Value::Array(config_options), "model") {
                    self.model(&model, out);
                }
            }
            SessionUpdate::UsageUpdate { used: None, .. }
            | SessionUpdate::UserMessageChunk { .. }
            | SessionUpdate::Other => {}
        }
    }

    fn on_tool(&mut self, update: &schema::ToolCall, t: i64, out: &mut Vec<Signal>) {
        let raw = update.tool_call_id.trim().to_string();
        if raw.is_empty() {
            return;
        }
        let call_id = format!("c-{}-{raw}", self.name);
        let shown = self.shown(&raw);
        if !self.tools.contains_key(&raw) {
            let name = tool_name(update);
            let input = tool_input(&name, update);
            let call = ToolCall {
                origin: Origin::User,
                id: call_id.clone(),
                turn: self.turn,
                name: name.clone(),
                input: input.clone(),
                status: ToolStatus::Running,
                output: None,
                started_at_ms: t,
                ended_at_ms: None,
                ..ToolCall::default()
            };
            self.tools.insert(raw.clone(), call.clone());
            out.push(Signal::Glimpse(Glimpse::Tool {
                id: shown.clone(),
                call_id,
                name: name.clone(),
                input,
                whole: true,
            }));
            out.push(Signal::Tool(call));
            self.moved(Activity::Tool { name }, out);
        }
        if !update.ended() {
            return;
        }
        let Some(call) = self.tools.get_mut(&raw) else {
            return;
        };
        if call.status != ToolStatus::Running {
            return; // told twice
        }
        // What the call wrote or changed may come only with its end.
        let later = tool_input(&call.name, update);
        if let (Some(kept), Some(later)) = (call.input.as_object_mut(), later.as_object()) {
            for (key, value) in later {
                let empty = kept
                    .get(key)
                    .is_none_or(|kept| kept.as_str().is_some_and(str::is_empty));
                if empty && value.as_str().is_some_and(|value| !value.is_empty()) {
                    kept.insert(key.clone(), value.clone());
                }
            }
        }
        let (came_back, exit_code) = tool_output(update);
        let failed =
            update.status.as_deref() == Some("failed") || exit_code.is_some_and(|code| code != 0);
        call.status = if failed {
            ToolStatus::Error
        } else {
            ToolStatus::Ok
        };
        call.output = Some(shorten(&came_back, crate::TOOL_OUTPUT_KEPT));
        call.ended_at_ms = Some(t);
        out.push(Signal::Glimpse(Glimpse::Returned {
            id: shown,
            content: Value::String(came_back),
            failed,
            exit_code,
        }));
        out.push(Signal::Tool(call.clone()));
        self.moved(Activity::Thinking, out);
    }

    fn on_exit(&mut self, code: Option<i32>, signal: Option<i32>, out: &mut Vec<Signal>) {
        if self.ended {
            return;
        }
        self.ended = true;
        self.close_thought(out);
        self.close_message(out);
        let told = self.told.clone();
        let outcome = match (&told, signal) {
            (_, Some(signal)) => RunOutcome::Interrupted {
                reason: if self.cancelled {
                    "Antigravity's turn was stopped".to_string()
                } else {
                    format!("agy was ended by signal {signal}")
                },
            },
            (Some(Told::Answered(stop)), _) => match stop {
                StopReason::Cancelled => RunOutcome::Interrupted {
                    reason: "Antigravity cancelled the turn".to_string(),
                },
                StopReason::Refusal => RunOutcome::Failed {
                    reason: "Antigravity refused to go on with this turn".to_string(),
                },
                StopReason::MaxTokens | StopReason::MaxTurnRequests => {
                    let what = if *stop == StopReason::MaxTokens {
                        "tokens"
                    } else {
                        "requests"
                    };
                    self.notice(
                        "agy-turn-limit",
                        format!(
                            "Antigravity stopped: it reached its limit of {what} for one turn."
                        ),
                        out,
                    );
                    RunOutcome::Completed
                }
                StopReason::EndTurn | StopReason::Other(_) => RunOutcome::Completed,
            },
            (Some(Told::Failed { stage, text }), _) => {
                let tail = self.stderr.text().unwrap_or_default();
                let mut failure = shared::classify(text);
                if failure == shared::Failure::Other && !tail.is_empty() {
                    failure = shared::classify(&format!("{text}\n{tail}"));
                }
                if stage == "auth" && failure == shared::Failure::Other {
                    failure = shared::Failure::SignedOut;
                }
                let said = if text.trim().is_empty() { &tail } else { text };
                RunOutcome::Failed {
                    reason: shorten(&shared::reason(&failure, said), 600),
                }
            }
            (None, _) if code != Some(0) => RunOutcome::Failed {
                reason: self.stderr.text().unwrap_or_else(|| {
                    format!(
                        "the Antigravity driver exited with code {}",
                        code.unwrap_or(-1)
                    )
                }),
            },
            (None, _) => RunOutcome::Failed {
                reason: self
                    .stderr
                    .text()
                    .unwrap_or_else(|| "agy ended without saying how its turn ended".to_string()),
            },
        };
        out.push(Signal::Ended(outcome));
    }
}

/// The fields in which Antigravity gives a whole file's content.
const WHOLE: [&str; 3] = ["Content", "CodeContent", "content"];

/// What a tool is called in the conversation: Antigravity's own name of it where the title
/// says one, else what its kind is, else its title.
fn tool_name(update: &schema::ToolCall) -> String {
    let title = update.title.as_deref().unwrap_or_default().trim();
    let native = title
        .split(|c: char| !(c.is_ascii_alphanumeric() || c == '_'))
        .find_map(agy::known_tool);
    if let Some(name) = native {
        return name.to_string();
    }
    let raw = update.raw_input.as_ref().unwrap_or(&Value::Null);
    if raw["CommandLine"].is_string() {
        return "Bash".to_string();
    }
    // A call that gives a whole file's content and nothing to replace writes the file.
    let whole = WHOLE.iter().any(|key| raw[*key].is_string());
    if whole && !raw["TargetContent"].is_string() {
        return "Write".to_string();
    }
    let by_kind = match update.kind.as_deref().unwrap_or_default() {
        "execute" => "Bash",
        "read" => "Read",
        "edit" | "delete" | "move" => "Edit",
        "search" => "Grep",
        "fetch" => "WebFetch",
        _ => "",
    };
    match (by_kind, title) {
        ("", "") => "tool".to_string(),
        ("", title) => shorten(title, 80),
        (kind, _) => kind.to_string(),
    }
}

/// What a tool was given, under the names the conversation's tools have; the file of a
/// call that names none in its input is the first place it says it touches.
fn tool_input(name: &str, update: &schema::ToolCall) -> Value {
    let mut input = agy::tool_input(name, update.raw_input.as_ref().unwrap_or(&Value::Null));
    let location = update
        .locations
        .as_ref()
        .and_then(|places| places.first())
        .map(|place| place.path.as_str())
        .filter(|path| !path.is_empty());
    if let (Some(path), Some(fields)) = (location, input.as_object_mut())
        && let Some(file) = fields.get_mut("file_path")
        && file.as_str().is_some_and(str::is_empty)
    {
        *file = json!(path);
    }
    // What was written or changed, where the call says it: in its input, or in the diff it
    // shows (the protocol's `{"type": "diff", "path", "oldText", "newText"}`).
    let raw = update.raw_input.as_ref().unwrap_or(&Value::Null);
    let said = |keys: &[&str]| {
        keys.iter()
            .find_map(|key| raw[*key].as_str().map(str::to_string))
    };
    let diff = update
        .content
        .iter()
        .flatten()
        .find(|block| block["type"] == "diff");
    let Some(fields) = input.as_object_mut() else {
        return input;
    };
    let mut fill = |key: &str, value: Option<String>| {
        let empty = fields
            .get(key)
            .is_none_or(|kept| kept.as_str().is_some_and(str::is_empty));
        if let Some(value) = value.filter(|value| !value.is_empty())
            && empty
        {
            fields.insert(key.to_string(), json!(value));
        }
    };
    let new_text = diff
        .and_then(|diff| diff["newText"].as_str())
        .map(str::to_string);
    if let Some(path) = diff.and_then(|diff| diff["path"].as_str()) {
        fill("file_path", Some(path.to_string()));
    }
    match name {
        "Write" => fill("content", said(&WHOLE).or(new_text)),
        "Edit" => {
            let old_text = diff
                .and_then(|diff| diff["oldText"].as_str())
                .map(str::to_string);
            fill(
                "old_string",
                said(&["TargetContent", "old_string"]).or(old_text),
            );
            fill(
                "new_string",
                said(&["ReplacementContent", "new_string"])
                    .or(new_text)
                    .or_else(|| said(&WHOLE)),
            );
        }
        _ => {}
    }
    input
}

/// What a tool gave back, and the code its command ended with where that is said.
fn tool_output(update: &schema::ToolCall) -> (String, Option<i64>) {
    let raw = update.raw_output.as_ref().unwrap_or(&Value::Null);
    for key in ["combinedOutput", "combined_output", "output"] {
        if let Some(output) = raw[key].as_str().filter(|output| !output.is_empty()) {
            return agy::came_back(output);
        }
    }
    if let Some(output) = raw.as_str().filter(|output| !output.is_empty()) {
        return agy::came_back(output);
    }
    let mut parts: Vec<String> = Vec::new();
    for block in update.content.iter().flatten() {
        match text_of(&block["type"]) {
            "content" => {
                let text = text_of(&block["content"]["text"]);
                if !text.is_empty() {
                    parts.push(text.to_string());
                }
            }
            "diff" => parts.push(format!("Edited {}", text_of(&block["path"]))),
            _ => {}
        }
    }
    if parts.is_empty() {
        return (String::new(), None);
    }
    agy::came_back(&parts.join("\n"))
}

impl Interpreter for AcpInterpreter {
    fn on_record(&mut self, record: &Record, out: &mut Vec<Signal>) {
        match &record.entry {
            Entry::Out(line) => self.on_line(line, record.t, out),
            Entry::Err(line) => self.stderr.push(line),
            Entry::Shim(ShimEvent::Exited { code, signal }) => {
                self.on_exit(*code, *signal, out);
            }
            Entry::Shim(_) | Entry::Hook { .. } => {}
        }
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::path::PathBuf;

    use super::*;

    fn spec(prompt: &str) -> TurnSpec {
        TurnSpec {
            agent_id: "a1".into(),
            session: "nova".into(),
            turn: 3,
            attempt: 1,
            trace_id: None,
            takeover: None,
            cwd: PathBuf::from("/work"),
            prompt: prompt.into(),
            backend_session: Some("conv-1".into()),
            new_session: None,
            model: Some("gemini-x".into()),
            effort: Some("HIGH".into()),
            identity: None,
            told: None,
        }
    }

    #[test]
    fn a_turn_is_launched_through_the_driver_with_its_order_on_standard_input() {
        let launch = AgyAcp::with_program("/opt/agy").launch(&spec("look at /d/uploads/s/a.png"));
        assert_eq!(launch.program, "clarp-shim");
        assert_eq!(launch.args, [shared::DRIVER, "/opt/agy"]);
        assert_eq!(launch.before_stop, Some(("SIGUSR1", STOP_GRACE_MS)));
        let order = Order::from_json(&serde_json::from_slice(&launch.stdin).unwrap());
        assert_eq!(order.session, "conv-1");
        assert_eq!(order.cwd, "/work");
        assert_eq!(order.effort, "high");
        assert_eq!(order.attachments, ["/d/uploads/s/a.png"]);
        assert_eq!(
            order.headed, order.prompt,
            "no head without what the Host tells"
        );
        assert!(AgyAcp::new().message_most().is_none());
        assert_eq!(AgyAcp::new().runs_through(), Some("agy"));
    }

    #[test]
    fn attached_files_are_the_uploads_of_a_kind_that_goes_as_more_than_a_path() {
        let prompt = "see (/d/uploads/s/a.png), /d/uploads/s/b.PDF. and /d/uploads/s/c.txt \
                      /home/x/d.png `/d/uploads/s/e.mp3` /d/uploads/s/a.png /d/uploads/../f.png";
        assert_eq!(
            attached(prompt),
            [
                "/d/uploads/s/a.png",
                "/d/uploads/s/b.PDF",
                "/d/uploads/s/e.mp3"
            ]
        );
    }

    /// A janitor's isolated question goes to Antigravity as one sandboxed print in a home
    /// of its own, exactly as for the plain card: never through ACP, and never into the
    /// conversation store the agents share.
    #[test]
    fn an_isolated_question_is_the_plain_cards_one_shot_in_its_own_home() {
        let reading = Reading {
            instructions: "Keep the books.".to_string(),
            asked: "GOAL AS RECORDED: ...".to_string(),
            schema: r#"{"type":"object"}"#.to_string(),
            model: Some("gemini-4-hydrogen-low".to_string()),
            effort: None,
        };
        let acp = AgyAcp::with_program("/opt/agy");
        let plain = crate::agy::Agy::with_program("/opt/agy");
        let reads = acp.reads(&reading).unwrap();
        assert_eq!(Some(reads.clone()), plain.reads(&reading));
        assert_eq!(reads.launch.program, "/opt/agy");
        assert!(reads.launch.args.iter().any(|arg| arg == "--sandbox"));
        assert!(!reads.launch.args.iter().any(|arg| arg == "acp"));
        assert_eq!(acp.own_home(), plain.own_home());
        assert!(acp.own_home().is_some());
    }
}
