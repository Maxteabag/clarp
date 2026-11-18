//! The Oracle's delegations on this Host: what it handed to agents, and what came of it.
//!
//! A delegation is a turn of an agent's that the Oracle asked for (`POST
//! /oracle/delegations`): sent as the Oracle's, without speech, waiting where the agent is
//! busy. The record of it is the kernel's book ([`clarp_kernel::oracle::delegations`]), kept
//! with the Host row by row. Every request has the book in one way ([`with_book`]): looked
//! at again before the request, and written after it, before anybody is told. What became
//! of a turn is asked of the agent it was handed to, by the id of the message that began
//! it, each time the book is looked at: an agent's conversation is the one record of its
//! turns, and it is there after a restart, so a delegation is never ended by a guess.
//!
//! The routes answer as the Python Host's (`server/lib/routes/oracle.py`).

use std::collections::{BTreeSet, HashMap};

use axum::body::Bytes;
use axum::extract::{Query, State};
use axum::http::StatusCode;
use axum::routing::{get, post};
use axum::{Extension, Json, Router};
use clarp_actor::TurnFate;
use clarp_kernel::conversation::Message;
use clarp_kernel::oracle::delegations::{
    Agent, Asked, Book, Delegation, Fate, Refused, SHOWN_USUALLY, Taken, normalize_id,
};
use clarp_kernel::{oracle, python};
use clarp_protocol::requests::SendRequest;
use serde_json::{Map, Value, json};

use crate::agent::Unavailable;
use crate::calls::caller;
use crate::edge::{self, ApiError, App, Principal};
use crate::personas::object;

/// The kind of thing a delegation is, among what is left with the fleet.
const KEPT: &str = "oracle-delegation";
/// Why a delegation failed whose turn ended with nothing said.
const SAID_NOTHING: &str = "Agent completed without a text result";

type Answered = Result<Json<Value>, ApiError>;

fn refusal(refused: &Refused) -> ApiError {
    let status = StatusCode::from_u16(refused.status).unwrap_or(StatusCode::BAD_REQUEST);
    ApiError::new(status, refused.why)
}

/// A field of a request as the text the Python Host makes of it (`str(value or "")`).
fn text(said: &Map<String, Value>, key: &str) -> String {
    oracle::said(said.get(key).unwrap_or(&Value::Null))
}

/// The book, read from what was kept the first time it is asked for.
async fn opened<'a>(app: &App, held: &'a mut Option<Book>) -> Result<&'a mut Book, Unavailable> {
    if held.is_none() {
        let kept = app.fleet.kept_all(KEPT).await?;
        let rows = kept
            .iter()
            .filter_map(|(_, row)| serde_json::from_slice::<Delegation>(row).ok());
        *held = Some(Book::kept(rows.collect()));
    }
    Ok(held.get_or_insert_with(Book::default))
}

/// What changed in the book is written, before anybody is told of it.
async fn written(app: &App, book: &mut Book) -> Result<(), Unavailable> {
    for row in book.written() {
        let kept = serde_json::to_vec(&row).map_err(|error| Unavailable(error.to_string()))?;
        app.fleet.keep(KEPT, row.delegation_id, Some(kept)).await?;
    }
    Ok(())
}

/// What became of the turn of a delegation, as the agent it was handed to says, and the
/// session of that agent's tool as it is now. An agent that is no longer there has no
/// turns.
async fn fate_of(app: &App, row: &Delegation) -> Result<(TurnFate, String), Unavailable> {
    let directory = app.fleet.directory();
    let Some(member) = directory.named(&row.agent_id) else {
        return Ok((TurnFate::Gone, String::new()));
    };
    let (message_id, _) = Message::filed(&row.client_msg_id);
    let fate = member.agent.fate(message_id).await?;
    let tool_session = member.agent.view().status.backend_session.clone();
    Ok((fate, tool_session.unwrap_or_default()))
}

/// The delegations that are not over are held against what became of their turns: one
/// whose turn began is under way; one whose turn ended is ended at the moment the turn
/// was, with its answer, or as failed with the words its failure was told in; and one
/// whose turn is gone failed.
async fn looked_again(app: &App, book: &mut Book) -> Result<(), Unavailable> {
    let open = book.rows().iter().filter(|row| row.status.active());
    let open: Vec<Delegation> = open.cloned().collect();
    let mut fates = Vec::new();
    for row in open {
        let (fate, tool_session) = fate_of(app, &row).await?;
        // A delegation taken before the agent's tool had a session is told with it from
        // the moment the tool has one.
        book.tool_session(&row.delegation_id, &tool_session);
        fates.push((row, fate));
    }
    let now = app.clock.now_ms();
    let mut became = HashMap::new();
    for (row, fate) in fates {
        let fate = match fate {
            TurnFate::Unknown | TurnFate::Waits => Fate::NotAdmitted,
            TurnFate::Runs => {
                book.started(&row.trace_id, now);
                Fate::Live
            }
            TurnFate::Gone => Fate::Lost,
            TurnFate::Ended {
                status,
                closes,
                text,
                ended_at_ms,
            } => {
                // No delegation changes before the moment it was last written.
                let at_ms = ended_at_ms.max(row.updated_at);
                if status != "completed" {
                    // Why, in the tool's own words where the Host has them: they stand
                    // below the sentence a user is told.
                    let why = text.split_once("\n\n");
                    let why = why.map_or(text.as_str(), |(_, tools)| tools);
                    book.failed_for_trace(&row.trace_id, why, at_ms);
                } else if python::strip(&text).is_empty() {
                    book.failed_for_trace(&row.trace_id, SAID_NOTHING, at_ms);
                }
                Fate::Said {
                    message_id: closes,
                    text,
                    at_ms,
                }
            }
        };
        became.insert(row.delegation_id, fate);
    }
    // What is still open is ended by the book's own rule: with the answer its turn left,
    // or as failed where the turn is gone.
    book.orphans(
        |row| {
            let fate = became.get(&row.delegation_id);
            fate.cloned().unwrap_or(Fate::NotAdmitted)
        },
        now,
    );
    Ok(())
}

/// The book for one request: looked at again, handed to the request, and written once the
/// request is done with it, whatever it answers. One request has the book at a time.
async fn with_book<T>(
    app: &App,
    request: impl AsyncFnOnce(&mut Book) -> Result<T, ApiError>,
) -> Result<T, ApiError> {
    let mut held = app.oracle.delegations.lock().await;
    let book = opened(app, &mut held).await?;
    looked_again(app, book).await?;
    let answered = request(book).await;
    written(app, book).await?;
    answered
}

/// `POST /oracle/delegations` with `{delegation_id, session, request}`: the Oracle hands
/// an agent something to look into. Taken once however often it is asked.
async fn hand(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    body: Bytes,
) -> Answered {
    let said = object(&body)?;
    let owner = caller(&principal)?;
    let (id, session, request) = (
        text(&said, "delegation_id"),
        text(&said, "session"),
        text(&said, "request"),
    );
    let delegation = handed(&app, &owner, (&id, &session, &request)).await?;
    Ok(Json(json!({ "delegation": delegation })))
}

/// Work is handed to an agent under a name, for the one who asks: a turn of the agent's,
/// sent as the Oracle's. Taken once however often it is asked. Says the delegation as it is
/// told.
pub(super) async fn handed(
    app: &App,
    owner: &str,
    (id, session, request): (&str, &str, &str),
) -> Result<Value, ApiError> {
    let member = app.anyone(python::strip(session));
    // A message of the delegation's own id in the agent's conversation: its turn was
    // admitted, and is the agent's to run, also after a restart.
    let has_message = match (&member, normalize_id(id)) {
        (Some(member), Ok(id)) => {
            let (message_id, _) = Message::filed(&format!("oracle-{id}"));
            member.agent.fate(message_id).await? != TurnFate::Unknown
        }
        _ => false,
    };
    with_book(app, async |book| {
        let asked = Asked {
            delegation_id: id,
            session,
            request,
            owner,
        };
        let agent = member.as_ref().map(|member| Agent {
            agent_id: &member.record.agent_id,
            janitor: member.record.is_janitor,
        });
        let taken = book.take(&asked, agent, has_message, app.clock.now_ms());
        let admit = match taken.map_err(|refused| refusal(&refused))? {
            Taken::AsItIs(row) => return Ok(row.told()),
            Taken::ToAdmit(admit) => admit,
        };
        let turn = SendRequest {
            session: admit.session.clone(),
            text: admit.text.clone(),
            client_msg_id: Some(admit.client_msg_id.clone()),
            trace_id: Some(admit.trace_id.clone()),
            synthesize_audio: Some(false),
            queue_if_busy: Some(true),
            origin: Some("oracle".to_string()),
            ..SendRequest::default()
        };
        let sent = match edge::send_message(app, turn).await {
            Ok(sent) => sent,
            Err(refused) => {
                // A turn that cannot be admitted: the delegation failed, in the words of that.
                book.fail(&admit.delegation_id, refused.message(), app.clock.now_ms());
                return Err(ApiError::new(refused.status(), refused.message()));
            }
        };
        let tool_session = member
            .as_ref()
            .and_then(|member| member.agent.view().status.backend_session.clone());
        let waits = sent.queued == Some(true);
        let row = book.dispatched(
            &admit.delegation_id,
            tool_session.as_deref().unwrap_or(""),
            waits,
            app.clock.now_ms(),
        );
        Ok(row.map_or(Value::Null, Delegation::told))
    })
    .await
}

/// `GET /oracle/delegations`: what the one who asks is still to be told, the result that
/// came first first; with `?all=1`, every delegation of theirs, the newest first.
async fn list(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    Query(asked): Query<HashMap<String, String>>,
) -> Answered {
    let owner = caller(&principal)?;
    let limit = asked
        .get("limit")
        .map(String::as_str)
        .filter(|limit| !limit.is_empty());
    let limit = limit.map_or(Some(SHOWN_USUALLY), |limit| {
        python::strip(limit).parse().ok()
    });
    let limit = limit.unwrap_or(SHOWN_USUALLY);
    let all = asked.get("all").map(|all| all.to_lowercase());
    let all = matches!(all.as_deref(), Some("1" | "true" | "yes"));
    with_book(&app, async |book| {
        let rows = if all {
            book.recent(&owner, limit)
        } else {
            book.undelivered(&owner, limit)
        };
        let rows: Vec<Value> = rows.into_iter().map(Delegation::told).collect();
        Ok(Json(json!({ "delegations": rows })))
    })
    .await
}

/// `POST /oracle/delegations/ack` with `{delegation_id}`: the phone that asked has the
/// result.
async fn acknowledge(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    body: Bytes,
) -> Answered {
    let said = object(&body)?;
    let owner = caller(&principal)?;
    let id = text(&said, "delegation_id");
    let id = normalize_id(&id).map_err(|refused| refusal(&refused))?;
    with_book(&app, async |book| {
        if !book.acknowledge(id, &owner, app.clock.now_ms()) {
            let why = "terminal delegation not found";
            return Err(ApiError::new(StatusCode::NOT_FOUND, why));
        }
        Ok(Json(json!({ "ok": true })))
    })
    .await
}

/// `POST /oracle/delegations/cancel`. With `{session}`: the agent is stopped, and what the
/// one who asks handed it and was not over is called off. With `{delegation_id}`: one that
/// is over is told as it is; one that is not is the agent's to stop.
async fn cancel(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    body: Bytes,
) -> Answered {
    let said = object(&body)?;
    let owner = caller(&principal)?;
    let session = text(&said, "session");
    let session = python::strip(&session);
    if !session.is_empty() {
        let rows = called_off(&app, &owner, session).await?;
        return Ok(Json(
            json!({ "cancelled_count": rows.len(), "delegations": rows }),
        ));
    }
    with_book(&app, async |book| {
        let id = text(&said, "delegation_id");
        let id = normalize_id(&id).map_err(|refused| refusal(&refused))?;
        let found = book.cancel(id, &owner);
        let found = found.map_err(|refused| refusal(&refused))?;
        let none = || ApiError::new(StatusCode::NOT_FOUND, "delegation not found");
        Ok(Json(
            json!({ "delegation": found.ok_or_else(none)?.told() }),
        ))
    })
    .await
}

/// An agent is stopped, and what the one who asks handed it and was not over is called off.
/// Says what was called off, as it is told.
pub(super) async fn called_off(
    app: &App,
    owner: &str,
    session: &str,
) -> Result<Vec<Value>, ApiError> {
    with_book(app, async |book| {
        let found = book.to_call_off(session, owner);
        let found = found.map_err(|refused| refusal(&refused))?;
        // The agent is stopped first: its turn ends, and what waits for it is held. Only
        // then is anything called off, so that a stop that fails leaves every delegation
        // as it was. What was not over before the stop is called off, whatever the stop
        // made of its turn.
        let member = app.member(session);
        if let Some(member) = &member {
            let stopped = member.agent.stop("user".to_string()).await;
            let failed = || ApiError::new(StatusCode::BAD_GATEWAY, "agent cancellation failed");
            stopped.map_err(|_| failed())?;
        }
        let ids: Vec<String> = found.iter().map(|one| one.delegation_id.clone()).collect();
        let called_off = book.called_off(&ids, app.clock.now_ms());
        // And what waited for the agent of these is taken out of its queue: what anybody
        // else left there stays.
        if let Some(member) = &member {
            let (waiting, _) = member.agent.queue().await?;
            let of_these = |client_id: &str| found.iter().any(|one| one.client_msg_id == client_id);
            for waits in waiting.iter().filter(|waits| of_these(&waits.client_id)) {
                let withdrawn = member.agent.withdraw(waits.turn, "user".to_string());
                withdrawn.await?;
            }
        }
        Ok(called_off.iter().map(Delegation::told).collect())
    })
    .await
}

/// The delegations of these names, as the book has them now: in the order they ended, or
/// were taken where they are not over. What a call of the Oracle tells of its own work.
pub(super) async fn rows_of(
    app: &App,
    ids: &BTreeSet<String>,
) -> Result<Vec<Delegation>, ApiError> {
    with_book(app, async |book| {
        let own = book
            .rows()
            .iter()
            .filter(|row| ids.contains(&row.delegation_id));
        let mut rows: Vec<Delegation> = own.cloned().collect();
        rows.sort_by(|one, other| {
            let ended = one.updated_at.cmp(&other.updated_at);
            ended.then_with(|| one.delegation_id.cmp(&other.delegation_id))
        });
        Ok(rows)
    })
    .await
}

/// Every delegation of the book, as it has them now.
pub(super) async fn all(app: &App) -> Result<Vec<Delegation>, ApiError> {
    with_book(app, async |book| Ok(book.rows().to_vec())).await
}

pub(super) fn routes() -> Router<App> {
    Router::new()
        .route("/oracle/delegations", get(list).post(hand))
        .route("/oracle/delegations/ack", post(acknowledge))
        .route("/oracle/delegations/cancel", post(cancel))
}
