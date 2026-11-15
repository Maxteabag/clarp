//! What the administrator does from the command line, by asking the Host that is running.
//!
//! The Host is the one writer of its own tables, so a command never touches them: it asks the
//! Host over the same door the apps use, with the administrator's token, from this machine.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::time::Duration;

use serde_json::{Value, json};

use crate::ids;

pub const PAIR_USAGE: &str =
    "usage: clarp-host pair --url URL [--name DEVICE] [--scope full|limited] \
[--ttl SECONDS] [--host ADDR:PORT]
URL is the address the device will reach this Host at. The administrator's token is read from
CLARP_HOST_TOKEN.";

pub const IMPORT_USAGE: &str =
    "usage: clarp-host import --from ADDR:PORT --session NAME [--host ADDR:PORT]
Reads the agent NAME and its conversation from the Host at --from and gives them to this Host.
The other Host's token is read from CLARP_SOURCE_TOKEN, this Host's from CLARP_HOST_TOKEN.";

/// How many rows of a conversation are asked for at a time.
const PAGE: usize = 1_000;

/// Sends one request to the Host at `address` and returns the status with the body as JSON.
fn request(
    method: &str,
    address: &str,
    path: &str,
    token: &str,
    body: Option<&Value>,
) -> Result<(u16, Value), String> {
    let address = address.trim_start_matches("http://").trim_end_matches('/');
    let mut stream = TcpStream::connect(address)
        .map_err(|error| format!("no Host answers at {address}: {error}"))?;
    let timeout = Some(Duration::from_secs(120));
    stream
        .set_read_timeout(timeout)
        .and_then(|()| stream.set_write_timeout(timeout))
        .map_err(|error| error.to_string())?;
    let body = body.map(Value::to_string).unwrap_or_default();
    let authorization = if token.is_empty() {
        String::new()
    } else {
        format!("Authorization: Bearer {token}\r\n")
    };
    let head = format!(
        "{method} {path} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n{authorization}Content-Type: application/json\r\nContent-Length: {}\r\n\r\n",
        body.len()
    );
    stream
        .write_all(head.as_bytes())
        .and_then(|()| stream.write_all(body.as_bytes()))
        .map_err(|error| error.to_string())?;
    let mut answer = String::new();
    stream
        .read_to_string(&mut answer)
        .map_err(|error| error.to_string())?;
    let (head, body) = answer.split_once("\r\n\r\n").unwrap_or((&answer, ""));
    let status = head
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse().ok())
        .ok_or("the Host's answer was not an HTTP response")?;
    Ok((status, serde_json::from_str(body).unwrap_or(Value::Null)))
}

pub fn post(address: &str, path: &str, token: &str, body: &Value) -> Result<(u16, Value), String> {
    request("POST", address, path, token, Some(body))
}

/// Asks a Host for something and returns it. Anything but a 200 is an error that says what.
fn get(address: &str, path: &str, token: &str) -> Result<Value, String> {
    match request("GET", address, path, token, None)? {
        (200, answer) => Ok(answer),
        (status, answer) => Err(format!(
            "the Host at {address} answered {status} to {path}: {}",
            answer["error"].as_str().unwrap_or("no reason given")
        )),
    }
}

/// `clarp-host import`: moves an agent, with its conversation, from another Host to this one.
///
/// It reads what any app reads from the other Host: the agent's row in the snapshot and the
/// conversation, page by page. The other Host is not changed. Returns what to show.
pub fn import(
    mut args: impl Iterator<Item = String>,
    source_token: &str,
    token: &str,
) -> Result<String, String> {
    let (mut from, mut session) = (None, None);
    let mut host = "127.0.0.1:7690".to_string();
    while let Some(arg) = args.next() {
        let mut value = |what: &str| args.next().ok_or(format!("{arg} needs {what}"));
        match arg.as_str() {
            "--from" => from = Some(value("an address")?),
            "--session" => session = Some(value("a session name")?),
            "--host" => host = value("an address")?,
            other => return Err(format!("unknown argument {other}")),
        }
    }
    let from = from.ok_or("the move needs --from: the address of the Host the agent is on")?;
    let session = session.ok_or("the move needs --session: the agent's session name")?;

    let snapshot = get(&from, "/agents/snapshot", source_token)?;
    let agent = snapshot["agents"]
        .as_array()
        .and_then(|agents| {
            agents
                .iter()
                .find(|agent| agent["session"] == session.as_str())
        })
        .ok_or(format!("the Host at {from} has no agent called {session}"))?
        .clone();
    if agent["busy"] == true {
        return Err(format!(
            "{session} is in the middle of a turn on the Host at {from}: let it end first"
        ));
    }
    // The conversation: the newest page, then further back until nothing is older.
    let mut turns: Vec<Value> = Vec::new();
    let mut path = format!("/log?session={}&limit={PAGE}", ids::in_a_link(&session));
    loop {
        let page = get(&from, &path, source_token)?;
        let rows = page["turns"].as_array().cloned().unwrap_or_default();
        let Some(oldest) = rows.first().and_then(|row| row["id"].as_str()) else {
            break;
        };
        path = format!(
            "/log?session={}&limit={PAGE}&before={}",
            ids::in_a_link(&session),
            ids::in_a_link(oldest)
        );
        turns.splice(0..0, rows);
        if page["has_more"] != true {
            break;
        }
    }
    let count = turns.len();
    let asked = json!({ "agent": agent, "from": format!("http://{from}"), "turns": turns });
    let (status, answer) = post(&host, "/agents/import", token, &asked)?;
    if status != 200 {
        let why = answer["error"].as_str().unwrap_or("no reason given");
        return Err(format!("this Host refused ({status}): {why}"));
    }
    Ok(format!(
        "{session} is on this Host now, with the {count} rows of its conversation.\n\
         It is still on the Host at {from} too. Archive it there, so that only one of the two takes its turns."
    ))
}

/// What a tool run by an agent ended with: its exit code, and what it prints.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct ToolOutcome {
    pub exit: u8,
    pub stdout: String,
    pub stderr: String,
}

/// `clarp-host tool NAME [ARGS...]`: one of an agent's own tools. It is a pipe: what it was
/// called with goes to the Host the agent runs on, and what the Host answers is printed. The
/// Host is found by what it told the turn (`CLARP_HOST_ADDR`); a tool that is called where no
/// Host said so does nothing and says why.
pub fn tool(mut args: impl Iterator<Item = String>, token: &str) -> ToolOutcome {
    let unavailable = |why: String| ToolOutcome {
        exit: 69,
        stdout: String::new(),
        stderr: format!("{why}\n"),
    };
    let Some(name) = args.next() else {
        return ToolOutcome {
            exit: 2,
            stdout: String::new(),
            stderr: "usage: clarp-host tool NAME [ARGS...]\n".to_string(),
        };
    };
    let Ok(address) = std::env::var(crate::TOOL_ADDR_ENV) else {
        return unavailable(format!(
            "{name}: this agent's Host is not known ({} is not set). Nothing was done.",
            crate::TOOL_ADDR_ENV
        ));
    };
    // What the Host may need to know of where the tool was called. Never a token.
    let env: std::collections::BTreeMap<String, String> = std::env::vars()
        .filter(|(key, _)| key.starts_with("CLARP_") || key.starts_with("CLAUDE_PWA_"))
        .filter(|(key, _)| key != "CLARP_HOST_TOKEN" && key != "CLARP_SOURCE_TOKEN")
        .collect();
    let cwd = std::env::current_dir()
        .map(|dir| dir.display().to_string())
        .unwrap_or_default();
    let asked = json!({
        "tool": name,
        "argv": args.collect::<Vec<_>>(),
        "cwd": cwd,
        "env": env,
        "ppid": std::os::unix::process::parent_id(),
    });
    match post(&address, "/tools/run", token, &asked) {
        Ok((200, answer)) => ToolOutcome {
            exit: answer["exit"]
                .as_i64()
                .and_then(|code| u8::try_from(code).ok())
                .unwrap_or(1),
            stdout: answer["stdout"].as_str().unwrap_or("").to_string(),
            stderr: answer["stderr"].as_str().unwrap_or("").to_string(),
        },
        Ok((status, answer)) => unavailable(format!(
            "{name}: the Host at {address} refused ({status}): {}",
            answer["error"].as_str().unwrap_or("no reason given")
        )),
        Err(why) => unavailable(format!(
            "{name}: the Host at {address} does not answer: {why}"
        )),
    }
}

/// `clarp-host pair`: has the running Host issue a pairing code, and returns what to show.
pub fn pair(mut args: impl Iterator<Item = String>, token: &str) -> Result<String, String> {
    let (mut url, mut name, mut scope, mut ttl) =
        (None, "iPhone".to_string(), "full".to_string(), 600i64);
    let mut host = "127.0.0.1:7690".to_string();
    while let Some(arg) = args.next() {
        let mut value = |what: &str| args.next().ok_or(format!("{arg} needs {what}"));
        match arg.as_str() {
            "--url" => url = Some(value("an address")?),
            "--name" => name = value("a name")?,
            "--scope" => scope = value("full or limited")?,
            "--ttl" => {
                ttl = value("a number of seconds")?
                    .parse()
                    .map_err(|_| "--ttl needs a number of seconds".to_string())?;
            }
            "--host" => host = value("an address")?,
            other => return Err(format!("unknown argument {other}")),
        }
    }
    let url = url.ok_or("pairing needs --url: the address the device will reach this Host at")?;
    let asked = json!({ "device_name": name, "scope": scope, "ttl_seconds": ttl, "url": url });
    let (status, answer) = post(&host, "/pairing/issue", token, &asked)?;
    if status != 200 {
        let why = answer["error"].as_str().unwrap_or("no reason given");
        return Err(format!("the Host refused ({status}): {why}"));
    }
    let uri = answer["uri"]
        .as_str()
        .ok_or("the Host's answer had no pairing link")?;
    Ok(format!(
        "{uri}\nOpen this link on the device. It expires in {ttl} seconds and works once."
    ))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    fn words(text: &str) -> impl Iterator<Item = String> + use<> {
        text.split_whitespace()
            .map(str::to_string)
            .collect::<Vec<_>>()
            .into_iter()
    }

    #[test]
    fn a_pairing_needs_an_address_and_a_host_that_answers() {
        assert!(pair(words(""), "").unwrap_err().contains("--url"));
        assert!(
            pair(words("--ttl soon --url http://x"), "")
                .unwrap_err()
                .contains("seconds")
        );
        assert!(
            pair(words("--what"), "")
                .unwrap_err()
                .contains("unknown argument")
        );
        // Nothing listens on port 1.
        let nobody = pair(words("--url http://x --host 127.0.0.1:1"), "");
        assert!(nobody.unwrap_err().contains("no Host answers"));
    }

    #[test]
    fn a_move_needs_to_know_where_from_and_whom() {
        assert!(
            import(words("--session maximus"), "", "")
                .unwrap_err()
                .contains("--from")
        );
        assert!(
            import(words("--from 127.0.0.1:1"), "", "")
                .unwrap_err()
                .contains("--session")
        );
        let nobody = import(
            words("--from http://127.0.0.1:1/ --session maximus"),
            "",
            "",
        );
        assert!(
            nobody
                .unwrap_err()
                .contains("no Host answers at 127.0.0.1:1")
        );
    }
}
