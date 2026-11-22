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
//! Codex rollout begins with `session_meta` and carries the text in `response_item` messages).
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
    let name = path.file_name().map(|n| n.to_string_lossy()).unwrap_or_default();
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
        if let Some((name, _)) = columns
            .iter()
            .find(|(name, required)| *required && name != "id" && !CODEX_COLUMNS.contains(&name.as_str()))
        {
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
        assert_eq!(written.files(), [file.clone()]);
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
                "uuid", "parentUuid", "timestamp", "sessionId", "cwd", "version",
                "gitBranch", "userType", "entrypoint", "isSidechain",
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
        assert!(answer["content"].is_array(), "an answer's content is a list of blocks");
        assert_eq!(answer["content"][0]["type"], "text");
        assert_eq!(answer["content"][0]["text"], "Done: the parser reads both shapes now.");
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
                |row| Ok((row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?, row.get(4)?)),
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
            .query_row("SELECT count(*) FROM threads WHERE id = ?1", [&thread], |row| row.get(0))
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
}
