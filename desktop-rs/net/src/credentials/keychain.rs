//! Device tokens in the macOS login Keychain: a generic password per Host,
//! service `com.maxteabag.Clarp`, account the Host URL. Through
//! `/usr/bin/security`, so the item's access list names that tool rather
//! than this ad-hoc signed binary, whose identity changes with every
//! build: an updated app still reads its token without a Keychain prompt.
//! The token goes over stdin (`security -i`), never on a command line. A
//! locked Keychain fails ("User interaction is not allowed") rather than
//! prompting.

use std::io::Write;
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

use super::APPLICATION;

const SECURITY: &str = "/usr/bin/security";
/// `errSecItemNotFound`, as `security`'s exit status.
const NOT_FOUND: i32 = 44;
/// A Keychain that wants its password puts up a dialog and `security`
/// waits on it, unseen from SSH: give up rather than hang.
const PATIENCE: Duration = Duration::from_secs(20);

/// `security -i` splits its commands on whitespace and has no escapes, so
/// a value goes in only as one plain word.
fn plain_word(value: &str) -> bool {
    !value.is_empty() && !value.chars().any(|c| c.is_whitespace() || c.is_control() || matches!(c, '"' | '\'' | '\\'))
}

fn failure(output: &Output) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stderr = stderr.trim();
    if stderr.is_empty() { format!("{SECURITY} exited with {}", output.status) } else { format!("Keychain: {stderr}") }
}

/// Runs `security` with `input` on stdin, killing it after `PATIENCE`.
/// Its output is a line or two, well within a pipe's buffer.
fn run(arguments: &[&str], input: Option<&str>) -> Result<Output, String> {
    let mut child = Command::new(SECURITY)
        .args(arguments)
        .stdin(if input.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("{SECURITY}: {error}"))?;
    if let Some(input) = input {
        // Dropped after writing: EOF ends `security -i`.
        let written = child.stdin.take().ok_or("no stdin for the Keychain tool")?.write_all(input.as_bytes());
        if let Err(error) = written {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("{SECURITY}: {error}"));
        }
    }
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return child.wait_with_output().map_err(|error| format!("{SECURITY}: {error}")),
            Ok(None) if started.elapsed() < PATIENCE => std::thread::sleep(Duration::from_millis(25)),
            Ok(None) => {
                let killed = child.kill().and_then(|()| child.wait().map(drop));
                let note = killed.err().map(|error| format!(" (and could not be stopped: {error})")).unwrap_or_default();
                return Err(format!("the Keychain did not answer in {} s; it may be locked and waiting for its password in a dialog on the Mac's screen{note}", PATIENCE.as_secs()));
            }
            Err(error) => return Err(format!("{SECURITY}: {error}")),
        }
    }
}

fn find(server_url: &str) -> Result<String, String> {
    let output = run(&["find-generic-password", "-s", APPLICATION, "-a", server_url, "-w"], None)?;
    match output.status.code() {
        Some(0) => Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned()),
        Some(NOT_FOUND) => Ok(String::new()),
        _ => Err(failure(&output)),
    }
}

fn add(server_url: &str, token: &str) -> Result<(), String> {
    if !plain_word(server_url) || !plain_word(token) {
        return Err("The Host URL or device token has characters the Keychain tool cannot take".into());
    }
    let command = format!("add-generic-password -U -s {APPLICATION} -a {server_url} -w {token}\n");
    let output = run(&["-i"], Some(&command))?;
    // Interactive mode reports a failed command on stderr but exits 0:
    // the token counts as stored only once it reads back.
    match find(server_url) {
        Ok(stored) if stored == token => Ok(()),
        Ok(_) => Err(if output.stderr.is_empty() { "The Keychain did not keep the device token".into() } else { failure(&output) }),
        Err(error) => Err(error),
    }
}

fn delete(server_url: &str) -> Result<(), String> {
    let output = run(&["delete-generic-password", "-s", APPLICATION, "-a", server_url], None)?;
    match output.status.code() {
        Some(0) | Some(NOT_FOUND) => Ok(()),
        _ => Err(failure(&output)),
    }
}

async fn blocking<T: Send + 'static>(work: impl FnOnce() -> Result<T, String> + Send + 'static) -> Result<T, String> {
    tokio::task::spawn_blocking(work).await.map_err(|error| format!("Keychain call failed: {error}"))?
}

/// The stored token for `server_url`, or an empty string when there is none
/// or the Keychain cannot be read.
pub async fn lookup(server_url: &str) -> String {
    let server = server_url.to_owned();
    match blocking(move || find(&server)).await {
        Ok(token) => token,
        Err(error) => {
            eprintln!("credentials: lookup for {server_url} failed: {error}");
            String::new()
        }
    }
}

/// Stores (replacing) the token for `server_url`.
pub async fn store(server_url: &str, token: &str) -> Result<(), String> {
    let (server, token) = (server_url.to_owned(), token.to_owned());
    blocking(move || add(&server, &token)).await
}

/// Deletes the token for `server_url`; succeeds when there was none.
pub async fn remove(server_url: &str) -> Result<(), String> {
    let server = server_url.to_owned();
    blocking(move || delete(&server)).await
}

#[cfg(test)]
mod tests {
    #[test]
    fn only_plain_words_go_to_the_keychain_tool() {
        assert!(super::plain_word("https://laptopstudio.tailf14237.ts.net"));
        assert!(super::plain_word("cld_AbC-123_x"));
        for bad in ["", "a b", "a\nadd-generic-password", "a\"b", "a'b", "a\\b", "a\tb"] {
            assert!(!super::plain_word(bad), "{bad:?}");
        }
    }
}
