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
    "usage: clarp-host pair --url URL | --relay [--name DEVICE] [--scope full|limited] \
[--ttl SECONDS] [--host ADDR:PORT]
URL is the address the device will reach this Host at; with --relay it is the relay's address
for this Host. The administrator's token is read from CLARP_HOST_TOKEN.";

pub const NETWORK_USAGE: &str = "usage: clarp-host network relay status [--host ADDR:PORT]
Says whether this Host is reached through the connection relay, how the connection stands, and
the address a phone is paired with through it. The administrator's token is read from
CLARP_HOST_TOKEN.";

pub const PEER_USAGE: &str =
    "usage: clarp-host peer add NAME URL | peer list | peer remove NAME  [--host ADDR:PORT]
Names another Host this one may write to: an agent here then writes to an agent there with
`clarp-admin prompt --server NAME`. The other Host's token is read from CLARP_PEER_TOKEN, this
Host's from CLARP_HOST_TOKEN.";

pub const IMPORT_USAGE: &str =
    "usage: clarp-host import --from ADDR:PORT --session NAME [--host ADDR:PORT] [--files]
                         [--files-from ADDR:PORT]
Reads the agent NAME, its conversation, its library, its plans and goals and the files it
published or shows from the Host at --from and gives them to this Host. With --files only
the files are given, for an agent that is here already: a move whose files did not all
arrive is finished that way. A file the Host at --from does not have is asked for at
--files-from: a conversation that came from another Host names files that stayed there.
The other Host's token is read from CLARP_SOURCE_TOKEN, this Host's from CLARP_HOST_TOKEN,
and the token of the Host at --files-from from CLARP_FILES_TOKEN.";

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

/// Where a move reads from and where it writes to.
struct Way<'a> {
    from: &'a str,
    source_token: &'a str,
    /// Another Host that is asked for a file the first does not have, and its token.
    files_from: Option<(&'a str, &'a str)>,
    host: &'a str,
    token: &'a str,
    session: &'a str,
}

/// How the files of a move fared.
#[derive(Debug, Default, PartialEq, Eq)]
struct Followed {
    arrived: usize,
    /// Of those that arrived, how many the other Host had and the first did not.
    elsewhere: usize,
    /// Named, and not on a Host that was asked: there was nothing to bring.
    absent: usize,
    /// Not read there, or not taken here.
    failed: usize,
    /// Why the first of those was not.
    why: String,
}

impl Followed {
    /// What the administrator is told of them: the words for the sentence that says what
    /// came, and the lines that say what did not.
    fn told(&self, way: &Way<'_>) -> (String, String) {
        let (from, session) = (way.from, way.session);
        let mut came = match self.arrived {
            0 => "no files".to_string(),
            1 => "the one file it published or shows".to_string(),
            many => format!("the {many} files it published or shows"),
        };
        if let (Some((other, _)), true) = (way.files_from, self.elsewhere > 0) {
            let count = match (self.elsewhere, self.arrived) {
                (1, 1) => "from".to_string(),
                (1, _) => "one of them from".to_string(),
                (all, arrived) if all == arrived => "all from".to_string(),
                (some, _) => format!("{some} of them from"),
            };
            came.push_str(&format!(" ({count} the Host at {other})"));
        }
        let mut more = String::new();
        let again = format!("`clarp-host import --files --from {from} --session {session}");
        let (one, many) = (self.absent == 1, self.absent);
        match (self.absent, way.files_from) {
            (0, _) => {}
            (_, Some((other, _))) => more.push_str(&format!(
                "{} on neither Host ({from}, {other}): there was nothing to bring.\n",
                if one {
                    "One file that it names is".to_string()
                } else {
                    format!("{many} files that it names are")
                }
            )),
            // A conversation that came from another Host names files that stayed there.
            (_, None) => more.push_str(&format!(
                "{} not on the Host at {from} either: there was nothing to bring. If another \
                 Host has {}, {again} --files-from ADDR:PORT` brings {} from there.\n",
                if one {
                    "One file that it names is".to_string()
                } else {
                    format!("{many} files that it names are")
                },
                if one { "it" } else { "them" },
                if one { "it" } else { "them" },
            )),
        }
        match self.failed {
            0 => {}
            1 => more.push_str(&format!(
                "One file did not arrive ({}): {again}` tries it again.\n",
                self.why
            )),
            many => more.push_str(&format!(
                "{many} files did not arrive (the first: {}): {again}` tries them again.\n",
                self.why
            )),
        }
        (came, more)
    }
}

/// Where a file a move wanted was found.
enum Brought {
    /// On the Host the agent is on.
    There,
    /// On the other Host that was asked.
    Elsewhere,
    Nowhere,
}

/// Reads one file from the Host the agent is on, or from the other Host that may be asked,
/// and gives it to this one under the id it has.
fn bring(way: &Way<'_>, want: &Wanted) -> Result<Brought, String> {
    let path = format!("/media/{}", ids::in_a_link(&want.id));
    let asked = |address: &str, token: &str| -> Result<Option<(String, Vec<u8>)>, String> {
        match exchange("GET", address, &path, token, &[], &[])? {
            (200, declared, bytes) => Ok(Some((declared, bytes))),
            // Named, and no longer there or never a file of that Host.
            (404, _, _) => Ok(None),
            (other, _, _) => Err(format!("the Host at {address} answered {other} for it")),
        }
    };
    let (found, declared, bytes) = match asked(way.from, way.source_token)? {
        Some((declared, bytes)) => (Brought::There, declared, bytes),
        None => match way.files_from {
            None => return Ok(Brought::Nowhere),
            Some((other, token)) => match asked(other, token)? {
                Some((declared, bytes)) => (Brought::Elsewhere, declared, bytes),
                None => return Ok(Brought::Nowhere),
            },
        },
    };
    // Who published it is said as it is, when it is a word a header can carry.
    let fits = |text: &str| {
        text.bytes()
            .all(|byte| byte == b' ' || byte.is_ascii_graphic())
    };
    let created_by = Some(want.created_by.as_str()).filter(|by| fits(by));
    let mut said = vec![
        ("X-Session", ids::in_a_link(way.session)),
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
    match exchange("POST", way.host, "/media", way.token, &said, &bytes)? {
        (200, _, _) => Ok(found),
        (status, _, answer) => {
            let answer: Value = serde_json::from_slice(&answer).unwrap_or(Value::Null);
            let why = answer["error"].as_str().unwrap_or("no reason given");
            Err(format!("this Host answered {status}: {why}"))
        }
    }
}

/// The files of an agent come with it: each is read from the Host it is on, or from the
/// other Host that may be asked, and given to this one under the id it has, which is what
/// names it. One that does not arrive does not keep the others from arriving.
fn files(way: &Way<'_>, wanted: &[Wanted]) -> Followed {
    let mut followed = Followed::default();
    for want in wanted {
        match bring(way, want) {
            Ok(Brought::There) => followed.arrived += 1,
            Ok(Brought::Elsewhere) => {
                followed.arrived += 1;
                followed.elsewhere += 1;
            }
            Ok(Brought::Nowhere) => followed.absent += 1,
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
/// How an agent is heard on the Host it comes from, when it is heard its own way there: the
/// language it is spoken to in and the engines that hear it, in the order they are tried.
/// That Host keeps both under the agent's name, its persona or its session.
fn heard_as(from: &str, source_token: &str, agent: &Value) -> (Option<String>, Option<String>) {
    let Ok(settings) = get(from, "/transcription-providers", source_token) else {
        return (None, None);
    };
    let own = |entries: &Value| -> Option<String> {
        ["name", "persona", "session"].iter().find_map(|key| {
            let named = agent[*key].as_str()?.trim().to_lowercase();
            let entry = entries[named.as_str()].as_str()?.trim();
            (!entry.is_empty()).then(|| entry.to_string())
        })
    };
    (
        own(&settings["agent_languages"]),
        own(&settings["agent_engines"]),
    )
}

/// Sets how the agent is heard on this Host as it was heard where it comes from, and says
/// so. What cannot be set here does not undo the move: it is said, with why.
fn bring_hearing(way: &Way<'_>, agent: &Value) -> String {
    let (language, engines) = heard_as(way.from, way.source_token, agent);
    if language.is_none() && engines.is_none() {
        return String::new();
    }
    // Here it is known by its session, which is its own.
    let its_own = |text: &str| -> Value {
        let mut entry = serde_json::Map::new();
        entry.insert(way.session.to_string(), Value::String(text.to_string()));
        Value::Object(entry)
    };
    let mut change = serde_json::Map::new();
    let mut said = Vec::new();
    if let Some(language) = &language {
        change.insert("agent_languages".to_string(), its_own(language));
        said.push(format!("in {language}"));
    }
    if let Some(engines) = &engines {
        change.insert("agent_engines".to_string(), its_own(engines));
        said.push(format!("by {}", engines.replace(',', ", then ")));
    }
    let said = said.join(", ");
    match post(
        way.host,
        "/transcription-providers",
        way.token,
        &Value::Object(change),
    ) {
        Ok((200, _)) => format!("It is heard here as it was there: {said}.\n"),
        Ok((status, answer)) => {
            let why = answer["error"].as_str().unwrap_or("no reason given");
            format!(
                "There it was heard {said}. That could not be set here ({status}): {why}. \
                 Until it is, it is heard like every agent of this Host.\n"
            )
        }
        Err(why) => format!(
            "There it was heard {said}. That could not be set here: {why}. Until it is, it is \
             heard like every agent of this Host.\n"
        ),
    }
}

pub fn import(
    mut args: impl Iterator<Item = String>,
    source_token: &str,
    token: &str,
    files_token: &str,
) -> Result<String, String> {
    let (mut from, mut session, mut only_files) = (None, None, false);
    let mut files_from: Option<String> = None;
    let mut host = "127.0.0.1:7690".to_string();
    while let Some(arg) = args.next() {
        let mut value = |what: &str| args.next().ok_or(format!("{arg} needs {what}"));
        match arg.as_str() {
            "--from" => from = Some(value("an address")?),
            "--session" => session = Some(value("a session name")?),
            "--host" => host = value("an address")?,
            "--files" => only_files = true,
            "--files-from" => files_from = Some(value("an address")?),
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
    let way = Way {
        from: &from,
        source_token,
        files_from: files_from.as_deref().map(|other| (other, files_token)),
        host: &host,
        token,
        session: &session,
    };
    if only_files {
        let followed = files(&way, &wants);
        let (came, more) = followed.told(&way);
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
    // Who it is there comes with it. How it speaks: as the persona of its name speaks there
    // (an agent's row does not say). And the picture it has of its own.
    let mut agent = agent;
    let called = agent["persona"].as_str().unwrap_or("").to_string();
    let personas = snapshot["personas"].as_array().into_iter().flatten();
    let way_of_speaking = personas
        .filter(|persona| {
            let name = persona["name"].as_str();
            name.is_some_and(|name| name.eq_ignore_ascii_case(&called))
        })
        .find_map(|persona| persona["personality"].as_str())
        .unwrap_or("");
    if !way_of_speaking.is_empty() {
        agent["personality"] = json!(way_of_speaking);
    }
    let own_face = agent["avatar_url"].as_str().unwrap_or("");
    let picture = if own_face.starts_with("/avatars/") {
        match exchange("GET", &from, own_face, source_token, &[], &[])? {
            (200, _, bytes) => Some(clarp_kernel::python::b64encode(&bytes)),
            (status, _, _) => {
                return Err(format!(
                    "the Host at {from} answered {status} for the picture {session} has of its own"
                ));
            }
        }
    } else {
        None
    };
    // The portraits it keeps, each with its picture: the one it wears among them, so that
    // it is the same portrait here, chosen from the same file of its gallery.
    let kept = get(
        &from,
        &format!("/agent-portraits?session={}", ids::in_a_link(&session)),
        source_token,
    )?;
    let mut portraits: Vec<Value> = Vec::new();
    for portrait in kept["portraits"].as_array().into_iter().flatten() {
        let address = portrait["url"].as_str().unwrap_or("");
        if address.is_empty() {
            continue;
        }
        match exchange("GET", &from, address, source_token, &[], &[])? {
            (200, _, bytes) => portraits.push(json!({
                "portrait_id": portrait["portrait_id"], "media_asset_id": portrait["media_asset_id"],
                "created_by": portrait["created_by"], "created_at": portrait["created_at"],
                "picture": clarp_kernel::python::b64encode(&bytes),
            })),
            (status, _, _) => {
                return Err(format!(
                    "the Host at {from} answered {status} for a portrait {session} keeps"
                ));
            }
        }
    }
    let asked = json!({
        "agent": agent, "from": format!("http://{from}"), "turns": turns, "plans": plans,
        "library": library, "picture": picture, "portraits": portraits,
    });
    let (status, answer) = post(&host, "/agents/import", token, &asked)?;
    if status != 200 {
        let why = answer["error"].as_str().unwrap_or("no reason given");
        return Err(format!("this Host refused ({status}): {why}"));
    }
    // The agent is here. Its files follow: one that does not arrive does not undo the move.
    let followed = files(&way, &wants);
    let (came, more) = followed.told(&way);
    // And how it is heard, when it is heard its own way.
    let heard = bring_hearing(&way, &agent);
    Ok(format!(
        "{session} is on this Host now, with the {count} rows of its conversation{committed} and {came}.\n\
         {more}{heard}\
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
        .filter(|(key, _)| !key.ends_with("_TOKEN"))
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
    let mut relay = false;
    while let Some(arg) = args.next() {
        let mut value = |what: &str| args.next().ok_or(format!("{arg} needs {what}"));
        match arg.as_str() {
            "--url" => url = Some(value("an address")?),
            "--relay" => relay = true,
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
    let mut asked = json!({ "device_name": name, "scope": scope, "ttl_seconds": ttl });
    if relay {
        // The Host knows its relay's address for it: nothing is read here.
        asked["relay"] = json!(true);
    } else {
        let none = "pairing needs --url: the address the device will reach this Host at";
        asked["url"] = json!(url.ok_or(none)?);
    }
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

/// `clarp-host network relay status`: how the running Host stands with its relay, as the
/// Python Host's own command shows it.
pub fn network(mut args: impl Iterator<Item = String>, token: &str) -> Result<String, String> {
    let asked = (args.next(), args.next());
    if asked != (Some("relay".to_string()), Some("status".to_string())) {
        return Err("say what to show: relay status".to_string());
    }
    let mut host = "127.0.0.1:7690".to_string();
    while let Some(arg) = args.next() {
        match arg.as_str() {
            "--host" => host = args.next().ok_or("--host needs an address")?,
            other => return Err(format!("unknown argument {other}")),
        }
    }
    let mut status = get(&host, "/network/relay", token)?;
    if status.is_object() && status.get("configured").is_none() {
        status["configured"] = json!(false);
    }
    serde_json::to_string_pretty(&status).map_err(|error| error.to_string())
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
        let way = |files_from| Way {
            from: "old:1",
            source_token: "",
            files_from,
            host: "new:2",
            token: "",
            session: "koko",
        };
        let (one, two) = (way(None), way(Some(("other:3", ""))));
        let all = Followed {
            arrived: 9,
            ..Followed::default()
        };
        assert_eq!(
            all.told(&one),
            (
                "the 9 files it published or shows".to_string(),
                String::new()
            )
        );
        assert_eq!(Followed::default().told(&one).0, "no files");
        let some = Followed {
            arrived: 1,
            absent: 155,
            failed: 2,
            why: "this Host answered 400: bad size".to_string(),
            ..Followed::default()
        };
        let (came, more) = some.told(&one);
        assert_eq!(came, "the one file it published or shows");
        assert_eq!(
            more,
            "155 files that it names are not on the Host at old:1 either: there was nothing to \
             bring. If another Host has them, `clarp-host import --files --from old:1 --session \
             koko --files-from ADDR:PORT` brings them from there.\n\
             2 files did not arrive (the first: this Host answered 400: bad size): \
             `clarp-host import --files --from old:1 --session koko` tries them again.\n"
        );
        let single = Followed {
            absent: 1,
            failed: 1,
            why: "why".to_string(),
            ..Followed::default()
        };
        let (_, more) = single.told(&one);
        assert!(more.starts_with(
            "One file that it names is not on the Host at old:1 either: there was nothing to \
             bring. If another Host has it, `clarp-host import"
        ));
        assert!(more.contains("--files-from ADDR:PORT` brings it from there.\n"));
        assert!(more.contains("One file did not arrive (why): `clarp-host import --files"));
        assert!(more.ends_with("tries it again.\n"));
        // With another Host to ask: what it had is said, and what neither has.
        let found = Followed {
            arrived: 160,
            elsewhere: 72,
            absent: 3,
            ..Followed::default()
        };
        assert_eq!(
            found.told(&two),
            (
                "the 160 files it published or shows (72 of them from the Host at other:3)"
                    .to_string(),
                "3 files that it names are on neither Host (old:1, other:3): there was nothing \
                 to bring.\n"
                    .to_string()
            )
        );
        let lone = Followed {
            arrived: 1,
            elsewhere: 1,
            absent: 1,
            ..Followed::default()
        };
        assert_eq!(
            lone.told(&two),
            (
                "the one file it published or shows (from the Host at other:3)".to_string(),
                "One file that it names is on neither Host (old:1, other:3): there was nothing \
                 to bring.\n"
                    .to_string()
            )
        );
        let every = Followed {
            arrived: 4,
            elsewhere: 4,
            ..Followed::default()
        };
        assert!(
            every
                .told(&two)
                .0
                .ends_with("(all from the Host at other:3)")
        );
        let first = Followed {
            arrived: 4,
            elsewhere: 1,
            ..Followed::default()
        };
        assert!(
            first
                .told(&two)
                .0
                .ends_with("(one of them from the Host at other:3)")
        );
        // Asked and not needed: nothing is said of the other Host.
        assert_eq!(all.told(&two).0, "the 9 files it published or shows");
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
    fn the_relays_state_is_asked_of_a_host_that_answers() {
        assert!(network(words(""), "").unwrap_err().contains("relay status"));
        assert!(
            network(words("relay"), "")
                .unwrap_err()
                .contains("relay status")
        );
        assert!(
            network(words("relay status --what"), "")
                .unwrap_err()
                .contains("unknown argument")
        );
        // Nothing listens on port 1.
        let nobody = network(words("relay status --host 127.0.0.1:1"), "");
        assert!(nobody.unwrap_err().contains("no Host answers"));
    }

    #[test]
    fn a_move_needs_to_know_where_from_and_whom() {
        assert!(
            import(words("--session maximus"), "", "", "")
                .unwrap_err()
                .contains("--from")
        );
        assert!(
            import(words("--from 127.0.0.1:1"), "", "", "")
                .unwrap_err()
                .contains("--session")
        );
        let nobody = import(
            words("--from http://127.0.0.1:1/ --session maximus"),
            "",
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
