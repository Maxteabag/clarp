//! The connection relay, as the Host's connector speaks to it.
//!
//! A Host without a public address is reached through a relay: the Host keeps one outbound
//! WebSocket to it, and whatever a phone asks comes down that socket as frames, each for one
//! stream: a request with its body, or a WebSocket. The Host answers in frames on the same
//! socket. The relay knows the Host by a name and a key; phones are not known to the relay at
//! all: the Host checks them, as it checks every remote caller.
//!
//! Here are the rules without the socket: what a frame is, what the connector does with each
//! frame it is given, with which headers a request is handed to the Host, how an answer's
//! head is written, how long the connector waits before it dials again, and which settings
//! name a relay. They are the Python Host's (`server/lib/relay_connector.py`,
//! `server/lib/relay_settings.py`), held to it by `tests/connector_vectors.rs`, so that one
//! relay serves either Host and a phone paired through it does not notice which.

use std::collections::BTreeMap;

use serde_json::{Map, Value, json};

use crate::python;
use crate::trust::{self, Url};

/// The kinds of frame. A frame is its kind, the number of its stream in four bytes with the
/// largest first, and what it carries.
pub const REQ: u8 = 0x01;
pub const REQ_BODY: u8 = 0x02;
pub const REQ_END: u8 = 0x03;
pub const RES_HEAD: u8 = 0x11;
pub const RES_BODY: u8 = 0x12;
pub const RES_END: u8 = 0x13;
pub const WS_OPEN: u8 = 0x21;
pub const WS_ACCEPT: u8 = 0x22;
pub const WS_MSG: u8 = 0x23;
pub const WS_CLOSE: u8 = 0x24;
pub const ABORT: u8 = 0x7F;

/// How much of an answer's body goes into one frame.
pub const CHUNK: usize = 256 * 1024;
/// How much of requests' bodies a Host holds at once, over all streams.
pub const MAX_BODY: usize = 64 * 1024 * 1024;
/// How many streams a Host carries at once.
pub const MAX_STREAMS: usize = 64;
/// How many things a phone said on one WebSocket may wait to be taken.
pub const HEARD_MOST: usize = 16;
/// The largest message the connector takes from the relay.
pub const MESSAGE_MOST: usize = 1024 * 1024;
/// How long a request may take to arrive whole, and how long the relay is given to answer
/// the dial.
pub const REQUEST_WHOLE_WITHIN_S: u64 = 60;
pub const DIAL_WITHIN_S: u64 = 20;
/// The connector asks the relay whether it is there this often, and gives it this long.
pub const PING_EVERY_S: u64 = 25;
pub const PONG_WITHIN_S: u64 = 20;

/// The headers that belong to one hop and are never handed on.
pub const HOP: [&str; 10] = [
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-connection",
    "proxy-authorization",
    "host",
    "content-length",
];

/// The headers the apps mark their own requests with: which app asks and how old it is, the
/// call a clip belongs to, and the interaction a request is part of. A relay's request may
/// carry them; any other header of the Host's own family (`x-clarp-`) says how a request
/// travelled or who sent it, which a remote caller is not believed about.
///
/// Until 16 Nov 2026 the Python connector dropped these three with the rest of the family
/// (reference defect R25, found by this port and fixed there the same day): a clip through
/// the relay no longer said which call it belonged to.
pub const THE_APPS_OWN: [&str; 3] = [
    "x-clarp-client",
    "x-clarp-call-id",
    "x-clarp-interaction-id",
];

/// When the relay is dialled again: at once after a session that was up, a little later the
/// longer it cannot be reached, and not at once when another connector took this Host's name.
pub const REDIAL_FAST_S: f64 = 0.3;
pub const REDIAL_MIN_S: f64 = 1.0;
pub const REDIAL_MAX_S: f64 = 30.0;
pub const REDIAL_REPLACED_S: f64 = 5.0;
/// How the relay closes a session when another connector logs in under the same name.
pub const REPLACED: (u16, &str) = (1012, "replaced");
/// How a phone's WebSocket is closed when the Host no longer has its stream, and how one
/// that the Host cannot serve.
pub const STREAM_GONE: (u16, &str) = (1012, "Host stream gone");
pub const SOCKET_FAILED: (u16, &str) = (1011, "Host WebSocket failed");
pub const SOCKET_CLOSED: &str = "Host connection closed";
/// What a request is answered with when the Host could not make it.
pub const REQUEST_FAILED: &str = r#"{"error":"Host request failed"}"#;

/// A frame.
pub fn frame(kind: u8, sid: u32, payload: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(5 + payload.len());
    out.push(kind);
    out.extend_from_slice(&sid.to_be_bytes());
    out.extend_from_slice(payload);
    out
}

/// A frame that carries a value, written as the Python connector writes it.
pub fn json_frame(kind: u8, sid: u32, said: &Value) -> Vec<u8> {
    frame(kind, sid, python::compact_ascii(said).as_bytes())
}

/// The frame that closes a phone's WebSocket.
pub fn closing(sid: u32, (code, reason): (u16, &str)) -> Vec<u8> {
    json_frame(WS_CLOSE, sid, &json!({ "code": code, "reason": reason }))
}

/// What a frame is of: its kind, its stream, and what it carries. `None`: too short for one.
pub fn parsed(frame: &[u8]) -> Option<(u8, u32, &[u8])> {
    let (head, payload) = frame.split_at_checked(5)?;
    let sid = u32::from_be_bytes([head[1], head[2], head[3], head[4]]);
    Some((head[0], sid, payload))
}

/// How long the connector waits before it dials again. `previous` is the wait before the
/// attempt that just ended (`None` before the first), `was_connected` whether that attempt
/// had a session, and `received` the close the relay sent, when it sent one.
pub fn redial_after(
    previous: Option<f64>,
    was_connected: bool,
    received: Option<(u16, &str)>,
) -> f64 {
    if was_connected {
        return if received == Some(REPLACED) {
            REDIAL_REPLACED_S
        } else {
            REDIAL_FAST_S
        };
    }
    match previous {
        None => REDIAL_MIN_S,
        Some(previous) => (previous * 2.0).clamp(REDIAL_MIN_S, REDIAL_MAX_S),
    }
}

// ---- the request a frame asks for ------------------------------------------------------------

/// What a `REQ` or a `WS_OPEN` says: which path, with which headers, how.
#[derive(Debug, Clone, PartialEq)]
pub struct Meta {
    /// The path and what follows it, as the Host is asked for it.
    pub path: String,
    /// The phone's headers as the relay wrote them, in its order.
    pub headers: Map<String, Value>,
    /// The method: `GET` where none is given. What is no text is no method.
    pub method: Value,
    /// Whether the relay passes a packed answer on as it is.
    pub passthrough: bool,
}

/// The encoding the Host is asked to answer in. An answer's body goes to the phone byte for
/// byte with its `Content-Encoding`, and a relay that builds its own answer from that packs
/// the body again. So a packed answer is asked for only when the relay says it passes one
/// through untouched and the phone takes gzip.
pub fn answer_encoding(headers: &[(String, String)], passthrough: bool) -> &'static str {
    if !passthrough {
        return "identity";
    }
    let mut named = headers.iter().rev();
    let accept = named.find(|(name, _)| name.eq_ignore_ascii_case("accept-encoding"));
    let accept = accept.map_or("", |(_, value)| value.as_str());
    let offered = accept.split(',').any(|part| {
        let name = part.split(';').next().unwrap_or_default();
        let weight = part.replace(' ', "").to_lowercase();
        let weight = weight.split_once(";q=").map_or("", |(_, weight)| weight);
        python::strip(name).eq_ignore_ascii_case("gzip")
            && !matches!(weight, "0" | "0.0" | "0.00" | "0.000")
    });
    if offered { "gzip" } else { "identity" }
}

/// Whether a sign may stand in a header's name (a token of HTTP).
fn in_a_name(sign: char) -> bool {
    sign.is_ascii_alphanumeric() || "!#$%&'*+-.^_`|~".contains(sign)
}

/// The headers a relay's request is handed to the Host with. First the one thing the
/// connector knows, that the request came through the relay: whatever follows, the Host has
/// read that. Then the phone's own, without what belongs to a hop, what a header of the hop
/// names, and what says how or for whom a request travelled; then where the Host is, how
/// it may answer, and for whom the relay says it asks, when that is an address.
///
/// `None`: something the relay handed over is no header HTTP can carry (a name that is no
/// token, a value that is no text or has a control sign in it). The request is refused
/// whole: such a name would not be caught by what is left out here.
pub fn handed_on(
    meta: &Map<String, Value>,
    local: &str,
    body_len: usize,
    passthrough: bool,
) -> Option<Vec<(String, String)>> {
    let mut given = Vec::with_capacity(meta.len());
    for (name, value) in meta {
        let value = value.as_str()?;
        let control = |sign: char| (sign < ' ' && sign != '\t') || sign == '\u{7f}';
        if name.is_empty() || !name.chars().all(in_a_name) || value.chars().any(control) {
            return None;
        }
        given.push((name.clone(), value.to_string()));
    }
    // A name written twice in different letters: what was written last counts.
    let last = |wanted: &str| {
        let mut named = given.iter().rev();
        let found = named.find(|(name, _)| name.to_lowercase() == wanted);
        found.map_or("", |(_, value)| value.as_str())
    };
    let of_the_hop: Vec<String> = last("connection")
        .split(',')
        .map(|name| python::strip(name).to_lowercase())
        .collect();
    let left_out = |name: &str| {
        let name = name.to_lowercase();
        let family = name.starts_with("x-clarp-") && !THE_APPS_OWN.contains(&name.as_str());
        HOP.contains(&name.as_str())
            || of_the_hop.contains(&name)
            || matches!(name.as_str(), "accept-encoding" | "forwarded" | "x-real-ip")
            || ["x-forwarded-", "tailscale-", "cf-", "sec-websocket"]
                .iter()
                .any(|begin| name.starts_with(begin))
            || family
    };
    let mut handed = vec![("X-Clarp-Transport".to_string(), "relay".to_string())];
    handed.extend(given.iter().filter(|(name, _)| !left_out(name)).cloned());
    let encoding = answer_encoding(&given, passthrough);
    handed.push(("Host".to_string(), local.to_string()));
    handed.push(("Accept-Encoding".to_string(), encoding.to_string()));
    if let Some(caller) = trust::ip_address(last("x-forwarded-for")) {
        handed.push(("X-Forwarded-For".to_string(), caller));
    }
    if body_len > 0 {
        handed.push(("Content-Length".to_string(), body_len.to_string()));
    }
    Some(handed)
}

/// The head of an answer, as it goes to the relay: its status, and its headers without what
/// belongs to a hop. A name the Host wrote twice goes once, with what it wrote last.
pub fn head(status: u16, headers: &[(String, String)]) -> Value {
    let mut kept = Map::new();
    for (name, value) in headers {
        if !HOP.contains(&name.to_lowercase().as_str()) {
            kept.insert(name.clone(), json!(value));
        }
    }
    json!({ "status": status, "headers": kept })
}

// ---- what the connector does with each frame -------------------------------------------------

/// Why a session ends: the relay sent what is no frame of this protocol. `class` is what the
/// Python connector's telemetry calls the same failure.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Violation {
    pub class: &'static str,
    pub words: String,
}

fn violation(words: &str) -> Violation {
    Violation {
        class: "ValueError",
        words: words.to_string(),
    }
}

/// What a phone said on a WebSocket.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Said<'a> {
    Text(&'a str),
    Bytes(&'a [u8]),
}

/// What follows from one frame.
#[derive(Debug, Clone, PartialEq)]
pub enum Step<'a> {
    /// Nothing.
    Nothing,
    /// A request begins. Its body follows in parts, then its end.
    Request { sid: u32, meta: Meta },
    /// A WebSocket is asked for.
    Socket { sid: u32, meta: Meta },
    /// A part of a request's body was taken.
    Body { sid: u32 },
    /// A request is whole.
    Whole { sid: u32 },
    /// A phone said something on its WebSocket.
    Heard { sid: u32, said: Said<'a> },
    /// A stream is over: whoever serves it stops. The relay is told `tell` first, when the
    /// Host is the one that ends it.
    Over { sid: u32, tell: Option<Vec<u8>> },
    /// The relay is told something about a stream this Host does not have.
    Tell(Vec<u8>),
}

/// The limits of a connector. A test makes them small.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Limits {
    pub max_body: usize,
    pub max_streams: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Self {
            max_body: MAX_BODY,
            max_streams: MAX_STREAMS,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
enum Stream {
    /// A request: what of its body has come, and whether its end has.
    Request {
        body: Vec<u8>,
        held: usize,
        ended: bool,
    },
    Socket,
}

/// The streams of one session with the relay.
#[derive(Debug, Clone, Default)]
pub struct Table {
    limits: Limits,
    streams: BTreeMap<u32, Stream>,
    /// How much of requests' bodies is held, over all streams.
    held: usize,
}

impl Table {
    pub fn new(limits: Limits) -> Self {
        Self {
            limits,
            ..Self::default()
        }
    }

    /// One frame from the relay. `Err`: the session ends.
    pub fn take<'a>(&mut self, frame: &'a [u8]) -> Result<Step<'a>, Violation> {
        let Some((kind, sid, payload)) = parsed(frame) else {
            return Err(violation("short relay frame"));
        };
        if matches!(kind, REQ | WS_OPEN) {
            return self.open(kind, sid, payload);
        }
        let Some(stream) = self.streams.get_mut(&sid) else {
            // The relay still has a phone's stream this session never saw (the Host was
            // started again, the relay lost its own state). It is answered, so that the
            // relay closes it for the phone to try again instead of leaving it to hang.
            return Ok(match kind {
                WS_MSG => Step::Tell(closing(sid, STREAM_GONE)),
                REQ_END => Step::Tell(self::frame(ABORT, sid, &[])),
                _ => Step::Nothing,
            });
        };
        match (kind, stream) {
            (REQ_BODY | REQ_END, Stream::Socket) => {
                Err(violation("HTTP frame on a WebSocket stream"))
            }
            (REQ_BODY, Stream::Request { ended: true, .. }) => {
                Err(violation("request body after end frame"))
            }
            (REQ_BODY, Stream::Request { .. })
                if self.held + payload.len() > self.limits.max_body =>
            {
                self.forget(sid);
                let tell = Some(self::frame(ABORT, sid, &[]));
                Ok(Step::Over { sid, tell })
            }
            (REQ_BODY, Stream::Request { body, held, .. }) => {
                body.extend_from_slice(payload);
                *held += payload.len();
                self.held += payload.len();
                Ok(Step::Body { sid })
            }
            (REQ_END, Stream::Request { ended, .. }) => {
                *ended = true;
                Ok(Step::Whole { sid })
            }
            (WS_MSG, Stream::Socket) => match payload.split_first() {
                None => Ok(Step::Nothing),
                Some((1, text)) => match std::str::from_utf8(text) {
                    Ok(text) => Ok(Step::Heard {
                        sid,
                        said: Said::Text(text),
                    }),
                    Err(error) => Err(Violation {
                        class: "UnicodeDecodeError",
                        words: error.to_string(),
                    }),
                },
                Some((_, bytes)) => Ok(Step::Heard {
                    sid,
                    said: Said::Bytes(bytes),
                }),
            },
            (ABORT | WS_CLOSE, _) => {
                self.forget(sid);
                Ok(Step::Over { sid, tell: None })
            }
            // What only a Host says, and what nobody says.
            _ => Ok(Step::Nothing),
        }
    }

    fn open<'a>(&mut self, kind: u8, sid: u32, payload: &[u8]) -> Result<Step<'a>, Violation> {
        if self.streams.contains_key(&sid) {
            return Err(violation("duplicate relay stream"));
        }
        if self.streams.len() >= self.limits.max_streams {
            return Ok(Step::Tell(frame(ABORT, sid, &[])));
        }
        let said: Value = serde_json::from_slice(payload).map_err(|error| Violation {
            class: "JSONDecodeError",
            words: error.to_string(),
        })?;
        let Value::Object(mut said) = said else {
            return Err(Violation {
                class: "AttributeError",
                words: "a request is an object".to_string(),
            });
        };
        let path = match said.get("path") {
            Some(Value::String(path)) => path.clone(),
            _ => String::new(),
        };
        let readable =
            path.starts_with('/') && !path.chars().any(|sign| sign < ' ' || sign == '\u{7f}');
        let Some(Value::Object(headers)) = said.remove("headers") else {
            return Err(violation("invalid relay request"));
        };
        if !readable {
            return Err(violation("invalid relay request"));
        }
        let meta = Meta {
            path,
            headers,
            method: said.remove("method").unwrap_or_else(|| json!("GET")),
            passthrough: said.get("passthrough_encoding") == Some(&Value::Bool(true)),
        };
        if kind == REQ {
            let stream = Stream::Request {
                body: Vec::new(),
                held: 0,
                ended: false,
            };
            self.streams.insert(sid, stream);
            Ok(Step::Request { sid, meta })
        } else {
            self.streams.insert(sid, Stream::Socket);
            Ok(Step::Socket { sid, meta })
        }
    }

    /// The body of a request that is whole, for whoever serves it. It still counts as held
    /// until the stream is forgotten: a request's memory is its own until its work is done.
    pub fn body(&mut self, sid: u32) -> Vec<u8> {
        match self.streams.get_mut(&sid) {
            Some(Stream::Request { body, .. }) => std::mem::take(body),
            _ => Vec::new(),
        }
    }

    /// What of a request's body has come so far.
    pub fn body_so_far(&self, sid: u32) -> &[u8] {
        match self.streams.get(&sid) {
            Some(Stream::Request { body, .. }) => body,
            _ => &[],
        }
    }

    /// A stream's work is done, or it was given up: it is no stream of this session any more.
    pub fn forget(&mut self, sid: u32) {
        if let Some(Stream::Request { held, .. }) = self.streams.remove(&sid) {
            self.held -= held;
        }
    }

    /// A phone says more on a WebSocket than is taken: the stream is given up, and the relay
    /// is told.
    pub fn heard_too_much(&mut self, sid: u32) -> Vec<u8> {
        self.forget(sid);
        frame(ABORT, sid, &[])
    }

    /// How much of requests' bodies is held.
    pub fn held(&self) -> usize {
        self.held
    }

    /// How many requests and how many WebSockets the session has.
    pub fn counted(&self) -> (usize, usize) {
        let streams = self.streams.values();
        let requests = streams.filter(|stream| matches!(stream, Stream::Request { .. }));
        let requests = requests.count();
        (requests, self.streams.len() - requests)
    }

    /// The streams as they are: for each, whether it is a request, whether its end has come,
    /// and how much of its body is held.
    pub fn shown(&self) -> Vec<(u32, bool, bool, usize)> {
        let streams = self.streams.iter();
        let shown = streams.map(|(sid, stream)| match stream {
            Stream::Request { held, ended, .. } => (*sid, true, *ended, *held),
            Stream::Socket => (*sid, false, false, 0),
        });
        shown.collect()
    }
}

// ---- the relay's settings --------------------------------------------------------------------

/// Which relay, under which name, with which key. The key is never shown.
#[derive(Clone, PartialEq, Eq)]
pub struct Settings {
    /// The relay's address, as `https://host[:port]`.
    pub url: String,
    /// The Host's name at the relay.
    pub host_id: String,
    key: String,
}

impl std::fmt::Debug for Settings {
    fn fmt(&self, out: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let Self { url, host_id, .. } = self;
        write!(out, "RelaySettings(url='{url}', host_id='{host_id}')")
    }
}

const NO_ORIGIN: &str = "relay URL must be an HTTPS origin without credentials, path or query";
const NO_FILE: &str = "invalid relay credential file";
/// Why a file of settings that others may read is not taken.
pub const NOT_PRIVATE: &str = "relay credential file must be private (chmod 600)";

/// Whether a name is one a relay knows a Host by: eight to sixty-four small letters,
/// digits and hyphens.
fn is_host_id(name: &str) -> bool {
    let allowed = |sign: u8| sign.is_ascii_lowercase() || sign.is_ascii_digit() || sign == b'-';
    (8..=64).contains(&name.len()) && name.bytes().all(allowed)
}

impl Settings {
    /// Settings, when they name a relay. `Err`: why not, in the Python Host's words.
    pub fn new(url: &str, host_id: &str, key: &str) -> Result<Self, String> {
        let parsed: Url = trust::urlsplit(url)?;
        let secure = matches!(parsed.scheme.as_str(), "https" | "wss");
        let whole = parsed.query.is_empty()
            && parsed.fragment.is_empty()
            && matches!(parsed.path.as_str(), "" | "/");
        if !secure || parsed.hostname().is_none() || parsed.names_somebody() || !whole {
            return Err(NO_ORIGIN.to_string());
        }
        if parsed.port().is_err() {
            return Err("invalid relay port".to_string());
        }
        if !is_host_id(host_id) {
            return Err("invalid relay Host ID".to_string());
        }
        let hex = |sign: u8| sign.is_ascii_digit() || (b'a'..=b'f').contains(&sign);
        if key.len() != 64 || !key.bytes().all(hex) {
            return Err("invalid relay connector key".to_string());
        }
        Ok(Self {
            url: format!("https://{}", parsed.netloc),
            host_id: host_id.to_string(),
            key: key.to_string(),
        })
    }

    /// Settings from the file they are kept in (`relay.json`), by its content and by who may
    /// read it. `Err`: why not, in the Python Host's words.
    pub fn read(text: &[u8], mode: u32) -> Result<Self, String> {
        if mode & 0o077 != 0 {
            return Err(NOT_PRIVATE.to_string());
        }
        let said: Value = serde_json::from_slice(text).map_err(|_| NO_FILE.to_string())?;
        let field = |name: &str| said.as_object().and_then(|said| said.get(name));
        let (Some(url), Some(host_id), Some(key)) = (field("url"), field("host_id"), field("key"))
        else {
            return Err(NO_FILE.to_string());
        };
        let Some(url) = url.as_str() else {
            return Err(NO_FILE.to_string());
        };
        // What is wrong is said in the order the fields are read: the address, the name, the
        // key. A field that is no text is no file of settings.
        let (any_name, any_key) = ("a".repeat(8), "a".repeat(64));
        Self::new(url, &any_name, &any_key)?;
        let Some(host_id) = host_id.as_str() else {
            return Err(NO_FILE.to_string());
        };
        Self::new(url, host_id, &any_key)?;
        let Some(key) = key.as_str() else {
            return Err(NO_FILE.to_string());
        };
        Self::new(url, host_id, key)
    }

    /// The address a phone is paired with, and saves.
    pub fn pairing_url(&self) -> String {
        format!("{}/h/{}", self.url, self.host_id)
    }

    /// The relay's address for a WebSocket.
    pub fn websocket_url(&self) -> String {
        self.url.replacen("https:", "wss:", 1)
    }

    /// The address the connector dials. It carries the key: it is never logged or shown.
    pub fn connect_url(&self) -> String {
        self.connect_url_at(&self.websocket_url())
    }

    /// The same at another address: a stand-in for the relay.
    pub fn connect_url_at(&self, relay: &str) -> String {
        let Self { host_id, key, .. } = self;
        let relay = relay.trim_end_matches('/');
        format!("{relay}/connect?host={host_id}&key={key}")
    }

    /// What anybody who may ask is told of the relay.
    pub fn status(&self) -> Value {
        json!({
            "configured": true,
            "url": self.url,
            "host_id": self.host_id,
            "pairing_url": self.pairing_url(),
        })
    }
}
