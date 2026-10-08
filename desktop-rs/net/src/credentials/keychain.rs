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

use super::APPLICATION;

const SECURITY: &str = "/usr/bin/security";
/// `errSecItemNotFound`, as `security`'s exit status.
const NOT_FOUND: i32 = 44;

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

fn run(arguments: &[&str]) -> Result<Output, String> {
    Command::new(SECURITY).args(arguments).stdin(Stdio::null()).output().map_err(|error| format!("{SECURITY}: {error}"))
}

fn find(server_url: &str) -> Result<String, String> {
    let output = run(&["find-generic-password", "-s", APPLICATION, "-a", server_url, "-w"])?;
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
    let mut child = Command::new(SECURITY)
        .arg("-i")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("{SECURITY}: {error}"))?;
    let command = format!("add-generic-password -U -s {APPLICATION} -a {server_url} -w {token}\n");
    let written = child.stdin.take().ok_or("no stdin for the Keychain tool")?.write_all(command.as_bytes());
    let output = child.wait_with_output().map_err(|error| format!("{SECURITY}: {error}"))?;
    written.map_err(|error| format!("{SECURITY}: {error}"))?;
    // Interactive mode reports a failed command on stderr but exits 0:
    // the token counts as stored only once it reads back.
    match find(server_url) {
        Ok(stored) if stored == token => Ok(()),
        Ok(_) => Err(if output.stderr.is_empty() { "The Keychain did not keep the device token".into() } else { failure(&output) }),
        Err(error) => Err(error),
    }
}

fn delete(server_url: &str) -> Result<(), String> {
    let output = run(&["delete-generic-password", "-s", APPLICATION, "-a", server_url])?;
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
