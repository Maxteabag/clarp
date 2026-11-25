//! What the administrator does from the command line, by asking the Host that is running.
//!
//! Commands normally ask the Host over HTTP with the administrator's token. Paired-device
//! migration is performed against the destination fleet store before the Host starts.

use std::ffi::OsString;
use std::io::{Read, Write};
use std::net::{TcpStream, ToSocketAddrs};
use std::path::PathBuf;
use std::time::Duration;

use serde_json::{Value, json};

use crate::ids;

mod import_devices;
mod import_jobs;
mod import_settings;
mod offline_import;

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
    "usage: clarp-host peers add [NAME] URL --code CODE [--device-name NAME]
       clarp-host peers add [NAME] URL --token-stdin
       clarp-host peers list | peers remove NAME
Use --host ADDR:PORT to name this Host on loopback. It takes the administrator's token from
CLARP_HOST_TOKEN. A named peer can receive clarp-admin prompt --server NAME.";

pub const IMPORT_USAGE: &str =
    "usage: clarp-host import --from ADDR:PORT --session NAME [--host ADDR:PORT] [--files]
                         [--files-from ADDR:PORT]
       clarp-host import --back --from ADDR:PORT --session NAME [--host ADDR:PORT]
       clarp-host import --devices --from-db PATH --data DIR
       clarp-host import --settings --from-db PATH --data DIR [--dry-run]
       clarp-host import --jobs --from-db PATH --data DIR [--dry-run]
Stop the Python Host before reading its database. Run a writing import before starting the
Rust Host, and for --jobs stop the Rust Host first. --dry-run prints the same plan without
updating the destination. PATH is the Python state.sqlite and DIR is the Rust Host's --data
directory with fleet.db. --jobs carries each running background job of an agent that has moved
here, as it stands, so that its worker, which keeps running, goes on reporting to this Host; a
job whose worker is gone, or that this Host already has, is left.
Reads the agent NAME, its conversation, its library, its plans and goals and the files it
published or shows from the Host at --from and gives them to this Host. With --files only
the files are given, for an agent that is here already: a move whose files did not all
arrive is finished that way. A file that answers 404 at --from is asked for at
--files-from: a conversation that came from another Host names files that stayed there.
With --back, hold the imported agent here and take back the source Host's move mark.
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
    marked_request(method, address, path, token, false, body)
}

/// [`request`], saying that the administrator's token is the owner's on purpose where it is
/// ([`crate::OWNER_MODE_HEADER`]).
fn marked_request(
    method: &str,
    address: &str,
    path: &str,
    token: &str,
    owner_mode: bool,
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
    let authorization = if owner_mode && !token.is_empty() {
        format!("{authorization}{}: 1\r\n", crate::OWNER_MODE_HEADER)
    } else {
        authorization
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
    /// Deliberately removed on the source Host or on the fallback Host.
    removed: usize,
    removed_elsewhere: usize,
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
                 Host has {}, {again} --files-from ADDR:PORT` can bring {} before the move finishes.\n",
                if one {
                    "One file that it names is".to_string()
                } else {
                    format!("{many} files that it names are")
                },
                if one { "it" } else { "them" },
                if one { "it" } else { "them" },
            )),
        }
        for (count, at) in [
            (self.removed, Some(from)),
            (
                self.removed_elsewhere,
                way.files_from.map(|(other, _)| other),
            ),
        ] {
            let Some(at) = at else { continue };
            match count {
                0 => {}
                1 => more.push_str(&format!("One file was removed on the Host at {at}.\n")),
                many => more.push_str(&format!("{many} files were removed on the Host at {at}.\n")),
            }
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
    Removed,
    RemovedElsewhere,
}

/// Reads one file from the Host the agent is on, or from the other Host that may be asked,
/// and gives it to this one under the id it has.
fn bring(way: &Way<'_>, want: &Wanted) -> Result<Brought, String> {
    enum FileReply {
        Available(String, Vec<u8>),
        Missing,
        Removed,
    }

    let path = format!("/media/{}", ids::in_a_link(&want.id));
    let asked = |address: &str, token: &str| -> Result<FileReply, String> {
        match exchange("GET", address, &path, token, &[], &[])? {
            (200, declared, bytes) => Ok(FileReply::Available(declared, bytes)),
            (404, _, _) => Ok(FileReply::Missing),
            (410, _, _) => Ok(FileReply::Removed),
            (other, _, _) => Err(format!("the Host at {address} answered {other} for it")),
        }
    };
    let (found, declared, bytes) = match asked(way.from, way.source_token)? {
        FileReply::Available(declared, bytes) => (Brought::There, declared, bytes),
        // A tombstone records a deliberate removal. Do not bring back another copy.
        FileReply::Removed => return Ok(Brought::Removed),
        FileReply::Missing => match way.files_from {
            None => return Ok(Brought::Nowhere),
            Some((other, token)) => match asked(other, token)? {
                FileReply::Available(declared, bytes) => (Brought::Elsewhere, declared, bytes),
                FileReply::Missing => return Ok(Brought::Nowhere),
                FileReply::Removed => return Ok(Brought::RemovedElsewhere),
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
            Ok(Brought::Removed) => followed.removed += 1,
            Ok(Brought::RemovedElsewhere) => followed.removed_elsewhere += 1,
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
/// library, its pictures.
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

/// The snapshot names Janitors explicitly. Older snapshots may omit the flag.
pub(crate) fn movable(agent: &Value) -> Result<(), &'static str> {
    if agent["is_janitor"] == true {
        return Err(
            "A Janitor cannot be moved. Use this Host's own built-in Janitors, or create a custom Janitor here from its definition.",
        );
    }
    Ok(())
}

/// The source's ordinary read is insufficient to recreate this kind. Keep that
/// decision here so a source with a migration export can supply it instead.
fn left_on_source(artifact: &Value) -> Option<String> {
    if artifact["type"] != "render_spec" {
        return None;
    }
    let id = artifact["artifact_id"].as_str().unwrap_or("");
    let title = artifact["title"].as_str().unwrap_or("");
    Some(format!("  {id:?} — {title:?}"))
}

fn skipped_report(left: &[String]) -> String {
    if left.is_empty() {
        return String::new();
    }
    let count = left.len();
    let kind = if count == 1 {
        "render spec"
    } else {
        "render specs"
    };
    format!(
        "Left on the source Host: {count} {kind}.\n{}\n",
        left.join("\n")
    )
}

fn snapshot_agent<'a>(snapshot: &'a Value, session: &str, host: &str) -> Result<&'a Value, String> {
    snapshot["agents"]
        .as_array()
        .and_then(|agents| agents.iter().find(|agent| agent["session"] == session))
        .ok_or_else(|| format!("the Host at {host} has no agent called {session}"))
}

fn has_move_mark(info: &Value) -> bool {
    info["capabilities"]["features"]
        .as_array()
        .is_some_and(|features| features.iter().any(|feature| feature == "agent_moved"))
}

fn move_target(way: &Way<'_>) -> Result<Value, String> {
    let identity = get(way.host, "/pairing/identity", "")?;
    let need = |key: &str| {
        identity[key]
            .as_str()
            .map(str::trim)
            .filter(|text| !text.is_empty())
            .ok_or_else(|| format!("this Host's pairing identity is missing {key}"))
    };
    Ok(json!({"host": need("name")?, "id": need("server_id")?}))
}

fn move_request(way: &Way<'_>, target: &Value) -> Result<Value, String> {
    let body = json!({"session": way.session, "moved_to": target});
    let (status, answer) = post(way.from, "/agent-moved", way.source_token, &body)?;
    if status != 200 || answer["ok"] != true {
        let error = answer["error"].as_str().unwrap_or("no error received");
        return Err(format!(
            "the source answered {status} to POST /agent-moved: {error}"
        ));
    }
    let confirmed = if target.is_null() {
        answer.get("moved") == Some(&Value::Null)
    } else {
        answer["moved"]["to"] == *target
    };
    if answer["session"] != way.session || !confirmed {
        return Err("the source answered 200 without confirming the requested move state".into());
    }
    Ok(answer)
}

fn manual_handoff(way: &Way<'_>) -> String {
    format!(
        "It is still on the Host at {} too. Archive it there, so that only one of the two takes its turns.\n\
         Here it is held until then: all messages and automatic work wait. Resume it here when it is archived there: the app's Resume, \
         or POST /turn-queue/resume.",
        way.from
    )
}

fn source_idle(way: &Way<'_>, agent_id: &Value, target: Option<&Value>) -> Result<(), String> {
    let snapshot = get(way.from, "/agents/snapshot", way.source_token)?;
    let agent = snapshot_agent(&snapshot, way.session, way.from)?;
    if !agent_id.is_string() || agent["agent_id"] != *agent_id {
        return Err("the source agent's identity changed during the move".into());
    }
    if target.is_some_and(|target| &agent["moved"]["to"] != target) {
        return Err("the source no longer confirms this move mark".into());
    }
    if agent["busy"] != false {
        return Err("the source has not confirmed the agent is idle".into());
    }
    Ok(())
}

const HOLD_UNCONFIRMED: &str =
    "The move is incomplete: this Host did not confirm the hold. The source was not marked moved.";

fn finish_handoff(way: &Way<'_>, agent_id: &Value, held: bool, files: &Followed) -> String {
    let unfinished = |why: &str| {
        format!(
            "The move is incomplete: {why}. The copy here stays held. It is on both Hosts until \
             marked moved or archived on the Host at {}.",
            way.from
        )
    };
    if !held {
        return HOLD_UNCONFIRMED.into();
    }
    if files.failed != 0 {
        return unfinished(&format!(
            "{} file(s) could not be transferred ({})",
            files.failed, files.why
        ));
    }
    let info = match get(way.from, "/server-info", way.source_token) {
        Ok(info) => info,
        Err(why) => return unfinished(&why),
    };
    if !has_move_mark(&info) {
        return manual_handoff(way);
    }
    let target = match move_target(way) {
        Ok(target) => target,
        Err(why) => return unfinished(&why),
    };
    if info["server_id"] == target["id"] {
        return unfinished("the source and destination are the same Host");
    }
    if let Err(why) = source_idle(way, agent_id, None) {
        return unfinished(&why);
    }
    if let Err(why) = move_request(way, &target) {
        return unfinished(&why);
    }

    let marked = format!(
        "It is marked moved on the Host at {}. Its turn-start doors there now refuse it.",
        way.from
    );
    // A turn may have started since the first snapshot. The source's mark lets it finish.
    if let Err(why) = source_idle(way, agent_id, Some(&target)) {
        return format!("The source accepted the move mark. The copy here stays held: {why}.");
    }
    let resumed = match post(
        way.host,
        "/turn-queue/resume",
        way.token,
        &json!({"session": way.session}),
    ) {
        Ok((200, answer)) if answer["session"] == way.session && answer["paused"] == false => {
            return format!("{marked} It is resumed here.");
        }
        Ok((status, answer)) => {
            let why = answer["error"]
                .as_str()
                .unwrap_or("no resumed queue was confirmed");
            format!("this Host answered {status} to POST /turn-queue/resume: {why}")
        }
        Err(why) => why,
    };
    // The resume might have landed before the connection was lost. Read back before saying
    // what the destination is doing. The source is already fenced by its mark.
    let path = format!("/turn-queue?session={}", ids::in_a_link(way.session));
    match get(way.host, &path, way.token) {
        Ok(state) if state["paused"] == false => {
            format!("{marked} It is resumed here. The resume receipt was unclear: {resumed}.")
        }
        Ok(state) if state["paused"] == true => {
            format!("{marked} The copy here stays held: {resumed}.")
        }
        _ => format!("{marked} The queue state here could not be confirmed: {resumed}."),
    }
}

/// An explanation when a files-only retry must leave the two Hosts alone. Otherwise
/// the destination has confirmed a held copy of the same agent.
fn files_handoff_status(way: &Way<'_>, there: &Value) -> Result<Option<String>, String> {
    let local = get(way.host, "/agents/snapshot", way.token)?;
    let here = snapshot_agent(&local, way.session, way.host)?;
    if !there["agent_id"].is_string() || here["agent_id"] != there["agent_id"] {
        return Err("this Host has a different agent under that session".into());
    }
    if !there["moved"].is_null() {
        let target = move_target(way)?;
        if there["moved"]["to"]["id"] != target["id"] {
            return Err("the source is marked moved to a different Host".into());
        }
        // A hold after the source was marked may be a later Stop. Leave it in place.
        let reported = match here["queue_paused"].as_bool() {
            Some(false) => "The move is already finished.",
            Some(true) => {
                "The source is already marked moved to this Host. The copy here stays held."
            }
            None => {
                "The source is already marked moved to this Host. The queue state here could not be confirmed."
            }
        };
        return Ok(Some(reported.into()));
    }
    if here["queue_paused"] != true || !here["moved"].is_null() {
        return Ok(Some(HOLD_UNCONFIRMED.into()));
    }
    Ok(None)
}

fn take_move_back(way: &Way<'_>) -> Result<String, String> {
    let source = get(way.from, "/agents/snapshot", way.source_token)?;
    let there = snapshot_agent(&source, way.session, way.from)?;
    if !has_move_mark(&get(way.from, "/server-info", way.source_token)?) {
        return Err("the source Host does not advertise agent_moved".into());
    }
    let target = move_target(way)?;
    if there["moved"]["to"]["id"] != target["id"] {
        return Err("the source has no move mark for this Host".into());
    }
    let local = get(way.host, "/agents/snapshot", way.token)?;
    let here = snapshot_agent(&local, way.session, way.host)?;
    if !there["agent_id"].is_string() || here["agent_id"] != there["agent_id"] {
        return Err("this Host has a different agent under that session".into());
    }
    if here["busy"] != false {
        return Err(
            "this Host has not confirmed the imported agent is idle; the source stays marked moved"
                .into(),
        );
    }
    let hold = json!({"session": way.session, "reason": "taking the move back"});
    let (status, stopped) = post(way.host, "/stop", way.token, &hold).map_err(|why| {
        format!("could not hold this Host's copy: {why}; the source stays marked moved")
    })?;
    if status != 200 || stopped["ok"] != true || stopped["queue_paused"] != true {
        let why = stopped["error"]
            .as_str()
            .unwrap_or("no mailbox hold was confirmed");
        return Err(format!(
            "this Host answered {status} to POST /stop: {why}; the source stays marked moved"
        ));
    }
    let local = get(way.host, "/agents/snapshot", way.token)?;
    let here = snapshot_agent(&local, way.session, way.host)?;
    if here["agent_id"] != there["agent_id"]
        || here["busy"] != false
        || here["queue_paused"] != true
    {
        return Err("this Host no longer confirms an idle, held copy of the agent; the source stays marked moved".into());
    }
    move_request(way, &Value::Null).map_err(|why| {
        format!("this Host's copy stays held; the return could not be confirmed: {why}")
    })?;
    Ok(format!(
        "{} is held here. What it did here since the move stays here. The source Host at {} goes on from where it was.",
        way.session, way.from
    ))
}

pub fn import(
    args: impl Iterator<Item = String>,
    source_token: &str,
    token: &str,
    files_token: &str,
) -> Result<String, String> {
    let mut args = args.peekable();
    match args.peek().map(String::as_str) {
        Some("--devices") => {
            args.next();
            return import_devices::run(args);
        }
        Some("--settings") => {
            args.next();
            return import_settings::run(args);
        }
        Some("--jobs") => {
            args.next();
            return import_jobs::run(args);
        }
        _ => {}
    }
    let (mut from, mut session, mut only_files, mut back) = (None, None, false, false);
    let mut files_from: Option<String> = None;
    let mut host = "127.0.0.1:7690".to_string();
    while let Some(arg) = args.next() {
        let mut value = |what: &str| args.next().ok_or(format!("{arg} needs {what}"));
        match arg.as_str() {
            "--from" => from = Some(value("an address")?),
            "--session" => session = Some(value("a session name")?),
            "--host" => host = value("an address")?,
            "--files" => only_files = true,
            "--back" => back = true,
            "--files-from" => files_from = Some(value("an address")?),
            other => return Err(format!("unknown argument {other}")),
        }
    }
    let from = from.ok_or("the move needs --from: the address of the Host the agent is on")?;
    let session = session.ok_or("the move needs --session: the agent's session name")?;

    let way = Way {
        from: &from,
        source_token,
        files_from: files_from.as_deref().map(|other| (other, files_token)),
        host: &host,
        token,
        session: &session,
    };
    if back {
        if only_files || files_from.is_some() {
            return Err("--back cannot be combined with file transfer options".into());
        }
        return take_move_back(&way);
    }
    let snapshot = get(&from, "/agents/snapshot", source_token)?;
    let agent = snapshot_agent(&snapshot, &session, &from)?.clone();
    movable(&agent)?;
    if only_files && let Some(status) = files_handoff_status(&way, &agent)? {
        return Ok(status);
    }
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
        let mut moved = json!({"plan":plan,"documents":documents});
        if plan["goal"].is_object() {
            let mut after = 0;
            let mut events = Vec::new();
            loop {
                let path = format!(
                    "/goal-ledger?plan_id={}&after={after}&limit=500",
                    ids::in_a_link(plan_id)
                );
                let page = get(&from, &path, source_token)?;
                let rows = page["events"]
                    .as_array()
                    .ok_or("invalid goal ledger page")?;
                events.extend(rows.iter().cloned());
                if page["has_more"] != true {
                    break;
                }
                let next = page["next_after"]
                    .as_i64()
                    .filter(|n| *n > after)
                    .ok_or("goal ledger cursor did not advance")?;
                after = next;
            }
            if !events.is_empty() || plan["ledger"]["event_count"] == 0 {
                moved["events"] = Value::Array(events);
            }
        }
        plans.push(moved);
    }
    // What it made and asked: its library, a page of it at a time, and for a report what
    // it was, version by version.
    let mut library: Vec<Value> = Vec::new();
    let mut left = Vec::new();
    let mut offset = 0;
    loop {
        let path = format!(
            "/artifacts?session={}&order=created&limit=500&offset={offset}",
            ids::in_a_link(&session)
        );
        let page = get(&from, &path, source_token)?;
        let rows = page["artifacts"].as_array().cloned().unwrap_or_default();
        let count = rows.len();
        offset += count;
        for artifact in rows {
            if let Some(line) = left_on_source(&artifact) {
                left.push(line);
                continue;
            }
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
    let skipped = skipped_report(&left);
    let wants = wanted(&gallery, &library, &plans, &turns);
    if only_files {
        let followed = files(&way, &wants);
        let (came, more) = followed.told(&way);
        let handoff = if followed.failed == 0 {
            let source = get(&from, "/agents/snapshot", source_token)?;
            let there = snapshot_agent(&source, &session, &from)?;
            if there["agent_id"] != agent["agent_id"] {
                return Err("the source agent's identity changed during the move".into());
            }
            match files_handoff_status(&way, there)? {
                Some(status) => status,
                None => finish_handoff(&way, &agent["agent_id"], true, &followed),
            }
        } else {
            finish_handoff(&way, &agent["agent_id"], true, &followed)
        };
        return Ok(format!(
            "{session} has {came} here.\n{skipped}{more}{handoff}"
        ));
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
    let mut avatar_not_carried = None;
    let picture = if own_face.starts_with("/avatars/") {
        match exchange("GET", &from, own_face, source_token, &[], &[]) {
            Ok((200, _, bytes)) if bytes.len() <= clarp_kernel::personas::AVATAR_MOST => {
                Some(clarp_kernel::python::b64encode(&bytes))
            }
            Ok((200, _, _)) => {
                avatar_not_carried = Some("too large".to_owned());
                None
            }
            Ok((status, _, _)) => {
                avatar_not_carried = Some(format!("source returned HTTP {status}"));
                None
            }
            Err(_) => {
                avatar_not_carried = Some("source unavailable".to_owned());
                None
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
            (200, _, bytes) if bytes.len() <= clarp_kernel::personas::AVATAR_MOST => portraits.push(json!({
                "portrait_id": portrait["portrait_id"], "media_asset_id": portrait["media_asset_id"],
                "created_by": portrait["created_by"], "created_at": portrait["created_at"],
                "picture": clarp_kernel::python::b64encode(&bytes),
            })),
            (200, _, _) => {}
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
    let avatar_not_carried = answer["avatar_not_carried"]
        .as_str()
        .or(avatar_not_carried.as_deref())
        .map_or_else(String::new, |why| format!("avatar not carried: {why}\n"));
    let cwd_created = answer["cwd_created"]
        .as_str()
        .map_or_else(String::new, |cwd| {
            format!("created working directory: {cwd}\n")
        });
    // The agent is here. Its files follow: one that does not arrive does not undo the move.
    let followed = files(&way, &wants);
    let (came, more) = followed.told(&way);
    // And how it is heard, when it is heard its own way.
    let heard = bring_hearing(&way, &agent);
    let handoff = finish_handoff(&way, &agent["agent_id"], answer["held"] == true, &followed);
    Ok(format!(
        "{session} is on this Host now, with the {count} rows of its conversation{committed} and {came}.\n\
         {avatar_not_carried}{cwd_created}{skipped}{more}{heard}{handoff}"
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

/// Find the first configuration that names this Rust Host. A tool forwarded from the
/// Python Host may inherit settings that point to the Python installation.
/// The token in a lane's turn file: one line, a few hundred bytes at most. Anything else is no
/// token.
fn turn_from_file(path: &std::path::Path) -> Option<String> {
    use std::io::Read as _;
    let mut text = String::new();
    std::fs::File::open(path)
        .ok()?
        .take(4096)
        .read_to_string(&mut text)
        .ok()?;
    let token = text.trim();
    (!token.is_empty() && !token.contains(char::is_whitespace)).then(|| token.to_string())
}

fn installed_host(env: impl Fn(&'static str) -> Option<OsString>) -> Option<(String, String)> {
    let named = |variable| env(variable).filter(|value| !value.is_empty());
    let read = |path: PathBuf| host_in(&std::fs::read_to_string(path).ok()?);
    named("CLAUDE_PWA_CONFIG")
        .map(PathBuf::from)
        .and_then(read)
        .or_else(|| {
            named("CLARP_CONFIG_DIR")
                .map(|dir| PathBuf::from(dir).join("config.toml"))
                .and_then(read)
        })
        .or_else(|| {
            let config = named("XDG_CONFIG_HOME")
                .map(PathBuf::from)
                .or_else(|| named("HOME").map(|home| PathBuf::from(home).join(".config")))?;
            read(config.join("clarp-host").join("config.toml"))
        })
}

/// `clarp-host tool NAME [ARGS...]`: one of an agent's own tools. It is a pipe: what it was
/// called with goes to the Host the agent runs on, and what the Host answers is printed. The
/// Host is found by what it told the turn (`CLARP_HOST_ADDR`), or, for a worker an agent left
/// running, in the file the installer wrote for the agents of this Host. A tool that is
/// called where neither says so does nothing and says why.
///
/// `owner_mode`: the token is the administrator's, put there on purpose by the owner's own
/// command (CLARP_HOST_TOKEN), and the Host is told so. A token read from the installer's file
/// is never marked.
pub fn tool(mut args: impl Iterator<Item = String>, token: &str, owner_mode: bool) -> ToolOutcome {
    let Some(name) = args.next() else {
        return ToolOutcome {
            exit: 2,
            stdout: String::new(),
            stderr: "usage: clarp-host tool NAME [ARGS...]\n".to_string(),
        };
    };
    let argv: Vec<String> = args.collect();
    let argv = match from_stdin(&name, argv, || {
        use std::io::Read as _;
        let mut text = String::new();
        std::io::stdin().read_to_string(&mut text).map(|_| text)
    }) {
        Ok(argv) => argv,
        Err(why) => {
            return ToolOutcome {
                exit: 2,
                stdout: String::new(),
                stderr: format!("{name}: {why}. Nothing was done.\n"),
            };
        }
    };
    // Job monitors stop their workers on exit 1. Only a valid Host answer may name
    // a terminal or superseded job. A failure to get that answer leaves it unknown.
    let job_active = name == "clarp-agent-bg" && argv.get(1).is_some_and(|arg| arg == "job-active");
    let wait_limit = wait_ends(&name, &argv);
    let unavailable = |why: String| ToolOutcome {
        exit: if job_active { 2 } else { 69 },
        stdout: String::new(),
        stderr: format!("{why}\n"),
    };
    let (address, token) = match (
        std::env::var(crate::TOOL_ADDR_ENV),
        installed_host(std::env::var_os),
    ) {
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
    let mut env: std::collections::BTreeMap<String, String> = std::env::vars()
        .filter(|(key, _)| key.starts_with("CLARP_") || key.starts_with("CLAUDE_PWA_"))
        .filter(|(key, _)| !key.ends_with("_TOKEN"))
        .collect();
    // A turn of a Codex lane is told who it is in a file its lane rewrites at each turn: its
    // app-server's environment cannot change between turns (issue 742).
    if !env.contains_key(crate::turn_identity::TURN_ENV)
        && let Some(token) = std::env::var(crate::lane::TURN_FILE_ENV)
            .ok()
            .and_then(|path| turn_from_file(std::path::Path::new(&path)))
    {
        env.insert(crate::turn_identity::TURN_ENV.to_string(), token);
    }
    let cwd = std::env::current_dir()
        .map(|dir| dir.display().to_string())
        .unwrap_or_default();
    let started_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map_or(0, |since| i64::try_from(since.as_millis()).unwrap_or(0));
    // Only peers list uses the agent tool route. The owner's add/remove command still
    // checks its authority locally before any remote pairing request.
    if name == "clarp-admin"
        && argv.first().is_some_and(|arg| arg == "peers")
        && !matches!(argv.as_slice(), [_, action] if action == "list")
    {
        let result = peer_command(argv.into_iter().skip(1), token, "", address, true);
        return match result {
            Ok(shown) => ToolOutcome {
                exit: 0,
                stdout: format!("{shown}\n"),
                stderr: String::new(),
            },
            Err(why) => ToolOutcome {
                exit: 2,
                stdout: String::new(),
                stderr: format!("clarp-admin peers: {why}\n"),
            },
        };
    }
    let tell_user = name == "clarp-admin" && argv.first().is_some_and(|arg| arg == "tell-user");
    // Read the capability in the invoking tool's process. The server never
    // opens an arbitrary path supplied by an HTTP tool caller.
    let mut credential = String::new();
    if matches!(name.as_str(), "clarp-goal" | "clarp-agent-tasks")
        && argv.first().is_some_and(|s| s == "bookkeeping")
        && match argv.get(1).map(String::as_str) {
            Some("observe") => {
                argv.len() == 3
                    || (argv.len() == 4 && serde_json::from_str::<Value>(&argv[3]).is_ok())
            }
            Some("record") => argv.len() == 6 && serde_json::from_str::<Value>(&argv[5]).is_ok(),
            _ => false,
        }
        && let Some(id) = argv.get(2)
    {
        let explicit = std::env::var_os("CLARP_DELEGATION_TOKEN_FILE")
            .filter(|s| !s.is_empty())
            .map(PathBuf::from);
        let valid_id = id.len() == 18
            && id.starts_with("dg")
            && id[2..].bytes().all(|b| b.is_ascii_hexdigit());
        if !valid_id {
            return ToolOutcome {
                exit: 4,
                stdout: String::new(),
                stderr: "agent_tasks: unknown delegation or wrong credential\n".into(),
            };
        }
        let path = explicit.unwrap_or_else(|| {
            let dir = std::env::var_os("CLARP_DELEGATION_DIR")
                .map(PathBuf::from)
                .unwrap_or_else(|| {
                    std::env::var_os("XDG_DATA_HOME")
                        .map(PathBuf::from)
                        .unwrap_or_else(|| crate::config::home().join(".local/share"))
                        .join("clarp-host/delegations")
                });
            dir.join(format!("{id}.token"))
        });
        let read = || -> std::io::Result<String> {
            use std::io::Read as _;
            let mut text = String::new();
            use std::os::unix::fs::OpenOptionsExt as _;
            let file = std::fs::OpenOptions::new()
                .read(true)
                .custom_flags(nix::libc::O_NONBLOCK)
                .open(&path)?;
            if !file.metadata()?.is_file() {
                return Err(std::io::ErrorKind::InvalidInput.into());
            }
            file.take(66).read_to_string(&mut text)?;
            if text.len() >= 66 {
                return Err(std::io::ErrorKind::InvalidData.into());
            }
            Ok(text)
        };
        match read() {
            Ok(value) => {
                let value = value.trim();
                if value.len() == 64 && value.bytes().all(|b| b.is_ascii_hexdigit()) {
                    credential = value.into();
                }
            }
            Err(e) => {
                return ToolOutcome {
                    exit: 4,
                    stdout: String::new(),
                    stderr: format!("agent_tasks: cannot read the delegation credential: {e}\n"),
                };
            }
        }
    }
    let mut asked = json!({
        "tool": name,
        "argv": argv,
        "cwd": cwd,
        "env": env,
        "ppid": std::os::unix::process::parent_id(),
        "started_ms": started_ms,
    });
    if !credential.is_empty() {
        asked["bookkeeping_credential"] = json!(credential);
    }
    if tell_user {
        // Each attempt carries the same id, including when the Host wrote the note but
        // the connection closed before the tool received the answer.
        asked["note_id"] = json!(format!("tell-{}", ids::hex(8)));
    }
    let mut retries = if tell_user { 6 } else { 0 };
    let mut delay = Duration::from_secs(1);
    // Since when the Host has not answered a wait for a card's answer.
    let began = std::time::Instant::now();
    let mut unanswered_since: Option<std::time::Instant> = None;
    loop {
        let result = marked_request(
            "POST",
            &address,
            "/tools/run",
            token,
            owner_mode,
            Some(&asked),
        );
        let retryable = match &result {
            Err(_) | Ok((502..=504, _)) => true,
            Ok((200, answer)) => answer.is_null(),
            _ => false,
        };
        if retryable && retries > 0 {
            retries -= 1;
            std::thread::sleep(delay);
            delay = (delay * 2).min(Duration::from_secs(16));
            continue;
        }
        if !retryable {
            unanswered_since = None;
        } else if asks_again(
            wait_limit,
            began.elapsed(),
            unanswered_since
                .get_or_insert_with(std::time::Instant::now)
                .elapsed(),
        ) {
            std::thread::sleep(Duration::from_secs(2));
            continue;
        }
        let answer = match result {
            Ok((200, answer)) if tell_user && answer.is_null() => {
                return unavailable(format!("{name}: the Host at {address} sent no tool answer"));
            }
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
        let exit = answer["exit"]
            .as_i64()
            .and_then(|code| u8::try_from(code).ok());
        if job_active && !matches!(exit, Some(0..=2)) {
            return unavailable(format!(
                "{name}: the Host at {address} sent no valid job state"
            ));
        }
        return ToolOutcome {
            exit: exit.unwrap_or(1),
            stdout: answer["stdout"].as_str().unwrap_or("").to_string(),
            stderr: answer["stderr"].as_str().unwrap_or("").to_string(),
        };
    }
}

/// The goal tool's argument that is exactly `-`, replaced by what was piped into the tool.
/// Linux holds one argument to 128 KiB, and a goal's stored texts can grow past that (issue
/// 768): read here, in the tool's own process, the body goes to the Host over HTTP whole. One
/// `-` a call, since one standard input fills one argument. Other tools keep `-` as it was
/// given: for some of them it is a word of their own.
fn from_stdin(
    name: &str,
    mut argv: Vec<String>,
    read: impl FnOnce() -> std::io::Result<String>,
) -> Result<Vec<String>, String> {
    if !matches!(name, "clarp-goal" | "clarp-agent-tasks") {
        return Ok(argv);
    }
    let mut dashes = argv.iter().enumerate().filter(|(_, arg)| *arg == "-");
    let Some((at, _)) = dashes.next() else {
        return Ok(argv);
    };
    if dashes.next().is_some() {
        return Err("only one argument may be - (read from standard input)".to_string());
    }
    argv[at] = read().map_err(|why| format!("cannot read standard input: {why}"))?;
    Ok(argv)
}

/// How long a wait for a card's answer asks again a Host that does not answer: a release
/// restarts the Host, and it is away for seconds.
const RESTART_GRACE: Duration = Duration::from_secs(120);

/// How long a wait for a card's answer lasts by its own `--timeout` (600 seconds unless given,
/// as the Host reads it): such a wait only reads, and every ask carries the time it began
/// ([`crate::artifact_tool`]), so asking it again changes nothing. `None` for every other
/// call, which is never asked again: a ring, for one, lives in the Host's memory, and asked
/// again after a restart it would be refused as a call already made, or placed once more.
fn wait_ends(name: &str, argv: &[String]) -> Option<Duration> {
    if name != "clarp-agent-artifacts" || argv.first().is_none_or(|arg| arg != "wait") {
        return None;
    }
    let mut seconds = 600;
    let mut args = argv.iter();
    while let Some(arg) = args.next() {
        let given = match arg.strip_prefix("--timeout=") {
            Some(value) => Some(value),
            None if arg == "--timeout" => args.next().map(String::as_str),
            None => None,
        };
        if let Some(value) = given.and_then(|value| value.trim().parse::<u64>().ok()) {
            seconds = value.max(1);
        }
    }
    Some(Duration::from_secs(seconds))
}

/// Whether a call the Host did not answer is asked again: a wait for a card's answer that has
/// time left of its own, while the Host has been away for less than [`RESTART_GRACE`].
fn asks_again(wait_limit: Option<Duration>, waited: Duration, away_for: Duration) -> bool {
    wait_limit.is_some_and(|limit| waited < limit) && away_for < RESTART_GRACE
}

/// `clarp-host peer`: names another Host for the running Host, lists the ones it knows, or
/// forgets one. Returns what to show.
pub fn peer(
    args: impl Iterator<Item = String>,
    token: &str,
    peer_token: &str,
    json_output: bool,
) -> Result<String, String> {
    peer_command(
        args,
        token,
        peer_token,
        "127.0.0.1:7690".to_string(),
        json_output,
    )
}

fn peer_command(
    mut args: impl Iterator<Item = String>,
    token: &str,
    peer_token: &str,
    mut host: String,
    json_output: bool,
) -> Result<String, String> {
    let mut words = Vec::new();
    let (mut code, mut explicit_token, mut device_name) = (None, None, None);
    let mut token_stdin = false;
    while let Some(arg) = args.next() {
        let mut value = || args.next().ok_or_else(|| format!("{arg} needs a value"));
        match arg.as_str() {
            "--host" => host = value()?,
            "--code" if code.is_none() => code = Some(value()?),
            "--device-name" if device_name.is_none() => device_name = Some(value()?),
            "--token" if explicit_token.is_none() => explicit_token = Some(value()?),
            "--token-stdin" if !token_stdin => token_stdin = true,
            flag if flag.starts_with('-') => {
                return Err("unknown or repeated peer option".to_string());
            }
            _ => words.push(arg),
        }
    }
    // A pairing token may go back to this Host only over loopback. Use the resolved
    // address in every request so a later lookup cannot change the destination.
    let host = host.trim_start_matches("http://").trim_end_matches('/');
    let host = host
        .to_socket_addrs()
        .map_err(|_| "invalid local Host address")?
        .find(|address| address.ip().is_loopback())
        .ok_or("the local Host address must be on loopback")?
        .to_string();
    let refused = |status: u16, answer: &Value| {
        let why = answer["error"].as_str().unwrap_or("no reason given");
        format!("the Host refused ({status}): {why}")
    };
    let modes = usize::from(code.is_some())
        + usize::from(explicit_token.is_some())
        + usize::from(token_stdin);
    let words: Vec<_> = words.iter().map(String::as_str).collect();
    match words.as_slice() {
        ["add", positions @ ..] if (1..=2).contains(&positions.len()) => {
            let (alias, url) = match positions {
                [name, url] => (name.trim(), *url),
                [url] => ("", *url),
                _ => return Err("say which peer URL to add".to_string()),
            };
            let url = crate::peers::normalize_url(url).ok_or("peer requires an HTTP(S) URL")?;
            if modes > 1 || (modes == 0 && (json_output || peer_token.is_empty())) {
                return Err("give exactly one of --code, --token-stdin or --token".to_string());
            }
            if device_name.is_some() && code.is_none() {
                return Err("--device-name requires --code".to_string());
            }
            // Check local authority before spending a code or reading the other Host.
            let (status, answer) = request("GET", &host, "/peers", token, None)?;
            if status != 200 {
                return Err(refused(status, &answer));
            }
            crate::peers::warn_plain_http(url);
            let paired = code.is_some();
            let identity = if paired {
                crate::peers::identity(url).map_err(|code| format!("peer identity: {code}"))?
            } else if modes > 0 {
                crate::peers::identity(url).unwrap_or_default()
            } else {
                serde_json::Map::new()
            };
            let learned_name = crate::peers::reported(&identity, "name");
            let name = if alias.is_empty() {
                learned_name
                    .as_deref()
                    .ok_or("could not learn the peer's name; give it: peers add NAME URL")?
            } else {
                alias
            };
            let server_id = crate::peers::reported(&identity, "server_id").unwrap_or_default();
            let credential = if let Some(code) = code {
                if code.trim().is_empty() {
                    return Err("--code needs a pairing code".to_string());
                }
                let device_name = match device_name.filter(|name| !name.is_empty()) {
                    Some(name) => name,
                    None => {
                        let info = get(&host, "/server-info", token)?;
                        let name = info["name"]
                            .as_str()
                            .ok_or("the local Host did not report a name")?;
                        format!("{name} (peer Host)")
                    }
                };
                crate::peers::exchange_code(url, &code, &device_name)
                    .map_err(|code| format!("peer pairing: {code}"))?
            } else if token_stdin {
                let mut credential = String::new();
                std::io::stdin()
                    .read_line(&mut credential)
                    .map_err(|_| "could not read peer token")?;
                credential
            } else {
                explicit_token.unwrap_or_else(|| peer_token.to_string())
            };
            let named = json!({
                "name": name, "url": url, "token": credential.trim(), "server_id": server_id,
            });
            match post(&host, "/peers", token, &named)? {
                (200, answer) if json_output => {
                    serde_json::to_string_pretty(&answer).map_err(|error| error.to_string())
                }
                (200, answer) => Ok(format!(
                    "{} is the Host at {}",
                    answer["name"].as_str().unwrap_or(name),
                    answer["url"].as_str().unwrap_or(url)
                )),
                (status, answer) => Err(refused(status, &answer)),
            }
        }
        ["list"] if modes == 0 && device_name.is_none() => {
            let peers = get(&host, "/peers", token)?;
            if json_output {
                return serde_json::to_string_pretty(&peers).map_err(|error| error.to_string());
            }
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
        ["remove", name] if modes == 0 && device_name.is_none() => {
            let mut path =
                url::Url::parse("http://localhost/peers").map_err(|error| error.to_string())?;
            path.path_segments_mut()
                .map_err(|()| "invalid peer route")?
                .push(name);
            match request("DELETE", &host, path.path(), token, None)? {
                (200, _) => Ok(format!("removed peer: {name}")),
                (status, answer) => Err(refused(status, &answer)),
            }
        }
        _ => Err("say what to do: add [NAME] URL, list, or remove NAME".to_string()),
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
    fn the_goal_tools_dash_is_what_was_piped_in_once_a_call() {
        let args = |list: &[&str]| {
            list.iter()
                .map(|arg| (*arg).to_string())
                .collect::<Vec<_>>()
        };
        let piped = || Ok("{\"progress\":\"p\"}\n".to_string());
        assert_eq!(
            from_stdin("clarp-goal", args(&["checkpoint", "p1", "3", "-"]), piped),
            Ok(args(&["checkpoint", "p1", "3", "{\"progress\":\"p\"}\n"]))
        );
        assert_eq!(
            from_stdin(
                "clarp-agent-tasks",
                args(&["subgoal", "p1", "add", "-"]),
                piped
            ),
            Ok(args(&["subgoal", "p1", "add", "{\"progress\":\"p\"}\n"]))
        );
        let unread = || -> std::io::Result<String> { panic!("nothing is read without a dash") };
        assert_eq!(
            from_stdin("clarp-goal", args(&["get", "p1"]), unread),
            Ok(args(&["get", "p1"]))
        );
        assert_eq!(
            from_stdin(
                "clarp-admin",
                args(&["agent", "fork", "--spec", "-"]),
                unread
            ),
            Ok(args(&["agent", "fork", "--spec", "-"])),
            "another tool keeps its dash"
        );
        let twice = from_stdin("clarp-goal", args(&["act", "p1", "-", "x", "-"]), unread);
        assert!(twice.is_err_and(|why| why.contains("only one")));
        let broken = || Err(std::io::ErrorKind::InvalidData.into());
        let failed = from_stdin("clarp-goal", args(&["checkpoint", "p1", "3", "-"]), broken);
        assert!(failed.is_err_and(|why| why.contains("standard input")));
    }

    #[test]
    fn a_wait_for_a_card_is_asked_again_through_a_restart_and_nothing_else_is() {
        let args = |list: &[&str]| {
            list.iter()
                .map(|arg| (*arg).to_string())
                .collect::<Vec<_>>()
        };
        let wait = |list: &[&str]| wait_ends("clarp-agent-artifacts", &args(list));
        let minutes = |count: u64| Some(Duration::from_secs(count * 60));
        assert_eq!(wait(&["wait", "card-1", "--timeout", "300"]), minutes(5));
        assert_eq!(wait(&["wait", "card-1", "--timeout=60"]), minutes(1));
        assert_eq!(
            wait(&["wait", "card-1"]),
            minutes(10),
            "the Host's own default"
        );
        assert_eq!(
            wait(&["input", "s", "t", "q"]),
            None,
            "a card is never made twice"
        );
        assert_eq!(wait(&[]), None);
        let ring = args(&["call", "ring", "--wait", "--reason", "x"]);
        assert_eq!(
            wait_ends("clarp-admin", &ring),
            None,
            "a ring is never asked again"
        );
        let second = Duration::from_secs(1);
        assert!(asks_again(minutes(5), Duration::ZERO, Duration::ZERO));
        assert!(asks_again(minutes(5), 60 * second, RESTART_GRACE - second));
        assert!(
            !asks_again(minutes(5), 60 * second, RESTART_GRACE),
            "a Host away this long is down"
        );
        assert!(
            !asks_again(minutes(1), 60 * second, Duration::ZERO),
            "the wait's own time is up"
        );
        assert!(
            !asks_again(None, Duration::ZERO, Duration::ZERO),
            "not a wait"
        );
    }

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

    #[test]
    fn a_tool_falls_through_python_configs_to_the_installed_rust_host() {
        let root = crate::once::a_place("tool-host-test").unwrap();
        let explicit = root.join("explicit.toml");
        let config_dir = root.join("config-dir");
        let default_dir = root.join(".config/clarp-host");
        std::fs::create_dir(&config_dir).unwrap();
        std::fs::create_dir_all(&default_dir).unwrap();

        let python = "[server]\nbind_addr = \"127.0.0.1\"\nport = 7682\nauth_token = \"python\"\n";
        let rust = |port| {
            format!(
                "[server]\nkind = \"clarp-host\"\nbind_addr = \"127.0.0.1\"\nport = {port}\nauth_token = \"rust\"\n"
            )
        };
        std::fs::write(&explicit, python).unwrap();
        std::fs::write(config_dir.join("config.toml"), python).unwrap();
        std::fs::write(default_dir.join("config.toml"), rust(7690)).unwrap();
        let find = || {
            installed_host(|name| {
                let path = match name {
                    "CLAUDE_PWA_CONFIG" => &explicit,
                    "CLARP_CONFIG_DIR" => &config_dir,
                    "HOME" => &root,
                    _ => return None,
                };
                Some(path.as_os_str().to_owned())
            })
        };
        let fallback = find();
        std::fs::write(config_dir.join("config.toml"), rust(7691)).unwrap();
        let directory = find();
        std::fs::write(&explicit, rust(7692)).unwrap();
        let named = find();
        std::fs::remove_dir_all(root).unwrap();

        let expected = |port| Some((format!("127.0.0.1:{port}"), "rust".to_string()));
        assert_eq!(fallback, expected(7690));
        assert_eq!(directory, expected(7691));
        assert_eq!(named, expected(7692));
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
             koko --files-from ADDR:PORT` can bring them before the move finishes.\n\
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
        assert!(more.contains("--files-from ADDR:PORT` can bring it before the move finishes.\n"));
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
    fn a_move_skips_removed_files() {
        use std::io::{BufRead, BufReader};
        use std::net::TcpListener;

        let source = TcpListener::bind("127.0.0.1:0").unwrap();
        let from = source.local_addr().unwrap().to_string();
        let serve = std::thread::spawn(move || {
            for (id, status) in [
                ("removed", "410 Gone"),
                ("also-removed", "410 Gone"),
                ("missing", "404 Not Found"),
            ] {
                let (stream, _) = source.accept().unwrap();
                stream
                    .set_read_timeout(Some(Duration::from_secs(3)))
                    .unwrap();
                let mut request = BufReader::new(stream);
                let mut line = String::new();
                request.read_line(&mut line).unwrap();
                assert_eq!(line, format!("GET /media/{id} HTTP/1.1\r\n"));
                loop {
                    line.clear();
                    if request.read_line(&mut line).unwrap() == 0 || line == "\r\n" {
                        break;
                    }
                }
                write!(
                    request.get_mut(),
                    "HTTP/1.1 {status}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                )
                .unwrap();
            }
        });
        let way = Way {
            from: &from,
            source_token: "",
            files_from: None,
            host: "127.0.0.1:0",
            token: "",
            session: "chat",
        };
        let wanted = ["removed", "also-removed", "missing"].map(|id| Wanted {
            id: id.to_string(),
            ..Wanted::default()
        });
        let followed = files(&way, &wanted);
        serve.join().unwrap();

        assert_eq!(
            followed,
            Followed {
                removed: 2,
                absent: 1,
                ..Followed::default()
            }
        );
        let (came, more) = followed.told(&way);
        assert_eq!(came, "no files");
        assert!(more.contains(&format!("2 files were removed on the Host at {from}.\n")));
        assert!(more.contains("One file that it names is not on the Host"));
        assert!(!more.contains("did not arrive"));
    }

    #[test]
    fn a_files_only_retry_finishes_a_held_move() {
        use std::io::{BufRead, BufReader};
        use std::net::TcpListener;

        let serve = |script: Vec<(&str, u16, Value)>| {
            let script: Vec<_> = script
                .into_iter()
                .map(|(request, status, body)| (request.to_string(), status, body))
                .collect();
            let listener = TcpListener::bind("127.0.0.1:0").unwrap();
            let address = listener.local_addr().unwrap().to_string();
            let worker = std::thread::spawn(move || {
                for (expected, status, response) in script {
                    let (stream, _) = listener.accept().unwrap();
                    stream
                        .set_read_timeout(Some(Duration::from_secs(3)))
                        .unwrap();
                    let mut request = BufReader::new(stream);
                    let mut line = String::new();
                    request.read_line(&mut line).unwrap();
                    assert_eq!(line, format!("{expected} HTTP/1.1\r\n"));
                    let mut length = 0;
                    loop {
                        line.clear();
                        if request.read_line(&mut line).unwrap() == 0 || line == "\r\n" {
                            break;
                        }
                        if let Some(count) = line.strip_prefix("Content-Length: ") {
                            length = count.trim().parse().unwrap();
                        }
                    }
                    let mut body = vec![0; length];
                    request.read_exact(&mut body).unwrap();
                    match expected.as_str() {
                        "POST /agent-moved" => assert_eq!(
                            serde_json::from_slice::<Value>(&body).unwrap(),
                            json!({"session": "chat", "moved_to": {"host": "new", "id": "destination"}}),
                        ),
                        "POST /turn-queue/resume" => assert_eq!(
                            serde_json::from_slice::<Value>(&body).unwrap(),
                            json!({"session": "chat"}),
                        ),
                        _ => assert!(body.is_empty()),
                    }
                    let response = response.to_string();
                    write!(
                        request.get_mut(),
                        "HTTP/1.1 {status} Reply\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{response}",
                        response.len()
                    )
                    .unwrap();
                }
            });
            (address, worker)
        };

        let at = |paused, moved: Value| {
            json!({"agents": [{
                "session": "chat", "agent_id": "a-chat", "busy": false,
                "queue_paused": paused, "moved": moved,
            }]})
        };
        let held = at(true, Value::Null);
        let moved = json!({"to": {"host": "new", "id": "destination"}, "at": 100});
        let marked = at(false, moved);
        let identity = json!({"name": "new", "server_id": "destination"});
        let before_files = || {
            vec![
                ("GET /agents/snapshot", 200, at(false, Value::Null)),
                (
                    "GET /log?session=chat&limit=1000",
                    200,
                    json!({"turns": []}),
                ),
                ("GET /task-plans?session=chat", 200, json!({})),
                (
                    "GET /artifacts?session=chat&order=created&limit=500&offset=0",
                    200,
                    json!({}),
                ),
                (
                    "GET /media?session=chat&limit=500",
                    200,
                    json!({"assets": [{"asset_id": "gone"}]}),
                ),
            ]
        };
        let run = |from: &str, host: &str| {
            import(
                [
                    "--files",
                    "--from",
                    from,
                    "--host",
                    host,
                    "--session",
                    "chat",
                ]
                .map(str::to_string)
                .into_iter(),
                "",
                "",
                "",
            )
        };

        let mut source = before_files();
        source.extend([
            ("GET /media/gone", 410, json!({})),
            ("GET /agents/snapshot", 200, at(false, Value::Null)),
            (
                "GET /server-info",
                200,
                json!({"server_id": "source", "capabilities": {"features": ["agent_moved"]}}),
            ),
            ("GET /agents/snapshot", 200, at(false, Value::Null)),
            (
                "POST /agent-moved",
                200,
                json!({"ok": true, "session": "chat", "moved": marked["agents"][0]["moved"]}),
            ),
            ("GET /agents/snapshot", 200, marked.clone()),
            // Repeating the command for the now-finished move only reads the two snapshots.
            ("GET /agents/snapshot", 200, marked),
        ]);
        let (from, old) = serve(source);
        let (host, new) = serve(vec![
            ("GET /agents/snapshot", 200, held.clone()),
            ("GET /agents/snapshot", 200, held.clone()),
            ("GET /pairing/identity", 200, identity.clone()),
            (
                "POST /turn-queue/resume",
                200,
                json!({"session": "chat", "paused": false}),
            ),
            ("GET /agents/snapshot", 200, at(false, Value::Null)),
            ("GET /pairing/identity", 200, identity),
        ]);
        let report = run(&from, &host).unwrap();
        assert!(
            report.contains(&format!("One file was removed on the Host at {from}.")),
            "{report}"
        );
        assert!(
            report.ends_with("Its turn-start doors there now refuse it. It is resumed here."),
            "{report}"
        );
        assert_eq!(run(&from, &host).unwrap(), "The move is already finished.");
        old.join().unwrap();
        new.join().unwrap();

        let mut source = before_files();
        source.push(("GET /media/gone", 503, json!({})));
        let (from, old) = serve(source);
        let (host, new) = serve(vec![("GET /agents/snapshot", 200, held)]);
        let report = run(&from, &host).unwrap();
        assert!(report.contains("One file did not arrive"), "{report}");
        assert!(report.contains("The move is incomplete"), "{report}");
        assert!(report.contains("The copy here stays held"), "{report}");
        old.join().unwrap();
        new.join().unwrap();
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
