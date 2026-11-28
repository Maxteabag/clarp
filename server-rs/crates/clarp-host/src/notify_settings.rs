//! What the owner is notified of, event by event (feature `notification_events`).
//!
//! `GET /notification-settings` says the owner's choices, `POST /notification-settings`
//! changes them and every app is told (`notification-settings-changed`, the same document).
//! They are kept among the Host's settings ([`clarp_kernel::notify_events::KEY`]), so every
//! device agrees; the rules are in [`clarp_kernel::notify_events`]. The global choice of
//! `call` is the ring switch (`POST /calls/ring/settings` `enabled`): a change of either is
//! the same change.
//!
//! `POST /chat-read` marks a chat read on every device without opening it: the apps are told
//! (`chat-read`) and the phones take its notifications down (a silent `read` push).

use axum::body::Bytes;
use axum::extract::State;
use axum::http::StatusCode;
use axum::{Extension, Json};
use clarp_kernel::notify_events::{CHANGED, Event, KEY, Settings};
use serde_json::{Value, json};

use crate::edge::{ApiError, App, Principal};
use crate::fleet::{AgentRecord, Directory};
use crate::pusher::Read;

/// The event a new kind of notification is told by when its alert is off (a failed turn the
/// owner switched off): the chat is marked unread, and an app pops up nothing. An app from
/// before `notification_events` would pop up a `user-notification` whatever its `alert` says.
pub(crate) const CHAT_UNREAD: &str = "chat-unread";

/// The owner's choices as they are kept now.
pub(crate) fn settings(directory: &Directory) -> Settings {
    Settings::stored(directory.setting(KEY))
}

/// Whether the owner is alerted of this event of this agent: pushed to, and shown a popup on
/// a desk. A helper reports to the agent that made it and pages nobody, as a muted agent.
/// The ring switch for every agent is held where a ring is placed, so a ring that is placed
/// is not held by it here.
pub(crate) fn alerts(directory: &Directory, event: Event, record: &AgentRecord) -> bool {
    let muted = event != Event::Call && (record.muted || record.is_helper());
    settings(directory).alerts(event, &record.agent_id, muted, true)
}

/// The same, where the agent's thread may not reach the fleet's settings: then the catalog's
/// default decides.
pub(crate) fn alerts_in(directory: Option<&Directory>, event: Event, record: &AgentRecord) -> bool {
    match directory {
        Some(directory) => alerts(directory, event, record),
        None => {
            let muted = record.muted || record.is_helper();
            Settings::default().alerts(event, &record.agent_id, muted, true)
        }
    }
}

/// The ring switch. A ring desk that cannot be read is taken as on: that is its default.
async fn rings_on(app: &App) -> bool {
    let desk = app.calls.clone();
    let read = desk.rings(app, |rings, _, _, _| rings.settings()["enabled"].as_bool());
    read.await.ok().flatten().unwrap_or(true)
}

/// The document the routes answer and the event carries.
pub(crate) async fn document(app: &App) -> Value {
    let rings_on = rings_on(app).await;
    let directory = app.fleet.directory();
    let agents = directory.members.iter().map(|member| {
        let record = &member.record;
        (
            record.agent_id.as_str(),
            record.session.as_str(),
            record.muted,
        )
    });
    settings(&directory).document(rings_on, agents)
}

/// Every app is told the choices as they are now.
pub(crate) async fn tell_changed(app: &App) -> Value {
    let document = document(app).await;
    let mut event = document.clone();
    event["type"] = json!(CHANGED);
    app.hub.publish(event, app.clock.now_ms());
    document
}

/// `GET /notification-settings`.
pub(crate) async fn get(State(app): State<App>) -> Json<Value> {
    Json(document(&app).await)
}

fn object(body: &Bytes) -> Result<Value, ApiError> {
    match serde_json::from_slice::<Value>(body) {
        Ok(asked @ Value::Object(_)) => Ok(asked),
        _ => Err(ApiError::new(StatusCode::BAD_REQUEST, "bad json")),
    }
}

/// `POST /notification-settings` with `{events: {<id>: bool}}`, or `{agent, events: {<id>:
/// bool | null}}` for one agent (by its session or its id; `null` follows the global choice
/// again). An unknown event or agent is refused and nothing changes. The owner's apps and the
/// agents' own tools may change it alike. Answers the document, as `GET` does.
pub(crate) async fn set(State(app): State<App>, body: Bytes) -> Result<Json<Value>, ApiError> {
    let asked = object(&body)?;
    // One change at a time, through every route that changes the choices: each is whole or
    // not at all, and has a version of its own.
    let _writing = app.notification_writer.lock().await;
    let directory = app.fleet.directory();
    let agent = asked_agent(&directory, &asked)?;
    let mut after = settings(&directory);
    let changed = after
        .change(&asked, agent.as_deref())
        .map_err(|why| refusal(&why))?;
    // The global choice of `call` is the ring switch, kept where the rings keep it: it is set
    // first, and set back where the rest cannot be kept.
    let rings_before = match changed.call {
        Some(on) => {
            let before = rings_on(&app).await;
            set_rings(&app, on).await?;
            Some(before)
        }
        None => None,
    };
    if let Err(unkept) = app.fleet.set(vec![(KEY.to_string(), after.kept())]).await {
        if let Some(before) = rings_before
            && let Err(why) = set_rings(&app, before).await
        {
            eprintln!(
                "clarp-host: the ring switch could not be set back after a failed notification \
                 change: {}",
                why.message()
            );
        }
        return Err(unkept.into());
    }
    Ok(Json(tell_changed(&app).await))
}

/// A refusal of a change, as a client is told it.
fn refusal(why: &clarp_kernel::notify_events::Refused) -> ApiError {
    let status = if why.code == "unknown_agent" {
        StatusCode::NOT_FOUND
    } else {
        StatusCode::BAD_REQUEST
    };
    ApiError::custom(
        status,
        why.message.clone(),
        json!({ "error": why.code, "message": why.message }),
    )
}

/// The agent a change names (`agent`: its session or its id), by its id. `None`: every agent.
fn asked_agent(directory: &Directory, asked: &Value) -> Result<Option<String>, ApiError> {
    let refused = |code: &'static str, message: &str| {
        refusal(&clarp_kernel::notify_events::Refused {
            code,
            message: message.to_string(),
        })
    };
    match asked.get("agent") {
        None | Some(Value::Null) => Ok(None),
        Some(Value::String(key)) if !key.trim().is_empty() => directory
            .named(key.trim())
            .map(|member| Some(member.record.agent_id.clone()))
            .ok_or_else(|| refused("unknown_agent", "no such agent")),
        Some(_) => Err(refused(
            "invalid_agent",
            "agent must be a session or an agent id",
        )),
    }
}

/// Sets the ring switch.
async fn set_rings(app: &App, on: bool) -> Result<(), ApiError> {
    let desk = app.calls.clone();
    let set = desk.rings(app, |rings, _, with, (out, _)| {
        rings.change(&json!({ "enabled": on }), with.at.now_ms, out)
    });
    match set.await {
        Ok(Ok(_)) => Ok(()),
        Ok(Err(refused)) => {
            eprintln!("clarp-host: the ring switch was not set: {refused:?}");
            Err(ApiError::new(
                StatusCode::SERVICE_UNAVAILABLE,
                "the ring switch could not be set",
            ))
        }
        Err(unavailable) => Err(unavailable.into()),
    }
}

/// A change made through another route (the ring switch, an agent's mute) is a change of the
/// choices too: hold [`App::notification_writer`], call this before making it, so the version
/// grows once for it, then [`tell_changed`] once it is made. A version that grew for a change
/// that then failed tells nothing wrong: an app reads the same document again.
pub(crate) async fn next_version(app: &App) -> Result<(), crate::agent::Unavailable> {
    let mut kept = settings(&app.fleet.directory());
    kept.touch();
    app.fleet.set(vec![(KEY.to_string(), kept.kept())]).await
}

/// `POST /chat-read` with `{session}`: the chat is read, on every device. Its selection and
/// focus stay as they are. Answers `{ok, session, agent_id, at_ms}`, what the apps are told.
pub(crate) async fn chat_read(
    State(app): State<App>,
    Extension(_principal): Extension<Principal>,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    let asked = object(&body)?;
    let Value::Object(asked) = asked else {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "bad json"));
    };
    let session = crate::edge::body_text(&asked, "session")?;
    if session.is_empty() {
        return Err(ApiError::new(StatusCode::BAD_REQUEST, "session required"));
    }
    let directory = app.fleet.directory();
    let Some(member) = directory.named(session) else {
        return Err(ApiError::new(StatusCode::NOT_FOUND, "no such session"));
    };
    let record = &member.record;
    let now = app.clock.now_ms();
    let order = crate::pusher::Order::observe(now);
    let mut told = chat_read_event(record, now);
    order.boundary(&mut told);
    app.hub.publish(told.clone(), now);
    app.pusher.read(Read {
        order,
        session: record.session.clone(),
        decision_id: String::new(),
    });
    let mut answer = told;
    answer["ok"] = json!(true);
    if let Some(fields) = answer.as_object_mut() {
        fields.remove("type");
    }
    Ok(Json(answer))
}

/// The event every app is told a chat was read by.
pub(crate) fn chat_read_event(record: &AgentRecord, at_ms: i64) -> Value {
    json!({
        "type": "chat-read",
        "session": record.session,
        "agent_id": record.agent_id,
        "at_ms": at_ms,
    })
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn a_body_that_is_no_object_is_refused() {
        for body in ["", "[]", "\"x\"", "{"] {
            let refused = object(&Bytes::from(body)).unwrap_err();
            assert_eq!(refused.status(), StatusCode::BAD_REQUEST, "{body}");
        }
        assert!(object(&Bytes::from(r#"{"events":{}}"#)).is_ok());
    }

    #[test]
    fn a_chat_read_names_the_chat_and_the_moment() {
        let record = AgentRecord {
            agent_id: "a1".to_string(),
            session: "vera".to_string(),
            ..AgentRecord::default()
        };
        assert_eq!(
            chat_read_event(&record, 7),
            json!({"type": "chat-read", "session": "vera", "agent_id": "a1", "at_ms": 7})
        );
    }
}
