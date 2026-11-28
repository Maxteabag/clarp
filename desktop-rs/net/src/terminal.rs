//! An agent's terminal on its Host: `GET /terminal/<session>` upgraded to a
//! WebSocket. The Host runs the agent's CLI in a terminal of its own, on the
//! agent's conversation, and holds the agent's turns while it is open; it
//! refuses the terminal while a turn of the agent runs, with a sentence that
//! says why. So the conversation has one writer at a time.
//!
//! The frames (docs/protocol.md; the server home terminal uses the same):
//! keystrokes go to the Host as binary frames, the terminal's output comes
//! back as binary frames, and the window's size goes as a text frame
//! `{"resize":{"cols":C,"rows":R}}`.
//!
//! The upgrade request is written here rather than by the WebSocket library,
//! so a refusal's whole body is read even when the Host sends it after its
//! headers, and its sentence is shown as the Host wrote it.

use std::io::Read;
use std::sync::Arc;
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use serde_json::Value;
use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};
use tokio::net::TcpStream;
use tokio::sync::mpsc::{UnboundedReceiver, UnboundedSender, unbounded_channel};
use tokio_tungstenite::WebSocketStream;
use tokio_tungstenite::tungstenite::handshake::client::generate_key;
use tokio_tungstenite::tungstenite::handshake::derive_accept_key;
use tokio_tungstenite::tungstenite::protocol::frame::coding::CloseCode;
use tokio_tungstenite::tungstenite::protocol::{Message, Role};
use url::Url;
use clarp_core::tr;

// i18n-screen: agent terminal

/// The longest answer head or refusal read; a Host's sentence is far shorter.
const LONGEST_ANSWER: usize = 64 * 1024;
/// How long reaching the Host and its answer to the upgrade may take.
const ANSWER_TIMEOUT: Duration = Duration::from_secs(20);
/// How long the rest of a refusal's body may take after its headers.
const BODY_TIMEOUT: Duration = Duration::from_secs(5);

/// A byte stream to the Host: plain TCP, or TLS for an https Host.
pub trait Io: AsyncRead + AsyncWrite + Unpin + Send {}
impl<T: AsyncRead + AsyncWrite + Unpin + Send> Io for T {}

/// An open terminal on the Host.
pub type Terminal = WebSocketStream<Box<dyn Io>>;

/// How a terminal ended.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Ended {
    /// It closed as terminals do: the agent's CLI exited.
    Closed(String),
    /// The Host closed it with a reason: its sentence.
    Refused(String),
    /// The connection broke.
    Lost(String),
}

impl Ended {
    /// The line that says why.
    pub fn line(&self) -> &str {
        match self {
            Ended::Closed(line) | Ended::Refused(line) | Ended::Lost(line) => line,
        }
    }

    /// The window stays until the line is read: anything but a plain close.
    pub fn holds(&self) -> bool {
        !matches!(self, Ended::Closed(_))
    }
}

/// The WebSocket address of `session`'s terminal on the Host at `base`
/// (`http` becomes `ws`, `https` `wss`; a path prefix is kept).
pub fn terminal_url(base: &str, session: &str) -> Result<Url, String> {
    // i18n: agent terminal window, error: the saved Host address cannot be read; {0} is the address, {1} the technical reason
    let mut url = Url::parse(base.trim()).map_err(|error| tr!("{0} is not a Host address: {1}", base, error))?;
    let scheme = match url.scheme() {
        "http" => "ws",
        "https" => "wss",
        // i18n: agent terminal window, error: {0} is the address, {1} its scheme (http and https stay as they are)
        other => return Err(tr!("{0} is not a Host address (it is {1}, not http or https)", base, other)),
    };
    if url.host_str().is_none_or(str::is_empty) {
        // i18n: agent terminal window, error: {} is the address
        return Err(tr!("{} is not a Host address (it names no machine)", base));
    }
    // i18n: agent terminal window, error: {} is the saved Host address
    url.set_scheme(scheme).map_err(|()| tr!("{} is not a Host address", base))?;
    url.set_query(None);
    url.set_fragment(None);
    // i18n: agent terminal window, error: {} is the saved Host address
    url.path_segments_mut().map_err(|()| tr!("{} is not a Host address", base))?.pop_if_empty().push("terminal").push(session);
    Ok(url)
}

/// The text frame that tells the Host the window's size.
pub fn resize_frame(cols: u16, rows: u16) -> String {
    serde_json::json!({"resize": {"cols": cols, "rows": rows}}).to_string()
}

/// The Host's sentence in a refusal: a plain-text body as it is, or the
/// `error` (else `message`) of a JSON body, a string or an object's own
/// `message`, `error` or `detail`; any other body as text. Without one, the
/// status says it.
pub fn refusal_sentence(status: u16, reason: &str, body: &[u8]) -> String {
    let text = String::from_utf8_lossy(body);
    let text = text.trim();
    if let Ok(Value::Object(object)) = serde_json::from_str::<Value>(text) {
        let said = |value: Option<&Value>| -> Option<String> {
            match value? {
                Value::String(words) if !words.trim().is_empty() => Some(words.trim().to_owned()),
                Value::Object(inner) => ["message", "error", "detail"]
                    .iter()
                    .find_map(|key| inner.get(*key).and_then(Value::as_str).map(str::trim).filter(|w| !w.is_empty()))
                    .map(str::to_owned),
                _ => None,
            }
        };
        if let Some(words) = said(object.get("error")).or_else(|| said(object.get("message"))) {
            return words;
        }
    }
    if !text.is_empty() && !text.starts_with('<') {
        return text.chars().take(2000).collect();
    }
    let reason = reason.trim();
    if reason.is_empty() {
        // i18n: agent terminal window, error: the Host would not open the terminal; {} is its HTTP status number
        tr!("The Host refused the terminal ({}).", status)
    } else {
        // i18n: agent terminal window, error: {0} is the HTTP status number, {1} its reason phrase (English, from the Host)
        tr!("The Host refused the terminal ({0} {1}).", status, reason)
    }
}

/// An HTTP answer's head: its status, reason and headers (names lowercased).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Head {
    pub status: u16,
    pub reason: String,
    pub headers: Vec<(String, String)>,
}

impl Head {
    pub fn header(&self, name: &str) -> Option<&str> {
        self.headers.iter().find(|(key, _)| key == name).map(|(_, value)| value.as_str())
    }
}

/// Reads an answer's head (`HTTP/1.1 409 Conflict` and its headers).
pub fn parse_head(head: &[u8]) -> Result<Head, String> {
    let text = String::from_utf8_lossy(head);
    let mut lines = text.split("\r\n");
    let first = lines.next().unwrap_or_default();
    let mut parts = first.splitn(3, ' ');
    let version = parts.next().unwrap_or_default();
    let status = parts.next().and_then(|code| code.parse::<u16>().ok());
    let (true, Some(status)) = (version.starts_with("HTTP/"), status) else {
        // i18n: agent terminal window, error: {} is the start of the answer as received
        return Err(tr!("The Host's answer was not HTTP: {}", first.chars().take(80).collect::<String>()));
    };
    let reason = parts.next().unwrap_or_default().trim().to_owned();
    let headers = lines
        .filter_map(|line| line.split_once(':'))
        .map(|(name, value)| (name.trim().to_ascii_lowercase(), value.trim().to_owned()))
        .collect();
    Ok(Head { status, reason, headers })
}

/// A chunked body's data.
pub fn dechunk(mut body: &[u8]) -> Vec<u8> {
    let mut data = Vec::new();
    while let Some(end) = body.windows(2).position(|w| w == b"\r\n") {
        let size = String::from_utf8_lossy(&body[..end]);
        let Ok(size) = usize::from_str_radix(size.split(';').next().unwrap_or_default().trim(), 16) else { break };
        body = &body[end + 2..];
        if size == 0 {
            break;
        }
        let take = size.min(body.len());
        data.extend_from_slice(&body[..take]);
        body = body.get(take + 2..).unwrap_or_default();
    }
    data
}

/// Opens `session`'s terminal on the Host at `base` with the device
/// `token`. Err is a sentence to show: the Host's refusal, or why it could
/// not be reached.
pub async fn open(base: &str, session: &str, token: &str) -> Result<Terminal, String> {
    let url = terminal_url(base, session)?;
    match tokio::time::timeout(ANSWER_TIMEOUT, upgrade(&url, token)).await {
        Ok(opened) => opened,
        // i18n: agent terminal window, error: {0} is the Host's address, {1} a number of seconds
        Err(_) => Err(tr!("The Host at {0} did not answer within {1} seconds.", base, ANSWER_TIMEOUT.as_secs())),
    }
}

async fn upgrade(url: &Url, token: &str) -> Result<Terminal, String> {
    if token.contains(['\r', '\n']) {
        // i18n: agent terminal window, error: this computer's saved sign-in key for the Host is damaged
        return Err(tr!("The device token for this Host is not usable."));
    }
    let named = url.host_str().unwrap_or_default().to_owned();
    // The machine to connect to: an IPv6 address without its brackets.
    let machine = match url.host() {
        Some(url::Host::Ipv6(address)) => address.to_string(),
        _ => named.clone(),
    };
    let port = url.port_or_known_default().unwrap_or(80);
    // i18n: agent terminal window, error: {0} is the Host's machine name, {1} its port number, {2} the technical reason
    let tcp = TcpStream::connect((machine.as_str(), port)).await.map_err(|error| tr!("Could not reach the Host at {0}:{1}: {2}.", named, port, error))?;
    let _ = tcp.set_nodelay(true);
    let mut stream: Box<dyn Io> = if url.scheme() == "wss" { Box::new(secured(&machine, tcp).await?) } else { Box::new(tcp) };

    let key = generate_key();
    let host = match url.port() {
        Some(port) => format!("{named}:{port}"),
        None => named.clone(),
    };
    let mut request = format!(
        "GET {} HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nUser-Agent: {}\r\n{}: {}\r\n",
        url.path(),
        crate::USER_AGENT,
        crate::CLIENT_HEADER,
        crate::client_header(),
    );
    if !token.is_empty() {
        request.push_str(&format!("Authorization: Bearer {token}\r\n"));
    }
    request.push_str("\r\n");
    // i18n: agent terminal window, error: {} is the technical reason
    let lost = |error: std::io::Error| tr!("The connection to the Host was lost: {}.", error);
    stream.write_all(request.as_bytes()).await.map_err(lost)?;
    stream.flush().await.map_err(lost)?;

    let (head, rest) = read_head(&mut stream).await?;
    let head = parse_head(&head)?;
    if head.status == 101 {
        let accepted = head.header("sec-websocket-accept").is_some_and(|accept| accept == derive_accept_key(key.as_bytes()));
        if !accepted {
            // i18n: agent terminal window, error (WebSocket is a technical term, usually kept)
            return Err(tr!("The Host's answer to the terminal was not a WebSocket."));
        }
        return Ok(WebSocketStream::from_partially_read(stream, rest, Role::Client, None).await);
    }
    let body = read_body(&mut stream, &head, rest).await;
    Err(refusal_sentence(head.status, &head.reason, &body))
}

async fn secured(machine: &str, tcp: TcpStream) -> Result<tokio_rustls::client::TlsStream<TcpStream>, String> {
    let mut roots = rustls::RootCertStore::empty();
    roots.extend(webpki_roots::TLS_SERVER_ROOTS.iter().cloned());
    let config = rustls::ClientConfig::builder_with_provider(Arc::new(rustls::crypto::ring::default_provider()))
        .with_safe_default_protocol_versions()
        // i18n: agent terminal window, error: {} is the technical reason
        .map_err(|error| tr!("No secure connection is possible: {}.", error))?
        .with_root_certificates(roots)
        .with_no_client_auth();
    // i18n: agent terminal window, error: {0} is the machine name, {1} the technical reason
    let name = rustls::pki_types::ServerName::try_from(machine.to_owned()).map_err(|error| tr!("{0} is not a Host name: {1}.", machine, error))?;
    tokio_rustls::TlsConnector::from(Arc::new(config))
        .connect(name, tcp)
        .await
        // i18n: agent terminal window, error: {0} is the machine name, {1} the technical reason
        .map_err(|error| tr!("Could not open a secure connection to {0}: {1}.", machine, error))
}

/// The answer's head (up to its blank line) and what came after it.
async fn read_head(stream: &mut Box<dyn Io>) -> Result<(Vec<u8>, Vec<u8>), String> {
    let mut buffer = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        if let Some(end) = buffer.windows(4).position(|w| w == b"\r\n\r\n") {
            let rest = buffer.split_off(end + 4);
            buffer.truncate(end);
            return Ok((buffer, rest));
        }
        if buffer.len() > LONGEST_ANSWER {
            // i18n: agent terminal window, error
            return Err(tr!("The Host's answer to the terminal was too long."));
        }
        match stream.read(&mut chunk).await {
            // i18n: agent terminal window, error
            Ok(0) => return Err(tr!("The Host closed the connection without answering.")),
            Ok(read) => buffer.extend_from_slice(&chunk[..read]),
            // i18n: agent terminal window, error: {} is the technical reason
            Err(error) => return Err(tr!("The connection to the Host was lost: {}.", error)),
        }
    }
}

/// A refusal's body: its `Content-Length`, or up to the end of the
/// connection; what has come when the Host stops sending.
async fn read_body(stream: &mut Box<dyn Io>, head: &Head, mut body: Vec<u8>) -> Vec<u8> {
    let length = head.header("content-length").and_then(|length| length.parse::<usize>().ok());
    let chunked = head.header("transfer-encoding").is_some_and(|coding| coding.to_ascii_lowercase().contains("chunked"));
    let wanted = length.unwrap_or(LONGEST_ANSWER).min(LONGEST_ANSWER);
    let mut chunk = [0u8; 4096];
    while body.len() < wanted && !(chunked && body.windows(5).any(|w| w == b"0\r\n\r\n")) {
        match tokio::time::timeout(BODY_TIMEOUT, stream.read(&mut chunk)).await {
            Ok(Ok(read)) if read > 0 => body.extend_from_slice(&chunk[..read]),
            _ => break,
        }
    }
    if chunked {
        return dechunk(&body);
    }
    body.truncate(wanted);
    body
}

/// Reads `reader` on a thread of its own into a channel, in the pieces
/// each read returns; the channel closes at its end. A blocked read cannot
/// be called off, so its next piece still arrives on the channel: whoever
/// asks for one more key after the terminal reads it there.
pub fn read_into_channel<R: Read + Send + 'static>(mut reader: R) -> UnboundedReceiver<Vec<u8>> {
    let (sender, receiver) = unbounded_channel();
    std::thread::spawn(move || {
        let mut buffer = [0u8; 8192];
        loop {
            match reader.read(&mut buffer) {
                Ok(0) => break,
                Ok(read) => {
                    if sender.send(buffer[..read].to_vec()).is_err() {
                        break;
                    }
                }
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                Err(_) => break,
            }
        }
    });
    receiver
}

/// Carries the terminal both ways until it ends: what arrives on `input`
/// goes to the Host as keystrokes, what the Host's terminal prints goes to
/// `output`, and each size on `sizes` is told to the Host (`size`, the
/// window's size now, first). The end of `input` stops only the keystrokes.
pub async fn carry<W: AsyncWrite + Unpin>(
    terminal: Terminal,
    input: &mut UnboundedReceiver<Vec<u8>>,
    output: &mut W,
    size: (u16, u16),
    mut sizes: UnboundedReceiver<(u16, u16)>,
) -> Ended {
    let (mut to_host, mut from_host) = terminal.split();
    // i18n: agent terminal window, error: {} is the technical reason
    let lost = |error: &dyn std::fmt::Display| Ended::Lost(tr!("The connection to the Host was lost: {}.", error));
    if let Err(error) = to_host.send(Message::text(resize_frame(size.0, size.1))).await {
        return lost(&error);
    }
    let (mut typing, mut resizing) = (true, true);
    loop {
        tokio::select! {
            keys = input.recv(), if typing => match keys {
                Some(keys) => {
                    if let Err(error) = to_host.send(Message::binary(keys)).await {
                        return lost(&error);
                    }
                }
                None => typing = false,
            },
            size = sizes.recv(), if resizing => match size {
                Some((cols, rows)) => {
                    if let Err(error) = to_host.send(Message::text(resize_frame(cols, rows))).await {
                        return lost(&error);
                    }
                }
                None => resizing = false,
            },
            message = from_host.next() => match message {
                Some(Ok(Message::Binary(bytes))) => {
                    if let Err(error) = shown(output, &bytes).await {
                        // i18n: agent terminal window, error: typing could not be sent; {} is the technical reason
                        return Ended::Lost(tr!("The terminal could not be written: {}.", error));
                    }
                }
                Some(Ok(Message::Text(text))) => {
                    if let Err(error) = shown(output, text.as_bytes()).await {
                        // i18n: agent terminal window, error: typing could not be sent; {} is the technical reason
                        return Ended::Lost(tr!("The terminal could not be written: {}.", error));
                    }
                }
                Some(Ok(Message::Close(frame))) => return closed(frame.map(|frame| (frame.code, frame.reason.as_str().to_owned()))),
                Some(Ok(_)) => {}
                Some(Err(error)) => return lost(&error),
                // i18n: agent terminal window, closing line
                None => return Ended::Lost(tr!("The Host closed the connection.")),
            },
        }
    }
}

async fn shown<W: AsyncWrite + Unpin>(output: &mut W, bytes: &[u8]) -> std::io::Result<()> {
    output.write_all(bytes).await?;
    output.flush().await
}

/// What a close frame says: a reason is the Host's sentence; a normal close
/// without one is the CLI's end.
fn closed(frame: Option<(CloseCode, String)>) -> Ended {
    match frame {
        Some((_, reason)) if !reason.trim().is_empty() => Ended::Refused(reason.trim().to_owned()),
        // i18n: agent terminal window, closing line when the terminal ended normally
        None | Some((CloseCode::Normal, _)) => Ended::Closed(tr!("The agent's terminal closed.")),
        // i18n: agent terminal window, closing line; {} is the WebSocket close code number
        Some((code, _)) => Ended::Lost(tr!("The Host closed the terminal ({}).", u16::from(code))),
    }
}

/// A channel of sizes and its sender, for whoever watches the window.
pub fn sizes() -> (UnboundedSender<(u16, u16)>, UnboundedReceiver<(u16, u16)>) {
    unbounded_channel()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_terminal_address_follows_the_host_and_names_the_session_as_one_segment() {
        assert_eq!(terminal_url("http://127.0.0.1:7682", "rachel").unwrap().as_str(), "ws://127.0.0.1:7682/terminal/rachel");
        assert_eq!(terminal_url("https://host.example/", "rachel").unwrap().as_str(), "wss://host.example/terminal/rachel");
        assert_eq!(terminal_url("https://relay.example/h/abc?x=1#y", "a b/c").unwrap().as_str(), "wss://relay.example/h/abc/terminal/a%20b%2Fc");
        assert!(terminal_url("ftp://host", "rachel").is_err());
        assert!(terminal_url("not a url", "rachel").is_err());
    }

    #[test]
    fn a_size_is_the_text_the_host_reads() {
        let frame: Value = serde_json::from_str(&resize_frame(132, 43)).unwrap();
        assert_eq!(frame, serde_json::json!({"resize": {"cols": 132, "rows": 43}}));
    }

    #[test]
    fn a_refusal_shows_the_hosts_own_sentence() {
        let turn = "Rachel is in a turn; her terminal opens when it ends.";
        assert_eq!(refusal_sentence(409, "Conflict", turn.as_bytes()), turn, "plain text");
        assert_eq!(refusal_sentence(409, "Conflict", format!("  {turn}\n").as_bytes()), turn, "trimmed");
        let json = serde_json::json!({"error": turn}).to_string();
        assert_eq!(refusal_sentence(409, "Conflict", json.as_bytes()), turn, "JSON error");
        let json = serde_json::json!({"message": turn, "code": "agent_turn_running"}).to_string();
        assert_eq!(refusal_sentence(409, "Conflict", json.as_bytes()), turn, "JSON message");
        let json = serde_json::json!({"error": {"code": "agent_turn_running", "message": turn}}).to_string();
        assert_eq!(refusal_sentence(409, "Conflict", json.as_bytes()), turn, "an error object's message");
        let json = serde_json::json!({"error": "", "message": turn}).to_string();
        assert_eq!(refusal_sentence(409, "Conflict", json.as_bytes()), turn, "an empty error passes to the message");
        assert_eq!(refusal_sentence(423, "Locked", b""), "The Host refused the terminal (423 Locked).");
        assert_eq!(refusal_sentence(502, "", b"<html>bad gateway</html>"), "The Host refused the terminal (502).");
        assert_eq!(refusal_sentence(409, "Conflict", b"{\"retryable\": true}"), "{\"retryable\": true}", "JSON without a sentence, as text");
        let running = r#"{"error":"A turn of this agent is running. Stop it or wait until it ends, then open the terminal.","code":"agent_turn_running"}"#;
        assert_eq!(refusal_sentence(409, "Conflict", running.as_bytes()), "A turn of this agent is running. Stop it or wait until it ends, then open the terminal.");
    }

    #[test]
    fn an_answer_head_is_read_with_its_headers() {
        let head = parse_head(b"HTTP/1.1 409 Conflict\r\nContent-Type: text/plain\r\nContent-Length: 12").unwrap();
        assert_eq!((head.status, head.reason.as_str()), (409, "Conflict"));
        assert_eq!(head.header("content-length"), Some("12"));
        assert_eq!(parse_head(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket").unwrap().status, 101);
        assert!(parse_head(b"SSH-2.0-OpenSSH").is_err());
    }

    #[test]
    fn a_chunked_body_is_its_data() {
        assert_eq!(dechunk(b"5\r\nhello\r\n7\r\n, world\r\n0\r\n\r\n"), b"hello, world");
        assert_eq!(dechunk(b"5;x=y\r\nhello\r\n0\r\n\r\n"), b"hello");
        assert_eq!(dechunk(b""), b"");
    }

    #[test]
    fn a_close_with_a_reason_is_the_hosts_sentence_and_a_plain_close_the_end() {
        assert_eq!(closed(Some((CloseCode::Policy, "A turn is running".into()))), Ended::Refused("A turn is running".into()));
        assert_eq!(closed(Some((CloseCode::Normal, String::new()))), Ended::Closed("The agent's terminal closed.".into()));
        assert_eq!(closed(None), Ended::Closed("The agent's terminal closed.".into()));
        assert_eq!(closed(Some((CloseCode::Away, String::new()))), Ended::Lost("The Host closed the terminal (1001).".into()));
        assert!(!Ended::Closed(String::new()).holds() && Ended::Refused(String::new()).holds() && Ended::Lost(String::new()).holds());
    }
}
