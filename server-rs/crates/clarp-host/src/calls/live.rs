//! Rings held as live voice calls, at the Host's edge.
//!
//! The rules are the kernel's: what a call is told ([`clarp_kernel::live_call`]), the ring
//! and its grant ([`clarp_kernel::rings`]), and the session between a phone and the voice
//! ([`clarp_kernel::live_session`]). Here is what a Host adds: the profiles as their folders
//! have them, the requests of an app (`POST /calls/live`, the close and the switch of a
//! call, a phone's own switch for rings), and the socket of a call (`GET /calls/ring/live`),
//! on which one task owns one session and stands between the phone and the voice.
//!
//! Where the authority of a call lies is on its ring, and the rings are the desk's
//! ([`super::Desk`]), under its one lock. Every step of a session takes that lock, writes on
//! the ring, and only then tells either side; and a request that ends a call tells its
//! session so before it lets go of the lock. So a call is never retired and its session not
//! told, and nothing is sent for a call that has lost its authority.
//!
//! The voice is a provider's, reached with the owner's key over a socket of its own
//! (`wss://api.openai.com/v1/live/sessions`); in a test it is a stand-in on this machine. The
//! routes answer as the Python Host's do (`server/lib/routes/calls.py`,
//! `agent_ring_live.py`, `agent_ring_live_socket.py`, `docs/agent-rings.md`).

use std::cell::RefCell;
use std::path::PathBuf;
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use clarp_kernel::calls::Refused;
use clarp_kernel::live_call::{self, Found, Scan};
use clarp_kernel::live_session::{self as rules, Book, Session, Wire};
use clarp_kernel::python;
use clarp_kernel::rings::{self, Android, Claim, Joined, Minted, Profiles, Rings};
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tokio::sync::{mpsc, watch};
use tokio::time::Instant;
use tokio_tungstenite::tungstenite::Message;
use tokio_tungstenite::tungstenite::client::IntoClientRequest;

use super::{Desk, RINGS, RINGS_KEPT, answered, caller, ring_asked, text};
use crate::agent::Unavailable;
use crate::edge::{App, Principal};
use crate::fleet::Directory;
use crate::ids;
use crate::line::{ENDED_WELL, Heard, Line, Socket, Told};

/// Where the voice of a live call is reached.
const VOICE_URL: &str = "wss://api.openai.com/v1/live/sessions";
/// How long the voice has to take the connection.
const VOICE_OPENS_WITHIN: Duration = Duration::from_secs(20);
/// How long the voice is given to take the word that the call is over.
const VOICE_ENDS_WITHIN: Duration = Duration::from_secs(1);
/// How long a switch to an ordinary call waits for the session of the live one to end.
const SESSION_ENDS_WITHIN: Duration = Duration::from_secs(5);
/// How many names of a folder of profiles are read at most, to find its first ones.
const NAMES_READ: usize = 4096;

// ---- the profiles ----------------------------------------------------------------------

/// The profiles of this Host, as their folders have them: `<data>/agent-live/profiles`,
/// each a folder of its name with a `profile.json` and the files that pins.
pub struct Shelf {
    root: PathBuf,
    /// The agents known to take the user's calls live, as it was kept before this request.
    known: Option<String>,
    /// And as it is to be kept after it, where that changed.
    keep: RefCell<Option<String>>,
}

impl Shelf {
    pub(super) fn of(app: &App, known: Option<String>) -> Self {
        let root = app.config.data_dir.join("agent-live").join("profiles");
        Self::at(root, known)
    }

    /// The profiles of one folder.
    fn at(root: PathBuf, known: Option<String>) -> Self {
        Self {
            root,
            known,
            keep: RefCell::new(None),
        }
    }

    /// What is to be kept of the agents that take the user's calls, where it changed.
    pub(super) fn to_keep(&self) -> Option<String> {
        self.keep.borrow().clone()
    }

    /// The folder of a profile, where the name is one a profile can have.
    fn folder(&self, profile_id: &str) -> Option<PathBuf> {
        live_call::profile_name(profile_id).then(|| self.root.join(profile_id))
    }

    /// The folder of profiles as it is found: its first entries in the order of their names,
    /// one more than are looked at.
    fn found(&self) -> Result<Vec<Found>, std::io::Error> {
        let mut names: Vec<(String, bool)> = Vec::new();
        for entry in std::fs::read_dir(&self.root)?.take(NAMES_READ) {
            let entry = entry?;
            // Whether it is a folder itself: a link to one is none.
            let is_folder = entry.file_type().is_ok_and(|kind| kind.is_dir());
            names.push((entry.file_name().to_string_lossy().into_owned(), is_folder));
        }
        names.sort();
        names.truncate(live_call::PROFILES_SCANNED + 1);
        let found = names.into_iter().map(|(name, is_folder)| {
            let held = std::fs::File::open(self.root.join(&name).join("profile.json"));
            // One byte more than a profile may be, so that one too large is seen to be.
            let profile = held.ok().and_then(|file| {
                use std::io::Read;
                let mut bytes = Vec::new();
                let most = live_call::PROFILE_BYTES as u64 + 1;
                file.take(most).read_to_end(&mut bytes).ok().map(|_| bytes)
            });
            Found {
                name,
                is_folder,
                profile,
            }
        });
        Ok(found.collect())
    }

    /// Which agents take the user's calls live, by what the profiles say now.
    fn taking(&self, strict: bool) -> live_call::Taking {
        let known = self.keep.borrow().clone().or_else(|| self.known.clone());
        let found = self.found();
        let scan = match &found {
            Ok(found) => Scan::Folders(found),
            Err(none) if none.kind() == std::io::ErrorKind::NotFound => Scan::NotThere,
            Err(_) => Scan::Unreadable,
        };
        let taking = live_call::taking(known.as_deref(), scan, strict);
        if let Some(keep) = &taking.keep {
            *self.keep.borrow_mut() = Some(keep.clone());
        }
        taking
    }
}

impl Profiles for Shelf {
    fn profile(&self, profile_id: &str) -> Option<String> {
        std::fs::read_to_string(self.folder(profile_id)?.join("profile.json")).ok()
    }

    fn file(&self, profile_id: &str, name: &str) -> Option<Vec<u8>> {
        let folder = self.folder(profile_id)?.canonicalize().ok()?;
        let file = folder.join(name).canonicalize().ok()?;
        // No link leads out of a profile.
        if !file.starts_with(&folder) {
            return None;
        }
        std::fs::read(file).ok()
    }

    fn takes_calls(&self, session: &str) -> Result<Option<String>, Refused> {
        Ok(self.taking(true).takes?.get(session).cloned().flatten())
    }
}

/// The Android phones' push rows that say how they can hold a ring's conversation.
pub(super) fn androids(directory: &Directory) -> Vec<Android> {
    let rows = directory.push.active().filter(|row| row.provider == "fcm");
    rows.filter_map(|row| {
        let modes: Vec<String> = serde_json::from_str(&row.supported_conversation_modes).ok()?;
        (!modes.is_empty()).then(|| Android {
            owner: row.principal.clone(),
            modes,
        })
    })
    .collect()
}

// ---- the sessions that run -------------------------------------------------------------

/// A session that runs: how it is told that its call was ended beside it, and how it is
/// known that it is over.
pub struct Running {
    stops: mpsc::UnboundedSender<String>,
    over: watch::Receiver<bool>,
}

impl Running {
    /// The call was retired on its ring: its session is to stop, for this reason.
    pub(super) fn stop(&self, reason: &str) {
        let _ = self.stops.send(reason.to_string());
    }
}

/// The ring of a session's call, written on as the session says: under the desk's lock.
struct OnRing<'a> {
    rings: &'a mut Rings,
    call: (&'a str, i64),
    out: rings::Effects,
}

impl Book for OnRing<'_> {
    fn holds(&self) -> bool {
        self.rings.holds(self.call.0, self.call.1)
    }

    fn opening(&mut self) -> bool {
        self.rings.opening(self.call, &mut self.out)
    }

    fn under_way(&mut self) -> bool {
        self.rings.under_way(self.call, &mut self.out)
    }

    fn heard(&mut self) -> bool {
        self.rings.heard(self.call, &mut self.out)
    }

    fn retire(&mut self, reason: &str) -> bool {
        self.rings.retired(self.call, reason, &mut self.out)
    }
}

/// What a step of a session tells the two sides, in its order: told once the step is done
/// and the books are let go of.
#[derive(Default)]
struct ToTell {
    said: Vec<Said>,
    /// The voice's socket is known to be gone: nothing more is told it.
    voice_gone: bool,
}

enum Said {
    Phone(Value),
    Voice(Value),
    VoiceOver,
    PhoneOver,
}

impl Wire for ToTell {
    fn phone(&mut self, said: Value) {
        self.said.push(Said::Phone(said));
    }

    fn voice(&mut self, said: Value) -> bool {
        if self.voice_gone {
            return false;
        }
        self.said.push(Said::Voice(said));
        true
    }

    fn voice_over(&mut self) {
        self.said.push(Said::VoiceOver);
    }

    fn phone_over(&mut self) {
        self.said.push(Said::PhoneOver);
    }
}

impl Desk {
    /// The agents that take the user's calls live, by their sessions: what an app's list of
    /// agents says of each (`call_mode`). Those known to are kept, so that a profile that
    /// cannot be read for a moment does not turn a live call into an ordinary one.
    pub async fn taking(&self, app: &App) -> std::collections::HashSet<String> {
        let mut held = self.books.lock().await;
        let Ok(books) = Self::read(&mut held, app).await else {
            return std::collections::HashSet::new();
        };
        let shelf = Shelf::of(app, books.taking.clone());
        let taking = shelf.taking(false);
        if let Some(keep) = shelf.to_keep() {
            let kept = serde_json::to_vec(&json!(keep)).unwrap_or_default();
            let name = super::TAKING_KEPT.to_string();
            if app.fleet.keep(RINGS, name, Some(kept)).await.is_ok() {
                books.taking = Some(keep);
            }
        }
        taking
            .takes
            .map_or_else(|_| Default::default(), |takes| takes.into_keys().collect())
    }

    /// Whether a client may be told of the ring of this id: a live ring only a phone that
    /// can hold one, a call the user started only its own phone. A ring that is not kept
    /// any more is nobody's secret.
    pub async fn shows(&self, app: &App, ring_id: &str, principal: &str) -> bool {
        let mut held = self.books.lock().await;
        let Ok(books) = Self::read(&mut held, app).await else {
            return true;
        };
        let Some(ring) = books
            .rings
            .rings()
            .iter()
            .find(|ring| ring.ring_id == ring_id)
        else {
            return true;
        };
        let directory = app.fleet.directory();
        let shelf = Shelf::of(app, None);
        let androids = androids(&directory);
        let paired = |principal: &str| directory.can_receive(principal);
        let world = rings::World {
            profiles: &shelf,
            android: &androids,
            can_push_android: false,
            device_ok: &paired,
            host_id: &app.server_id,
            has_key: true,
            runs_tools: false,
        };
        books.rings.sees(ring, principal, &world)
    }

    /// One step of a session: its ring is written on under the desk's lock, and kept where
    /// it changed, before anything of the step is told to either side.
    async fn step<T>(
        &self,
        app: &App,
        call: (&str, i64),
        act: impl FnOnce(&mut dyn Book) -> T,
    ) -> Result<T, Unavailable> {
        let mut held = self.books.lock().await;
        let books = Self::read(&mut held, app).await?;
        let mut book = OnRing {
            rings: &mut books.rings,
            call,
            out: rings::Effects::default(),
        };
        let done = act(&mut book);
        if book.out.changed.rings {
            let kept = serde_json::to_vec(books.rings.rings()).unwrap_or_default();
            app.fleet
                .keep(RINGS, RINGS_KEPT.to_string(), Some(kept))
                .await?;
        }
        Ok(done)
    }

    /// A phone takes the socket of a live call: one phone holds one. `false`: it holds one
    /// already.
    pub(crate) async fn claim_voice(&self, app: &App, principal: &str) -> bool {
        let mut held = self.books.lock().await;
        match Self::read(&mut held, app).await {
            Ok(books) => books.voices.insert(principal.to_string()),
            Err(_) => false,
        }
    }

    pub(crate) async fn release_voice(&self, principal: &str) {
        if let Some(books) = self.books.lock().await.as_mut() {
            books.voices.remove(principal);
        }
    }

    /// A session begins: from now on a request that ends its call tells it so.
    async fn began(&self, call_id: &str, running: Running) {
        if let Some(books) = self.books.lock().await.as_mut() {
            books.sessions.insert(call_id.to_string(), running);
        }
    }

    async fn ended(&self, call_id: &str) {
        if let Some(books) = self.books.lock().await.as_mut() {
            books.sessions.remove(call_id);
        }
    }

    /// Whether the session of a call is over, waited for no longer than a switch waits. A
    /// call that never had a session has none to end.
    async fn settled(&self, call_id: &str) -> bool {
        let over = {
            let held = self.books.lock().await;
            let running = held.as_ref().and_then(|books| books.sessions.get(call_id));
            running.map(|running| running.over.clone())
        };
        let Some(mut over) = over else {
            return true;
        };
        let ended = async {
            while !*over.borrow() {
                if over.changed().await.is_err() {
                    break;
                }
            }
        };
        tokio::time::timeout(SESSION_ENDS_WITHIN, ended)
            .await
            .is_ok()
    }
}

// ---- what an app asks ------------------------------------------------------------------

/// A refusal in the kernel's own form, for what a route itself refuses.
fn refusal(status: u16, error: &str) -> Refused {
    Refused {
        status,
        body: json!({ "ok": false, "error": error }),
    }
}

/// New names for an answered call: its id and its attempt, the owner's nonce and the ticket.
pub(super) struct Names([String; 4]);

impl Names {
    pub(super) fn new() -> Self {
        Self([ids::hex(16), ids::hex(16), ids::hex(32), ids::hex(32)])
    }

    pub(super) fn minted(&self) -> Minted<'_> {
        let [call, attempt, nonce, ticket] = &self.0;
        Minted {
            call,
            attempt,
            nonce,
            ticket,
        }
    }
}

/// A ring that was placed, and whom it is pushed to: the phones that are pushed it as an
/// incoming call (the token each left for calls and its side of the push service), and how
/// many Android phones a live ring would be told to.
pub(super) struct Placed {
    pub ring: rings::Ring,
    pub calls: Vec<(String, String)>,
    pub android: usize,
}

/// Whom a ring that was just placed is pushed to, read under the desk's lock.
pub(super) fn placed(rings: &Rings, ring: rings::Ring, world: &rings::World) -> Placed {
    let phones = rings.rung_as_calls(&ring, world);
    let tokens = phones.iter().filter_map(|phone| rings.call_token(phone));
    let calls = tokens.map(|(token, environment)| (token.to_string(), environment.to_string()));
    Placed {
        calls: calls.collect(),
        android: rings.sent(&ring, "ring", world).android.len(),
        ring,
    }
}

/// `POST /calls/live` with exactly `{agent, request_id, client_nonce}`: the user calls an
/// agent that takes calls live. The answer is the grant of a call already answered.
async fn user_call(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    bytes: Bytes,
) -> Response {
    let (caller, said) = match ring_asked(&principal, &bytes) {
        Ok(asked) => asked,
        Err(refused) => return refused.into_response(),
    };
    let names = Names::new();
    let desk = app.calls.clone();
    let called = desk.rings(&app, |rings, _, with, (out, _)| {
        // What is no paired phone is told so before anything of what it sent is looked at.
        if !caller.starts_with("device_") || !(with.world.device_ok)(&caller) {
            return Err(refusal(403, "live_auth"));
        }
        // Three things and no more: who is called, and the two names of the request. That
        // each of them is there, and well made, the rules of the call hold.
        if said.as_object().is_none_or(|fields| fields.len() != 3) {
            return Err(refusal(400, "live_request_invalid"));
        }
        rings.user_call(
            (&caller, &said["agent"]),
            (&said["request_id"], &said["client_nonce"]),
            with.known,
            &with.world,
            (with.new_id, names.minted(), with.at.now_ms),
            out,
        )
    });
    answered(called.await)
}

/// `POST /calls/ring/live/close`: the phone hangs up beside its socket. The call is retired
/// on its ring, and its session is told before the books are let go of.
async fn close(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    bytes: Bytes,
) -> Response {
    let (caller, said) = match ring_asked(&principal, &bytes) {
        Ok(asked) => asked,
        Err(refused) => return refused.into_response(),
    };
    let desk = app.calls.clone();
    let closed = desk.rings(&app, |rings, _, with, (out, _)| {
        let (answer, call_id) = rings.close(&caller, &said, &with.world, with.at.now_ms, out)?;
        let reason = text(&said, "reason");
        with.stops.borrow_mut().push((call_id, reason));
        Ok(answer)
    });
    answered(closed.await)
}

/// `POST /calls/ring/live/fallback`: the one switch of a live call to an ordinary one. The
/// call is retired for it and its session stopped; only when that has ended the opening
/// line is voiced and the ordinary call starts.
async fn fallback(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    bytes: Bytes,
) -> Response {
    let (caller, said) = match ring_asked(&principal, &bytes) {
        Ok(asked) => asked,
        Err(refused) => return refused.into_response(),
    };
    let desk = app.calls.clone();
    let claimed = desk.rings(&app, |rings, calls, with, (out, _)| {
        let claim = rings.switch(&caller, &said, (&*calls, &with.world), with.at.now_ms, out)?;
        if let Claim::Stop { call_id } = &claim {
            with.stops
                .borrow_mut()
                .push((call_id.clone(), "fallback".to_string()));
        }
        // Whose line is said when the ordinary call starts: the ring's agent, its opening.
        let named = text(&said, "call_id");
        let of_the_call =
            |ring: &&rings::Ring| ring.live.as_ref().is_some_and(|live| live.call_id == named);
        let ring = rings.rings().iter().find(of_the_call);
        Ok((
            claim,
            ring.map(|ring| (ring.session.clone(), ring.opening_line.clone())),
        ))
    });
    let (call_id, of_ring) = match claimed.await {
        Ok(Ok((Claim::Before(answer), _))) => return Json(answer).into_response(),
        Ok(Ok((Claim::Stop { call_id }, of_ring))) => (call_id, of_ring),
        Ok(Err(refused)) => return answered(Ok(Err(refused))),
        Err(unavailable) => return answered(Err(unavailable)),
    };
    let settled = desk.settled(&call_id).await;
    // Said in the agent's own voice, as the opening of any ring is.
    let voiced = match of_ring.and_then(|(session, line)| Some((app.member(&session)?, line))) {
        Some((member, line)) if settled => {
            let said = app.mouth.said_outside_a_turn(&member.record, &line);
            said.await
        }
        _ => None,
    };
    let voiced = voiced.map_or_else(String::new, |name| format!("/audio/{name}"));
    let switched = desk.rings(&app, |rings, calls, with, effects| {
        rings.switched(
            calls,
            (&caller, &said),
            (settled, &voiced),
            (with.roster, with.new_id),
            with.at,
            effects,
        )
    });
    answered(switched.await)
}

/// `GET /calls/ring/device`: whether this phone receives rings.
async fn receiving(State(app): State<App>, Extension(principal): Extension<Principal>) -> Response {
    let caller = match caller(&principal) {
        Ok(caller) => caller,
        Err(refused) => return refused.into_response(),
    };
    let unbound = unbound_alerts(&app.fleet.directory());
    let desk = app.calls.clone();
    let told = desk.rings(&app, |rings, _, with, _| {
        rings.receiving(&caller, &with.world, unbound)
    });
    answered(told.await)
}

/// `POST /calls/ring/device` with `{receive_rings}`: a phone switches rings on or off for
/// itself alone.
async fn receive(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    bytes: Bytes,
) -> Response {
    let (caller, said) = match ring_asked(&principal, &bytes) {
        Ok(asked) => asked,
        Err(refused) => return refused.into_response(),
    };
    let unbound = unbound_alerts(&app.fleet.directory());
    let desk = app.calls.clone();
    let switched = desk.rings(&app, |rings, _, with, (out, _)| {
        rings.receive(&caller, &said, (&with.world, unbound), with.at.now_ms, out)
    });
    answered(switched.await)
}

/// How many of the phones' push rows nobody is known to own: while there is one, a phone
/// that switched rings off may still be told of a ring through it.
fn unbound_alerts(directory: &Directory) -> usize {
    let rows = directory.push.active();
    rows.filter(|row| row.principal.is_empty()).count()
}

// ---- the socket of a call --------------------------------------------------------------

/// The voice's end of a call.
pub(crate) type Voice =
    tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

/// Reaches the voice with the owner's key. `None`: it could not be reached.
pub(crate) async fn dial(app: &App, key: &str) -> Option<Voice> {
    let address = match app.config.live_endpoint.as_str() {
        "" => VOICE_URL,
        stand_in => stand_in,
    };
    let mut request = address.into_client_request().ok()?;
    let token = format!("Bearer {key}").parse().ok()?;
    request.headers_mut().insert("Authorization", token);
    let trust = crate::connector::trust_of_the_web();
    let dial = tokio_tungstenite::connect_async_tls_with_config(request, None, true, trust);
    match tokio::time::timeout(VOICE_OPENS_WITHIN, dial).await {
        Ok(Ok((voice, _))) => Some(voice),
        _ => None,
    }
}

/// What a phone is told on the socket of a call: without a blank, as the Python Host writes
/// it there.
fn written(said: &Value) -> Told {
    Told::Text(python::compact_ascii(said))
}

/// The refusal of a socket that is not opened: one form, with the code and no `ok`.
fn refused(status: StatusCode, code: &str) -> Response {
    let mut response = (status, Json(json!({ "error": code }))).into_response();
    let close = axum::http::HeaderValue::from_static("close");
    response
        .headers_mut()
        .insert(axum::http::header::CONNECTION, close);
    response
}

/// `GET /calls/ring/live`: the socket of a live call. A paired phone with all rights opens
/// it, one at a time, and says its join first: the grant of its answer, which is held
/// against the ring and used up before the voice is reached.
async fn socket(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    socket: Socket,
) -> Response {
    if !socket.asked() {
        return refused(StatusCode::UPGRADE_REQUIRED, "live_websocket_required");
    }
    let Ok(caller) = caller(&principal) else {
        return refused(StatusCode::UNAUTHORIZED, "live_auth");
    };
    let paired = caller.starts_with("device_") && app.fleet.directory().can_receive(&caller);
    if !paired {
        return refused(StatusCode::FORBIDDEN, "live_auth");
    }
    let desk = app.calls.clone();
    // One phone holds one live call's socket: a second is never put in the first one's place.
    if !desk.claim_voice(&app, &caller).await {
        return refused(StatusCode::CONFLICT, "live_owner_conflict");
    }
    let held = Held {
        app: app.clone(),
        principal: caller.clone(),
    };
    socket.open(move |line| async move {
        carried(&app, line, &caller).await;
        drop(held);
    })
}

/// A phone's hold on the socket of a live call: let go of when the socket is done with,
/// however that comes about, also where it was never opened.
struct Held {
    app: App,
    principal: String,
}

impl Drop for Held {
    fn drop(&mut self) {
        let (app, principal) = (self.app.clone(), std::mem::take(&mut self.principal));
        tokio::spawn(async move { app.calls.release_voice(&principal).await });
    }
}

/// What a phone is told when its join is refused: the names it claimed, where they are well
/// made, and the code.
fn join_refused(event: &Value, code: &str) -> Value {
    let mut said = json!({ "type": "agent_live.error" });
    if let (Some(said), Value::Object(claimed)) = (said.as_object_mut(), rings::claimed(event)) {
        said.extend(claimed);
    }
    said["code"] = json!(code);
    said["message"] = json!(code);
    said["retryable"] = json!(false);
    said
}

/// The time a session counts by.
fn seconds(since: Instant) -> f64 {
    since.elapsed().as_secs_f64()
}

/// Carries one call on its socket: the join, then the session until it is over.
async fn carried(app: &App, mut line: Line, principal: &str) {
    let desk = app.calls.clone();
    // The join: the first thing said, within its time, and a text.
    let join_within = Duration::from_secs_f64(rules::JOIN_WITHIN_S);
    let first = tokio::time::timeout(join_within, line.heard()).await;
    let event: Result<Value, &'static str> = match first {
        Ok(Some(Heard::Text(text))) => {
            serde_json::from_str(&text).map_err(|_| "live_owner_conflict")
        }
        _ => Err("live_join_expired"),
    };
    let joined = match &event {
        Err(code) => Err((*code, Value::Null)),
        Ok(event) => {
            let joined = desk.rings(app, |rings, _, with, (out, _)| {
                rings.join(principal, event, &with.world, with.at.now_ms, out)
            });
            match joined.await {
                Ok(Ok(joined)) => Ok(joined),
                Ok(Err(code)) => Err((code, event.clone())),
                Err(_) => Err(("live_unavailable", event.clone())),
            }
        }
    };
    let joined = match joined {
        Ok(joined) => joined,
        Err((code, event)) => {
            line.tell(written(&join_refused(&event, code))).await;
            line.tell(Told::Close(ENDED_WELL, String::new())).await;
            return;
        }
    };
    session(app, &mut line, joined).await;
    line.tell(Told::Close(ENDED_WELL, String::new())).await;
}

/// One session, owned by this one task from the join to its end.
async fn session(app: &App, line: &mut Line, joined: Joined) {
    let desk = app.calls.clone();
    let began = Instant::now();
    let call_id = joined.call_id.clone();
    let call = (call_id.as_str(), joined.call_epoch);
    let principal = joined.principal.clone();
    let mut machine = Session::new(joined, &ids::hex(16), seconds(began));
    let (stops, mut stopped) = mpsc::unbounded_channel::<String>();
    let (is_over, over) = watch::channel(false);
    desk.began(&call_id, Running { stops, over }).await;
    // The voice is reached with the owner's key, where the Host has one.
    let key = app.ears.key("openai");
    let mut voice = match &key {
        Some(key) => dial(app, key).await,
        None => None,
    };
    let mut told = ToTell::default();
    let reached = (key.is_some(), voice.is_some());
    let host_id = app.server_id.clone();
    let started = desk.step(app, call, |book| {
        machine.start(reached, &host_id, book, &mut told)
    });
    if started.await.is_err() {
        told.said.push(Said::PhoneOver);
    }
    let mut tick = tokio::time::interval(Duration::from_secs_f64(rules::TICK_S));
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let idle = Duration::from_secs_f64(rules::PHONE_IDLE_S);
    let mut phone_spoke = Instant::now();
    let mut signs_seen = line.signs_of_life();
    loop {
        // What the step before this told is said now, in its order, the books let go of.
        for said in std::mem::take(&mut told.said) {
            match said {
                Said::Phone(said) => {
                    line.tell(written(&said)).await;
                }
                Said::Voice(said) => {
                    let sent = match voice.as_mut() {
                        Some(voice) => voice
                            .send(Message::Text(python::dumps_ascii(&said).into()))
                            .await
                            .is_ok(),
                        None => false,
                    };
                    told.voice_gone |= !sent;
                }
                Said::VoiceOver => {
                    if let Some(mut voice) = voice.take() {
                        let bye = async {
                            let _ = voice
                                .send(Message::Text(r#"{"type": "session.close"}"#.into()))
                                .await;
                            let _ = voice.close(None).await;
                        };
                        let _ = tokio::time::timeout(VOICE_ENDS_WITHIN, bye).await;
                    }
                }
                Said::PhoneOver => {}
            }
        }
        if machine.is_over() {
            break;
        }
        let now = seconds(began);
        let from_voice = async {
            match voice.as_mut() {
                Some(voice) => voice.next().await,
                None => std::future::pending().await,
            }
        };
        tokio::select! {
            biased;
            // A request ended the call on its ring: that is why the session is over.
            Some(reason) = stopped.recv() => machine.stopped(&mut told, &reason),
            said = from_voice => {
                let event = match said {
                    Some(Ok(Message::Text(text))) => serde_json::from_str::<Value>(text.as_str()).ok(),
                    Some(Ok(Message::Close(_)) | Err(_)) | None => {
                        voice = None;
                        told.voice_gone = true;
                        let gone = desk.step(app, call, |book| machine.voice_gone(book, &mut told));
                        let _ = gone.await;
                        continue;
                    }
                    Some(Ok(_)) => None,
                };
                if let Some(event) = event.filter(Value::is_object) {
                    let said = desk.step(app, call, |book| machine.voice(&event, book, &mut told, now));
                    let _ = said.await;
                }
            }
            heard = tokio::time::timeout_at(phone_spoke + idle, line.heard()) => {
                phone_spoke = Instant::now();
                // A sign of life since the last look: what it said, or its socket's own
                // asking whether the Host is there.
                let signs = line.signs_of_life();
                let alive = signs != signs_seen;
                signs_seen = signs;
                match heard {
                    Ok(Some(Heard::Text(text))) => {
                        let event = serde_json::from_str::<Value>(&text).ok();
                        if let Some(event) = event.filter(Value::is_object) {
                            let said = desk.step(app, call, |book| machine.phone(&event, book, &mut told, now));
                            let _ = said.await;
                        }
                    }
                    Ok(Some(Heard::Bytes(_))) => {}
                    // It said nothing for a while, and was there all the same: a phone whose
                    // microphone is paused asks now and then whether the Host is there.
                    Err(_) if alive => {}
                    // The phone is gone, or it gave no sign of life for too long.
                    Ok(None) | Err(_) => {
                        let gone = desk.step(app, call, |book| machine.phone_gone(book, &mut told));
                        let _ = gone.await;
                    }
                }
            }
            _ = tick.tick() => {
                // What waits of a call that was ended beside the session is taken first.
                if let Ok(reason) = stopped.try_recv() {
                    machine.stopped(&mut told, &reason);
                    continue;
                }
                let paired = app.fleet.directory().can_receive(&principal);
                let ticked = desk.step(app, call, |book| machine.tick(paired, book, &mut told, now));
                let _ = ticked.await;
            }
        }
    }
    drop(voice);
    desk.ended(&call_id).await;
    let _ = is_over.send(true);
}

pub(super) fn routes(router: Router<App>) -> Router<App> {
    router
        .route("/calls/live", post(user_call))
        .route("/calls/ring/live", get(socket))
        .route("/calls/ring/live/close", post(close))
        .route("/calls/ring/live/fallback", post(fallback))
        .route("/calls/ring/device", get(receiving).post(receive))
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    /// A folder of a test's own, with a folder of profiles in it.
    fn scratch(name: &str) -> (PathBuf, PathBuf) {
        let root = std::env::temp_dir().join(format!("clarp-live-{name}-{}", ids::hex(6)));
        let profiles = root.join("profiles");
        std::fs::create_dir_all(&profiles).unwrap();
        (root, profiles)
    }

    /// A profile that takes the calls of one agent, in a folder of its name.
    fn profile(folder: &std::path::Path, name: &str, session: &str) {
        std::fs::create_dir_all(folder).unwrap();
        let held = json!({
            "profile_id": name, "agent_session": session, "user_call_default": "live",
        });
        std::fs::write(folder.join("profile.json"), held.to_string()).unwrap();
        std::fs::write(folder.join("PERSONA.md"), "You guard the pumps.\n").unwrap();
    }

    #[test]
    fn a_profile_is_read_from_its_own_folder_and_no_link_leads_out_of_it() {
        let (root, profiles) = scratch("files");
        profile(&profiles.join("pump-guard"), "pump-guard", "theo-97e5");
        // Beside the profiles: what is no profile's to read, and a folder that is none of
        // theirs with a file like a profile's in it.
        std::fs::write(root.join("secret.md"), "not for a voice\n").unwrap();
        profile(&root.join("outside"), "outside", "mike-86db");
        let own = profiles.join("pump-guard");
        std::os::unix::fs::symlink(root.join("secret.md"), own.join("OUT.md")).unwrap();
        std::os::unix::fs::symlink(own.join("PERSONA.md"), own.join("SAME.md")).unwrap();
        let shelf = Shelf::at(profiles, None);
        let persona = Some(b"You guard the pumps.\n".to_vec());
        assert_eq!(shelf.file("pump-guard", "PERSONA.md"), persona);
        // A link that stays in the profile is the file it leads to; one that leaves is none.
        assert_eq!(shelf.file("pump-guard", "SAME.md"), persona);
        assert_eq!(shelf.file("pump-guard", "OUT.md"), None);
        assert_eq!(shelf.file("pump-guard", "../../secret.md"), None);
        assert_eq!(shelf.file("pump-guard", "GONE.md"), None);
        // What is no name of a profile is no folder to look into, whatever is there.
        assert!(shelf.profile("pump-guard").is_some());
        assert!(root.join("profiles/../outside/profile.json").exists());
        assert_eq!(shelf.profile("../outside"), None);
        assert_eq!(shelf.file("../outside", "PERSONA.md"), None);
        assert_eq!(shelf.profile("none-such"), None);
        std::fs::remove_dir_all(root).unwrap();
    }

    #[test]
    fn the_agents_that_take_calls_are_found_among_the_folders_and_a_link_is_none() {
        let (root, profiles) = scratch("taking");
        // No folder of profiles at all: nobody takes a call, and that is certain.
        let nobody = Shelf::at(root.join("none"), None);
        assert_eq!(nobody.takes_calls("theo-97e5"), Ok(None));
        profile(&profiles.join("pump-guard"), "pump-guard", "theo-97e5");
        let shelf = Shelf::at(profiles.clone(), None);
        assert_eq!(
            shelf.takes_calls("theo-97e5"),
            Ok(Some("pump-guard".to_string()))
        );
        assert_eq!(shelf.takes_calls("mike-86db"), Ok(None));
        // Who takes calls is to be kept, once it is known.
        assert_eq!(shelf.to_keep(), Some(r#"["theo-97e5"]"#.to_string()));
        // A link to a folder is no folder of the profiles: the profile behind it takes no
        // call, and while it is there not all of the folder is known, so that no call is
        // placed on what was found.
        profile(&root.join("elsewhere"), "mike-desk", "mike-86db");
        std::os::unix::fs::symlink(root.join("elsewhere"), profiles.join("mike-desk")).unwrap();
        let shelf = Shelf::at(profiles, Some(r#"["theo-97e5"]"#.to_string()));
        let found = shelf.taking(false).takes.unwrap();
        let taken: Vec<(&str, Option<&str>)> = found
            .iter()
            .map(|(session, profile)| (session.as_str(), profile.as_deref()))
            .collect();
        assert_eq!(taken, [("theo-97e5", Some("pump-guard"))]);
        assert_eq!(shelf.takes_calls("theo-97e5").unwrap_err().status, 409);
        std::fs::remove_dir_all(root).unwrap();
    }
}
