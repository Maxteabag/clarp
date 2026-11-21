//! A call of the Oracle put through to an agent, and back, on this Host.
//!
//! The record of a handoff and every rule of it are the kernel's
//! ([`clarp_kernel::oracle::handoffs`]). Here the record is kept, a book a phone, with the
//! fleet: read when it is first asked for, which is when the Host starts, and written
//! before anybody is told. One request has the desk at a time, and what the desk says to
//! tell is told before the next has it: the calls that are open by the way to their own
//! tasks, the apps on the stream of the Host's events, and the Host's focus as `POST
//! /select` moves it. One task runs the deadlines. The three doors of the contract
//! (`docs/oracle-handoff.md`) answer as the Python Host's (`server/lib/routes/oracle.py`).

use std::collections::{BTreeMap, HashMap};
use std::sync::{Mutex, MutexGuard};
use std::time::Duration;

use axum::body::Bytes;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use clarp_kernel::oracle::handoffs::{
    ADMINISTRATOR, Agent, Book, CONNECT_WAITS_MS, Call, Desk, Effect, Refused, ToCall,
};
use clarp_kernel::{oracle, python};
use serde_json::Value;
use tokio::sync::{Notify, mpsc};

use crate::agent::Unavailable;
use crate::calls::caller;
use crate::edge::{ApiError, App, Principal};
use crate::ids;
use crate::personas::object;

/// What this desk keeps, declared once (`kept.rs`: the registry walks it at every start).
pub(crate) const KINDS: &[crate::kept::Kind] = &[crate::kept::Kind::book(
    KEPT,
    crate::kept::Key::Plain,
    crate::kept::fits::<Book>,
)];

/// The kind of thing a phone's book of handoffs is, among what is left with the fleet.
const KEPT: &str = "oracle-handoff";
/// What a phone that may only read and send is told.
const FULL_ONLY: &str = "Oracle handoffs require full-device authentication";
/// How long the task of the deadlines waits before it looks again where the desk could
/// not be had.
const LOOKS_AGAIN_AFTER: Duration = Duration::from_secs(5);

/// The handoffs of this Host.
#[derive(Default)]
pub struct Handoffs {
    /// The desk, read from what was kept the first time it is asked for.
    desk: tokio::sync::Mutex<Option<Desk>>,
    /// The way to the task of each call that is open, by the call's own name.
    lines: Mutex<HashMap<String, mpsc::UnboundedSender<ToCall>>>,
    /// A handoff changed: who waits for one looks again, and so does the task of the
    /// deadlines.
    changed: Notify,
}

impl Handoffs {
    fn lines(&self) -> MutexGuard<'_, HashMap<String, mpsc::UnboundedSender<ToCall>>> {
        self.lines
            .lock()
            .unwrap_or_else(std::sync::PoisonError::into_inner)
    }
}

/// The books that are not as they were are written.
async fn written(
    app: &App,
    before: &BTreeMap<String, Book>,
    desk: &Desk,
) -> Result<(), Unavailable> {
    for (principal, book) in &desk.books {
        if before.get(principal) == Some(book) {
            continue;
        }
        let kept = serde_json::to_vec(book).map_err(|error| Unavailable::new(error.to_string()))?;
        app.fleet.keep(KEPT, principal.clone(), Some(kept)).await?;
    }
    Ok(())
}

/// What the desk said to tell is told, in its order.
async fn told(app: &App, effects: Vec<Effect>, now: i64) {
    for effect in effects {
        match effect {
            Effect::Call(voice, what) => {
                let line = app.oracle.handoffs.lines().get(&voice).cloned();
                if let Some(line) = line {
                    let _ = line.send(what);
                }
            }
            Effect::Apps(event) => {
                app.hub.publish(event, now);
            }
            // The Host's focus follows the user to the agent, as when an app selects it.
            Effect::Focus(agent) => {
                if let Ok(Some(record)) = app.fleet.select(agent.session).await {
                    app.mouth.opened(&record.session).await;
                }
            }
        }
    }
}

/// The desk for one request: read from what was kept the first time, with the time of every
/// step that was under way running anew; handed to the request; what changed written; and
/// what is to be told told, before the next request has the desk.
async fn with_desk<T>(
    app: &App,
    request: impl FnOnce(&mut Desk, i64) -> (T, Vec<Effect>),
) -> Result<T, Unavailable> {
    let handoffs = &app.oracle.handoffs;
    let mut held = handoffs.desk.lock().await;
    let now = app.clock.now_ms();
    if held.is_none() {
        let mut desk = Desk {
            host_id: app.server_id.clone(),
            ..Desk::default()
        };
        for (principal, kept) in app.fleet.kept_all(KEPT).await? {
            if let Ok(book) = serde_json::from_slice::<Book>(&kept) {
                desk.books.insert(principal, book);
            }
        }
        let before = desk.books.clone();
        desk.recovered(now);
        written(app, &before, &desk).await?;
        *held = Some(desk);
    }
    let desk = held.get_or_insert_with(Desk::default);
    let before = desk.books.clone();
    let (answer, effects) = request(desk, now);
    written(app, &before, desk).await?;
    let changed = before != desk.books;
    told(app, effects, now).await;
    if changed {
        handoffs.changed.notify_waiters();
    }
    Ok(answer)
}

/// A call of the Oracle was opened: from now on it can be offered a handoff, where its
/// phone asked for that. Says the way what the desk tells this call comes by: what is
/// there already is to be told before anything else of the call.
pub(super) async fn opened(app: &App, call: Call) -> mpsc::UnboundedReceiver<ToCall> {
    let (line, tells) = mpsc::unbounded_channel();
    let name = call.voice_session_id.clone();
    app.oracle.handoffs.lines().insert(name, line);
    let _ = with_desk(app, |desk, now| ((), desk.register(call, now))).await;
    tells
}

/// A call of the Oracle is over.
pub(super) async fn closed(app: &App, principal: &str, voice_session_id: &str) {
    app.oracle.handoffs.lines().remove(voice_session_id);
    let unregister = |desk: &mut Desk, _| {
        desk.unregister(principal, voice_session_id);
        ((), Vec::new())
    };
    let _ = with_desk(app, unregister).await;
}

/// Runs the deadlines of the handoffs, from the start of the Host: a phone that does not
/// answer an offer, or does not get on with a step, is not waited for longer than the step
/// may take.
pub fn run(app: &App) {
    let app = app.clone();
    tokio::spawn(async move {
        loop {
            let changed = app.oracle.handoffs.changed.notified();
            tokio::pin!(changed);
            changed.as_mut().enable();
            let next = |desk: &mut Desk, _| (desk.deadlines().into_iter().next(), Vec::new());
            let Ok(next) = with_desk(&app, next).await else {
                tokio::time::sleep(LOOKS_AGAIN_AFTER).await;
                continue;
            };
            let Some((principal, handoff_id, deadline, revision)) = next else {
                changed.await;
                continue;
            };
            let left = u64::try_from(deadline - app.clock.now_ms()).unwrap_or(0);
            if tokio::time::timeout(Duration::from_millis(left), changed)
                .await
                .is_err()
            {
                let expire = |desk: &mut Desk, now| {
                    ((), desk.expire(&principal, &handoff_id, revision, now))
                };
                let _ = with_desk(&app, expire).await;
            }
        }
    });
}

/// Who asks about a handoff: a phone with all rights by its id, or the administrator.
fn asks(principal: &Principal) -> Result<String, ApiError> {
    caller(principal).map_err(|_| ApiError::new(StatusCode::UNAUTHORIZED, FULL_ONLY))
}

fn refusal(refused: &Refused) -> Response {
    let status = StatusCode::from_u16(refused.status).unwrap_or(StatusCode::CONFLICT);
    (status, Json(refused.body())).into_response()
}

fn answer(answered: Result<Value, Refused>) -> Response {
    match answered {
        Ok(answer) => Json(answer).into_response(),
        Err(refused) => refusal(&refused),
    }
}

/// `GET /oracle/handoff`: the newest handoff of the phone that asks, and the one it is with
/// an agent by. The administrator names the phone it asks about.
async fn snapshot(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Query(query): Query<Vec<(String, String)>>,
) -> Result<Json<Value>, ApiError> {
    let mut who = asks(&principal)?;
    if who == ADMINISTRATOR {
        let named = query
            .iter()
            .find(|(name, said)| name == "principal" && !said.is_empty());
        if let Some((_, named)) = named {
            who.clone_from(named);
        }
    }
    let shown = with_desk(&app, |desk, now| (desk.snapshot(&who, now), Vec::new())).await?;
    Ok(Json(shown))
}

/// `POST /oracle/connect` with `{agent, principal?}`: the call of the one who asks is put
/// through to an agent, or, where the Oracle is named, the talk with an agent back to the
/// Oracle. Answered once the phone has settled it, or has taken too long.
async fn connect(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    body: Bytes,
) -> Result<Response, ApiError> {
    let who = asks(&principal)?;
    let said = object(&body)?;
    let field = |name: &str| said.get(name).cloned().unwrap_or(Value::Null);
    Ok(answer(
        put_through(&app, &who, &field("agent"), &field("principal")).await?,
    ))
}

/// Offers a handoff for the one who asks, waits for the phone, and says what came of it:
/// for the door, and for `clarp-admin oracle connect`.
pub(crate) async fn put_through(
    app: &App,
    who: &str,
    target: &Value,
    phone: &Value,
) -> Result<Result<Value, Refused>, ApiError> {
    let named = python::truthy(phone).then(|| oracle::said(phone));
    let id = format!("hof_{}", ids::hex(16));
    let offer = |desk: &mut Desk, now| {
        // Which agent a name means, among the contacts an app shows.
        let directory = app.fleet.directory();
        let agents = super::agents(&directory);
        let agent = |name: &str| {
            let found = oracle::resolve_visible(name, &agents).map_err(str::to_string)?;
            let member = directory.by_session(found.session);
            let member = member.ok_or_else(|| "unknown agent".to_string())?;
            Ok(Agent {
                session: member.record.session.clone(),
                agent_id: member.record.agent_id.clone(),
                persona: member.record.persona.clone(),
            })
        };
        desk.offer((who, named.as_deref()), target, agent, &id, now)
    };
    let (whose, handoff_id) = match with_desk(app, offer).await? {
        Ok(offered) => offered,
        Err(refused) => return Ok(Err(refused)),
    };
    // The phone settles it, step by step; whoever asked is not kept longer than every step
    // may take, and a little more.
    let handoffs = &app.oracle.handoffs;
    let give_up =
        tokio::time::Instant::now() + Duration::from_millis(CONNECT_WAITS_MS.unsigned_abs());
    loop {
        let changed = handoffs.changed.notified();
        tokio::pin!(changed);
        changed.as_mut().enable();
        let settled = |desk: &mut Desk, _| (desk.settled(&whose, &handoff_id), Vec::new());
        if with_desk(app, settled).await? {
            break;
        }
        if tokio::time::timeout_at(give_up, changed).await.is_err() {
            break;
        }
    }
    let outcome = |desk: &mut Desk, now| (desk.outcome(&whose, &handoff_id, true, now), Vec::new());
    let gone = || Refused {
        status: 404,
        error: "unknown_handoff".to_string(),
        handoff: None,
    };
    Ok(with_desk(app, outcome)
        .await?
        .unwrap_or_else(|| Err(gone())))
}

/// `POST /oracle/handoffs/ack`: a phone says how far it is with a handoff.
async fn ack(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    body: Bytes,
) -> Result<Response, ApiError> {
    let who = asks(&principal)?;
    let said = Value::Object(object(&body)?);
    Ok(answer(
        with_desk(&app, |desk, now| desk.ack(&who, &said, now)).await?,
    ))
}

pub fn routes() -> Router<App> {
    Router::new()
        .route("/oracle/handoff", get(snapshot))
        .route("/oracle/connect", post(connect))
        .route("/oracle/handoffs/ack", post(ack))
}
