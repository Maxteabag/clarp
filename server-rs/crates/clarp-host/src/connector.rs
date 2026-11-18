//! The connection relay's connector: the one socket a phone's requests come down.
//!
//! A Host without a public address keeps one outbound WebSocket to a relay, and whatever a
//! phone asks at the relay comes down that socket as frames. The rules of the frames are in
//! [`clarp_kernel::connector`]; here is the socket, and what serves a stream.
//!
//! A stream is served by the Host's own router, asked from inside the Host: the same routes,
//! the same checks, the same answers as for a request that came through the door. Such a
//! request has no socket of its own, so nothing about it says "this machine": the
//! administrator's token and the token of an agent's tools, which are taken from this machine
//! only, are never taken from the relay. A phone presents the token of its pairing, as it
//! does from anywhere.
//!
//! The Host is told of its relay by a file it is given by name (`--relay-config`): a file of
//! the Python Host's kind, with `[network] relay_enabled` in it and `relay.json` beside it.
//! Both Hosts read the same two files the same way, so one that takes the other's place
//! keeps its name at the relay, and the phones their pairing. A Host that is told of no file
//! dials nobody. Two Hosts must never dial with one name: the relay gives a name's phones to
//! whoever dialled last.

use std::collections::HashMap;
use std::os::unix::fs::PermissionsExt;
use std::panic::AssertUnwindSafe;
use std::sync::{Arc, Mutex};
use std::time::Duration;

use axum::body::Body;
use axum::extract::State;
use axum::http::{HeaderName, HeaderValue, Method, Request, StatusCode, Uri};
use axum::response::Response;
use axum::routing::get;
use axum::{Json, Router};
use clarp_kernel::connector::{self as rules, Limits, Meta, Settings, Step, Table};
use futures_util::{FutureExt, SinkExt, StreamExt};
use serde_json::{Map, Value, json};
use tokio::sync::{mpsc, oneshot};
use tokio::time::Instant;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::{CloseFrame, WebSocketConfig};
use tokio_tungstenite::tungstenite::{Error as SocketError, Message};
use tower::ServiceExt;

use crate::config::Config;
use crate::edge::App;
use crate::line::{Carried, Carrier, Heard, Hearer, Line, NotTaken, Told};

type Socket =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;
type Sink = futures_util::stream::SplitSink<Socket, Message>;

/// How long the relay's socket is given to take a frame. A socket that takes nothing for so
/// long is lost, whatever it says.
const TAKEN_WITHIN: Duration = Duration::from_secs(rules::PONG_WITHIN_S);
/// How many frames may wait for the socket: a stream that answers faster than the relay
/// takes waits here.
const WAITING_MOST: usize = 64;

/// What a Host knows of its relay, and how the connection stands.
pub struct Connector {
    /// Whether the owner's settings turn the relay on.
    enabled: bool,
    /// The relay, when the settings name one.
    settings: Option<Settings>,
    /// Why the relay is not used though it is turned on.
    invalid: Option<String>,
    /// A stand-in for the relay on this machine, by its address. Empty: the relay itself.
    stand_in: String,
    state: Mutex<&'static str>,
    /// What was known of the last session that was lost, or of the last dial that failed.
    last_loss: Mutex<Option<Value>>,
}

impl Default for Connector {
    fn default() -> Self {
        Self {
            enabled: false,
            settings: None,
            invalid: None,
            stand_in: String::new(),
            state: Mutex::new("starting"),
            last_loss: Mutex::new(None),
        }
    }
}

/// Whether the owner's file turns the relay on (`[network] relay_enabled`).
fn turned_on(file: &str) -> bool {
    let Ok(file) = file.parse::<toml::Table>() else {
        return false;
    };
    let network = file.get("network").and_then(toml::Value::as_table);
    let switch = network.and_then(|network| network.get("relay_enabled"));
    switch.and_then(toml::Value::as_bool).unwrap_or(false)
}

impl Connector {
    /// What this Host is told of its relay as it starts. Nothing is dialled here.
    pub fn told(config: &Config) -> Self {
        let Some(file) = &config.relay_config else {
            return Self::default();
        };
        let enabled = match std::fs::read_to_string(file) {
            Ok(text) => turned_on(&text),
            Err(error) => {
                let file = file.display();
                eprintln!("clarp-host: the relay's settings ({file}) cannot be read: {error}");
                false
            }
        };
        if !enabled {
            return Self::default();
        }
        // The relay's name for this Host and its key are beside the owner's file.
        let credential = file.with_file_name("relay.json");
        let read = std::fs::metadata(&credential).and_then(|about| {
            let text = std::fs::read(&credential)?;
            Ok((text, about.permissions().mode()))
        });
        let settings = match read {
            Ok((text, mode)) => Settings::read(&text, mode),
            Err(_) => Err("the relay credential file cannot be read".to_string()),
        };
        let (settings, mut invalid) = match settings {
            Ok(settings) => (Some(settings), None),
            Err(why) => (None, Some(why)),
        };
        // A Host anybody may speak to is not put where anybody can reach it.
        if invalid.is_none() && config.auth_token.is_empty() {
            invalid = Some("relay networking requires authentication".to_string());
        }
        Self {
            enabled,
            settings,
            invalid,
            stand_in: config.relay_stand_in.clone(),
            ..Self::default()
        }
    }

    /// The relay this Host uses, when it uses one.
    fn used(&self) -> Option<&Settings> {
        let usable = self.enabled && self.invalid.is_none();
        self.settings.as_ref().filter(|_| usable)
    }

    /// The address a phone is paired with through the relay, when the relay is used.
    pub fn pairing_url(&self) -> Option<String> {
        self.used().map(Settings::pairing_url)
    }

    /// What an app is told of the relay (`GET /network/relay`).
    pub fn status(&self) -> Value {
        if !self.enabled {
            return json!({ "enabled": false, "state": "disabled" });
        }
        let Some(settings) = self.used() else {
            return json!({
                "configured": self.settings.is_some(),
                "enabled": true,
                "state": "invalid",
                "error": self.invalid,
            });
        };
        let mut status = settings.status();
        status["enabled"] = json!(true);
        status["state"] = json!(*lock(&self.state));
        if let Some(loss) = lock(&self.last_loss).clone() {
            status["last_loss"] = loss;
        }
        status
    }

    fn is(&self, state: &'static str) {
        *lock(&self.state) = state;
    }
}

fn lock<T>(held: &Mutex<T>) -> std::sync::MutexGuard<'_, T> {
    held.lock()
        .unwrap_or_else(std::sync::PoisonError::into_inner)
}

// ---- what is known of a session that ended ---------------------------------------------------

/// Why a session ended or a dial failed, in what is safe to write down: never the text of an
/// error, which may carry the address that was dialled, and with it the key.
#[derive(Debug, Clone, Default, PartialEq)]
struct Lost {
    /// What the Python connector's telemetry calls the same failure.
    kind: &'static str,
    /// Whether a socket that was up was closed, by either side or by neither.
    closed: bool,
    received: Option<(u16, String)>,
    sent: Option<(u16, String)>,
    http_status: Option<u16>,
    errno: Option<i32>,
}

impl Lost {
    /// A socket that was up is no more.
    fn closed(received: Option<(u16, String)>, sent: Option<(u16, String)>) -> Self {
        // A close the relay sent is answered in kind before the socket goes.
        let sent = sent.or_else(|| received.as_ref().map(|(code, _)| (*code, String::new())));
        let clean = matches!(received, Some((1000 | 1001, _)));
        Self {
            kind: if clean {
                "ConnectionClosedOK"
            } else {
                "ConnectionClosedError"
            },
            closed: true,
            received,
            sent,
            ..Self::default()
        }
    }

    fn of_kind(kind: &'static str) -> Self {
        Self {
            kind,
            ..Self::default()
        }
    }

    /// A dial that failed.
    fn dial(error: &SocketError) -> Self {
        match error {
            SocketError::Http(answer) => Self {
                http_status: Some(answer.status().as_u16()),
                ..Self::of_kind("InvalidStatus")
            },
            SocketError::Io(error) => {
                use std::io::ErrorKind;
                let kind = match (error.kind(), error.raw_os_error()) {
                    (ErrorKind::ConnectionRefused, _) => "ConnectionRefusedError",
                    (ErrorKind::ConnectionReset, _) => "ConnectionResetError",
                    (ErrorKind::ConnectionAborted, _) => "ConnectionAbortedError",
                    (ErrorKind::TimedOut, _) => "TimeoutError",
                    // What TLS refuses comes as data that cannot be read.
                    (ErrorKind::InvalidData | ErrorKind::UnexpectedEof, None) => "SSLError",
                    // And a name nobody answers for comes with no number of the system's.
                    (_, None) => "gaierror",
                    _ => "OSError",
                };
                Self {
                    errno: error.raw_os_error(),
                    ..Self::of_kind(kind)
                }
            }
            SocketError::Tls(_) => Self::of_kind("SSLError"),
            SocketError::Url(_) => Self::of_kind("InvalidURI"),
            _ => Self::of_kind("InvalidHandshake"),
        }
    }

    /// The close the relay sent, as the rule of dialling again reads it.
    fn received(&self) -> Option<(u16, &str)> {
        let received = self.received.as_ref();
        received.map(|(code, reason)| (*code, reason.as_str()))
    }

    /// The fields of the event, as the Python connector writes them.
    fn detail(&self) -> Map<String, Value> {
        let mut detail = Map::new();
        detail.insert("error_type".to_string(), json!(self.kind));
        if self.closed {
            let code = |close: Option<&(u16, String)>| close.map(|close| close.0);
            let reason = |close: Option<&(u16, String)>| close.map(|close| close.1.clone());
            let (received, sent) = (self.received.as_ref(), self.sent.as_ref());
            let shown = reason(received).unwrap_or_default();
            detail.insert("code".into(), json!(code(received).unwrap_or(1006)));
            detail.insert("reason".into(), json!(shown));
            detail.insert("rcvd_code".into(), json!(code(received)));
            detail.insert("rcvd_reason".into(), json!(reason(received)));
            detail.insert("sent_code".into(), json!(code(sent)));
            detail.insert("sent_reason".into(), json!(reason(sent)));
            let both = received.is_some() && sent.is_some();
            detail.insert("rcvd_then_sent".into(), json!(both.then_some(true)));
        } else if let Some(status) = self.http_status {
            detail.insert("http_status".into(), json!(status));
        } else if let Some(errno) = self.errno {
            detail.insert("errno".into(), json!(errno));
        }
        detail
    }
}

/// A number the journal shows as Python shows one that may be missing.
fn or_none(number: Option<u16>) -> String {
    number.map_or_else(|| "None".to_string(), |number| number.to_string())
}

impl Connector {
    /// A session is up: said in the journal, with how long the Host had none.
    fn opened(&self, session: u64, downtime_ms: u128) {
        let detail = json!({ "session": session, "downtime_ms": downtime_ms });
        eprintln!(
            "clarp-host: relay connected (session {session}, after {downtime_ms} ms) \
             relayConnectionOpened {detail}"
        );
    }

    /// A session was lost, or a dial failed: said in the journal with what is known of it,
    /// and kept for whoever asks how the relay stands.
    fn lost(&self, lost: &Lost, session: Option<(u64, f64)>, (requests, sockets): (usize, usize)) {
        let mut detail = lost.detail();
        let age = session.map(|(_, age)| (age * 10.0).round() / 10.0);
        detail.insert("session".into(), json!(session.map(|(number, _)| number)));
        detail.insert("session_age_s".into(), json!(age));
        detail.insert("streams_torn_down".into(), json!(requests + sockets));
        detail.insert("http_streams".into(), json!(requests));
        detail.insert("ws_streams".into(), json!(sockets));
        let detail = Value::Object(detail);
        let kind = lost.kind;
        match age {
            Some(age) => {
                let received = lost.received();
                let code = or_none(lost.closed.then(|| received.map_or(1006, |close| close.0)));
                let reason = received.map_or("", |close| close.1);
                let sent = or_none(lost.sent.as_ref().map(|close| close.0));
                let streams = requests + sockets;
                eprintln!(
                    "clarp-host: relay connection lost ({kind} code={code} reason='{reason}' \
                     sent={sent} age={age:.1}s streams={streams}) relayConnectionLost {detail}"
                );
            }
            None => {
                let more = match (lost.http_status, lost.errno) {
                    (Some(status), _) => format!(" http={status}"),
                    (None, Some(errno)) => format!(" errno={errno}"),
                    (None, None) => String::new(),
                };
                eprintln!(
                    "clarp-host: relay connect failed ({kind}{more}) relayConnectionLost {detail}"
                );
            }
        }
        *lock(&self.last_loss) = Some(detail);
    }
}

// ---- the socket ------------------------------------------------------------------------------

/// How the connector trusts the relay to be the relay: by the authorities the web is trusted
/// by, with nothing of this machine's own added and nothing left to a default.
fn trust_of_the_web() -> Option<tokio_tungstenite::Connector> {
    let mut roots = rustls::RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let config = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .ok()?
        .with_root_certificates(roots)
        .with_no_client_auth();
    Some(tokio_tungstenite::Connector::Rustls(Arc::new(config)))
}

/// Starts the connector, where the Host uses a relay: it dials, serves what comes down the
/// socket by the Host's own router, and dials again by itself whenever the socket is lost.
pub fn run(app: &App, router: Router) {
    let connector = app.connector.clone();
    let Some(settings) = connector.used().cloned() else {
        if let Some(why) = &connector.invalid {
            eprintln!("clarp-host: the relay is turned on and not used: {why}");
        }
        return;
    };
    let Some(trust) = trust_of_the_web() else {
        eprintln!("clarp-host: the relay is not used: TLS cannot be set up");
        return;
    };
    // The address that is dialled carries the key: it is said nowhere.
    let address = if connector.stand_in.is_empty() {
        settings.connect_url()
    } else {
        settings.connect_url_at(&connector.stand_in)
    };
    // A request that came down the socket is handed to the Host as one for its own door.
    let local = format!("127.0.0.1:{}", app.config.listen.port());
    eprintln!(
        "clarp-host: the relay is {} and this Host is {} there",
        settings.url, settings.host_id
    );
    tokio::spawn(async move {
        let mut waited: Option<f64> = None;
        let mut without_since = Instant::now();
        let mut session = 0u64;
        loop {
            connector.is("connecting");
            let limits = WebSocketConfig::default()
                .max_message_size(Some(rules::MESSAGE_MOST))
                .max_frame_size(Some(rules::MESSAGE_MOST));
            let dial = tokio_tungstenite::connect_async_tls_with_config(
                address.as_str(),
                Some(limits),
                true,
                Some(trust.clone()),
            );
            let within = Duration::from_secs(rules::DIAL_WITHIN_S);
            let (lost, up, torn) = match tokio::time::timeout(within, dial).await {
                Ok(Ok((socket, _))) => {
                    session += 1;
                    let up_since = Instant::now();
                    connector.is("connected");
                    connector.opened(session, (up_since - without_since).as_millis());
                    let (lost, torn) = serve(&router, socket, &local).await;
                    let age = up_since.elapsed().as_secs_f64();
                    (lost, Some((session, age)), torn)
                }
                Ok(Err(error)) => (Lost::dial(&error), None, (0, 0)),
                Err(_) => (Lost::of_kind("TimeoutError"), None, (0, 0)),
            };
            without_since = Instant::now();
            connector.lost(&lost, up, torn);
            let wait = rules::redial_after(waited, up.is_some(), lost.received());
            waited = Some(wait);
            connector.is("reconnecting");
            tokio::time::sleep(Duration::from_secs_f64(wait)).await;
        }
    });
}

/// What a stream's work says to the session.
enum Said {
    /// A frame for the relay.
    Frame {
        sid: u32,
        serial: u64,
        frame: Vec<u8>,
    },
    /// The work is done.
    Done { sid: u32, serial: u64 },
}

/// A stream that is being served.
struct Serving {
    /// Which of the streams that ever had this number it is: what an earlier one still had
    /// to say is not said for a later one.
    serial: u64,
    work: tokio::task::AbortHandle,
    /// Where a request's body goes once it is whole.
    body: Option<oneshot::Sender<Vec<u8>>>,
    /// Where what a phone says on a socket goes.
    heard: Option<Hearer>,
}

/// Hands the relay a message. `false`: the socket did not take it, and is lost.
async fn put(sink: &mut Sink, message: Message) -> bool {
    let taken = tokio::time::timeout(TAKEN_WITHIN, sink.send(message)).await;
    matches!(taken, Ok(Ok(())))
}

async fn tell(sink: &mut Sink, frame: Vec<u8>) -> bool {
    put(sink, Message::Binary(frame.into())).await
}

/// One session with the relay, from the socket's first frame to its last. Answers why it
/// ended, and how many requests and WebSockets ended with it.
async fn serve(router: &Router, socket: Socket, local: &str) -> (Lost, (usize, usize)) {
    let (mut sink, mut source) = socket.split();
    let (say, mut saying) = mpsc::channel::<Said>(WAITING_MOST);
    let mut table = Table::new(Limits::default());
    let mut serving: HashMap<u32, Serving> = HashMap::new();
    let mut serial = 0u64;
    let mut received: Option<(u16, String)> = None;
    let every = Duration::from_secs(rules::PING_EVERY_S);
    let mut pings = tokio::time::interval_at(Instant::now() + every, every);
    pings.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut pong_by: Option<Instant> = None;
    let lost = loop {
        let late = pong_by;
        let pong_is_late = async move {
            match late {
                Some(by) => tokio::time::sleep_until(by).await,
                None => std::future::pending().await,
            }
        };
        tokio::select! {
            message = source.next() => {
                let data = match message {
                    Some(Ok(Message::Binary(data))) => data,
                    Some(Ok(Message::Pong(_))) => {
                        pong_by = None;
                        continue;
                    }
                    Some(Ok(Message::Close(close))) => {
                        // The socket says why it goes; it is gone with the next reading.
                        received = Some(match close {
                            Some(close) => {
                                let reason = close.reason.as_str().chars().take(120).collect();
                                (u16::from(close.code), reason)
                            }
                            None => (1005, String::new()),
                        });
                        continue;
                    }
                    // The relay's word that it is there, and what nobody reads.
                    Some(Ok(_)) => continue,
                    Some(Err(_)) | None => break Lost::closed(received.take(), None),
                };
                let step = match table.take(&data) {
                    Ok(step) => step,
                    Err(violation) => {
                        // What is no frame of the protocol ends the session.
                        let _ = put(&mut sink, Message::Close(None)).await;
                        break Lost::of_kind(violation.class);
                    }
                };
                let told = match step {
                    Step::Nothing | Step::Body { .. } => true,
                    Step::Heard { sid, said } => {
                        let said = match said {
                            rules::Said::Text(text) => Heard::Text(text.to_string()),
                            rules::Said::Bytes(bytes) => Heard::Bytes(bytes.to_vec()),
                        };
                        let line = serving.get(&sid).and_then(|stream| stream.heard.as_ref());
                        match line.map(|line| line.said(said)) {
                            // A phone that says more than its route takes is let go: the
                            // relay's one socket waits for no stream.
                            Some(Err(NotTaken::TooMuch)) => {
                                if let Some(stream) = serving.remove(&sid) {
                                    stream.work.abort();
                                }
                                tell(&mut sink, table.heard_too_much(sid)).await
                            }
                            // Said to a route that is done: its close is on its way.
                            _ => true,
                        }
                    }
                    Step::Request { sid, meta } => {
                        serial += 1;
                        let (body, whole) = oneshot::channel();
                        let work = request(
                            router.clone(),
                            say.clone(),
                            (sid, serial),
                            meta,
                            whole,
                            local.to_string(),
                        );
                        let work = tokio::spawn(work).abort_handle();
                        let stream = Serving {
                            serial,
                            work,
                            body: Some(body),
                            heard: None,
                        };
                        serving.insert(sid, stream);
                        true
                    }
                    Step::Whole { sid } => {
                        let whole = table.body(sid);
                        let waiting = serving.get_mut(&sid).and_then(|stream| stream.body.take());
                        if let Some(waiting) = waiting {
                            let _ = waiting.send(whole);
                        }
                        true
                    }
                    // A phone asks for a socket: the Host's own router is asked for it, with
                    // the line its route will speak on, and the line is carried from then on.
                    Step::Socket { sid, meta } => {
                        serial += 1;
                        let (line, carrier) = Line::new();
                        let heard = carrier.heard.clone();
                        let work = conversation(
                            router.clone(),
                            say.clone(),
                            (sid, serial),
                            meta,
                            (line, carrier),
                            local.to_string(),
                        );
                        let work = tokio::spawn(work).abort_handle();
                        let stream = Serving {
                            serial,
                            work,
                            body: None,
                            heard: Some(heard),
                        };
                        serving.insert(sid, stream);
                        true
                    }
                    Step::Over { sid, tell: last } => {
                        if let Some(stream) = serving.remove(&sid) {
                            stream.work.abort();
                        }
                        match last {
                            Some(frame) => tell(&mut sink, frame).await,
                            None => true,
                        }
                    }
                    Step::Tell(frame) => tell(&mut sink, frame).await,
                };
                if !told {
                    break Lost::closed(received.take(), None);
                }
            }
            Some(said) = saying.recv() => match said {
                Said::Frame { sid, serial, frame } => {
                    // What a stream said before it was given up is not said after.
                    let served = serving.get(&sid).is_some_and(|stream| stream.serial == serial);
                    if served && !tell(&mut sink, frame).await {
                        break Lost::closed(received.take(), None);
                    }
                }
                Said::Done { sid, serial } => {
                    if serving.get(&sid).is_some_and(|stream| stream.serial == serial) {
                        serving.remove(&sid);
                        table.forget(sid);
                    }
                }
            },
            _ = pings.tick() => {
                // The relay is asked whether it is there in the protocol's way, which its
                // edge answers, and in the relay's own, which keeps its end awake.
                let asked = put(&mut sink, Message::Ping(Vec::new().into())).await
                    && put(&mut sink, Message::Text("ping".into())).await;
                if !asked {
                    break Lost::closed(received.take(), None);
                }
                pong_by = pong_by.or(Some(Instant::now() + TAKEN_WITHIN));
            }
            () = pong_is_late => {
                let why = (1011, "keepalive ping timeout".to_string());
                let close = CloseFrame {
                    code: CloseCode::from(why.0),
                    reason: why.1.as_str().into(),
                };
                let _ = put(&mut sink, Message::Close(Some(close))).await;
                break Lost::closed(received.take(), Some(why));
            }
        }
    };
    // Every stream of the session ends with it.
    let torn = table.counted();
    for stream in serving.into_values() {
        stream.work.abort();
    }
    (lost, torn)
}

// ---- a request, served by the Host's own router ----------------------------------------------

/// A header's value as the text Python reads it as: every byte one sign.
fn text_of(value: &HeaderValue) -> String {
    value
        .as_bytes()
        .iter()
        .map(|byte| char::from(*byte))
        .collect()
}

/// The answer of the Host's own router to what a frame asked. `None`: it is no request the
/// Host can be asked.
async fn answer(router: Router, meta: &Meta, local: &str, body: Vec<u8>) -> Option<Response> {
    asked(router, meta, local, body, None).await
}

/// The same, for a request that may come with the line of a socket.
async fn asked(
    router: Router,
    meta: &Meta,
    local: &str,
    body: Vec<u8>,
    line: Option<Carried>,
) -> Option<Response> {
    let method = Method::from_bytes(meta.method.as_str()?.as_bytes()).ok()?;
    let uri: Uri = meta.path.parse().ok()?;
    let handed = rules::handed_on(&meta.headers, local, body.len(), meta.passthrough)?;
    let mut request = Request::new(Body::from(body));
    if let Some(line) = line {
        request.extensions_mut().insert(line);
    }
    *request.method_mut() = method;
    *request.uri_mut() = uri;
    for (name, value) in handed {
        let name = HeaderName::from_bytes(name.as_bytes()).ok()?;
        let bytes: Option<Vec<u8>> = value
            .chars()
            .map(|sign| u8::try_from(u32::from(sign)).ok())
            .collect();
        let value = HeaderValue::from_bytes(&bytes?).ok()?;
        request.headers_mut().append(name, value);
    }
    // No socket's address goes with the request: it came from elsewhere, whoever sent it.
    router.oneshot(request).await.ok()
}

/// The work of one request: waits for its body, asks the Host, and says the answer to the
/// relay as it comes, so that an answer without an end (a stream of events) flows.
async fn request(
    router: Router,
    say: mpsc::Sender<Said>,
    (sid, serial): (u32, u64),
    meta: Meta,
    whole: oneshot::Receiver<Vec<u8>>,
    local: String,
) {
    let frame = |frame: Vec<u8>| Said::Frame { sid, serial, frame };
    let work = async {
        let within = Duration::from_secs(rules::REQUEST_WHOLE_WITHIN_S);
        let answered = match tokio::time::timeout(within, whole).await {
            Ok(Ok(body)) => answer(router, &meta, &local, body).await,
            _ => None,
        };
        let Some(answered) = answered else {
            let head = json!({ "status": 502, "headers": { "content-type": "application/json" } });
            let failed = [
                rules::json_frame(rules::RES_HEAD, sid, &head),
                rules::frame(rules::RES_BODY, sid, rules::REQUEST_FAILED.as_bytes()),
                rules::frame(rules::RES_END, sid, &[]),
            ];
            for part in failed {
                if say.send(frame(part)).await.is_err() {
                    return;
                }
            }
            return;
        };
        let (parts, body) = answered.into_parts();
        let headers = parts.headers.iter();
        let headers: Vec<(String, String)> = headers
            .map(|(name, value)| (name.as_str().to_string(), text_of(value)))
            .collect();
        let head = rules::head(parts.status.as_u16(), &headers);
        let head = rules::json_frame(rules::RES_HEAD, sid, &head);
        if say.send(frame(head)).await.is_err() {
            return;
        }
        let mut body = body.into_data_stream();
        while let Some(part) = body.next().await {
            let Ok(part) = part else {
                // The head is said: an answer that breaks off is given up, not ended.
                let _ = say.send(frame(rules::frame(rules::ABORT, sid, &[]))).await;
                return;
            };
            for piece in part.chunks(rules::CHUNK) {
                let piece = rules::frame(rules::RES_BODY, sid, piece);
                if say.send(frame(piece)).await.is_err() {
                    return;
                }
            }
        }
        let _ = say
            .send(frame(rules::frame(rules::RES_END, sid, &[])))
            .await;
    };
    // A handler that fails altogether fails its own stream, and no other.
    if AssertUnwindSafe(work).catch_unwind().await.is_err() {
        let _ = say.send(frame(rules::frame(rules::ABORT, sid, &[]))).await;
    }
    let _ = say.send(Said::Done { sid, serial }).await;
}

// ---- a socket, served by the Host's own router -----------------------------------------------

/// The work of one socket: asks the Host for it, and, where the Host opened it, carries
/// what its route tells to the relay until the route is done. A socket the Host does not
/// open is closed in the words for one that failed, whatever the Host answered instead.
/// What the phone says is handed to the line by the session itself, and a phone that goes
/// ends this work: the line's other end then hears that nobody is there.
async fn conversation(
    router: Router,
    say: mpsc::Sender<Said>,
    (sid, serial): (u32, u64),
    meta: Meta,
    (line, mut carrier): (Line, Carrier),
    local: String,
) {
    let frame = |frame: Vec<u8>| Said::Frame { sid, serial, frame };
    let work = async {
        let line = Some(Carried::new(line));
        let answered = asked(router, &meta, &local, Vec::new(), line).await;
        let opened =
            answered.is_some_and(|answer| answer.status() == StatusCode::SWITCHING_PROTOCOLS);
        if !opened {
            let failed = rules::closing(sid, rules::SOCKET_FAILED);
            let _ = say.send(frame(failed)).await;
            return;
        }
        let accepted = rules::json_frame(rules::WS_ACCEPT, sid, &json!({ "ok": true }));
        if say.send(frame(accepted)).await.is_err() {
            return;
        }
        // What the route tells, each thing with its kind before it: one for a text, nought
        // for bytes. The route's close, or its end, closes the phone's socket with the
        // route's code and the connector's own word.
        let mut code = crate::line::ENDED_WELL;
        while let Some(told) = carrier.told.recv().await {
            let (kind, said) = match told {
                Told::Text(text) => (1u8, text.into_bytes()),
                Told::Bytes(bytes) => (0u8, bytes),
                Told::Close(with, _) => {
                    code = with;
                    break;
                }
            };
            let mut message = Vec::with_capacity(1 + said.len());
            message.push(kind);
            message.extend_from_slice(&said);
            let message = rules::frame(rules::WS_MSG, sid, &message);
            if say.send(frame(message)).await.is_err() {
                return;
            }
        }
        let closed = rules::closing(sid, (code, rules::SOCKET_CLOSED));
        let _ = say.send(frame(closed)).await;
    };
    // A route that fails altogether fails its own stream, and no other.
    if AssertUnwindSafe(work).catch_unwind().await.is_err() {
        let failed = rules::closing(sid, rules::SOCKET_FAILED);
        let _ = say.send(frame(failed)).await;
    }
    let _ = say.send(Said::Done { sid, serial }).await;
}

// ---- what the apps ask of the relay ----------------------------------------------------------

/// `GET /network/relay`: whether the relay is used, how the connection stands, and the
/// address a phone is paired with through it.
async fn status(State(app): State<App>) -> Json<Value> {
    Json(app.connector.status())
}

pub fn routes() -> Router<App> {
    Router::new().route("/network/relay", get(status))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use std::sync::atomic::{AtomicUsize, Ordering};

    use super::*;
    use crate::line::testing;

    /// The work of one socket of the relay's, asked of the route of the tests: what it
    /// says to the session, where the phone's words go in, and the work itself.
    fn a_conversation(
        served: &Arc<AtomicUsize>,
        path: &str,
    ) -> (mpsc::Receiver<Said>, Hearer, tokio::task::JoinHandle<()>) {
        async fn broken() -> Response {
            panic!("a route that fails altogether")
        }
        let router = Router::new()
            .route("/echo", testing::echo(served.clone()))
            .route("/broken", get(broken));
        let (say, said) = mpsc::channel::<Said>(WAITING_MOST);
        let meta = Meta {
            path: path.to_string(),
            headers: Map::new(),
            method: json!("GET"),
            passthrough: false,
        };
        let (line, carrier) = Line::new();
        let heard = carrier.heard.clone();
        let local = "127.0.0.1:7690".to_string();
        let work = conversation(router, say, (5, 9), meta, (line, carrier), local);
        (said, heard, tokio::spawn(work))
    }

    /// The next frame a socket's work says: its kind and what it carries.
    async fn frame(said: &mut mpsc::Receiver<Said>) -> (u8, Vec<u8>) {
        match next(said).await {
            Some(Said::Frame { sid, serial, frame }) => {
                assert_eq!((sid, serial), (5, 9));
                let (kind, of, payload) = rules::parsed(&frame).unwrap();
                assert_eq!(of, 5);
                (kind, payload.to_vec())
            }
            Some(Said::Done { .. }) => panic!("the work is done"),
            None => panic!("the work is gone"),
        }
    }

    /// The next thing a socket's work says, when it says anything in time: a wrong rule
    /// fails a test, and does not keep it waiting.
    async fn next(said: &mut mpsc::Receiver<Said>) -> Option<Said> {
        let said = tokio::time::timeout(Duration::from_secs(10), said.recv()).await;
        said.expect("the work of a socket says something in time")
    }

    /// The work says that it is done, and nothing after.
    async fn done(said: &mut mpsc::Receiver<Said>) {
        assert!(matches!(
            next(said).await,
            Some(Said::Done { sid: 5, serial: 9 })
        ));
        assert!(next(said).await.is_none());
    }

    fn closed(code: u16, reason: &str) -> (u8, Vec<u8>) {
        let said = json!({ "code": code, "reason": reason });
        (
            rules::WS_CLOSE,
            rules::json_frame(rules::WS_CLOSE, 5, &said)[5..].to_vec(),
        )
    }

    #[tokio::test]
    async fn a_socket_of_the_relays_is_told_open_carried_and_closed_with_its_routes_code() {
        let served = Arc::new(AtomicUsize::new(0));
        let (mut said, heard, _work) = a_conversation(&served, "/echo");
        // The relay is told that the socket is open, and then what the route tells: each
        // thing with its kind before it, one for a text and nought for bytes.
        assert_eq!(
            frame(&mut said).await,
            (rules::WS_ACCEPT, br#"{"ok":true}"#.to_vec())
        );
        assert_eq!(
            frame(&mut said).await,
            (rules::WS_MSG, b"\x01ready".to_vec())
        );
        heard.said(Heard::Text("hello".into())).unwrap();
        assert_eq!(
            frame(&mut said).await,
            (rules::WS_MSG, b"\x01text: hello".to_vec())
        );
        heard.said(Heard::Bytes(vec![1, 2, 3])).unwrap();
        assert_eq!(frame(&mut said).await, (rules::WS_MSG, vec![0, 3, 2, 1]));
        assert_eq!(served.load(Ordering::SeqCst), 1);
        // The route closes: the phone's socket is closed with the route's code and the
        // connector's own word, and the work is done.
        heard.said(Heard::Text("bye".into())).unwrap();
        assert_eq!(
            frame(&mut said).await,
            closed(4001, "Host connection closed")
        );
        done(&mut said).await;
        testing::nobody_is_served(&served).await;

        // A route that is done without a word is closed as one that ended well.
        let (mut said, heard, _work) = a_conversation(&served, "/echo");
        assert_eq!(frame(&mut said).await.0, rules::WS_ACCEPT);
        assert_eq!(
            frame(&mut said).await,
            (rules::WS_MSG, b"\x01ready".to_vec())
        );
        heard.said(Heard::Text("end".into())).unwrap();
        assert_eq!(
            frame(&mut said).await,
            closed(1000, "Host connection closed")
        );
        done(&mut said).await;

        // What the Host does not open is closed as a socket that failed: a path that is
        // no route, and a route that fails altogether, which fails no other stream.
        for path in ["/nothing-here", "/broken"] {
            let (mut said, _heard, _work) = a_conversation(&served, path);
            assert_eq!(
                frame(&mut said).await,
                closed(1011, "Host WebSocket failed"),
                "{path}"
            );
            done(&mut said).await;
        }

        // The phone goes: the work is ended, and the route hears that nobody is there,
        // also when it listens to nothing.
        for word in ["hello", "deaf"] {
            let (mut said, heard, work) = a_conversation(&served, "/echo");
            assert_eq!(frame(&mut said).await.0, rules::WS_ACCEPT);
            assert_eq!(frame(&mut said).await.0, rules::WS_MSG);
            heard.said(Heard::Text(word.into())).unwrap();
            assert_eq!(frame(&mut said).await.0, rules::WS_MSG);
            assert_eq!(served.load(Ordering::SeqCst), 1);
            work.abort();
            drop(heard);
            testing::nobody_is_served(&served).await;
        }
    }

    #[test]
    fn the_owners_file_turns_the_relay_on_with_one_word() {
        assert!(turned_on("[network]\nrelay_enabled = true\n"));
        assert!(!turned_on("[network]\nrelay_enabled = false\n"));
        assert!(!turned_on("[network]\nmode = \"tailscale\"\n"));
        assert!(!turned_on("[server]\nrelay_enabled = true\n"));
        assert!(!turned_on("relay_enabled = true\n"));
        assert!(!turned_on("[network]\nrelay_enabled = \"true\"\n"));
        assert!(!turned_on(""));
        assert!(!turned_on("no settings at all ["));
    }

    #[test]
    fn a_host_that_is_told_of_no_relay_says_so_and_pairs_through_none() {
        let connector = Connector::default();
        assert_eq!(
            connector.status(),
            json!({ "enabled": false, "state": "disabled" })
        );
        assert_eq!(connector.pairing_url(), None);
    }

    #[test]
    fn a_relay_that_is_turned_on_and_cannot_be_used_says_why() {
        let connector = Connector {
            enabled: true,
            invalid: Some("relay credential file must be private (chmod 600)".to_string()),
            ..Connector::default()
        };
        assert_eq!(
            connector.status(),
            json!({
                "configured": false,
                "enabled": true,
                "state": "invalid",
                "error": "relay credential file must be private (chmod 600)",
            })
        );
        assert_eq!(connector.pairing_url(), None);
    }

    #[test]
    fn a_relay_that_is_used_is_shown_as_the_python_host_shows_it() {
        let key = "ab".repeat(32);
        let settings = Settings::new("https://relay.example/", "elitebook-test", &key).unwrap();
        let connector = Connector {
            enabled: true,
            settings: Some(settings),
            ..Connector::default()
        };
        connector.is("connected");
        let status = connector.status();
        assert_eq!(
            serde_json::to_string(&status).unwrap(),
            "{\"configured\":true,\"url\":\"https://relay.example\",\"host_id\":\"elitebook-test\",\
             \"pairing_url\":\"https://relay.example/h/elitebook-test\",\"enabled\":true,\
             \"state\":\"connected\"}"
        );
        assert!(!status.to_string().contains(&key));
        assert_eq!(
            connector.pairing_url().as_deref(),
            Some("https://relay.example/h/elitebook-test")
        );
    }

    #[test]
    fn what_is_known_of_a_lost_session_is_written_as_the_python_connector_writes_it() {
        // The socket died without a word.
        let dropped = Lost::closed(None, None);
        assert_eq!(
            Value::Object(dropped.detail()),
            json!({
                "error_type": "ConnectionClosedError", "code": 1006, "reason": "",
                "rcvd_code": null, "rcvd_reason": null, "sent_code": null, "sent_reason": null,
                "rcvd_then_sent": null,
            })
        );
        assert_eq!(dropped.received(), None);
        // Another connector took this Host's name.
        let replaced = Lost::closed(Some((1012, "replaced".to_string())), None);
        assert_eq!(replaced.received(), Some(rules::REPLACED));
        assert_eq!(replaced.kind, "ConnectionClosedError");
        assert_eq!(replaced.detail()["rcvd_then_sent"], json!(true));
        assert_eq!(replaced.detail()["sent_code"], json!(1012));
        // The relay went away in order.
        let away = Lost::closed(Some((1001, String::new())), None);
        assert_eq!(away.kind, "ConnectionClosedOK");
        // The relay stopped answering, and the Host said so before it left.
        let silent = Lost::closed(None, Some((1011, "keepalive ping timeout".to_string())));
        assert_eq!(silent.detail()["code"], json!(1006));
        assert_eq!(silent.detail()["sent_code"], json!(1011));
        assert_eq!(silent.detail()["rcvd_then_sent"], json!(null));
        // A dial the relay refused, and one that never arrived.
        let refused = std::io::Error::from_raw_os_error(111);
        let refused = Lost::dial(&SocketError::Io(refused));
        assert_eq!(
            Value::Object(refused.detail()),
            json!({ "error_type": "ConnectionRefusedError", "errno": 111 })
        );
        assert_eq!(
            Value::Object(Lost::of_kind("TimeoutError").detail()),
            json!({ "error_type": "TimeoutError" })
        );
    }
}
