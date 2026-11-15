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
Reads the agent NAME, its conversation, its library, its plans and goals and the files it
published or shows from the Host at --from and gives them to this Host. With --files only
the files are given, for an agent that is here already: a move whose files did not all
arrive is finished that way.
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

/// Adds to `found` the ids of the published files a text names: in the Markdown that shows
/// an image (`clarp-media://asset/<id>`) and in the address a file is fetched by
/// (`/media/<id>`). In the order they are first named, each once. A path of the machine
/// that merely has `/media/` in it names no file: an id begins with `asset_`.
fn named_in(text: &str, found: &mut Vec<String>) {
    for marker in ["clarp-media://asset/", "/media/"] {
        for after in text.split(marker).skip(1) {
            let plain = |sign: &char| sign.is_ascii_alphanumeric() || *sign == '_' || *sign == '-';
            let id: String = after.chars().take_while(plain).collect();
            if id.starts_with("asset_") && id.len() > 6 && !found.contains(&id) {
                found.push(id);
            }
        }
    }
}

/// A file a move wants: the id it has, and what is known of it.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
struct Wanted {
    id: String,
    name: String,
    caption: String,
    created_by: String,
    /// Its kind where that is known. Where it is not, the Host it is read from says it.
    mime: String,
    created_at: Option<i64>,
    /// Named, and not known to be the agent's own.
    named_only: bool,
}

/// The files a move wants, each once.
///
/// First the agent's own. Its pictures, as the Host it is on lists them, the oldest first.
/// Then the files its recordings, films and documents in the library are made of: that Host
/// let it make them of its own files only, under the name and the kind the artifact says,
/// so that much is known of them, and the time is the artifact's, which was made right
/// after its file.
///
/// Then whatever else its library, its plans and its conversation name. Such a file may be
/// another agent's, which this agent shows: it comes along, and is not made this agent's.
fn wanted(gallery: &[Value], library: &[Value], plans: &[Value], rows: &[Value]) -> Vec<Wanted> {
    let text = |value: &Value| value.as_str().unwrap_or("").to_string();
    let plain = |id: &str| {
        let sign = |byte: u8| byte.is_ascii_alphanumeric() || byte == b'_' || byte == b'-';
        (1..=180).contains(&id.len()) && id.bytes().all(sign)
    };
    let mut wanted: Vec<Wanted> = Vec::new();
    for asset in gallery.iter().rev() {
        let id = text(&asset["asset_id"]);
        if plain(&id) && !wanted.iter().any(|want| want.id == id) {
            wanted.push(Wanted {
                id,
                name: text(&asset["source_name"]),
                caption: text(&asset["caption"]),
                created_by: text(&asset["created_by"]),
                mime: text(&asset["mime_type"]),
                created_at: asset["created_at"].as_i64(),
                named_only: false,
            });
        }
    }
    for artifact in library.iter().map(|entry| &entry["artifact"]) {
        let of_a_file = ["audio", "video", "file"]
            .iter()
            .any(|kind| artifact["type"] == *kind);
        let payload = &artifact["payload"];
        let id = payload["url"]
            .as_str()
            .and_then(|url| url.strip_prefix("/media/"))
            .unwrap_or("");
        if of_a_file && plain(id) && !wanted.iter().any(|want| want.id == id) {
            wanted.push(Wanted {
                id: id.to_string(),
                name: text(&payload["file_name"]),
                mime: text(&payload["mime_type"]),
                created_at: artifact["created_at"].as_i64(),
                ..Wanted::default()
            });
        }
    }
    let mut named: Vec<String> = Vec::new();
    for said in library.iter().chain(plans) {
        named_in(&said.to_string(), &mut named);
    }
    for said in rows.iter().filter_map(|row| row["text"].as_str()) {
        named_in(said, &mut named);
    }
    for id in named {
        if !wanted.iter().any(|want| want.id == id) {
            wanted.push(Wanted {
                id,
                named_only: true,
                ..Wanted::default()
            });
        }
    }
    wanted
}

/// How the files of a move fared.
#[derive(Debug, Default, PartialEq, Eq)]
struct Followed {
    arrived: usize,
    /// Named, and not on the Host the agent comes from: there was nothing to bring.
    absent: usize,
    /// Not read there, or not taken here.
    failed: usize,
    /// Why the first of those was not.
    why: String,
}

impl Followed {
    /// What the administrator is told of them: the words for the sentence that says what
    /// came, and the lines that say what did not.
    fn told(&self, from: &str, session: &str) -> (String, String) {
        let came = match self.arrived {
            0 => "no files".to_string(),
            1 => "the one file it published or shows".to_string(),
            many => format!("the {many} files it published or shows"),
        };
        let mut more = String::new();
        match self.absent {
            0 => {}
            1 => more.push_str(&format!(
                "One file that it names is not on the Host at {from} either: there was nothing to bring.\n"
            )),
            many => more.push_str(&format!(
                "{many} files that it names are not on the Host at {from} either: there was nothing to bring.\n"
            )),
        }
        let again = format!("`clarp-host import --files --from {from} --session {session}`");
        match self.failed {
            0 => {}
            1 => more.push_str(&format!(
                "One file did not arrive ({}): {again} tries it again.\n",
                self.why
            )),
            many => more.push_str(&format!(
                "{many} files did not arrive (the first: {}): {again} tries them again.\n",
                self.why
            )),
        }
        (came, more)
    }
}

/// Reads one file from the Host the agent is on and gives it to this one under the id it
/// has. `Ok(false)` when that Host does not have it.
fn bring(
    from: &str,
    source_token: &str,
    host: &str,
    token: &str,
    session: &str,
    want: &Wanted,
) -> Result<bool, String> {
    let path = format!("/media/{}", ids::in_a_link(&want.id));
    let (status, declared, bytes) = exchange("GET", from, &path, source_token, &[], &[])?;
    match status {
        200 => {}
        // Named, and no longer there or never a file of that Host.
        404 => return Ok(false),
        other => return Err(format!("the Host at {from} answered {other} for it")),
    }
    // Who published it is said as it is, when it is a word a header can carry.
    let fits = |text: &str| {
        text.bytes()
            .all(|byte| byte == b' ' || byte.is_ascii_graphic())
    };
    let created_by = Some(want.created_by.as_str()).filter(|by| fits(by));
    let mut said = vec![
        ("X-Session", ids::in_a_link(session)),
        ("X-Asset-Id", want.id.clone()),
        ("X-File-Name", ids::in_a_link(&want.name)),
        ("X-Caption", ids::in_a_link(&want.caption)),
        ("X-Created-By", created_by.unwrap_or("").to_string()),
        (
            "Content-Type",
            match want.mime.as_str() {
                "" => declared,
                kind => kind.to_string(),
            },
        ),
    ];
    if let Some(at) = want.created_at {
        said.push(("X-Created-At", at.to_string()));
    }
    if want.named_only {
        said.push(("X-Named-Only", "1".to_string()));
    }
    match exchange("POST", host, "/media", token, &said, &bytes)? {
        (200, _, _) => Ok(true),
        (status, _, answer) => {
            let answer: Value = serde_json::from_slice(&answer).unwrap_or(Value::Null);
            let why = answer["error"].as_str().unwrap_or("no reason given");
            Err(format!("this Host answered {status}: {why}"))
        }
    }
}

/// The files of an agent come with it: each is read from the Host it is on and given to
/// this one under the id it has, which is what names it. One that does not arrive does not
/// keep the others from arriving.
fn files(
    from: &str,
    source_token: &str,
    host: &str,
    token: &str,
    session: &str,
    wanted: &[Wanted],
) -> Followed {
    let mut followed = Followed::default();
    for want in wanted {
        match bring(from, source_token, host, token, session, want) {
            Ok(true) => followed.arrived += 1,
            Ok(false) => followed.absent += 1,
            Err(why) => {
                followed.failed += 1;
                if followed.why.is_empty() {
                    followed.why = why;
                }
            }
        }
    }
    followed
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

/// `clarp-host import`: moves an agent from another Host to this one, with its conversation,
/// its plans and goals, its library and its files.
///
/// It reads what any app reads from the other Host, and all of it before it gives this Host
/// anything: the agent's row in the snapshot, the conversation page by page, its plans, its
/// library, its pictures. The other Host is not changed. Returns what to show.
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
    // What it is committed to: its plans as the other Host shows them, each goal with the
    // newest of its documents.
    let listed = get(
        &from,
        &format!("/task-plans?session={}", ids::in_a_link(&session)),
        source_token,
    )?;
    let mut plans: Vec<Value> = Vec::new();
    for plan in listed["plans"].as_array().cloned().unwrap_or_default() {
        let plan_id = plan["plan_id"].as_str().unwrap_or("");
        let named = plan["goal"]["documents"].as_array().cloned();
        let mut documents: Vec<Value> = Vec::new();
        for name in named
            .iter()
            .flatten()
            .filter_map(|row| row["name"].as_str())
        {
            let path = format!(
                "/task-plan/document?plan_id={}&name={}",
                ids::in_a_link(plan_id),
                ids::in_a_link(name)
            );
            documents.push(get(&from, &path, source_token)?["document"].clone());
        }
        plans.push(json!({ "plan": plan, "documents": documents }));
    }
    // What it made and asked: its library, a page of it at a time, and for a report what
    // it was, version by version.
    let mut library: Vec<Value> = Vec::new();
    loop {
        let path = format!(
            "/artifacts?session={}&order=created&limit=500&offset={}",
            ids::in_a_link(&session),
            library.len()
        );
        let page = get(&from, &path, source_token)?;
        let rows = page["artifacts"].as_array().cloned().unwrap_or_default();
        let count = rows.len();
        for artifact in rows {
            let mut revisions: Vec<Value> = Vec::new();
            if artifact["type"] == "html_form" && artifact["read_only"] == true {
                let id = ids::in_a_link(artifact["artifact_id"].as_str().unwrap_or(""));
                let was = get(
                    &from,
                    &format!("/artifacts/{id}/revisions?limit=100"),
                    source_token,
                )?;
                let versions = was["revisions"].as_array().cloned().unwrap_or_default();
                for version in versions
                    .iter()
                    .filter_map(|entry| entry["version"].as_str())
                {
                    let path = format!(
                        "/artifacts/{id}/revisions?version={}",
                        ids::in_a_link(version)
                    );
                    revisions.push(get(&from, &path, source_token)?["revision"].clone());
                }
            }
            library.push(json!({ "artifact": artifact, "revisions": revisions }));
        }
        if count < 500 {
            break;
        }
    }
    // The files: its pictures as the other Host lists them, and what its library, its plans
    // and its conversation name.
    let gallery = get(
        &from,
        &format!("/media?session={}&limit=500", ids::in_a_link(&session)),
        source_token,
    )?;
    let gallery = gallery["assets"].as_array().cloned().unwrap_or_default();
    let wants = wanted(&gallery, &library, &plans, &turns);
    if only_files {
        let followed = files(&from, source_token, &host, token, &session, &wants);
        let (came, more) = followed.told(&from, &session);
        return Ok(format!("{session} has {came} here.\n{more}")
            .trim_end()
            .to_string());
    }
    let made = match library.len() {
        0 => String::new(),
        1 => ", the one thing in its library".to_string(),
        many => format!(", the {many} things in its library"),
    };
    let committed = match plans.len() {
        0 => made,
        1 => format!("{made}, its plan"),
        many => format!("{made}, its {many} plans"),
    };
    let asked = json!({
        "agent": agent, "from": format!("http://{from}"), "turns": turns, "plans": plans,
        "library": library,
    });
    let (status, answer) = post(&host, "/agents/import", token, &asked)?;
    if status != 200 {
        let why = answer["error"].as_str().unwrap_or("no reason given");
        return Err(format!("this Host refused ({status}): {why}"));
    }
    // The agent is here. Its files follow: one that does not arrive does not undo the move.
    let followed = files(&from, source_token, &host, token, &session, &wants);
    let (came, more) = followed.told(&from, &session);
    Ok(format!(
        "{session} is on this Host now, with the {count} rows of its conversation{committed} and {came}.\n\
         {more}\
         It is still on the Host at {from} too. Archive it there, so that only one of the two takes its turns.\n\
         Here it is held until then: it answers what you say to it, and what would start by itself \
         (a goal that is due) waits. Resume it here when it is archived there: the app's Resume, \
         or POST /turn-queue/resume."
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
    fn a_conversation_names_the_files_it_shows_and_a_path_of_the_machine_names_none() {
        let rows = [
            json!({ "text": "see ![a chart](clarp-media://asset/asset_AbC_123) and /media/asset_dEf-456." }),
            json!({ "text": "it is under /media/peter/disk and ~/dotfiles/media/notes, not published" }),
            json!({ "text": "again clarp-media://asset/asset_AbC_123, and /media/asset_" }),
            json!({ "kind": "tool" }),
        ];
        let named = |rows: &[Value]| -> Vec<String> {
            let wanted = wanted(&[], &[], &[], rows);
            assert!(wanted.iter().all(|want| want.named_only));
            wanted.into_iter().map(|want| want.id).collect()
        };
        assert_eq!(named(&rows), ["asset_AbC_123", "asset_dEf-456"]);
    }

    #[test]
    fn a_move_wants_the_agents_own_files_as_its_own_and_what_it_names_besides() {
        // Its pictures, the newest first as a Host lists them.
        let gallery = [
            json!({ "asset_id": "asset_new", "source_name": "new.png", "caption": "New",
                    "created_by": "agent", "mime_type": "image/png", "created_at": 30 }),
            json!({ "asset_id": "asset_old", "source_name": "old.png", "caption": "",
                    "created_by": "Peter", "mime_type": "image/png", "created_at": 10 }),
        ];
        // A film made of its own file, with a still that is one of its pictures; a report
        // whose page shows another agent's picture; notes that name nothing.
        let library = [
            json!({ "artifact": { "type": "video", "created_at": 20, "payload": {
                "url": "/media/asset_film", "file_name": "film.mp4", "mime_type": "video/mp4",
                "thumbnail_url": "/media/asset_old" } }, "revisions": [] }),
            json!({ "artifact": { "type": "html_form", "payload": {
                "content": "<img src=\"/media/asset_theirs\">" } },
                "revisions": [{ "payload": { "content": "<img src='/media/asset_before'>" } }] }),
            json!({ "artifact": { "type": "document", "payload": { "url": "/media/not-a-file" } } }),
        ];
        let plans = [json!({ "plan": { "title": "see clarp-media://asset/asset_planned" } })];
        let rows = [json!({ "text": "![x](clarp-media://asset/asset_new) and /media/asset_said" })];
        let wanted = wanted(&gallery, &library, &plans, &rows);
        let own: Vec<(&str, &str, &str, Option<i64>)> = wanted
            .iter()
            .filter(|want| !want.named_only)
            .map(|want| {
                (
                    want.id.as_str(),
                    want.name.as_str(),
                    want.mime.as_str(),
                    want.created_at,
                )
            })
            .collect();
        assert_eq!(
            own,
            [
                ("asset_old", "old.png", "image/png", Some(10)),
                ("asset_new", "new.png", "image/png", Some(30)),
                ("asset_film", "film.mp4", "video/mp4", Some(20)),
            ]
        );
        assert_eq!(
            (wanted[0].created_by.as_str(), wanted[1].caption.as_str()),
            ("Peter", "New")
        );
        let named: Vec<&str> = wanted
            .iter()
            .filter(|want| want.named_only)
            .map(|want| want.id.as_str())
            .collect();
        assert_eq!(
            named,
            [
                "asset_theirs",
                "asset_before",
                "asset_planned",
                "asset_said"
            ]
        );
    }

    #[test]
    fn a_move_says_what_came_what_was_not_there_and_what_did_not_arrive() {
        let all = Followed {
            arrived: 9,
            ..Followed::default()
        };
        assert_eq!(
            all.told("old:1", "koko"),
            (
                "the 9 files it published or shows".to_string(),
                String::new()
            )
        );
        let none = Followed::default();
        assert_eq!(none.told("old:1", "koko").0, "no files");
        let some = Followed {
            arrived: 1,
            absent: 155,
            failed: 2,
            why: "this Host answered 400: bad size".to_string(),
        };
        let (came, more) = some.told("old:1", "koko");
        assert_eq!(came, "the one file it published or shows");
        assert_eq!(
            more,
            "155 files that it names are not on the Host at old:1 either: there was nothing to bring.\n\
             2 files did not arrive (the first: this Host answered 400: bad size): \
             `clarp-host import --files --from old:1 --session koko` tries them again.\n"
        );
        let one = Followed {
            arrived: 0,
            absent: 1,
            failed: 1,
            why: "why".to_string(),
        };
        let (_, more) = one.told("old:1", "koko");
        assert!(more.starts_with("One file that it names is not on the Host at old:1 either"));
        assert!(more.contains("One file did not arrive (why): `clarp-host import --files"));
        assert!(more.ends_with("tries it again.\n"));
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
