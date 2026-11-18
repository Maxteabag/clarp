//! The sessions a tool kept on a machine: what a person who makes an agent is offered to
//! take up again.
//!
//! Claude Code keeps every session as a file of lines under the directory of its project,
//! `<home>/.claude/projects/<the directory it ran in, with a dash for every sign that is no
//! letter and no digit>/<session>.jsonl`, and may keep an index of them beside those files
//! (`sessions-index.json`). A session is listed by what the index says of it where the index
//! names it, and otherwise by what its own file says: where it ran, what was asked first,
//! and the title it was given, by its user or by the tool.
//!
//! The rule is the Python Host's (`server/lib/session_catalog.py`, `list_claude_sessions`),
//! held by `tests/fixtures/past-sessions-vectors.json`. Two things are done otherwise, on
//! purpose, and named in the test:
//!
//! - A project is looked for under the name Claude Code gives it. The Python Host puts a
//!   dash for every slash only, and so offers nothing for a directory whose path has a dot,
//!   an underscore or a space.
//! - A session the index marks as a side chain (the transcript of a helper inside a turn)
//!   is not offered. The Python Host leaves it out where it reads the index and then offers
//!   it after all, as a transcript the index does not name.

use std::collections::{BTreeMap, BTreeSet};
use std::path::{Path, PathBuf};
use std::time::SystemTime;

use clarp_kernel::{native_session_name as one_name, python};
use serde_json::{Value, json};

/// How many lines from the start of a transcript are read, how much of its end, and how
/// much of what was asked first and of a title is shown.
const HEAD_LINES: usize = 120;
const TAIL_BYTES: u64 = 64 * 1024;
const PREVIEW_MOST: usize = 240;
const TITLE_MOST: usize = 160;
/// A moment that is counted in more than this is counted in milliseconds.
const MOST_SECONDS: i64 = 10_000_000_000;

/// A path as it is compared: without a slash at its end and without a slash said twice.
/// Nothing at all is the directory one is in.
fn plain(path: &str) -> String {
    let parts: Vec<&str> = path.split('/').filter(|part| !part.is_empty()).collect();
    match (path.starts_with('/'), parts.is_empty()) {
        (true, _) => format!("/{}", parts.join("/")),
        (false, true) => ".".to_string(),
        (false, false) => parts.join("/"),
    }
}

/// The name Claude Code gives the project of a directory: a dash for every sign of its
/// path that is no letter and no digit.
pub fn claude_project(cwd: &str) -> String {
    let dashed = |sign: char| {
        if sign.is_ascii_alphanumeric() {
            sign
        } else {
            '-'
        }
    };
    plain(cwd).chars().map(dashed).collect()
}

/// A value as Python's `str()` makes a text of it, where nothing and what counts as
/// nothing is no text.
fn text_of(value: Option<&Value>) -> String {
    match value {
        Some(Value::String(text)) => text.clone(),
        Some(Value::Number(number)) if number.as_f64() != Some(0.0) => number.to_string(),
        Some(Value::Bool(true)) => "True".to_string(),
        _ => String::new(),
    }
}

fn truthy(value: Option<&Value>) -> bool {
    match value {
        None | Some(Value::Null) => false,
        Some(Value::Bool(yes)) => *yes,
        Some(Value::Number(number)) => number.as_f64() != Some(0.0),
        Some(Value::String(text)) => !text.is_empty(),
        Some(Value::Array(items)) => !items.is_empty(),
        Some(Value::Object(fields)) => !fields.is_empty(),
    }
}

/// When a file was last written, in whole seconds. A file that cannot be asked: 0.
fn written(path: &Path) -> (Option<SystemTime>, i64) {
    let at = std::fs::metadata(path)
        .and_then(|file| file.modified())
        .ok();
    let seconds = at.and_then(|at| at.duration_since(SystemTime::UNIX_EPOCH).ok());
    let seconds = seconds.map_or(0, |since| {
        i64::try_from(since.as_secs()).unwrap_or(i64::MAX)
    });
    (at, seconds)
}

/// What a line of a transcript says a user asked: the text of a user's message, or the
/// first part of it that is text.
fn asked(event: &Value) -> String {
    if event["type"] != "user" {
        return String::new();
    }
    match &event["message"]["content"] {
        Value::String(text) => python::strip(text).to_string(),
        Value::Array(parts) => {
            let text = parts.iter().find(|part| part["type"] == "text");
            let text = text.map(|part| text_of(part.get("text")));
            python::strip(&text.unwrap_or_default()).to_string()
        }
        _ => String::new(),
    }
}

/// What a transcript says of its session: where it ran, what was asked first, and its
/// title: the one its user gave it, else the one the tool made. Its first lines are read,
/// and its end, where a name that was given later stands.
fn read(path: &Path) -> Option<Value> {
    let bytes = std::fs::read(path).ok()?;
    let (mut cwd, mut preview) = (String::new(), String::new());
    let (mut made, mut given) = (String::new(), String::new());
    let mut take = |line: &str| {
        let Ok(Value::Object(event)) = serde_json::from_str::<Value>(line) else {
            return;
        };
        let event = Value::Object(event);
        let said = |key: &str| Some(text_of(event.get(key))).filter(|text| !text.is_empty());
        cwd = said("cwd").unwrap_or(std::mem::take(&mut cwd));
        if preview.is_empty() {
            preview = asked(&event);
        }
        if event["type"] == "custom-title" {
            given = said("customTitle").unwrap_or(std::mem::take(&mut given));
        }
        made = said("aiTitle").unwrap_or(std::mem::take(&mut made));
    };
    // A line ends where a new one begins, however that is written.
    let whole = String::from_utf8_lossy(&bytes);
    let whole = whole.replace("\r\n", "\n").replace('\r', "\n");
    for line in whole.split_inclusive('\n').take(HEAD_LINES) {
        take(line);
    }
    let size = bytes.len() as u64;
    let from = usize::try_from(size.saturating_sub(TAIL_BYTES)).unwrap_or(0);
    let tail = String::from_utf8_lossy(&bytes[from..]);
    let mut lines = python::splitlines(&tail);
    if size > TAIL_BYTES && !lines.is_empty() {
        lines.remove(0); // the end of a line that began before
    }
    for line in lines {
        take(line);
    }
    let id = path.file_stem()?.to_string_lossy().into_owned();
    let title = if given.is_empty() { made } else { given };
    Some(json!({
        "id": id, "mtime": written(path).1, "preview": python::first(&preview, PREVIEW_MOST),
        "title": python::first(&title, TITLE_MOST), "cwd": cwd,
    }))
}

/// The files of a directory, in the order of their names.
fn files_of(dir: &Path) -> Vec<PathBuf> {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return Vec::new();
    };
    let mut paths: Vec<PathBuf> = entries.flatten().map(|entry| entry.path()).collect();
    paths.sort();
    paths
}

/// The moment an index says a session was last written: in seconds or in milliseconds, or
/// as a date with its time and its zone. What says neither: 0.
fn moment(entry: &Value) -> i64 {
    let counted = match &entry["fileMtime"] {
        Value::Number(number) => number.as_i64().or(number.as_f64().map(|n| n as i64)),
        _ => None,
    };
    match counted.unwrap_or(0) {
        0 => {
            let said = text_of(entry.get("modified"));
            let read = said.parse::<jiff::Timestamp>();
            read.map_or(0, |moment| moment.as_second())
        }
        milliseconds if milliseconds > MOST_SECONDS => milliseconds / 1000,
        seconds => seconds,
    }
}

/// Claude Code's sessions under `projects` (`<home>/.claude/projects`): those of the
/// project of `cwd`, or of every project, the newest first, `limit` at most. Each as an app
/// reads it: `id`, `mtime` in seconds, `preview`, `title`, `cwd`.
pub fn claude_sessions(projects: &Path, cwd: &str, all_projects: bool, limit: usize) -> Vec<Value> {
    if !projects.is_dir() {
        return Vec::new();
    }
    let own = projects.join(claude_project(cwd));
    let dirs: Vec<PathBuf> = if all_projects {
        let all = files_of(projects).into_iter();
        all.filter(|path| path.is_dir()).collect()
    } else {
        vec![own]
    };
    let wanted = if cwd.is_empty() {
        String::new()
    } else {
        plain(cwd)
    };
    // What the indexes say, in the order they say it. A session named twice is listed by
    // the newer word of it.
    let mut listed: Vec<Value> = Vec::new();
    let mut place: BTreeMap<String, usize> = BTreeMap::new();
    let mut named: BTreeSet<String> = BTreeSet::new();
    let mut put = |item: Value, listed: &mut Vec<Value>| {
        let id = item["id"].as_str().unwrap_or_default().to_string();
        match place.get(&id) {
            Some(at) if item["mtime"].as_i64() > listed[*at]["mtime"].as_i64() => {
                listed[*at] = item
            }
            Some(_) => {}
            None => {
                place.insert(id, listed.len());
                listed.push(item);
            }
        }
    };
    for dir in &dirs {
        let index = std::fs::read(dir.join("sessions-index.json")).ok();
        let index = index.and_then(|bytes| serde_json::from_slice::<Value>(&bytes).ok());
        let entries = index.as_ref().and_then(|index| index["entries"].as_array());
        for entry in entries
            .into_iter()
            .flatten()
            .filter(|entry| entry.is_object())
        {
            let id = text_of(entry.get("sessionId"));
            if id.is_empty() {
                continue;
            }
            if truthy(entry.get("isSidechain")) {
                // No session of its own: also not as a transcript the index does not name.
                named.insert(id);
                continue;
            }
            let declared = PathBuf::from(text_of(entry.get("fullPath")));
            let beside = dir.join(format!("{id}.jsonl"));
            if !declared.is_file() && !beside.is_file() {
                continue;
            }
            named.insert(id.clone());
            let ran_in = text_of(entry.get("projectPath"));
            let elsewhere = !wanted.is_empty() && !ran_in.is_empty() && plain(&ran_in) != wanted;
            if !all_projects && elsewhere {
                continue;
            }
            let title = match text_of(entry.get("customTitle")) {
                title if title.is_empty() => text_of(entry.get("summary")),
                title => title,
            };
            let item = json!({
                "id": id, "mtime": moment(entry),
                "preview": python::first(&text_of(entry.get("firstPrompt")), PREVIEW_MOST),
                "title": python::first(&title, TITLE_MOST), "cwd": ran_in,
            });
            put(item, &mut listed);
        }
    }
    // The transcripts no index names, the newest first: an index may be missing, or behind.
    let mut unnamed: Vec<(Option<SystemTime>, PathBuf)> = Vec::new();
    for dir in &dirs {
        for path in files_of(dir) {
            let transcript = path.extension().is_some_and(|ending| ending == "jsonl");
            let id = path
                .file_stem()
                .map(|stem| stem.to_string_lossy().into_owned());
            if transcript && path.is_file() && id.is_some_and(|id| !named.contains(&id)) {
                unnamed.push((written(&path).0, path));
            }
        }
    }
    unnamed.sort_by(|one, other| other.0.cmp(&one.0).then_with(|| one.1.cmp(&other.1)));
    for (_, path) in unnamed.into_iter().take(limit) {
        let Some(item) = read(&path) else {
            continue;
        };
        let ran_in = item["cwd"].as_str().unwrap_or_default();
        if !all_projects && !wanted.is_empty() && !ran_in.is_empty() && ran_in != wanted {
            continue;
        }
        put(item, &mut listed);
    }
    listed.sort_by_key(|item| std::cmp::Reverse(item["mtime"].as_i64().unwrap_or(0)));
    listed.truncate(limit);
    listed
}

// ---- Codex -------------------------------------------------------------------------------
//
// Codex keeps every session as a rollout, `<home>/.codex/sessions/<year>/<month>/<day>/
// rollout-<when>-<session>.jsonl`, and an index of its threads in a database of its own
// (`<home>/.codex/state_5.sqlite`, the table `threads`). A session is listed by what the
// index says of it, and the newest rollouts are looked into beside it, so that an index that
// is missing or behind hides no session that is there. The rule is the Python Host's
// (`server/lib/codex_transcript.py`, `list_sessions` and `find_latest_jsonl`), held by
// `tests/fixtures/codex-sessions-vectors.json`.

/// How many lines from the start of a rollout are read for what it says of itself.
const ROLLOUT_HEAD_LINES: usize = 80;
/// How many of the newest rollouts are looked into at least.
const ROLLOUTS_LOOKED_INTO: usize = 200;

/// The files under a directory, however deep, whose names are wanted. A directory that is
/// only pointed at is not gone into.
fn files_under(root: &Path, wanted: &dyn Fn(&str) -> bool) -> Vec<PathBuf> {
    let mut found = Vec::new();
    let mut dirs = vec![root.to_path_buf()];
    while let Some(dir) = dirs.pop() {
        let Ok(entries) = std::fs::read_dir(&dir) else {
            continue;
        };
        for entry in entries.flatten() {
            let path = entry.path();
            match entry.file_type() {
                Ok(kind) if kind.is_dir() => dirs.push(path),
                Ok(_) if wanted(&entry.file_name().to_string_lossy()) => found.push(path),
                _ => {}
            }
        }
    }
    found
}

/// What a rollout says of itself at its start: its session, where it ran, and what its user
/// asked first.
fn rollout_head(path: &Path) -> (String, String, String) {
    use std::io::BufRead;
    let (mut session, mut cwd, mut preview) = (String::new(), String::new(), String::new());
    let Ok(file) = std::fs::File::open(path) else {
        return (session, cwd, preview);
    };
    let mut lines = std::io::BufReader::new(file);
    let mut line = Vec::new();
    for _ in 0..ROLLOUT_HEAD_LINES {
        line.clear();
        if !matches!(lines.read_until(b'\n', &mut line), Ok(read) if read > 0) {
            break;
        }
        let Ok(Value::Object(record)) = serde_json::from_slice::<Value>(&line) else {
            continue;
        };
        let nothing = serde_json::Map::new();
        let payload = record
            .get("payload")
            .and_then(Value::as_object)
            .unwrap_or(&nothing);
        let either = |name: &str| match text_of(payload.get(name)) {
            said if said.is_empty() => text_of(record.get(name)),
            said => said,
        };
        let kind = either("type");
        if kind == "session_meta"
            || record
                .get("type")
                .is_some_and(|kind| kind == "session_meta")
        {
            (session, cwd) = (either("id"), either("cwd"));
        } else if kind == "user_message" && preview.is_empty() {
            let asked = payload
                .get("message")
                .and_then(Value::as_str)
                .unwrap_or_default();
            preview = python::strip(asked.trim_start_matches('›')).to_string();
        }
        if !session.is_empty() && !cwd.is_empty() && !preview.is_empty() {
            break;
        }
    }
    (session, cwd, preview)
}

/// What a field of Codex's index holds, as a text: nothing, and what counts as nothing, is
/// no text.
fn indexed_text(value: &rusqlite::types::Value) -> String {
    match value {
        rusqlite::types::Value::Text(text) => text.clone(),
        rusqlite::types::Value::Integer(number) if *number != 0 => number.to_string(),
        _ => String::new(),
    }
}

fn indexed_whole(value: &rusqlite::types::Value) -> i64 {
    match value {
        rusqlite::types::Value::Integer(number) => *number,
        rusqlite::types::Value::Real(number) => number.trunc() as i64,
        rusqlite::types::Value::Text(text) => {
            let read = python::int(text).and_then(|number| i64::try_from(number).ok());
            read.unwrap_or(0)
        }
        _ => 0,
    }
}

/// The sessions Codex's own index names, in the order it gives them: those a user wrote in,
/// of one directory or of all, whose rollout is there.
fn indexed(index: &Path, wanted: &str, limit: usize) -> rusqlite::Result<Vec<Value>> {
    use rusqlite::types::Value as Held;
    let flags =
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let db = rusqlite::Connection::open_with_flags(index, flags)?;
    db.busy_timeout(std::time::Duration::from_secs(1))?;
    let of_one = if wanted.is_empty() {
        ""
    } else {
        " AND cwd = ?2"
    };
    let question = format!(
        "SELECT id,cwd,name,title,preview,first_user_message,updated_at,updated_at_ms,rollout_path \
         FROM threads WHERE (has_user_event = 1 OR COALESCE(first_user_message, '') != ''){of_one} \
         ORDER BY recency_at_ms DESC,updated_at_ms DESC LIMIT ?1"
    );
    let mut asked = db.prepare(&question)?;
    let limit = i64::try_from(limit).unwrap_or(i64::MAX);
    let read = |row: &rusqlite::Row<'_>| -> rusqlite::Result<Vec<Held>> {
        (0..9).map(|at| row.get(at)).collect()
    };
    let rows: Vec<Vec<Held>> = if wanted.is_empty() {
        asked
            .query_map(rusqlite::params![limit], read)?
            .collect::<Result<_, _>>()?
    } else {
        asked
            .query_map(rusqlite::params![limit, wanted], read)?
            .collect::<Result<_, _>>()?
    };
    let mut listed: Vec<Value> = Vec::new();
    for row in rows {
        let [
            id,
            cwd,
            name,
            title,
            preview,
            first,
            updated,
            updated_ms,
            rollout,
        ] = row.as_slice()
        else {
            continue;
        };
        if !Path::new(&indexed_text(rollout)).is_file() {
            continue;
        }
        let id = indexed_text(id);
        if id.is_empty() {
            continue;
        }
        let at = match indexed_whole(updated_ms) {
            0 => indexed_whole(updated),
            milliseconds => milliseconds.div_euclid(1000),
        };
        let or = |first: &Held, second: &Held| match indexed_text(first) {
            said if said.is_empty() => indexed_text(second),
            said => said,
        };
        let item = json!({
            "id": id, "mtime": at, "preview": python::first(&or(preview, first), PREVIEW_MOST),
            "title": python::first(&or(name, title), TITLE_MOST), "cwd": indexed_text(cwd),
        });
        match listed.iter_mut().find(|held| held["id"] == item["id"]) {
            Some(held) => *held = item,
            None => listed.push(item),
        }
    }
    Ok(listed)
}

/// Codex's sessions under `sessions` (`<home>/.codex/sessions`): those that ran in `cwd`, or
/// in every directory, the newest first, `limit` at most. `index`: Codex's own index of its
/// threads, where there is one. Each as an app reads it: `id`, `mtime` in seconds,
/// `preview`, `title`, `cwd`.
pub fn codex_sessions(
    sessions: &Path,
    index: Option<&Path>,
    cwd: &str,
    all_projects: bool,
    limit: usize,
) -> Vec<Value> {
    if !sessions.is_dir() {
        return Vec::new();
    }
    let wanted = if cwd.is_empty() {
        String::new()
    } else {
        plain(cwd)
    };
    let of_one = if all_projects { "" } else { wanted.as_str() };
    let index = index.filter(|index| index.is_file());
    // An index that cannot be read names nothing: the rollouts are looked into all the same.
    let mut listed = index
        .and_then(|index| indexed(index, of_one, limit).ok())
        .unwrap_or_default();
    let mut rollouts: Vec<(Option<SystemTime>, PathBuf)> = files_under(sessions, &|name| {
        name.starts_with("rollout-") && name.ends_with(".jsonl")
    })
    .into_iter()
    .map(|path| (written(&path).0, path))
    .collect();
    rollouts.sort_by(|one, other| other.0.cmp(&one.0).then_with(|| one.1.cmp(&other.1)));
    for (_, path) in rollouts
        .into_iter()
        .take(ROLLOUTS_LOOKED_INTO.max(limit.saturating_mul(4)))
    {
        let (session, ran_in, preview) = rollout_head(&path);
        if session.is_empty() || listed.iter().any(|held| held["id"] == session.as_str()) {
            continue;
        }
        if !of_one.is_empty() && plain(&ran_in) != of_one {
            continue;
        }
        listed.push(json!({
            "id": session, "mtime": written(&path).1, "preview": python::first(&preview, PREVIEW_MOST),
            "title": "", "cwd": ran_in,
        }));
    }
    listed.sort_by_key(|item| std::cmp::Reverse(item["mtime"].as_i64().unwrap_or(0)));
    listed.truncate(limit);
    listed
}

/// Claude resumes in the current project. A fork can also take its source
/// from another project in the same store.
pub fn claude_transcript(
    projects: &Path,
    cwd: &str,
    session: &str,
    other_projects: bool,
) -> Option<PathBuf> {
    if !one_name(session) {
        return None;
    }
    let root = projects.canonicalize().ok()?;
    let name = format!("{session}.jsonl");
    let own = root.join(claude_project(cwd)).join(&name);
    confined(&root, &own).or_else(|| {
        if !other_projects {
            return None;
        }
        files_of(&root)
            .into_iter()
            .find_map(|dir| confined(&root, &dir.join(&name)))
    })
}

/// The rollout of a session of Codex: the newest file under `sessions` whose name ends with
/// the session's.
pub fn codex_rollout(sessions: &Path, session: &str) -> Option<PathBuf> {
    if !one_name(session) || !sessions.is_dir() {
        return None;
    }
    let root = sessions.canonicalize().ok()?;
    let ending = format!("{session}.jsonl");
    let named = files_under(sessions, &|name| name.ends_with(&ending));
    let mut newest: Option<(SystemTime, PathBuf)> = None;
    for path in named.into_iter().filter_map(|p| confined(&root, &p)) {
        let Some(at) = written(&path).0 else {
            continue;
        };
        let newer = newest
            .as_ref()
            .is_none_or(|(held, by)| at > *held || (at == *held && path < *by));
        if newer {
            newest = Some((at, path));
        }
    }
    newest.map(|(_, path)| path)
}

// ---- Antigravity ---------------------------------------------------------------------------
//
// agy keeps what is its own under `<home>/.gemini/antigravity-cli` (`base` below): the
// conversations it still has, each a file `conversations/<name>.db`; what was said in each,
// `brain/<name>/.system_generated/logs/transcript.jsonl` ([`crate::history::agy`]); since agy
// 1.2 a catalogue of them with their titles and the directories they ran in
// (`conversation_summaries.db`); and before that only the last conversation of each
// directory (`cache/last_conversations.json`). The rule is the Python Host's
// (`server/lib/agy_transcript.py`, `list_sessions`), held by
// `tests/fixtures/agy-sessions-vectors.json`.

/// Whether agy still has the conversation of this name. It lets go of all but the newest
/// few hundred, and what it lets go of leaves its transcript behind: only the conversation's
/// own file counts. Where agy has no store at all on this machine nothing is known, and the
/// conversation counts as kept (`conversation_exists`). Malformed names are refused.
pub fn agy_kept(base: &Path, conversation: &str) -> bool {
    if !one_name(conversation) {
        return false;
    }
    let store = base.join("conversations");
    if !store.is_dir() {
        return true;
    }
    base.canonicalize()
        .is_ok_and(|root| confined(&root, &store.join(format!("{conversation}.db"))).is_some())
}

/// The transcript of a conversation of agy, where there is one (`find_latest_jsonl`).
pub fn agy_transcript(base: &Path, conversation: &str) -> Option<PathBuf> {
    if !one_name(conversation) {
        return None;
    }
    let path = base
        .join("brain")
        .join(conversation)
        .join(".system_generated/logs/transcript.jsonl");
    confined(&base.canonicalize().ok()?, &path)
}

/// A text of a URI with every `%` and two digits read as the byte they stand for
/// (`urllib.parse.unquote`).
fn unquoted(text: &str) -> String {
    let bytes = text.as_bytes();
    let digit = |at: usize| {
        bytes
            .get(at)
            .and_then(|byte| char::from(*byte).to_digit(16))
    };
    let (mut out, mut at) = (Vec::with_capacity(bytes.len()), 0);
    while at < bytes.len() {
        let escaped = (bytes[at] == b'%')
            .then(|| digit(at + 1).zip(digit(at + 2)))
            .flatten();
        match escaped.and_then(|(high, low)| u8::try_from(high * 16 + low).ok()) {
            Some(byte) => {
                out.push(byte);
                at += 3;
            }
            None => {
                out.push(bytes[at]);
                at += 1;
            }
        }
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// The directory a `file://` URI names (`urllib.parse.urlsplit(uri).path`, unquoted): what
/// follows the machine's name, without a question and without a part.
fn directory_of(uri: &str) -> Option<String> {
    let rest = uri.strip_prefix("file://")?;
    let rest: String = rest
        .chars()
        .filter(|sign| !matches!(sign, '\t' | '\r' | '\n'))
        .collect();
    let rest = &rest[..rest.find(['?', '#']).unwrap_or(rest.len())];
    Some(unquoted(rest.find('/').map_or("", |at| &rest[at..])))
}

/// A directory the Host made for work of its own: one question to a tool, a dream
/// (`_clarp_scratch`).
fn scratch(workspace: &str) -> bool {
    let temp = std::env::temp_dir();
    let roots = [Path::new("/tmp"), Path::new("/var/tmp"), temp.as_path()];
    roots.iter().any(|root| {
        let under = Path::new(workspace).strip_prefix(root).ok();
        let first = under.and_then(|rest| rest.components().next());
        first.is_some_and(|first| first.as_os_str().to_string_lossy().starts_with("clarp-"))
    })
}

/// The directories a row of agy's catalogue names: a list of URIs, written as JSON. What is
/// no such list names none, but for an object, whose names are read as Python reads them.
fn directories(held: &rusqlite::types::Value) -> Vec<String> {
    let written = match held {
        rusqlite::types::Value::Text(text) if !text.is_empty() => text.as_str(),
        rusqlite::types::Value::Text(_)
        | rusqlite::types::Value::Null
        | rusqlite::types::Value::Integer(0) => "[]",
        _ => return Vec::new(),
    };
    let uris: Vec<String> = match serde_json::from_str::<Value>(written) {
        Ok(Value::Array(uris)) => {
            let texts = uris.iter().filter_map(Value::as_str);
            texts.map(str::to_string).collect()
        }
        Ok(Value::Object(named)) => named.keys().cloned().collect(),
        _ => Vec::new(),
    };
    uris.iter().filter_map(|uri| directory_of(uri)).collect()
}

/// The conversations agy's own catalogue names, the last changed first: those that are no
/// helper's within another, that ran in the directory asked for (or anywhere), that agy
/// still has and that have a transcript. Not those of the Host's own scratch directories.
fn agy_catalogued(
    catalogue: &Path,
    base: &Path,
    wanted: &str,
    limit: usize,
) -> rusqlite::Result<Vec<Value>> {
    use rusqlite::types::Value as Held;
    let flags =
        rusqlite::OpenFlags::SQLITE_OPEN_READ_ONLY | rusqlite::OpenFlags::SQLITE_OPEN_NO_MUTEX;
    let db = rusqlite::Connection::open_with_flags(catalogue, flags)?;
    db.busy_timeout(std::time::Duration::from_secs(1))?;
    let mut asked = db.prepare(
        "SELECT conversation_id, title, preview, workspace_uris FROM conversation_summaries \
         WHERE nesting_depth = 0 ORDER BY last_modified_time DESC",
    )?;
    let read = |row: &rusqlite::Row<'_>| -> rusqlite::Result<Vec<Held>> {
        (0..4).map(|at| row.get(at)).collect()
    };
    let rows: Vec<Vec<Held>> = asked.query_map([], read)?.collect::<Result<_, _>>()?;
    let mut listed: Vec<Value> = Vec::new();
    for row in rows {
        let [id, title, preview, uris] = row.as_slice() else {
            continue;
        };
        let ran_in = directories(uris);
        if ran_in.iter().any(|directory| scratch(directory)) {
            continue;
        }
        if !wanted.is_empty() && !ran_in.iter().any(|directory| directory == wanted) {
            continue;
        }
        let id = match id {
            Held::Text(text) => text.clone(),
            Held::Integer(number) => number.to_string(),
            Held::Real(number) => python::float(*number),
            _ => "None".to_string(),
        };
        // agy lets go of old conversations and keeps what its catalogue says of them, and
        // some of their transcripts: only one it still has, with what was said in it, can
        // be taken up.
        let Some(transcript) = agy_transcript(base, &id).filter(|_| agy_kept(base, &id)) else {
            continue;
        };
        listed.push(json!({
            "id": id, "mtime": written(&transcript).1,
            "preview": python::first(&indexed_text(preview), PREVIEW_MOST),
            "title": indexed_text(title), "cwd": ran_in.first().cloned().unwrap_or_default(),
        }));
        if listed.len() >= limit {
            break;
        }
    }
    Ok(listed)
}

/// What an agy without a catalogue knows: the last conversation of each directory, the one
/// whose transcript was written last first. Each is shown by what its user asked first.
fn agy_last(base: &Path, wanted: &str, limit: usize) -> Vec<Value> {
    let read = std::fs::read_to_string(base.join("cache/last_conversations.json")).ok();
    let last = read.and_then(|text| serde_json::from_str::<Value>(&text).ok());
    let Some(Value::Object(last)) = last else {
        return Vec::new();
    };
    let of: Vec<(&str, Option<&Value>)> = if wanted.is_empty() {
        let all = last.iter();
        all.map(|(cwd, named)| (cwd.as_str(), Some(named)))
            .collect()
    } else {
        vec![(wanted, last.get(wanted))]
    };
    let mut listed: Vec<Value> = Vec::new();
    for (cwd, named) in of {
        let Some(named) = named.filter(|named| python::truthy(named)) else {
            continue;
        };
        let id = clarp_kernel::tools::cells::said(named);
        let Some(transcript) = agy_transcript(base, &id) else {
            continue;
        };
        let turns = std::fs::read_to_string(&transcript).map(|text| crate::history::agy(&text));
        let asked_first = turns.unwrap_or_default().into_iter().find_map(|turn| {
            let text = turn["text"].as_str().filter(|text| !text.is_empty())?;
            (turn["role"] == "user").then(|| python::first(text, PREVIEW_MOST).to_string())
        });
        listed.push(json!({
            "id": id, "mtime": written(&transcript).1, "preview": asked_first.unwrap_or_default(),
            "title": "", "cwd": cwd,
        }));
    }
    listed.sort_by_key(|session| std::cmp::Reverse(session["mtime"].as_i64()));
    listed.truncate(limit);
    listed
}

/// The conversations agy kept under `base` (`<home>/.gemini/antigravity-cli`): those that
/// ran in `cwd`, or in every directory where `cwd` is empty, `limit` at most. Each as an app
/// reads it: `id`, `mtime` in seconds, `preview`, `title`, `cwd`. From agy's catalogue where
/// it has one that can be read, and from what an older agy kept otherwise.
pub fn agy_sessions(base: &Path, cwd: &str, limit: usize) -> Vec<Value> {
    let wanted = if cwd.is_empty() {
        String::new()
    } else {
        plain(cwd)
    };
    let catalogue = base.join("conversation_summaries.db");
    if catalogue.exists()
        && let Ok(listed) = agy_catalogued(&catalogue, base, &wanted, limit)
    {
        return listed;
    }
    agy_last(base, &wanted, limit)
}

/// Native session stores of Grok Build and Muse Code.
pub fn native_home(tool: clarp_kernel::grok_muse::Tool, home: &Path) -> PathBuf {
    use clarp_kernel::grok_muse::Tool;
    let (variable, default) = match tool {
        Tool::Grok => ("CLAUDE_PWA_GROK_HOME", ".grok"),
        Tool::Muse => ("CLAUDE_PWA_MUSE_HOME", ".local/share/muse"),
    };
    std::env::var_os(variable)
        .filter(|v| !v.is_empty())
        .map_or_else(|| home.join(default), PathBuf::from)
}

fn date_dirs(root: &Path, digits: usize) -> impl Iterator<Item = PathBuf> + use<> {
    files_of(root).into_iter().filter(move |p| {
        p.file_name()
            .and_then(|v| v.to_str())
            .is_some_and(|v| v.len() == digits && v.bytes().all(|b| b.is_ascii_digit()))
    })
}

fn roots(tool: clarp_kernel::grok_muse::Tool, base: &Path) -> Vec<PathBuf> {
    match tool {
        clarp_kernel::grok_muse::Tool::Grok => files_of(base),
        clarp_kernel::grok_muse::Tool::Muse => date_dirs(base, 4)
            .flat_map(|y| date_dirs(&y, 2))
            .flat_map(|m| date_dirs(&m, 2))
            .collect(),
    }
}

fn native_file(tool: clarp_kernel::grok_muse::Tool) -> &'static str {
    match tool {
        clarp_kernel::grok_muse::Tool::Grok => "chat_history.jsonl",
        clarp_kernel::grok_muse::Tool::Muse => "session.jsonl",
    }
}

fn confined(base: &Path, file: &Path) -> Option<PathBuf> {
    let path = file.canonicalize().ok()?;
    (path.starts_with(base) && path.is_file()).then_some(path)
}

pub fn native_transcript(
    tool: clarp_kernel::grok_muse::Tool,
    home: &Path,
    id: &str,
) -> Option<String> {
    native_transcript_in(tool, &native_home(tool, home).join("sessions"), id)
}

fn native_transcript_in(
    tool: clarp_kernel::grok_muse::Tool,
    sessions: &Path,
    id: &str,
) -> Option<String> {
    use clarp_kernel::grok_muse::Tool;
    if !one_name(id) {
        return None;
    }
    if tool == Tool::Muse
        && (!(8..=64).contains(&id.len())
            || !id.bytes().all(|b| b.is_ascii_hexdigit() || b == b'-'))
    {
        return None;
    }
    let base = sessions.canonicalize().ok()?;
    let mut dirs = roots(tool, &base);
    if tool == Tool::Muse {
        dirs.reverse();
    } else {
        dirs.insert(0, base.clone());
    }
    let file = dirs
        .iter()
        .find_map(|d| confined(&base, &d.join(id).join(native_file(tool))))?;
    Some(String::from_utf8_lossy(&std::fs::read(file).ok()?).into_owned())
}

pub fn native_sessions(
    tool: clarp_kernel::grok_muse::Tool,
    home: &Path,
    cwd: &str,
    limit: usize,
) -> Vec<Value> {
    use clarp_kernel::{grok_muse::Tool, grok_muse_history, told};
    let Ok(base) = native_home(tool, home).join("sessions").canonicalize() else {
        return Vec::new();
    };
    let mut sessions = Vec::new();
    for parent in roots(tool, &base) {
        let decoded = unquoted(&parent.file_name().unwrap_or_default().to_string_lossy());
        if tool == Tool::Grok && !cwd.is_empty() && cwd != decoded {
            continue;
        }
        for dir in files_of(&parent) {
            let Some(file) = confined(&base, &dir.join(native_file(tool))) else {
                continue;
            };
            let Ok(bytes) = std::fs::read(&file) else {
                continue;
            };
            let log = String::from_utf8_lossy(&bytes);
            let (workspace, preview, title) = match tool {
                Tool::Grok => {
                    let turns = grok_muse_history::grok(&log);
                    let preview = turns
                        .iter()
                        .find(|r| r["role"] == "user")
                        .and_then(|r| r["text"].as_str())
                        .unwrap_or("")
                        .to_string();
                    let info: Value = confined(&base, &dir.join("summary.json"))
                        .and_then(|p| std::fs::read(p).ok())
                        .and_then(|b| serde_json::from_slice(&b).ok())
                        .unwrap_or_default();
                    let title = info["generated_title"]
                        .as_str()
                        .filter(|s| !s.is_empty())
                        .or(info["session_summary"].as_str())
                        .unwrap_or("")
                        .to_string();
                    (decoded.clone(), preview, title)
                }
                Tool::Muse => {
                    let mut workspace = String::new();
                    let mut preview = String::new();
                    for r in log
                        .lines()
                        .filter_map(|l| serde_json::from_str::<Value>(l).ok())
                    {
                        let p = &r["payload"];
                        let location = match r["payload_type"].as_str() {
                            Some("runtime.session.route_facts") => p["record"]["cwd"].as_str(),
                            Some("runtime.session.metadata") if workspace.is_empty() => {
                                p["record"]["workspace_root"].as_str()
                            }
                            _ => None,
                        };
                        if let Some(s) = location.filter(|s| !s.is_empty()) {
                            workspace = s.into();
                        }
                        if preview.is_empty()
                            && r["payload_type"] == "runtime.session"
                            && p["kind"] == "run"
                            && p["event"]["kind"] == "started"
                        {
                            preview = told::unheaded(p["event"]["prompt"].as_str().unwrap_or(""))
                                .trim()
                                .into();
                        }
                        if !workspace.is_empty() && !preview.is_empty() {
                            break;
                        }
                    }
                    (workspace, preview, String::new())
                }
            };
            if !cwd.is_empty() && workspace != cwd {
                continue;
            }
            sessions.push(json!({"id":dir.file_name().unwrap_or_default().to_string_lossy(),"mtime":written(&file).1,"preview":python::first(&preview,240),"title":title,"cwd":workspace}));
        }
    }
    sessions.sort_by_key(|s| std::cmp::Reverse(s["mtime"].as_i64().unwrap_or(0)));
    sessions.truncate(limit);
    sessions
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod confinement {
    use super::*;
    use clarp_kernel::grok_muse::Tool;

    #[test]
    fn all_native_session_lookups_require_one_name_and_a_file_in_the_store() {
        let root = std::env::temp_dir().join(format!("clarp-native-lookup-{}", std::process::id()));
        let outside = root.join("outside");
        std::fs::create_dir_all(&root).unwrap();
        std::fs::write(&outside, "fixture").unwrap();
        let id = "00000000-1234-4321-abcd-000000000001";
        for tool in ["claude", "codex", "agy", "grok", "muse"] {
            let store = root.join(tool);
            let file = match tool {
                "claude" => store
                    .join(claude_project("/work"))
                    .join(format!("{id}.jsonl")),
                "codex" => store.join(format!("rollout-{id}.jsonl")),
                "agy" => store
                    .join("brain")
                    .join(id)
                    .join(".system_generated/logs/transcript.jsonl"),
                "grok" => store.join(id).join("chat_history.jsonl"),
                "muse" => store.join("2026/11/18").join(id).join("session.jsonl"),
                _ => unreachable!(),
            };
            let lookup = |name: &str| match tool {
                "claude" => claude_transcript(&store, "/work", name, false).is_some(),
                "codex" => codex_rollout(&store, name).is_some(),
                "agy" => agy_transcript(&store, name).is_some(),
                "grok" => native_transcript_in(Tool::Grok, &store, name).is_some(),
                "muse" => native_transcript_in(Tool::Muse, &store, name).is_some(),
                _ => unreachable!(),
            };
            std::fs::create_dir_all(file.parent().unwrap()).unwrap();
            std::fs::write(&file, "fixture").unwrap();
            assert!(lookup(id), "{tool}");
            for invalid in ["two/components", ".."] {
                assert!(!lookup(invalid), "{tool} {invalid}");
            }
            std::fs::remove_file(&file).unwrap();
            std::os::unix::fs::symlink(&outside, &file).unwrap();
            assert!(!lookup(id), "{tool} accepted an outside file");
            if tool == "claude" {
                assert!(claude_transcript(&store, "/different", id, true).is_none());
            }
            if tool == "agy" {
                let db = store.join("conversations").join(format!("{id}.db"));
                std::fs::create_dir_all(db.parent().unwrap()).unwrap();
                std::os::unix::fs::symlink(&outside, db).unwrap();
                assert!(!agy_kept(&store, id));
            }
        }
        std::fs::remove_dir_all(root).unwrap();
    }
}
