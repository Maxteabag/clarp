//! What the administrator does from the command line, by asking the Host that is running.
//!
//! The Host is the one writer of its own tables, so a command never touches them: it asks the
//! Host over the same door the apps use, with the administrator's token, from this machine.

use std::io::{Read, Write};
use std::net::TcpStream;
use std::path::PathBuf;
use std::time::Duration;

use serde_json::{Value, json};

use crate::ids;

pub const PAIR_USAGE: &str =
    "usage: clarp-host pair --url URL [--name DEVICE] [--scope full|limited] \
[--ttl SECONDS] [--host ADDR:PORT]
URL is the address the device will reach this Host at. The administrator's token is read from
CLARP_HOST_TOKEN.";

pub const PEER_USAGE: &str =
    "usage: clarp-host peer add NAME URL | peer list | peer remove NAME  [--host ADDR:PORT]
Names another Host this one may write to: an agent here then writes to an agent there with
`clarp-admin prompt --server NAME`. The other Host's token is read from CLARP_PEER_TOKEN, this
Host's from CLARP_HOST_TOKEN.";

pub const IMPORT_USAGE: &str =
    "usage: clarp-host import --from ADDR:PORT --session NAME [--host ADDR:PORT] [--files]
Reads the agent NAME, its conversation and the files it published from the Host at --from and
gives them to this Host. With --files only the files are read, for an agent that is here
already: a move whose files did not all arrive is finished that way.
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

/// One request whose body and answer are bytes: a file on its way from one Host to another.
/// Returns the status, the answer's declared type and its body.
fn exchange(
    method: &str,
    address: &str,
    path: &str,
    token: &str,
    said: &[(&str, String)],
    body: &[u8],
) -> Result<(u16, String, Vec<u8>), String> {
    let address = address.trim_start_matches("http://").trim_end_matches('/');
    let mut stream = TcpStream::connect(address)
        .map_err(|error| format!("no Host answers at {address}: {error}"))?;
    let timeout = Some(Duration::from_secs(120));
    stream
        .set_read_timeout(timeout)
        .and_then(|()| stream.set_write_timeout(timeout))
        .map_err(|error| error.to_string())?;
    let mut head = format!(
        "{method} {path} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\nContent-Length: {}\r\n",
        body.len()
    );
    if !token.is_empty() {
        head.push_str(&format!("Authorization: Bearer {token}\r\n"));
    }
    for (name, value) in said {
        head.push_str(&format!("{name}: {value}\r\n"));
    }
    head.push_str("\r\n");
    stream
        .write_all(head.as_bytes())
        .and_then(|()| stream.write_all(body))
        .map_err(|error| error.to_string())?;
    let mut answer = Vec::new();
    stream
        .read_to_end(&mut answer)
        .map_err(|error| error.to_string())?;
    let end = answer
        .windows(4)
        .position(|window| window == b"\r\n\r\n")
        .ok_or("the Host's answer was not an HTTP response")?;
    let head = String::from_utf8_lossy(&answer[..end]).into_owned();
    let status = head
        .split_whitespace()
        .nth(1)
        .and_then(|code| code.parse().ok())
        .ok_or("the Host's answer was not an HTTP response")?;
    let declared = head
        .lines()
        .filter_map(|line| line.split_once(':'))
        .find(|(name, _)| name.trim().eq_ignore_ascii_case("content-type"))
        .map_or("", |(_, value)| value.trim())
        .to_string();
    Ok((status, declared, answer.split_off(end + 4)))
}

/// The ids of the published files a conversation names: in the Markdown that shows an
/// image (`clarp-media://asset/<id>`) and in the address a file is fetched by
/// (`/media/<id>`). In the order they are first named, each once.
fn named_files(rows: &[Value]) -> Vec<String> {
    let mut found: Vec<String> = Vec::new();
    for text in rows.iter().filter_map(|row| row["text"].as_str()) {
        for marker in ["clarp-media://asset/", "/media/"] {
            for after in text.split(marker).skip(1) {
                let plain =
                    |sign: &char| sign.is_ascii_alphanumeric() || *sign == '_' || *sign == '-';
                let id: String = after.chars().take_while(plain).collect();
                if !id.is_empty() && !found.contains(&id) {
                    found.push(id);
                }
            }
        }
    }
    found
}

/// The files an agent published come with it: its gallery, and every file its conversation
/// names. Each is read from the Host it is on and given to this one under the id it has,
/// which the conversation names it by. Returns how many arrived and how many did not.
fn files(
    from: &str,
    source_token: &str,
    host: &str,
    token: &str,
    session: &str,
    rows: &[Value],
) -> Result<(usize, usize), String> {
    let gallery = get(
        from,
        &format!("/media?session={}&limit=500", ids::in_a_link(session)),
        source_token,
    )?;
    let listed = gallery["assets"].as_array().cloned().unwrap_or_default();
    let known = |id: &str| listed.iter().find(|asset| asset["asset_id"] == id);
    let mut wanted: Vec<String> = listed
        .iter()
        .rev()
        .filter_map(|asset| asset["asset_id"].as_str().map(str::to_string))
        .collect();
    for id in named_files(rows) {
        if !wanted.contains(&id) {
            wanted.push(id);
        }
    }
    let (mut arrived, mut missing) = (0, 0);
    for id in wanted {
        let path = format!("/media/{}", ids::in_a_link(&id));
        let (status, declared, bytes) = exchange("GET", from, &path, source_token, &[], &[])?;
        if status != 200 {
            // Named in the conversation and no longer there, or never a file at all.
            missing += 1;
            continue;
        }
        let row = known(&id);
        let of = |field: &str| row.and_then(|row| row[field].as_str()).unwrap_or("");
        // Who published it is said as it is, when it is a word a header can carry.
        let plain = |text: &str| {
            let fits = text
                .bytes()
                .all(|byte| byte == b' ' || byte.is_ascii_graphic());
            if fits { text } else { "" }.to_string()
        };
        let mut said = vec![
            ("X-Session", ids::in_a_link(session)),
            ("X-Asset-Id", id.clone()),
            ("X-File-Name", ids::in_a_link(of("source_name"))),
            ("X-Caption", ids::in_a_link(of("caption"))),
            ("X-Created-By", plain(of("created_by"))),
            (
                "Content-Type",
                match of("mime_type") {
                    "" => declared,
                    kind => kind.to_string(),
                },
            ),
        ];
        if let Some(at) = row.and_then(|row| row["created_at"].as_i64()) {
            said.push(("X-Created-At", at.to_string()));
        }
        match exchange("POST", host, "/media", token, &said, &bytes)? {
            (200, _, _) => arrived += 1,
            _ => missing += 1,
        }
    }
    Ok((arrived, missing))
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
    let (mut from, mut session, mut only_files) = (None, None, false);
    let mut host = "127.0.0.1:7690".to_string();
    while let Some(arg) = args.next() {
        let mut value = |what: &str| args.next().ok_or(format!("{arg} needs {what}"));
        match arg.as_str() {
            "--from" => from = Some(value("an address")?),
            "--session" => session = Some(value("a session name")?),
            "--host" => host = value("an address")?,
            "--files" => only_files = true,
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
    if agent["busy"] == true && !only_files {
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
    let came_with = |arrived: usize, missing: usize| {
        let files = match arrived {
            1 => "1 file it published".to_string(),
            arrived => format!("{arrived} files it published"),
        };
        match missing {
            0 => files,
            missing => format!(
                "{files} ({missing} more that its conversation names could not be read \
                 there or were not taken here: `clarp-host import --files` tries them again)"
            ),
        }
    };
    if only_files {
        let (arrived, missing) = files(&from, source_token, &host, token, &session, &turns)?;
        return Ok(format!(
            "{session} has {} here.",
            came_with(arrived, missing)
        ));
    }
    let asked = json!({ "agent": agent, "from": format!("http://{from}"), "turns": turns });
    let (status, answer) = post(&host, "/agents/import", token, &asked)?;
    if status != 200 {
        let why = answer["error"].as_str().unwrap_or("no reason given");
        return Err(format!("this Host refused ({status}): {why}"));
    }
    // The agent is here. Its files follow: one that does not arrive does not undo the move.
    let followed = match files(&from, source_token, &host, token, &session, &turns) {
        Ok((arrived, missing)) => came_with(arrived, missing),
        Err(why) => format!("none of its files ({why}): `clarp-host import --files` brings them"),
    };
    Ok(format!(
        "{session} is on this Host now, with the {count} rows of its conversation and {followed}.\n\
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

/// The Host a `config.toml` names, as the installer writes one for the Rust Host: its
/// address and the token it takes. `None` for any other file: a tool of this Host never
/// speaks to a Host of another kind.
fn host_in(config: &str) -> Option<(String, String)> {
    let (mut ours, mut address, mut port, mut token) = (false, "", "", "");
    let mut in_server = false;
    for line in config.lines().map(str::trim) {
        if line.starts_with('[') {
            in_server = line == "[server]";
            continue;
        }
        let Some((key, value)) = line.split_once('=') else {
            continue;
        };
        let value = value.trim().trim_matches('"');
        match (in_server, key.trim()) {
            (true, "kind") => ours = value == "clarp-host",
            (true, "bind_addr") => address = value,
            (true, "port") => port = value,
            (true, "auth_token") => token = value,
            _ => {}
        }
    }
    (ours && !address.is_empty() && !port.is_empty())
        .then(|| (format!("{address}:{port}"), token.to_string()))
}

/// The Host this installation runs, for a tool that no turn told where its Host is: a worker
/// an agent left running, which carries the agent's world but not the turn's own words. It is
/// in the file the installer wrote, which that world names.
fn installed_host() -> Option<(String, String)> {
    let named = |variable: &str| std::env::var_os(variable).filter(|value| !value.is_empty());
    let path = named("CLAUDE_PWA_CONFIG")
        .map(PathBuf::from)
        .or_else(|| named("CLARP_CONFIG_DIR").map(|dir| PathBuf::from(dir).join("config.toml")))
        .or_else(|| {
            let config = named("XDG_CONFIG_HOME")
                .map(PathBuf::from)
                .or_else(|| named("HOME").map(|home| PathBuf::from(home).join(".config")))?;
            Some(config.join("clarp-host").join("config.toml"))
        })?;
    host_in(&std::fs::read_to_string(path).ok()?)
}

/// `clarp-host tool NAME [ARGS...]`: one of an agent's own tools. It is a pipe: what it was
/// called with goes to the Host the agent runs on, and what the Host answers is printed. The
/// Host is found by what it told the turn (`CLARP_HOST_ADDR`), or, for a worker an agent left
/// running, in the file the installer wrote for the agents of this Host. A tool that is
/// called where neither says so does nothing and says why.
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
    let (address, token) = match (std::env::var(crate::TOOL_ADDR_ENV), installed_host()) {
        (Ok(address), _) => (address, token.to_string()),
        // The token the caller has, when it has one; else the one in the file.
        (Err(_), Some((address, kept))) if token.is_empty() => (address, kept),
        (Err(_), Some((address, _))) => (address, token.to_string()),
        (Err(_), None) => {
            return unavailable(format!(
                "{name}: this agent's Host is not known ({} is not set, and no file of an \
                 installed Rust Host names one). Nothing was done.",
                crate::TOOL_ADDR_ENV
            ));
        }
    };
    let token = token.as_str();
    // What the Host may need to know of where the tool was called. Never a token.
    let env: std::collections::BTreeMap<String, String> = std::env::vars()
        .filter(|(key, _)| key.starts_with("CLARP_") || key.starts_with("CLAUDE_PWA_"))
        .filter(|(key, _)| key != "CLARP_HOST_TOKEN" && key != "CLARP_SOURCE_TOKEN")
        .collect();
    let cwd = std::env::current_dir()
        .map(|dir| dir.display().to_string())
        .unwrap_or_default();
    let started_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |since| i64::try_from(since.as_millis()).unwrap_or(0));
    let asked = json!({
        "tool": name,
        "argv": args.collect::<Vec<_>>(),
        "cwd": cwd,
        "env": env,
        "ppid": std::os::unix::process::parent_id(),
        "started_ms": started_ms,
    });
    loop {
        let answer = match post(&address, "/tools/run", token, &asked) {
            Ok((200, answer)) => answer,
            Ok((status, answer)) => {
                return unavailable(format!(
                    "{name}: the Host at {address} refused ({status}): {}",
                    answer["error"].as_str().unwrap_or("no reason given")
                ));
            }
            Err(why) => {
                return unavailable(format!(
                    "{name}: the Host at {address} does not answer: {why}"
                ));
            }
        };
        // What the tool waits for has not happened yet: the Host says when to ask again.
        if let Some(again_ms) = answer["again_ms"].as_u64() {
            std::thread::sleep(Duration::from_millis(again_ms.clamp(100, 60_000)));
            continue;
        }
        return ToolOutcome {
            exit: answer["exit"]
                .as_i64()
                .and_then(|code| u8::try_from(code).ok())
                .unwrap_or(1),
            stdout: answer["stdout"].as_str().unwrap_or("").to_string(),
            stderr: answer["stderr"].as_str().unwrap_or("").to_string(),
        };
    }
}

/// `clarp-host peer`: names another Host for the running Host, lists the ones it knows, or
/// forgets one. Returns what to show.
pub fn peer(
    mut args: impl Iterator<Item = String>,
    token: &str,
    peer_token: &str,
) -> Result<String, String> {
    let mut words: Vec<String> = Vec::new();
    let mut host = "127.0.0.1:7690".to_string();
    while let Some(arg) = args.next() {
        if arg == "--host" {
            host = args.next().ok_or("--host needs an address")?;
        } else {
            words.push(arg);
        }
    }
    let refused = |status: u16, answer: &Value| {
        let why = answer["error"].as_str().unwrap_or("no reason given");
        format!("the Host refused ({status}): {why}")
    };
    let words: Vec<&str> = words.iter().map(String::as_str).collect();
    match words.as_slice() {
        ["add", name, url] => {
            if peer_token.is_empty() {
                return Err("the other Host's token is read from CLARP_PEER_TOKEN".to_string());
            }
            let named = json!({ "name": name, "url": url, "token": peer_token });
            match post(&host, "/peers", token, &named)? {
                (200, answer) => Ok(format!(
                    "{} is the Host at {}",
                    answer["name"].as_str().unwrap_or(name),
                    answer["url"].as_str().unwrap_or(url)
                )),
                (status, answer) => Err(refused(status, &answer)),
            }
        }
        ["list"] => {
            let peers = get(&host, "/peers", token)?;
            let lines: Vec<String> = peers
                .as_array()
                .map(Vec::as_slice)
                .unwrap_or_default()
                .iter()
                .map(|peer| {
                    let off = if peer["enabled"] == false {
                        " (off)"
                    } else {
                        ""
                    };
                    format!(
                        "{} {}{off}",
                        peer["name"].as_str().unwrap_or(""),
                        peer["url"].as_str().unwrap_or("")
                    )
                })
                .collect();
            Ok(lines.join("\n"))
        }
        ["remove", name] => {
            match request("DELETE", &host, &format!("/peers/{name}"), token, None)? {
                (200, _) => Ok(format!("removed peer: {name}")),
                (status, answer) => Err(refused(status, &answer)),
            }
        }
        _ => Err("say what to do: add NAME URL, list, or remove NAME".to_string()),
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

    #[test]
    fn a_tool_finds_its_host_in_the_file_the_installer_wrote_and_in_no_other() {
        let written = "# Written by install-host.\n[server]\nkind = \"clarp-host\"\n\
                       bind_addr = \"127.0.0.1\"\nport = 7690\nauth_token = \"s3cret\"\n";
        assert_eq!(
            host_in(written),
            Some(("127.0.0.1:7690".to_string(), "s3cret".to_string()))
        );
        // A Python Host's file has the same fields, and is not this Host's.
        let python = "[server]\nbind_addr = \"127.0.0.1\"\nport = 7682\nauth_token = \"other\"\n";
        assert_eq!(host_in(python), None);
        // What stands under another heading is not the Host's address.
        let elsewhere = "[server]\nkind = \"clarp-host\"\n[tts]\nbind_addr = \"x\"\nport = 1\n";
        assert_eq!(host_in(elsewhere), None);
        assert_eq!(host_in(""), None);
    }

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
