//! A stalled turn (Host contract 149, feature `stalled_turn`, #249): the
//! agent's tool has said nothing for 10 minutes while fresh user or Oracle
//! words wait behind it. The Host puts `stalled_since` (epoch ms, or null)
//! and `stalled_action` ("stop", or null) on the snapshot row and on the
//! `agent-activity` event when a stall starts and when it clears. The
//! desktop then offers its existing Stop. Only a Host that lists the
//! `stalled_turn` feature is read: an older one shows nothing new.
//!
//! Every word the desktop shows for it lives here, so the wording can change
//! in one place.

use serde_json::Value;

use crate::json::Object;
use crate::protocol::Agent;
use crate::tr;

// i18n-screen: chat

/// The Host's feature name in `/server-info` (`capabilities.features`).
pub const FEATURE: &str = "stalled_turn";

/// The chat list's status text for a stalled agent.
pub const ROW_STATUS: &str = "Seems stuck";

/// The indicator's Stop button: the existing Stop.
pub const STOP_LABEL: &str = "Stop";

/// `ROW_STATUS` in the app's language.
pub fn row_status() -> String {
    // i18n: chat list, status of an agent whose turn shows no progress for minutes
    tr!("Seems stuck")
}

/// The open chat's working indicator while the turn seems stuck: whole
/// minutes since `stalled_since`, and a line for the user's queued words.
pub fn indicator(stalled_since: i64, now_ms: i64, queued: bool) -> String {
    let minutes = (now_ms - stalled_since).max(0) / 60_000;
    // i18n: chat, working indicator of a turn with no progress; {} is a number of minutes
    let mut text = tr!("Seems stuck · no progress for {} min", minutes);
    if queued {
        text.push_str(" · ");
        // i18n: chat, after "Seems stuck": the user's message waits behind the stuck turn
        text.push_str(&tr!("Your message is waiting"));
    }
    text
}

/// Whether a `/server-info` answer offers the feature.
pub fn offered_by(info: &Object) -> bool {
    info.get("capabilities")
        .and_then(|c| c.get("features"))
        .and_then(Value::as_array)
        .is_some_and(|features| features.iter().any(|f| f.as_str() == Some(FEATURE)))
}

/// `stalled_since` from a row or event: a positive number, else None.
pub fn since(object: &Object) -> Option<i64> {
    object.get("stalled_since").and_then(Value::as_i64).filter(|t| *t > 0)
}

/// `stalled_action` from a row or event: a non-empty string, else None.
pub fn action(object: &Object) -> Option<String> {
    object.get("stalled_action").and_then(Value::as_str).filter(|a| !a.is_empty()).map(str::to_owned)
}

/// Whether the row or event says anything about a stall (a Host of
/// contract 149 or later always does, a clear included).
pub fn reported(object: &Object) -> bool {
    object.contains_key("stalled_since")
}

/// When the agent's stall began, if the Host offers Stop for it and the
/// turn still runs; None otherwise.
pub fn stop_offered(agent: &Agent) -> Option<i64> {
    match (agent.stalled_since, agent.stalled_action.as_deref()) {
        (Some(since), Some("stop")) if agent.busy => Some(since),
        _ => None,
    }
}
