//! What a phone is sent: the payload of a push.
//!
//! A push is sent for two things. A turn ended with something for the user: a banner like a
//! chat message, quiet unless the user still has something to answer. And an agent asks the
//! user something: the one thing it cannot finish without the user, so it is told when it is
//! asked, not when the turn ends.
//!
//! The apps read these payloads (the notification extension that shows the agent's portrait,
//! the link a tap follows, the reply typed from the banner), so they are the Python Host's,
//! field for field (`server/lib/apns.py`: `turn_done_payload`, `decision_payload`).

use serde_json::{Value, json};

/// What a banner says when there is nothing of the reply to show.
const DEFAULT_BODY: &str = "Done — your turn 👋";

/// The first `max` characters.
fn clip(text: &str, max: usize) -> String {
    text.chars().take(max).collect()
}

/// A finished turn, as a phone is told of it. An empty text is "not there".
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Told<'a> {
    pub persona: &'a str,
    pub session: &'a str,
    /// What the agent said, as a preview.
    pub body: &'a str,
    /// Where the agent's portrait is, for the device that is told.
    pub avatar_url: &'a str,
    /// The portrait is one the user gave the agent.
    pub avatar_custom: bool,
    pub notification_id: &'a str,
    /// Which Host this is, for an app that knows several.
    pub server_instance_id: &'a str,
    /// The user has something to answer: the push makes a sound.
    pub needs_response: bool,
}

/// The payload for a turn that ended.
pub fn turn_done(told: &Told<'_>) -> Value {
    let name = match told.persona.trim() {
        "" => "Clarp",
        name => name,
    };
    let body = if told.body.is_empty() {
        DEFAULT_BODY
    } else {
        told.body
    };
    let mut payload = json!({
        "aps": {
            "alert": { "title": name, "body": body },
            // A hint to refresh the conversation before the app is opened again.
            "content-available": 1,
            // The level of a chat message: it respects Focus and Do Not Disturb.
            "interruption-level": "active",
            // Lets the app's extension show the agent's portrait.
            "mutable-content": 1,
        },
        "kind": "user-notification",
        "session": told.session,
        "persona": name,
        "needs_response": told.needs_response,
    });
    if told.needs_response {
        payload["aps"]["sound"] = json!("default");
    }
    if !told.notification_id.is_empty() {
        payload["notification_id"] = json!(told.notification_id);
    }
    if !told.server_instance_id.is_empty() {
        payload["server_instance_id"] = json!(told.server_instance_id);
    }
    if !told.session.is_empty() {
        payload["aps"]["thread-id"] = json!(told.session);
    }
    if !told.avatar_url.is_empty() {
        payload["avatar_url"] = json!(told.avatar_url);
        if told.avatar_custom {
            payload["avatar_custom"] = json!(true);
        }
    }
    payload
}

/// A request to the user, as a phone is told of it.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Asked<'a> {
    pub persona: &'a str,
    pub session: &'a str,
    pub title: &'a str,
    pub question: &'a str,
    pub decision_id: &'a str,
    pub artifact_id: &'a str,
    /// The agent is blocked on it and said it cannot wait: it breaks through Focus.
    pub time_sensitive: bool,
    /// The answer is a text, which can be typed from the banner.
    pub text_input: bool,
    pub revision: i64,
    pub expires_at: Option<i64>,
    pub avatar_url: &'a str,
    pub avatar_custom: bool,
    pub server_instance_id: &'a str,
}

/// The name a request's push goes by: sent again, it takes the place of the first.
pub fn decision_notification_id(decision_id: &str) -> String {
    clip(&format!("decision-{decision_id}"), 64)
}

/// The payload for a request that was just made. A tap leads to the agent's conversation,
/// where the request is pinned.
pub fn decision(asked: &Asked<'_>) -> Value {
    let said = if asked.question.is_empty() {
        asked.title
    } else {
        asked.question
    };
    let body = clip(said.trim(), 500);
    let notification_id = decision_notification_id(asked.decision_id);
    let mut payload = turn_done(&Told {
        persona: asked.persona,
        session: asked.session,
        body: &body,
        avatar_url: asked.avatar_url,
        avatar_custom: asked.avatar_custom,
        notification_id: &notification_id,
        server_instance_id: asked.server_instance_id,
        // A request is something to answer: it always makes a sound.
        needs_response: true,
    });
    let heading = asked.title.trim();
    if !heading.is_empty() && payload["aps"]["alert"]["body"] != heading {
        payload["aps"]["alert"]["subtitle"] = json!(clip(heading, 120));
    }
    payload["aps"]["interruption-level"] = json!(if asked.time_sensitive {
        "time-sensitive"
    } else {
        "active"
    });
    payload["reason"] = json!("decision");
    if asked.text_input {
        payload["aps"]["category"] = json!("input-request");
        payload["response_type"] = json!("text_input");
        payload["revision"] = json!(asked.revision);
        if let Some(expires_at) = asked.expires_at {
            payload["expires_at"] = json!(expires_at);
        }
    }
    payload["decision_id"] = json!(asked.decision_id);
    payload["artifact_id"] = json!(asked.artifact_id);
    payload
}

/// Whether a request breaks through Focus: only when the agent is blocked on it and has
/// said that it cannot wait. Blocked alone, or urgent alone, is an ordinary notification.
pub fn needs_interruption(blocks_progress: bool, urgency: &str) -> bool {
    blocks_progress && urgency == "time_sensitive"
}

/// The id under which a later push replaces an earlier one. Every finished turn has its
/// own, so that replies that come close together do not replace each other; only the same
/// turn, sent again, collapses.
pub fn collapse_id(payload: &Value) -> String {
    let text = |key: &str| payload[key].as_str().unwrap_or("").trim();
    let notification_id = text("notification_id");
    if !notification_id.is_empty() {
        return clip(notification_id, 64);
    }
    match text("session") {
        "" => "turn-done".to_string(),
        session => clip(&format!("turn-done-{session}"), 64),
    }
}

/// A collapse id as the push service takes it: at most 64 bytes, cut between characters.
pub fn collapse_id_on_the_wire(collapse_id: &str) -> &str {
    let mut end = collapse_id.len().min(64);
    while !collapse_id.is_char_boundary(end) {
        end -= 1;
    }
    &collapse_id[..end]
}

#[cfg(test)]
#[allow(clippy::unwrap_used, clippy::expect_used)]
mod tests {
    use super::*;

    #[test]
    fn an_ordinary_reply_is_quiet_and_one_that_leaves_a_question_makes_a_sound() {
        let told = Told {
            persona: "Vera",
            session: "vera",
            body: "The build is green.",
            notification_id: "pn-1",
            ..Told::default()
        };
        let quiet = turn_done(&told);
        assert_eq!(quiet["aps"]["alert"]["body"], "The build is green.");
        assert_eq!(quiet["aps"]["thread-id"], "vera");
        assert!(quiet["aps"].get("sound").is_none());
        let loud = turn_done(&Told {
            needs_response: true,
            ..told
        });
        assert_eq!(loud["aps"]["sound"], "default");
        assert_eq!(loud["aps"]["interruption-level"], "active");
        assert_eq!(collapse_id(&loud), "pn-1");
    }

    #[test]
    fn a_collapse_id_is_cut_at_64_bytes_between_characters() {
        let long = "é".repeat(40);
        assert_eq!(collapse_id_on_the_wire(&long).len(), 64);
        let odd = format!("a{}", "é".repeat(40));
        assert_eq!(collapse_id_on_the_wire(&odd).len(), 63);
        assert_eq!(collapse_id_on_the_wire("short"), "short");
    }
}
