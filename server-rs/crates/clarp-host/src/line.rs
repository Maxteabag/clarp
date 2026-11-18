//! A socket: what a client says and is told on one connection, whoever carries it.
//!
//! Some of what an app asks of a Host is no request and answer but a conversation: the keys
//! and the screen of a terminal, the stream of speech and what was understood of it. An app
//! holds that conversation over a WebSocket. It reaches a Host in one of two ways: at the
//! Host's own door, as the upgrade of a request, or through the connection relay, as a
//! stream of the one socket the Host keeps to the relay. A route that serves a socket does
//! not know which: it is handed a [`Line`], hears what the client says on it and tells the
//! client things on it, until one of the two is gone.
//!
//! A socket is asked for through the Host's router either way, so the same checks stand
//! before every one: who asks, with what scope, from where. The relay's connector asks with
//! a request that carries the line already (an extension of the request); a route takes its
//! socket with [`Socket`], which finds that line or, where there is none, the upgrade.
//!
//! **A client that says more than its route takes is let go**, by either carrier. A line
//! holds what was said and not heard yet up to [`WAITING_MOST`], counted in bytes so that
//! many small things said at once (keys, pieces of speech after a network stood still) are
//! held like one large thing; what does not fit ends the socket. Neither carrier waits for
//! a route instead: the relay's socket is one for every stream, and a carrier that stopped
//! reading a client would not learn that the client is gone. So a route is always told
//! that nobody is there any more, also while it waits for something else ([`Line::gone`]).

use std::convert::Infallible;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex, PoisonError};
use std::time::Duration;

use axum::extract::FromRequestParts;
use axum::extract::ws::{CloseFrame, Message, WebSocket, WebSocketUpgrade};
use axum::http::request::Parts;
use axum::http::{StatusCode, header};
use axum::response::{IntoResponse, Response};
use tokio::sync::{OwnedSemaphorePermit, Semaphore, mpsc};

/// The largest message that is taken on a socket, as the Python Host's connector takes one.
pub const MESSAGE_MOST: usize = clarp_kernel::connector::MESSAGE_MOST;
/// How much a client may have said that its route has not heard yet, as it weighs: the
/// largest message twice.
pub const WAITING_MOST: usize = 2 * MESSAGE_MOST;
/// What a thing that was said weighs beside its own bytes, so that things of no length
/// are bounded as well.
pub const WEIGHS_BESIDE: usize = 256;
/// How many things may wait to be told: a route that tells faster than its client takes
/// waits for the client.
const TOLD_MOST: usize = 64;
/// How long a client is given to take the word that its socket closes.
const CLOSES_WITHIN: Duration = Duration::from_secs(5);
/// The code of a socket that ended as it should.
pub const ENDED_WELL: u16 = 1000;
/// The code and the word a client at the door is let go with when it said too much.
pub const SAID_TOO_MUCH: (u16, &str) = (1008, "more was said than is taken");

/// What a client said.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Heard {
    Text(String),
    Bytes(Vec<u8>),
}

impl Heard {
    /// What it weighs on a line.
    fn weight(&self) -> usize {
        let length = match self {
            Self::Text(text) => text.len(),
            Self::Bytes(bytes) => bytes.len(),
        };
        length + WEIGHS_BESIDE
    }
}

/// What a client is told. After a [`Told::Close`] nothing more is.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Told {
    Text(String),
    Bytes(Vec<u8>),
    /// The socket is closed, with a code and a word of why.
    Close(u16, String),
}

/// A socket, as the route that serves it has it.
pub struct Line {
    /// What was said, each thing with the room it takes until it is heard.
    heard: mpsc::UnboundedReceiver<(Heard, OwnedSemaphorePermit)>,
    told: mpsc::Sender<Told>,
    /// How many signs of life the client gave: what it said, and what its carrier saw of it
    /// beside (a socket's own asking whether the other end is there).
    signs: Arc<AtomicU64>,
}

/// The same socket, as whoever carries it has it.
pub struct Carrier {
    pub heard: Hearer,
    pub told: mpsc::Receiver<Told>,
}

/// Where what a client says goes in.
#[derive(Clone)]
pub struct Hearer {
    heard: mpsc::UnboundedSender<(Heard, OwnedSemaphorePermit)>,
    /// The room that is left on the line.
    room: Arc<Semaphore>,
    signs: Arc<AtomicU64>,
}

/// Why a line did not take what a client said.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum NotTaken {
    /// More waits to be heard than a line holds: the client is let go.
    TooMuch,
    /// The route is done.
    NobodyHears,
}

impl Hearer {
    /// The client is there, though it said nothing a route hears: a route that lets a silent
    /// client go does not take this one for gone.
    pub fn alive(&self) {
        self.signs.fetch_add(1, Ordering::Relaxed);
    }

    /// Hands the route what its client said. It never waits.
    pub fn said(&self, said: Heard) -> Result<(), NotTaken> {
        self.alive();
        let weight = u32::try_from(said.weight()).unwrap_or(u32::MAX);
        let Ok(room) = self.room.clone().try_acquire_many_owned(weight) else {
            return Err(NotTaken::TooMuch);
        };
        let handed = self.heard.send((said, room));
        handed.map_err(|_| NotTaken::NobodyHears)
    }
}

impl Line {
    /// A socket and its carrier's end.
    pub fn new() -> (Self, Carrier) {
        let (heard_by, heard) = mpsc::unbounded_channel();
        let (told, told_to) = mpsc::channel(TOLD_MOST);
        let signs = Arc::new(AtomicU64::new(0));
        let hearer = Hearer {
            heard: heard_by,
            room: Arc::new(Semaphore::new(WAITING_MOST)),
            signs: signs.clone(),
        };
        let carrier = Carrier {
            heard: hearer,
            told: told_to,
        };
        let line = Self { heard, told, signs };
        (line, carrier)
    }

    /// How many signs of life the client has given so far. A route that lets a client go
    /// for saying nothing looks whether this grew meanwhile.
    pub fn signs_of_life(&self) -> u64 {
        self.signs.load(Ordering::Relaxed)
    }

    /// The next thing the client said. `None`: the client is gone.
    pub async fn heard(&mut self) -> Option<Heard> {
        // What is heard takes no room on the line any more.
        self.heard.recv().await.map(|(said, _room)| said)
    }

    /// Tells the client something. Whether it could be: `false` when the client is gone.
    pub async fn tell(&self, said: Told) -> bool {
        self.told.send(said).await.is_ok()
    }

    /// A way to tell the client things from elsewhere while the route listens.
    pub fn teller(&self) -> mpsc::Sender<Told> {
        self.told.clone()
    }

    /// Waits until the client is gone: for a route that waits for something else than
    /// what its client says, and must not wait for it when nobody is there any more.
    pub async fn gone(&self) {
        self.told.closed().await;
    }
}

/// A line that came with its request: the relay's connector put it there.
#[derive(Clone)]
pub struct Carried(Arc<Mutex<Option<Line>>>);

impl Carried {
    pub fn new(line: Line) -> Self {
        Self(Arc::new(Mutex::new(Some(line))))
    }

    fn take(&self) -> Option<Line> {
        self.0.lock().unwrap_or_else(PoisonError::into_inner).take()
    }
}

/// How a route takes its socket: the line that came with the request, or the upgrade of a
/// request at the door. A request that asks for no socket has none, and its route says so
/// in its own words: the Python Host's routes do not all say it alike.
pub struct Socket(Option<Asked>);

enum Asked {
    Door(Box<WebSocketUpgrade>),
    Carried(Line),
}

impl<S: Send + Sync> FromRequestParts<S> for Socket {
    type Rejection = Infallible;

    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        let carried = parts.extensions.get::<Carried>().and_then(Carried::take);
        if let Some(line) = carried {
            return Ok(Self(Some(Asked::Carried(line))));
        }
        // At the door a socket is asked for as the Python Host reads a request for one,
        // and is there when the connection can become one.
        if !clarp_kernel::socket::asked_for(&crate::edge::headers_of(&parts.headers)) {
            return Ok(Self(None));
        }
        let upgrade = WebSocketUpgrade::from_request_parts(parts, state).await;
        Ok(Self(
            upgrade.ok().map(|upgrade| Asked::Door(Box::new(upgrade))),
        ))
    }
}

impl Socket {
    /// Whether the request asked for a socket.
    pub fn asked(&self) -> bool {
        self.0.is_some()
    }

    /// Opens the socket and has `serve` serve it; the answer says that it was opened. The
    /// socket is closed when `serve` is done, if it has not closed it itself.
    pub fn open<F, Served>(self, serve: F) -> Response
    where
        F: FnOnce(Line) -> Served + Send + 'static,
        Served: Future<Output = ()> + Send + 'static,
    {
        match self.0 {
            Some(Asked::Door(upgrade)) => {
                let upgrade = upgrade.max_message_size(MESSAGE_MOST);
                upgrade.on_upgrade(move |socket| async move {
                    let (line, carrier) = Line::new();
                    tokio::join!(carried_at_the_door(socket, carrier), serve(line));
                })
            }
            Some(Asked::Carried(line)) => {
                tokio::spawn(serve(line));
                StatusCode::SWITCHING_PROTOCOLS.into_response()
            }
            None => refused(StatusCode::UPGRADE_REQUIRED, "upgrade required (websocket)"),
        }
    }
}

/// What a route of sockets answers a request it does not open one for: a status and plain
/// words, on a connection that is closed after them (`http_utils.py`,
/// `send_plain_http_error`).
pub fn refused(status: StatusCode, words: &str) -> Response {
    let headers = [
        (header::CONTENT_TYPE, "text/plain"),
        (header::CONNECTION, "close"),
    ];
    (status, headers, words.to_string()).into_response()
}

/// Carries a line over a WebSocket at the door: what the client says goes to the route,
/// what the route tells goes to the client, until one of them is gone or the route closes
/// the socket.
async fn carried_at_the_door(mut socket: WebSocket, carrier: Carrier) {
    let Carrier { heard, mut told } = carrier;
    let last = loop {
        tokio::select! {
            said = socket.recv() => {
                let said = match said {
                    Some(Ok(Message::Text(text))) => Heard::Text(text.as_str().to_string()),
                    Some(Ok(Message::Binary(bytes))) => Heard::Bytes(bytes.to_vec()),
                    // The library answers a ping by itself, and a close: the next reading
                    // sends its answer and says that the client is gone. A client that asks
                    // whether the Host is there is there itself.
                    Some(Ok(Message::Ping(_) | Message::Pong(_))) => {
                        heard.alive();
                        continue;
                    }
                    Some(Ok(Message::Close(_))) => continue,
                    // The client is gone: the route learns of it by both ends of its line.
                    Some(Err(_)) | None => return,
                };
                match heard.said(said) {
                    // A route that is done hears nothing: what it still told is carried.
                    Ok(()) | Err(NotTaken::NobodyHears) => {}
                    Err(NotTaken::TooMuch) => break closing(SAID_TOO_MUCH.0, SAID_TOO_MUCH.1),
                }
            }
            said = told.recv() => {
                let sent = match said {
                    Some(Told::Text(text)) => socket.send(Message::Text(text.into())).await,
                    Some(Told::Bytes(bytes)) => socket.send(Message::Binary(bytes.into())).await,
                    Some(Told::Close(code, reason)) => break closing(code, &reason),
                    // The route is done: the socket is closed as one that ended well.
                    None => break closing(ENDED_WELL, ""),
                };
                if sent.is_err() {
                    return;
                }
            }
        }
    };
    let closes = socket.send(Message::Close(Some(last)));
    let _ = tokio::time::timeout(CLOSES_WITHIN, closes).await;
}

fn closing(code: u16, reason: &str) -> CloseFrame {
    CloseFrame {
        code,
        reason: reason.into(),
    }
}

/// A route for the tests of what carries a socket.
#[cfg(test)]
pub(crate) mod testing {
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use axum::routing::{MethodRouter, get};

    use super::*;

    /// What the route says to a request that asks for no socket.
    pub(crate) const NO_SOCKET: &str = "this route is a socket";

    /// A route that says `ready` and then says back what it hears: a text with `text: `
    /// before it, bytes in reverse. Told `bye` it closes in words of its own, told `end`
    /// it ends without a word, and told `deaf` it hears nothing more until its client is
    /// gone. `served` counts the sockets it serves at the moment.
    pub(crate) fn echo(served: Arc<AtomicUsize>) -> MethodRouter {
        get(move |socket: Socket| {
            let served = served.clone();
            async move {
                if !socket.asked() {
                    return refused(StatusCode::UPGRADE_REQUIRED, NO_SOCKET);
                }
                socket.open(move |mut line| async move {
                    served.fetch_add(1, Ordering::SeqCst);
                    line.tell(Told::Text("ready".into())).await;
                    while let Some(heard) = line.heard().await {
                        let told = match heard {
                            Heard::Text(text) if text == "bye" => {
                                let why = "it said bye".to_string();
                                line.tell(Told::Close(4001, why)).await;
                                break;
                            }
                            Heard::Text(text) if text == "end" => break,
                            Heard::Text(text) if text == "deaf" => {
                                line.tell(Told::Text("deaf".into())).await;
                                line.gone().await;
                                break;
                            }
                            Heard::Text(text) => Told::Text(format!("text: {text}")),
                            Heard::Bytes(mut bytes) => {
                                bytes.reverse();
                                Told::Bytes(bytes)
                            }
                        };
                        if !line.tell(told).await {
                            break;
                        }
                    }
                    served.fetch_sub(1, Ordering::SeqCst);
                })
            }
        })
    }

    /// Waits until the route serves no socket any more.
    pub(crate) async fn nobody_is_served(served: &AtomicUsize) {
        let waited = tokio::time::timeout(Duration::from_secs(10), async {
            while served.load(Ordering::SeqCst) != 0 {
                tokio::time::sleep(Duration::from_millis(10)).await;
            }
        });
        assert!(waited.await.is_ok(), "a socket is still served");
    }
}

#[cfg(test)]
mod tests {
    #![allow(clippy::unwrap_used, clippy::expect_used)]

    use std::net::SocketAddr;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};

    use futures_util::{SinkExt, StreamExt};
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio_tungstenite::tungstenite::Message as Said;

    use super::*;

    type Client = tokio_tungstenite::WebSocketStream<
        tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>,
    >;

    /// The route of the tests behind a door of its own: where it listens, and how many
    /// sockets it serves.
    async fn a_door() -> (SocketAddr, Arc<AtomicUsize>) {
        let served = Arc::new(AtomicUsize::new(0));
        let router = axum::Router::new().route("/echo", testing::echo(served.clone()));
        let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
        let address = listener.local_addr().unwrap();
        tokio::spawn(async move { axum::serve(listener, router).await });
        (address, served)
    }

    /// No test waits longer than this for what a socket should say: a wrong rule fails a
    /// test, and does not keep it waiting.
    const PATIENCE: Duration = Duration::from_secs(10);

    /// A client whose socket the route opened: it has been told `ready`.
    async fn a_client(address: SocketAddr) -> Client {
        let opened = tokio_tungstenite::connect_async(format!("ws://{address}/echo"));
        let opened = tokio::time::timeout(PATIENCE, opened).await;
        let (mut client, _) = opened.expect("the socket is opened in time").unwrap();
        assert_eq!(told(&mut client).await, Said::Text("ready".into()));
        client
    }

    /// The next thing a client hears of its socket, when it hears anything in time.
    async fn next(
        client: &mut Client,
    ) -> Option<Result<Said, tokio_tungstenite::tungstenite::Error>> {
        let heard = tokio::time::timeout(PATIENCE, client.next()).await;
        heard.expect("the client hears of its socket in time")
    }

    /// The next thing a client is told, but for the answers to its pings.
    async fn told(client: &mut Client) -> Said {
        loop {
            match next(client).await.unwrap().unwrap() {
                Said::Pong(_) => {}
                other => return other,
            }
        }
    }

    /// What a request written out by hand is answered, to the end of its connection.
    async fn answered(address: SocketAddr, headers: &str) -> String {
        let mut stream = tokio::net::TcpStream::connect(address).await.unwrap();
        let request = format!("GET /echo HTTP/1.1\r\nHost: test\r\n{headers}\r\n");
        stream.write_all(request.as_bytes()).await.unwrap();
        let mut answer = String::new();
        let whole = stream.read_to_string(&mut answer);
        let whole = tokio::time::timeout(PATIENCE, whole).await;
        whole
            .expect("the connection is closed after a refusal")
            .unwrap();
        answer
    }

    #[tokio::test]
    async fn a_socket_at_the_door_is_carried_both_ways_and_closed_in_its_routes_words() {
        let (address, served) = a_door().await;
        let mut client = a_client(address).await;
        assert_eq!(served.load(Ordering::SeqCst), 1);

        // A text is heard as a text and bytes as bytes, and each is told in its kind.
        client.send(Said::Text("hello".into())).await.unwrap();
        assert_eq!(told(&mut client).await, Said::Text("text: hello".into()));
        client
            .send(Said::Binary(vec![1, 2, 3].into()))
            .await
            .unwrap();
        assert_eq!(told(&mut client).await, Said::Binary(vec![3, 2, 1].into()));
        // A ping is the library's to answer: the route hears nothing of it.
        client.send(Said::Ping(vec![7].into())).await.unwrap();
        client.send(Said::Text("after".into())).await.unwrap();
        assert_eq!(told(&mut client).await, Said::Text("text: after".into()));
        // The largest message is taken whole.
        let largest = vec![5u8; MESSAGE_MOST];
        client
            .send(Said::Binary(largest.clone().into()))
            .await
            .unwrap();
        assert_eq!(told(&mut client).await, Said::Binary(largest.into()));

        // The route closes the socket in words of its own.
        client.send(Said::Text("bye".into())).await.unwrap();
        let Said::Close(Some(closed)) = told(&mut client).await else {
            panic!("the socket was not closed with a word");
        };
        assert_eq!(
            (u16::from(closed.code), closed.reason.as_str()),
            (4001, "it said bye")
        );
        testing::nobody_is_served(&served).await;

        // A route that is done without a word closes its socket as one that ended well.
        let mut client = a_client(address).await;
        client.send(Said::Text("end".into())).await.unwrap();
        let Said::Close(Some(closed)) = told(&mut client).await else {
            panic!("the socket was not closed with a word");
        };
        assert_eq!(
            (u16::from(closed.code), closed.reason.as_str()),
            (ENDED_WELL, "")
        );
        testing::nobody_is_served(&served).await;

        // A client that closes is answered by the library, and its route hears that
        // nobody is there.
        let mut client = a_client(address).await;
        client.close(None).await.unwrap();
        assert_eq!(told(&mut client).await, Said::Close(None));
        testing::nobody_is_served(&served).await;
        // And so does the route of a client that is gone without a word.
        let client = a_client(address).await;
        assert_eq!(served.load(Ordering::SeqCst), 1);
        drop(client);
        testing::nobody_is_served(&served).await;
    }

    #[tokio::test]
    async fn a_client_at_the_door_that_says_more_than_its_route_takes_is_let_go() {
        let (address, served) = a_door().await;
        let mut client = a_client(address).await;
        client.send(Said::Text("deaf".into())).await.unwrap();
        assert_eq!(told(&mut client).await, Said::Text("deaf".into()));
        // The route hears nothing more. What the client says waits on the line as far
        // as the line holds it, and then the client is let go with a word.
        let piece = 64 * 1024;
        let fit = WAITING_MOST / (piece + WEIGHS_BESIDE);
        assert_eq!(fit, 31);
        for _ in 0..fit {
            let said = Said::Binary(vec![9u8; piece].into());
            client.send(said).await.unwrap();
        }
        let quiet = tokio::time::timeout(Duration::from_millis(300), client.next());
        assert!(
            quiet.await.is_err(),
            "so much is taken, and nothing is said of it"
        );
        assert_eq!(served.load(Ordering::SeqCst), 1);
        client
            .send(Said::Binary(vec![9u8; piece].into()))
            .await
            .unwrap();
        let Said::Close(Some(closed)) = told(&mut client).await else {
            panic!("the socket was not closed with a word");
        };
        assert_eq!(
            (u16::from(closed.code), closed.reason.as_str()),
            SAID_TOO_MUCH
        );
        // And the route, which listens to nothing, learns that nobody is there.
        testing::nobody_is_served(&served).await;

        // So does the route of a client that goes without a word while its route listens
        // to nothing.
        let mut client = a_client(address).await;
        client.send(Said::Text("deaf".into())).await.unwrap();
        assert_eq!(told(&mut client).await, Said::Text("deaf".into()));
        assert_eq!(served.load(Ordering::SeqCst), 1);
        drop(client);
        testing::nobody_is_served(&served).await;

        // A message larger than the largest is not taken: the socket is given up.
        let mut client = a_client(address).await;
        let too_large = vec![5u8; MESSAGE_MOST + 1];
        let _ = client.send(Said::Binary(too_large.into())).await;
        let after = next(&mut client).await;
        assert!(
            !matches!(after, Some(Ok(Said::Binary(_) | Said::Text(_)))),
            "{after:?}"
        );
        testing::nobody_is_served(&served).await;
    }

    #[tokio::test]
    async fn what_asks_for_no_socket_is_told_so_in_the_routes_words() {
        let (address, served) = a_door().await;
        let key = "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n";
        let version = "Sec-WebSocket-Version: 13\r\n";
        let upgrade = "Upgrade: websocket\r\nConnection: Upgrade\r\n";
        for (what, headers) in [
            ("a plain request", String::new()),
            // What the Python Host does not read as a request for a socket is none here,
            // though the library would open it: a key that is blank, a `Connection` that
            // only resembles, a header said again otherwise.
            (
                "a blank key",
                format!("{upgrade}Sec-WebSocket-Key:   \r\n{version}"),
            ),
            (
                "a connection that resembles",
                format!("Upgrade: websocket\r\nConnection: upgraded\r\n{key}{version}"),
            ),
            (
                "an upgrade said again otherwise",
                format!("{upgrade}Upgrade: h2c\r\n{key}{version}"),
            ),
            // And what the library cannot open is none either.
            ("no version", format!("{upgrade}{key}")),
        ] {
            let answer = answered(address, &headers).await;
            let (head, body) = answer.split_once("\r\n\r\n").unwrap();
            let head = head.to_lowercase();
            assert!(head.starts_with("http/1.1 426 "), "{what}: {answer}");
            assert!(
                head.contains("\r\ncontent-type: text/plain\r\n"),
                "{what}: {head}"
            );
            assert!(head.contains("\r\nconnection: close"), "{what}: {head}");
            assert_eq!(body, testing::NO_SOCKET, "{what}");
        }
        assert_eq!(served.load(Ordering::SeqCst), 0);
        // The same request with all of it is opened.
        let mut stream = tokio::net::TcpStream::connect(address).await.unwrap();
        let request = format!("GET /echo HTTP/1.1\r\nHost: test\r\n{upgrade}{key}{version}\r\n");
        stream.write_all(request.as_bytes()).await.unwrap();
        let mut head = [0u8; 12];
        let read = tokio::time::timeout(PATIENCE, stream.read_exact(&mut head)).await;
        read.expect("the request is answered in time").unwrap();
        assert_eq!(&head, b"HTTP/1.1 101");
    }

    #[tokio::test]
    async fn a_line_carries_what_is_said_both_ways_and_ends_with_either_end() {
        let (mut line, mut carrier) = Line::new();
        carrier.heard.said(Heard::Text("one".into())).unwrap();
        carrier.heard.said(Heard::Bytes(vec![2])).unwrap();
        assert_eq!(line.heard().await, Some(Heard::Text("one".into())));
        assert_eq!(line.heard().await, Some(Heard::Bytes(vec![2])));
        assert!(line.tell(Told::Text("three".into())).await);
        let teller = line.teller();
        teller.send(Told::Bytes(vec![4])).await.unwrap();
        assert_eq!(carrier.told.recv().await, Some(Told::Text("three".into())));
        assert_eq!(carrier.told.recv().await, Some(Told::Bytes(vec![4])));

        // A line holds so much that was said and not heard, by its weight, and no more:
        // the largest message twice is too much, since each weighs a little beside its
        // bytes, and things of no length are bounded by that little.
        let largest = || Heard::Bytes(vec![0; MESSAGE_MOST]);
        assert_eq!(carrier.heard.said(largest()), Ok(()));
        assert_eq!(carrier.heard.said(largest()), Err(NotTaken::TooMuch));
        let nothing = || Heard::Text(String::new());
        let fit = (WAITING_MOST - MESSAGE_MOST - WEIGHS_BESIDE) / WEIGHS_BESIDE;
        assert_eq!(fit, 4095);
        for _ in 0..fit {
            assert_eq!(carrier.heard.said(nothing()), Ok(()));
        }
        assert_eq!(carrier.heard.said(nothing()), Err(NotTaken::TooMuch));
        // What is heard makes room again, and from another place the same line is spoken
        // to in the same way.
        assert_eq!(line.heard().await, Some(largest()));
        let elsewhere = carrier.heard.clone();
        assert_eq!(elsewhere.said(largest()), Ok(()));
        assert_eq!(elsewhere.said(nothing()), Err(NotTaken::TooMuch));
        assert_eq!(carrier.heard.said(nothing()), Err(NotTaken::TooMuch));

        // The carrier gone, the route hears what was said and then that nobody is there,
        // and can tell nothing more; the route gone, the carrier is told nothing more and
        // what a client says is taken by nobody.
        let waits = tokio::time::timeout(Duration::from_millis(50), line.gone());
        assert!(waits.await.is_err(), "nobody is gone yet");
        let Carrier { heard, mut told } = carrier;
        drop((heard, elsewhere));
        for _ in 0..=fit {
            assert!(line.heard().await.is_some());
        }
        assert_eq!(line.heard().await, None);
        let (other, others) = Line::new();
        let Carrier {
            heard: said,
            told: nobody,
        } = others;
        drop(nobody);
        other.gone().await;
        assert!(!other.tell(Told::Text("nobody hears this".into())).await);
        drop(other);
        assert_eq!(said.said(nothing()), Err(NotTaken::NobodyHears));
        drop((line, teller));
        assert_eq!(told.recv().await, None);

        // A line that came with a request is taken once.
        let (line, _carrier) = Line::new();
        let carried = Carried::new(line);
        assert!(carried.clone().take().is_some());
        assert!(carried.take().is_none());
    }
}
