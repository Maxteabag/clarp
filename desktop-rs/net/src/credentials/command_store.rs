//! Device tokens retrieved, stored, or removed via user-configured shell
//! commands. Every command receives `CLARP_SERVER_URL` and `CLARP_SERVER_HOST`
//! in its environment, and may also use `{url}` and `{host}` placeholders.
//! Store commands receive the token on stdin so it never appears on a command
//! line or in environment variables.

use std::io::Write;
use std::process::{Command, Output, Stdio};
use std::time::{Duration, Instant};

/// Give up if a command hangs (for example waiting on an interactive prompt).
const PATIENCE: Duration = Duration::from_secs(20);

fn host_of(server_url: &str) -> String {
    url::Url::parse(server_url).ok().and_then(|u| u.host_str().map(str::to_owned)).unwrap_or_default()
}

/// Characters safe to substitute directly into `{url}` or `{host}` inside a
/// shell command (unquoted, single-quoted, or double-quoted) without enabling
/// shell metacharacter expansion.
fn safe_placeholder_value(value: &str) -> bool {
    !value.is_empty()
        && !value
            .chars()
            .any(|c| c.is_whitespace() || c.is_control() || matches!(c, '\'' | '"' | '`' | '$' | '\\' | ';' | '&' | '|' | '<' | '>' | '(' | ')' | '{' | '}' | '*' | '?' | '#' | '!'))
}

fn expand_template(template: &str, server_url: &str, host: &str) -> Result<String, String> {
    let mut expanded = template.to_owned();
    if expanded.contains("{url}") {
        if !safe_placeholder_value(server_url) {
            return Err(format!("Host URL {server_url:?} has characters that cannot be substituted into {{url}}; use $CLARP_SERVER_URL instead"));
        }
        expanded = expanded.replace("{url}", server_url);
    }
    if expanded.contains("{host}") {
        if !safe_placeholder_value(host) {
            return Err(format!("Host name {host:?} has characters that cannot be substituted into {{host}}; use $CLARP_SERVER_HOST instead"));
        }
        expanded = expanded.replace("{host}", host);
    }
    Ok(expanded)
}

fn failure(output: &Output) -> String {
    let stderr = String::from_utf8_lossy(&output.stderr);
    let stderr = stderr.trim();
    if stderr.is_empty() { format!("credential command exited with {}", output.status) } else { format!("credential command failed: {stderr}") }
}

fn run_shell(template: &str, server_url: &str, stdin_input: Option<&str>) -> Result<Output, String> {
    let template = template.trim();
    if template.is_empty() {
        return Err("credential command is empty".into());
    }
    let host = host_of(server_url);
    let expanded = expand_template(template, server_url, &host)?;

    let mut command = if cfg!(windows) {
        let mut c = Command::new("cmd");
        c.args(["/C", &expanded]);
        c
    } else {
        let mut c = Command::new("/bin/sh");
        c.args(["-c", &expanded]);
        c
    };
    command
        .env("CLARP_SERVER_URL", server_url)
        .env("CLARP_SERVER_HOST", &host)
        .stdin(if stdin_input.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    let mut child = command.spawn().map_err(|error| format!("cannot start credential command: {error}"))?;
    if let Some(input) = stdin_input {
        let written = child.stdin.take().ok_or("no standard input for credential command")?.write_all(input.as_bytes());
        if let Err(error) = written {
            let _ = child.kill();
            let _ = child.wait();
            return Err(format!("cannot write token to credential command input: {error}"));
        }
    }

    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => return child.wait_with_output().map_err(|error| format!("credential command failed: {error}")),
            Ok(None) if started.elapsed() < PATIENCE => std::thread::sleep(Duration::from_millis(25)),
            Ok(None) => {
                let killed = child.kill().and_then(|()| child.wait().map(drop));
                let note = killed.err().map(|error| format!(" (and could not be stopped: {error})")).unwrap_or_default();
                return Err(format!("credential command did not finish in {} s{note}", PATIENCE.as_secs()));
            }
            Err(error) => return Err(format!("credential command failed: {error}")),
        }
    }
}

async fn blocking<T: Send + 'static>(work: impl FnOnce() -> Result<T, String> + Send + 'static) -> Result<T, String> {
    tokio::task::spawn_blocking(work).await.map_err(|error| format!("credential command call failed: {error}"))?
}

/// Runs the lookup command and returns its trimmed stdout, or an empty string
/// on failure.
pub async fn lookup(template: String, server_url: &str) -> String {
    if template.trim().is_empty() {
        eprintln!("credentials: backend is 'command' but credentials/command is not set");
        return String::new();
    }
    let server = server_url.to_owned();
    match blocking(move || {
        let output = run_shell(&template, &server, None)?;
        if output.status.success() {
            Ok(String::from_utf8_lossy(&output.stdout).trim().to_owned())
        } else {
            Err(failure(&output))
        }
    })
    .await
    {
        Ok(token) => token,
        Err(error) => {
            eprintln!("credentials: command lookup for {server_url} failed: {error}");
            String::new()
        }
    }
}

/// Runs the optional store command with `token` on stdin.
pub async fn store(template: Option<String>, server_url: &str, token: &str) -> Result<(), String> {
    let Some(template) = template.map(|t| t.trim().to_owned()).filter(|t| !t.is_empty()) else {
        return Err("credential backend 'command' has no store command configured (set credentials/storeCommand, or save the token in your secret store manually)".into());
    };
    let server = server_url.to_owned();
    let token = token.to_owned();
    blocking(move || {
        let output = run_shell(&template, &server, Some(&token))?;
        if output.status.success() { Ok(()) } else { Err(failure(&output)) }
    })
    .await
}

/// Runs the optional remove command.
pub async fn remove(template: Option<String>, server_url: &str) -> Result<(), String> {
    let Some(template) = template.map(|t| t.trim().to_owned()).filter(|t| !t.is_empty()) else {
        return Err("credential backend 'command' has no remove command configured (set credentials/removeCommand, or delete the token from your secret store manually)".into());
    };
    let server = server_url.to_owned();
    blocking(move || {
        let output = run_shell(&template, &server, None)?;
        if output.status.success() { Ok(()) } else { Err(failure(&output)) }
    })
    .await
}

#[cfg(test)]
mod tests {
    use super::{expand_template, host_of};

    #[test]
    fn host_and_placeholders_expand_safely() {
        let url = "https://elitebook2.tailf14237.ts.net";
        assert_eq!(host_of(url), "elitebook2.tailf14237.ts.net");
        assert_eq!(
            expand_template("secret-tool lookup --url {url} --host {host}", url, "elitebook2.tailf14237.ts.net").unwrap(),
            "secret-tool lookup --url https://elitebook2.tailf14237.ts.net --host elitebook2.tailf14237.ts.net"
        );
        assert!(expand_template("echo {url}", "https://host;rm -rf /", "host").is_err());
        assert_eq!(expand_template("echo \"$CLARP_SERVER_URL\"", "https://host;rm -rf /", "host").unwrap(), "echo \"$CLARP_SERVER_URL\"");
    }
}
