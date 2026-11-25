//! A chat the owner put out of sight until a moment, the same on every app (issue 871).
//!
//! A snooze was each app's own, so a chat snoozed on the phone stood in the list on the
//! desktop. The moment is now kept on the agent's record (`snoozed_until_ms`) and shown on its
//! row (`snoozed_until`). The owner's apps set and clear it (`POST /agent-snooze`); an agent
//! never does. Every app is told of a change by an `agent-roster` event of the kind
//! [`KIND`] that carries the field (`roster_fields`).
//!
//! It ends by itself two ways, as a snooze on one app ended: when its moment comes (a timer
//! for each snooze, made again for those that stand when the Host starts), and when the agent
//! writes something for the user, which is what the Host tells the apps as a
//! `user-notification` (an answer the user is shown, or a note), muted or not.

use std::time::Duration;

use axum::body::Bytes;
use axum::extract::State;
use axum::http::{HeaderMap, StatusCode};
use axum::routing::post;
use axum::{Extension, Json, Router};
use serde_json::{Value, json};

use crate::edge::{self, ApiError, App, Principal};
use crate::fleet::{Amend, NotAmended};

/// The kind of the `agent-roster` event that tells a snooze, set or over.
pub const KIND: &str = "snooze-changed";

/// How far ahead a chat may be put out of sight: a year. A moment further on is a mistake.
const FURTHEST_MS: i64 = 366 * 24 * 60 * 60 * 1_000;

pub fn routes() -> Router<App> {
    Router::new().route("/agent-snooze", post(snooze))
}

fn refused(status: StatusCode, message: &str) -> ApiError {
    ApiError::new(status, message)
}

/// Whether a request comes from the owner: a paired device, or the Host's own token, and in
/// either case not from a turn of an agent (`X-Clarp-Turn`): a snooze is the owner's choice of
/// what he sees, never an agent's. The token of the agents' tools is shut out of this route
/// before it gets here.
fn from_the_owner(principal: &Principal, headers: &HeaderMap) -> bool {
    let owner = matches!(
        principal,
        Principal::Device { .. } | Principal::Administrator
    );
    owner && !headers.contains_key(crate::turn_identity::HEADER)
}

/// When a snooze asked for ends: `Ok(None)` brings the chat back (`until` null, absent or 0);
/// else a whole number of milliseconds since the epoch, after now and within a year.
fn until_of(asked: &serde_json::Map<String, Value>, now_ms: i64) -> Result<Option<i64>, ApiError> {
    let until = match asked.get("until") {
        None | Some(Value::Null) => return Ok(None),
        Some(Value::Number(number)) if !number.is_f64() => number.as_i64(),
        Some(_) => None,
    };
    let Some(until) = until else {
        return Err(refused(StatusCode::BAD_REQUEST, "until must be an integer"));
    };
    if until == 0 {
        return Ok(None);
    }
    if until <= now_ms {
        return Err(refused(
            StatusCode::BAD_REQUEST,
            "until is not in the future",
        ));
    }
    if until - now_ms > FURTHEST_MS {
        return Err(refused(
            StatusCode::BAD_REQUEST,
            "until is more than a year away",
        ));
    }
    Ok(Some(until))
}

/// `POST /agent-snooze` with `{session, until}`: puts the agent's chat out of sight on every
/// app until `until` (milliseconds since the epoch), or brings it back (`until` null or 0).
/// Answers `{ok, session, snoozed_until}`.
async fn snooze(
    State(app): State<App>,
    Extension(principal): Extension<Principal>,
    headers: HeaderMap,
    body: Bytes,
) -> Result<Json<Value>, ApiError> {
    if !from_the_owner(&principal, &headers) {
        return Err(refused(StatusCode::FORBIDDEN, "agents_may_not"));
    }
    let Ok(Value::Object(asked)) = serde_json::from_slice::<Value>(&body) else {
        return Err(refused(StatusCode::BAD_REQUEST, "bad json"));
    };
    let session = edge::body_text(&asked, "session")?;
    if session.is_empty() {
        return Err(refused(StatusCode::BAD_REQUEST, "session required"));
    }
    let until = until_of(&asked, app.clock.now_ms())?;
    let record = match app
        .fleet
        .amend(session.to_string(), Amend::Snooze(until))
        .await?
    {
        Ok(record) => record,
        Err(NotAmended::Unknown) => {
            return Err(refused(StatusCode::NOT_FOUND, "no such agent"));
        }
        Err(_) => return Err(refused(StatusCode::CONFLICT, "not snoozed")),
    };
    if let Some(until) = record.snoozed_until_ms {
        over_at(&app, &record.agent_id, until);
    }
    Ok(Json(json!({
        "ok": true,
        "session": record.session,
        "snoozed_until": record.snoozed_until_ms,
    })))
}

/// Brings the agent's chat back when its snooze's moment comes, where it is still snoozed
/// until that very moment ([`Amend::SnoozeOver`]): a snooze moved or ended since leaves the
/// timer nothing to do.
fn over_at(app: &App, agent_id: &str, until: i64) {
    let app = app.clone();
    let agent_id = agent_id.to_string();
    tokio::spawn(async move {
        let wait = until.saturating_sub(app.clock.now_ms()).max(0);
        tokio::time::sleep(Duration::from_millis(u64::try_from(wait).unwrap_or(0))).await;
        bring_back(&app, &agent_id, Amend::SnoozeOver(until)).await;
    });
}

async fn bring_back(app: &App, agent_id: &str, change: Amend) {
    match app.fleet.amend(agent_id.to_string(), change).await {
        Ok(Ok(_) | Err(NotAmended::Unknown)) => {}
        // Nothing is lost but this ending: the row still says the moment, which has passed.
        Ok(Err(_)) | Err(_) => {
            eprintln!("clarp-host: a snooze of {agent_id} could not be ended now");
        }
    }
}

/// Ends snoozes by themselves: a timer for each snooze that stands as the Host starts, and the
/// end of an agent's snooze when it writes something for the user.
pub fn run(app: &App) {
    let directory = app.fleet.directory();
    for member in &directory.members {
        if let Some(until) = member.record.snoozed_until_ms {
            over_at(app, &member.record.agent_id, until);
        }
    }
    let app = app.clone();
    tokio::spawn(async move {
        let (_, mut told) = app.hub.subscribe(None);
        loop {
            let published = match told.recv().await {
                Ok(published) => published,
                // What was missed is missed: a snooze it would have ended stands until its
                // moment or the agent's next message.
                Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
                Err(tokio::sync::broadcast::error::RecvError::Closed) => return,
            };
            if let Some(agent_id) = notified(&published.json) {
                let snoozed = app.fleet.directory().members.iter().any(|member| {
                    member.record.agent_id == agent_id && member.record.snoozed_until_ms.is_some()
                });
                if snoozed {
                    bring_back(&app, &agent_id, Amend::Snooze(None)).await;
                }
            }
        }
    });
}

/// The agent a `user-notification` is of: the agent wrote something for the user. Any other
/// event is of none. Most events are not one, and are passed over without being read whole.
fn notified(event: &str) -> Option<String> {
    if !event.contains("\"user-notification\"") {
        return None;
    }
    let event: Value = serde_json::from_str(event).ok()?;
    (event["type"] == "user-notification")
        .then(|| event["agent_id"].as_str().map(str::to_string))
        .flatten()
        .filter(|agent_id| !agent_id.is_empty())
}

#[cfg(test)]
#[allow(clippy::unwrap_used)]
mod tests {
    use super::*;

    fn asked(until: Value) -> serde_json::Map<String, Value> {
        let Value::Object(asked) = json!({ "session": "s", "until": until }) else {
            unreachable!()
        };
        asked
    }

    /// A snooze ends at a moment after now and within a year; null, 0 or no moment brings the
    /// chat back; anything else is refused.
    #[test]
    fn a_snooze_ends_at_a_whole_moment_ahead() {
        let now = 1_000_000;
        assert_eq!(
            until_of(&asked(json!(now + 60_000)), now).unwrap(),
            Some(now + 60_000)
        );
        assert_eq!(until_of(&asked(Value::Null), now).unwrap(), None);
        assert_eq!(until_of(&asked(json!(0)), now).unwrap(), None);
        assert_eq!(until_of(&serde_json::Map::new(), now).unwrap(), None);
        for wrong in [json!(now), json!(now - 1), json!(1.5e6), json!("soon")] {
            assert!(until_of(&asked(wrong.clone()), now).is_err(), "{wrong}");
        }
        assert!(until_of(&asked(json!(now + FURTHEST_MS + 1)), now).is_err());
    }

    /// Only the owner snoozes: a paired device or the Host's own token, from no turn of an
    /// agent; never the agents' tools.
    #[test]
    fn only_the_owner_snoozes() {
        let mut turn = HeaderMap::new();
        turn.insert(crate::turn_identity::HEADER, "t".parse().unwrap());
        let device = Principal::Device {
            device_id: "d".into(),
            scope: crate::pairing::Scope::Full,
        };
        let plain = HeaderMap::new();
        assert!(from_the_owner(&device, &plain));
        assert!(from_the_owner(&Principal::Administrator, &plain));
        assert!(!from_the_owner(&Principal::Administrator, &turn));
        assert!(!from_the_owner(&device, &turn));
        assert!(!from_the_owner(&Principal::Tools, &plain));
    }

    /// What the agent wrote for the user ends its snooze; no other event does.
    #[test]
    fn a_notification_names_the_agent_whose_snooze_ends() {
        let told = json!({"type": "user-notification", "agent_id": "a-1", "event_id": 4});
        assert_eq!(notified(&told.to_string()), Some("a-1".to_string()));
        let roster =
            json!({"type": "agent-roster", "kind": "user-notification", "agent_id": "a-1"});
        assert_eq!(notified(&roster.to_string()), None);
        let state = json!({"type": "agent-state", "agent_id": "a-1"});
        assert_eq!(notified(&state.to_string()), None);
    }
}
