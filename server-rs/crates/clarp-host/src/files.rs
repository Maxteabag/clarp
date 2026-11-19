//! An agent's files, read and never written: what an app shows of where an agent works.
//!
//! `GET /agent-files` lists a directory beneath the agent's working directory, `GET
//! /agent-file` hands over a file of text from it. The rules are the Python Host's
//! (`server/lib/agent_files.py`, `routes/agents.py`), and so are the words a request is
//! refused in. Nothing is followed that is a link: each part of a path is opened beneath the
//! directory before it, by the directory that was opened and not by its name, so that a name
//! that is swapped for a link while it is read leads nowhere else. It is the owner looking at
//! his own machine, which is why any directory he names can be the root; a device with
//! limited scope is kept out before it gets here.

use std::fs::{File, OpenOptions};
use std::io::Read;
use std::os::fd::AsRawFd;
use std::os::unix::fs::{MetadataExt, OpenOptionsExt};
use std::path::{Path, PathBuf};

use axum::extract::{RawQuery, State};
use axum::http::StatusCode;
use axum::routing::get;
use axum::{Json, Router};
use nix::libc;
use serde_json::{Value, json};

use crate::edge::{ApiError, App};

/// The largest file that is handed over, and the most entries of a directory that are listed.
const FILE_MOST: u64 = 2 * 1024 * 1024;
const ENTRIES_MOST: usize = 1000;

fn refused(status: StatusCode, why: &str) -> ApiError {
    ApiError::new(status, why)
}

/// The parts of a path beneath an agent's directory. Refused: a path from the root, a part
/// that climbs out, a sign no name has. A part that is nothing or a dot does not count.
fn parts(relative: &str) -> Result<Vec<&str>, ApiError> {
    let invalid = || refused(StatusCode::FORBIDDEN, "invalid workspace-relative path");
    if relative.contains('\0') || relative.starts_with('/') {
        return Err(invalid());
    }
    let named: Vec<&str> = relative
        .split('/')
        .filter(|part| !part.is_empty() && *part != ".")
        .collect();
    if named.contains(&"..") {
        return Err(invalid());
    }
    Ok(named)
}

/// Opens a directory, or what is in an opened directory under one name, following no link.
pub(crate) fn open(at: &Path, directory: bool) -> std::io::Result<File> {
    let kind = if directory {
        libc::O_DIRECTORY
    } else {
        libc::O_NONBLOCK
    };
    OpenOptions::new()
        .read(true)
        .custom_flags(libc::O_NOFOLLOW | kind)
        .open(at)
}

/// What is under a name in a directory that is open: named by the opened directory, not by
/// the directory's own name.
pub(crate) fn beneath(directory: &File, name: &str) -> PathBuf {
    PathBuf::from(format!("/proc/self/fd/{}", directory.as_raw_fd())).join(name)
}

/// Opens what a path names beneath a root, part by part, and answers the root as it really
/// is called.
pub(crate) fn open_beneath(
    root: &Path,
    relative: &str,
    directory: bool,
) -> Result<(PathBuf, File), ApiError> {
    let gone = || {
        refused(
            StatusCode::NOT_FOUND,
            "agent working directory is unavailable",
        )
    };
    let resolved = std::fs::canonicalize(root).map_err(|_| gone())?;
    if !resolved.is_dir() {
        return Err(gone());
    }
    let named = parts(relative)?;
    let failed = |error: std::io::Error| {
        let missing = error.kind() == std::io::ErrorKind::NotFound
            || error.raw_os_error() == Some(libc::ENOTDIR);
        if missing {
            refused(StatusCode::NOT_FOUND, "path not found")
        } else {
            refused(StatusCode::FORBIDDEN, "path cannot be opened safely")
        }
    };
    let mut at = open(&resolved, true).map_err(failed)?;
    for (index, part) in named.iter().enumerate() {
        let last = index + 1 == named.len();
        at = open(&beneath(&at, part), !last || directory).map_err(failed)?;
    }
    Ok((resolved, at))
}

/// When something was last changed, in seconds, as Python reads it off a file.
fn changed_at(found: &std::fs::Metadata) -> f64 {
    #[allow(clippy::cast_precision_loss)]
    let (seconds, nanos) = (found.mtime() as f64, found.mtime_nsec() as f64);
    seconds + 1e-9 * nanos
}

/// A directory beneath a root, as an app is told it: the directories first, then the files,
/// each kind by its name whatever the case of its letters. Links, and what is neither a file
/// nor a directory, are left out.
pub(crate) fn listed(root: &Path, relative: &str) -> Result<Value, ApiError> {
    let (resolved, directory) = open_beneath(root, relative, true)?;
    let unreadable = |error: std::io::Error| refused(StatusCode::BAD_REQUEST, &error.to_string());
    let children = std::fs::read_dir(beneath(&directory, "")).map_err(unreadable)?;
    let under: Vec<&str> = parts(relative)?;
    let mut rows: Vec<(bool, String, Value)> = Vec::new();
    let mut truncated = false;
    for (index, child) in children.enumerate() {
        if index >= ENTRIES_MOST {
            truncated = true;
            break;
        }
        let Ok(child) = child else { continue };
        let Ok(found) = child.metadata() else {
            continue;
        };
        let is_directory = found.is_dir();
        if found.file_type().is_symlink() || !(is_directory || found.is_file()) {
            continue;
        }
        let name = child.file_name().to_string_lossy().into_owned();
        let mut path = under.clone();
        path.push(&name);
        let row = json!({
            "name": name,
            "path": path.join("/"),
            "is_directory": is_directory,
            "size": if is_directory { 0 } else { found.len() },
            "modified_at": changed_at(&found),
        });
        rows.push((!is_directory, name.to_lowercase(), row));
    }
    rows.sort_by(|a, b| (a.0, &a.1).cmp(&(b.0, &b.1)));
    let root_name = resolved.file_name().map_or_else(
        || resolved.display().to_string(),
        |name| name.to_string_lossy().into_owned(),
    );
    Ok(json!({
        "root_name": root_name,
        "root_path": resolved.display().to_string(),
        "path": relative,
        "entries": rows.into_iter().map(|(_, _, row)| row).collect::<Vec<Value>>(),
        "truncated": truncated,
    }))
}

/// A file of text beneath a root, whole. Refused: what is no file, a file over two
/// megabytes, and what is not text.
pub(crate) fn read(root: &Path, relative: &str) -> Result<Value, ApiError> {
    let (_, mut file) = open_beneath(root, relative, false)?;
    let unreadable = |error: std::io::Error| refused(StatusCode::BAD_REQUEST, &error.to_string());
    let found = file.metadata().map_err(unreadable)?;
    if !found.is_file() {
        return Err(refused(StatusCode::BAD_REQUEST, "path is not a file"));
    }
    let too_large = || refused(StatusCode::PAYLOAD_TOO_LARGE, "file is larger than 2 MB");
    if found.len() > FILE_MOST {
        return Err(too_large());
    }
    // One byte more than is taken: a file that grew while it was read is too large too.
    let mut raw = Vec::new();
    let taken = (&mut file).take(FILE_MOST + 1).read_to_end(&mut raw);
    taken.map_err(unreadable)?;
    if raw.len() as u64 > FILE_MOST {
        return Err(too_large());
    }
    let unsupported = |why: &str| refused(StatusCode::UNSUPPORTED_MEDIA_TYPE, why);
    if raw.contains(&0) {
        return Err(unsupported("binary files cannot be previewed"));
    }
    let size = raw.len();
    let content = String::from_utf8(raw).map_err(|_| unsupported("file is not UTF-8 text"))?;
    let name = parts(relative)?.last().copied().unwrap_or("").to_string();
    Ok(json!({
        "path": relative,
        "name": name,
        "content": content,
        "size": size,
        "modified_at": changed_at(&found),
    }))
}

/// One value of a query as it was written: `%` and two signs are a byte, and a plus is a
/// plus. A path may have a plus in it, which a form's reading of a query would make a space.
fn raw_value(query: &str, key: &str) -> String {
    let decoded = |text: &str| -> String {
        let bytes = text.as_bytes();
        let mut out = Vec::with_capacity(bytes.len());
        let mut at = 0;
        while at < bytes.len() {
            let hex = |index: usize| {
                bytes
                    .get(index)
                    .and_then(|byte| (*byte as char).to_digit(16))
            };
            match (bytes[at], hex(at + 1), hex(at + 2)) {
                (b'%', Some(high), Some(low)) => {
                    out.push(u8::try_from(high * 16 + low).unwrap_or(b'?'));
                    at += 3;
                }
                (byte, _, _) => {
                    out.push(byte);
                    at += 1;
                }
            }
        }
        String::from_utf8_lossy(&out).into_owned()
    };
    for field in query.split('&') {
        if let Some((name, value)) = field.split_once('=')
            && decoded(name) == key
        {
            return decoded(value);
        }
    }
    String::new()
}

/// A root as it was asked for, with `~` as the home of whoever runs this Host.
fn expanded(root: &str) -> PathBuf {
    match root.strip_prefix('~') {
        Some("") => crate::config::home(),
        Some(under) if under.starts_with('/') => crate::config::home().join(&under[1..]),
        _ => PathBuf::from(root),
    }
}

/// The agent a request names, and the directory it works in.
fn working_dir(app: &App, session: &str) -> Result<String, ApiError> {
    let directory = app.fleet.directory();
    let member = directory.named(session);
    let member = member.ok_or_else(|| refused(StatusCode::NOT_FOUND, "agent not found"))?;
    Ok(member.record.cwd.clone())
}

/// The root a request reads beneath: the one it names, else the agent's own directory.
fn root_of(cwd: String, asked: &str) -> Result<PathBuf, ApiError> {
    let chosen = if asked.is_empty() {
        cwd
    } else {
        asked.to_string()
    };
    let unavailable = || refused(StatusCode::NOT_FOUND, "agent workspace unavailable");
    if chosen.is_empty() {
        return Err(unavailable());
    }
    let root = expanded(&chosen);
    if !asked.is_empty() && !root.is_absolute() {
        return Err(refused(
            StatusCode::BAD_REQUEST,
            "root path must be absolute",
        ));
    }
    if !root.is_dir() {
        return Err(unavailable());
    }
    Ok(root)
}

/// `GET /agent-files?session=&path=&root=`: a directory beneath where an agent works, or
/// beneath a root the owner names: `@workspace/...` and `@home/...` for what is beneath the
/// agent's directory and the owner's home, or any directory by its whole path.
async fn files(State(app): State<App>, RawQuery(query): RawQuery) -> Result<Json<Value>, ApiError> {
    let query = query.unwrap_or_default();
    let (session, relative) = (raw_value(&query, "session"), raw_value(&query, "path"));
    let mut asked = raw_value(&query, "root");
    let session = session.trim();
    if session.is_empty() {
        return Err(refused(StatusCode::BAD_REQUEST, "session required"));
    }
    let cwd = working_dir(&app, session)?;
    if asked.starts_with('@') {
        let (alias, under) = asked.split_once('/').unwrap_or((asked.as_str(), ""));
        let base = match alias {
            "@workspace" => PathBuf::from(&cwd),
            "@home" => crate::config::home(),
            _ => return Err(refused(StatusCode::BAD_REQUEST, "unknown directory root")),
        };
        // Where the name leads when every link and every `..` in it is taken: it has to be
        // beneath the base as that really is.
        let base = std::fs::canonicalize(&base).unwrap_or(base);
        let joined = base.join(under);
        let candidate = std::fs::canonicalize(&joined).unwrap_or_else(|_| lexical(&joined));
        if !candidate.starts_with(&base) {
            return Err(refused(StatusCode::FORBIDDEN, "directory escapes root"));
        }
        asked = candidate.display().to_string();
    }
    let root = root_of(cwd, &asked)?;
    let listed = tokio::task::spawn_blocking(move || listed(&root, &relative)).await;
    listed
        .map_err(|_| refused(StatusCode::BAD_REQUEST, "the directory could not be read"))?
        .map(Json)
}

/// A path without its `.` and `..`, by its letters alone: where a name that is not there
/// would lead.
fn lexical(path: &Path) -> PathBuf {
    let mut out = PathBuf::new();
    for part in path.components() {
        match part {
            std::path::Component::ParentDir => {
                out.pop();
            }
            std::path::Component::CurDir => {}
            other => out.push(other),
        }
    }
    out
}

/// `GET /agent-file?session=&path=&root=`: a file of text beneath where an agent works, or
/// beneath a directory the owner names by its whole path.
async fn file(State(app): State<App>, RawQuery(query): RawQuery) -> Result<Json<Value>, ApiError> {
    let query = query.unwrap_or_default();
    let (session, relative) = (raw_value(&query, "session"), raw_value(&query, "path"));
    let asked = raw_value(&query, "root");
    let session = session.trim();
    if session.is_empty() || relative.is_empty() {
        return Err(refused(
            StatusCode::BAD_REQUEST,
            "session and path required",
        ));
    }
    let root = root_of(working_dir(&app, session)?, &asked)?;
    let read = tokio::task::spawn_blocking(move || read(&root, &relative)).await;
    read.map_err(|_| refused(StatusCode::BAD_REQUEST, "the file could not be read"))?
        .map(Json)
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/agent-files", get(files))
        .route("/agent-file", get(file))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn why<T: std::fmt::Debug>(result: Result<T, ApiError>) -> (u16, String) {
        let error = result.unwrap_err();
        (error.status().as_u16(), error.message().to_string())
    }

    #[test]
    fn a_directory_is_listed_and_a_file_is_read_and_no_link_is_followed() {
        let root = std::env::temp_dir().join(format!("clarp-files-{}", std::process::id()));
        let work = root.join("work");
        std::fs::create_dir_all(work.join("src/deep")).unwrap();
        std::fs::create_dir_all(work.join("Assets")).unwrap();
        std::fs::create_dir_all(root.join("outside")).unwrap();
        std::fs::write(work.join("b.txt"), "second\n").unwrap();
        std::fs::write(work.join("A.txt"), "first\n").unwrap();
        std::fs::write(work.join("src/deep/notes+plus.md"), "# Notes\n").unwrap();
        std::fs::write(work.join("binary.bin"), [1u8, 0, 2]).unwrap();
        std::fs::write(work.join("latin.txt"), [0xe6u8, 0xf8, 0xe5]).unwrap();
        std::fs::write(root.join("outside/secret.txt"), "not for this view\n").unwrap();
        std::os::unix::fs::symlink(root.join("outside"), work.join("link-out")).unwrap();
        std::os::unix::fs::symlink(work.join("b.txt"), work.join("link-file")).unwrap();

        // The directories first, then the files, each by its name whatever its case; links
        // are not there.
        let top = listed(&work, "").unwrap();
        let names: Vec<&str> = top["entries"]
            .as_array()
            .unwrap()
            .iter()
            .map(|entry| entry["name"].as_str().unwrap())
            .collect();
        assert_eq!(
            names,
            ["Assets", "src", "A.txt", "b.txt", "binary.bin", "latin.txt"]
        );
        assert_eq!(top["root_name"], "work");
        assert_eq!(
            top["root_path"],
            std::fs::canonicalize(&work).unwrap().display().to_string()
        );
        assert_eq!(
            (&top["path"], &top["truncated"]),
            (&json!(""), &json!(false))
        );
        let first = &top["entries"][0];
        assert_eq!(
            (&first["path"], &first["is_directory"], &first["size"]),
            (&json!("Assets"), &json!(true), &json!(0))
        );
        let file = &top["entries"][3];
        assert_eq!(
            (&file["path"], &file["is_directory"], &file["size"]),
            (&json!("b.txt"), &json!(false), &json!(7))
        );
        assert!(file["modified_at"].as_f64().unwrap() > 1.5e9);
        // Beneath it, by a path with parts that do not count.
        let deep = listed(&work, "src//./deep").unwrap();
        assert_eq!(deep["entries"][0]["path"], "src/deep/notes+plus.md");
        assert_eq!(deep["path"], "src//./deep");

        // A file of text, whole.
        let notes = read(&work, "src/deep/notes+plus.md").unwrap();
        assert_eq!(
            (
                &notes["name"],
                &notes["content"],
                &notes["size"],
                &notes["path"]
            ),
            (
                &json!("notes+plus.md"),
                &json!("# Notes\n"),
                &json!(8),
                &json!("src/deep/notes+plus.md")
            )
        );

        // What is refused, in the Python Host's words.
        for (path, status, said) in [
            ("../outside", 403, "invalid workspace-relative path"),
            ("src/../../outside", 403, "invalid workspace-relative path"),
            ("/etc", 403, "invalid workspace-relative path"),
            ("a\0b", 403, "invalid workspace-relative path"),
            ("nothing-of-the-kind", 404, "path not found"),
            ("A.txt/under-a-file", 404, "path not found"),
            ("A.txt", 404, "path not found"),
            // A link to a directory is no directory to one who follows no link: not found,
            // as on the Python Host, which opens with the same flags.
            ("link-out", 404, "path not found"),
        ] {
            assert_eq!(
                why(listed(&work, path)),
                (status, said.to_string()),
                "{path:?}"
            );
        }
        for (path, status, said) in [
            ("link-file", 403, "path cannot be opened safely"),
            ("link-out/secret.txt", 404, "path not found"),
            ("src", 400, "path is not a file"),
            (".", 400, "path is not a file"),
            ("binary.bin", 415, "binary files cannot be previewed"),
            ("latin.txt", 415, "file is not UTF-8 text"),
            ("nothing.txt", 404, "path not found"),
        ] {
            assert_eq!(
                why(read(&work, path)),
                (status, said.to_string()),
                "{path:?}"
            );
        }
        let gone = (404, "agent working directory is unavailable".to_string());
        assert_eq!(why(listed(&root.join("no-such-root"), "")), gone);
        // Two megabytes are handed over, a byte more is not.
        std::fs::write(work.join("large.txt"), vec![b'x'; 2 * 1024 * 1024]).unwrap();
        assert_eq!(read(&work, "large.txt").unwrap()["size"], 2 * 1024 * 1024);
        std::fs::write(work.join("too-large.txt"), vec![b'x'; 2 * 1024 * 1024 + 1]).unwrap();
        let too_large = (413, "file is larger than 2 MB".to_string());
        assert_eq!(why(read(&work, "too-large.txt")), too_large);
        std::fs::remove_dir_all(&root).unwrap();
    }

    #[test]
    fn a_query_is_read_as_it_was_written() {
        let query = "session=echo&path=src%2Fa+b%20c.md&root=%40home/x&empty=&path=second";
        assert_eq!(raw_value(query, "path"), "src/a+b c.md");
        assert_eq!(raw_value(query, "root"), "@home/x");
        assert_eq!(raw_value(query, "empty"), "");
        assert_eq!(raw_value(query, "nothing"), "");
        assert_eq!(raw_value("path=100%25%zz", "path"), "100%%zz");
        assert_eq!(parts("a//./b/").unwrap(), ["a", "b"]);
        assert_eq!(parts("").unwrap(), Vec::<&str>::new());
        assert_eq!(lexical(Path::new("/a/b/../c/./d")), PathBuf::from("/a/c/d"));
    }
}
