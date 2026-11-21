//! Making an agent's conversation shorter, asked for from an app (`POST /compact`).
//!
//! A conversation that grows fills what its model can hold. A tool can sum its own
//! conversation up and go on in the summary. Where a tool takes its word for that as a message
//! ([`clarp_backend::Backend::shortens_with`]: Claude Code's `/compact`), this Host asks for
//! it in a turn of the agent's own: the turn waits its turn like any other, is nobody's
//! message and says nothing, and is left out of the chat an app reads. While it runs the
//! agent's row says `compacting`; afterwards it says how much the conversation holds, and what
//! the compaction did (`last_compaction`, decided in `clarp_kernel::compaction` when the turn
//! has ended).
//!
//! The answers are the Python Host's (`lib/compaction.py`, `routes/send.py`
//! `_handle_compact`): started, already at it, or refused in words. How it is done is not:
//! that Host starts the tool's own screen in a terminal and types into it, which also serves
//! Codex and Antigravity. Here a tool that takes no message for it is refused as one that
//! cannot.

use axum::Json;
use axum::Router;
use axum::body::Bytes;
use axum::extract::State;
use axum::http::StatusCode;
use axum::response::{IntoResponse, Response};
use axum::routing::post;
use clarp_kernel::conversation::Message;
use clarp_kernel::turn::{Admission, COMPACTION, Origin};
use serde_json::{Value, json};

use crate::edge::App;
use crate::ids;

fn refused(why: &str) -> Response {
    let told = json!({ "ok": false, "error": why });
    (StatusCode::CONFLICT, Json(told)).into_response()
}

fn bad(why: &str) -> Response {
    (StatusCode::BAD_REQUEST, Json(json!({ "error": why }))).into_response()
}

/// `POST /compact {"session": ...}`: the agent's tool is asked to make its conversation
/// shorter. Answered at once; an app watches the agent's row (`compacting`, then
/// `last_compaction`).
async fn compact(State(app): State<App>, body: Bytes) -> Response {
    let Ok(Value::Object(asked)) = serde_json::from_slice::<Value>(&body) else {
        return bad("bad json");
    };
    let session = match crate::edge::body_text(&asked, "session") {
        Ok("") => return bad("session required"),
        Ok(session) => session,
        Err(why) => return why.into_response(),
    };
    let directory = app.fleet.directory();
    let Some(member) = directory.named(session) else {
        return refused("no such agent");
    };
    let backend = member.record.backend.as_str();
    let view = member.agent.view();
    let status = &view.status;
    if status.backend_session.is_none() {
        return refused("no live session to compact");
    }
    let word = clarp_backend::by_id(backend).and_then(|tool| tool.shortens_with());
    let Some(word) = word else {
        return refused(&format!("compaction unsupported for {backend}"));
    };
    let at_it = |origin: &Origin| matches!(origin, Origin::Trigger { name } if name == COMPACTION);
    if status.live.as_ref().is_some_and(|live| at_it(&live.origin)) {
        let told = json!({ "ok": true, "status": "already_compacting", "backend": backend });
        return Json(told).into_response();
    }
    // Not while a turn runs or waits: the conversation is made shorter between turns.
    if status.live.is_some() {
        return refused("agent is busy \u{2014} try again when idle");
    }
    if !status.waiting.is_empty() {
        return refused(
            "messages are waiting for this agent \u{2014} try again when they have run",
        );
    }
    let request = ids::hex(8);
    let (id, marked) = Message::filed(&format!("compact-{request}"));
    let message = Message {
        id,
        marked,
        unminted: true,
        text: word.to_string(),
        origin: Origin::Trigger {
            name: COMPACTION.to_string(),
        },
        trace_id: Some(request),
        ..Message::default()
    };
    match member.agent.send(message, Admission::Queue).await {
        Ok(_) => {
            let told = json!({ "ok": true, "status": "started", "backend": backend });
            Json(told).into_response()
        }
        Err(_) => refused("agent is busy \u{2014} try again when idle"),
    }
}

pub fn routes() -> Router<App> {
    Router::new().route("/compact", post(compact))
}
