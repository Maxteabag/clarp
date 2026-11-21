//! Coordination between agents at the Host's edge (the Python Host's PR 482, its contract 108):
//! who made a send, from the turn token it presents and a peer Host's word, and the note an
//! agent leaves for its user (`POST /agent-notes`, and `clarp-admin tell-user` as an agent's
//! own tool). The rules are the kernel's ([`clarp_kernel::coordination`]); this module reads
//! what a request presents and hands it to them.

use axum::Json;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use clarp_actor::Noted;
use clarp_kernel::coordination::{self as rules, Caller, Refusal};
use serde_json::{Value, json};

use crate::edge::{ApiError, App};
use crate::ids;

/// The header a peer Host names itself by when it sends (the Python Host's `server_peers.send`).
pub const PEER_HEADER: &str = "x-clarp-peer";

/// Who made a send, beyond what its body says of itself.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Made {
    /// The agent of the turn whose token the send presents (any turn of it, whatever became of
    /// it since), with that agent's session when this Host still has the agent.
    pub turn: Option<(String, Option<String>)>,
    /// A peer Host sent it: it forwards only its agents' messages.
    pub peer: bool,
    /// The caller named the send's trace itself (else the Host names one).
    pub trace_named: bool,
}

/// A refusal of the kernel's rules, as the client is told it.
pub(crate) fn refused(refusal: Refusal) -> ApiError {
    let status = StatusCode::from_u16(refusal.status).unwrap_or(StatusCode::BAD_REQUEST);
    ApiError::new(status, refusal.words)
}

/// The agent and the turn a presented token names, when it is a token of this Host's (`None`
/// for none, an empty one, or one this Host did not make). The agent need not exist any more.
/// A key that cannot be read is answered so, and that it may be asked again (503).
pub(crate) async fn token_of(
    app: &App,
    token: Option<&str>,
) -> Result<Option<(String, u64)>, ApiError> {
    let Some(token) = token.filter(|token| !token.is_empty()) else {
        return Ok(None);
    };
    crate::turn_identity::claim(app, token)
        .await
        .map_err(|why| ApiError::retryable(StatusCode::SERVICE_UNAVAILABLE, why.0))
}

/// The session of an agent of this Host, a janitor's too, by its id.
pub(crate) fn session_of(app: &App, agent_id: &str) -> Option<String> {
    let directory = app.fleet.directory();
    let mut everyone = directory.members.iter().chain(directory.janitors.iter());
    everyone
        .find(|member| member.record.agent_id == agent_id)
        .map(|member| member.record.session.clone())
}

/// Who made a send, from the token of a turn (`X-Clarp-Turn`, or what an agent's tool was
/// given) and the peer header it presents.
pub(crate) async fn made(
    app: &App,
    token: Option<&str>,
    peer: bool,
    trace_named: bool,
) -> Result<Made, ApiError> {
    let turn = token_of(app, token).await?.map(|(agent_id, _)| {
        let session = session_of(app, &agent_id);
        (agent_id, session)
    });
    Ok(Made {
        turn,
        peer,
        trace_named,
    })
}

/// Whether a request says it comes from a peer Host.
pub(crate) fn from_a_peer(headers: &HeaderMap) -> bool {
    headers
        .get(PEER_HEADER)
        .is_some_and(|peer| !peer.as_bytes().is_empty())
}

/// Whether the request carries a non-blank turn or paired-Host claim.
pub(crate) fn from_an_agent(headers: &HeaderMap) -> bool {
    [crate::turn_identity::HEADER, PEER_HEADER]
        .into_iter()
        .any(|name| {
            headers.get(name).is_some_and(|value| {
                value
                    .as_bytes()
                    .iter()
                    .copied()
                    .map(char::from)
                    .any(|character| !character.is_whitespace())
            })
        })
}

/// `POST /agent-notes {session, text, note_id?}`: an agent's note to its user, from its own
/// running turn, proven by the turn's token (`X-Clarp-Turn`).
pub(crate) async fn agent_note(
    State(app): State<App>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    let token = headers
        .get(crate::turn_identity::HEADER)
        .map(|token| token.to_str().unwrap_or_default());
    // What is no JSON object is refused as one (`bad json`), after the token is read.
    let body = serde_json::from_slice::<Value>(&body).unwrap_or(Value::Null);
    Ok(Json(leave_note(&app, &body, token).await?))
}

/// Leaves a note for an agent's user, as `POST /agent-notes` asks it, from the turn whose
/// token the request presents. The refusals in the Python Host's order: the token (503 when
/// the key cannot be read), the body, the agent, whose turn it is, whether it runs, and an id
/// the agent has for other words. A note asked again with the same words is the same note:
/// nobody is told again.
pub(crate) async fn leave_note(
    app: &App,
    body: &Value,
    token: Option<&str>,
) -> Result<Value, ApiError> {
    let caller = token_of(app, token).await?;
    let asked = rules::asked(body).map_err(refused)?;
    let member = app
        .anyone(&asked.session)
        .ok_or_else(|| refused(rules::no_such_agent()))?;
    let record = &member.record;
    let other = caller
        .as_ref()
        .filter(|(agent_id, _)| *agent_id != record.agent_id)
        .map(|(agent_id, _)| session_of(app, agent_id));
    let who = match (&caller, &other) {
        (None, _) => Caller::Nobody,
        (Some(_), None) => Caller::Own,
        (Some(_), Some(session)) => Caller::Other(session.as_deref()),
    };
    rules::may_note(&record.session, who).map_err(refused)?;
    let turn = caller.map_or(0, |(_, turn)| turn);
    let note_id = asked.note_id.unwrap_or_else(|| ids::hex(8));
    let id = rules::note_message_id(&record.agent_id, &note_id);
    let (noted, told) = member
        .agent
        .leave_note(turn, id.clone(), asked.text.clone())
        .await?;
    let (trace_id, created, notified, reason) = match (noted, told) {
        (Noted::NotRunning, _) => return Err(refused(rules::not_running(&record.session))),
        (Noted::OtherWords, _) => {
            return Err(refused(rules::other_words(&record.session, &note_id)));
        }
        (Noted::Again { trace_id, .. }, _) => (trace_id, false, false, rules::AGAIN.to_string()),
        (Noted::Written { trace_id, .. }, Some(Ok(told))) => {
            let reason = told["reason"].as_str().unwrap_or_default().to_string();
            (trace_id, true, true, reason)
        }
        (Noted::Written { trace_id, .. }, Some(Err(reason))) => (trace_id, true, false, reason),
        (Noted::Written { trace_id, .. }, None) => (trace_id, true, false, String::new()),
    };
    Ok(json!({
        "ok": true,
        "session": record.session,
        "message_id": id,
        "trace_id": trace_id,
        "created": created,
        "notified": notified,
        "reason": reason,
    }))
}
