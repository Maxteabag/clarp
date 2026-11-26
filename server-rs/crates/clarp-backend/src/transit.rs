//! Carrying a conversation into another tool: what an agent that changes its backend keeps.
//!
//! An agent that moves from one tool to another keeps its conversation. The Host has its own
//! record of what was said (the user's words and the agent's answers, turn by turn), and a
//! writer here puts that record where the target tool keeps its sessions, in the tool's own
//! format, so that the tool's next turn continues it as one of its own. What is carried is
//! the text of each turn, in order, with the tools the agent used and what they gave back:
//! in the target's own form where it has one (Claude's `tool_use` and `tool_result` blocks,
//! Codex's `function_call` and `function_call_output` items, OpenCode's tool parts), and as
//! quoted blocks in the text where it has none (Grok, and the record Antigravity and OpenCode
//! are given at the head of a new session, [`quoted`]). A result longer than [`RESULT_MOST`]
//! is cut, with a line that says how much was. Attachments are carried as what the user's
//! message holds of them, the path of each file, never their content. Thinking is left out on
//! purpose: it holds for the model that thought it (Claude signs its own), not another.
//!
//! This is the `transit` tool's injectors, rebuilt for the Host (its notes on the formats are
//! the rules: a Claude assistant row has a list of blocks as its content, never a string; a
//! Codex rollout begins with `session_meta` and carries the text in `response_item` messages;
//! a Grok summary names a model; OpenCode is handed its session through its own import).
//! Antigravity has no writer: its database is never opened while it may run.
//!
//! A writer writes and registers, and nothing else: it never runs the tool. What it wrote is
//! named in [`Written`], so that whoever asked can take it back ([`Written::undo`]) when the
//! change it was for does not happen. A writer that fails takes back what it wrote itself.

use std::io::{Read as _, Write as _};
use std::path::{Path, PathBuf};

use serde_json::{Value, json};
use sha2::{Digest as _, Sha256};

/// How many signs of the conversation are read at most to carry it: more than the largest
/// budget of any target ([`budget`]), so that what is left out is known.
pub const CARRIED_MOST: usize = 3_000_000;

/// How many signs a token is reckoned at. Prose has about four; code, JSON and paths fewer:
/// three keeps a budget on the safe side.
const SIGNS_PER_TOKEN: usize = 3;

/// How many tokens a model takes in: what its tool's catalogue says of it
/// (`context_tokens`), else what its tool's models take, as the tools say. A tool given more
/// than its model can hold would fail its next turn, or shorten what it was given at once.
pub fn context_tokens(backend: &str, model: &str) -> usize {
    let declared = crate::catalogue::declared(backend);
    let listed = declared["models"].as_array().and_then(|models| {
        models
            .iter()
            .find(|row| row["id"] == model)
            .and_then(|row| row["context_tokens"].as_u64())
    });
    if let Some(tokens) = listed.and_then(|tokens| usize::try_from(tokens).ok()) {
        return tokens;
    }
    match backend {
        "claude" if model.contains("[1m]") => 1_000_000,
        "claude" => 200_000,
        "codex" => 272_000,
        "grok" => 256_000,
        "agy" | "agy-acp" => 1_000_000,
        _ => 128_000,
    }
}

/// How many signs of the conversation are carried into a model at most: about 60% of what it
/// takes in, the rest left for the tool's own instructions and the turns to come.
pub fn budget(backend: &str, model: &str) -> usize {
    budget_of_tokens(context_tokens(backend, model))
}

/// The budget of a model that takes in `tokens`: about 60% of it, in signs.
pub fn budget_of_tokens(tokens: usize) -> usize {
    tokens * 6 / 10 * SIGNS_PER_TOKEN
}

/// How long the summary of what did not fit may be, in signs: what it is asked for is much
/// shorter, and what is longer is cut.
pub const SUMMARY_MOST: usize = 16_000;

/// Who wrote the summary of what did not fit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum SummaryBy {
    /// The agent itself, in one more turn on its old tool just before it moved.
    Agent,
    /// Another model, of this name, from the Host's record.
    Model(String),
}

impl SummaryBy {
    /// As an app is told it.
    pub fn word(&self) -> &'static str {
        match self {
            SummaryBy::Agent => "agent",
            SummaryBy::Model(_) => "model",
        }
    }
}

/// A summary of the oldest part of a conversation, which did not fit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Summary {
    pub text: String,
    pub by: SummaryBy,
}

/// What is carried of a conversation within a budget: from which thing that was said it is
/// carried as it was, and what stands at its head when something older did not fit.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Fitted {
    pub from: usize,
    /// The summary of what did not fit, or a note that it was left out.
    pub head: Option<String>,
    pub summarized: bool,
}

/// What of `said` is carried within `budget` signs, from the tool `from` (as a person reads
/// it) to `to`: all of it where it fits; else its newest part, under the `summary` of the
/// older part where there is one, else under a note that the older part was left out.
pub fn fitted(
    said: &[Said],
    budget: usize,
    summary: Option<&Summary>,
    from: &str,
    to: &str,
) -> Fitted {
    let whole = kept_from(said, budget);
    if whole == 0 {
        return Fitted {
            from: 0,
            head: None,
            summarized: false,
        };
    }
    let at = kept_from(said, budget.saturating_sub(SUMMARY_MOST + 1_000));
    let turns = match at {
        1 => "oldest turn".to_string(),
        n => format!("{n} oldest turns"),
    };
    let head = match summary.filter(|summary| !summary.text.trim().is_empty()) {
        Some(summary) => {
            let who = match &summary.by {
                SummaryBy::Agent => "the agent itself, just before the move".to_string(),
                SummaryBy::Model(model) => format!("{model}, from Clarp's record"),
            };
            let text: String = summary.text.trim().chars().take(SUMMARY_MOST).collect();
            format!(
                "[Clarp: this conversation moved from {from} to {to}. Its {turns} did not fit \
                 and are summarized here; what follows the summary is carried as it was. \
                 Summary, written by {who}:]\n\n{text}"
            )
        }
        None => format!(
            "[Clarp: this conversation moved from {from} to {to}. Its {turns} did not fit and \
             were left out: no summary of them could be written. What follows is its most \
             recent part.]"
        ),
    };
    Fitted {
        from: at,
        summarized: summary.is_some_and(|summary| !summary.text.trim().is_empty()),
        head: Some(head),
    }
}

/// What a writer is given of `said` as it was [`fitted`]: the head first, as something the
/// user said, then the newest part.
pub fn with_head(said: &[Said], fitted: &Fitted) -> Vec<Said> {
    let kept = &said[fitted.from..];
    let mut carried = Vec::with_capacity(kept.len() + 1);
    if let Some(head) = &fitted.head {
        let at_ms = kept.first().map_or(0, |one| one.at_ms);
        carried.push(Said::new(Role::User, head.clone(), at_ms));
    }
    carried.extend_from_slice(kept);
    carried
}

/// What another model is asked when the agent cannot summarize its own conversation, with
/// the shape of its answer: the older part of the conversation, as quoted blocks, newest
/// last, as much of its end as `most` signs hold.
pub fn summary_reading(older: &[Said], most: usize) -> (String, String, String) {
    let mut blocks: Vec<String> = Vec::new();
    let mut size = 0usize;
    for one in older.iter().rev() {
        let who = match one.role {
            Role::User => "User",
            Role::Assistant => "Assistant",
        };
        let block = format!("{who}: {}", with_quoted(one));
        size += block.chars().count() + 2;
        if size > most && !blocks.is_empty() {
            break;
        }
        blocks.push(block);
    }
    blocks.reverse();
    let instructions = SUMMARY_ASK.to_string();
    let asked = format!("The conversation:\n\n{}", blocks.join("\n\n"));
    let schema = json!({
        "type": "object",
        "properties": {"summary": {"type": "string"}},
        "required": ["summary"],
        "additionalProperties": false,
    })
    .to_string();
    (instructions, asked, schema)
}

/// What a summary of the older part of a conversation is to hold.
pub const SUMMARY_ASK: &str = "Summarize this conversation between a user and an AI agent \
for the agent, which goes on with it without having it. Keep what it needs to go on: what \
the user wants and asked it to remember, decisions made and why, facts learned, files, paths, \
commands, ids and names that matter, what was done and what is still open. Leave out \
pleasantries and what no longer matters. At most 1,500 words, plain text.";

/// What the agent itself is asked, in one more turn on its old tool, just before it moves:
/// the summary of its conversation up to its newest part, which goes along as it was.
pub fn summary_turn(to: &str, newest_begins: &str) -> String {
    let begins: String = newest_begins
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let begins: String = begins.chars().take(160).collect();
    format!(
        "[Clarp] You are about to move to {to}. Your conversation is too long to carry whole: \
         its older part will be replaced by a summary that you write now, and its newest part, \
         from the message that begins \u{201c}{begins}\u{201d}, goes along as it was. Write that \
         summary of everything before it, for yourself, to go on from: what the user wants and \
         asked you to remember, decisions made and why, facts learned, files, paths, commands, \
         ids and names that matter, what was done and what is still open. Do not use any tool \
         and do not change anything: answer with the summary alone, at most 1,500 words, \
         plain text."
    )
}

/// Who said it.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Role {
    User,
    Assistant,
}

impl Role {
    fn name(self) -> &'static str {
        match self {
            Role::User => "user",
            Role::Assistant => "assistant",
        }
    }
}

/// One thing that was said in the conversation, as the Host recorded it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Said {
    pub role: Role,
    pub text: String,
    pub at_ms: i64,
    /// The tools an answer used after its text, in order: none for a user's message.
    pub tools: Vec<Used>,
}

impl Said {
    /// A thing said, without tools.
    pub fn new(role: Role, text: impl Into<String>, at_ms: i64) -> Self {
        Self {
            role,
            text: text.into(),
            at_ms,
            tools: Vec::new(),
        }
    }

    /// How many signs it takes of what is carried: its text, and of each tool what is
    /// carried of its input and result.
    pub fn size(&self) -> usize {
        self.text.chars().count()
            + self
                .tools
                .iter()
                .map(|used| used.name.len() + used.input_text().len() + used.result().len())
                .sum::<usize>()
    }

    fn carried(&self) -> bool {
        !self.text.trim().is_empty() || !self.tools.is_empty()
    }
}

/// How long a tool's result, or its input, is carried at most, in bytes: what is past it is
/// cut, with a line that says how much ([`cut`]).
pub const RESULT_MOST: usize = 8 * 1024;

/// How long a carried call id is at most, in signs: Codex's provider refuses a longer one,
/// and with it every turn of the conversation.
pub const CALL_ID_MOST: usize = 64;

/// One use of a tool by the agent, with what it gave back, as the Host recorded it (or, for
/// a result the Host kept only the start of, as the source tool's own transcript has it).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Used {
    /// The id the source tool gave the call, where it gave one the Host kept.
    pub id: String,
    pub name: String,
    pub input: Value,
    /// What it gave back. `None`: it never returned (its turn was cut off).
    pub output: Option<String>,
    /// It returned an error.
    pub failed: bool,
    /// The Host kept only the start of the result, and nothing has the rest of it.
    pub shortened: bool,
    /// The session of the source tool the call was made in, where the Host knows it: where
    /// a result the Host kept only the start of is looked up whole.
    pub session: Option<String>,
}

impl Used {
    /// The call's input as JSON text, cut past [`RESULT_MOST`].
    pub fn input_text(&self) -> String {
        let input = if self.input.is_null() {
            "{}".to_string()
        } else {
            self.input.to_string()
        };
        cut(&input, RESULT_MOST)
    }

    /// The call's input as an object, which is what a tool call's input must be: anything
    /// else is put under `input`.
    pub fn input_object(&self) -> Value {
        match &self.input {
            Value::Object(_) => self.input.clone(),
            Value::Null => json!({}),
            other => json!({"input": other}),
        }
    }

    /// What is carried of the result: the result, cut past [`RESULT_MOST`], or a line that
    /// says there is none.
    pub fn result(&self) -> String {
        match &self.output {
            None => "[The call never returned: its turn was cut off.]".to_string(),
            Some(output) if self.shortened => format!(
                "{}\n[Clarp kept only the start of this result; the rest was not carried.]",
                cut(output.trim_end_matches('\u{2026}'), RESULT_MOST)
            ),
            Some(output) => cut(output, RESULT_MOST),
        }
    }

    /// A call id the target tools take: letters, digits, `_` and `-`, never empty, at most
    /// [`CALL_ID_MOST`] signs. A longer id (one carried from OpenCode, or a Host row id) becomes
    /// `call_` and the start of the SHA-256 of the whole id, the same on every run, so a call
    /// and its output, both written from this, still pair.
    pub fn call_id(&self, at: usize) -> String {
        let id: String = self
            .id
            .chars()
            .map(|c| {
                if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                    c
                } else {
                    '_'
                }
            })
            .collect();
        if id.is_empty() {
            format!("call_carried_{at}")
        } else if id.len() > CALL_ID_MOST {
            let digest = Sha256::digest(self.id.as_bytes());
            let hex: String = digest[..16].iter().map(|b| format!("{b:02x}")).collect();
            format!("call_{hex}")
        } else {
            id
        }
    }

    /// A tool name the target tools take: letters, digits, `_` and `-`, at most 64 signs.
    pub fn tool_name(&self) -> String {
        let name: String = self
            .name
            .chars()
            .map(|c| {
                if c.is_ascii_alphanumeric() || c == '-' || c == '_' {
                    c
                } else {
                    '_'
                }
            })
            .take(64)
            .collect();
        if name.is_empty() {
            "tool".to_string()
        } else {
            name
        }
    }
}

impl Used {
    /// A call as the Host's record has it, made in the source tool's session `session`.
    pub fn recorded(call: &clarp_kernel::conversation::ToolCall, session: Option<String>) -> Self {
        use clarp_kernel::conversation::ToolStatus;
        let output = call
            .output
            .clone()
            .filter(|_| !matches!(call.status, ToolStatus::Running | ToolStatus::Interrupted));
        // A result the record shortened ends in the mark, past as many signs as it keeps.
        let shortened = output.as_deref().is_some_and(|output| {
            output.ends_with('\u{2026}') && output.chars().count() > crate::TOOL_OUTPUT_KEPT
        });
        Self {
            id: call.id.clone(),
            name: call.name.clone(),
            input: call.input.clone(),
            output,
            failed: call.status == ToolStatus::Error,
            shortened,
            session,
        }
    }
}

/// Fills in, from Claude Code's own transcripts under `projects`, the results of calls made
/// in Claude that the Host kept only the start of, or nothing of (a picture has no text):
/// Claude Code keeps each whole, under the id the Host keeps the call by. A picture is never
/// carried: it is named, with the file it was read from where the call says.
pub fn whole_results(said: &mut [Said], projects: &Path, cwd: &str) {
    use std::collections::{BTreeMap, BTreeSet};
    let mut wanted: BTreeMap<String, BTreeSet<String>> = BTreeMap::new();
    for used in said.iter().flat_map(|one| &one.tools) {
        let short = used.shortened || used.output.as_deref().is_some_and(str::is_empty);
        if let Some(session) = used
            .session
            .as_ref()
            .filter(|_| short && !used.id.is_empty())
        {
            wanted
                .entry(session.clone())
                .or_default()
                .insert(used.id.clone());
        }
    }
    let mut found: BTreeMap<String, String> = BTreeMap::new();
    for (session, ids) in wanted {
        let Some(path) = crate::past::claude_transcript(projects, cwd, &session, true) else {
            continue;
        };
        let Ok(file) = std::fs::File::open(path) else {
            continue;
        };
        for line in std::io::BufRead::split(std::io::BufReader::new(file), b'\n') {
            let Ok(line) = line else {
                break;
            };
            // Only a line that holds a result is read whole.
            if !line.windows(11).any(|w| w == b"tool_result") {
                continue;
            }
            let Ok(entry) = serde_json::from_slice::<Value>(&line) else {
                continue;
            };
            for block in entry["message"]["content"].as_array().into_iter().flatten() {
                let id = block["tool_use_id"].as_str().unwrap_or_default();
                if block["type"] == "tool_result" && ids.contains(id) {
                    found.insert(id.to_string(), result_text(&block["content"]));
                }
            }
        }
    }
    for used in said.iter_mut().flat_map(|one| &mut one.tools) {
        if let Some(text) = found.get(&used.id) {
            let file = ["file_path", "path"]
                .iter()
                .find_map(|key| used.input[key].as_str());
            let named = match file {
                Some(file) => format!("[A picture, not carried: it is the file {file}.]"),
                None => "[A picture, not carried.]".to_string(),
            };
            used.output = Some(text.replace(PICTURE, &named));
            used.shortened = false;
        }
    }
}

const PICTURE: &str = "\u{0}picture\u{0}";

/// The text of a result Claude Code kept: its text, or the texts of its parts, a picture
/// as a mark that [`whole_results`] names.
fn result_text(content: &Value) -> String {
    match content {
        Value::String(text) => text.clone(),
        Value::Array(parts) => parts
            .iter()
            .filter_map(|part| match part["type"].as_str() {
                Some("image") => Some(PICTURE.to_string()),
                _ => part["text"].as_str().map(str::to_string),
            })
            .collect::<Vec<_>>()
            .join("\n"),
        _ => String::new(),
    }
}

/// `text` whole where it is no longer than `most` bytes; else its first `most` bytes (never a
/// sign cut in half) and a line that says how many bytes were cut.
pub fn cut(text: &str, most: usize) -> String {
    if text.len() <= most {
        return text.to_string();
    }
    let mut end = most;
    while !text.is_char_boundary(end) {
        end -= 1;
    }
    format!(
        "{}\n[\u{2026} {} more bytes of this were cut when the conversation moved.]",
        &text[..end],
        text.len() - end
    )
}

/// A tool's use as a quoted block in the text, for a target that has no form of its own for
/// it: the tool's name, its input, and what it gave back, each line of the result quoted.
pub fn quoted(used: &Used) -> String {
    let status = if used.failed { " (an error)" } else { "" };
    let result: Vec<String> = used
        .result()
        .lines()
        .map(|line| format!("> {line}"))
        .collect();
    format!(
        "[Tool call: {}]\nInput: {}\nResult{status}:\n{}\n[End of tool call]",
        used.name,
        used.input_text(),
        result.join("\n")
    )
}

/// An answer's text with its tools' uses as quoted blocks after it ([`quoted`]).
pub fn with_quoted(said: &Said) -> String {
    let mut parts: Vec<String> = Vec::new();
    if !said.text.trim().is_empty() {
        parts.push(said.text.clone());
    }
    parts.extend(said.tools.iter().map(quoted));
    parts.join("\n\n")
}

/// Where a writer writes, and what it says of the session.
#[derive(Debug, Clone)]
pub struct Place {
    /// The target tool's own directory: Claude Code's configuration directory, Codex's home.
    /// [`tool_home`] says where the Host's turns find it.
    pub tool_home: PathBuf,
    /// The directory the agent works in: the tool files its sessions by it.
    pub cwd: String,
    pub now_ms: i64,
    /// The version of the tool, as it says it (`2.1.321`), when the Host knows it.
    pub tool_version: Option<String>,
    /// The model the agent runs on there, when one is chosen.
    pub model: Option<String>,
    /// The tool's program, for a tool a session is handed to (OpenCode's `import`).
    pub program: Option<PathBuf>,
}

/// What a writer did: the session it made, and what to remove to take it back.
#[derive(Debug)]
#[must_use = "a session that is not used is taken back with `undo`"]
pub struct Written {
    /// The tool's own name for the session: what its next turn resumes.
    pub session: String,
    /// How many things that were said it holds.
    pub carried: usize,
    files: Vec<PathBuf>,
    /// Directories it made for the session alone, removed once empty.
    dirs: Vec<PathBuf>,
    rows: Vec<(PathBuf, String)>,
}

impl Written {
    /// Removes what was written, as far as it can. Nothing of it is ever named to the tool.
    pub fn undo(self) {
        for (index, id) in &self.rows {
            if let Ok(db) = rusqlite::Connection::open(index) {
                let _ = db.busy_timeout(std::time::Duration::from_secs(5));
                let _ = db.execute("DELETE FROM threads WHERE id = ?1", [id]);
            }
        }
        for file in self.files.iter().rev() {
            let _ = std::fs::remove_file(file);
        }
        for dir in self.dirs.iter().rev() {
            let _ = std::fs::remove_dir(dir);
        }
    }

    /// The files it wrote, for a test or a log line.
    pub fn files(&self) -> &[PathBuf] {
        &self.files
    }
}

/// How a writer failed: in making the session, or in telling the tool of it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Failed {
    Translate(String),
    Register(String),
}

impl Failed {
    /// The stage a client is told.
    pub fn stage(&self) -> &'static str {
        match self {
            Failed::Translate(_) => "translate",
            Failed::Register(_) => "register",
        }
    }

    pub fn why(&self) -> &str {
        match self {
            Failed::Translate(why) | Failed::Register(why) => why,
        }
    }
}

impl std::fmt::Display for Failed {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.stage(), self.why())
    }
}

/// One target tool.
pub trait Writer: Send + Sync {
    fn backend(&self) -> &'static str;
    /// Writes `said` as a session of the tool and registers it where the tool looks for it.
    fn write(&self, said: &[Said], place: &Place) -> Result<Written, Failed>;
}

/// The writer of a tool, for a tool a conversation can be written into natively.
///
/// OpenCode has none for now: a session handed to its import is taken, but OpenCode's own
/// free models then refuse every turn on it ("Free usage limit reached") while they answer a
/// session OpenCode began itself (issue 720, measured with 1.18.33). Its next turn begins a
/// session of its own with the Host's record at its head, as Antigravity's does.
/// [`OpenCodeWriter`] stays, tested against a genuine export, for when that is understood.
pub fn writer(backend: &str) -> Option<&'static dyn Writer> {
    match backend {
        "claude" => Some(&ClaudeWriter),
        "codex" => Some(&CodexWriter),
        "grok" => Some(&GrokWriter),
        _ => None,
    }
}

/// Where the Host's turns find a tool's own directory, for a user whose home is `home`: as the
/// tool itself reads it, from the environment the turns inherit.
pub fn tool_home(backend: &str, home: &Path) -> PathBuf {
    let from = |name: &str, fallback: &str| {
        std::env::var_os(name)
            .filter(|dir| !dir.is_empty())
            .map_or_else(|| home.join(fallback), PathBuf::from)
    };
    match backend {
        "claude" => from("CLAUDE_CONFIG_DIR", ".claude"),
        "codex" => from("CODEX_HOME", ".codex"),
        "grok" => crate::past::native_home(clarp_kernel::grok_muse::Tool::Grok, home),
        _ => home.to_path_buf(),
    }
}

/// From which thing that was said on the conversation is carried: the newest that fit in
/// `most` signs together, and never one cut in half.
pub fn kept_from(said: &[Said], most: usize) -> usize {
    let mut size = 0usize;
    for (at, one) in said.iter().enumerate().rev() {
        size += one.size();
        if size > most {
            return at + 1;
        }
    }
    0
}

// ---- ids and moments ------------------------------------------------------------------------

fn random(count: usize) -> Result<Vec<u8>, Failed> {
    let mut bytes = vec![0u8; count];
    std::fs::File::open("/dev/urandom")
        .and_then(|mut source| source.read_exact(&mut bytes))
        .map_err(|why| Failed::Translate(format!("no random bytes: {why}")))?;
    Ok(bytes)
}

fn hyphenated(bytes: &[u8]) -> String {
    let h: String = bytes.iter().map(|byte| format!("{byte:02x}")).collect();
    format!(
        "{}-{}-{}-{}-{}",
        &h[0..8],
        &h[8..12],
        &h[12..16],
        &h[16..20],
        &h[20..32]
    )
}

/// A random UUID of version 4, as Claude Code names its sessions.
pub fn uuid4() -> Result<String, Failed> {
    let mut bytes = random(16)?;
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    Ok(hyphenated(&bytes))
}

/// A UUID of version 7, ordered by time, as Codex and Grok name theirs.
pub fn uuid7(now_ms: i64) -> Result<String, Failed> {
    let mut bytes = random(16)?;
    let ms = u64::try_from(now_ms.max(0)).unwrap_or(0).to_be_bytes();
    bytes[..6].copy_from_slice(&ms[2..]);
    bytes[6] = (bytes[6] & 0x0f) | 0x70;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    Ok(hyphenated(&bytes))
}

fn stamp(ms: i64) -> jiff::Timestamp {
    jiff::Timestamp::from_millisecond(ms).unwrap_or(jiff::Timestamp::UNIX_EPOCH)
}

/// A moment as the tools write it: `2026-11-22T20:15:07.123Z`.
fn iso(ms: i64) -> String {
    let at = stamp(ms);
    format!(
        "{}.{:03}Z",
        at.strftime("%Y-%m-%dT%H:%M:%S"),
        ms.rem_euclid(1000)
    )
}

/// The first line of what was first asked, as a title.
fn title(said: &[Said], most: usize) -> String {
    let first = said
        .iter()
        .find(|one| one.role == Role::User && !one.text.trim().is_empty())
        .map_or("Continued conversation", |one| one.text.as_str());
    let joined = first.split_whitespace().collect::<Vec<_>>().join(" ");
    joined.chars().take(most).collect()
}

/// Writes a new file whole or not at all: under a name of its own beside it first, then put
/// in place. A file that is there already is never written over.
fn write_new(path: &Path, body: &[u8]) -> Result<(), Failed> {
    let fail = |why: std::io::Error| Failed::Translate(format!("{}: {why}", path.display()));
    let dir = path
        .parent()
        .ok_or_else(|| Failed::Translate(format!("{} has no directory", path.display())))?;
    std::fs::create_dir_all(dir).map_err(fail)?;
    if path.exists() {
        return Err(Failed::Translate(format!("{} exists", path.display())));
    }
    let name = path
        .file_name()
        .map(|n| n.to_string_lossy())
        .unwrap_or_default();
    let aside = dir.join(format!(".{name}.carrying"));
    let mut file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&aside)
        .map_err(fail)?;
    let written = file.write_all(body).and_then(|()| file.sync_all());
    drop(file);
    if let Err(why) = written.and_then(|()| std::fs::hard_link(&aside, path)) {
        let _ = std::fs::remove_file(&aside);
        return Err(fail(why));
    }
    let _ = std::fs::remove_file(&aside);
    Ok(())
}

fn lines(rows: &[Value]) -> Vec<u8> {
    let mut body = Vec::new();
    for row in rows {
        body.extend_from_slice(row.to_string().as_bytes());
        body.push(b'\n');
    }
    body
}

fn spoken(said: &[Said]) -> impl Iterator<Item = &Said> {
    said.iter().filter(|one| one.carried())
}

// ---- Claude Code ----------------------------------------------------------------------------

/// Claude Code keeps a session as a file of lines under its project's directory,
/// `<config>/projects/<project of the cwd>/<session>.jsonl`, and resumes it by that name.
pub struct ClaudeWriter;

impl ClaudeWriter {
    /// The rows of a session `session` that holds `said`, in the shape Claude Code 2.1 writes.
    pub fn rows(said: &[Said], place: &Place, session: &str) -> Result<Vec<Value>, Failed> {
        let version = place.tool_version.clone().unwrap_or_default();
        let model = place.model.clone().unwrap_or_default();
        let mut rows = vec![
            json!({"type": "mode", "mode": "normal", "sessionId": session}),
            json!({"type": "permission-mode", "permissionMode": "bypassPermissions", "sessionId": session}),
        ];
        let mut parent = Value::Null;
        let mut calls = 0usize;
        for one in spoken(said) {
            let id = uuid4()?;
            let envelope = |kind: &str, id: &str, parent: &Value| {
                json!({
                    "parentUuid": parent,
                    "isSidechain": false,
                    "type": kind,
                    "uuid": id,
                    "timestamp": iso(one.at_ms),
                    "userType": "external",
                    "entrypoint": "sdk-cli",
                    "cwd": place.cwd,
                    "sessionId": session,
                    "version": version,
                    "gitBranch": "",
                })
            };
            let mut row = envelope(one.role.name(), &id, &parent);
            // The calls of an answer, each under the id its result names.
            let ids: Vec<String> = one
                .tools
                .iter()
                .map(|used| {
                    calls += 1;
                    used.call_id(calls)
                })
                .collect();
            match one.role {
                // Its content is a list of blocks. Claude Code looks into each block of an
                // answer (`"tool_use_id" in block`); a string it would walk sign by sign, and
                // die on the first. Its text comes first, then the tools it used.
                Role::Assistant => {
                    let mut content: Vec<Value> = Vec::new();
                    if !one.text.trim().is_empty() {
                        content.push(json!({"type": "text", "text": one.text}));
                    }
                    for (used, call) in one.tools.iter().zip(&ids) {
                        content.push(json!({
                            "type": "tool_use", "id": call, "name": used.tool_name(),
                            "input": used.input_object(),
                        }));
                    }
                    let stop = if one.tools.is_empty() {
                        "end_turn"
                    } else {
                        "tool_use"
                    };
                    row["message"] = json!({
                        "model": model,
                        "id": format!("msg_{}", id.replace('-', "")),
                        "type": "message",
                        "role": "assistant",
                        "content": content,
                        "stop_reason": stop,
                        "stop_sequence": null,
                        "usage": {
                            "input_tokens": 0,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0,
                            "output_tokens": 0,
                        },
                    });
                }
                // A user's words are a plain string.
                Role::User => {
                    row["message"] = json!({"role": "user", "content": one.text});
                    row["permissionMode"] = json!("bypassPermissions");
                    row["promptId"] = json!(uuid4()?);
                    row["promptSource"] = json!("sdk");
                }
            }
            parent = json!(id);
            rows.push(row);
            // What the tools gave back comes in the user's next message, as Claude Code
            // writes it: one block for each call, under the call's id.
            if !one.tools.is_empty() {
                let id = uuid4()?;
                let mut row = envelope("user", &id, &parent);
                let results: Vec<Value> = one
                    .tools
                    .iter()
                    .zip(&ids)
                    .map(|(used, call)| {
                        let is_error = used.failed || used.output.is_none();
                        let mut content = used.result();
                        // Claude rejects an empty failed tool result when it resumes the session.
                        if is_error && content.trim().is_empty() {
                            content = "[The tool failed without output.]".to_string();
                        }
                        json!({
                            "type": "tool_result", "tool_use_id": call,
                            "content": content,
                            "is_error": is_error,
                        })
                    })
                    .collect();
                row["message"] = json!({"role": "user", "content": results});
                parent = json!(id);
                rows.push(row);
            }
        }
        Ok(rows)
    }
}

impl Writer for ClaudeWriter {
    fn backend(&self) -> &'static str {
        "claude"
    }

    fn write(&self, said: &[Said], place: &Place) -> Result<Written, Failed> {
        let session = uuid4()?;
        let rows = Self::rows(said, place, &session)?;
        let project = crate::past::claude_project(&place.cwd);
        let file = place
            .tool_home
            .join("projects")
            .join(project)
            .join(format!("{session}.jsonl"));
        write_new(&file, &lines(&rows))?;
        Ok(Written {
            session,
            carried: spoken(said).count(),
            files: vec![file],
            dirs: Vec::new(),
            rows: Vec::new(),
        })
    }
}

// ---- Codex ----------------------------------------------------------------------------------

/// Codex keeps a session as a rollout, `<home>/sessions/<y>/<m>/<d>/rollout-<moment>-<id>.jsonl`,
/// and indexes it in its state database (`state_<n>.sqlite`, table `threads`).
pub struct CodexWriter;

/// The columns of Codex's index of threads that this Host fills. Codex adds columns from
/// version to version: one it requires and this Host does not know refuses the write.
const CODEX_COLUMNS: &[&str] = &[
    "id",
    "rollout_path",
    "created_at",
    "updated_at",
    "source",
    "model_provider",
    "cwd",
    "title",
    "sandbox_policy",
    "approval_mode",
    "has_user_event",
    "cli_version",
    "first_user_message",
    "preview",
    "model",
    "originator",
];

impl CodexWriter {
    /// The rollout of a thread `thread` that holds `said`.
    pub fn rows(said: &[Said], place: &Place, thread: &str) -> Vec<Value> {
        let now = iso(place.now_ms);
        let mut rows = vec![json!({
            "timestamp": now,
            "type": "session_meta",
            "payload": {
                "id": thread,
                "session_id": thread,
                "timestamp": now,
                "cwd": place.cwd,
                "originator": "clarp",
                "cli_version": place.tool_version.clone().unwrap_or_default(),
                "source": "exec",
                "model_provider": "openai",
            },
        })];
        let mut calls = 0usize;
        for one in spoken(said) {
            let part = match one.role {
                Role::User => "input_text",
                Role::Assistant => "output_text",
            };
            let item = |payload: Value| json!({"timestamp": iso(one.at_ms), "type": "response_item", "payload": payload});
            if !one.text.trim().is_empty() {
                rows.push(item(json!({
                    "type": "message",
                    "role": one.role.name(),
                    "content": [{"type": part, "text": one.text}],
                })));
            }
            // The tools an answer used, each a call and its output under the same id, as
            // Codex writes a function the model called.
            for used in &one.tools {
                calls += 1;
                let call = used.call_id(calls);
                rows.push(item(json!({
                    "type": "function_call", "name": used.tool_name(),
                    "arguments": used.input_object().to_string(), "call_id": call,
                })));
                rows.push(item(json!({
                    "type": "function_call_output", "call_id": call, "output": used.result(),
                })));
            }
        }
        rows
    }

    /// Codex's index: the state database of the highest number in its home, if it has one.
    pub fn index(home: &Path) -> Option<PathBuf> {
        let entries = std::fs::read_dir(home).ok()?;
        entries
            .filter_map(Result::ok)
            .filter_map(|entry| {
                let name = entry.file_name().to_string_lossy().into_owned();
                let number = name.strip_prefix("state_")?.strip_suffix(".sqlite")?;
                Some((number.parse::<u32>().ok()?, entry.path()))
            })
            .max_by_key(|(number, _)| *number)
            .map(|(_, path)| path)
    }

    fn register(
        index: &Path,
        thread: &str,
        rollout: &Path,
        said: &[Said],
        place: &Place,
    ) -> Result<(), Failed> {
        let fail = |why: rusqlite::Error| Failed::Register(format!("codex index: {why}"));
        let db = rusqlite::Connection::open(index).map_err(fail)?;
        db.busy_timeout(std::time::Duration::from_secs(5))
            .map_err(fail)?;
        // (name, required without a default)
        let mut columns: Vec<(String, bool)> = Vec::new();
        {
            let mut asked = db
                .prepare("SELECT name, \"notnull\", dflt_value FROM pragma_table_info('threads')")
                .map_err(fail)?;
            let mut found = asked.query([]).map_err(fail)?;
            while let Some(row) = found.next().map_err(fail)? {
                let name: String = row.get(0).map_err(fail)?;
                let not_null: i64 = row.get(1).map_err(fail)?;
                let default: Option<String> = row.get(2).map_err(fail)?;
                columns.push((name, not_null != 0 && default.is_none()));
            }
        }
        if columns.is_empty() {
            return Err(Failed::Register(
                "codex index has no table of threads".to_string(),
            ));
        }
        if let Some((name, _)) = columns.iter().find(|(name, required)| {
            *required && name != "id" && !CODEX_COLUMNS.contains(&name.as_str())
        }) {
            return Err(Failed::Register(format!(
                "codex index requires a column this Host does not know: {name}"
            )));
        }
        let seconds = place.now_ms.div_euclid(1000);
        let first = title(said, 120);
        let value = |name: &str| -> rusqlite::types::Value {
            use rusqlite::types::Value as V;
            match name {
                "id" => V::Text(thread.to_string()),
                "rollout_path" => V::Text(rollout.display().to_string()),
                "created_at" | "updated_at" => V::Integer(seconds),
                "source" => V::Text("exec".into()),
                "model_provider" => V::Text("openai".into()),
                "cwd" => V::Text(place.cwd.clone()),
                "title" | "first_user_message" | "preview" => V::Text(first.clone()),
                "sandbox_policy" => V::Text(r#"{"type":"disabled"}"#.into()),
                "approval_mode" => V::Text("never".into()),
                "has_user_event" => V::Integer(1),
                "cli_version" => V::Text(place.tool_version.clone().unwrap_or_default()),
                "model" => place.model.clone().map_or(V::Null, V::Text),
                "originator" => V::Text("clarp".into()),
                _ => V::Null,
            }
        };
        let present: Vec<&str> = CODEX_COLUMNS
            .iter()
            .copied()
            .filter(|known| columns.iter().any(|(name, _)| name == known))
            .collect();
        let sql = format!(
            "INSERT INTO threads ({}) VALUES ({})",
            present.join(", "),
            (1..=present.len())
                .map(|at| format!("?{at}"))
                .collect::<Vec<_>>()
                .join(", ")
        );
        let values: Vec<rusqlite::types::Value> = present.iter().map(|name| value(name)).collect();
        db.execute(&sql, rusqlite::params_from_iter(values))
            .map_err(fail)?;
        Ok(())
    }
}

impl Writer for CodexWriter {
    fn backend(&self) -> &'static str {
        "codex"
    }

    fn write(&self, said: &[Said], place: &Place) -> Result<Written, Failed> {
        let thread = uuid7(place.now_ms)?;
        let rows = Self::rows(said, place, &thread);
        // Codex names its rollouts by the moment on the machine's clock.
        let local = stamp(place.now_ms).to_zoned(jiff::tz::TimeZone::system());
        let file = place
            .tool_home
            .join("sessions")
            .join(local.strftime("%Y").to_string())
            .join(local.strftime("%m").to_string())
            .join(local.strftime("%d").to_string())
            .join(format!(
                "rollout-{}-{thread}.jsonl",
                local.strftime("%Y-%m-%dT%H-%M-%S")
            ));
        write_new(&file, &lines(&rows))?;
        let mut written = Written {
            session: thread.clone(),
            carried: spoken(said).count(),
            files: vec![file.clone()],
            dirs: Vec::new(),
            rows: Vec::new(),
        };
        if let Some(index) = Self::index(&place.tool_home) {
            if let Err(failed) = Self::register(&index, &thread, &file, said, place) {
                written.undo();
                return Err(failed);
            }
            written.rows.push((index, thread));
        }
        Ok(written)
    }
}

// ---- Grok -----------------------------------------------------------------------------------

/// Grok keeps a session as a directory, `<home>/sessions/<the cwd, URL-encoded>/<id>/`, with
/// its chat history and a summary it indexes sessions by, and resumes it by its id from that
/// directory (`grok --resume <id>`).
pub struct GrokWriter;

/// A path as Grok names its directory: every byte but letters, digits and `_.-~` encoded.
pub fn url_encoded(text: &str) -> String {
    let mut out = String::with_capacity(text.len());
    for byte in text.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'.' | b'-' | b'~') {
            out.push(char::from(byte));
        } else {
            out.push_str(&format!("%{byte:02X}"));
        }
    }
    out
}

impl GrokWriter {
    /// The model Grok's summary names: the agent's, else that of the newest session Grok
    /// keeps, else the first the Host knows of Grok. Grok refuses a session whose summary
    /// names none ("Session does not exist").
    fn model(place: &Place) -> String {
        if let Some(model) = place.model.clone().filter(|model| !model.is_empty()) {
            return model;
        }
        let mut newest: Option<(String, String)> = None;
        let sessions = place.tool_home.join("sessions");
        for dir in std::fs::read_dir(&sessions).into_iter().flatten().flatten() {
            for session in std::fs::read_dir(dir.path())
                .into_iter()
                .flatten()
                .flatten()
            {
                let Ok(text) = std::fs::read_to_string(session.path().join("summary.json")) else {
                    continue;
                };
                let Ok(summary) = serde_json::from_str::<Value>(&text) else {
                    continue;
                };
                let (Some(model), at) = (
                    summary["current_model_id"]
                        .as_str()
                        .filter(|m| !m.is_empty()),
                    summary["last_active_at"].as_str().unwrap_or_default(),
                ) else {
                    continue;
                };
                if newest.as_ref().is_none_or(|(when, _)| at > when.as_str()) {
                    newest = Some((at.to_string(), model.to_string()));
                }
            }
        }
        newest.map(|(_, model)| model).unwrap_or_else(|| {
            crate::catalogue::declared("grok")["models"][0]["id"]
                .as_str()
                .unwrap_or("grok-build")
                .to_string()
        })
    }

    /// The rows of Grok's chat history for `said`, and how many prompts it holds.
    pub fn rows(said: &[Said]) -> (Vec<Value>, usize) {
        let mut rows = Vec::new();
        let mut prompts = 0usize;
        for one in spoken(said) {
            match one.role {
                Role::User => {
                    rows.push(json!({
                        "type": "user",
                        "prompt_index": prompts,
                        "content": [{"type": "text", "text": format!("<user_query>\n{}\n</user_query>", one.text)}],
                    }));
                    prompts += 1;
                }
                // Grok's history has no form of its own for a tool's use that this Host
                // knows to be safe: they come as quoted blocks after the answer's text.
                Role::Assistant => {
                    rows.push(json!({"type": "assistant", "content": with_quoted(one)}));
                }
            }
        }
        (rows, prompts)
    }
}

impl Writer for GrokWriter {
    fn backend(&self) -> &'static str {
        "grok"
    }

    fn write(&self, said: &[Said], place: &Place) -> Result<Written, Failed> {
        let session = uuid7(place.now_ms)?;
        let dir = place
            .tool_home
            .join("sessions")
            .join(url_encoded(&place.cwd))
            .join(&session);
        let (rows, prompts) = Self::rows(said);
        let now = iso(place.now_ms);
        let started = said
            .first()
            .map_or_else(|| now.clone(), |one| iso(one.at_ms));
        let summary = json!({
            "info": {"id": session, "cwd": place.cwd},
            "session_summary": title(said, 80),
            "generated_title": title(said, 80),
            "created_at": started,
            "updated_at": now,
            "last_active_at": now,
            "num_messages": rows.len(),
            "num_chat_messages": rows.len(),
            "next_trace_turn": prompts,
            "chat_format_version": 1,
            "current_model_id": Self::model(place),
            "grok_home": place.tool_home.display().to_string(),
        });
        let mut written = Written {
            session: session.clone(),
            carried: rows.len(),
            files: Vec::new(),
            dirs: vec![dir.clone()],
            rows: Vec::new(),
        };
        let mut put = |name: &str, body: &[u8]| -> Result<(), Failed> {
            let file = dir.join(name);
            write_new(&file, body)?;
            written.files.push(file);
            Ok(())
        };
        let made = put("chat_history.jsonl", &lines(&rows))
            .and_then(|()| {
                let body = serde_json::to_vec_pretty(&summary)
                    .map_err(|why| Failed::Translate(why.to_string()))?;
                put("summary.json", &body)
            })
            .and_then(|()| {
                for name in [
                    "chat_history.jsonl.lock",
                    "rewind_points.jsonl",
                    "rewind_points.jsonl.lock",
                    "summary.json.lock",
                    "updates.jsonl",
                    "updates.jsonl.lock",
                ] {
                    put(name, b"")?;
                }
                Ok(())
            });
        if let Err(failed) = made {
            written.undo();
            return Err(failed);
        }
        Ok(written)
    }
}

// ---- OpenCode -------------------------------------------------------------------------------

/// OpenCode keeps its sessions in a database of its own. Nothing here writes it: a session is
/// handed to OpenCode's own `opencode import <file>`, in the shape `opencode export` writes.
pub struct OpenCodeWriter;

const BASE62: &[u8; 62] = b"0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz";

/// An OpenCode id: its prefix, twelve hex signs of the moment (so ids sort), fourteen random
/// signs.
pub fn opencode_id(prefix: &str, ms: i64, seq: u64) -> Result<String, Failed> {
    let moment = (u64::try_from(ms.max(0))
        .unwrap_or(0)
        .wrapping_mul(4096)
        .wrapping_add(seq))
        & 0xFFFF_FFFF_FFFF;
    let tail: String = random(14)?
        .iter()
        .map(|byte| char::from(BASE62[usize::from(*byte) % 62]))
        .collect();
    Ok(format!("{prefix}_{moment:012x}{tail}"))
}

impl OpenCodeWriter {
    /// The session `session` as `opencode export` writes one.
    pub fn export(said: &[Said], place: &Place, session: &str) -> Result<Value, Failed> {
        let mut messages = Vec::new();
        // The user's message an answer answers: OpenCode's import refuses an answer without
        // one (`parentID`), as every answer of a real export has.
        let mut asked: Option<String> = None;
        for (index, one) in spoken(said).enumerate() {
            if one.role == Role::Assistant && asked.is_none() {
                // An answer before anything was asked has nothing to answer, in OpenCode.
                continue;
            }
            let seq = u64::try_from(index).unwrap_or(0) * 256; // room for its parts
            let message = opencode_id("msg", one.at_ms, seq)?;
            let mut info = json!({
                "id": message, "sessionID": session, "role": one.role.name(), "agent": "build",
            });
            match one.role {
                Role::Assistant => {
                    info["parentID"] = json!(asked);
                    info["time"] = json!({"created": one.at_ms, "completed": one.at_ms});
                    info["mode"] = json!("build");
                    info["path"] = json!({"cwd": place.cwd, "root": place.cwd});
                    info["cost"] = json!(0);
                    info["tokens"] = json!({"input": 0, "output": 0, "reasoning": 0,
                                            "cache": {"read": 0, "write": 0}});
                    info["modelID"] = json!("imported");
                    info["providerID"] = json!("clarp");
                    info["finish"] = json!("stop");
                }
                Role::User => {
                    asked = Some(message.clone());
                    info["time"] = json!({"created": one.at_ms});
                    info["summary"] = json!({"diffs": []});
                    info["model"] = json!({"providerID": "clarp", "modelID": "imported"});
                }
            }
            let mut parts = Vec::new();
            if !one.text.trim().is_empty() {
                parts.push(json!({
                    "id": opencode_id("prt", one.at_ms, seq + 1)?,
                    "sessionID": session, "messageID": message, "type": "text", "text": one.text,
                }));
            }
            // The tools an answer used, each a part of it as OpenCode keeps a tool's use: the
            // call's id, the tool, and its state when it ended.
            for (at, used) in one.tools.iter().enumerate() {
                let part = u64::try_from(at).unwrap_or(0) + 2;
                let state = if used.failed || used.output.is_none() {
                    json!({"status": "error", "input": used.input_object(), "error": used.result(),
                           "time": {"start": one.at_ms, "end": one.at_ms}})
                } else {
                    json!({"status": "completed", "input": used.input_object(),
                           "output": used.result(), "title": used.name, "metadata": {},
                           "time": {"start": one.at_ms, "end": one.at_ms}})
                };
                parts.push(json!({
                    "id": opencode_id("prt", one.at_ms, seq + part)?,
                    "sessionID": session, "messageID": message, "type": "tool",
                    "callID": used.call_id(at + 1), "tool": used.tool_name(), "state": state,
                }));
            }
            messages.push(json!({"info": info, "parts": parts}));
        }
        let started = said.first().map_or(place.now_ms, |one| one.at_ms);
        let slug: String = session
            .chars()
            .rev()
            .take(6)
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        Ok(json!({
            "info": {
                "id": session, "slug": format!("clarp-{}", slug.to_ascii_lowercase()),
                "projectID": "global", "directory": place.cwd, "title": title(said, 80),
                "version": "clarp", "summary": {"additions": 0, "deletions": 0, "files": 0},
                "time": {"created": started, "updated": place.now_ms},
            },
            "messages": messages,
        }))
    }
}

impl Writer for OpenCodeWriter {
    fn backend(&self) -> &'static str {
        "opencode"
    }

    fn write(&self, said: &[Said], place: &Place) -> Result<Written, Failed> {
        let program = place
            .program
            .clone()
            .ok_or_else(|| Failed::Register("opencode is not installed".to_string()))?;
        let session = opencode_id("ses", place.now_ms, 0)?;
        let export = Self::export(said, place, &session)?;
        let carried = export["messages"].as_array().map_or(0, Vec::len);
        let file = std::env::temp_dir().join(format!("clarp-carry-{}.json", uuid4()?));
        let body = serde_json::to_vec(&export).map_err(|why| Failed::Translate(why.to_string()))?;
        write_new(&file, &body)?;
        let ran = run_bounded(
            clarp_child::command(&program)
                .arg("import")
                .arg(&file)
                .current_dir(if Path::new(&place.cwd).is_dir() {
                    place.cwd.as_str()
                } else {
                    "/"
                }),
            IMPORT_MOST,
        );
        let _ = std::fs::remove_file(&file);
        match ran {
            Ok(done) if done.status.success() => Ok(Written {
                session,
                carried,
                files: Vec::new(),
                dirs: Vec::new(),
                rows: Vec::new(),
            }),
            Ok(done) => {
                let said = String::from_utf8_lossy(&done.stderr);
                let line = said
                    .lines()
                    .rev()
                    .find(|line| !line.trim().is_empty())
                    .unwrap_or("");
                Err(Failed::Register(format!(
                    "opencode import ended with {}: {}",
                    done.status,
                    line.chars().take(300).collect::<String>()
                )))
            }
            Err(why) => Err(Failed::Register(format!("opencode import: {why}"))),
        }
    }
}

/// How long OpenCode's import may take.
const IMPORT_MOST: std::time::Duration = std::time::Duration::from_secs(60);

/// Runs a program with nothing on its input and nothing kept of its output but its errors, and
/// ends it when it takes longer than `most`.
fn run_bounded(
    command: &mut std::process::Command,
    most: std::time::Duration,
) -> std::io::Result<std::process::Output> {
    let mut child = command
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::piped())
        .spawn()?;
    let until = std::time::Instant::now() + most;
    loop {
        if child.try_wait()?.is_some() {
            return child.wait_with_output();
        }
        if std::time::Instant::now() >= until {
            let _ = child.kill();
            let _ = child.wait();
            return Err(std::io::Error::new(
                std::io::ErrorKind::TimedOut,
                format!("took longer than {} s", most.as_secs()),
            ));
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn temp(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!(
            "clarp-transit-{name}-{}-{}",
            std::process::id(),
            uuid4().unwrap()
        ));
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn said() -> Vec<Said> {
        vec![
            Said::new(Role::User, "Fix the parser, please.", 1_800_000_000_000),
            Said::new(
                Role::Assistant,
                "Done: the parser reads both shapes now.",
                1_800_000_001_000,
            ),
            Said::new(Role::User, "   ", 1_800_000_002_000),
            Said::new(Role::User, "And the tests?", 1_800_000_003_000),
        ]
    }

    fn place(home: &Path) -> Place {
        Place {
            tool_home: home.to_path_buf(),
            cwd: "/home/someone/work/repo.x".into(),
            now_ms: 1_800_000_010_000,
            tool_version: Some("2.1.321".into()),
            model: Some("claude-opus-5-5".into()),
            program: None,
        }
    }

    fn read_lines(path: &Path) -> Vec<Value> {
        std::fs::read_to_string(path)
            .unwrap()
            .lines()
            .map(|line| serde_json::from_str(line).unwrap())
            .collect()
    }

    fn used(id: &str, output: Option<&str>) -> Used {
        Used {
            id: id.into(),
            name: "Bash".into(),
            input: json!({"command": "cat notes.txt"}),
            output: output.map(str::to_string),
            failed: false,
            shortened: false,
            session: None,
        }
    }

    /// A conversation in which the agent used tools: one in its answer, one by itself after
    /// it, and one whose turn was cut off.
    fn said_with_tools() -> Vec<Said> {
        let mut said = said();
        said[1].tools = vec![used("toolu_01", Some("line one\nline two"))];
        let mut cut_off = Said::new(Role::Assistant, "", 1_800_000_004_000);
        cut_off.tools = vec![used("c-codex-item 7", None)];
        said.push(cut_off);
        said
    }

    #[test]
    fn a_long_result_is_cut_with_how_much_was() {
        assert_eq!(cut("short", RESULT_MOST), "short");
        let long = "é".repeat(RESULT_MOST); // two bytes each
        let carried = cut(&long, RESULT_MOST);
        let (kept, marker) = carried.split_once('\n').unwrap();
        assert_eq!(kept.len(), RESULT_MOST);
        assert_eq!(
            marker,
            format!(
                "[\u{2026} {} more bytes of this were cut when the conversation moved.]",
                RESULT_MOST
            )
        );
        // A sign is never cut in half.
        let odd = format!("a{}", "é".repeat(RESULT_MOST));
        assert_eq!(
            cut(&odd, RESULT_MOST).split_once('\n').unwrap().0.len(),
            RESULT_MOST - 1
        );
        let mut shortened = used("x", Some(&format!("{}\u{2026}", "a".repeat(10))));
        shortened.shortened = true;
        assert!(shortened.result().ends_with("the rest was not carried.]"));
        assert!(!shortened.result().contains('\u{2026}'));
        assert!(used("x", None).result().contains("never returned"));
    }

    #[test]
    fn what_tools_take_is_carried_in_the_budget() {
        let said = said_with_tools();
        let tools = said[1].size() - said[1].text.chars().count();
        assert!(tools > 0);
        assert_eq!(
            said[4].size(),
            said[4].tools[0].name.len()
                + said[4].tools[0].input_text().len()
                + said[4].tools[0].result().len()
        );
    }

    #[test]
    fn claude_gets_the_calls_as_its_own_blocks() {
        let rows = ClaudeWriter::rows(&said_with_tools(), &place(Path::new("/x")), "s").unwrap();
        let turns = &rows[2..];
        // user, answer with its call, the result, user, user, a call by itself, its result.
        let kinds: Vec<&str> = turns
            .iter()
            .map(|row| row["type"].as_str().unwrap())
            .collect();
        assert_eq!(
            kinds,
            ["user", "assistant", "user", "user", "assistant", "user"]
        );
        let answer = &turns[1]["message"];
        assert_eq!(answer["stop_reason"], "tool_use");
        assert_eq!(answer["content"][0]["type"], "text");
        let call = &answer["content"][1];
        assert_eq!(call["type"], "tool_use");
        assert_eq!(call["id"], "toolu_01");
        assert_eq!(call["name"], "Bash");
        assert_eq!(call["input"]["command"], "cat notes.txt");
        let result = &turns[2]["message"]["content"][0];
        assert_eq!(result["type"], "tool_result");
        assert_eq!(result["tool_use_id"], "toolu_01");
        assert_eq!(result["content"], "line one\nline two");
        assert_eq!(result["is_error"], false);
        assert_eq!(turns[2]["parentUuid"], turns[1]["uuid"]);
        // An answer of no words is its call alone; a call that never returned is an error.
        let alone = &turns[4]["message"]["content"];
        assert_eq!(alone.as_array().unwrap().len(), 1);
        assert_eq!(alone[0]["id"], "c-codex-item_7", "an id Claude takes");
        assert_eq!(
            turns[5]["message"]["content"][0]["tool_use_id"],
            "c-codex-item_7"
        );
        assert_eq!(turns[5]["message"]["content"][0]["is_error"], true);
    }

    #[test]
    fn claude_gets_nonempty_content_for_a_failed_tool_without_output() {
        let mut said = said_with_tools();
        let mut failed = used("toolu_empty_error", Some(""));
        failed.failed = true;
        said[1]
            .tools
            .extend([failed, used("toolu_empty_success", Some(""))]);

        let rows = ClaudeWriter::rows(&said, &place(Path::new("/x")), "s").unwrap();
        let calls = &rows[3]["message"]["content"];
        let results = &rows[4]["message"]["content"];
        let error = &results[1];
        assert_eq!(error["tool_use_id"], calls[2]["id"]);
        assert_eq!(error["is_error"], true);
        assert!(!error["content"].as_str().unwrap().trim().is_empty());
        let success = &results[2];
        assert_eq!(success["tool_use_id"], calls[3]["id"]);
        assert_eq!(success["is_error"], false);
        assert_eq!(success["content"], "");
    }

    #[test]
    fn codex_gets_the_calls_as_function_calls_with_their_output() {
        let rows = CodexWriter::rows(&said_with_tools(), &place(Path::new("/x")), "t");
        let items: Vec<&Value> = rows[1..].iter().map(|row| &row["payload"]).collect();
        let kinds: Vec<&str> = items
            .iter()
            .map(|item| item["type"].as_str().unwrap())
            .collect();
        assert_eq!(
            kinds,
            [
                "message",
                "message",
                "function_call",
                "function_call_output",
                "message",
                "function_call",
                "function_call_output"
            ]
        );
        assert_eq!(items[2]["name"], "Bash");
        assert_eq!(items[2]["call_id"], "toolu_01");
        let arguments: Value =
            serde_json::from_str(items[2]["arguments"].as_str().unwrap()).unwrap();
        assert_eq!(arguments["command"], "cat notes.txt");
        assert_eq!(items[3]["call_id"], "toolu_01");
        assert_eq!(items[3]["output"], "line one\nline two");
    }

    #[test]
    fn a_call_id_too_long_for_the_target_is_made_short_and_stays_paired() {
        let long = format!(
            "c-clarp-admin-{}-exec-{}",
            "0123456789abcdef", "6f1c2d3e-4a5b-4c6d-8e7f-90a1b2c3d4e5"
        );
        assert!(long.len() > 70);
        let carried = used(&long, Some("done")).call_id(1);
        assert!(carried.len() <= CALL_ID_MOST, "{carried}");
        assert!(carried.starts_with("call_"));
        // The same on every call, and not the same for another long id.
        assert_eq!(carried, used(&long, None).call_id(9));
        assert_eq!(
            carried,
            "call_".to_string() + &format!("{:x}", Sha256::digest(long.as_bytes()))[..32]
        );
        assert_ne!(carried, used(&format!("{long}x"), None).call_id(1));
        // An id that fits stays as it is.
        let fits = "a".repeat(CALL_ID_MOST);
        assert_eq!(used(&fits, None).call_id(1), fits);

        let mut said = said();
        said[1].tools = vec![used(&long, Some("line one"))];
        let rows = CodexWriter::rows(&said, &place(Path::new("/x")), "t");
        let ids: Vec<&Value> = rows
            .iter()
            .filter(|row| row["payload"]["call_id"].is_string())
            .map(|row| &row["payload"]["call_id"])
            .collect();
        assert_eq!(ids.len(), 2, "a call and its output");
        assert_eq!(ids[0], ids[1]);
        assert_eq!(ids[0], carried.as_str());

        let rows = ClaudeWriter::rows(&said, &place(Path::new("/x")), "s").unwrap();
        let call = rows.iter().find_map(|row| {
            row["message"]["content"]
                .as_array()?
                .iter()
                .find(|b| b["type"] == "tool_use")
                .cloned()
        });
        let result = rows.iter().find_map(|row| {
            row["message"]["content"]
                .as_array()?
                .iter()
                .find(|b| b["type"] == "tool_result")
                .cloned()
        });
        assert_eq!(call.unwrap()["id"], carried.as_str());
        assert_eq!(result.unwrap()["tool_use_id"], carried.as_str());

        let export = OpenCodeWriter::export(&said, &place(Path::new("/x")), "ses_x").unwrap();
        assert_eq!(
            export["messages"][1]["parts"][1]["callID"],
            carried.as_str()
        );
    }

    #[test]
    fn grok_gets_the_calls_as_quoted_blocks() {
        let (rows, _) = GrokWriter::rows(&said_with_tools());
        let answer = rows[1]["content"].as_str().unwrap();
        assert_eq!(
            answer,
            "Done: the parser reads both shapes now.\n\n[Tool call: Bash]\n\
             Input: {\"command\":\"cat notes.txt\"}\nResult:\n> line one\n> line two\n\
             [End of tool call]"
        );
        let alone = rows.last().unwrap()["content"].as_str().unwrap();
        assert!(alone.starts_with("[Tool call: Bash]"), "{alone}");
    }

    #[test]
    fn opencode_gets_the_calls_as_tool_parts() {
        let export =
            OpenCodeWriter::export(&said_with_tools(), &place(Path::new("/x")), "ses_x").unwrap();
        let answer = &export["messages"][1]["parts"];
        assert_eq!(answer[0]["type"], "text");
        let tool = &answer[1];
        assert_eq!(tool["type"], "tool");
        assert_eq!(tool["callID"], "toolu_01");
        assert_eq!(tool["tool"], "Bash");
        assert_eq!(tool["state"]["status"], "completed");
        assert_eq!(tool["state"]["output"], "line one\nline two");
        assert_eq!(tool["state"]["input"]["command"], "cat notes.txt");
        // Its parts sort after its text and before the next message.
        let ids: Vec<&str> = export["messages"]
            .as_array()
            .unwrap()
            .iter()
            .flat_map(|message| {
                std::iter::once(message["info"]["id"].as_str().unwrap()).chain(
                    message["parts"]
                        .as_array()
                        .unwrap()
                        .iter()
                        .map(|p| p["id"].as_str().unwrap()),
                )
            })
            .map(|id| &id[4..16])
            .collect();
        let mut sorted = ids.clone();
        sorted.sort_unstable();
        assert_eq!(ids, sorted);
        let cut_off = &export["messages"].as_array().unwrap().last().unwrap()["parts"][0];
        assert_eq!(cut_off["state"]["status"], "error");
    }

    #[test]
    fn claude_transcripts_give_the_whole_of_what_the_host_shortened() {
        let projects = temp("whole");
        let session = uuid4().unwrap();
        let cwd = "/home/someone/work/repo.x";
        let dir = projects.join(crate::past::claude_project(cwd));
        std::fs::create_dir_all(&dir).unwrap();
        let whole = "w".repeat(RESULT_MOST + 100);
        let rows = [
            json!({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "toolu_long"}]}}),
            json!({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_long", "content": whole}]}}),
            json!({"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "toolu_pic", "content": [
                    {"type": "image", "source": {"type": "base64", "data": "aGk="}}]}]}}),
        ];
        std::fs::write(dir.join(format!("{session}.jsonl")), lines(&rows)).unwrap();
        let mut long = used("toolu_long", Some(&format!("{}\u{2026}", "w".repeat(4000))));
        long.shortened = true;
        long.session = Some(session.clone());
        let mut picture = used("toolu_pic", Some(""));
        picture.input = json!({"file_path": "/work/shot.png"});
        picture.session = Some(session.clone());
        let mut elsewhere = used("toolu_gone", Some(&format!("{}\u{2026}", "g".repeat(4000))));
        elsewhere.shortened = true;
        elsewhere.session = Some(uuid4().unwrap());
        let mut said = vec![Said::new(Role::Assistant, "Looked.", 1)];
        said[0].tools = vec![long, picture, elsewhere];
        whole_results(&mut said, &projects, cwd);
        let [long, picture, elsewhere] = &said[0].tools[..] else {
            panic!("three calls");
        };
        assert!(!long.shortened);
        assert_eq!(long.output.as_deref(), Some(whole.as_str()));
        assert!(
            long.result()
                .ends_with("100 more bytes of this were cut when the conversation moved.]")
        );
        assert_eq!(
            picture.output.as_deref(),
            Some("[A picture, not carried: it is the file /work/shot.png.]")
        );
        assert!(
            elsewhere.shortened,
            "a session Claude does not have leaves the record's"
        );
        std::fs::remove_dir_all(projects).unwrap();
    }

    #[test]
    fn a_target_is_given_about_sixty_percent_of_its_model() {
        assert_eq!(budget("claude", "opus"), 200_000 * 6 / 10 * 3);
        assert_eq!(budget("claude", "opus[1m]"), 1_000_000 * 6 / 10 * 3);
        assert_eq!(budget("codex", "gpt-5.5"), 272_000 * 6 / 10 * 3);
        assert!(budget("opencode", "anything") < budget("grok", "grok-4.6"));
    }

    fn long_said() -> Vec<Said> {
        (0..10)
            .map(|at| {
                let role = if at % 2 == 0 {
                    Role::User
                } else {
                    Role::Assistant
                };
                Said::new(role, format!("{at}:{}", "x".repeat(9_998)), at)
            })
            .collect()
    }

    #[test]
    fn what_fits_goes_whole_and_nothing_stands_at_its_head() {
        let fitted = fitted(&long_said(), 1_000_000, None, "Claude Code", "Codex");
        assert_eq!(
            fitted,
            Fitted {
                from: 0,
                head: None,
                summarized: false
            }
        );
        assert_eq!(with_head(&long_said(), &fitted), long_said());
    }

    #[test]
    fn what_does_not_fit_is_summarized_at_the_head_within_the_budget() {
        let said = long_said();
        let budget = 60_000;
        let summary = Summary {
            text: "The user wants the parser fixed.".into(),
            by: SummaryBy::Agent,
        };
        let fitted = fitted(&said, budget, Some(&summary), "Claude Code", "Codex");
        assert!(fitted.summarized);
        // Room is kept for the summary: what goes as it was is the newest that fit beside it.
        assert_eq!(fitted.from, kept_from(&said, budget - SUMMARY_MOST - 1_000));
        let head = fitted.head.clone().unwrap();
        assert!(head.starts_with(&format!(
            "[Clarp: this conversation moved from Claude Code to Codex. Its {} oldest turns",
            fitted.from
        )));
        assert!(head.contains("written by the agent itself, just before the move:]"));
        assert!(head.ends_with("The user wants the parser fixed."));
        let carried = with_head(&said, &fitted);
        assert_eq!(carried[0].role, Role::User);
        assert_eq!(carried[0].text, head);
        assert_eq!(&carried[1..], &said[fitted.from..]);
        assert!(carried.iter().map(Said::size).sum::<usize>() <= budget);

        // No summary: a note that the oldest were left out.
        let left = super::fitted(&said, budget, None, "Claude Code", "Codex");
        assert!(!left.summarized);
        assert_eq!(left.from, fitted.from);
        assert!(
            left.head
                .unwrap()
                .contains("were left out: no summary of them could be written")
        );
        let by_model = Summary {
            text: "x".repeat(SUMMARY_MOST * 2),
            by: SummaryBy::Model("Claude Code (haiku)".into()),
        };
        let cut = super::fitted(&said, budget, Some(&by_model), "Claude Code", "Codex");
        let head = cut.head.unwrap();
        assert!(head.contains("written by Claude Code (haiku), from Clarp's record:]"));
        assert!(
            head.chars().count() < SUMMARY_MOST + 1_000,
            "a long summary is cut"
        );
    }

    #[test]
    fn another_model_reads_the_end_of_the_older_part() {
        let said = long_said();
        let (instructions, asked, schema) = summary_reading(&said, 25_000);
        assert_eq!(instructions, SUMMARY_ASK);
        assert!(asked.contains("Assistant: 9:x") && asked.contains("User: 8:x"));
        assert!(!asked.contains("7:x"), "only what fits, newest kept");
        let schema: Value = serde_json::from_str(&schema).unwrap();
        assert_eq!(schema["required"][0], "summary");
        let turn = summary_turn("Codex", "Fix   the\nparser, please.");
        assert!(turn.contains("move to Codex"));
        assert!(turn.contains("\u{201c}Fix the parser, please.\u{201d}"));
        assert!(turn.contains("Do not use any tool"));
    }

    /// Writes, with the writers themselves, a Claude session and a Codex rollout in which a
    /// tool returned a code word that no text holds, followed by another prompt of the user's:
    /// what a live resume is to read back (issue 720). Only when asked:
    /// `CLARP_BUILD_ENV="CLARP_TRANSIT_PROOF_DIR=/dir CLARP_TRANSIT_PROOF_WORD=word
    /// CLARP_TRANSIT_PROOF_CWD=/cwd" tools/build test -p clarp-backend proof -- --ignored`.
    #[test]
    #[ignore = "writes sessions for a live resume"]
    fn proof_sessions_for_a_live_resume() {
        let dir = PathBuf::from(std::env::var("CLARP_TRANSIT_PROOF_DIR").unwrap());
        let word = std::env::var("CLARP_TRANSIT_PROOF_WORD").unwrap();
        let cwd = std::env::var("CLARP_TRANSIT_PROOF_CWD").unwrap();
        let mut said = vec![
            Said::new(
                Role::User,
                "Please look at the note file in this folder.",
                1_800_000_000_000,
            ),
            Said::new(Role::Assistant, "I'll read it.", 1_800_000_001_000),
            Said::new(
                Role::User,
                "Thanks. Keep what it says in mind.",
                1_800_000_003_000,
            ),
            Said::new(Role::Assistant, "Noted.", 1_800_000_004_000),
        ];
        said[1].tools = vec![Used {
            id: "toolu_01ProofRead".into(),
            name: "Read".into(),
            input: json!({"file_path": format!("{cwd}/note.txt")}),
            output: Some(format!("The code word is {word}.")),
            failed: false,
            shortened: false,
            session: None,
        }];
        for (backend, writer) in [
            ("claude", &ClaudeWriter as &dyn Writer),
            ("codex", &CodexWriter),
        ] {
            let mut place = place(&dir.join(backend));
            place.cwd = cwd.clone();
            place.model = None;
            place.tool_version = Some(
                if backend == "claude" {
                    "2.1.321"
                } else {
                    "0.167.1"
                }
                .into(),
            );
            let written = writer.write(&said, &place).unwrap();
            println!("{backend} session {}", written.session);
        }
    }

    #[test]
    fn ids_have_their_versions() {
        let four = uuid4().unwrap();
        assert_eq!(four.len(), 36);
        assert_eq!(&four[14..15], "4");
        assert!(matches!(&four[19..20], "8" | "9" | "a" | "b"));
        let seven = uuid7(1_800_000_000_000).unwrap();
        assert_eq!(&seven[14..15], "7");
        // The time leads: 1_800_000_000_000 ms is 0x01a3185c5000.
        assert_eq!(&seven[..13], "01a3185c-5000");
        assert!(clarp_kernel::native_session_name(&four));
    }

    #[test]
    fn the_newest_turns_are_kept_within_the_budget() {
        let said = said();
        assert_eq!(kept_from(&said, 10_000), 0);
        // "And the tests?" (14) + "   " (3) fit in 20; the answer before them does not.
        assert_eq!(kept_from(&said, 20), 2);
        assert_eq!(kept_from(&said, 0), 4);
    }

    /// The shape Claude Code resumes: what `transit`'s notes and a transcript of 2.1.321 hold.
    #[test]
    fn a_claude_session_is_written_as_claude_code_writes_one() {
        let home = temp("claude");
        let written = ClaudeWriter.write(&said(), &place(&home)).unwrap();
        assert!(clarp_kernel::native_session_name(&written.session));
        let file = home
            .join("projects")
            .join("-home-someone-work-repo-x")
            .join(format!("{}.jsonl", written.session));
        assert_eq!(written.files(), std::slice::from_ref(&file));
        let rows = read_lines(&file);
        assert_eq!(rows[0]["type"], "mode");
        assert_eq!(rows[1]["type"], "permission-mode");
        let turns: Vec<&Value> = rows[2..].iter().collect();
        // The empty one is not carried.
        assert_eq!(turns.len(), 3);
        assert_eq!(written.carried, 3);
        let mut parent = Value::Null;
        for row in &turns {
            for key in [
                "uuid",
                "parentUuid",
                "timestamp",
                "sessionId",
                "cwd",
                "version",
                "gitBranch",
                "userType",
                "entrypoint",
                "isSidechain",
            ] {
                assert!(row.get(key).is_some(), "{key} missing");
            }
            assert_eq!(row["sessionId"], written.session.as_str());
            assert_eq!(row["parentUuid"], parent);
            assert_eq!(row["cwd"], "/home/someone/work/repo.x");
            parent = row["uuid"].clone();
        }
        assert_eq!(turns[0]["type"], "user");
        assert_eq!(turns[0]["message"]["role"], "user");
        assert!(turns[0]["message"]["content"].is_string());
        assert_eq!(turns[0]["message"]["content"], "Fix the parser, please.");
        assert_eq!(turns[0]["timestamp"], "2027-01-15T08:00:00.000Z");
        let answer = &turns[1]["message"];
        assert_eq!(turns[1]["type"], "assistant");
        assert!(
            answer["content"].is_array(),
            "an answer's content is a list of blocks"
        );
        assert_eq!(answer["content"][0]["type"], "text");
        assert_eq!(
            answer["content"][0]["text"],
            "Done: the parser reads both shapes now."
        );
        for key in ["role", "type", "id", "model", "stop_reason", "usage"] {
            assert!(answer.get(key).is_some(), "{key} missing");
        }
        written.undo();
        assert!(!file.exists());
        std::fs::remove_dir_all(home).unwrap();
    }

    fn codex_index(home: &Path) -> PathBuf {
        let index = home.join("state_5.sqlite");
        let db = rusqlite::Connection::open(&index).unwrap();
        // The table as Codex 0.167 has it, its required columns and a few of the others.
        db.execute_batch(
            "CREATE TABLE threads (
                id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL, source TEXT NOT NULL, model_provider TEXT NOT NULL,
                cwd TEXT NOT NULL, title TEXT NOT NULL, sandbox_policy TEXT NOT NULL,
                approval_mode TEXT NOT NULL, tokens_used INTEGER NOT NULL DEFAULT 0,
                has_user_event INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
                cli_version TEXT NOT NULL DEFAULT '', first_user_message TEXT NOT NULL DEFAULT '',
                model TEXT, preview TEXT NOT NULL DEFAULT '',
                history_mode TEXT NOT NULL DEFAULT 'legacy', originator TEXT);",
        )
        .unwrap();
        index
    }

    #[test]
    fn a_codex_rollout_begins_with_its_meta_and_is_indexed() {
        let home = temp("codex");
        let index = codex_index(&home);
        let mut at = place(&home);
        at.tool_version = Some("0.167.1".into());
        at.model = Some("gpt-5.5".into());
        let written = CodexWriter.write(&said(), &at).unwrap();
        assert_eq!(&written.session[14..15], "7");
        let file = written.files()[0].clone();
        let name = file.file_name().unwrap().to_string_lossy().into_owned();
        assert!(name.starts_with("rollout-2027-01-1"), "{name}");
        assert!(name.ends_with(&format!("-{}.jsonl", written.session)));
        // Where the Host's own reader finds a thread's rollout.
        assert_eq!(
            crate::past::codex_rollout(&home.join("sessions"), &written.session),
            Some(file.canonicalize().unwrap())
        );
        let rows = read_lines(&file);
        assert_eq!(rows[0]["type"], "session_meta");
        assert_eq!(rows[0]["payload"]["id"], written.session.as_str());
        assert_eq!(rows[0]["payload"]["cwd"], "/home/someone/work/repo.x");
        let roles: Vec<&str> = rows[1..]
            .iter()
            .map(|row| {
                assert_eq!(row["type"], "response_item");
                assert_eq!(row["payload"]["type"], "message");
                row["payload"]["role"].as_str().unwrap()
            })
            .collect();
        assert_eq!(roles, ["user", "assistant", "user"]);
        assert_eq!(rows[1]["payload"]["content"][0]["type"], "input_text");
        assert_eq!(rows[2]["payload"]["content"][0]["type"], "output_text");
        assert_eq!(
            rows[2]["payload"]["content"][0]["text"],
            "Done: the parser reads both shapes now."
        );
        let db = rusqlite::Connection::open(&index).unwrap();
        let (path, cwd, title, source, model): (String, String, String, String, String) = db
            .query_row(
                "SELECT rollout_path, cwd, title, source, model FROM threads WHERE id = ?1",
                [&written.session],
                |row| {
                    Ok((
                        row.get(0)?,
                        row.get(1)?,
                        row.get(2)?,
                        row.get(3)?,
                        row.get(4)?,
                    ))
                },
            )
            .unwrap();
        assert_eq!(path, file.display().to_string());
        assert_eq!(cwd, "/home/someone/work/repo.x");
        assert_eq!(title, "Fix the parser, please.");
        assert_eq!(source, "exec");
        assert_eq!(model, "gpt-5.5");
        let thread = written.session.clone();
        written.undo();
        assert!(!file.exists());
        let left: i64 = db
            .query_row(
                "SELECT count(*) FROM threads WHERE id = ?1",
                [&thread],
                |row| row.get(0),
            )
            .unwrap();
        assert_eq!(left, 0);
        std::fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn a_codex_index_it_cannot_fill_leaves_nothing_behind() {
        let home = temp("codex-unknown");
        let db = rusqlite::Connection::open(home.join("state_9.sqlite")).unwrap();
        db.execute_batch(
            "CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL,
             created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL, source TEXT NOT NULL,
             model_provider TEXT NOT NULL, cwd TEXT NOT NULL, title TEXT NOT NULL,
             sandbox_policy TEXT NOT NULL, approval_mode TEXT NOT NULL,
             something_new TEXT NOT NULL);",
        )
        .unwrap();
        let failed = CodexWriter.write(&said(), &place(&home)).unwrap_err();
        assert_eq!(failed.stage(), "register");
        assert!(failed.why().contains("something_new"));
        let mut files = Vec::new();
        let mut dirs = vec![home.join("sessions")];
        while let Some(dir) = dirs.pop() {
            for entry in std::fs::read_dir(&dir).into_iter().flatten().flatten() {
                if entry.path().is_dir() {
                    dirs.push(entry.path());
                } else {
                    files.push(entry.path());
                }
            }
        }
        assert!(files.is_empty(), "{files:?}");
        std::fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn a_codex_home_without_an_index_gets_the_rollout_alone() {
        let home = temp("codex-bare");
        let written = CodexWriter.write(&said(), &place(&home)).unwrap();
        assert!(written.files()[0].is_file());
        written.undo();
        std::fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn a_grok_session_has_its_history_and_a_summary_that_names_a_model() {
        let home = temp("grok");
        let mut at = place(&home);
        at.model = None;
        // An older session says which model Grok runs.
        let older = home.join("sessions").join("x").join("older");
        std::fs::create_dir_all(&older).unwrap();
        std::fs::write(
            older.join("summary.json"),
            r#"{"current_model_id": "grok-build-2", "last_active_at": "2026-11-01T00:00:00Z"}"#,
        )
        .unwrap();
        let written = GrokWriter.write(&said(), &at).unwrap();
        assert_eq!(&written.session[14..15], "7");
        let dir = home
            .join("sessions")
            .join("%2Fhome%2Fsomeone%2Fwork%2Frepo.x")
            .join(&written.session);
        let rows = read_lines(&dir.join("chat_history.jsonl"));
        assert_eq!(rows.len(), 3);
        assert_eq!(rows[0]["type"], "user");
        assert_eq!(rows[0]["prompt_index"], 0);
        assert_eq!(
            rows[0]["content"][0]["text"],
            "<user_query>\nFix the parser, please.\n</user_query>"
        );
        assert_eq!(rows[1]["type"], "assistant");
        assert_eq!(
            rows[1]["content"],
            "Done: the parser reads both shapes now."
        );
        assert_eq!(rows[2]["prompt_index"], 1);
        let summary: Value =
            serde_json::from_str(&std::fs::read_to_string(dir.join("summary.json")).unwrap())
                .unwrap();
        assert_eq!(summary["info"]["id"], written.session.as_str());
        assert_eq!(summary["info"]["cwd"], "/home/someone/work/repo.x");
        assert_eq!(summary["current_model_id"], "grok-build-2");
        assert_eq!(summary["next_trace_turn"], 2);
        assert!(dir.join("updates.jsonl.lock").is_file());
        written.undo();
        assert!(!dir.exists());
        std::fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn an_opencode_session_goes_through_its_import() {
        let home = temp("opencode");
        // A stand-in for OpenCode that keeps what it is asked to import.
        let program = home.join("opencode");
        let kept = home.join("imported.json");
        std::fs::write(
            &program,
            format!(
                "#!/bin/sh\n[ \"$1\" = import ] || exit 2\ncp \"$2\" '{}'\n",
                kept.display()
            ),
        )
        .unwrap();
        let mut permissions = std::fs::metadata(&program).unwrap().permissions();
        std::os::unix::fs::PermissionsExt::set_mode(&mut permissions, 0o755);
        std::fs::set_permissions(&program, permissions).unwrap();
        let mut at = place(&home);
        at.program = Some(program.clone());
        let written = OpenCodeWriter.write(&said(), &at).unwrap();
        assert!(written.session.starts_with("ses_"));
        assert_eq!(written.session.len(), 4 + 12 + 14);
        assert!(clarp_kernel::native_session_name(&written.session));
        let export: Value = serde_json::from_str(&std::fs::read_to_string(&kept).unwrap()).unwrap();
        assert_eq!(export["info"]["id"], written.session.as_str());
        assert_eq!(export["info"]["directory"], "/home/someone/work/repo.x");
        let messages = export["messages"].as_array().unwrap();
        assert_eq!(messages.len(), 3);
        assert_eq!(messages[0]["info"]["role"], "user");
        assert_eq!(messages[1]["info"]["role"], "assistant");
        assert_eq!(messages[1]["parts"][0]["type"], "text");
        assert_eq!(
            messages[1]["parts"][0]["text"],
            "Done: the parser reads both shapes now."
        );
        assert_eq!(
            messages[1]["parts"][0]["messageID"],
            messages[1]["info"]["id"]
        );
        assert_eq!(
            messages[1]["parts"][0]["sessionID"],
            written.session.as_str()
        );
        written.undo();
        // An import that fails is said as a failure to register.
        std::fs::write(&program, "#!/bin/sh\necho 'no database' >&2\nexit 1\n").unwrap();
        let failed = OpenCodeWriter.write(&said(), &at).unwrap_err();
        assert_eq!(failed.stage(), "register");
        assert!(failed.why().contains("no database"), "{failed}");
        std::fs::remove_dir_all(home).unwrap();
    }

    /// What `opencode export` (1.18.33) wrote of a session `opencode import` took: each
    /// answer names the message it answers (`parentID`), which the import requires.
    fn genuine_export() -> Value {
        serde_json::from_str(include_str!(
            "../tests/fixtures/opencode-1.18.33-export.json"
        ))
        .unwrap()
    }

    fn keys(value: &Value) -> std::collections::BTreeSet<String> {
        value.as_object().unwrap().keys().cloned().collect()
    }

    #[test]
    fn an_opencode_export_has_what_a_genuine_one_has_and_each_answer_its_question() {
        let mut said = said();
        // An answer before anything was asked, and two answers in a row.
        said.insert(
            0,
            Said::new(Role::Assistant, "Before any question.", 1_799_999_999_000),
        );
        said.push(Said::new(Role::Assistant, "Green.", 1_800_000_004_000));
        said.push(Said::new(
            Role::Assistant,
            "Also the docs.",
            1_800_000_005_000,
        ));
        let ours = OpenCodeWriter::export(&said, &place(Path::new("/nowhere")), "ses_x").unwrap();
        let genuine = genuine_export();
        let ours_messages = ours["messages"].as_array().unwrap();
        let roles: Vec<&str> = ours_messages
            .iter()
            .map(|message| message["info"]["role"].as_str().unwrap())
            .collect();
        assert_eq!(
            roles,
            ["user", "assistant", "user", "assistant", "assistant"]
        );
        for role in ["user", "assistant"] {
            let of = |messages: &Value| {
                let message = messages
                    .as_array()
                    .unwrap()
                    .iter()
                    .find(|message| message["info"]["role"] == role)
                    .unwrap()
                    .clone();
                (keys(&message["info"]), keys(&message["parts"][0]))
            };
            let (theirs, their_part) = of(&genuine["messages"]);
            let (ours, our_part) = of(&ours["messages"]);
            assert!(
                theirs.is_subset(&ours),
                "{role}: {theirs:?} not in {ours:?}"
            );
            assert_eq!(their_part, our_part, "{role}");
        }
        // Each answer names the user's message before it, there and here.
        for messages in [&genuine["messages"], &ours["messages"]] {
            let mut asked = None;
            for message in messages.as_array().unwrap() {
                let info = &message["info"];
                if info["role"] == "user" {
                    asked = Some(info["id"].clone());
                } else {
                    assert_eq!(Some(info["parentID"].clone()), asked, "{info}");
                }
            }
        }
    }

    /// The real `opencode import` takes what this writer writes, in a home of its own. Run on
    /// purpose only: `CLARP_TEST_OPENCODE=1`, with `opencode` on the path.
    #[test]
    #[ignore = "runs the real opencode"]
    fn the_real_opencode_imports_what_is_written() {
        let home = temp("opencode-real");
        let program = PathBuf::from(
            std::env::var("CLARP_OPENCODE_PROGRAM").unwrap_or_else(|_| "opencode".into()),
        );
        let work = home.join("work");
        std::fs::create_dir_all(&work).unwrap();
        // Everything OpenCode keeps goes under the test's home, never the machine's.
        let program_in_home = home.join("opencode-in-home");
        std::fs::write(
            &program_in_home,
            format!(
                "#!/bin/sh\nexport HOME='{h}' XDG_DATA_HOME='{h}/data' XDG_CONFIG_HOME='{h}/config' \
                 XDG_CACHE_HOME='{h}/cache' XDG_STATE_HOME='{h}/state'\nexec '{p}' \"$@\"\n",
                h = home.display(),
                p = program.display()
            ),
        )
        .unwrap();
        let mut permissions = std::fs::metadata(&program_in_home).unwrap().permissions();
        std::os::unix::fs::PermissionsExt::set_mode(&mut permissions, 0o755);
        std::fs::set_permissions(&program_in_home, permissions).unwrap();
        let mut at = place(&home);
        at.cwd = work.display().to_string();
        at.program = Some(program_in_home);
        let written = OpenCodeWriter.write(&said(), &at).unwrap();
        assert!(written.session.starts_with("ses_"));
        std::fs::remove_dir_all(home).unwrap();
    }

    #[test]
    fn paths_are_encoded_as_grok_names_its_directories() {
        assert_eq!(
            url_encoded("/home/a b/c-d_e.f~"),
            "%2Fhome%2Fa%20b%2Fc-d_e.f~"
        );
    }
}
