//! A call of the Oracle, on its socket (`GET /oracle/v2`).
//!
//! The rules of a call are the kernel's: the conversation between a phone and a voice
//! ([`clarp_kernel::oracle::conversation`]), the routing of what the user asked
//! ([`clarp_kernel::oracle::routing`]), the memory of the thread a call belongs to
//! ([`clarp_kernel::oracle::threads`]) and the tools of the fleet
//! ([`clarp_kernel::oracle::tools`]). Here is what a Host adds: who may open the socket and
//! how, one call a phone at a time, the voice and the router reached with the owner's key,
//! and one task that owns the conversation of a call and stands between its phone and its
//! voice. What takes time beside the talk (the router's thinking, a tool at work, a file
//! being written) is done apart and comes back to that task as something that happened, so
//! that sound goes both ways all the while.
//!
//! The voice is a provider's, reached over a socket of its own; the router is a provider's,
//! asked over the web. In a test both are stand-ins on this machine. The socket answers as
//! the Python Host's does (`oracle_live_stable.serve`).

use std::collections::HashMap;
use std::sync::{Mutex, MutexGuard};
use std::time::Duration;

use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::Response;
use axum::routing::get;
use axum::{Extension, Router};
use clarp_kernel::oracle::conversation::{
    Begin, Conversation, Ids, Now, Phoned, Strategy, Thread, Waiting, Wire,
};
use clarp_kernel::oracle::delegations::Delegation;
use clarp_kernel::oracle::handoffs::{self as handoff_rules, ToCall};
use clarp_kernel::oracle::routing::{self, Fed, Route, Step};
use clarp_kernel::oracle::threads::{self, Excerpt, Opened, Opening};
use clarp_kernel::oracle::voice::{self, Beginning};
use clarp_kernel::oracle::{self as rules, CONTACT_KEY, attention, phone, tools as tool_rules};
use clarp_kernel::python;
use futures_util::{SinkExt, StreamExt};
use serde_json::{Value, json};
use tokio::sync::{Notify, mpsc};
use tokio::time::Instant;
use tokio_tungstenite::tungstenite::Message;

use super::memory::with_memory;
use super::{agents, delegations, handoffs, keep, router, tools};
use crate::calls::{Voice, caller, dial};
use crate::edge::{App, Principal};
use crate::ids;
use crate::library;
use crate::line::{self, ENDED_WELL, Heard, Line, Socket, Told};
use crate::owner::OwnerFile;

/// How long a call that a phone still has is given to end, when the phone opens another.
const TAKES_OVER_WITHIN: Duration = Duration::from_secs(3);
/// How long a phone may say nothing: it streams its microphone, and asks whether the Host
/// is there while that is paused.
const PHONE_IDLE: Duration = Duration::from_secs(30);
/// How often time is told to pass in a call.
const TICK: Duration = Duration::from_millis(250);
/// How long the voice is given to say that it took the end of the call.
const VOICE_ENDS_WITHIN: Duration = Duration::from_secs(2);
const HANDOFF_FILES: &str = "oracle-handoffs";

// ---- one call a phone ------------------------------------------------------------------

/// A call that is open: the way to tell it to stop once its session runs, and whether it
/// is ending.
#[derive(Default)]
struct Open {
    stop: Option<mpsc::UnboundedSender<()>>,
    closing: bool,
}

/// The calls of the Oracle that are open, by the phone each is of. A phone has one voice at
/// a time, whatever it speaks with ([`crate::calls`] keeps whose voices are taken).
#[derive(Default)]
pub struct Calls {
    open: Mutex<HashMap<String, Open>>,
    freed: Notify,
}

impl Calls {
    fn open(&self) -> MutexGuard<'_, HashMap<String, Open>> {
        self.open
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }

    /// A phone takes the voice of a call. One phone runs one client, so a call it still has
    /// is stale: a dead path, an app that was put away. That call is told to stop and waited
    /// for, for a while; one that is ending is only waited for. A voice that another kind of
    /// call has, or a call that is not under way yet, is left alone. `None`: the phone has a
    /// voice. Whoever is given the claim lets go of it ([`Claimed`]).
    async fn claim(&self, app: &App, principal: &str) -> Option<Claimed> {
        let deadline = Instant::now() + TAKES_OVER_WITHIN;
        loop {
            let freed = self.freed.notified();
            tokio::pin!(freed);
            freed.as_mut().enable();
            if app.calls.claim_voice(app, principal).await {
                self.open().insert(principal.to_string(), Open::default());
                return Some(Claimed {
                    app: app.clone(),
                    principal: principal.to_string(),
                });
            }
            match self.open().get_mut(principal) {
                Some(held) if held.closing => {}
                Some(held) => {
                    held.stop.as_ref()?.send(()).ok();
                }
                None => return None,
            }
            tokio::time::timeout_at(deadline, freed).await.ok()?;
        }
    }

    /// The session of a call runs: from now on a later call of the phone can end it.
    fn runs(&self, principal: &str, stop: mpsc::UnboundedSender<()>) {
        if let Some(held) = self.open().get_mut(principal) {
            held.stop = Some(stop);
        }
    }

    /// The call is ending by itself: a later call of the phone waits for it, and does not
    /// tell it to stop.
    fn closing(&self, principal: &str) {
        if let Some(held) = self.open().get_mut(principal) {
            held.closing = true;
        }
    }
}

/// A phone's claim on the voice of a call: let go of when the request is done with, however
/// that comes about, also where no socket was opened for it. From that moment the call is
/// ending, and a later call of the phone waits for the voice to be free.
struct Claimed {
    app: App,
    principal: String,
}

impl Drop for Claimed {
    fn drop(&mut self) {
        let (app, principal) = (self.app.clone(), std::mem::take(&mut self.principal));
        app.oracle.calls.closing(&principal);
        tokio::spawn(async move {
            app.oracle.calls.open().remove(&principal);
            app.calls.release_voice(&principal).await;
            app.oracle.calls.freed.notify_waiters();
        });
    }
}

// ---- what a phone asks a call with -----------------------------------------------------

/// What a phone asked its call with, as the Host takes it.
struct Asked {
    strategy: Strategy,
    /// The Oracle's contact for this call, where there is one.
    contact: String,
    thread_id: Option<String>,
    fresh: bool,
    /// Its phone takes handoffs: the call can be put through to an agent.
    handoff: bool,
    /// The handoff this call is the way back of, when it is one.
    handoff_id: String,
}

/// The values a request names a thing with, in their order: a thing may be named more than
/// once, and some may not.
fn named<'a>(query: &'a [(String, String)], name: &str) -> Vec<&'a str> {
    let of_name = query.iter().filter(|(key, _)| key == name);
    of_name.map(|(_, value)| value.as_str()).collect()
}

impl Asked {
    /// Reads what a call is asked with. `Err`: why it cannot be a call, in the words a
    /// phone is told.
    async fn read(app: &App, query: &[(String, String)]) -> Result<Self, String> {
        let strategy = match named(query, "delegation_strategy").as_slice() {
            [] => Strategy::Operator,
            [one] => Strategy::named(Some(one)).ok_or("Unsupported Oracle delegation strategy")?,
            _ => return Err("Choose one Oracle delegation strategy".to_string()),
        };
        let wanted = named(query, "oracle_session")
            .first()
            .copied()
            .unwrap_or("");
        let (contact, let_go) = {
            let directory = app.fleet.directory();
            let kept = directory.setting(CONTACT_KEY).unwrap_or("");
            let agents = agents(&directory);
            match strategy {
                Strategy::Operator => rules::contact_of_a_call(kept, &json!(wanted), &agents),
                Strategy::DirectContact => {
                    let (contact, let_go) =
                        rules::contact_of_a_direct_call(kept, &json!(wanted), &agents);
                    (contact?, let_go)
                }
            }
        };
        if let_go {
            // A contact that names no agent any more is let go, as when it is asked for.
            let _ = keep(app, CONTACT_KEY, String::new()).await;
        }
        if !named(query, "podcast_artifact").is_empty() {
            return Err("Podcast detours are not available on this Host".to_string());
        }
        for name in ["thread_id", "new_conversation"] {
            if named(query, name).len() > 1 {
                return Err("Use one Oracle context selection".to_string());
            }
        }
        let fresh = match named(query, "new_conversation").first() {
            None | Some(&"0") => false,
            Some(&"1") => true,
            Some(_) => return Err("new_conversation must be 0 or 1".to_string()),
        };
        for name in ["handoff", "handoff_id"] {
            if named(query, name).len() > 1 {
                return Err(format!("Use one {name}"));
            }
        }
        if named(query, "handoff")
            .first()
            .is_some_and(|how| *how != "hands_free")
        {
            return Err("handoff must be hands_free".to_string());
        }
        // The handoff a call comes back from is named as a handoff is, or the call is none.
        let handoff_id = named(query, "handoff_id").first().copied().unwrap_or("");
        if !handoff_id.is_empty() && !phone::handoff_id(handoff_id) {
            return Err("Invalid handoff_id".to_string());
        }
        Ok(Self {
            strategy,
            contact,
            thread_id: named(query, "thread_id")
                .first()
                .map(|id| (*id).to_string()),
            fresh,
            handoff: !named(query, "handoff").is_empty(),
            handoff_id: handoff_id.to_string(),
        })
    }
}

/// `GET /oracle/v2`: the socket of a call of the Oracle. A phone with all rights opens it,
/// one call at a time; the thread the call goes on in is taken and the voice is reached
/// before the socket is opened, so that a call that cannot be is refused in plain words.
async fn socket(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Query(query): Query<Vec<(String, String)>>,
    socket: Socket,
) -> Response {
    if !socket.asked() {
        return line::refused(StatusCode::UPGRADE_REQUIRED, "WebSocket upgrade required");
    }
    let Ok(owner) = caller(&principal) else {
        let words = "Oracle v2 requires full-device authentication";
        return line::refused(StatusCode::UNAUTHORIZED, words);
    };
    let Some(key) = app.ears.key("openai") else {
        let words = "Oracle v2 needs an OpenAI key on this Host";
        return line::refused(StatusCode::SERVICE_UNAVAILABLE, words);
    };
    let asked = match Asked::read(&app, &query).await {
        Ok(asked) => asked,
        Err(why) => return line::refused(StatusCode::BAD_REQUEST, &why),
    };
    let Some(claimed) = app.oracle.calls.claim(&app, &owner).await else {
        return line::refused(StatusCode::CONFLICT, "An Oracle session is already active");
    };
    // The name of this connection: the thread of the call is its own under it.
    let token = ids::hex(16);
    let (thread_id, voice) = match reached(&app, (&owner, &token), &asked, &key).await {
        Ok(reached) => reached,
        Err((status, why)) => return line::refused(status, why),
    };
    let call = Call {
        owner,
        token,
        thread_id,
        key,
        asked,
    };
    socket.open(move |line| async move {
        carried(&app, line, voice, call).await;
        drop(claimed);
    })
}

/// The thread a call goes on in is taken for its connection, and its voice is reached.
/// `Err`: what the phone is answered instead of a call, and in which words.
async fn reached(
    app: &App,
    (owner, token): (&str, &str),
    asked: &Asked,
    key: &str,
) -> Result<(String, Voice), (StatusCode, &'static str)> {
    const UNAVAILABLE: (StatusCode, &str) =
        (StatusCode::BAD_GATEWAY, "Oracle v2 upstream unavailable");
    let new_id = format!("oracle-{}", ids::hex(16));
    let now_ms = app.clock.now_ms();
    let opened = with_memory(app, |memory| {
        let opening = Opening {
            owner,
            contact: &asked.contact,
            thread_id: asked.thread_id.as_deref(),
            connection: token,
            fresh: asked.fresh,
            new_id: &new_id,
        };
        memory.open(&opening, now_ms)
    });
    let thread_id = match opened.await {
        Ok(Ok(thread_id)) => thread_id,
        Ok(Err(why)) => return Err((StatusCode::BAD_REQUEST, why)),
        Err(_) => return Err(UNAVAILABLE),
    };
    let voice = dial(app, key).await.ok_or(UNAVAILABLE)?;
    Ok((thread_id, voice))
}

// ---- the session of a call -------------------------------------------------------------

/// What a call is: whose, on which connection, in which thread, and what it was asked with.
struct Call {
    owner: String,
    token: String,
    thread_id: String,
    key: String,
    asked: Asked,
}

/// What a conversation tells the two sides of its call, in its order.
#[derive(Default)]
struct Said(Vec<(bool, Value)>);

impl Wire for Said {
    fn voice(&mut self, said: Value) {
        self.0.push((true, said));
    }

    fn phone(&mut self, said: Value) {
        self.0.push((false, said));
    }
}

/// Something that was done apart from the talk, for the routing that waits for it.
enum Came {
    /// The user may have been quiet for long enough.
    Quiet,
    /// What the router answered; nothing where it did not.
    Answer(Option<Value>),
    /// Where the file of where a request came from was written; nothing where it was not.
    Written(Option<String>),
    /// What came of a tool.
    Done(tools::Done),
}

/// How a call ended.
enum Ending {
    /// The phone went away; or the thread of the call is another call's now.
    Left,
    /// The voice ended it, or went away.
    VoiceGone,
    /// A later call of the same phone took its place: the voice is let go without a word.
    Replaced,
}

/// The one task of a call: everything of the call that changes is here.
struct Session<'a> {
    app: &'a App,
    call: &'a Call,
    machine: Conversation,
    began: Instant,
    said: Said,
    /// The routing that is under way, and the place of the action it waits for.
    route: Option<Route>,
    doing: Option<(i64, usize)>,
    came: mpsc::UnboundedSender<Came>,
}

impl Session<'_> {
    fn now(&self) -> Now {
        let ms = i64::try_from(self.began.elapsed().as_millis()).unwrap_or(i64::MAX);
        Now {
            ms,
            wall_ms: self.app.clock.now_ms(),
        }
    }

    fn opened(&self) -> Opened<'_> {
        Opened {
            thread_id: &self.call.thread_id,
            owner: &self.call.owner,
            connection: &self.call.token,
        }
    }

    /// The delegations of this call, as the book has them now.
    async fn rows(&self) -> Vec<Delegation> {
        if self.machine.owned().is_empty() {
            return Vec::new();
        }
        let rows = delegations::rows_of(self.app, self.machine.owned()).await;
        rows.unwrap_or_default()
    }

    /// What the conversation said to keep of the talk is kept. `false`: the thread is no
    /// longer this call's, and the call is over.
    async fn keeps(&mut self) -> bool {
        let Some(state) = self.machine.to_keep() else {
            return true;
        };
        let (opened, now_ms) = (self.opened(), self.app.clock.now_ms());
        let kept = with_memory(self.app, |memory| memory.save(opened, &state, now_ms));
        matches!(kept.await, Ok(Ok(())))
    }

    /// The actions the routing began are written down. `acts`: and something is about to be
    /// done for one, which is done only for the call that still has its thread. `false`: an
    /// action was not taken as begun, or the thread is another call's now.
    async fn admits(&mut self, acts: bool) -> bool {
        let begun = self.machine.begun();
        if begun.is_empty() && !acts {
            return true;
        }
        let (opened, now_ms) = (self.opened(), self.app.clock.now_ms());
        let taken = with_memory(self.app, |memory| {
            for action in &begun {
                let handed: Value = serde_json::from_str(&action.arguments).unwrap_or(Value::Null);
                let place = (action.revision, action.index);
                memory.admission(opened, place, (&action.tool, &handed), now_ms)?;
            }
            Ok::<bool, &'static str>(memory.has(opened))
        });
        matches!(taken.await, Ok(Ok(true)))
    }

    /// One step of the routing that is under way, and what it needs next is set going.
    async fn routed(&mut self, fed: Fed<'_>) {
        let Some(mut route) = self.route.take() else {
            return;
        };
        let (rows, now) = (self.rows().await, self.now());
        let mut step = self
            .machine
            .route_step(&mut route, fed, &rows, &mut self.said, now);
        // An action is written down as begun before anything is done for it. One that is
        // not taken as begun fails its routing, and the call goes on.
        let acts = matches!(step, Step::Write(_) | Step::Do { .. });
        if !self.admits(acts).await && acts {
            let failed = Fed::Failed;
            step = self
                .machine
                .route_step(&mut route, failed, &rows, &mut self.said, now);
        }
        let (app, came) = (self.app.clone(), self.came.clone());
        match step {
            Step::Over => return,
            // Whether the user has been quiet for long enough is looked at where it comes
            // back.
            Step::Wait => {
                let _ = came.send(Came::Quiet);
            }
            Step::Ask(question) => {
                let key = self.call.key.clone();
                tokio::spawn(async move {
                    let answer = router::asked(&app, &key, routing::request(&question)).await;
                    let _ = came.send(Came::Answer(answer));
                });
            }
            Step::Write(provenance) => {
                let folder = app.config.data_dir.join("media").join(HANDOFF_FILES);
                tokio::spawn(async move {
                    let written =
                        tokio::task::spawn_blocking(move || written(&folder, &provenance));
                    let _ = came.send(Came::Written(written.await.ok().flatten()));
                });
            }
            Step::Do {
                name,
                arguments,
                call_id,
                admitted,
            } => {
                self.doing = Some(admitted);
                let who = (self.call.owner.clone(), self.call.asked.contact.clone());
                tokio::spawn(async move {
                    let wanted = (name.as_str(), &arguments, call_id.as_str());
                    let done = tools::execute(&app, (&who.0, &who.1), wanted).await;
                    let _ = came.send(Came::Done(done));
                });
            }
        }
        self.route = Some(route);
    }

    /// Something that was done apart came back.
    async fn came(&mut self, came: Came) {
        match came {
            Came::Quiet => {
                let now = self.now();
                if now.ms < self.machine.quiet_at() {
                    // The user spoke again meanwhile: it is waited once more.
                    let wait = u64::try_from(self.machine.quiet_at() - now.ms).unwrap_or(0);
                    let came = self.came.clone();
                    tokio::spawn(async move {
                        tokio::time::sleep(Duration::from_millis(wait)).await;
                        let _ = came.send(Came::Quiet);
                    });
                    return;
                }
                let (roster, contact_there) = {
                    let directory = self.app.fleet.directory();
                    let agents = tools::agents(&directory);
                    let contact = self.call.asked.contact.as_str();
                    let there = tool_rules::resolve(&agents, &json!(contact)).is_ok();
                    (tool_rules::list_agents(&agents, contact), there)
                };
                let quiet = Fed::Quiet {
                    roster: &roster,
                    contact_there,
                };
                self.routed(quiet).await;
            }
            Came::Answer(Some(answer)) => self.routed(Fed::Answer(&answer)).await,
            Came::Written(Some(path)) => self.routed(Fed::Written(&path)).await,
            Came::Answer(None) | Came::Written(None) => self.routed(Fed::Failed).await,
            Came::Done(done) => {
                // What an agent was told to leave is not told as a result of this call,
                // whatever else came of the tool.
                if let Some(session) = &done.left {
                    self.machine.supersede(session);
                }
                let place = self.doing.take();
                let (Some(place), Some(receipt)) = (place, done.receipt) else {
                    return self.routed(Fed::Failed).await;
                };
                // The action is ended with what its tool answered, before the voice is told.
                let (opened, now_ms) = (self.opened(), self.app.clock.now_ms());
                let book = delegations::all(self.app).await.unwrap_or_default();
                let finished = with_memory(self.app, |memory| {
                    let owner_of = |operation: &str| {
                        let found = book.iter().find(|row| row.delegation_id == operation);
                        found.map(|row| row.owner_principal.clone())
                    };
                    memory.finish(opened, place, &receipt, &owner_of, now_ms)
                });
                match finished.await {
                    Ok(Ok(())) => self.routed(Fed::Receipt(&receipt)).await,
                    _ => self.routed(Fed::Failed).await,
                }
            }
        }
    }

    /// Something the desk of the handoffs says to tell this call.
    fn handoff(&mut self, what: ToCall) {
        let now = self.now();
        let (machine, said) = (&mut self.machine, &mut self.said);
        match what {
            ToCall::Session(frame) => said.phone(frame),
            ToCall::Offer(persona) => machine.handoff_offer(&persona, said, now),
            ToCall::Mirror(record) => machine.handoff_mirror(&record, said),
            ToCall::Hold => machine.handoff_hold(),
            ToCall::Resume => machine.handoff_resume(),
            ToCall::Failed {
                persona,
                reason,
                note,
            } => machine.handoff_failed((&persona, &reason, note), said, now),
            ToCall::Returned(persona) => machine.handoff_returned(&persona, said, now),
        }
    }

    /// Time passes: what waits for the user is read where the call says it is the moment.
    async fn ticked(&mut self) {
        let now = self.now();
        let looks = self.machine.tick(&mut self.said, now);
        let waits = looks.then(|| {
            let listed = library::inbox(self.app, true, false);
            let listed = listed["items"].as_array().cloned().unwrap_or_default();
            let decisions = attention::pending(&listed, attention::PENDING_MOST);
            // What each agent last finished for its user, as its own thread has it.
            let directory = self.app.fleet.directory();
            let views = directory.members.iter().map(|member| member.agent.view());
            let views: Vec<_> = views.collect();
            let had = views.iter().filter_map(|view| view.finished.as_deref());
            let at = self.app.clock.now_ms();
            let finished = attention::finished(had, at, attention::PENDING_MOST);
            (decisions, finished)
        });
        let waiting = waits.as_ref().map(|(decisions, completions)| Waiting {
            decisions,
            completions,
        });
        let rows = self.rows().await;
        let claim = self.machine.ticked(&rows, waiting, &mut self.said, now);
        if let Some(claim) = claim {
            let (opened, now_ms) = (self.opened(), self.app.clock.now_ms());
            let told = with_memory(self.app, |memory| {
                let what = (claim.kind, claim.id.as_str());
                memory.claim(opened, what, (claim.reference_ts, false), now_ms)
            });
            let _ = told.await;
        }
    }
}

/// The name of the zone the clocks of this machine show: what it was told (`TZ`), else what
/// its own time is linked to. Nothing where neither names one: the world's own time.
fn zone_of_this_machine() -> String {
    let told = std::env::var("TZ").unwrap_or_default();
    let told = told.trim_start_matches(':');
    if !told.is_empty() {
        return told.to_string();
    }
    let linked = std::fs::read_link("/etc/localtime").unwrap_or_default();
    let linked = linked.to_string_lossy();
    let name = linked.split_once("zoneinfo/").map_or("", |(_, name)| name);
    name.to_string()
}

/// Writes where a request came from, for the agent that is handed the request: a file of
/// its own in a folder only the owner reads. Says where it is.
fn written(folder: &std::path::Path, provenance: &Value) -> Option<String> {
    use std::io::Write;
    use std::os::unix::fs::{DirBuilderExt, OpenOptionsExt};
    let mut made = std::fs::DirBuilder::new();
    made.recursive(true).mode(0o700).create(folder).ok()?;
    let file = folder.join(format!("handoff-{}.json", ids::hex(8)));
    let mut options = std::fs::OpenOptions::new();
    let mut open = options
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(&file)
        .ok()?;
    open.write_all(python::dumps(provenance).as_bytes()).ok()?;
    Some(file.to_string_lossy().into_owned())
}

/// What a phone is told when the voice of its call is gone without a word.
fn voice_lost() -> Value {
    json!({ "type": "error", "error": { "message": "Oracle v2 disconnected" } })
}

/// Carries one call on its socket, from the voice's first word to the end, and lets the
/// thread and the voice go as a call is to leave them.
async fn carried(app: &App, mut line: Line, mut voice: Voice, call: Call) {
    let (stops, mut stopped) = mpsc::unbounded_channel::<()>();
    app.oracle.calls.runs(&call.owner, stops);
    let (came, mut comes) = mpsc::unbounded_channel::<Came>();
    let opened = Opened {
        thread_id: &call.thread_id,
        owner: &call.owner,
        connection: &call.token,
    };
    // What was kept of the thread, with what its lost receipts are known to have come to.
    let book = delegations::all(app).await.unwrap_or_default();
    let now_ms = app.clock.now_ms();
    let zone = threads::zone_named(&zone_of_this_machine());
    // What the owner wrote for the Oracle: the languages its user speaks, and whether it
    // plays cues.
    let owners = OwnerFile::new(app.config.speech_keys.clone());
    let file = owners.read().unwrap_or_default();
    let written = file.get("oracle").and_then(|oracle| oracle.as_table());
    let of_oracle = |name: &str| written.and_then(|oracle| oracle.get(name));
    let codes = of_oracle("languages").and_then(|codes| serde_json::to_value(codes).ok());
    let languages = voice::languages(&codes.unwrap_or(Value::Null));
    let no_cues = of_oracle("earcons").and_then(toml::Value::as_bool) == Some(false);
    let roster = {
        let directory = app.fleet.directory();
        tool_rules::list_agents(&tools::agents(&directory), &call.asked.contact)
    };
    let direct = call.asked.strategy == Strategy::DirectContact;
    let begun = with_memory(app, |memory| {
        let _ = memory.reconcile(opened, &book, now_ms);
        let saved = memory.load(opened).unwrap_or(Value::Null);
        let work = memory.work(opened, &book).unwrap_or_default();
        let told = memory.told_of(opened.thread_id);
        let excerpt = Excerpt {
            thread_id: opened.thread_id,
            state: &saved,
            work: &work,
            contexts: &[],
            roster: &roster,
        };
        let history = threads::history(&excerpt, now_ms, &zone);
        let work: Vec<String> = work.iter().map(|row| row.delegation_id.clone()).collect();
        let thread = Thread {
            thread_id: opened.thread_id,
            saved: &saved,
            work: &work,
            contexts: &[],
            told: &told,
        };
        let begin = Begin {
            provider_session: opened.connection,
            principal: opened.owner,
            strategy: call.asked.strategy,
            contact: &call.asked.contact,
            direct_prefix: &ids::hex(16),
            thread,
            ids: Ids::new(&ids::hex(12)),
        };
        (Conversation::begin(begin, now_ms), history)
    });
    let Ok((mut machine, history)) = begun.await else {
        line.tell(Told::Close(ENDED_WELL, String::new())).await;
        return;
    };
    if no_cues {
        machine.without_earcons();
    }
    let mut session = Session {
        app,
        call: &call,
        machine,
        began: Instant::now(),
        said: Said::default(),
        route: None,
        doing: None,
        came,
    };
    // From here on the call can be put through to an agent, where its phone asked for that:
    // the phone is told which call this is before anything else of it, and before the voice
    // is told that the call begins.
    let known = handoff_rules::Call {
        principal: call.owner.clone(),
        voice_session_id: call.token.clone(),
        provider_session: call.token.clone(),
        thread_id: call.thread_id.clone(),
        capable: call.asked.handoff,
        handoff_id: call.asked.handoff_id.clone(),
        held: false,
    };
    let mut handoff_tells = handoffs::opened(app, known).await;
    while let Ok(what) = handoff_tells.try_recv() {
        session.handoff(what);
    }
    if direct {
        // A phone that asked for it is told that no router is asked in this call.
        session.said.phone(json!({
            "type": "oracle_v2.routing_mode",
            "strategy": call.asked.strategy.word(),
            "primary_contact": call.asked.contact,
            "operator_model_called": false,
        }));
    }
    let beginning = Beginning {
        roster: Some(&roster),
        direct,
        vocabulary: None,
        history: history.as_array().map_or(&[][..], Vec::as_slice),
        languages: &languages,
    };
    session
        .said
        .voice(json!({ "type": "session.start", "session": voice::beginning(&beginning) }));
    let mut tick = tokio::time::interval(TICK);
    tick.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Delay);
    let mut phone_spoke = Instant::now();
    let mut signs_seen = line.signs_of_life();
    let ending = loop {
        // What the step before this told is said now, in its order; then what it said to
        // keep is kept.
        let mut voice_gone = false;
        for (to_voice, said) in std::mem::take(&mut session.said.0) {
            let written = python::dumps_ascii(&said);
            if to_voice {
                voice_gone |= voice.send(Message::Text(written.into())).await.is_err();
            } else {
                line.tell(Told::Text(written)).await;
            }
        }
        if voice_gone {
            line.tell(Told::Text(python::dumps_ascii(&voice_lost())))
                .await;
            break Ending::VoiceGone;
        }
        if session.machine.closed() {
            // The voice said that the call is over.
            break Ending::VoiceGone;
        }
        if !session.keeps().await {
            break Ending::Left;
        }
        // One routing at a time: the next thing the voice wanted is taken up when the one
        // before is over.
        if session.route.is_none()
            && let Some(ident) = session.machine.next_wanted()
        {
            session.route = Some(session.machine.route(&ident));
            session.routed(Fed::Begin).await;
            continue;
        }
        tokio::select! {
            biased;
            // A later call of the same phone takes this one's place.
            Some(()) = stopped.recv() => break Ending::Replaced,
            Some(came) = comes.recv() => session.came(came).await,
            // What became of a handoff of this call, as its desk says to tell it.
            Some(what) = handoff_tells.recv() => session.handoff(what),
            said = voice.next() => {
                let event = match said {
                    Some(Ok(Message::Text(text))) => serde_json::from_str::<Value>(text.as_str()).ok(),
                    // The voice ended the call in its own way: nothing more is said.
                    Some(Ok(Message::Close(_))) => break Ending::VoiceGone,
                    Some(Err(_)) | None => {
                        line.tell(Told::Text(python::dumps_ascii(&voice_lost()))).await;
                        break Ending::VoiceGone;
                    }
                    Some(Ok(_)) => None,
                };
                if let Some(event) = event.filter(Value::is_object) {
                    let now = session.now();
                    session.machine.voice(&event, &mut session.said, now);
                }
            }
            heard = tokio::time::timeout_at(phone_spoke + PHONE_IDLE, line.heard()) => {
                phone_spoke = Instant::now();
                let signs = line.signs_of_life();
                let alive = signs != signs_seen;
                signs_seen = signs;
                match heard {
                    Ok(Some(Heard::Text(text))) => {
                        let now = session.now();
                        let phoned = session.machine.phone(&text, &mut session.said, now);
                        if matches!(phoned, Phoned::Close { .. }) {
                            app.oracle.calls.closing(&call.owner);
                        }
                    }
                    Ok(Some(Heard::Bytes(_))) => {}
                    // It said nothing for a while, and was there all the same.
                    Err(_) if alive => {}
                    // The phone is gone, or it gave no sign of life for too long.
                    Ok(None) | Err(_) => break Ending::Left,
                }
            }
            _ = tick.tick() => session.ticked().await,
        }
    };
    if matches!(ending, Ending::Left) {
        // The voice is told that the call is over, and given a moment to say that it took it.
        let bye = async {
            let _ = voice
                .send(Message::Text(r#"{"type": "session.close"}"#.into()))
                .await;
            while let Some(Ok(said)) = voice.next().await {
                let closed = matches!(&said, Message::Text(text) if text.as_str().contains("session.closed"));
                if closed || matches!(said, Message::Close(_)) {
                    break;
                }
            }
        };
        let _ = tokio::time::timeout(VOICE_ENDS_WITHIN, bye).await;
    }
    if !matches!(ending, Ending::Replaced) {
        // The end of the call is kept with its thread: a call that begins soon goes on as
        // this one.
        let now = session.now();
        session.machine.ends(now);
        let _ = session.keeps().await;
    }
    let _ = voice.close(None).await;
    line.tell(Told::Close(ENDED_WELL, String::new())).await;
    handoffs::closed(app, &call.owner, &call.token).await;
}

pub(super) fn routes() -> Router<App> {
    Router::new().route("/oracle/v2", get(socket))
}
