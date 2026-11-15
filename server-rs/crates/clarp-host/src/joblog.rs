//! The tail of the log a background job's worker writes.
//!
//! A worker names a file; whoever looks at the job is shown its last lines. The Host reads a
//! file for someone else here, so it reads only what a worker could have written as a log: a
//! regular file whose real path lies under the worker's directory, the owner's directory, or
//! `/var/tmp`. A link anywhere in the path cannot lead it elsewhere: the path is resolved
//! first, and its last part is opened without following a link.

use std::io::{Read, Seek, SeekFrom};
use std::os::unix::fs::OpenOptionsExt;
use std::path::{Path, PathBuf};

use serde_json::{Value, json};

/// How much of a log is shown.
pub const TAIL_BYTES: u64 = 64 * 1024;
/// Where any worker may keep a log.
const SHARED_ROOTS: [&str; 1] = ["/var/tmp"];

/// The directories a job's log may be in, resolved. The root of the file system is never one:
/// a worker that was started there must not make every file a log.
pub fn roots(worker_cwd: &str, owner_cwd: &str) -> Vec<PathBuf> {
    let mut roots: Vec<PathBuf> = Vec::new();
    for raw in [worker_cwd, owner_cwd].into_iter().chain(SHARED_ROOTS) {
        let raw = raw.trim();
        if !raw.starts_with('/') {
            continue;
        }
        let real = resolved(Path::new(raw));
        if real != Path::new("/") && !roots.contains(&real) {
            roots.push(real);
        }
    }
    roots
}

/// A path with its links followed. A path that does not exist yet is resolved as far as it
/// does: its directory, with its own name after it.
fn resolved(path: &Path) -> PathBuf {
    if let Ok(real) = std::fs::canonicalize(path) {
        return real;
    }
    match (path.parent(), path.file_name()) {
        (Some(dir), Some(name)) => resolved(dir).join(name),
        _ => path.to_path_buf(),
    }
}

fn withheld(path: &str, reason: &str) -> Value {
    json!({ "path": path, "available": false, "reason": reason, "text": "", "size": 0, "truncated": false })
}

/// What a caller who may not read logs at all is given.
pub fn forbidden() -> Value {
    withheld("", "forbidden")
}

/// The last `limit` bytes of a job's log, from a line's beginning, or why it is withheld.
pub fn tail(path: &str, roots: &[PathBuf], limit: u64) -> Value {
    if path.is_empty() {
        return withheld(path, "no_log");
    }
    if !path.starts_with('/') {
        return withheld(path, "not_absolute");
    }
    let real = resolved(Path::new(path));
    if !roots.iter().any(|root| real.starts_with(root)) {
        return withheld(path, "outside_allowed_roots");
    }
    let opened = std::fs::OpenOptions::new()
        .read(true)
        .custom_flags(nix::libc::O_NOFOLLOW | nix::libc::O_NONBLOCK)
        .open(&real);
    let mut file = match opened {
        Ok(file) => file,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
            return withheld(path, "missing");
        }
        Err(_) => return withheld(path, "unreadable"),
    };
    let size = match file.metadata() {
        Ok(info) if info.is_file() => info.len(),
        Ok(_) => return withheld(path, "not_a_regular_file"),
        Err(_) => return withheld(path, "unreadable"),
    };
    let start = size.saturating_sub(limit.max(1));
    let mut data = Vec::new();
    if file.seek(SeekFrom::Start(start)).is_err()
        || file.take(size - start).read_to_end(&mut data).is_err()
    {
        return withheld(path, "unreadable");
    }
    if start > 0 {
        // What is shown starts where a line starts.
        if let Some(newline) = data.iter().position(|byte| *byte == b'\n')
            && newline < data.len() - 1
        {
            data.drain(..=newline);
        }
    }
    json!({
        "path": path,
        "available": true,
        "reason": "",
        "text": String::from_utf8_lossy(&data),
        "size": size,
        "truncated": start > 0,
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn scratch(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("clarp-joblog-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::canonicalize(dir).unwrap()
    }

    #[test]
    fn a_log_is_shown_from_a_lines_beginning_and_only_its_end() {
        let dir = scratch("tail");
        let log = dir.join("build.log");
        std::fs::write(&log, "first line\nsecond line\nthird line\n").unwrap();
        let roots = roots(dir.to_str().unwrap(), "");
        let all = tail(log.to_str().unwrap(), &roots, TAIL_BYTES);
        assert_eq!(
            (&all["available"], &all["truncated"], &all["size"]),
            (&json!(true), &json!(false), &json!(34))
        );
        assert_eq!(all["text"], "first line\nsecond line\nthird line\n");
        let end = tail(log.to_str().unwrap(), &roots, 16);
        assert_eq!(
            (&end["text"], &end["truncated"]),
            (&json!("third line\n"), &json!(true)),
            "the cut-off line is left out"
        );
        let _ = std::fs::remove_dir_all(dir);
    }

    #[test]
    fn only_what_could_be_a_workers_log_is_read() {
        let dir = scratch("fence");
        let roots = roots(dir.to_str().unwrap(), "relative/is/no/root");
        let reason = |path: &str| tail(path, &roots, TAIL_BYTES)["reason"].clone();
        assert_eq!(reason(""), "no_log");
        assert_eq!(reason("build.log"), "not_absolute");
        assert_eq!(reason("/etc/hostname"), "outside_allowed_roots");
        assert_eq!(reason(dir.join("nothing.log").to_str().unwrap()), "missing");
        assert_eq!(reason(dir.to_str().unwrap()), "not_a_regular_file");
        // A link out of the directory leads nowhere: its real path is what counts.
        let link = dir.join("link.log");
        std::os::unix::fs::symlink("/etc/hostname", &link).unwrap();
        assert_eq!(reason(link.to_str().unwrap()), "outside_allowed_roots");
        // The whole file system is never a directory of logs.
        assert!(!super::roots("/", "/").contains(&PathBuf::from("/")));
        assert!(super::roots("", "").contains(&std::fs::canonicalize("/var/tmp").unwrap()));
        assert_eq!(forbidden()["reason"], "forbidden");
        let _ = std::fs::remove_dir_all(dir);
    }
}
