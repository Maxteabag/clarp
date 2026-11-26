//! A Stop and a repair, as an agent's tool and a request both ask them (contract 61).
//!
//! Who acts and why is the kernel's rule ([`clarp_kernel::control`]); this finds out whose turn a
//! token is, asks the agent, and answers in the Python Host's shapes and words.

use axum::http::StatusCode;
use clarp_kernel::control::{self, Control};
use clarp_kernel::python;
use clarp_protocol::responses::StopResponse;
use serde_json::{Map, Value, json};

use crate::edge::{ApiError, App};

/// Who stops or repairs an agent, and why: the agent whose own turn asks, by the token it
/// presents (`token`); the user, where a paired device (`device`) says so; else the claim, which
/// the Host does not know to be true. A token whose agent cannot be said is answered so (503),
/// never taken for the claim: an agent's Stop taken for the user's would do what only the user's
/// does. A token of nobody is no turn's, and the claim stands.
pub(crate) async fn control_of(
    app: &App,
    device: bool,
    token: Option<&str>,
    actor: Option<&Value>,
    reason: Option<&Value>,
) -> Result<Control, ApiError> {
    let claimed = control::claimed(actor);
    let turn_of = match token {
        None => None,
        Some(token) => {
            let found = crate::turn_identity::caller(app, token).await;
            let found =
                found.map_err(|why| ApiError::retryable(StatusCode::SERVICE_UNAVAILABLE, why.0))?;
            found.map(|member| member.record.session.clone())
        }
    };
    let (actor, verified) = control::acting(&claimed, turn_of.as_deref(), device);
    Ok(Control {
        actor,
        verified,
        reason: control::reason(reason),
    })
}

/// `POST /stop`: end the turn and pause the queue. A user's Stop lets their waiting sends run.
/// `main`: the agent's own turn whatever runs. Otherwise, while the agent's conversation fork
/// runs a turn (issue 718), that turn is stopped instead, alone: the agent's own turn and its
/// queue go on.
pub(crate) async fn stop(
    app: &App,
    session: &str,
    by: Control,
    main: bool,
) -> Result<StopResponse, ApiError> {
    let answer = StopResponse {
        ok: true,
        stop_actor: Some(by.actor.clone()),
        stop_actor_verified: Some(by.verified),
        stop_reason: Some(by.reason.clone()),
        text: Some(control::STOP_TEXT.to_owned()),
        ..StopResponse::default()
    };
    let Some(member) = app.member(session) else {
        // Nothing runs under a name the Host does not have: nothing was ended, and that is all.
        return Ok(answer);
    };
    let stopped = member.agent.stop_as(by, !main).await?;
    Ok(StopResponse {
        terminated: u64::from(stopped.told),
        // The queue as the Stop left it, read in the same step: what `GET /turn-queue` says
        // after it until the user sends again or resumes it. A fork's Stop holds nothing and
        // leaves the queue as it was.
        queue_paused: Some(stopped.paused),
        goals_paused: Some(stopped.goals_paused as u64),
        ..answer
    })
}

/// `POST /repair-slot {session, reason, actor?}`: gives back a turn of an agent that nothing
/// runs, interrupting nothing and holding or letting go nothing, and keeps a note of who and why
/// on its state. Nothing on this Host can hold a turn that nothing runs (a turn ends when its
/// shim stops answering, and is found ended after a restart), so nothing is given back and no
/// queue is started; the note is kept, as the Python Host keeps it whatever it released.
pub(crate) async fn repair(
    app: &App,
    data: &Map<String, Value>,
    device: bool,
    token: Option<&str>,
) -> Result<Value, ApiError> {
    let session = control::field(data.get("session"));
    let session = python::strip(&session).to_string();
    let member = app.fleet.directory().named(&session).cloned();
    let Some(member) = member.filter(|_| !session.is_empty()) else {
        return Err(ApiError::new(StatusCode::NOT_FOUND, "agent not found"));
    };
    let by = control_of(app, device, token, data.get("actor"), data.get("reason")).await?;
    if by.reason.is_empty() {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "reason is required"));
    }
    member.agent.repair(by.clone()).await?;
    Ok(json!({
        "ok": true,
        "session": session,
        "released_trace": "",
        "queue_recovered": 0,
        "repair_actor": by.actor,
        "repair_actor_verified": by.verified,
        "repair_reason": by.reason,
    }))
}
