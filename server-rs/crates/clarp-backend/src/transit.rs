//! Carrying a conversation into another tool: what an agent that changes its backend keeps.
//!
//! An agent that moves from one tool to another keeps its conversation. The Host has its own
//! record of what was said (the user's words and the agent's answers, turn by turn), and a
//! writer here puts that record where the target tool keeps its sessions, in the tool's own
//! format, so that the tool's next turn continues it as one of its own. What is carried is
//! the text of each turn, in order; tool calls, their results, thinking and attachments are
//! not.
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

/// How many signs of the conversation are carried at most: the newest turns that fit. A tool
/// given more than its model can hold would fail its next turn, or shorten what it was given
/// at once; a quarter of a million tokens is far more than any agent needs to go on.
pub const CARRIED_MOST: usize = 400_000;

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
pub fn writer(backend: &str) -> Option<&'static dyn Writer> {
    match backend {
        "claude" => Some(&ClaudeWriter),
        "codex" => Some(&CodexWriter),
        "grok" => Some(&GrokWriter),
        "opencode" => Some(&OpenCodeWriter),
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
        size += one.text.chars().count();
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
    said.iter().filter(|one| !one.text.trim().is_empty())
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
        for one in spoken(said) {
            let id = uuid4()?;
            let mut row = json!({
                "parentUuid": parent,
                "isSidechain": false,
                "type": one.role.name(),
                "uuid": id,
                "timestamp": iso(one.at_ms),
                "userType": "external",
                "entrypoint": "sdk-cli",
                "cwd": place.cwd,
                "sessionId": session,
                "version": version,
                "gitBranch": "",
            });
            match one.role {
                // Its content is a list of blocks. Claude Code looks into each block of an
                // answer (`"tool_use_id" in block`); a string it would walk sign by sign, and
                // die on the first.
                Role::Assistant => {
                    row["message"] = json!({
                        "model": model,
                        "id": format!("msg_{}", id.replace('-', "")),
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": one.text}],
                        "stop_reason": "end_turn",
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
            carried: rows.len() - 2,
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
        for one in spoken(said) {
            let part = match one.role {
                Role::User => "input_text",
                Role::Assistant => "output_text",
            };
            rows.push(json!({
                "timestamp": iso(one.at_ms),
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": one.role.name(),
                    "content": [{"type": part, "text": one.text}],
                },
            }));
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
            carried: rows.len() - 1,
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
                Role::Assistant => rows.push(json!({"type": "assistant", "content": one.text})),
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
            let seq = u64::try_from(index).unwrap_or(0) * 2;
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
            messages.push(json!({
                "info": info,
                "parts": [{
                    "id": opencode_id("prt", one.at_ms, seq + 1)?,
                    "sessionID": session, "messageID": message, "type": "text", "text": one.text,
                }],
            }));
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
            Said {
                role: Role::User,
                text: "Fix the parser, please.".into(),
                at_ms: 1_800_000_000_000,
            },
            Said {
                role: Role::Assistant,
                text: "Done: the parser reads both shapes now.".into(),
                at_ms: 1_800_000_001_000,
            },
            Said {
                role: Role::User,
                text: "   ".into(),
                at_ms: 1_800_000_002_000,
            },
            Said {
                role: Role::User,
                text: "And the tests?".into(),
                at_ms: 1_800_000_003_000,
            },
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
            Said {
                role: Role::Assistant,
                text: "Before any question.".into(),
                at_ms: 1_799_999_999_000,
            },
        );
        said.push(Said {
            role: Role::Assistant,
            text: "Green.".into(),
            at_ms: 1_800_000_004_000,
        });
        said.push(Said {
            role: Role::Assistant,
            text: "Also the docs.".into(),
            at_ms: 1_800_000_005_000,
        });
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
